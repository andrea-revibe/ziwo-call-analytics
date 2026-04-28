# Cohort Methodology Changelog

Tracks every change to a cohort filter or to the analytics pipeline that produces the labels cohorts depend on. Format: one entry per methodology version.

## v1.0 — 2026-04-27 — Baseline lock

**Context:** First versioned snapshot of the seven-project portfolio defined in the CC Improvement Programme pre-read. Captures the state of the pipeline and cohort filters as they exist on 2026-04-27, before any planned upstream improvements ship.

**Cohort filters (see `audit_cohorts.py` for code):**

| Code | Filter approach | Audit precision |
|---|---|---|
| DEL-03 | `partial_reason == 'callback_promised'` (broad pipeline label) | ~66% strict / ~90% inclusive |
| DEL-01 | `qualifier_theme` in `Status Inquiry` / `Shipping Provider Issue` | ~85% |
| REF-01 | Refund Processing + tight regex (refund near delay phrase) | ~94% |
| RET-02 | Return Process + tight regex OR Warranty Claims + tight regex (union) | ~100% |
| DEL-02 | Delivery Issues + `customs` substring | ~94% |
| RET-01 | `qualifier_theme == 'Quality Check Wait'` | ~99% |
| REF-02 | Refund Processing + tight regex (explicit problem signal) | ~92% |

**Baseline numbers (Apr 9-22, 2026, 2,930 unique calls):**

| Code | n | Avg friction | Esc % | Repeat % | Impact |
|---|---|---|---|---|---|
| DEL-03 | 951 | 1.71 | 12.5% | 51.7% | 1,864 |
| DEL-01 | 320 | 0.98 | 3.4% | 30.0% | 336 |
| REF-01 | 118 | 1.77 | 16.9% | 37.3% | 249 |
| RET-02 | 78 | 1.74 | 7.7% | 15.4% | 148 |
| DEL-02 | 79 | 1.56 | 10.1% | 36.7% | 139 |
| RET-01 | 58 | 1.59 | 12.1% | 29.3% | 106 |
| REF-02 | 39 | 1.49 | 7.7% | 41.0% | 64 |

**Snapshot file:** `baseline_2026-04-09_to_2026-04-22_v1.0.json`
**Audit file:** `baseline_2026-04-09_to_2026-04-22_v1.0_audit.json`

**Known limitations of v1.0:**
- DEL-03 cohort is over-broad — pipeline label `callback_promised` includes ~30-50% calls that are not Revibe-side outbound contact commitments (agent advice, internal escalations, customer-side actions). Addressed in v1.1 by tightening the prompt definition.
- DEL-01 cohort has ~15% noise — `qualifier_theme = Status Inquiry` catches some product-feature questions about existing orders. Addressed in v1.1 by adding theme disambiguation guidance to the prompt.

---

## v1.1 — 2026-04-27 — Smoke-test only, superseded by v1.1.1

**Status:** Drafted, smoke-tested on Apr 22 only, NEVER deployed to full re-extract. Superseded by v1.1.1 below.

**What was attempted:**

1. Tightened `callback_promised` definition in `ziwo/extract.py:160` to require Revibe-side outbound contact directed at the customer.
2. Added theme disambiguation guidance after `ziwo/extract.py:143` for `Status Inquiry` / `Shipping Provider Issue` / `Quality Check Wait` / `Delivery Delays` / `Other`.

**Why superseded:** smoke test on Apr 22 (288 calls) showed callback_promised cohort reduced by only −9% (target was −30 to −50%). Detailed audit found Gemini was correctly removing 18/93 over-broad classifications BUT also adding 12 new false positives where the agent's commitment was past-tense ("agent provided email") or customer-side ("agent advised customer to..."). The original v1.1 prompt's anti-examples weren't strong enough to prevent these.

**Key learning:** the include cues (`"the team will reach out to you"`, `"we'll send you a tracking link"`) were lexically matching past-tense or completed actions. v1.1.1 adds explicit FUTURE-tense requirement and a stronger DEFAULT rule.

---

## v1.1.1 — 2026-04-27 — SHIPPED

**Status:** Deployed. Full re-extraction completed for the Apr 9-22 window. Snapshot file: `baseline_2026-04-09_to_2026-04-22_v1.1.1.json`.

**What shipped (4 diffs in a single logical change):**

| File | Location | Change |
|---|---|---|
| `ziwo/extract.py` | callback_promised definition (~L171) | Three-part requirement (FUTURE-TENSE + REVIBE-INITIATED + SPECIFIC) with explicit DEFAULT-NOT rule and 7 categories of anti-examples |
| `ziwo/extract.py` | After theme list (~L145) | Theme disambiguation guidance for `Status Inquiry`, `Shipping Provider Issue`, `Quality Check Wait`, `Delivery Delays`, `Other`, plus product-feature routing rules |
| `docs/transcript-analysis-methodology.md` | callback_promised entry (~L103) | Lockstep update of the doc to match the new prompt definition |
| `docs/transcript-analysis-methodology.md` | After themes table (~L84) | New "Theme disambiguation rules" paragraph |

**Cohort changes (v1.0 → v1.1.1):**

| Code | n | Δn | Δ% | fric | esc% | impact | Notes |
|---|---|---|---|---|---|---|---|
| DEL-03 | 951 → 843 | −108 | −11% | 1.71 → 1.71 | 12.5 → 14.4 | 1,864 → 1,684 | Cleaner cohort; esc% UP, fric stable; less reduction than target but precision improved more (66% → 88%) |
| DEL-01 | 320 → 498 | **+178** | **+56%** | 0.98 → 1.20 | 3.4 → 5.8 | 336 → 656 | New theme guidance correctly routes carrier-side calls (was misclassified as Delivery Delays / Cancellation / etc.) |
| REF-01 | 118 → 121 | +3 | +3% | 1.77 → 1.67 | 16.9 → 14.9 | 249 → 238 | Stable (independent of changed labels) |
| RET-02 | 78 → 78 | 0 | 0% | 1.74 → 1.72 | 7.7 → 11.5 | 148 → 152 | Stable |
| DEL-02 | 79 → 102 | +23 | +29% | 1.56 → 1.33 | 10.1 → 8.8 | 139 → 154 | More customs-related calls correctly classified into Delivery Issues |
| RET-01 | 58 → 135 | **+77** | **+133%** | 1.59 → 1.38 | 12.1 → 10.4 | 106 → 214 | Quality Check Wait now correctly captures QC-stuck calls across multiple themes (Refund Processing, Claim Status, Delivery Delays) |
| REF-02 | 39 → 32 | −7 | −18% | 1.49 → 1.59 | 7.7 → 6.2 | 64 → 55 | Slight further drop |

**Project ranking — top 3 unchanged. RET-01 swapped up to rank 4 (was rank 6). RET-02 swapped down to rank 6 (was rank 4).**

**Per-country DEL-03 share** (the headline number from the pre-read):

| | v1.0 | v1.1.1 |
|---|---|---|
| UAE | 33.2% | 26.8% |
| KSA | 38.6% | 34.5% |
| ZA | 25.6% | 23.6% |

Headline shifts from "32% of all calls" → "~28% of all calls".

**Audit precision improvements (50 calls per cohort, fresh seed 5000):**

| Project | v1.0 precision | v1.1.1 precision | Δ |
|---|---|---|---|
| DEL-03 | ~66% | ~88% | **+22pp** |
| DEL-01 | ~85% | ~98% | **+13pp** |
| REF-01 | ~91% | 100% | +9pp |
| RET-01 | ~99% | ~98% | comparable |
| DEL-02 | ~94% | 100% | +6pp |
| RET-02 | ~100% | 100% | unchanged |
| REF-02 | ~85% | 100% | +15pp |

**All cohorts now ≥88% precision. Five of seven at 98-100%.**

**Cascading side effects to be aware of:**

1. **Partial → Yes resolution shifts.** Smoke test showed ~7% of calls flipped from `Partial` to `Yes` resolution because the new `callback_promised` definition is stricter — calls that previously had Partial+callback_promised but didn't fit any new partial_reason category were correctly reclassified to Yes (action completed in-call). This affects:
   - Avg friction (Partial = +1 friction; Yes = +0). Dataset-wide friction floor drops slightly.
   - Partial % metric on the dashboard (decreases by ~5-7pp window-wide).
   - Unresolved % is unchanged (No is still No).
   - **Implication:** v1.0 vs v1.1.1 friction comparisons are NOT apples-to-apples. Stakeholders who saw v1.0 dashboard numbers should be flagged that v1.1.1 friction is technically more accurate but moved due to methodology, not data drift.

2. **Baseline LLM noise (independent of prompt change):** even at temperature 0.1, Gemini drifts ~16-18% on subjective fields (`sentiment`, `intent_action`) on any re-extraction. This is unavoidable and should be expected on every future re-extract. Worth a footnote when comparing snapshots over time.

3. **Smoke test under-predicted full-window growth.** The Apr 22 smoke test (288 calls) predicted +5/+16 cohort growth for DEL-01/RET-01; full window saw +178/+77. Single-day smoke tests are useful for prompt validation (does it parse, does it move in the right direction?) but poor for sizing prediction.

**Validation evidence:**

- Two precision audits run with seeds 1337 and 5000, ~50 calls per cohort each
- All cohorts validated at ≥88% precision (most at 98-100%)
- DEL-01 +178 additions audited at 100% precision (all carrier/tracking related)
- RET-01 +77 additions audited at ~88% precision (small over-trigger on non-strict QC mentions, acceptable)
- Snapshot file: `baseline_2026-04-09_to_2026-04-22_v1.1.1.json`
- Audit samples: `baseline_2026-04-09_to_2026-04-22_v1.1.1_audit.json`

**Cost & time of full re-extraction:**

- 2,941 calls × Gemini 2.5 Flash (1 call each)
- Time: ~30-40 minutes wall-clock with `EXTRACT_CONCURRENCY=10`
- Cost: estimated $2-5 in Gemini API charges
- 1 call failed during extraction (well within normal noise; not in cohort)

**Backward compatibility:**

- DB backup: `data/calls.db.v1.0.backup` (preserved for rollback)
- Dashboard CSV backup: `~/Desktop/ziwo-dashboard/data.v1.0.backup/` (preserved)
- Rollback procedure documented in the pre-read rollout plan
