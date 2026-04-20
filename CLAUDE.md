# Revibe Call Analytics — Collaboration Guide

Backend pipeline: ingests Ziwo inbound calls → transcribes (Gemini) → extracts structured features → maps to MECE taxonomy → exports CSV for a dashboard. **Out of scope:** the dashboard UI (separate project at `~/Desktop/ziwo-dashboard/`), agent QA, real-time processing.

**Canonical spec:** `docs/transcript-analysis-methodology.md`. Do NOT read it unless the task touches extraction or taxonomy logic — it's large and most turns don't need it.

## Pipeline

Resumable per-call state machine in `data/calls.db`:
`pending → downloaded → transcribed → extracted` (+ `failed` branch).
- **Transcription** includes automatic cleanup of filler tags (repeated `[Music]`, `[Noise]`, etc.) via `clean_transcript()` in `transcribe.py`.
- Deterministic post-extract passes populate `country/language/queue_intent` (`queues.py`) and `category/subcategory/friction_score/queue_matches_category` (`mece.py`).
- **Order lookup** (`queues.py`): matches `caller_id_number` to customer orders in the Revibe MySQL production DB. Both sides are normalised to the rightmost 9 digits. Orders older than 60 days before the call date are excluded. Multiple matches are stored comma-separated as `order_number (date), ...`. Runs during the `queues` step; skipped gracefully if MySQL is not configured.

## Where things live

| Concern                  | File                                 |
| ------------------------ | ------------------------------------ |
| CLI entrypoint           | `pipeline.py`                        |
| DB schema + migration    | `ziwo/db.py`                         |
| Extraction (Gemini)      | `ziwo/extract.py`                    |
| Queue parsing + orders   | `ziwo/queues.py`                     |
| MECE mapping + friction  | `ziwo/mece.py`                       |
| Methodology (spec)       | `docs/transcript-analysis-methodology.md` |
| Dashboard CSV output     | `~/Desktop/ziwo-dashboard/data/`     |
| Transcript cleanup       | `ziwo/transcribe.py` (`clean_transcript()`) |
| Transcript backfill      | `scripts/clean_transcripts.py`       |
| Throwaway analysis       | `scratch/` (not the repo root)       |

## Rules

- **Confirm before any DB UPDATE/DELETE.** Read-only queries are fine.
- **POC scale** (~500/day target). No batching, retries, feature flags, or backwards-compat shims until we clear ~1000/day.
- **Two-layer classification.** `qualifier_theme` is LLM (semantic). `category`/`subcategory` is deterministic (`mece.py`). Don't let the LLM emit category directly.
- **Queue stays independent of LLM.** Don't feed `queue_name` into the extraction prompt — divergence is the signal.
- **Friction score = sentiment + resolution only.** Other fields become dashboard filters, not score components.
- **Keep `methodology.md` in sync** when extraction or taxonomy logic changes.
- **After any extraction-prompt change**, remind the user to spot-check with `/sample-bucket` on affected themes. Don't auto-run it. A ground-truth eval harness is a planned future step — not in place yet.
- **Schema changes** go in both `SCHEMA` (CREATE TABLE) and `EXTENSION_COLUMNS` (migration dict) in `ziwo/db.py`.

## Operational

- Activate venv first: `source .venv/bin/activate`
- Subcommands: `python pipeline.py {ingest|download|transcribe|extract|queues|mece|status|show|run}`
- DB inspect: `sqlite3 data/calls.db "..."` — truncate transcript-bearing columns with `substr(call_summary,1,120)` etc. Never `SELECT *`.
- **MySQL order lookup** requires `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE` in `.env`. Queries `orders` + `order_customers` tables. If not configured, `queues` step still runs without order linkage.
- **Transcript backfill cleanup**: `python scripts/clean_transcripts.py [--dry-run]` — one-shot script to clean filler tags from existing transcripts. Safe to re-run (idempotent).
