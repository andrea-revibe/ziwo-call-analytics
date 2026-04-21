# Transcript Optimization Plan — Strip Non-Speech Before Transcription

**Status:** Phase 1 complete (2026-04-21). Phase 2 not started.
**Drafted:** 2026-04-20
**Revised:** 2026-04-21 — switched from inaSpeechSegmenter to silero-vad after Phase 1 dep friction; see appendix for context.

## Problem

Hold music is being transcribed as speech. Two observed symptoms:

1. **Cost/latency inflation** — long calls with multi-minute hold stretches still send the full audio to Gemini.
2. **Hallucinated repetition** — Gemini fills music stretches by repeating earlier speaker turns verbatim. Example: call `695764` (1553s, ~26 min) produced a 225KB transcript with large sections of the same turns duplicated 3–5× in a row. This breaks downstream extraction and friction scoring because repeated complaints look like escalation.

`clean_transcript()` in `ziwo/transcribe.py` already collapses `[Music]`/`[Hold music]` tags post-hoc, but that only helps when Gemini correctly labels the music. When it hallucinates speech instead, we have no signal.

## Goal

Detect non-speech (music, silence, noise) **before** transcription and send only speech-bearing audio to Gemini. Secondary win: a per-call `non_speech_fraction` metric for dashboard filtering (a high value flags abandoned-on-hold or deserted-IVR calls).

## Detection approach

**silero-vad v4.0 ONNX** via `onnxruntime` — pure ONNX, no torch/tensorflow dependency, actively maintained. Labels are binary (speech / non-speech) at ~32ms resolution (512-sample windows @ 16kHz).

- Model is downloaded once to `~/.cache/silero-vad/silero_vad_v4.onnx` (~1.8MB).
- Decoding is via `ffmpeg` (already a system dep) — mono f32 PCM at 16kHz.
- Stateful LSTM inference, ~60× real-time on CPU.

Already implemented in `ziwo/segment.py`:
- `analyze(audio_path) → (segments, stats)` — returns labeled intervals + aggregate timings.
- `SegmentStats` dataclass with `speech_seconds`, `non_speech_seconds`, `total_seconds`, and derived `speech_fraction` / `non_speech_fraction`.

Phase 2 will add `write_speech_only()` for trimming (sketch below).

## Phase 1 findings (2026-04-21 sample, n=69 calls)

Stratified sample of 69 calls (20 short / 20 mid / 20 long by talk_time, plus the 10 longest for detailed interval dumps). Raw data in `scratch/segment_survey.csv`.

**Aggregate:**

| Metric | Value |
|---|---|
| Total audio in sample | 841 min |
| Speech-only (post-trim estimate) | 367 min |
| Non-speech savings | **56.4%** of total duration |

**Distribution of `non_speech_fraction`:**

| Percentile | Value |
|---|---|
| min | 0.25 |
| p25 | 0.39 |
| p50 (median) | 0.47 |
| p75 | 0.64 |
| p90 | 0.71 |
| p95 | 0.78 |
| max | 0.93 |

Normal calls are ~half non-speech (pauses, turn gaps, brief hold). Only the tail is dominated by hold music.

**Skip-threshold sensitivity:**

| Threshold | Calls skipped / 69 |
|---|---|
| `non_speech_fraction ≥ 0.75` | 7 |
| `≥ 0.85` | 2 (694825, 695280) |
| `≥ 0.90` | 1 (694825) |
| `≥ 0.95` | 0 |

`speech_seconds < 5` never fires in this sample — the 45s `MIN_TALK_TIME` ingest filter already drops those.

**Calls flagged as candidates for `skipped_non_speech` (ground-truth pending):**

| Call | talk_time | non_speech_fraction |
|---|---|---|
| 694825 | 729s | 0.929 |
| 695280 | 450s | 0.895 |
| 695209 | 762s | 0.800 |
| 695119 | 1673s | 0.793 |
| 695385 | 1269s | 0.778 |

**Call 695764 (the hallucinated-repetition case):** 43.6% speech / 56.4% non-speech — a *typical* call, not an outlier. The hallucination problem isn't "mostly music calls"; it's moderate music stretches confusing Gemini. Trimming still reclaims meaningful audio: 1553s → 677s of speech.

Detail files per call (speech/non-speech intervals, second-level): `scratch/segment_detail/{id}.txt`.

## Proposed pipeline shape

New step `preprocess` between `downloaded` and `transcribed`:

```
pending → downloaded → preprocessed → transcribed → extracted
                                   ↘ skipped_non_speech (if call is mostly silence/music)
```

`preprocess` produces:
- `data/audio/{id}.trimmed.mp3` — speech-only concatenation with 200ms flanking padding per segment. Original `{id}.mp3` retained for A/B and reprocessing.
- DB fields: `audio_path_trimmed`, `speech_seconds`, `non_speech_seconds`, `non_speech_fraction`.

`transcribe` prefers `audio_path_trimmed` when present; falls back to `audio_path`.

## Decisions (resolved during Phase 1)

| Question | Resolution |
|---|---|
| Goal weighting | Both cost/latency and hallucination prevention — both have measurable wins. |
| Segmenter | silero-vad v4.0 ONNX. (See appendix for why not inaSpeech / v5.) |
| Pipeline shape | Separate `preprocess` step — state-machine consistency, allows reprocessing without re-download. |
| Keep originals? | Yes. Disk is trivial at POC scale; keeping both enables A/B and reprocessing. |
| Segment padding | 200ms flanking per speech segment to avoid clipping word edges. |
| Re-run on transcribed calls | Skip by default; no `--force` flag in v1. |
| Ground-truth validation | Required before thresholds lock in (3 calls: 694825, 695280, 695764). |
| Column shape | 4 DB columns (see below) — music/silence/noise collapse into `non_speech_*` because silero doesn't distinguish them. |

### Outstanding decision (Phase 2 kickoff)

**Threshold values** — tentative defaults, validate via ground-truth before Phase 2 code merges:

```python
NON_SPEECH_SKIP_THRESHOLD = 0.90  # skips 1 call / 69 in sample
MIN_SPEECH_SECONDS = 5.0           # safety net; never fires today thanks to ingest filter
```

If ground-truth on 694825 confirms it really is 100% hold music, 0.90 is safe. If 695280 (0.895) also looks music-only, consider dropping to 0.85.

## Code sketches (Phase 2)

### Add to `ziwo/segment.py`

```python
import subprocess

def write_speech_only(
    audio_path: Path,
    segments: list[tuple[str, float, float]],
    out_path: Path,
    pad_seconds: float = 0.2,
) -> bool:
    """Concat speech segments (with flanking padding) via ffmpeg. Returns
    True if the output was written, False if there were no speech segments."""
    speech = [(s, e) for lab, s, e in segments if lab == "speech"]
    if not speech:
        return False

    # pad + merge overlapping intervals
    padded: list[tuple[float, float]] = []
    for s, e in speech:
        s = max(0.0, s - pad_seconds)
        e = e + pad_seconds
        if padded and s <= padded[-1][1]:
            padded[-1] = (padded[-1][0], max(padded[-1][1], e))
        else:
            padded.append((s, e))

    # ffmpeg aselect filter — keep only samples inside any speech interval
    between = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in padded)
    filter_expr = f"aselect='{between}',asetpts=N/SR/TB"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-af", filter_expr,
            "-b:a", "64k",
            str(out_path),
        ],
        check=True,
    )
    return True
```

### New CLI subcommand: `pipeline.py preprocess`

```python
# in pipeline.py
def cmd_preprocess(args):
    from ziwo.segment import analyze, write_speech_only
    from ziwo.db import connect, list_by_status, update_status

    NON_SPEECH_SKIP_THRESHOLD = 0.90  # tentative — validate on ground truth
    MIN_SPEECH_SECONDS = 5.0           # safety net

    with connect() as conn:
        rows = list_by_status(conn, "downloaded", limit=args.limit)
        for row in tqdm(rows, desc="preprocess"):
            audio_path = Path(row["audio_path"])
            if not audio_path.exists():
                update_status(conn, row["id"], "failed", error_message="audio missing")
                continue
            try:
                segments, stats = analyze(audio_path)

                if (
                    stats.non_speech_fraction >= NON_SPEECH_SKIP_THRESHOLD
                    or stats.speech_seconds < MIN_SPEECH_SECONDS
                ):
                    update_status(
                        conn, row["id"], "skipped_non_speech",
                        speech_seconds=stats.speech_seconds,
                        non_speech_seconds=stats.non_speech_seconds,
                        non_speech_fraction=stats.non_speech_fraction,
                    )
                    continue

                trimmed_path = audio_path.with_suffix(".trimmed.mp3")
                write_speech_only(audio_path, segments, trimmed_path, pad_seconds=0.2)
                update_status(
                    conn, row["id"], "preprocessed",
                    audio_path_trimmed=str(trimmed_path),
                    speech_seconds=stats.speech_seconds,
                    non_speech_seconds=stats.non_speech_seconds,
                    non_speech_fraction=stats.non_speech_fraction,
                )
            except Exception as e:
                update_status(conn, row["id"], "failed", error_message=f"preprocess: {e}")
            conn.commit()
```

### Schema migration (`ziwo/db.py`)

Add to both `SCHEMA` (CREATE TABLE) and `EXTENSION_COLUMNS`:

```python
# in EXTENSION_COLUMNS
"audio_path_trimmed": "TEXT",
"speech_seconds": "REAL",
"non_speech_seconds": "REAL",
"non_speech_fraction": "REAL",
```

### `transcribe.py` change

```python
# prefer trimmed audio if present
audio_path = row["audio_path_trimmed"] or row["audio_path"]
```

And update the status filter to read `preprocessed` rows instead of `downloaded`.

## Expected impact

Based on Phase 1 numbers (n=69, 56.4% corpus-wide non-speech):

- **Cost:** ~half the audio sent to Gemini on average. Long calls benefit most (the top 10 by absolute non-speech time would save 6.5 to 30 min of audio each).
- **Hallucination fix:** for call `695764`, input drops from 1553s → ~677s of speech. Gemini never sees the music stretches that trigger the repetition.
- **Skipped calls:** ~1–2% of volume likely to hit `skipped_non_speech` based on the sample. These are abandoned-on-hold customer drops where transcription would have cost something for near-zero signal.

## Rollout checklist

### Phase 1 — tune on existing mp3s (complete, 2026-04-21)
- [x] `brew install ffmpeg` — done
- [x] Add `onnxruntime>=1.17` to requirements — done
- [x] Implement `ziwo/segment.py::analyze` — done
- [x] Survey script `scratch/segment_survey.py` — resumable, writes CSV + per-call interval dumps
- [x] Run on 69-call stratified sample + 10-call detail dump — done
- [x] Smoke-test on 695764 — 43.6% speech, 1477 segments, consistent with manual expectation
- [ ] **Ground-truth eyeball on 694825, 695280, 695764** — pending user spot-check

### Phase 2 — implement (pending Phase 1 ground-truth)
- [ ] Lock final thresholds (`NON_SPEECH_SKIP_THRESHOLD`, `MIN_SPEECH_SECONDS`) based on ground-truth
- [ ] Implement `ziwo/segment.py::write_speech_only` (sketch above)
- [ ] Schema migration in `ziwo/db.py` (4 new columns in both `SCHEMA` and `EXTENSION_COLUMNS`)
- [ ] `pipeline.py preprocess` subcommand (sketch above)
- [ ] Update `transcribe.py` to consume `audio_path_trimmed` + filter on `preprocessed` status
- [ ] Update `run` meta-command to include `preprocess` between `download` and `transcribe`
- [ ] Backfill: reprocess ~5 already-`transcribed` calls where hallucination was suspected (incl. 695764), compare transcript quality side-by-side
- [ ] Update `CLAUDE.md` pipeline diagram + subcommand list
- [ ] Append cutover entry to `docs/transcript-analysis-methodology.md` — "Appendix: Pipeline cutover history"

## Appendix: Why silero-vad over inaSpeechSegmenter

The original draft of this plan specified `inaSpeechSegmenter` because it labels music / noise / silence separately, which promised a richer `music_fraction` dashboard metric than a binary speech/non-speech call.

During Phase 1 we spent four rounds fighting its dependency tree:

1. `numpy 2.x` removed `numpy.lib.pad` — had to pin `numpy<2`.
2. `pyannote.algorithms` (abandoned) passes generators to `np.vstack` — had to monkey-patch both call sites in site-packages.
3. TensorFlow 2.16+ ships Keras 3, which reads the bundled model's `input_shape` differently — had to install `tf-keras` + set `TF_USE_LEGACY_KERAS=1`.
4. After all that it ran correctly, but at the cost of three pins and a monkey-patch against an abandoned dep chain.

**Why we switched:** the only thing we lose going to silero is the music-vs-noise-vs-silence distinction, and for our actual use case (trim non-speech before Gemini) that distinction doesn't matter — they all get trimmed either way. For the dashboard, "non_speech_fraction" is arguably more useful as a single clarity signal ("was this call mostly not-speech?") than three separate fractions.

**What silero gets us:**
- Pure ONNX runtime (`onnxruntime`, ~20MB) — no torch, no tensorflow.
- Actively maintained, single `pip install`.
- Runs ~60× real-time on CPU.
- A single binary speech/non-speech label.

**Note on v4 vs v5:** silero-vad is on v5.1.2 as of this writing, but the v5 ONNX model returns ~0 probabilities for all audio under direct `onnxruntime` invocation, suggesting an undocumented preprocessing step or a regression. v4.0's ONNX interface (separate `h` / `c` LSTM state inputs, explicit `sr` arg) works correctly out of the box. Revisit if/when v5 stabilises.
