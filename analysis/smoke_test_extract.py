"""Smoke test — re-extract one day's calls with the CURRENT prompt, diff against DB.

Reads transcripts from calls.db for a given date, runs each through the current
ziwo.extract.PROMPT (whatever is in extract.py right now), and compares the new
extraction against the existing extraction stored in the DB. Writes a JSON diff
report and prints a summary.

Does NOT modify the DB. Safe to run repeatedly.

Use this to validate prompt changes before triggering a full re-extraction:
the diff report shows whether the labels shifted in the expected direction
(e.g., fewer callback_promised, fewer Status Inquiry on product-info calls).

Usage:
    # Smoke test on Apr 22 (biggest day, ~288 calls)
    python smoke_test_extract.py --date 2026-04-22

    # Quick sample (60 calls — ~5 sec, costs cents)
    python smoke_test_extract.py --date 2026-04-22 --limit 60

    # Custom output and version label
    python smoke_test_extract.py --date 2026-04-22 \\
        --methodology-version v1.1 \\
        --output smoke_2026-04-22_v1.1.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Use the pipeline's own modules (parent of analysis/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402
from tqdm import tqdm  # noqa: E402

from ziwo.config import DB_PATH, EXTRACT_CONCURRENCY, GEMINI_API_KEY  # noqa: E402
from ziwo.extract import _extract_one  # noqa: E402


COMPARE_FIELDS = [
    "qualifier_theme",
    "partial_reason",
    "resolution",
    "intent_action",
    "sentiment",
    "escalation_requested",
]


def load_calls_for_date(date: str, limit: int | None = None) -> list[dict]:
    """Load calls for a given date that have transcripts AND existing extraction."""
    sql = f"""
        SELECT id, started_at, country, transcript, subcategory,
               call_summary, intent_action, intent_object, intent_qualifier,
               qualifier_theme, sentiment, resolution, partial_reason,
               escalation_requested
          FROM calls
         WHERE substr(started_at, 1, 10) = ?
           AND transcript IS NOT NULL AND transcript != ''
           AND status IN ('extracted', 'classified')
         ORDER BY id
         {f'LIMIT {int(limit)}' if limit else ''}
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (date,)).fetchall()
    return [dict(r) for r in rows]


def run_diff(date: str, limit: int | None) -> dict:
    if not GEMINI_API_KEY:
        sys.exit("GEMINI_API_KEY not set in .env")

    rows = load_calls_for_date(date, limit)
    if not rows:
        sys.exit(f"No extracted calls found for {date}")
    print(f"Loaded {len(rows)} calls for {date}")

    client = genai.Client(api_key=GEMINI_API_KEY)
    new_results: dict[int, dict] = {}
    workers = max(1, EXTRACT_CONCURRENCY)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_extract_one, client, r["id"], r["transcript"]): r for r in rows}
        for f in tqdm(as_completed(futures), total=len(futures), desc="re-extracting", unit="call"):
            res = f.result()
            new_results[res["call_id"]] = res

    diffs: list[dict] = []
    n_failed = 0
    for old in rows:
        cid = old["id"]
        new = new_results.get(cid)
        if not new or not new.get("ok"):
            n_failed += 1
            continue
        np = new["parsed"]
        new_dict = {
            "qualifier_theme": np.qualifier_theme,
            "partial_reason": np.partial_reason,
            "resolution": np.resolution,
            "intent_action": np.intent_action,
            "sentiment": np.sentiment,
            "escalation_requested": int(np.escalation_requested),
        }
        # Coerce DB escalation to int for fair comparison (stored as int)
        old_dict = {k: old[k] for k in COMPARE_FIELDS}
        if old_dict["escalation_requested"] is not None:
            old_dict["escalation_requested"] = int(old_dict["escalation_requested"])
        changed = [k for k in COMPARE_FIELDS if old_dict[k] != new_dict[k]]
        diffs.append({
            "call_id": cid,
            "country": old["country"],
            "subcategory": old["subcategory"],
            "summary_excerpt": (old["call_summary"] or "")[:240],
            "transcript_excerpt": (old["transcript"] or "")[:400],
            "old": old_dict,
            "new": new_dict,
            "fields_changed": changed,
        })

    n_succeeded = len(diffs)
    n_changed = sum(1 for d in diffs if d["fields_changed"])

    field_change_counts: dict[str, int] = defaultdict(int)
    for d in diffs:
        for f in d["fields_changed"]:
            field_change_counts[f] += 1

    def transitions(field: str) -> dict:
        out: dict[str, int] = defaultdict(int)
        for d in diffs:
            if field in d["fields_changed"]:
                old_v = str(d["old"][field]) if d["old"][field] is not None else "(null)"
                new_v = str(d["new"][field]) if d["new"][field] is not None else "(null)"
                out[f"{old_v} → {new_v}"] += 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    n_cb_old = sum(1 for d in diffs if d["old"]["partial_reason"] == "callback_promised")
    n_cb_new = sum(1 for d in diffs if d["new"]["partial_reason"] == "callback_promised")
    n_si_old = sum(1 for d in diffs if d["old"]["qualifier_theme"] == "Status Inquiry")
    n_si_new = sum(1 for d in diffs if d["new"]["qualifier_theme"] == "Status Inquiry")
    n_spi_old = sum(1 for d in diffs if d["old"]["qualifier_theme"] == "Shipping Provider Issue")
    n_spi_new = sum(1 for d in diffs if d["new"]["qualifier_theme"] == "Shipping Provider Issue")

    return {
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_calls": len(rows),
        "extraction_succeeded": n_succeeded,
        "extraction_failed": n_failed,
        "calls_with_any_change": n_changed,
        "calls_with_any_change_pct": round(100 * n_changed / n_succeeded, 1) if n_succeeded else 0,
        "field_change_counts": dict(field_change_counts),
        "callback_promised": {
            "old_count": n_cb_old,
            "new_count": n_cb_new,
            "delta": n_cb_new - n_cb_old,
            "pct_change": round(100 * (n_cb_new - n_cb_old) / n_cb_old, 1) if n_cb_old else 0,
        },
        "status_inquiry": {
            "old_count": n_si_old,
            "new_count": n_si_new,
            "delta": n_si_new - n_si_old,
            "pct_change": round(100 * (n_si_new - n_si_old) / n_si_old, 1) if n_si_old else 0,
        },
        "shipping_provider_issue": {
            "old_count": n_spi_old,
            "new_count": n_spi_new,
            "delta": n_spi_new - n_spi_old,
        },
        "partial_reason_transitions": transitions("partial_reason"),
        "qualifier_theme_transitions": transitions("qualifier_theme"),
        "all_diffs": diffs,
    }


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--date", required=True, help="Date YYYY-MM-DD to re-extract")
    p.add_argument("--limit", type=int, default=None, help="Sample only first N calls")
    p.add_argument("--output", default=None, help="Output JSON path")
    p.add_argument("--methodology-version", default="v1.1", help="Label for the new prompt version")
    args = p.parse_args()

    report = run_diff(args.date, args.limit)
    report["methodology_version"] = args.methodology_version

    output = args.output or f"smoke_{args.date}_{args.methodology_version}.json"
    with open(output, "w") as fh:
        json.dump(report, fh, indent=2)

    n_total = report["total_calls"]
    n_ok = report["extraction_succeeded"]
    n_chg = report["calls_with_any_change"]
    cb = report["callback_promised"]
    si = report["status_inquiry"]
    spi = report["shipping_provider_issue"]

    print()
    print("=" * 72)
    print(f"SMOKE TEST RESULT — {args.date} ({n_ok}/{n_total} extracted successfully)")
    print("=" * 72)
    print(f"\nCalls with ANY field change:  {n_chg} / {n_ok}  ({100*n_chg/n_ok:.0f}%)")
    print("\nField change counts:")
    for f, c in sorted(report["field_change_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {f:<25} {c}")
    print()
    print(f"callback_promised:  {cb['old_count']:>3} → {cb['new_count']:>3}  "
          f"({cb['delta']:+d}, {cb['pct_change']:+.0f}%)")
    print(f"  TARGET: −30 to −50%. {'PASS' if -50 <= cb['pct_change'] <= -25 else 'INVESTIGATE'}")
    print()
    print(f"Status Inquiry:     {si['old_count']:>3} → {si['new_count']:>3}  "
          f"({si['delta']:+d}, {si['pct_change']:+.0f}%)")
    print(f"  TARGET: −10 to −20%. {'PASS' if -25 <= si['pct_change'] <= -5 else 'INVESTIGATE'}")
    print()
    print(f"Shipping Provider Issue: {spi['old_count']} → {spi['new_count']} ({spi['delta']:+d})")
    print(f"  Should be roughly stable.")
    print()
    print("Top partial_reason transitions:")
    for transition, n in list(report["partial_reason_transitions"].items())[:8]:
        print(f"  {n:>3}  {transition}")
    print("\nTop qualifier_theme transitions:")
    for transition, n in list(report["qualifier_theme_transitions"].items())[:8]:
        print(f"  {n:>3}  {transition}")
    print(f"\nFull report written to: {output}")
    print("Open it to inspect individual call diffs (see 'all_diffs' array).")


if __name__ == "__main__":
    main()
