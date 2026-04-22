"""Ziwo call analytics pipeline CLI.

Usage:
    python pipeline.py fetch
    python pipeline.py ingest [--csv PATH]
    python pipeline.py download [--limit N]
    python pipeline.py transcribe [--limit N]
    python pipeline.py extract [--limit N]
    python pipeline.py enrich
    python pipeline.py export [--date YYYY-MM-DD]
    python pipeline.py run [--limit N] [--skip-fetch]
    python pipeline.py status
    python pipeline.py show <call_id>
"""

import argparse
from pathlib import Path

from ziwo.config import EXPORTS_DIR, TARGET_DATE
from ziwo.db import connect, init_db, status_counts
from ziwo.download import download_pending
from ziwo.enrich import enrich_extracted
from ziwo.export import export_dashboard
from ziwo.extract import extract_transcribed
from ziwo.fetch import fetch_calls_for_date
from ziwo.ingest import MIN_TALK_TIME, ingest_csv
from ziwo.transcribe import transcribe_downloaded


def _default_csv() -> Path:
    return EXPORTS_DIR / f"calls_{TARGET_DATE}.csv"


def cmd_fetch(_args: argparse.Namespace) -> None:
    r = fetch_calls_for_date(TARGET_DATE)
    print(
        f"Fetched {r['kept']} calls → {r['output_path']} "
        f"(skipped: too-new={r['skipped_too_new']}, "
        f"not-inbound={r['skipped_not_inbound']}, "
        f"no-recording={r['skipped_no_recording']})"
    )


def cmd_ingest(args: argparse.Namespace) -> None:
    csv_path = Path(args.csv) if args.csv else _default_csv()
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")
    inserted, excluded_short = ingest_csv(csv_path)
    print(
        f"Ingested {inserted} new rows, skipped {excluded_short} short calls "
        f"(<{MIN_TALK_TIME}s talk_time) from {csv_path}"
    )


def cmd_download(args: argparse.Namespace) -> None:
    done, failed = download_pending(limit=args.limit)
    print(f"Downloaded {done} mp3s, {failed} failed.")


def cmd_transcribe(args: argparse.Namespace) -> None:
    done, failed = transcribe_downloaded(limit=args.limit)
    print(f"Transcribed {done} calls, {failed} failed.")


def cmd_extract(args: argparse.Namespace) -> None:
    done, failed = extract_transcribed(limit=args.limit)
    print(f"Extracted {done} calls, {failed} failed.")


def cmd_enrich(_args: argparse.Namespace) -> None:
    n = enrich_extracted()
    print(f"Enriched {n} calls (queues + mece) and advanced status to 'classified'.")


def cmd_export(args: argparse.Namespace) -> None:
    date = args.date or TARGET_DATE
    r = export_dashboard(date)
    print(f"[export] {r['rows']} rows -> {r['csv_path']} ({r['size_kb']} KB)")
    print(
        f"[manifest] {r['manifest_files']} files "
        f"(min={r['min_date']}, max={r['max_date']})"
    )


def cmd_run(args: argparse.Namespace) -> None:
    if not args.skip_fetch:
        r = fetch_calls_for_date(TARGET_DATE)
        print(
            f"Fetched {r['kept']} calls → {r['output_path']} "
            f"(skipped: too-new={r['skipped_too_new']}, "
            f"not-inbound={r['skipped_not_inbound']}, "
            f"no-recording={r['skipped_no_recording']})"
        )
    csv_path = _default_csv()
    if csv_path.exists():
        inserted, excluded_short = ingest_csv(csv_path)
        print(
            f"Ingested {inserted} new rows, skipped {excluded_short} short calls "
            f"(<{MIN_TALK_TIME}s talk_time) from {csv_path}"
        )
    d_done, d_failed = download_pending(limit=args.limit)
    print(f"Downloaded {d_done} mp3s, {d_failed} failed.")
    t_done, t_failed = transcribe_downloaded(limit=args.limit)
    print(f"Transcribed {t_done} calls, {t_failed} failed.")
    e_done, e_failed = extract_transcribed(limit=args.limit)
    print(f"Extracted {e_done} calls, {e_failed} failed.")
    n_enriched = enrich_extracted()
    print(f"Enriched {n_enriched} calls (queues + mece) and advanced status to 'classified'.")


def cmd_status(_args: argparse.Namespace) -> None:
    init_db()
    with connect() as conn:
        rows = status_counts(conn)
        if not rows:
            print("No calls in database yet. Run: python pipeline.py ingest")
            return
        print("Status counts:")
        for r in rows:
            print(f"  {r['status']:15s} {r['n']}")


def cmd_show(args: argparse.Namespace) -> None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM calls WHERE id = ?", (args.call_id,)
        ).fetchone()
    if not row:
        raise SystemExit(f"Call {args.call_id} not found")
    print(f"Call {row['id']}  [{row['status']}]")
    print(f"  started_at: {row['started_at']}")
    print(f"  agent:      {row['agent_first_name']} {row['agent_last_name']} ({row['agent_cc_login']})")
    print(f"  queue:      {row['queue_name']}")
    print(f"  duration:   {row['duration']}s (talk {row['talk_time']}s)")
    print(f"  language:   {row['transcript_language']}")
    if row["error_message"]:
        print(f"  error:      {row['error_message']}")
    if row["intent_action"]:
        print(f"\nSummary:    {row['call_summary']}")
        print(
            f"Intent:     {row['intent_action']} | {row['intent_object']} | {row['intent_qualifier']}"
        )
        print(f"Theme:      {row['qualifier_theme']}")
        print(f"Sentiment:  {row['sentiment']}")
        res_line = row['resolution']
        if row['resolution'] == 'Partial' and row['partial_reason']:
            res_line = f"{res_line} ({row['partial_reason']})"
        print(f"Resolution: {res_line}")
    if row["transcript"]:
        print("\n--- Transcript ---")
        print(row["transcript"])


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch Ziwo callHistory for TARGET_DATE → CSV")
    p_fetch.set_defaults(func=cmd_fetch)

    p_ing = sub.add_parser("ingest", help="Load CSV rows into SQLite")
    p_ing.add_argument("--csv", help=f"CSV path (default: {_default_csv()})")
    p_ing.set_defaults(func=cmd_ingest)

    p_dl = sub.add_parser("download", help="Download mp3s for pending calls")
    p_dl.add_argument("--limit", type=int, default=None)
    p_dl.set_defaults(func=cmd_download)

    p_tx = sub.add_parser("transcribe", help="Transcribe downloaded mp3s with Gemini")
    p_tx.add_argument("--limit", type=int, default=None)
    p_tx.set_defaults(func=cmd_transcribe)

    p_ex = sub.add_parser("extract", help="Extract intent/sentiment/resolution from transcripts")
    p_ex.add_argument("--limit", type=int, default=None)
    p_ex.set_defaults(func=cmd_extract)

    p_en = sub.add_parser(
        "enrich",
        help="Post-extract: queues + mece passes; advance status 'extracted' → 'classified'",
    )
    p_en.set_defaults(func=cmd_enrich)

    p_exp = sub.add_parser("export", help="Export dashboard CSV + rebuild index.json")
    p_exp.add_argument("--date", help=f"Date to export (default: {TARGET_DATE})")
    p_exp.set_defaults(func=cmd_export)

    p_run = sub.add_parser(
        "run",
        help="Fetch + ingest + download + transcribe + extract + enrich in order",
    )
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip the Ziwo API fetch step (use the existing CSV on disk)",
    )
    p_run.set_defaults(func=cmd_run)

    p_st = sub.add_parser("status", help="Show pipeline status counts")
    p_st.set_defaults(func=cmd_status)

    p_show = sub.add_parser("show", help="Print one call (metadata + transcript)")
    p_show.add_argument("call_id", type=int)
    p_show.set_defaults(func=cmd_show)

    args = p.parse_args()
    init_db()
    args.func(args)


if __name__ == "__main__":
    main()
