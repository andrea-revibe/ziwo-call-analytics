"""One-shot backfill: reset rows whose transcript is a Gemini output-token loop.

Selection criteria: missing language trailer (transcript_language IS NULL/empty)
AND length(transcript) > 30000. Matches 11 rows as of 2026-04-22 — all clearly
looped (top line repeats thousands of times).

Action: null out transcript + all downstream extract/enrich columns, reset
status to 'downloaded' and transcribe_attempts to 0. The audio file on disk is
preserved so the next `python pipeline.py run --skip-fetch` re-transcribes it
against the new degenerate-output guards.

Usage:
    python scripts/reset_looped_transcripts.py [--dry-run]

DESTRUCTIVE — review --dry-run output before running without the flag.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ziwo.db import connect, init_db  # noqa: E402

OVERSIZE_CHARS = 30_000

# Every field downstream of a bad transcript must be cleared so the pipeline
# can regenerate them from scratch on the next run.
CLEAR_COLUMNS = [
    "transcript",
    "transcript_language",
    "transcribed_at",
    "transcript_translated_at",
    "intent_action",
    "intent_object",
    "intent_qualifier",
    "qualifier_theme",
    "call_summary",
    "sentiment",
    "resolution",
    "partial_reason",
    "escalation_requested",
    "extracted_at",
    "country",
    "language",
    "queue_intent",
    "object_bucket",
    "category",
    "subcategory",
    "friction_score",
    "queue_matches_category",
    "order_number",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be reset without touching the DB",
    )
    args = parser.parse_args()

    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, status, transcript_language,
                   length(transcript) AS tlen, talk_time
            FROM calls
            WHERE (transcript_language IS NULL OR transcript_language = '')
              AND transcript IS NOT NULL
              AND length(transcript) > ?
            ORDER BY tlen DESC
            """,
            (OVERSIZE_CHARS,),
        ).fetchall()

    if not rows:
        print("No looped transcripts found. Nothing to reset.")
        return

    print(f"Found {len(rows)} looped transcript(s) (tlen > {OVERSIZE_CHARS}).")
    status_breakdown: dict[str, int] = {}
    for r in rows:
        status_breakdown[r["status"]] = status_breakdown.get(r["status"], 0) + 1
    for status, n in sorted(status_breakdown.items()):
        print(f"  {status:15s} {n}")

    for r in rows:
        print(
            f"  id={r['id']:>7}  status={r['status']:<11}  "
            f"tlen={r['tlen']:>7}  talk_time={r['talk_time']}s"
        )

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return

    set_clause = ", ".join(f"{c} = NULL" for c in CLEAR_COLUMNS)
    set_clause += (
        ", status = 'downloaded'"
        ", transcribe_attempts = 0"
        ", error_message = NULL"
        ", updated_at = ?"
    )
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    now = datetime.utcnow().isoformat(timespec="seconds")

    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE calls SET {set_clause} WHERE id IN ({placeholders})",
            [now, *ids],
        )
        changed = cursor.rowcount

    print(f"\nReset {changed} row(s) to status='downloaded'.")
    print("Next step: python pipeline.py run --skip-fetch")


if __name__ == "__main__":
    main()
