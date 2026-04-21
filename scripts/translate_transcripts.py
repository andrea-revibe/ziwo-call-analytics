"""Text-to-text backfill: translate existing non-English transcripts into English.

Overwrites the `transcript` column in place. `transcript_language` is preserved
so we retain a record of the source language. Idempotent via
`transcript_translated_at` — already-translated rows are skipped on re-run.

Scope: rows where transcript IS NOT NULL AND transcript_language IN
('ar','ar-en','other') OR transcript_language IS NULL. Rows with
transcript_language = 'en' are left alone.

Usage:
    python scripts/translate_transcripts.py [--dry-run] [--limit N] [--call-id ID]
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai  # noqa: E402
from tqdm import tqdm  # noqa: E402

from ziwo.config import GEMINI_API_KEY, GEMINI_MODEL  # noqa: E402
from ziwo.db import connect, init_db  # noqa: E402


TRANSLATE_PROMPT = """You will receive a transcript of a customer-support phone call, currently in Egyptian Arabic, mixed Arabic/English, or another non-English language.

Translate every turn into natural, fluent English. Preserve meaning, tone, and any proper nouns (names, brands, product names, cities). Preserve the "Agent:" / "Customer:" / "Speaker:" turn labels exactly — one label per line, at the start of the line. Do not summarize. Do not add content that is not in the source transcript. Do not add explanations or commentary.

If a line is already in English, keep it as-is. Keep bracketed stage tags like [Hold music] unchanged.

Output plain text only — no markdown, no code fences, no preamble.

Transcript to translate:
---
{transcript}
"""


def _select_sql(call_id: int | None) -> tuple[str, list]:
    if call_id is not None:
        return (
            "SELECT id, transcript, transcript_language FROM calls "
            "WHERE id = ? AND transcript IS NOT NULL",
            [call_id],
        )
    return (
        "SELECT id, transcript, transcript_language FROM calls "
        "WHERE transcript IS NOT NULL "
        "AND transcript_translated_at IS NULL "
        "AND (transcript_language IN ('ar','ar-en','other') "
        "     OR transcript_language IS NULL) "
        "ORDER BY id",
        [],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would run without calling Gemini or writing")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--call-id", type=int, default=None,
                        help="Translate a single call by id (useful for spot-check)")
    args = parser.parse_args()

    if not args.dry_run and not GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY is not set in .env")

    init_db()
    sql, params = _select_sql(args.call_id)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    if args.limit is not None:
        rows = rows[: args.limit]

    if not rows:
        print("No transcripts match the translation criteria. Nothing to do.")
        return

    print(f"Selected {len(rows)} transcript(s) for translation.")
    if args.dry_run:
        for row in rows[:20]:
            tl = row["transcript_language"] or "NULL"
            size = len(row["transcript"])
            print(f"  call {row['id']}: lang={tl}, {size:,} chars")
        if len(rows) > 20:
            print(f"  ... (+{len(rows) - 20} more)")
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    translated = failed = 0

    for row in tqdm(rows, desc="translating", unit="call"):
        call_id = row["id"]
        original = row["transcript"]
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[TRANSLATE_PROMPT.format(transcript=original)],
            )
            text = (response.text or "").strip()
            if not text:
                raise RuntimeError("empty Gemini response")
            now = datetime.utcnow().isoformat(timespec="seconds")
            with connect() as conn:
                conn.execute(
                    "UPDATE calls SET transcript = ?, transcript_translated_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (text, now, now, call_id),
                )
            translated += 1
        except Exception as e:
            tqdm.write(f"  call {call_id}: FAILED — {e}")
            failed += 1

    print(f"Translated {translated} transcripts, {failed} failed.")


if __name__ == "__main__":
    main()
