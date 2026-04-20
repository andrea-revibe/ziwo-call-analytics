---
description: Reset extracted rows and re-run extract with current code/prompt
---

Goal: when the extraction prompt or enum changes, re-run extract on already-extracted rows and show how outputs shifted.

Steps:

1. Parse `$ARGUMENTS` — either a comma-separated list of call IDs, the word `all`, or empty (ask user which).
2. Count how many rows will be reset and **ask the user to confirm before mutating the DB**.
3. Before resetting, dump the current values to `scratch/backfill_{YYYY-MM-DD_HHMM}.csv` with columns: `id, intent_action, intent_object, intent_qualifier, qualifier_theme, call_summary, sentiment, resolution`.
4. Run the reset:
   ```sql
   UPDATE calls SET status='transcribed',
     intent_action=NULL, intent_object=NULL, intent_qualifier=NULL,
     qualifier_theme=NULL, call_summary=NULL, sentiment=NULL,
     resolution=NULL, extracted_at=NULL, error_message=NULL
   WHERE <scope>;
   ```
5. Run `python pipeline.py extract` (with `--limit` if scoped).
6. Run `python pipeline.py mece` to refresh category/subcategory/friction from the new themes.
7. Print a diff table: per field (`intent_action`, `qualifier_theme`, `sentiment`, `resolution`), count of rows whose value changed vs the snapshot CSV.
8. Remind the user to spot-check with `/sample-bucket` on the theme with the most changes.
