# Ziwo Call Analytics

Backend pipeline that turns Revibe's inbound customer-support calls into a structured dataset for **project ideation** — identifying which call topics are highest-volume, highest-friction, and least-resolved. It ingests calls from the Ziwo telephony API, transcribes them with Gemini, extracts structured features (intent, theme, sentiment, resolution) with a second Gemini pass, and maps everything into a MECE taxonomy. The output is a CSV that feeds a dashboard (separate project at `~/Desktop/ziwo-dashboard/`).

**Out of scope:** the dashboard UI itself, agent QA / per-agent scoring, real-time processing.

## At a glance

| | |
|---|---|
| Input | Ziwo API: call metadata + `.mp3` recordings for a single day |
| Output | `~/Desktop/ziwo-dashboard/data/calls_{YYYY-MM-DD}.csv` |
| Storage | Local SQLite at `data/calls.db` + `.mp3` files at `data/audio/` |
| LLM | Gemini 2.5 Flash (transcription + structured feature extraction) |
| Scale | POC: one day per run (~150–500 calls). Project scope: 10 days total. |
| Analysis spec | [`docs/transcript-analysis-methodology.md`](docs/transcript-analysis-methodology.md) |

## Pipeline stages

The pipeline is a **resumable per-call state machine** persisted in `data/calls.db`. Every row advances through these stages; a failure records `error_message` and the row can be retried without reprocessing siblings.

```
          [Ziwo API]
              │
              ▼
        pipeline.py fetch        →  CSV snapshot of one day's calls
         (ziwo/fetch.py)
              │
              ▼
   ┌─────────────────────┐
   │       pending       │  ← ingest CSV into SQLite (ziwo/ingest.py)
   └──────────┬──────────┘     rows with talk_time < 45s are skipped
              │                (MIN_TALK_TIME in ziwo/ingest.py)
              ▼  download .mp3 (ziwo/download.py)
   ┌─────────────────────┐
   │     downloaded      │
   └──────────┬──────────┘
              │
              ▼  Gemini transcribe + translate to English
                 (ziwo/transcribe.py; source language recorded in
                  transcript_language)
   ┌─────────────────────┐
   │     transcribed     │
   └──────────┬──────────┘
              │
              ▼  Gemini extract (ziwo/extract.py)
   ┌─────────────────────┐
   │      extracted      │  ← final status for successfully processed rows
   └──────────┬──────────┘
              │
              ▼  deterministic post-passes (no LLM):
                 • ziwo/queues.py → country, language, queue_intent
                 • ziwo/mece.py   → category, subcategory, friction_score,
                                    queue_matches_category
              │
              ▼
       [CSV export to ~/Desktop/ziwo-dashboard/data/]
```

Any stage can hit the `failed` branch; see **Troubleshooting** below for recovery.

## Setup (one-time)

```bash
cd ~/Desktop/ziwo-call-analytics
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy the template and fill in real values
cp .env.example .env
```

`.env` keys:

- `ZIWO_BASE_URL`, `ZIWO_USERNAME`, `ZIWO_PASSWORD` — Ziwo API credentials
- `ZIWO_TARGET_DATE` — the single day to process (format `YYYY-MM-DD`)
- `GEMINI_API_KEY` — from [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- `GEMINI_MODEL` — defaults to `gemini-2.5-flash`; override if needed

## Running the pipeline

Always activate the venv first: `source .venv/bin/activate`.

```bash
# 1. Pull the day's calls into a CSV snapshot.
python pipeline.py fetch

# 2. Load the CSV into SQLite (rows start as `pending`).
python pipeline.py ingest

# 3. (Recommended) dry-run a few calls end-to-end before the full batch.
python pipeline.py download   --limit 3
python pipeline.py transcribe --limit 3
python pipeline.py extract    --limit 3
python pipeline.py show <call_id>   # inspect one row
python pipeline.py status           # see counts per status

# 4. Process the rest.
python pipeline.py download
python pipeline.py transcribe
python pipeline.py extract

# 5. Deterministic post-passes (cheap, no LLM).
python pipeline.py queues
python pipeline.py mece

# 6. Export the dashboard CSV.
#    Run the /export-dashboard slash command inside Claude Code,
#    or see .claude/commands/export-dashboard.md for the spec.
```

Shortcut: `python pipeline.py run [--limit N]` chains **fetch → ingest → download → transcribe → extract** in one command. Pass `--skip-fetch` to re-run later stages without hitting the Ziwo API again. Run `queues` and `mece` separately after.

All stages are **resumable and idempotent**. Re-running skips rows already past that stage. A new day is processed by changing `ZIWO_TARGET_DATE` in `.env` and re-running from step 1 — the DB accumulates across days.

## Project layout

```
.
├── pipeline.py                       CLI dispatcher for all pipeline stages
├── .env / .env.example               Local config
├── requirements.txt
├── CLAUDE.md                         Collaboration conventions for Claude Code
├── README.md                         This file
├── docs/                             Project documentation
│   ├── transcript-analysis-methodology.md  Canonical spec for the analysis
│   └── transcript-optimization-plan.md     Parked plan: strip hold music before transcription
├── ziwo/                             Reusable package
│   ├── config.py                     Env loading + project paths
│   ├── db.py                         SQLite schema, migration, helpers
│   ├── fetch.py                      Ziwo API → CSV snapshot (step 1)
│   ├── ingest.py                     CSV → SQLite (filters talk_time < MIN_TALK_TIME)
│   ├── download.py                   Ziwo recording → local .mp3
│   ├── transcribe.py                 .mp3 → English speaker-labeled transcript (Gemini translates on the fly)
│   ├── extract.py                    Transcript → structured features (Gemini, Pydantic)
│   ├── queues.py                     queue_name → country/language/queue_intent
│   └── mece.py                       Deterministic MECE taxonomy + friction score
├── scripts/                          One-shot backfills (safe to re-run; use --dry-run first)
│   ├── clean_transcripts.py          Strip filler tags from existing transcripts
│   ├── translate_transcripts.py      Text-to-text translate non-English transcripts to English
│   └── delete_short_calls.py         Hard-delete rows with talk_time < MIN_TALK_TIME + their .mp3 files
├── .claude/commands/                 Project-specific Claude Code slash commands
│   ├── backfill-extraction.md        Reset + re-extract after a prompt change
│   ├── sample-bucket.md              Pull 3–5 sample calls from a theme/category
│   ├── taxonomy-audit.md             Distribution report (themes, friction, queues)
│   ├── export-dashboard.md           Write CSV to ~/Desktop/ziwo-dashboard/data/
│   └── snapshot-taxonomy.md          Timestamped snapshot for iteration diffing
├── scratch/                          Throwaway analysis, snapshots, one-offs
└── data/                             Local data (add to .gitignore when repo is created)
    ├── calls.db                      SQLite source of truth
    ├── audio/                        Downloaded .mp3 files
    └── exports/                      CSV snapshots from the fetch stage
```

## Data model

Single table: `calls`. Schema lives in [`ziwo/db.py`](ziwo/db.py). Grouped by source:

| Group | Columns | Populated by |
|---|---|---|
| **Ziwo metadata** | `id`, `direction`, `queue_name`, `started_at`, `duration`, `talk_time`, `ring_time`, `audio_quality`, `caller_id_number`, `agent_id`, `agent_cc_login`, `agent_first_name`, `agent_last_name`, `recording_file` | `ingest.py` |
| **Pipeline state** | `status`, `audio_path`, `error_message`, `created_at`, `updated_at` | Every stage |
| **Transcription** | `transcript` (English), `transcript_language` (source language), `transcribed_at`, `transcript_translated_at` (set by the text-to-text translate backfill script) | `transcribe.py`, `scripts/translate_transcripts.py` |
| **LLM extraction** | `call_summary`, `intent_action`, `intent_object`, `intent_qualifier`, `qualifier_theme`, `sentiment`, `resolution`, `partial_reason`, `escalation_requested`, `extracted_at` | `extract.py` |
| **Queue parse** | `country`, `language`, `queue_intent` | `queues.py` |
| **MECE mapping** | `category`, `subcategory`, `friction_score`, `queue_matches_category` | `mece.py` |

**Extraction details** (Pydantic schema, prompt, enum values) live in [`ziwo/extract.py`](ziwo/extract.py). The full reasoning — why these fields, what each enum value means, and the MECE taxonomy — is in [`docs/transcript-analysis-methodology.md`](docs/transcript-analysis-methodology.md).

## Going deeper

- **What the analysis does and why** → [`docs/transcript-analysis-methodology.md`](docs/transcript-analysis-methodology.md). Read this before making any change to extraction prompts, enum values, or the MECE mapping. The methodology doc's "Appendix: Pipeline cutover history" tracks dated discontinuities in the corpus (prompt versions, filter thresholds) — check it before comparing metrics across dates.
- **Collaboration conventions** (how to work in this repo with Claude Code) → [`CLAUDE.md`](CLAUDE.md).
- **Workflow shortcuts** → the slash commands in `.claude/commands/`. Each file is a documented prompt; you can read them standalone.

## Troubleshooting

**A row is stuck in `failed`.** Check the `error_message`:

```bash
sqlite3 data/calls.db "SELECT id, status, substr(error_message,1,200) FROM calls WHERE status='failed';"
```

Reset the row to retry a stage:

```bash
# Retry download: set status back to 'pending' and clear the error
sqlite3 data/calls.db "UPDATE calls SET status='pending', error_message=NULL WHERE id=<call_id>;"

# Retry transcribe / extract similarly, setting status to 'downloaded' / 'transcribed'.
```

**Re-extracting after a prompt change.** Use the `/backfill-extraction` slash command — it snapshots old values, resets status, re-runs, and prints a per-field diff.

**Gemini quota or rate limits.** The extraction stage is currently one call per transcript. Failures retry on the next run (status stays `transcribed`). Scaling above ~1000 calls/day should introduce batching — see `CLAUDE.md` for POC-scale conventions.

**Inspecting the DB.** Always truncate transcript-bearing columns in exploratory queries:

```bash
sqlite3 data/calls.db "SELECT id, intent_action, qualifier_theme, substr(call_summary,1,120) FROM calls WHERE status='extracted' LIMIT 5;"
```
