---
description: Distribution report of themes, categories, friction, queue matching
---

Run on `data/calls.db` for rows with `status='extracted'` and print each section:

1. **Theme distribution** — `qualifier_theme`, count, %. Sort desc. Flag if `"Other"` > 10% of extracted rows.
2. **Category distribution** — `category`, count, %.
3. **Subcategory distribution** — `subcategory`, count, %.
4. **Friction** — overall distribution (0/1/2 counts + %) and average friction score per subcategory, sorted desc.
5. **Queue match** — per `queue_intent`, breakdown of `queue_matches_category` (Yes/No/NULL) with %. Flag any named queue with >30% mismatch.
6. **Unmapped rows** — count and sample IDs where `category IS NULL` among extracted rows (indicates an `intent_action` missing from `ACTION_DEFAULTS` in `mece.py`).

Format output as plain markdown tables. Keep it tight — no transcript dumps, no call_summary output.
