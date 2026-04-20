import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY,
    direction TEXT,
    queue_name TEXT,
    started_at TEXT,
    audio_quality INTEGER,
    caller_id_number TEXT,
    duration INTEGER,
    talk_time INTEGER,
    ring_time INTEGER,
    agent_id INTEGER,
    agent_cc_login TEXT,
    agent_first_name TEXT,
    agent_last_name TEXT,
    recording_file TEXT,

    status TEXT NOT NULL DEFAULT 'pending',
    audio_path TEXT,
    transcript TEXT,
    transcript_language TEXT,
    transcribed_at TEXT,
    error_message TEXT,

    intent_action TEXT,
    intent_object TEXT,
    intent_qualifier TEXT,
    qualifier_theme TEXT,
    call_summary TEXT,
    sentiment TEXT,
    resolution TEXT,
    escalation_requested INTEGER,
    extracted_at TEXT,

    country TEXT,
    language TEXT,
    queue_intent TEXT,

    object_bucket TEXT,
    category TEXT,
    subcategory TEXT,
    friction_score INTEGER,
    queue_matches_category TEXT,
    order_number TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
"""

EXTENSION_COLUMNS = {
    "intent_action": "TEXT",
    "intent_object": "TEXT",
    "intent_qualifier": "TEXT",
    "qualifier_theme": "TEXT",
    "call_summary": "TEXT",
    "sentiment": "TEXT",
    "resolution": "TEXT",
    "escalation_requested": "INTEGER",
    "extracted_at": "TEXT",
    "country": "TEXT",
    "language": "TEXT",
    "queue_intent": "TEXT",
    "object_bucket": "TEXT",
    "category": "TEXT",
    "subcategory": "TEXT",
    "friction_score": "INTEGER",
    "queue_matches_category": "TEXT",
    "order_number": "TEXT",
}


def _existing_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(calls)")}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = _existing_columns(conn)
    for col, col_type in EXTENSION_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE calls ADD COLUMN {col} {col_type}")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def insert_if_new(conn: sqlite3.Connection, row: dict) -> bool:
    existing = conn.execute("SELECT 1 FROM calls WHERE id = ?", (row["id"],)).fetchone()
    if existing:
        return False
    now = datetime.utcnow().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO calls (
            id, direction, queue_name, started_at, audio_quality,
            caller_id_number, duration, talk_time, ring_time,
            agent_id, agent_cc_login, agent_first_name, agent_last_name,
            recording_file, status, created_at, updated_at
        ) VALUES (
            :id, :direction, :queue_name, :started_at, :audio_quality,
            :caller_id_number, :duration, :talk_time, :ring_time,
            :agent_id, :agent_cc_login, :agent_first_name, :agent_last_name,
            :recording_file, 'pending', :now, :now
        )
        """,
        {**row, "now": now},
    )
    return True


def update_status(
    conn: sqlite3.Connection, call_id: int, status: str, **fields
) -> None:
    fields["status"] = status
    fields["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(
        f"UPDATE calls SET {set_clause} WHERE id = :id",
        {**fields, "id": call_id},
    )


def list_by_status(
    conn: sqlite3.Connection, status: str, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM calls WHERE status = ? ORDER BY started_at"
    params: list = [status]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def status_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT status, COUNT(*) AS n FROM calls GROUP BY status ORDER BY status"
    ).fetchall()
