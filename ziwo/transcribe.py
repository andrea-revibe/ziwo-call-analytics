import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from tqdm import tqdm

from .config import GEMINI_API_KEY, GEMINI_MODEL, TRANSCRIBE_CONCURRENCY
from .db import connect, list_by_status, update_status

# Bracketed tags that are pure filler — collapse consecutive runs into one line.
# Matches [Music], [Hold music], [Noise], [White noise], [Music playing], etc.
_FILLER_RE = re.compile(
    r"(\[(?:music|hold music|background music|music playing|music on hold"
    r"|hold music plays|hold music continues|guitar music"
    r"|noise\d*|white noise|silence)\][\s]*){2,}",
    re.IGNORECASE,
)

# Single-occurrence noise/typing tags that add no analytical value.
_NOISE_RE = re.compile(
    r"^\[(?:noise\d*|white noise|typing sounds?|typing sounds? continue"
    r"|chair squeak|throat clear|silence)\]$",
    re.IGNORECASE | re.MULTILINE,
)

_REPEAT_COLLAPSE_THRESHOLD = 4


def _collapse_repeated_lines(text: str) -> str:
    """Collapse runs of 4+ consecutive identical non-empty lines into one marker."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i + 1
        while j < len(lines) and lines[j] == lines[i]:
            j += 1
        run = j - i
        if run >= _REPEAT_COLLAPSE_THRESHOLD and lines[i].strip():
            out.append(f"{lines[i]} (repeated {run}x)")
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out)


def clean_transcript(text: str) -> str:
    """Remove filler tags from a transcript.

    - Consecutive music/hold tags → single [Hold music].
    - Standalone noise tags → removed.
    - Runs of 4+ identical consecutive lines → one line + "(repeated Nx)".
    - Collapses resulting blank lines.
    """
    # Collapse repeated music/hold/noise runs into a single marker
    text = _FILLER_RE.sub("[Hold music]\n", text)
    # Strip standalone noise tags
    text = _NOISE_RE.sub("", text)
    # Collapse any remaining runs of identical consecutive lines
    text = _collapse_repeated_lines(text)
    # Collapse multiple blank lines into one
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


PROMPT = """You are a faithful transcriber and translator of a customer-support phone call.

The audio is typically in Egyptian Arabic, sometimes with English code-switching. Translate everything spoken into natural, fluent English. Preserve meaning, tone, and any proper nouns (names, brands, product names, cities). Do NOT summarize. Do NOT add content that is not in the audio. If a turn is unintelligible, write "[unintelligible]". Stop transcribing once the call audibly ends — do NOT pad with repeated "[unintelligible]" or background-noise lines.

Label each turn with "Agent:" or "Customer:" at the start of a new line. Use your best judgement based on context (who greets, who asks questions, who describes a problem). If genuinely unsure for a turn, use "Speaker:".

Transcript format (plain text — no markdown, no JSON, no code fences):
Agent: <English translation of what the agent said>
Customer: <English translation of what the customer said>
Agent: ...
Customer: ...

After the transcript, append one separator line and one language line reporting the SOURCE language actually spoken in the audio:
---
Language: <ar | ar-en | en | other>
"""


def _split_transcript(text: str) -> tuple[str, str | None, bool]:
    """Return (transcript, language, has_trailer). has_trailer is False when the
    `---\nLanguage:` footer is missing — a strong loop/truncation signal."""
    lines = text.strip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().lower().startswith("language:"):
            language = lines[i].split(":", 1)[1].strip() or None
            for j in range(i - 1, -1, -1):
                if lines[j].strip() == "---":
                    return "\n".join(lines[:j]).strip(), language, True
            return "\n".join(lines[:i]).strip(), language, True
    return text.strip(), None, False


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        if code == 429:
            return True
        if isinstance(code, int) and 500 <= code < 600:
            return True
    return False


# Hard cap on Gemini output — comfortably above the biggest legit transcript
# observed (~63 KB ≈ 16 K tokens) but well below model default, so MAX_TOKENS
# becomes a meaningful "loop truncated" signal.
MAX_OUTPUT_TOKENS = 20_000

# >20 identical consecutive speaker lines ≈ impossible for a legit call;
# observed loops run thousands of repetitions.
_LOOP_RUN_THRESHOLD = 20

# Oversize + missing trailer is a near-perfect loop signature in the corpus.
_OVERSIZE_NO_TRAILER_CHARS = 30_000

# First attempt + 1 retry = 2 total; after that, permanent failure.
MAX_TRANSCRIBE_ATTEMPTS = 2


def _finish_reason(response) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    fr = getattr(candidates[0], "finish_reason", None)
    if fr is None:
        return None
    return getattr(fr, "name", None) or str(fr)


def _looks_degenerate(
    text: str, finish_reason: str | None, has_trailer: bool
) -> str | None:
    """Return a short reason if the transcript looks looped/truncated, else None."""
    if finish_reason and "MAX_TOKENS" in finish_reason:
        return "max_output_tokens reached"
    lines = text.splitlines()
    run = 1
    for i in range(1, len(lines)):
        if lines[i].strip() and lines[i] == lines[i - 1]:
            run += 1
            if run > _LOOP_RUN_THRESHOLD:
                return f"looped line ({run}x): {lines[i][:60]!r}"
        else:
            run = 1
    if not has_trailer and len(text) > _OVERSIZE_NO_TRAILER_CHARS:
        return "missing language trailer + oversize transcript"
    return None


def _transcribe_one(client: genai.Client, call_id: int, audio_path: str) -> dict:
    try:
        audio_bytes = Path(audio_path).read_bytes()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/mp3"),
                PROMPT,
            ],
            config=types.GenerateContentConfig(
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("empty Gemini response")

        transcript, language, has_trailer = _split_transcript(text)
        reason = _looks_degenerate(transcript, _finish_reason(response), has_trailer)
        if reason:
            return {
                "call_id": call_id,
                "ok": False,
                "error": f"transcribe: degenerate output — {reason}",
                "degenerate": True,
            }

        transcript = clean_transcript(transcript)
        return {
            "call_id": call_id,
            "ok": True,
            "transcript": transcript,
            "language": language,
        }
    except Exception as e:
        return {
            "call_id": call_id,
            "ok": False,
            "error": f"transcribe: {e}",
            "retryable": _is_retryable(e),
        }


def transcribe_downloaded(limit: int | None = None) -> tuple[int, int]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=GEMINI_API_KEY)
    done = failed = 0

    with connect() as conn:
        rows = list_by_status(conn, "downloaded", limit=limit)
        if not rows:
            return 0, 0

        pending = []
        for row in rows:
            call_id = row["id"]
            audio_path = row["audio_path"]
            if not audio_path or not Path(audio_path).exists():
                update_status(
                    conn, call_id, "failed", error_message="audio file missing"
                )
                failed += 1
                conn.commit()
                continue
            attempts = row["transcribe_attempts"] or 0
            pending.append((call_id, audio_path, attempts))

        if not pending:
            return done, failed

        workers = max(1, TRANSCRIBE_CONCURRENCY)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_transcribe_one, client, cid, path): attempts
                for cid, path, attempts in pending
            }
            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="transcribing",
                unit="call",
            ):
                attempts = futures[future]
                result = future.result()
                call_id = result["call_id"]
                if result["ok"]:
                    update_status(
                        conn,
                        call_id,
                        "transcribed",
                        transcript=result["transcript"],
                        transcript_language=result["language"],
                        transcribed_at=datetime.utcnow().isoformat(timespec="seconds"),
                        transcribe_attempts=0,
                        error_message=None,
                    )
                    done += 1
                elif result.get("degenerate"):
                    new_attempts = attempts + 1
                    if new_attempts >= MAX_TRANSCRIBE_ATTEMPTS:
                        update_status(
                            conn,
                            call_id,
                            "failed",
                            transcribe_attempts=new_attempts,
                            error_message=result["error"],
                        )
                    else:
                        update_status(
                            conn,
                            call_id,
                            "downloaded",
                            transcribe_attempts=new_attempts,
                            error_message=result["error"],
                        )
                    failed += 1
                elif result.get("retryable"):
                    # Transient API error — don't count against the attempt cap.
                    update_status(
                        conn,
                        call_id,
                        "downloaded",
                        error_message=result["error"],
                    )
                    failed += 1
                else:
                    update_status(
                        conn, call_id, "failed", error_message=result["error"]
                    )
                    failed += 1
                conn.commit()

    return done, failed
