"""Post-extraction enrichment: queue parsing + order lookup + MECE classification.

Runs on rows at status='extracted', then advances them to status='classified'.
"""

from __future__ import annotations

from .db import connect
from .mece import classify_mece
from .queues import classify_queues


def enrich_extracted() -> int:
    """Run queues + mece passes on extracted rows, advance status to 'classified'."""
    n_queues = classify_queues()
    n_mece = classify_mece()

    with connect() as conn:
        cur = conn.execute(
            "UPDATE calls SET status = 'classified' "
            "WHERE status = 'extracted' AND category IS NOT NULL"
        )
        advanced = cur.rowcount

    print(
        f"  Queues: {n_queues} rows | MECE: {n_mece} rows | "
        f"advanced {advanced} → 'classified'."
    )
    return advanced
