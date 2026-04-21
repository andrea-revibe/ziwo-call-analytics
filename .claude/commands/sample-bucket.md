---
description: Print 3-5 sample calls for a given theme, category, or subcategory for QA
---

Argument: `$ARGUMENTS` — a value of `qualifier_theme`, `category`, or `subcategory` (e.g., `"Delivery Issues"`, `"After-Sales Support"`, `"Product Complaints"`).

Steps:

1. Detect which column the arg belongs to: try `qualifier_theme` first, then `subcategory`, then `category`. If no match in any, ask the user to clarify.
2. Select up to 5 calls from `data/calls.db` where `status='extracted'` and the matched column equals the arg. Use `ORDER BY RANDOM() LIMIT 5` unless the user specified otherwise.
3. For each call, print:
   - `call_id`, `intent_action | intent_object | intent_qualifier`, `qualifier_theme`, `sentiment`, `resolution` (+ `partial_reason` when Partial), `friction_score`
   - `call_summary` (full — it's 2 sentences)
   - First ~400 chars of `transcript`
4. After printing, ask: "Do these classifications look right for this bucket? Any misclassifications stand out?"

Keep DB reads tight — don't pull the full transcript column for rows you're not displaying.
