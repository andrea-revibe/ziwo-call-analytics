# Revibe — Inbound Call Analysis Pipeline

## Overview

This pipeline analyzes transcripts of inbound customer-support phone calls to Revibe to identify where callers experience the most friction. The output is a structured dataset that powers a dashboard for data-driven project ideation: which call topics are highest-volume, highest-friction, and least-resolved.

The pipeline has three stages, mirroring the chatbot analysis pipeline so results are comparable across channels:

1. **Preprocess** — light cleanup of raw transcripts (speaker-label normalization, turn merging)
2. **Extract** — use an LLM (Gemini 2.5 Flash) to tag each transcript with intent, sentiment, and call resolution
3. **Categorize** — map extracted features into a MECE taxonomy with friction scores

All labels (intents, sentiment, categories) are in **English** for consistency and cross-channel comparison. Transcripts are stored as **English translations** produced by Gemini during transcription; the original spoken language (primarily Egyptian Arabic, occasional English) is preserved in the `transcript_language` column for filtering and audit.

**Scope note:** agent performance is deliberately out of scope. The methodology focuses on *what customers call about and where they struggle*, not *how individual agents handle calls*.

---

## Stage 1: Preprocessing

Raw transcripts arrive as speaker-labeled turns (`Agent:` / `Customer:`) from Gemini transcription. Preprocessing is minimal compared to the chatbot pipeline because spoken calls don't contain markdown, URLs, or session-closing boilerplate.

Steps:

- **Normalize speaker labels** — resolve any `Speaker:` fallback tags to `Agent:` or `Customer:` when inferable from neighbors; otherwise keep `Speaker:`
- **Merge consecutive same-speaker turns** into single blocks to reduce fragmentation
- **Strip obvious ASR noise** — empty lines, one-character tokens, non-speech markers like `[silence]` or `[music]`
- **No length truncation** — phone calls are naturally bounded (typically under 10 minutes) and fit in Gemini's context window without trimming

Omissions vs. the chatbot pipeline: no URL/markdown stripping, no boilerplate closing removal, no 5000-char truncation. None apply to speech.

---

## Stage 2: Feature Extraction

Each preprocessed transcript is sent to Gemini 2.5 Flash with a Pydantic `response_schema` for structured JSON output. Input to the extractor is always English (translated at the transcription stage), so the prompt assumes English idioms and cue phrases for resolution / partial-reason discrimination. For the current POC (179 calls) transcripts are processed one-per-request; batching (10 per call) will be added when scaling to thousands per day.

Eight fields are extracted per call:

### Call Summary

A 2-sentence maximum English summary capturing (a) what the customer called about and (b) the outcome of the call (resolved, escalated, callback promised, hung up, etc.). Neutral, factual tone — no direct customer quotes.

Purpose: gives analysts enough context to understand a bucket by skimming ~20 summaries without opening each transcript, and provides richer context than a 2-4-word qualifier alone.

### Intent

Format: `[Action] | [Object] | [Qualifier]`

- **Action** — the customer's concrete request. Constrained enum: Return, Refund, Inquiry, Track Order, Complaint, Purchase, Technical Support, Cancellation, Sell Device, Warranty Claim, Payment Issue, Account Issue, Website/App Issue. **Complaint is a residual action** — use it only when the customer is expressing dissatisfaction *without* a concrete ask. If a concrete ask exists underneath the complaint (refund, return, warranty, tech fix, etc.), use that action instead. Escalation (asking for a supervisor) is captured separately as a boolean flag (`escalation_requested`), not an action — register and topic are orthogonal.
- **Object** — what the request is about (e.g., iPhone, MacBook, Payment, Voucher, Order, Account, Delivery). Short English noun phrase; "General" if genuinely unclear. Free-text for drilldown; a deterministic `object_bucket` is derived post-extraction (see Stage 3).
- **Qualifier** — 2–4 words of descriptive English context, optimized for human readability (e.g., "Cracked Screen", "Tracking Status Confusion", "Store Credit"). **Free text, does not need to match any enum.** This field is for display and drilldown only — theme classification (below) handles bucketing.

Changes vs. the chatbot action list:

- **Website/App Issue** — renamed from "Website Issue" to include mobile app, a common call driver.
- **Escalation no longer an action.** Supervisor/manager requests are captured as a separate boolean field (`escalation_requested`). Register and topic are orthogonal: a call can both demand a supervisor *and* be about a refund. Keeping escalation as a flag preserves both signals instead of collapsing them into one bucket.
- **Complaint narrowed to residual.** Previously the LLM used Complaint broadly for any dissatisfaction; now it's reserved for calls with no concrete underlying request. Expressed dissatisfaction is captured by `sentiment` (Frustrated/Angry), independent of action.

### Qualifier Theme

**LLM-classified** from a constrained enum of 34 themes + `"Other"`. Replaces the chatbot pipeline's keyword-matching approach, which was brittle on novel free-text qualifiers (e.g., "Canceled Tracking" would fail every keyword regex and land in "Other" despite obviously being a shipping-status issue).

Why LLM instead of keywords:
- Semantic matching catches paraphrases and novel phrasings — coverage jumps from ~60% to 90%+
- Frees the `intent_qualifier` field from dual-duty (no longer needs to be both human-readable *and* regex-matchable)
- `"Other"` is included in the enum explicitly, so non-matching calls are flagged rather than silently miscategorized

Deterministic reproducibility is preserved by low temperature (0.1) + structured output + fixed enum.

| Theme group           | Themes                                                                      |
| --------------------- | --------------------------------------------------------------------------- |
| Delivery & logistics  | Delivery Delays, Address Issues, Quality Check Wait, Status Inquiry, Shipping Provider Issue |
| Cashback & promotions | Missing Cashback, Promotions & Vouchers                                     |
| Payment               | Installment Payments, Payment Failures, Cash on Delivery, Pricing Inquiry   |
| Device issues         | Battery & Charging, Screen Issues, SIM & Connectivity, Hardware Defects, Product Complaint |
| Warranty & returns    | Claim Status, Coverage Inquiry, Return Process, Refund Processing           |
| Account & platform    | Account Registration, Login Issues, Video Upload Issue                      |
| Purchase              | Trade-In, Store Location, Product Research                                  |
| Cross-channel         | Chatbot Handoff, Website Couldn't Find Answer, App Problem                  |
| Misc                  | Cancellation, Order Modification, Wrong or Missing Item, Supervisor Request, Initial or Unclear Contact |
| Catch-all             | Other                                                                       |

Calls-specific addition: the **Cross-channel** theme group. Inbound calls frequently reference prior attempts in chatbot, website, or app ("I tried to do this online but it didn't work"). Surfacing this quantifies how often digital channels push volume to phone — a valuable input for prioritizing digital self-serve fixes.

### Sentiment

A single-word classification constrained to: **Frustrated**, **Neutral**, **Inquisitive**, **Satisfied**, or **Angry**.

Sentiment is judged from the transcript text only. **Note:** audio-native sentiment analysis (passing the mp3 to Gemini instead of the transcript) is expected to be meaningfully more accurate for phone calls — tone of voice, sighs, raised volume, and silences carry signal that transcripts discard. Transcript-only is used in the POC to keep cost and latency low; upgrading to audio-based sentiment is a one-line change and should be reconsidered before scaling or before any decision becomes sentiment-critical.

### Call Resolution

A three-tier judgment of **information/action finality** — did the agent deliver a definitive outcome on this call? Deliberately not a measure of customer happiness; a call can be `Yes` with a frustrated customer (firm policy denial) and `No` with a calm one (disconnect during hold).

- **Yes** — a final answer or action was delivered. Covers explicit confirmation, firm policy decisions ("the firm no"), specific status updates with concrete data, intra-session success (including after transfers or supervisor intervention), and "resolved but escalated" cases where a final answer was given even though the customer demanded a manager.
- **Partial** — agent engaged with the inquiry but it is not concluded on this call; work remains. Always paired with `partial_reason` (closed enum):
  - `callback_promised` — agent/back-office will follow up later (return call, email, ticket).
  - `vague_guidance` — agent gave only a range or generic statement (e.g., "3–5 business days") with no concrete commitment.
  - `system_or_knowledge_gap` — agent/supervisor could not answer due to system outage, access, or knowledge, and did not commit to a callback.
  - `customer_action_required` — agent's side is complete but the customer must still act (click a link, visit a warehouse, reply to an email).
  - `handoff_to_other_team` — routed to another team/channel; this leg ended without resolution.
- **No** — the call ended with no useful outcome: technical disconnect, customer hung up during hold, customer gave up waiting, or agent never meaningfully engaged.

Rationale for the Partial tier: stakeholders consuming the prior binary signal overestimated unresolved rate because "firm policy no" and "specific status update" calls were being marked `No`. The information-finality framing makes those `Yes`, and the new `Partial` tier captures operational debt (callbacks, vague answers, system gaps) as a distinct signal the dashboard can act on separately from catastrophic drops (`No`).

### Escalation Requested

A boolean (`true` / `false`) indicating whether the customer explicitly asked for a supervisor, manager, team lead, or any higher authority during the call. LLM-extracted from the transcript.

Kept as a separate field rather than as an action or category because escalation is a **register signal**, orthogonal to the call's topic. A refund call that escalates is still a refund call; collapsing both signals into one "Escalation" bucket would hide the topic and prevent escalation analytics from being filtered per category. As a flag, escalation can be filtered independently across every bucket.

---

## Stage 3: MECE Categorization

The extracted actions and objects are mapped into a two-level MECE taxonomy using deterministic rules — no LLM calls, fully reproducible and dashboard-ready.

### Why MECE

The goal is to identify **high-impact improvement projects**. MECE bucketing ensures:

- **No double-counting** — every call maps to exactly one category and subcategory
- **No blind spots** — every call is categorized; volume percentages are trustworthy
- **Clear ownership** — each bucket maps to a distinct operational area

### Taxonomy Structure (Calls)

The taxonomy is organized by **customer journey stage** (a topic axis), not by emotional register or call behavior. Complaining and escalating are register/behavioral signals captured separately as flags (see Register Flags below) so the topic axis stays strictly MECE.

| Category                | Subcategories                                                                  | What it captures                                                                 |
| ----------------------- | ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| **Pre-Purchase**        | Product Information, Pricing & Installments, Promotions & Vouchers, Purchase Intent | Customers exploring products, prices, and promos before buying                  |
| **Order Management**    | Order Tracking, Delivery Issues, Order Modification, Order Cancellation        | Post-order logistics and mid-flight changes: tracking, delivery, modify, cancel  |
| **After-Sales Support** | Returns & Refunds, Warranty Claims, Technical Support, Product Defects         | Product issues after delivery: defects, returns, refunds, device troubleshooting |
| **Account & Platform**  | Account Management, Payment Issues, Website/App Issues                         | Non-product operational friction: account setup, payment failures, app/site bugs |
| **Trade-In Program**    | Device Trade-In                                                                | Customers looking to sell their used devices                                     |

Changes vs. the chatbot taxonomy:

- **No "Complaints & Escalations" category.** Complaining is a register (captured by `sentiment`); escalation is a behavior (captured by `escalation_requested`). Neither is a topic. A product complaint = After-Sales / Product Defects. A service complaint about a late delivery = Order Management / Delivery Issues. A supervisor request about a refund = After-Sales / Returns & Refunds with `escalation_requested=true`. This keeps the taxonomy strictly topic-driven and MECE.
- **Product Defects** — new subcategory under After-Sales, neutral topical label replacing the register-flavored "Product Complaints".
- **Order Modification** — split out from Order Cancellation. Different operational fixes deserve different buckets.
- **Purchase Intent** — renamed from "General Purchase" for clarity. Captures customers ready to buy who need help completing the purchase (vs. Product Information, which is research-stage).
- **Website/App Issues** — renamed from "Website Issues" to capture mobile-app complaints, which are a meaningful share of Account & Platform volume.

### Mapping Logic

Primary key is **`qualifier_theme`** — the LLM-classified semantic topic. Action and object are disambiguators, not primary keys. This flip (from action-primary to theme-primary) is intentional: the action enum is too coarse and the LLM frequently picks "Inquiry" as a catch-all, causing topic-clear calls to land in the wrong category. The theme is the strongest topical signal extracted, so it drives the mapping.

Mapping proceeds in four tiers. The first matching tier wins.

**Tier 1 — theme-determined.** The theme alone decides category and subcategory. Covers the majority of themes:

| Theme(s) | → Category / Subcategory |
| --- | --- |
| Delivery Delays · Address Issues · Shipping Provider Issue · Quality Check Wait | Order Management / Delivery Issues |
| Order Modification | Order Management / Order Modification |
| Promotions & Vouchers · Missing Cashback | Pre-Purchase / Promotions & Vouchers |
| Installment Payments · Cash on Delivery · Pricing Inquiry | Pre-Purchase / Pricing & Installments |
| Store Location · Product Research | Pre-Purchase / Product Information |
| Payment Failures | Account & Platform / Payment Issues |
| Return Process · Refund Processing | After-Sales Support / Returns & Refunds |
| Claim Status · Coverage Inquiry | After-Sales Support / Warranty Claims |
| Account Registration · Login Issues | Account & Platform / Account Management |
| Video Upload Issue · Chatbot Handoff · Website Couldn't Find Answer · App Problem | Account & Platform / Website/App Issues |
| Trade-In | Trade-In Program / Device Trade-In |
| Product Complaint | After-Sales Support / Product Defects |

**Tier 2 — theme + `object_bucket`.** For themes where the topic is genuinely ambiguous without an object signal:

| Theme | Object bucket → Category / Subcategory |
| --- | --- |
| Status Inquiry | order/delivery → Order Management / Order Tracking · warranty → After-Sales / Warranty Claims · refund → After-Sales / Returns & Refunds · payment → Account & Platform / Payment Issues · else → Order Management / Order Tracking |
| Cancellation | order/delivery → Order Management / Order Cancellation · account → Account & Platform / Account Management · trade-in → Trade-In Program / Device Trade-In · else → Order Management / Order Cancellation |
| Initial or Unclear Contact | order/delivery → Order Management / Order Tracking · warranty → After-Sales / Warranty Claims · account → Account & Platform / Account Management · product/pricing → Pre-Purchase / Product Information · else → Pre-Purchase / Product Information |
| Supervisor Request | route by object bucket as in the rows above; if no topical object, defaults to Account & Platform / Account Management. The `escalation_requested` flag should already be `true` for these calls — the theme is preserved for analytics on calls where escalation is the *only* discernible signal. |

**Tier 3 — theme + `intent_action`.** For device-issue and missing-item themes where the customer's *concrete ask* determines the bucket:

| Theme | Action → Category / Subcategory |
| --- | --- |
| Battery & Charging · Screen Issues · SIM & Connectivity · Hardware Defects | Return → After-Sales / Returns & Refunds · Warranty Claim → After-Sales / Warranty Claims · Technical Support → After-Sales / Technical Support · Complaint → After-Sales / Product Defects · else → After-Sales / Technical Support |
| Wrong or Missing Item | Return / Refund → After-Sales / Returns & Refunds · else → Order Management / Delivery Issues |

**Tier 4 — fallback.** `Other` theme falls through to an action-based default (legacy behavior). Logged for enum review per the Appendix policy.

**Loud failure on unknown themes.** Any extracted `qualifier_theme` not in the map raises an error in `mece.py` rather than silently defaulting. This catches enum drift the moment a new theme ships without a mapping update.

### Object Bucket

`object_bucket` is a deterministic normalization of the free-text `intent_object` field into a small closed set:

`order, delivery, product, payment, account, warranty, refund, trade-in, promotion, agent, unknown`

Implemented as substring rules (mirroring the `queues.py` style), evaluated in precedence order (specific buckets before generic ones — e.g., `refund` before `payment`, `warranty` before generic `claim`). New free-text objects that match no rule land in `unknown` and fall through to the theme's default — graceful degradation, no breakage. Periodic audit of `unknown` objects surfaces candidates for new substring rules.

This two-layer pattern mirrors `qualifier_theme` → `category`: the LLM provides semantic nuance; deterministic rules provide the stable enum the dashboard can group on.

### Register Flags (cross-cutting, non-MECE)

Signals that apply across every category, used as dashboard filters and friction inputs rather than as categorization axes:

| Flag | Source | Meaning |
| --- | --- | --- |
| `friction_score` (0–3) | Deterministic: sentiment + resolution | Scale expanded for three-tier resolution |
| `escalation_requested` (bool) | LLM extraction | Customer explicitly asked for supervisor/manager |
| `is_complaint` (derived) | `sentiment ∈ {Frustrated, Angry}` | No new column; derived on the dashboard |

Register flags are intentionally orthogonal to category. A refund call that includes a supervisor request is tallied once under After-Sales / Returns & Refunds with `escalation_requested=true` — never split across two buckets.

### Friction Score

Each call receives a friction score (0–3) from two independent signals:

| Component   | Contribution                                                       |
| ----------- | ------------------------------------------------------------------ |
| Sentiment   | +1 if sentiment is **Frustrated** or **Angry**                     |
| Resolution  | +0 if **Yes**, +1 if **Partial**, +2 if **No**                     |

A score of **0** means the customer was calm and the agent delivered a definitive outcome. A score of **3** means the customer was upset *and* the call ended with nothing useful delivered. The Partial tier deliberately sits between — a calm customer with an open ticket is +1, not +2, and is ranked below a true drop. Sorting subcategories by **average friction score** (primary) and **call volume** (secondary) surfaces the highest-pain, highest-impact areas.

**Structured metadata signals deliberately excluded from the core score**, available as dashboard filters:

- `talk_time` > P90 — a very long call hints at complexity or multiple transfers
- `hangup_cause = ORIGINATOR_CANCEL` — customer hung up before resolution
- `duration − talk_time` — a large gap implies long holds

These fields are already in SQLite (populated from Ziwo) and require no LLM. Keeping them out of the score preserves cross-channel comparability with the chatbot pipeline; using them as filters lets analysts slice the dashboard without distorting rankings.

---

## Stage 4: Queue-Declared Intent (Parallel Dimension)

Ziwo's IVR routes each call into one of four **queues**, a self-selected category the customer chooses before reaching an agent. This is a free, deterministic, transcript-independent signal that captures what the customer *said* they wanted — a useful complement to the MECE category (what the call was *actually* about).

### Queue-name structure

Queue names follow the pattern `<Country>[_<Language>]_<QueueType>` and are parsed deterministically into three normalized columns:

| Raw `queue_name` example | `country` | `language` | `queue_intent` |
| ------------------------ | --------- | ---------- | -------------- |
| `ZA_Want-to-buy`         | ZA        | EN         | Want to Buy    |
| `ZA_Order-tracking`      | ZA        | EN         | Order Tracking |
| `ZA_Warranty`            | ZA        | EN         | Warranty       |
| `ZA_Other`               | ZA        | EN         | Other          |
| `UAE_EN_Want-to-buy`     | UAE       | EN         | Want to Buy    |
| `UAE_AR_Warranty`        | UAE       | AR         | Warranty       |
| `KSA_EN_Order-tracking`  | KSA       | EN         | Order Tracking |
| *(NULL)*                 | NULL      | NULL       | NULL           |

Parsing rules:

- Split on `_`. First segment is always the country (ZA, UAE, KSA).
- ZA queues have no language segment; `language` defaults to `"EN"` (ZA is single-language).
- UAE and KSA queues carry an explicit language segment (`EN` | `AR`).
- The final segment is the queue type, normalized to title case: `Want-to-buy → "Want to Buy"`, `Order-tracking → "Order Tracking"`, `Warranty → "Warranty"`, `Other → "Other"`.
- Unrecognized patterns and NULL queue names are preserved with `NULL` in all three columns — they are not dropped.

### Queue vs. category: misrouting signal

A fourth deterministic column, `queue_matches_category`, compares the customer's self-declared intent against the MECE category derived from the transcript:

| `queue_intent` | Expected `category`  | `queue_matches_category` |
| -------------- | -------------------- | ------------------------ |
| Want to Buy    | Pre-Purchase         | Yes / No                 |
| Order Tracking | Order Management     | Yes / No                 |
| Warranty       | After-Sales Support  | Yes / No                 |
| Other          | any                  | N/A — the customer opted out of categorization; no mismatch is possible |

Disagreements are not errors; they are **signals**:

- **High mismatch % in a named queue** points to IVR-menu UX problems — customers misread or misunderstand the options, pushing volume into the wrong queue.
- **Composition of the "Other" queue** — once the LLM has categorized every call, the "Other" queue can be broken down into "actually Pre-Purchase / actually Order Management / actually After-Sales / genuinely other". A big share of one of the three named categories inside "Other" means the named queues are phrased or ordered confusingly, and customers can't find them.

### Independence from LLM extraction

`queue_intent` is deliberately **not** passed into the LLM prompt as prior context. Keeping the two signals independent is what lets us measure divergence honestly. If the queue were fed to the LLM, the extracted intent would be biased toward agreement and the misrouting analytics would collapse.

### Not part of the friction score

Queue fields do not contribute to `friction_score`. Friction stays defined by sentiment + resolution only, preserving cross-channel comparability with the chatbot pipeline. Queue fields are used as filters and as the basis for misrouting analytics, not as score inputs.

---

## Dashboard Output

Each call produces one row with the following columns, exported to CSV and loaded into the dashboard:

| Column                                                       | Source                            |
| ------------------------------------------------------------ | --------------------------------- |
| `call_id`, `started_at`, `duration`, `talk_time`, `hangup_cause`, `queue_name`, `agent_id`, `caller_id_number` | Ziwo (already in SQLite)          |
| `transcript`, `transcript_language`                          | Stage 0 — Gemini transcription    |
| `call_summary`                                               | Stage 2 — LLM                     |
| `intent_action`, `intent_object`, `intent_qualifier`         | Stage 2 — LLM                     |
| `qualifier_theme`                                            | Stage 2 — LLM                     |
| `sentiment`                                                  | Stage 2 — LLM                     |
| `resolution` (Yes/Partial/No)                                | Stage 2 — LLM                     |
| `partial_reason` (nullable enum, set only when resolution=Partial) | Stage 2 — LLM                |
| `escalation_requested` (bool)                                | Stage 2 — LLM                     |
| `object_bucket`                                              | Stage 3 — deterministic normalization of `intent_object` |
| `category`, `subcategory`                                    | Stage 3 — deterministic mapping   |
| `friction_score` (0–3)                                       | Stage 3 — deterministic           |
| `country`, `language`, `queue_intent`, `queue_matches_category` | Stage 4 — deterministic parsing of `queue_name` |

The dashboard's primary views:

1. **Subcategory heatmap** — volume × average friction score; project candidates sit in the top-right quadrant
2. **Qualifier-theme drilldown** within each subcategory — splits high-friction areas by root cause (e.g., Order Tracking / Delivery Delays vs. Order Tracking / Status Inquiry)
3. **Cross-channel share** — what share of calls reference prior chatbot / website / app attempts; trend over time, a leading indicator of digital-channel health
4. **Queue misrouting & "Other" composition** — mismatch rate per named queue (Want to Buy, Order Tracking, Warranty); breakdown of the Other queue into derived MECE categories. Flags IVR UX problems and mislabeled menu options.
5. **Country / language / time-of-day slices** — using `country`, `language`, and timestamp fields as filters across all other views, not as friction signals

---

## Pipeline Files

| File                               | Purpose                                                         |
| ---------------------------------- | --------------------------------------------------------------- |
| `ziwo/preprocess.py` (planned)     | Cleans raw transcripts                                          |
| `ziwo/extract.py`                  | LLM feature extraction (summary, intent, theme, sentiment, resolution) |
| `ziwo/mece.py` (planned)           | Deterministic MECE categorization + friction scoring            |
| `ziwo/queues.py` (planned)         | Deterministic queue-name parsing + `queue_matches_category` logic |
| `data/calls.db`                    | SQLite source of truth (raw metadata, transcripts, extractions) |
| `data/exports/calls_{date}.csv`    | Dashboard-ready export                                          |

---

## Notes and Open Considerations

- **Audio-based sentiment** is expected to be materially better than transcript-only for phone calls, and the case is stronger now that transcripts are English translations rather than verbatim text. Translation drops tone-carrying particles and register shifts on top of the tone/volume/silence signal already lost when going from audio to text. Not adopted in the POC to keep cost and latency low. Straightforward to upgrade by passing the mp3 bytes instead of the transcript string to Gemini. Revisit after the first dashboard review, or whenever a decision becomes sentiment-critical.
- **Sample size** — 179 calls is enough to validate the methodology end-to-end but too small to rank subcategories with high confidence. Treat Phase-1 dashboard numbers as directional. A meaningful volume target for trustworthy rankings is ~1000+ calls per analyzed window.
- **Batching** — for the POC, per-call extraction is simpler and the cost is trivial. At ~1000 calls/day, batch ~10 transcripts per LLM call to cut cost/latency; add rate limiting.
- **Cross-channel comparability** — the MECE tree is intentionally close to the chatbot version (minus the calls-specific additions). This lets volume and friction be compared like-for-like across channels, so we can answer questions like "of all customer contacts about Delivery Issues, what share arrive via chatbot vs. phone?".
- **Agent performance is out of scope** for this pipeline. If added later, it would go as a separate axis (per-call agent QA scores), not by changing the friction score.

---

## Appendix: Pipeline cutover history

Structural changes that create discontinuities in the corpus and should be annotated on any over-time view:

- **2026-04-21 — English-translation transcription.** The transcription prompt switched from verbatim (original language) to English-translated output. Pre-existing Arabic transcripts were backfilled via text-to-text translation (`scripts/translate_transcripts.py`). The `transcript_language` column still reports the source language.
- **2026-04-21 — Extraction prompt v2 (English-input).** `ziwo/extract.py` prompt was retuned for English input: dropped the "return values in English" redundancy, added cue phrases for `partial_reason` discrimination, added a "mostly [unintelligible]/[Hold music] → No" resolution rule, and tightened `call_summary` to past-tense narrative. **Historical rows were intentionally NOT re-extracted** — they retain extractions produced directly from Arabic source transcripts, which preserve more signal than a second pass over translated text would. Expect a small break in `partial_reason` distribution and `call_summary` prose style at the cutover date; this is a prompt-version artifact, not a behavioral change.
- **2026-04-21 — 45s minimum talk-time filter at ingest.** Calls shorter than 45s are no longer ingested (started at 30s, bumped to 45s same-day). One-shot `scripts/delete_short_calls.py` removed existing short calls. Volume counts before this date include those short calls; after, they do not.
- **2026-04-22 — `classified` status + `enrich` step.** The `queues` and `mece` CLI subcommands were replaced by a single `enrich` subcommand (`ziwo/enrich.py`) that runs both passes on `extracted` rows and advances status to `classified`. Both passes are now scoped to `extracted` only — previously-enriched rows are never re-touched (important for the MySQL order lookup, which has a 60-day window). A one-time backfill bumped all existing `extracted`-with-category rows to `classified` so they remain visible to the dashboard export. `scripts/export_dashboard.py` was replaced by the `export` subcommand (`ziwo/export.py`), which now filters on `status='classified'`.

---

## Appendix: the "Other" theme long tail

With LLM classification the expected `"Other"` share is materially lower than the chatbot pipeline's ~40% (which was driven by keyword-match failures on paraphrases). For calls the target is ~10% or less: the LLM should only return "Other" when no theme genuinely applies. A spike above that range is a signal to revisit the enum — either to add a missing theme or to tighten the prompt.

Policy for handling "Other":

1. **Don't chase singletons.** If the residual "Other" bucket is dominated by one-off phrasings that each cover fewer than ~15 calls, leave them unclassified rather than adding micro-themes that clutter the dashboard.
2. **High-friction "Other" subcategories are already actionable** — when a subcategory has both a high "Other" share and high friction, the subcategory label itself is usually specific enough (e.g., "Order Cancellation", "Technical Support") to drive a project without a dedicated theme.
3. **Revisit the enum on sustained drift.** If "Other" share rises over time, it typically indicates a new call driver that deserves its own theme rather than an LLM failure.
