"""One-shot backfill: hard-delete calls with talk_time < 30 seconds.

Also removes the corresponding data/audio/{id}.mp3 files if present.

Usage:
    python scripts/delete_short_calls.py [--dry-run]

DESTRUCTIVE — review --dry-run output before running without the flag.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ziwo.config import AUDIO_DIR  # noqa: E402
from ziwo.db import connect, init_db  # noqa: E402
from ziwo.ingest import MIN_TALK_TIME  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be deleted without touching the DB or disk")
    args = parser.parse_args()

    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, talk_time, audio_path, status FROM calls "
            "WHERE talk_time IS NOT NULL AND talk_time < ? "
            "ORDER BY id",
            (MIN_TALK_TIME,),
        ).fetchall()

    if not rows:
        print(f"No calls with talk_time < {MIN_TALK_TIME}s. Nothing to delete.")
        return

    print(f"Found {len(rows)} call(s) with talk_time < {MIN_TALK_TIME}s.")
    status_breakdown: dict[str, int] = {}
    for r in rows:
        status_breakdown[r["status"]] = status_breakdown.get(r["status"], 0) + 1
    for status, n in sorted(status_breakdown.items()):
        print(f"  {status:15s} {n}")

    for r in rows[:10]:
        print(f"  sample: id={r['id']} talk_time={r['talk_time']}s status={r['status']}")
    if len(rows) > 10:
        print(f"  ... (+{len(rows) - 10} more)")

    if args.dry_run:
        print("\n--dry-run: no changes made.")
        return

    deleted_rows = 0
    deleted_files = 0
    with connect() as conn:
        for r in rows:
            call_id = r["id"]
            audio_candidates = []
            if r["audio_path"]:
                audio_candidates.append(Path(r["audio_path"]))
            audio_candidates.append(AUDIO_DIR / f"{call_id}.mp3")
            for p in audio_candidates:
                if p.exists():
                    p.unlink()
                    deleted_files += 1
                    break
            conn.execute("DELETE FROM calls WHERE id = ?", (call_id,))
            deleted_rows += 1

    print(f"\nDeleted {deleted_rows} row(s) and {deleted_files} audio file(s).")


if __name__ == "__main__":
    main()
