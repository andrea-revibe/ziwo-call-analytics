# Transcript Optimization Plan — Strip Hold Music Before Transcription

**Status:** Planned, not started. Parked behind other priorities.
**Drafted:** 2026-04-20

## Problem

Hold music is being transcribed as speech. Two observed symptoms:

1. **Cost/latency inflation** — long calls with multi-minute hold stretches still send the full audio to Gemini.
2. **Hallucinated repetition** — Gemini fills music stretches by repeating earlier speaker turns verbatim. Example: call `695764` (1604s, ~26 min) produced a 200KB transcript with large sections of the same turns duplicated 3–5× in a row. This breaks downstream extraction and friction scoring because repeated complaints look like escalation.

`clean_transcript()` in `ziwo/transcribe.py` already collapses `[Music]`/`[Hold music]` tags post-hoc, but that only helps when Gemini correctly labels the music. When it hallucinates speech instead, we have no signal.

## Goal

Detect music/silence **before** transcription and send only speech-bearing audio to Gemini. Secondary win: a per-call `music_fraction` metric for dashboard filtering.

## Proposed pipeline shape

New step `preprocess` between `downloaded` and `transcribed`:

```
pending → downloaded → preprocessed → transcribed → extracted
                                   ↘ skipped_music (if call is ~all music)
```

`preprocess` produces:
- `data/audio/{id}.trimmed.mp3` — speech-only concatenation.
- DB fields: `speech_seconds`, `music_seconds`, `silence_seconds`, `music_fraction`, `audio_path_trimmed`.

`transcribe` prefers `audio_path_trimmed` when present; falls back to `audio_path`.

## Detection approach

Use **`inaSpeechSegmenter`** — pretrained speech/music/noise/silence segmenter, CPU-only, returns labeled intervals. Purpose-built for exactly this. Alternative: `silero-vad` (speech vs non-speech only — doesn't distinguish music from speech as cleanly).

Dependencies to add:
- `ffmpeg` (system, via `brew install ffmpeg`)
- `inaSpeechSegmenter` (pip)
- `pydub` (pip) — for concatenating speech segments

## Code sketches

### New module: `ziwo/segment.py`

```python
from dataclasses import dataclass
from pathlib import Path

from inaSpeechSegmenter import Segmenter
from pydub import AudioSegment

# Singleton — model load is slow (~seconds), reuse across calls.
_segmenter: Segmenter | None = None

def _get_segmenter() -> Segmenter:
    global _segmenter
    if _segmenter is None:
        _segmenter = Segmenter(vad_engine="smn", detect_gender=False)
    return _segmenter


@dataclass
class SegmentStats:
    speech_seconds: float
    music_seconds: float
    silence_seconds: float
    noise_seconds: float
    total_seconds: float

    @property
    def music_fraction(self) -> float:
        return self.music_seconds / self.total_seconds if self.total_seconds else 0.0


def analyze(audio_path: Path) -> tuple[list[tuple[str, float, float]], SegmentStats]:
    """Run segmentation. Returns (segments, stats).

    segments: [(label, start_sec, end_sec), ...] where label ∈ {speech,music,noise,noEnergy}
    """
    seg = _get_segmenter()
    raw = seg(str(audio_path))  # list of (label, start, end)
    by_label: dict[str, float] = {}
    total = 0.0
    for label, start, end in raw:
        dur = end - start
        by_label[label] = by_label.get(label, 0.0) + dur
        total += dur
    stats = SegmentStats(
        speech_seconds=by_label.get("speech", 0.0),
        music_seconds=by_label.get("music", 0.0),
        silence_seconds=by_label.get("noEnergy", 0.0),
        noise_seconds=by_label.get("noise", 0.0),
        total_seconds=total,
    )
    return raw, stats


def write_speech_only(audio_path: Path, segments: list[tuple[str, float, float]], out_path: Path) -> None:
    """Concat only speech segments into out_path."""
    audio = AudioSegment.from_file(audio_path)
    trimmed = AudioSegment.empty()
    for label, start, end in segments:
        if label == "speech":
            trimmed += audio[int(start * 1000) : int(end * 1000)]
    trimmed.export(out_path, format="mp3", bitrate="64k")
```

### New CLI subcommand: `pipeline.py preprocess`

```python
# in pipeline.py
def cmd_preprocess(args):
    from ziwo.segment import analyze, write_speech_only
    from ziwo.db import connect, list_by_status, update_status

    MUSIC_SKIP_THRESHOLD = 0.95  # OPEN — see open questions
    MIN_SPEECH_SECONDS = 5.0

    with connect() as conn:
        rows = list_by_status(conn, "downloaded", limit=args.limit)
        for row in tqdm(rows, desc="preprocess"):
            audio_path = Path(row["audio_path"])
            if not audio_path.exists():
                update_status(conn, row["id"], "failed", error_message="audio missing")
                continue
            try:
                segments, stats = analyze(audio_path)
                trimmed_path = audio_path.with_suffix(".trimmed.mp3")

                if stats.music_fraction >= MUSIC_SKIP_THRESHOLD or stats.speech_seconds < MIN_SPEECH_SECONDS:
                    update_status(
                        conn, row["id"], "skipped_music",
                        speech_seconds=stats.speech_seconds,
                        music_seconds=stats.music_seconds,
                        silence_seconds=stats.silence_seconds,
                        music_fraction=stats.music_fraction,
                    )
                    continue

                write_speech_only(audio_path, segments, trimmed_path)
                update_status(
                    conn, row["id"], "preprocessed",
                    audio_path_trimmed=str(trimmed_path),
                    speech_seconds=stats.speech_seconds,
                    music_seconds=stats.music_seconds,
                    silence_seconds=stats.silence_seconds,
                    music_fraction=stats.music_fraction,
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
"music_seconds": "REAL",
"silence_seconds": "REAL",
"music_fraction": "REAL",
```

### `transcribe.py` change

```python
# prefer trimmed audio if present
audio_path = row["audio_path_trimmed"] or row["audio_path"]
```

And update `list_by_status` caller to pull `"preprocessed"` rows instead of `"downloaded"`.

## Expected impact

- Call `695764`: if ~40% of the call is hold music, input audio drops from 1604s → ~960s. Proportional cost drop, and — more importantly — hallucinated repetition should disappear because Gemini never sees the music stretches that trigger it.
- Short fully-abandoned calls (customer dropped while on hold) get skipped entirely, saving 100% of their transcription cost.

## Open questions (decide before implementing)

1. **Primary goal weighting** — cost/latency, hallucination prevention, or both equally? Affects how aggressively we trim (lossy trim risks cutting real speech edges).
2. **Dependency choice** — `inaSpeechSegmenter` (richer labels, larger dep) vs. `silero-vad` (tiny, speech-only, can't distinguish music from noise). Current plan assumes `inaSpeechSegmenter`.
3. **Pipeline shape** — separate `preprocess` step (current plan) vs. fold into `transcribe`. Separate step lets you reprocess without re-downloading and keeps failure modes distinct.
4. **Keep originals?** — current plan keeps `.mp3` and writes `.trimmed.mp3` alongside. Disk cost is trivial; keeping both enables A/B comparison and reprocessing.
5. **Skip thresholds** — `music_fraction > 0.95` and `speech_seconds < 5` are placeholders. Need to sample real calls to tune. What status label? (`skipped_music` proposed.)
6. **DB columns** — current plan adds 5 columns. Are all needed for the dashboard, or just `music_fraction`?
7. **Segment padding** — when concatenating speech, should we include ~200ms of flanking audio per segment to avoid clipping word onsets/offsets?
8. **Re-run safety** — if a call is already `transcribed`, should `preprocess` be a no-op, or support a `--force` flag to redo with trimmed audio (would require re-transcribing)?
9. **Ground-truth validation** — before rolling out, do we hand-pick ~10 calls with known music and verify the segmenter's labels match? Needed given we have no eval harness yet.

## Rollout checklist (when work resumes)

- [ ] Install `ffmpeg` locally, add `inaSpeechSegmenter` + `pydub` to requirements.
- [ ] Answer open questions above.
- [ ] Implement `ziwo/segment.py` with tests on ~5 hand-picked calls (include `695764`).
- [ ] Schema migration in `ziwo/db.py` (both SCHEMA and EXTENSION_COLUMNS).
- [ ] Add `preprocess` CLI subcommand.
- [ ] Update `transcribe.py` to consume `audio_path_trimmed` and `preprocessed` status.
- [ ] Update `run` meta-command to include the new step.
- [ ] Backfill: reprocess a sample of already-`transcribed` calls where hallucination is suspected, compare cost/quality.
- [ ] Update `CLAUDE.md` pipeline diagram + subcommand list.
- [ ] Update `docs/transcript-analysis-methodology.md` if the taxonomy is affected (it shouldn't be — this is upstream of extraction).
