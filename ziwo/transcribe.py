import re
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from tqdm import tqdm

from .config import GEMINI_API_KEY, GEMINI_MODEL
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


def clean_transcript(text: str) -> str:
    """Remove filler tags from a transcript.

    - Consecutive music/hold tags → single [Hold music].
    - Standalone noise tags → removed.
    - Collapses resulting blank lines.
    """
    # Collapse repeated music/hold/noise runs into a single marker
    text = _FILLER_RE.sub("[Hold music]\n", text)
    # Strip standalone noise tags
    text = _NOISE_RE.sub("", text)
    # Collapse multiple blank lines into one
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


PROMPT = """You are a faithful transcriber and translator of a customer-support phone call.

The audio is typically in Egyptian Arabic, sometimes with English code-switching. Translate everything spoken into natural, fluent English. Preserve meaning, tone, and any proper nouns (names, brands, product names, cities). Do NOT summarize. Do NOT add content that is not in the audio. If a turn is unintelligible, write "[unintelligible]".

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


def _split_transcript(text: str) -> tuple[str, str | None]:
    lines = text.strip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().lower().startswith("language:"):
            language = lines[i].split(":", 1)[1].strip() or None
            for j in range(i - 1, -1, -1):
                if lines[j].strip() == "---":
                    return "\n".join(lines[:j]).strip(), language
            return "\n".join(lines[:i]).strip(), language
    return text.strip(), None


def transcribe_downloaded(limit: int | None = None) -> tuple[int, int]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=GEMINI_API_KEY)
    done = failed = 0

    with connect() as conn:
        rows = list_by_status(conn, "downloaded", limit=limit)
        if not rows:
            return 0, 0

        for row in tqdm(rows, desc="transcribing", unit="call"):
            call_id = row["id"]
            audio_path = row["audio_path"]
            if not audio_path or not Path(audio_path).exists():
                update_status(
                    conn, call_id, "failed", error_message="audio file missing"
                )
                failed += 1
                conn.commit()
                continue

            try:
                audio_bytes = Path(audio_path).read_bytes()
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[
                        types.Part.from_bytes(
                            data=audio_bytes, mime_type="audio/mp3"
                        ),
                        PROMPT,
                    ],
                )
                text = (response.text or "").strip()
                if not text:
                    raise RuntimeError("empty Gemini response")

                transcript, language = _split_transcript(text)
                transcript = clean_transcript(transcript)
                update_status(
                    conn,
                    call_id,
                    "transcribed",
                    transcript=transcript,
                    transcript_language=language,
                    transcribed_at=datetime.utcnow().isoformat(timespec="seconds"),
                    error_message=None,
                )
                done += 1
            except Exception as e:
                update_status(
                    conn, call_id, "failed", error_message=f"transcribe: {e}"
                )
                failed += 1
            conn.commit()

    return done, failed
