"""One-shot backfill: clean filler tags from all existing transcripts.

Usage:
    python scripts/clean_transcripts.py [--dry-run]
"""

import argparse
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ziwo.db import connect, init_db
from ziwo.transcribe import clean_transcript


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to the DB",
    )
    args = parser.parse_args()

    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, transcript FROM calls WHERE transcript IS NOT NULL"
        ).fetchall()

        changed = 0
        saved_chars = 0
        for row in rows:
            original = row["transcript"]
            cleaned = clean_transcript(original)
            if cleaned != original:
                diff = len(original) - len(cleaned)
                changed += 1
                saved_chars += diff
                if args.dry_run:
                    print(f"  call {row['id']}: {len(original):,} → {len(cleaned):,} chars (−{diff:,})")
                else:
                    conn.execute(
                        "UPDATE calls SET transcript = ? WHERE id = ?",
                        (cleaned, row["id"]),
                    )

    action = "Would update" if args.dry_run else "Updated"
    print(f"{action} {changed}/{len(rows)} transcripts, saving {saved_chars:,} characters.")


if __name__ == "__main__":
    main()
