---
description: Save a timestamped taxonomy distribution snapshot for paper-trail comparison
---

Run the same queries as `/taxonomy-audit`, but instead of printing to the terminal, write the output as markdown to:

`scratch/snapshots/taxonomy_{YYYY-MM-DD_HHMM}.md`

Include at the top of the file:
- Run timestamp (ISO)
- Extracted row count
- Brief one-line description of what prompted the snapshot (ask the user; default: "routine snapshot")

Then the distribution tables (theme, category, subcategory, friction, queue match, unmapped).

After writing, print the destination path and summarize the two or three biggest shifts vs the most recent prior snapshot in `scratch/snapshots/` — if one exists. If no prior snapshot, just note "first snapshot".
