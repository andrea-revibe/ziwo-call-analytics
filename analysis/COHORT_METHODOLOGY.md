# Cohort Methodology — CC Improvement Programme

This folder contains the canonical, runnable definitions of the seven project cohorts proposed in the Customer Service Improvement Programme pre-read. The pre-read's headline numbers are derived from `audit_cohorts.py`; treat that script as the single source of truth.

## Why this exists

The CC pre-read sizes seven projects against per-call signals (friction, resolution, escalation, repeat-caller rate, etc.). Several projects are scoped using regex or substring filters over the AI-generated `call_summary` field, and several depend on the AI-generated `qualifier_theme` and `partial_reason` labels. Both of those are produced by the Gemini extraction in `ziwo/extract.py`.

When the extraction prompt changes — for example, when we tighten the `callback_promised` definition to reduce false positives — every cohort downstream of those labels will shift. Without a versioned baseline and reproducible audit, we cannot tell whether a future number change is:

- **Data drift** — actual customer behaviour changed
- **Methodology drift** — the cohort filter changed
- **Pipeline drift** — the AI prompt or model changed

This folder lets us tell them apart.

## What's in here

| File | Purpose |
|---|---|
| `audit_cohorts.py` | Runnable script. Defines all 7 cohort filters as Python functions, computes KPIs, writes a JSON snapshot. Optionally samples N calls per cohort for precision audit. |
| `baseline_<window>_<version>.json` | Locked JSON snapshot of cohort sizes, KPIs, and per-country splits. Compare future runs against the baseline to detect drift. |
| `baseline_<window>_<version>_audit.json` | Optional companion file: sampled call summaries per cohort for precision audit. |
| `COHORT_METHODOLOGY.md` | This file. |
| `CHANGELOG.md` | Tracks every change to a cohort filter or the methodology version. |

## How to run

```bash
# Default: replicates the v1.0 baseline (Apr 9-22, 2026) using current code
python audit_cohorts.py

# After a pipeline change — bump version, write to a new file
python audit_cohorts.py --methodology-version v1.1 \
    --output baseline_2026-04-09_to_2026-04-22_v1.1.json

# Custom window
python audit_cohorts.py --start 2026-05-01 --end 2026-05-31 \
    --methodology-version v1.0 \
    --output snapshot_2026-05-01_to_2026-05-31_v1.0.json

# With precision audit (samples 20 calls per cohort)
python audit_cohorts.py --audit 20 --seed 1337
```

The script reads CSVs from `~/Desktop/ziwo-dashboard/data/` by default (the dashboard's local data directory). Override with `--data-dir`.

## Cohort definitions — quick reference

Full definitions live as docstrings on the filter functions in `audit_cohorts.py`. This table is the at-a-glance summary; use the script as the source of truth.

| Code | Project | Filter type | Expected precision |
|---|---|---|---|
| **DEL-03** | Callback Promise Reliability | Exact field match: `partial_reason == 'callback_promised'` | ~66% strict / ~90% inclusive — broad pipeline label, addressed by upstream change in v1.1 |
| **DEL-01** | Carrier Tracking Granularity | qualifier_theme: `Status Inquiry` OR `Shipping Provider Issue` (with subcategory check) | ~85% — qualifier_theme noise (some product-info calls slip in), addressed by upstream change in v1.1 |
| **REF-01** | PSP Refund Velocity | Refund Processing + tight regex requiring `refund` near a delay/timing phrase | ~94% |
| **RET-02** | Procedural Claim Recovery | Return Process + tight regex OR Warranty Claims + tight regex (union) | ~100% |
| **DEL-02** | Customs Hold Resolution | Delivery Issues + `customs` substring | ~94% |
| **RET-01** | Warranty QC Visibility | qualifier_theme: `Quality Check Wait` | ~99% |
| **REF-02** | Refund Choice & Visibility | Refund Processing + tight regex requiring an explicit problem signal | ~92% |

## How to interpret a number change between two snapshots

When comparing two `baseline_*.json` files (typically before and after a pipeline change):

1. **Check `methodology_version` first.** Different versions = expected difference.
2. **Compare `country_totals`.** If those changed, the underlying call volume changed (data drift), not the methodology.
3. **Per-cohort comparison logic:**
   - **Cohort `n` changed dramatically (>20%)**: likely a pipeline label change (qualifier_theme, partial_reason). Cross-reference CHANGELOG.md to confirm which prompt change caused it.
   - **`avg_friction` or `escalation_pct` changed**: could be data drift OR could be the cohort recomposing (e.g., tighter filter excludes lower-friction calls, raising the average). Sanity-check against the precision-audit samples.
   - **`repeat_caller_pct_of_calls` changed by >10pp**: usually means cohort scope changed. Repeat-caller patterns are sticky for a given cohort definition.
   - **`impact_score` changed**: derived metric — moves with `n`, `friction`, and `escalation`. A change here is interpretable only after explaining the underlying KPI changes.
4. **Use the audit samples** (`*_audit.json`) to spot-check whether the precision changed for the right reasons. If the new sample has fewer obvious false positives, the change worked.

## Bumping the methodology version

When making any change that affects what calls land in a cohort:

1. Edit `audit_cohorts.py`. Update the relevant filter function and its docstring.
2. Bump the default `--methodology-version` in `audit_cohorts.py` (or always pass `--methodology-version` explicitly going forward).
3. Re-run on the same baseline window to produce `baseline_<window>_<new_version>.json`.
4. Add a CHANGELOG entry describing what changed and the observed impact (cohort delta, precision delta).
5. **Keep the previous baseline file.** Never overwrite. The diff between baselines is the audit trail.

## Pipeline-side dependencies

Two AI-generated fields drive most cohort definitions and are the most likely source of upstream changes:

| Field | Source | Cohorts depending on it |
|---|---|---|
| `partial_reason` (specifically `callback_promised`) | `ziwo/extract.py:160` | DEL-03 (entirely) |
| `qualifier_theme` | `ziwo/extract.py:160` (theme list at L133-143) | DEL-01, DEL-02 (subcategory check), RET-01, plus theme filter on REF-01, REF-02, RET-02 |

When changing either of those in the prompt:

1. Capture the current baseline first (don't change the prompt before snapshotting — irreversible loss of the comparison point).
2. Re-process the data with the new prompt.
3. Run `audit_cohorts.py` with a new `--methodology-version`.
4. Diff old vs new snapshots and document in CHANGELOG.

## Known limitations

- **10-day window**: the v1.0 baseline (Apr 9-22, 2026) is short for the smaller cohorts (REF-02 at n=39 in particular). Longer windows give more stable per-country shares.
- **`partial_reason='callback_promised'` is a broad pipeline label**: see DEL-03 docstring. The upper-bound caveat is documented in the pre-read.
- **Repeat-caller % is a floor**: only counts callers whose multiple calls both fall inside the window. Real rates are higher.
- **Sub-pattern audits use small samples**: precision figures (~85%, ~94%, etc.) are based on 20-50 sampled calls per cohort across multiple seeds. They're directionally accurate but not statistically tight.
