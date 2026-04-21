"""Audio segmentation via silero-vad (ONNX).

Phase 1 scope: `analyze()` only. Trimming (`write_speech_only`) lands in Phase 2
once we've tuned thresholds on real samples.

Pure ONNX runtime — no torch, no tensorflow. Model is downloaded on first use
to ~/.cache/silero-vad/silero_vad.onnx (~2MB) and reused.
"""
from __future__ import annotations

import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort

# v4.0 — last release with a simple, documented ONNX I/O surface.
# v5.x produces ~0 probabilities under the same ort invocation (undocumented
# preprocessing requirement somewhere); v4 runs correctly as-is.
MODEL_URL = "https://github.com/snakers4/silero-vad/raw/v4.0/files/silero_vad.onnx"
MODEL_CACHE = Path.home() / ".cache" / "silero-vad" / "silero_vad_v4.onnx"
SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512  # 32ms @ 16kHz
SPEECH_THRESHOLD = 0.5

_session: ort.InferenceSession | None = None


def _ensure_model() -> Path:
    if MODEL_CACHE.exists():
        return MODEL_CACHE
    MODEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading silero-vad model → {MODEL_CACHE}")
    urllib.request.urlretrieve(MODEL_URL, MODEL_CACHE)
    return MODEL_CACHE


def _get_session() -> ort.InferenceSession:
    global _session
    if _session is None:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        _session = ort.InferenceSession(
            str(_ensure_model()),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
    return _session


def _decode_to_pcm(audio_path: Path) -> np.ndarray:
    """Decode any audio file to mono 16kHz float32 PCM via ffmpeg."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(audio_path),
        "-f",
        "f32le",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, check=True)
    return np.frombuffer(result.stdout, dtype=np.float32)


@dataclass
class SegmentStats:
    speech_seconds: float
    non_speech_seconds: float
    total_seconds: float

    @property
    def non_speech_fraction(self) -> float:
        return (
            self.non_speech_seconds / self.total_seconds
            if self.total_seconds
            else 0.0
        )

    @property
    def speech_fraction(self) -> float:
        return (
            self.speech_seconds / self.total_seconds if self.total_seconds else 0.0
        )


def analyze(
    audio_path: Path, threshold: float = SPEECH_THRESHOLD
) -> tuple[list[tuple[str, float, float]], SegmentStats]:
    """Run silero-vad on the audio file.

    Returns (segments, stats). segments: [(label, start_sec, end_sec), ...]
    with label ∈ {"speech", "non_speech"}.
    """
    pcm = _decode_to_pcm(audio_path)
    session = _get_session()
    h = np.zeros((2, 1, 64), dtype=np.float32)
    c = np.zeros((2, 1, 64), dtype=np.float32)
    sr = np.array(SAMPLE_RATE, dtype=np.int64)

    num_windows = len(pcm) // WINDOW_SAMPLES
    labels: list[str] = []
    for i in range(num_windows):
        chunk = pcm[i * WINDOW_SAMPLES : (i + 1) * WINDOW_SAMPLES]
        out, h, c = session.run(
            None,
            {"input": chunk.reshape(1, -1), "sr": sr, "h": h, "c": c},
        )
        prob = float(out[0][0])
        labels.append("speech" if prob >= threshold else "non_speech")

    segments: list[tuple[str, float, float]] = []
    if labels:
        current = labels[0]
        start_idx = 0
        for i in range(1, len(labels)):
            if labels[i] != current:
                segments.append(
                    (
                        current,
                        start_idx * WINDOW_SAMPLES / SAMPLE_RATE,
                        i * WINDOW_SAMPLES / SAMPLE_RATE,
                    )
                )
                current = labels[i]
                start_idx = i
        segments.append(
            (
                current,
                start_idx * WINDOW_SAMPLES / SAMPLE_RATE,
                len(labels) * WINDOW_SAMPLES / SAMPLE_RATE,
            )
        )

    speech_sec = sum(e - s for lab, s, e in segments if lab == "speech")
    non_speech_sec = sum(e - s for lab, s, e in segments if lab == "non_speech")

    return segments, SegmentStats(
        speech_seconds=speech_sec,
        non_speech_seconds=non_speech_sec,
        total_seconds=speech_sec + non_speech_sec,
    )
