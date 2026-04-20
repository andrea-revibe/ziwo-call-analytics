"""Fetch Ziwo callHistory for a single day, filter inbound + recorded, write CSV."""

import csv
import json
import os
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.environ["ZIWO_BASE_URL"].rstrip("/")
USERNAME = os.environ["ZIWO_USERNAME"]
PASSWORD = os.environ["ZIWO_PASSWORD"]
TARGET_DATE = os.environ.get("ZIWO_TARGET_DATE", "2026-04-10")

TZ = ZoneInfo("Africa/Cairo")
PAGE_SIZE = 100


def login() -> str:
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


def fetch_page(token: str, skip: int, limit: int = PAGE_SIZE) -> list[dict]:
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


def parse_started(val: str) -> datetime:
    return datetime.fromisoformat(val.replace("Z", "+00:00"))


def main() -> None:
    day_start = datetime.combine(
        datetime.strptime(TARGET_DATE, "%Y-%m-%d").date(), time.min, tzinfo=TZ
    )
    day_end = day_start + timedelta(days=1)
    print(f"Target window: {day_start.isoformat()} → {day_end.isoformat()}")

    token = login()
    print(f"Authenticated. Token: {token[:8]}…")

    # Discovery: dump one raw call so real field names are visible before full run.
    sample = fetch_page(token, skip=0, limit=1)
    if sample:
        print("\n=== Sample call (adjust field mapping below if needed) ===")
        print(json.dumps(sample[0], indent=2, default=str))
        print("=== End sample ===\n")
    else:
        print("No calls returned on sample fetch — nothing to do.")
        return

    rows: list[dict] = []
    skipped_too_new = 0
    skipped_not_inbound = 0
    skipped_no_recording = 0
    skip = 0

    while True:
        calls = fetch_page(token, skip=skip, limit=PAGE_SIZE)
        if not calls:
            break

        stop = False
        for call in calls:
            started_raw = call.get("startedAt")
            if not started_raw:
                continue
            started = parse_started(started_raw).astimezone(TZ)

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

    exports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "exports")
    os.makedirs(exports_dir, exist_ok=True)
    out_path = os.path.join(exports_dir, f"calls_{TARGET_DATE}.csv")
    if rows:
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    print("\nDone.")
    print(f"  kept:                   {len(rows)}")
    print(f"  skipped (too new):      {skipped_too_new}")
    print(f"  skipped (not inbound):  {skipped_not_inbound}")
    print(f"  skipped (no recording): {skipped_no_recording}")
    print(f"  output:                 {out_path}")


if __name__ == "__main__":
    main()
