"""Cohort audit and snapshot tool for the CC Improvement Programme pre-read.

Defines the 7 cohort filters as Python functions, computes per-cohort KPIs
(volume, friction, escalation, repeat-caller, impact score, per-country split),
and writes a JSON snapshot for diffing against future runs after pipeline changes.

The cohort filters are the source of truth for the pre-read document. Any change
to a filter should bump the methodology_version and be recorded in CHANGELOG.md.

Usage:
    # Default (replicates the pre-read baseline):
    python audit_cohorts.py

    # Custom date range / data dir / output:
    python audit_cohorts.py --start 2026-04-09 --end 2026-04-22 \\
        --data-dir /Users/andreagrossi/Desktop/ziwo-dashboard/data \\
        --output baseline_2026-04-09_to_2026-04-22_v1.0.json \\
        --methodology-version v1.0

    # Audit precision (samples N calls per cohort and prints summaries):
    python audit_cohorts.py --audit 20 --seed 1337
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import random
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

DEFAULT_DATA_DIR = Path.home() / "Desktop" / "ziwo-dashboard" / "data"


# =====================================================================
# Cohort filter definitions — single source of truth for the pre-read
# =====================================================================
# Each filter takes a single CSV row (dict) and returns True if the call
# belongs in the cohort. All regexes are case-insensitive.
#
# Methodology version v1.0 (2026-04-27) — these are the filters that
# produced the baseline snapshot. Bump version + add CHANGELOG entry on
# any change.


def del01(r: dict) -> bool:
    """DEL-01 Carrier Tracking Granularity.

    Pure taxonomy filter. Calls about delivery/carrier tracking, either
    at the order-tracking level (Status Inquiry) or about a shipping
    provider problem.

    Audit precision: ~85% (qualifier_theme classification noise — some
    product-info calls slip in. To be addressed by upstream pipeline change.)
    """
    return ((r["subcategory"] == "Order Tracking" and r["qualifier_theme"] == "Status Inquiry")
         or (r["subcategory"] == "Delivery Issues" and r["qualifier_theme"] == "Shipping Provider Issue"))


_TIGHT_REF01 = re.compile(
    r"refund[^\.]{0,60}(delayed|long[- ]delayed|overdue|still pending|still waiting|"
    r"not (yet )?received|haven'?t received|hasn'?t (been )?received|never received|"
    r"pending for|stuck|exceeded|past (the )?10|over (a )?(month|10)|more than (10|a month)|"
    r"month ago|weeks ago|month prior|month later|three months|two months)|"
    r"(delayed|long[- ]delayed|overdue|not (yet )?received|haven'?t received|hasn'?t (been )?received|"
    r"never received|still waiting|still pending|pending|stuck)[^\.]{0,60}refund|"
    r"10[- ]day refund|10 working day refund|refund.{0,30}10 (working )?days?",
    re.I,
)


def ref01(r: dict) -> bool:
    """REF-01 PSP Refund Velocity.

    Refund Processing calls referencing extended/overdue refund processing
    timelines. Tightened from the original loose substring filter after
    audit showed 50-60% precision (false matches on 'not received' for
    delayed phones, etc.). Tight regex requires 'refund' in proximity to
    a delay/timing phrase.

    Audit precision: ~94%.
    """
    if r["qualifier_theme"] != "Refund Processing":
        return False
    return bool(_TIGHT_REF01.search(r.get("call_summary", "") or ""))


_TIGHT_REF02 = re.compile(
    r"store credit instead|credit instead.{0,40}(original|bank|payment|method)|"
    r"(want|wanted|expect|expecting|requested|request|prefer|instead).{0,40}(original|bank).{0,30}(payment|method|card|account|refund|money)|"
    r"switch.{0,30}(credit|method|payment)|"
    r"change.{0,30}(refund|payment).{0,20}method|"
    r"(refund|credit|amount) (was|already)? ?(issued|processed|sent|credited|returned) (as|to)? ?(store credit|wallet|gift card)|"
    r"(store credit|wallet)[^\.]{0,50}(redeemed|already used|disputed|unusable|cannot use|can't use)|"
    r"refund (back )?(to|into) (my )?(original|bank|account|card)|"
    r"(bank|cash) refund.{0,50}(instead of|rather than)|"
    r"(received|got) store credit",
    re.I,
)


def ref02(r: dict) -> bool:
    """REF-02 Refund Choice & Visibility.

    Refund Processing calls expressing dissatisfaction with the refund
    method received, or asking to switch. Tightened from a loose substring
    filter after audit showed 50-65% precision (false matches on routine
    'refund processed to original payment method' status updates). Tight
    regex requires an explicit problem signal.

    Audit precision: ~92%.
    """
    if r["qualifier_theme"] != "Refund Processing":
        return False
    return bool(_TIGHT_REF02.search(r.get("call_summary", "") or ""))


def del02(r: dict) -> bool:
    """DEL-02 Customs Hold Resolution.

    Delivery Issues calls referencing customs anywhere in the summary.
    Audit precision: ~94%.
    """
    if r["subcategory"] != "Delivery Issues":
        return False
    return "customs" in (r.get("call_summary", "") or "").lower()


def del03(r: dict) -> bool:
    """DEL-03 Callback Promise Reliability.

    Calls labeled by the analytics pipeline as having a customer follow-up
    commitment. CAVEAT: the current pipeline label is broad — audit shows
    ~30-50% of labelled calls don't actually involve Revibe-side outbound
    contact (agent advice, customer-side action, internal-only escalation).
    The 949-call cohort and impact score 1,861 should be read as upper
    bounds. This is the upstream label that the planned pipeline change
    targets.

    Audit precision: ~66% strict / ~90% inclusive of broad commitments.
    """
    return r.get("partial_reason") == "callback_promised"


def ret01(r: dict) -> bool:
    """RET-01 Warranty QC Visibility.

    Pure taxonomy filter — calls labelled 'Quality Check Wait'.
    Audit precision: ~99%.
    """
    return r["qualifier_theme"] == "Quality Check Wait"


_TIGHT_RETURN = re.compile(
    r"return (request|claim|attempt)s?[^\.]{0,50}(reject|cancel|closed)|"
    r"(reject|cancel|closed)[^\.]{0,50}return (request|claim|attempt)s?|"
    r"(reject|cancel)[^\.]{0,30}(in the|by the) (system|app|website)|"
    r"missed the 24[- ]hour|"
    r"return claim was (closed|cancelled|rejected)|"
    r"previous return.{0,30}(reject|cancel)|"
    r"closed without resolution",
    re.I,
)

_TIGHT_WARRANTY = re.compile(
    r"warranty (claim|request|return)s?[^\.]{0,50}(reject|cancel|closed)|"
    r"(reject|cancel|closed)[^\.]{0,50}warranty (claim|request)s?|"
    r"missed the 24[- ]hour|"
    r"closed without resolution",
    re.I,
)


def ret02(r: dict) -> bool:
    """RET-02 Procedural Claim Recovery.

    Return Process OR Warranty Claims calls where the claim was procedurally
    rejected/cancelled/auto-closed. Two parallel regexes, one per qualifier
    theme, because the same procedural mechanism appears under both.
    Audit precision: ~100%.
    """
    s = r.get("call_summary", "") or ""
    return ((r["qualifier_theme"] == "Return Process" and bool(_TIGHT_RETURN.search(s)))
         or (r["subcategory"] == "Warranty Claims" and bool(_TIGHT_WARRANTY.search(s))))


# Cohort registry — preserves the rank order from the pre-read.
COHORTS: list[tuple[str, str, Callable[[dict], bool]]] = [
    ("DEL-03", "Callback Promise Reliability", del03),
    ("DEL-01", "Carrier Tracking Granularity", del01),
    ("REF-01", "PSP Refund Velocity", ref01),
    ("RET-02", "Procedural Claim Recovery (Returns + Warranty)", ret02),
    ("DEL-02", "Customs Hold Resolution", del02),
    ("RET-01", "Warranty QC Visibility", ret01),
    ("REF-02", "Refund Choice & Visibility", ref02),
]


# =====================================================================
# Data loading
# =====================================================================

def load_calls(data_dir: Path, start_date: str | None, end_date: str | None) -> list[dict]:
    """Load and dedupe calls from CSV files. Filename format: calls_YYYY-MM-DD.csv.

    Dedupe is by call_id, first occurrence wins (matches dashboard's data-loader.js).
    """
    rows: list[dict] = []
    seen: set[str] = set()
    files = sorted(glob.glob(str(Path(data_dir) / "calls_*.csv")))
    if not files:
        sys.exit(f"No calls_*.csv files found in {data_dir}")

    for f in files:
        date_str = Path(f).stem.split("_")[1]
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                cid = r.get("call_id")
                if cid and cid not in seen:
                    seen.add(cid)
                    rows.append(r)
    return rows


# =====================================================================
# KPI computation
# =====================================================================

def compute_kpis(subset: list[dict], country_totals: dict[str, int]) -> dict:
    n = len(subset)
    if n == 0:
        return {"n": 0}

    fr = [float(r["friction_score"]) for r in subset if r.get("friction_score")]
    avg_friction = round(sum(fr) / len(fr), 2) if fr else 0.0

    n_esc = sum(1 for r in subset if r.get("escalation_requested") == "1")

    callers: dict[str, int] = defaultdict(int)
    for r in subset:
        cid = (r.get("caller_id_number") or "").strip()
        if cid:
            callers[cid] += 1
    n_unique_callers = len(callers)
    n_repeat_callers = sum(1 for v in callers.values() if v >= 2)
    n_calls_from_repeaters = sum(v for v in callers.values() if v >= 2)

    impact_score = round(n * (avg_friction + 2 * n_esc / n))

    by_country: dict[str, dict] = {}
    for c in ["UAE", "KSA", "ZA"]:
        sub_c = [r for r in subset if r.get("country") == c]
        n_c = len(sub_c)
        pct = round(100 * n_c / country_totals[c], 1) if country_totals.get(c) else 0.0
        n_esc_c = sum(1 for r in sub_c if r.get("escalation_requested") == "1")
        by_country[c] = {
            "n": n_c,
            "pct_of_country": pct,
            "escalation_pct": round(100 * n_esc_c / n_c, 1) if n_c else 0.0,
        }

    return {
        "n": n,
        "avg_friction": avg_friction,
        "escalation_pct": round(100 * n_esc / n, 1),
        "repeat_caller_pct_of_calls": round(100 * n_calls_from_repeaters / n, 1),
        "repeat_caller_pct_of_callers": round(100 * n_repeat_callers / n_unique_callers, 1) if n_unique_callers else 0.0,
        "n_unique_callers": n_unique_callers,
        "impact_score": impact_score,
        "by_country": by_country,
    }


# =====================================================================
# Optional precision audit (sample summaries)
# =====================================================================

def audit_samples(subset: list[dict], n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    sampled = rng.sample(subset, min(n, len(subset)))
    return [
        {
            "call_id": r.get("call_id"),
            "country": r.get("country"),
            "subcategory": r.get("subcategory"),
            "qualifier_theme": r.get("qualifier_theme"),
            "friction_score": r.get("friction_score"),
            "resolution": r.get("resolution"),
            "escalation_requested": r.get("escalation_requested"),
            "partial_reason": r.get("partial_reason"),
            "call_summary": r.get("call_summary") or "",
        }
        for r in sampled
    ]


# =====================================================================
# Main
# =====================================================================

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Directory containing calls_YYYY-MM-DD.csv files")
    p.add_argument("--start", default="2026-04-09", help="Start date (YYYY-MM-DD, inclusive)")
    p.add_argument("--end", default="2026-04-22", help="End date (YYYY-MM-DD, inclusive)")
    p.add_argument("--output", default=None, help="Output JSON path (default: baseline_<start>_to_<end>_<version>.json)")
    p.add_argument("--methodology-version", default="v1.0", help="Methodology version label written into the snapshot")
    p.add_argument("--audit", type=int, default=0, help="If >0, sample N calls per cohort for precision audit and write to a separate _audit.json file")
    p.add_argument("--seed", type=int, default=1337, help="Random seed for the audit sample")
    args = p.parse_args()

    rows = load_calls(Path(args.data_dir), args.start, args.end)
    print(f"Loaded {len(rows)} unique calls from {args.start} to {args.end}")

    country_totals: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("country"):
            country_totals[r["country"]] += 1
    print(f"Country split: {dict(country_totals)}")

    snapshot = {
        "methodology_version": args.methodology_version,
        "data_window": {"start": args.start, "end": args.end},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_calls": len(rows),
        "country_totals": dict(country_totals),
        "cohorts": {},
    }

    audit_data = {}

    print()
    print(f"{'CODE':<7} {'NAME':<48} {'n':>5} {'fric':>5} {'esc%':>5} {'rep%':>5} {'impact':>7}")
    print("-" * 90)
    for code, name, fn in COHORTS:
        subset = [r for r in rows if fn(r)]
        kpis = compute_kpis(subset, country_totals)
        snapshot["cohorts"][code] = {"name": name, "kpis": kpis}
        if args.audit > 0:
            audit_data[code] = audit_samples(subset, args.audit, args.seed)
        print(
            f"{code:<7} {name:<48} {kpis['n']:>5} "
            f"{kpis.get('avg_friction', 0):>5} "
            f"{kpis.get('escalation_pct', 0):>5} "
            f"{kpis.get('repeat_caller_pct_of_calls', 0):>5} "
            f"{kpis.get('impact_score', 0):>7}"
        )

    output_path = args.output or f"baseline_{args.start}_to_{args.end}_{args.methodology_version}.json"
    with open(output_path, "w") as fh:
        json.dump(snapshot, fh, indent=2)
    print(f"\nSnapshot written to: {output_path}")

    if args.audit > 0:
        audit_path = output_path.replace(".json", "_audit.json")
        with open(audit_path, "w") as fh:
            json.dump(
                {
                    "methodology_version": args.methodology_version,
                    "data_window": snapshot["data_window"],
                    "audit_seed": args.seed,
                    "audit_n_per_cohort": args.audit,
                    "samples": audit_data,
                },
                fh,
                indent=2,
            )
        print(f"Audit samples written to: {audit_path}")


if __name__ == "__main__":
    main()
