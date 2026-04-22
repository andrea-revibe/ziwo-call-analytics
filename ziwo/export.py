"""Dashboard CSV export + manifest regeneration.

Exports rows at status='classified' for a given date into
~/Desktop/ziwo-dashboard/data/calls_<date>.csv, then rewrites index.json
so the dashboard's Date Range picker sees the new file.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .db import connect

csv.field_size_limit(sys.maxsize)

DASHBOARD_DATA_DIR = Path.home() / "Desktop" / "ziwo-dashboard" / "data"

# (db_column, csv_header) — order matches the dashboard contract.
COLUMNS: list[tuple[str, str]] = [
    ("id",                     "call_id"),
    ("started_at",             "started_at"),
    ("duration",               "duration"),
    ("talk_time",              "talk_time"),
    ("ring_time",              "ring_time"),
    ("queue_name",             "queue_name"),
    ("agent_id",               "agent_id"),
    ("agent_cc_login",         "agent_cc_login"),
    ("agent_first_name",       "agent_first_name"),
    ("agent_last_name",        "agent_last_name"),
    ("caller_id_number",       "caller_id_number"),
    ("transcript_language",    "transcript_language"),
    ("call_summary",           "call_summary"),
    ("intent_action",          "intent_action"),
    ("intent_object",          "intent_object"),
    ("intent_qualifier",       "intent_qualifier"),
    ("qualifier_theme",        "qualifier_theme"),
    ("sentiment",              "sentiment"),
    ("resolution",             "resolution"),
    ("partial_reason",         "partial_reason"),
    ("escalation_requested",   "escalation_requested"),
    ("object_bucket",          "object_bucket"),
    ("category",               "category"),
    ("subcategory",            "subcategory"),
    ("friction_score",         "friction_score"),
    ("country",                "country"),
    ("language",               "language"),
    ("queue_intent",           "queue_intent"),
    ("queue_matches_category", "queue_matches_category"),
    ("order_number",           "order_number"),
    ("transcript",             "transcript"),
]

FILENAME_RE = re.compile(r"^calls_(\d{4}-\d{2}-\d{2})\.csv$")


def _write_csv(target_date: str, out_dir: Path) -> tuple[Path, int, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"calls_{target_date}.csv"

    db_cols = ", ".join(c[0] for c in COLUMNS)
    header = [c[1] for c in COLUMNS]

    with connect() as conn:
        rows = conn.execute(
            f"SELECT {db_cols} FROM calls "
            "WHERE status = 'classified' "
            "  AND date(started_at) = ? "
            "ORDER BY started_at",
            (target_date,),
        )
        count = 0
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(header)
            for row in rows:
                writer.writerow([row[c[0]] for c in COLUMNS])
                count += 1

    size_kb = round(out_path.stat().st_size / 1024)
    return out_path, count, size_kb


def _rebuild_manifest(data_dir: Path) -> dict:
    entries = []
    for p in data_dir.iterdir():
        m = FILENAME_RE.match(p.name)
        if not m:
            continue
        with p.open("r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            row_count = sum(1 for _ in reader)
        entries.append({
            "filename": p.name,
            "date": m.group(1),
            "row_count": row_count,
            "size_kb": round(p.stat().st_size / 1024),
        })
    entries.sort(key=lambda e: e["date"])

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_date": entries[0]["date"] if entries else None,
        "max_date": entries[-1]["date"] if entries else None,
        "files": entries,
    }
    (data_dir / "index.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def export_dashboard(target_date: str, data_dir: Path = DASHBOARD_DATA_DIR) -> dict:
    out_path, rows, size_kb = _write_csv(target_date, data_dir)
    manifest = _rebuild_manifest(data_dir)
    return {
        "csv_path": out_path,
        "rows": rows,
        "size_kb": size_kb,
        "manifest_files": len(manifest["files"]),
        "min_date": manifest["min_date"],
        "max_date": manifest["max_date"],
    }
