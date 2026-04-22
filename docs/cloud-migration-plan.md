# Cloud Migration Plan — Daily Pipeline on a GCE VM

**Status:** Planned, not started.
**Drafted:** 2026-04-20

## Problem

Today the pipeline is a manual daily ritual:

1. User sets the date, runs `python pipeline.py run` locally.
2. mp3s pile up on the laptop (no retention).
3. Generated CSV is copy-pasted (without headers) into a Google Sheet that feeds the dashboard.
4. Nothing runs if the laptop is asleep, travelling, or offline.

Two hard constraints pushing a migration:

- **≤10 min/day manual work** — today the Sheets append alone eats ~5 min.
- **No local storage/processing issues** — mp3s must leave the laptop.

## Goal

A fully automated daily run: previous-day's calls are fetched, processed, and appended to the dashboard sheet by the time the user opens their laptop in the morning. A short email summarises counts and flags failures.

## Scope

Intended for **indefinite production use**, not a throwaway POC:

- **Volume:** ~400–500 calls/day steady-state, with rare external-event spikes (e.g. war-driven); spikes are not an architectural driver.
- **Operators:** single person (the user). No CI/CD, no team handoff tooling.
- **Readers:** Google Sheet is read-only for the dashboard; no other concurrent DB consumers → SQLite stays viable indefinitely.
- **Legal:** no PII/erasure plumbing required.
- **Downtime tolerance:** a missed day is acceptable provided the next morning's run catches up automatically.
- **Tenancy:** single-tenant forever.

These constraints let us keep the architecture simple; the only production hardening beyond the baseline is spelled out explicitly below.

## Decision summary

| Concern | Choice | Why not the alternative |
| --- | --- | --- |
| Compute | **GCE VM (e2-small, ~$13/mo)** running cron | Cloud Run Job requires containerising + migrating SQLite → Postgres; not worth the complexity for a single-tenant batch job. e2-small (over e2-micro free tier) gives 2 GB RAM headroom for occasional spikes and avoids the free-tier-expiry cliff. Laptop-cron ruled out: laptop is not always on. |
| State DB | **SQLite on VM persistent disk** + daily `.backup` → GCS + **scheduled PD snapshots** | No concurrent readers (Sheet is the dashboard's only source), so SQLite is fine indefinitely at 400–500/day. Two backup layers (app-level dump + disk-level snapshot) cover the "disk dies between snapshots" window. |
| mp3 storage | **GCS bucket** with 30-day lifecycle rule on `mp3s/` prefix | Native auto-deletion, no OAuth, no Drive quota. |
| Transcripts / extracted rows | Keep forever in SQLite + daily snapshot to GCS | User requirement. |
| Revibe MySQL access | Direct from VM (public IP, no VPN confirmed) | No network plumbing needed. |
| Sheets append | **Sheets API + service account**, idempotent via new DB column `pushed_to_sheet_at` | Reading the whole sheet each run to dedupe is slow; DB-side tracking is cleaner. |
| Cutover | **3-day parallel run**, cloud writes to a second tab; compare; switch on day 4 | User cap. |
| Notifications | **Email** (Gmail SMTP w/ app password); recipients configurable | No team Slack exists yet. |
| Secrets | `.env` file on VM (SSH-only access); upgrade to Secret Manager later | Simplest; VM isn't public-facing. |

## Target architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       GCE VM (e2-micro)                          │
│  ┌───────────────┐  cron 06:00 local  ┌────────────────────┐    │
│  │ pipeline.py   │◀───────────────────│ /etc/cron.d/ziwo   │    │
│  │    run        │                    └────────────────────┘    │
│  └──────┬────────┘                                               │
│         │                                                        │
│         │  ingest (Ziwo API)                                     │
│         │  download mp3 ──────────────────────────────────┐      │
│         │  transcribe (Gemini)                            │      │
│         │  extract (Gemini)  ┌──────────────────┐         │      │
│         │  enrich ◀──────────│ Revibe MySQL     │         │      │
│         │   (queues+mece)    └──────────────────┘         │      │
│         │  push_sheets ──────────────┐                    │      │
│         │  report ─────────────┐     │                    │      │
│         ▼                      │     │                    │      │
│  /opt/ziwo/data/calls.db       │     │                    │      │
│         │ (daily snapshot)     │     │                    │      │
│         ▼                      │     ▼                    ▼      │
└─────────┼──────────────────────┼─────┼──────────── ┌─────────┐   │
          │                      │     │              │   GCS   │   │
          ▼                      │     │              │ mp3s/   │◀──┘
  ┌───────────────┐              │     │              │ (30-day │
  │   GCS         │              │     │              │ TTL)    │
  │ backups/      │              │     │              │ backups/│
  │ calls.db.gz   │              │     │              └─────────┘
  └───────────────┘              │     ▼
                                 │  ┌──────────────────┐
                                 │  │ Google Sheet     │
                                 │  │ (dashboard feed) │
                                 │  └──────────────────┘
                                 ▼
                          ┌──────────────────┐
                          │ Email (SMTP)     │
                          │ daily report     │
                          └──────────────────┘
```

## Code changes

### 1. `ziwo/storage.py` (new) — GCS mp3 layer

Thin wrapper around `google-cloud-storage`:

```python
def upload_mp3(local_path: Path, call_id: str) -> str:
    """Upload to gs://{bucket}/mp3s/{call_id}.mp3, return gs:// URI."""

def download_mp3(call_id: str, local_path: Path) -> None:
    """Fetch back for re-processing if needed."""
```

`download.py` changes: after fetching the mp3 from Ziwo, upload to GCS, store the `gs://` URI in `audio_path`, delete the local copy. `transcribe.py` downloads to a temp file if the path is a `gs://` URI.

### 2. `ziwo/sheets.py` (new) — idempotent append

```python
def push_unpushed_rows(conn, spreadsheet_id: str, sheet_name: str) -> int:
    """
    Select rows where pushed_to_sheet_at IS NULL and status = 'classified'.
    Append to sheet via Sheets API.
    Mark pushed_to_sheet_at = now() on success.
    Returns number of rows pushed.
    """
```

New DB column in `EXTENSION_COLUMNS` (and `SCHEMA`): `pushed_to_sheet_at TIMESTAMP`.

During the 3-day parallel window, the cloud pipeline writes to a *second tab* (e.g. `cloud_auto`); after cutover, it writes to the existing tab.

### 3. `ziwo/report.py` (new) — email summary

Sends a per-run email with:

- Date processed.
- Counts: calls ingested, with audio, downloaded, transcribed, extracted, classified (enriched), pushed to sheet, failed.
- Top 3 failure reasons (from `error_message`).
- Sanity checks: any step where `count < expected` (e.g. extracted < downloaded by >10%).
- Dashboard link.

SMTP via Gmail app password (simplest). Recipients list from env var.

### 4. `pipeline.py run` — extend, default-date, and catch-up

Confirm (and fix if needed) that `run` with no `--date` arg defaults to **yesterday in the VM's configured timezone**. Add `push_sheets` and `report` as final steps:

```
run = ingest → download → transcribe → extract → enrich → push_sheets → report
```

**Catch-up logic:** if the VM was down or the pipeline failed, the next morning's run should heal itself. Before defaulting to "yesterday," the cron-invoked entrypoint should check the last N=7 days and process any date where `COUNT(calls WHERE call_date=X) = 0` **or** where `COUNT(calls WHERE call_date=X AND pushed_to_sheet_at IS NULL) > 0`. Process missing dates oldest-first, then finish with yesterday. Bounded to 7 days to prevent a month-long outage from melting the Gemini budget in one go — anything older surfaces in the report and requires a manual `--date` invocation.

### 5. Daily SQLite snapshot (two layers)

**App-level dump** — `scripts/snapshot_db.sh`: `sqlite3 data/calls.db ".backup /tmp/calls.db"` → gzip → `gsutil cp` to `gs://{bucket}/backups/calls.db.{date}.gz`. Cron'd 15 min after the pipeline finishes. Keep **90 days** of dumps via GCS lifecycle (covers "noticed a silent corruption a month later").

**Disk-level snapshot** — GCP Persistent Disk **scheduled snapshot policy** attached to the VM's data disk: daily, retain 14 days. Configured once at provisioning, zero runtime code. Covers the window between the pipeline finishing and the app-level dump completing.

### 6. Config — `.env` additions

```
GCS_BUCKET=ziwo-call-analytics-prod
GOOGLE_APPLICATION_CREDENTIALS=/opt/ziwo/secrets/sa.json
SHEET_SPREADSHEET_ID=16nIRRSg1jb0bEivebOp46MEjnwa0V_oLc4VCjX6Jsm8
SHEET_TAB_NAME=calls            # or cloud_auto during parallel window
REPORT_EMAIL_FROM=...@gmail.com
REPORT_EMAIL_TO=andrea.grossi2@gmail.com
REPORT_SMTP_PASSWORD=...         # Gmail app password
HEALTHCHECKS_PING_URL=https://hc-ping.com/<uuid>   # dead-man's-switch
PIPELINE_TIMEZONE=Asia/Dubai     # OPEN — confirm
```

### 7. Monitoring (lightweight, production-grade-enough for a one-person system)

Three cheap layers, no Prometheus, no Cloud Monitoring setup burden:

1. **Daily email report** — primary signal; absence itself is meaningful.
2. **healthchecks.io dead-man's switch** — free tier, ~30 seconds to set up. After a successful pipeline run, `curl` the ping URL. If no ping arrives within the expected window, healthchecks.io emails you. This catches "VM is dead and no email was ever going to arrive" — the failure mode email-only monitoring misses. One line in the cron script: `curl -fsS -m 10 --retry 3 $HEALTHCHECKS_PING_URL > /dev/null`.
3. **GCP billing budget alert** — email at 50% / 90% / 100% of a monthly cap (e.g. $50). Catches Gemini cost runaway and any pricing surprises.

No SLA, no pager. If a day is missed, catch-up logic handles it; if two days are missed, you investigate manually via SSH.

## Work breakdown

### Phase 0 — Provision (one-time, ~2–3h)

- [ ] Create GCP project `ziwo-analytics`.
- [ ] Create GCS bucket `ziwo-call-analytics-prod` with lifecycle rules: delete `mp3s/` after 30 days; delete `backups/` after 90 days.
- [ ] Create service account `pipeline-runner` with roles: `Storage Object Admin` (bucket-scoped). Sheets API access is granted by sharing the sheet with the SA email (editor).
- [ ] Share the Google Sheet with the service account email (editor).
- [ ] Create Gmail app password for the report sender account.
- [ ] Provision **e2-small** VM (Debian 12), 30 GB balanced persistent disk. Single zone (any), single-tenant expectations make region choice near-irrelevant.
- [ ] Attach a **scheduled snapshot policy** to the data disk: daily, retain 14 snapshots.
- [ ] Static external IP (not strictly required, but helps if Ziwo/MySQL ever adds allowlisting later).
- [ ] `apt install python3.12 python3-venv ffmpeg git sqlite3 curl`.
- [ ] Clone repo, `python -m venv .venv`, `pip install -r requirements.txt` (+ `google-cloud-storage`, `google-api-python-client`, `google-auth`).
- [ ] Place `.env` and service-account JSON in `/opt/ziwo/secrets/` (mode 600).
- [ ] Create a **healthchecks.io** check (daily, grace period 2h); note the ping URL into `.env`.
- [ ] Set a **GCP billing budget alert** (e.g. $50/mo cap with 50/90/100% email triggers).

### Phase 1 — Code changes (~1–2 days)

- [ ] `ziwo/storage.py` + wire into `download.py` / `transcribe.py`.
- [ ] `ziwo/sheets.py` + `pushed_to_sheet_at` column in `ziwo/db.py` (both `SCHEMA` and `EXTENSION_COLUMNS`).
- [ ] `ziwo/report.py` + email config.
- [ ] Extend `pipeline.py run` with `push_sheets` and `report` steps; confirm default date = yesterday.
- [ ] `scripts/snapshot_db.sh`.
- [ ] Local dry-run against a sandbox GCS bucket + scratch sheet tab.

### Phase 2 — Deploy (~2h)

- [ ] Rsync/clone repo to VM.
- [ ] Install cron entries (note the pipeline line ends by pinging healthchecks.io only on success, so the dead-man's switch fires if the pipeline crashes mid-run):
  ```
  0 6 * * * cd /opt/ziwo && .venv/bin/python pipeline.py run >> /var/log/ziwo/pipeline.log 2>&1 && curl -fsS -m 10 --retry 3 "$HEALTHCHECKS_PING_URL" > /dev/null
  15 6 * * * /opt/ziwo/scripts/snapshot_db.sh >> /var/log/ziwo/backup.log 2>&1
  ```
- [ ] Trigger one manual run, verify: mp3s in GCS, rows in sheet's `cloud_auto` tab, email received, healthchecks.io marks the check green.

### Phase 3 — Parallel cutover (3 days)

- [ ] Day 1–3: local pipeline continues to feed `calls` tab; VM writes to `cloud_auto` tab.
- [ ] Daily diff: same call_ids? same theme/category distribution? any failures cloud-side that local didn't hit?
- [ ] If clean on all 3 days → proceed.

### Phase 4 — Cutover (~30 min)

- [ ] Switch `SHEET_TAB_NAME=calls` in VM `.env`.
- [ ] Stop running the local pipeline (keep the code + local DB as a cold backup for at least 2 weeks).
- [ ] Update `CLAUDE.md`: operational section now points to VM, not laptop.

## Operational runbook (post-cutover)

- **SSH in:** `gcloud compute ssh ziwo-vm --zone=us-central1-a`
- **Tail today's run:** `tail -f /var/log/ziwo/pipeline.log`
- **Inspect DB:** `sqlite3 /opt/ziwo/data/calls.db "SELECT status, COUNT(*) FROM calls GROUP BY status"`
- **Manual re-run (specific date):** `.venv/bin/python pipeline.py run --date 2026-04-19`
- **Replay a single call:** `UPDATE calls SET status='pending' WHERE id=...; run`
- **Recover from disk loss:** latest `calls.db.*.gz` from `gs://.../backups/`; mp3s are still in GCS (within 30-day window).

## Open questions

1. **Run time + timezone.** Recommending 06:00 `Asia/Dubai` — confirm Ziwo tenant TZ and that previous-day recordings are reliably available by then.
2. **Email recipients.** Just `andrea.grossi2@gmail.com` for now, or add anyone else?
3. **First-run sheet headers.** Current sheet has no headers. Keep that convention, or add headers during this migration (it's a good time)?
4. **Backfill.** Do we backfill historical mp3s from the laptop into GCS before cutover, or let GCS start fresh and keep local archives as cold storage?
5. **Sheet tab during parallel.** Create `cloud_auto` tab manually, or have the pipeline create it on first run?
6. **Provisioning.** Do you want to do Phase 0 yourself with this as a checklist, or should I script it as a `scripts/provision_gcp.sh` (gcloud CLI)?

## Risks

- **Gemini quota at fixed run time.** 500 calls at 06:00 every morning means a spike. If we hit rate limits, either stagger or implement a small sleep/backoff. Flag if it bites during parallel week.
- **Ziwo recordings late-arriving.** If a call from 23:55 isn't available on the API until 30 min later, it'd be missed by a 06:00 run. Mitigation: the catch-up logic in `run` will pick it up the next day, idempotent via `pushed_to_sheet_at`.
- **Sheets row cap.** 10M cells / sheet. At 500 rows/day × ~30 cols, that's ~54 years — non-issue.
- **Sheet grows unwieldy.** Long before the cap, load times or collaborator pain may push us toward BigQuery as a warehouse. Not urgent; revisit in ~12 months.
- **SQLite single-writer.** Only cron writes; fine. Just don't accidentally run the pipeline twice in parallel. A simple `flock` on the cron line would prevent overlap if a run ever goes long.
- **VM going rogue.** e2-small is not preemptible but can be stopped by quota/billing issues or zonal outage. **healthchecks.io dead-man's switch** is the primary signal; catch-up logic heals the missed day on the next morning's run.
- **Concurrent long run.** If one morning's run is still going when the next cron fires (unlikely at 500/day but possible during a catch-up after multi-day outage), both could contend. Mitigate with `flock` on the pipeline cron line.
- **Secret rotation.** Gmail app passwords + service account keys live on the VM. Document rotation in runbook; not automated.

## Rollout checklist (when work resumes)

- [ ] Answer open questions above.
- [ ] Phase 0 provisioning.
- [ ] Phase 1 code (one PR per module ideally; keep diffs reviewable).
- [ ] Local dry-run against sandbox GCS + scratch sheet.
- [ ] Phase 2 deploy + smoke test.
- [ ] Phase 3 parallel × 3 days with daily diff.
- [ ] Phase 4 cutover, update `CLAUDE.md`.
- [ ] Archive local cron/flow; keep laptop DB as cold backup for 2 weeks before deleting.
