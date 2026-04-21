"""Fetch Ziwo callHistory for a single day, filter inbound + recorded, write CSV."""

import csv
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .config import BASE_URL, EXPORTS_DIR, PASSWORD, USERNAME, ensure_dirs

TZ = ZoneInfo("Africa/Cairo")
PAGE_SIZE = 100


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    token = (data.get("content") or {}).get("access_token") or data.get("access_token")
    if not token:
        sys.exit(f"No access_token in login response: {json.dumps(data)[:500]}")
    return token


def _fetch_page(token: str, skip: int, limit: int = PAGE_SIZE) -> list[dict]:
    r = requests.get(
        f"{BASE_URL}/callHistory/",
        params=[
            ("dataset", "tags"),
            ("dataset", "notes"),
            ("order", "~startedAt"),
            ("skip", skip),
            ("limit", limit),
        ],
        headers={"access_token": token, "Accept": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    calls = data.get("content") if isinstance(data, dict) else data
    return calls or []


def _parse_started(val: str) -> datetime:
    return datetime.fromisoformat(val.replace("Z", "+00:00"))


def fetch_calls_for_date(target_date: str) -> dict:
    """Fetch inbound calls with recordings for target_date (YYYY-MM-DD), write CSV.

    Returns a dict with kept/skipped counts and the output CSV path.
    Prints page-by-page progress to stdout.
    """
    if not BASE_URL or not USERNAME or not PASSWORD:
        raise RuntimeError(
            "ZIWO_BASE_URL / ZIWO_USERNAME / ZIWO_PASSWORD must be set in .env"
        )

    day_start = datetime.combine(
        datetime.strptime(target_date, "%Y-%m-%d").date(), time.min, tzinfo=TZ
    )
    day_end = day_start + timedelta(days=1)
    print(f"Target window: {day_start.isoformat()} → {day_end.isoformat()}")

    token = _login()
    print(f"Authenticated. Token: {token[:8]}…")

    rows: list[dict] = []
    skipped_too_new = 0
    skipped_not_inbound = 0
    skipped_no_recording = 0
    skip = 0

    while True:
        calls = _fetch_page(token, skip=skip, limit=PAGE_SIZE)
        if not calls:
            break

        stop = False
        for call in calls:
            started_raw = call.get("startedAt")
            if not started_raw:
                continue
            started = _parse_started(started_raw).astimezone(TZ)

            if started >= day_end:
                skipped_too_new += 1
                continue
            if started < day_start:
                stop = True
                break

            if (call.get("direction") or "").lower() != "inbound":
                skipped_not_inbound += 1
                continue

            recording = call.get("recordingFile")
            if not recording:
                skipped_no_recording += 1
                continue

            rec_id = recording.split(".")[0]
            recording_url = (
                f"{BASE_URL}/callHistory/{rec_id}/recording?access_token={token}"
            )

            agent_id = call.get("agentId")
            agent_first = agent_last = agent_cc_login = None
            if agent_id is not None:
                queue_agents = (
                    ((call.get("extendedInfo") or {}).get("queues") or {}).get(
                        "agents"
                    )
                    or []
                )
                for a in queue_agents:
                    if a.get("id") == agent_id:
                        agent_first = a.get("firstName")
                        agent_last = a.get("lastName")
                        agent_cc_login = a.get("ccLogin")
                        break

            rows.append(
                {
                    "id": call.get("id"),
                    "direction": call.get("direction"),
                    "queueName": call.get("queueName"),
                    "startedAt": started.isoformat(),
                    "audioQuality": call.get("audioQuality"),
                    "callerIDNumber": call.get("callerIDNumber"),
                    "duration": call.get("duration"),
                    "talkTime": call.get("talkTime"),
                    "ringTime": call.get("ringTime"),
                    "agentId": agent_id,
                    "agentCCLogin": agent_cc_login,
                    "agentFirstName": agent_first,
                    "agentLastName": agent_last,
                    "recordingFile": recording,
                    "recordingUrl": recording_url,
                }
            )

        print(f"  page skip={skip}: kept {len(rows)} so far")
        if stop:
            break
        skip += PAGE_SIZE

    ensure_dirs()
    out_path = Path(EXPORTS_DIR) / f"calls_{target_date}.csv"
    if rows:
        with out_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    return {
        "kept": len(rows),
        "skipped_too_new": skipped_too_new,
        "skipped_not_inbound": skipped_not_inbound,
        "skipped_no_recording": skipped_no_recording,
        "output_path": out_path,
    }
