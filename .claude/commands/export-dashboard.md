---
description: Export dashboard-ready CSV to ~/Desktop/ziwo-dashboard/data/
---

Export rows from `data/calls.db` that match:
- `status = 'extracted'`
- `category IS NOT NULL` (i.e., the mece pass ran)
- `date(started_at) = <TARGET_DATE from .env>` (so each daily file only contains that day's calls)

Destination: `~/Desktop/ziwo-dashboard/data/calls_{TARGET_DATE}.csv`. Overwrite if exists. Create the `data/` subdir if missing.

Columns (order matters — match exactly):
- `call_id` (= `id`), `started_at`, `duration`, `talk_time`, `ring_time`, `queue_name`, `agent_id`, `agent_cc_login`, `agent_first_name`, `agent_last_name`, `caller_id_number`
- `transcript_language`
- `call_summary`, `intent_action`, `intent_object`, `intent_qualifier`, `qualifier_theme`
- `sentiment`, `resolution`, `escalation_requested`
- `object_bucket`, `category`, `subcategory`, `friction_score`
- `country`, `language`, `queue_intent`, `queue_matches_category`, `order_number`
- `transcript` (always included — the dashboard needs it)

Use standard CSV quoting (`csv.QUOTE_MINIMAL`). Header row required.

**After writing the CSV, regenerate the manifest** at `~/Desktop/ziwo-dashboard/data/index.json`. The manifest is the contract between this pipeline and the dashboard module; it lists every available daily CSV and drives the dashboard's Date Range picker. Shape:

```json
{
  "generated_at": "<ISO 8601 UTC of this run>",
  "min_date": "<earliest date found>",
  "max_date": "<latest date found>",
  "files": [
    { "filename": "calls_2026-04-10.csv", "date": "2026-04-10", "row_count": 177, "size_kb": 1481 }
  ]
}
```

Steps:
1. Scan `~/Desktop/ziwo-dashboard/data/` for every file matching `calls_YYYY-MM-DD.csv`.
2. For each, parse the date from the filename, count rows (minus the header) **using `csv.reader`, not line counting** — transcripts contain embedded newlines inside quoted fields. Bump `csv.field_size_limit(sys.maxsize)` first; transcripts can exceed the default 128 KB field cap. Record the file size in KB.
3. Sort files ascending by date.
4. Derive `min_date` / `max_date` from the sorted list.
5. Write `index.json` with `generated_at` = current UTC ISO timestamp. Overwrite if it exists.

Report row count, destination CSV path, file size, **and** the manifest's `files` count after writing.
