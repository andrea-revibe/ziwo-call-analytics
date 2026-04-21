import csv
from pathlib import Path

from .db import connect, init_db, insert_if_new

CSV_TO_DB = {
    "id": "id",
    "direction": "direction",
    "queueName": "queue_name",
    "startedAt": "started_at",
    "audioQuality": "audio_quality",
    "callerIDNumber": "caller_id_number",
    "duration": "duration",
    "talkTime": "talk_time",
    "ringTime": "ring_time",
    "agentId": "agent_id",
    "agentCCLogin": "agent_cc_login",
    "agentFirstName": "agent_first_name",
    "agentLastName": "agent_last_name",
    "recordingFile": "recording_file",
}

INT_FIELDS = {"id", "audio_quality", "duration", "talk_time", "ring_time", "agent_id"}

MIN_TALK_TIME = 45


def _coerce(db_field: str, value: str):
    if value is None or value == "":
        return None
    if db_field in INT_FIELDS:
        return int(value)
    return value


def ingest_csv(csv_path: Path) -> tuple[int, int]:
    init_db()
    inserted = 0
    excluded_short = 0
    with open(csv_path, newline="") as f, connect() as conn:
        reader = csv.DictReader(f)
        for csv_row in reader:
            db_row = {
                db_key: _coerce(db_key, csv_row.get(csv_key))
                for csv_key, db_key in CSV_TO_DB.items()
            }
            if db_row["id"] is None:
                continue
            talk_time = db_row.get("talk_time")
            if talk_time is not None and talk_time < MIN_TALK_TIME:
                excluded_short += 1
                continue
            if insert_if_new(conn, db_row):
                inserted += 1
    return inserted, excluded_short
