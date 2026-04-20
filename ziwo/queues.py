"""Deterministic parsing of Ziwo queue names into country / language / queue_intent.

Also performs MySQL lookup to link caller phone numbers to customer order numbers.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pymysql

from .config import MYSQL_DATABASE, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER
from .db import connect

COUNTRIES = {"ZA", "UAE", "KSA"}
LANGUAGES = {"EN", "AR"}

QUEUE_TYPES = {
    "Want-to-buy": "Want to Buy",
    "Order-tracking": "Order Tracking",
    "Warranty": "Warranty",
    "Other": "Other",
}

QUEUE_INTENT_TO_CATEGORY = {
    "Want to Buy": "Pre-Purchase",
    "Order Tracking": "Order Management",
    "Warranty": "After-Sales Support",
}

# How far back to look for orders relative to the call date
ORDER_LOOKBACK_DAYS = 60


def parse_queue_name(
    queue_name: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Return (country, language, queue_intent). All-None if unparseable or NULL."""
    if not queue_name:
        return None, None, None
    parts = queue_name.split("_")
    country = parts[0]
    if country not in COUNTRIES:
        return None, None, None

    if country == "ZA":
        if len(parts) != 2:
            return None, None, None
        language = "EN"
        queue_seg = parts[1]
    else:
        if len(parts) != 3 or parts[1] not in LANGUAGES:
            return None, None, None
        language = parts[1]
        queue_seg = parts[2]

    queue_intent = QUEUE_TYPES.get(queue_seg)
    if queue_intent is None:
        return None, None, None
    return country, language, queue_intent


def queue_matches_category(
    queue_intent: str | None, category: str | None
) -> str | None:
    """Yes/No if comparable; None if queue_intent is 'Other' or either input is missing."""
    if not queue_intent or not category:
        return None
    expected = QUEUE_INTENT_TO_CATEGORY.get(queue_intent)
    if expected is None:
        return None
    return "Yes" if category == expected else "No"


def _normalize_phone(raw: str | None) -> str | None:
    """Strip non-digits and return the last 9 digits (matching MySQL RIGHT(...,9) logic)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 9:
        return None
    return digits[-9:]


def _mysql_available() -> bool:
    return bool(MYSQL_HOST and MYSQL_USER and MYSQL_DATABASE)


def _lookup_orders(
    phones: dict[str, list[int]], call_dates: dict[int, str]
) -> dict[int, str]:
    """Query MySQL for order numbers matching normalized phone suffixes.

    Args:
        phones: mapping of normalized-9-digit phone → list of call IDs sharing that phone
        call_dates: mapping of call ID → started_at ISO string

    Returns:
        mapping of call ID → comma-separated order number(s)
    """
    if not phones:
        return {}

    conn = pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=30,
    )
    try:
        result: dict[int, str] = {}
        phone_list = list(phones.keys())

        # Find the earliest call date across all calls, subtract lookback
        all_dates = []
        for d in call_dates.values():
            try:
                all_dates.append(datetime.fromisoformat(d))
            except (ValueError, TypeError):
                pass
        if not all_dates:
            return {}
        earliest_cutoff = (min(all_dates) - timedelta(days=ORDER_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%d"
        )

        with conn.cursor() as cur:
            # Batch query: find all orders whose customer phone (last 9 digits) matches
            placeholders = ", ".join(["%s"] * len(phone_list))
            sql = f"""
                SELECT
                    order_number,
                    o.created_at,
                    RIGHT(TRIM(REPLACE(c.phone_number, ' ', '')), 9) AS phone
                FROM orders o
                LEFT JOIN order_customers c ON o.id = c.order_id
                WHERE RIGHT(TRIM(REPLACE(c.phone_number, ' ', '')), 9) IN ({placeholders})
                  AND o.created_at >= %s
                ORDER BY o.created_at DESC
            """
            cur.execute(sql, [*phone_list, earliest_cutoff])
            rows = cur.fetchall()

        # Group order numbers by normalized phone, with created_at date
        phone_orders: dict[str, list[str]] = {}
        seen: dict[str, set[str]] = {}
        for row in rows:
            ph = row["phone"]
            on = str(row["order_number"])
            if ph not in seen:
                seen[ph] = set()
                phone_orders[ph] = []
            if on not in seen[ph]:
                seen[ph].add(on)
                created = row["created_at"]
                if created:
                    date_str = str(created)[:10]
                    phone_orders[ph].append(f"{on} ({date_str})")
                else:
                    phone_orders[ph].append(on)

        # Map back to call IDs
        for ph, call_ids in phones.items():
            orders = phone_orders.get(ph, [])
            if orders:
                order_str = ", ".join(orders)
                for cid in call_ids:
                    result[cid] = order_str

        return result
    finally:
        conn.close()


def classify_queues() -> int:
    """Populate country/language/queue_intent and order_number for every call row. Idempotent."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, queue_name, caller_id_number, started_at FROM calls"
        ).fetchall()

        # Phase 1: deterministic queue parsing (always runs)
        for row in rows:
            country, language, queue_intent = parse_queue_name(row["queue_name"])
            conn.execute(
                "UPDATE calls SET country = ?, language = ?, queue_intent = ? "
                "WHERE id = ?",
                (country, language, queue_intent, row["id"]),
            )

        # Phase 2: MySQL order lookup (only if configured)
        if _mysql_available():
            phones: dict[str, list[int]] = {}
            call_dates: dict[int, str] = {}
            for row in rows:
                norm = _normalize_phone(row["caller_id_number"])
                if norm:
                    phones.setdefault(norm, []).append(row["id"])
                    call_dates[row["id"]] = row["started_at"]

            try:
                order_map = _lookup_orders(phones, call_dates)
                for call_id, order_num in order_map.items():
                    conn.execute(
                        "UPDATE calls SET order_number = ? WHERE id = ?",
                        (order_num, call_id),
                    )
                print(f"  Order lookup: matched {len(order_map)}/{len(rows)} calls.")
            except Exception as e:
                print(f"  Warning: MySQL order lookup failed: {e}")
                print("  Queue classification completed without order numbers.")
        else:
            print("  MySQL not configured — skipping order lookup.")

    return len(rows)
