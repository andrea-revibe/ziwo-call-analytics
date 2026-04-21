"""LLM feature extraction: summary, intent, theme, sentiment, resolution."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Literal, Optional

from google import genai
from google.genai import types
from google.genai import errors as genai_errors
from pydantic import BaseModel
from tqdm import tqdm

from .config import EXTRACT_CONCURRENCY, GEMINI_API_KEY, GEMINI_MODEL
from .db import connect, list_by_status, update_status

ACTIONS = Literal[
    "Return",
    "Refund",
    "Inquiry",
    "Track Order",
    "Complaint",
    "Purchase",
    "Technical Support",
    "Cancellation",
    "Sell Device",
    "Warranty Claim",
    "Payment Issue",
    "Account Issue",
    "Website/App Issue",
]

SENTIMENTS = Literal[
    "Frustrated",
    "Neutral",
    "Inquisitive",
    "Satisfied",
    "Angry",
]

RESOLUTIONS = Literal["Yes", "Partial", "No"]

PARTIAL_REASONS = Literal[
    "callback_promised",
    "vague_guidance",
    "system_or_knowledge_gap",
    "customer_action_required",
    "handoff_to_other_team",
]

THEMES = Literal[
    # Delivery & logistics
    "Delivery Delays",
    "Address Issues",
    "Quality Check Wait",
    "Status Inquiry",
    "Shipping Provider Issue",
    # Cashback & promotions
    "Missing Cashback",
    "Promotions & Vouchers",
    # Payment
    "Installment Payments",
    "Payment Failures",
    "Cash on Delivery",
    "Pricing Inquiry",
    # Device issues
    "Battery & Charging",
    "Screen Issues",
    "SIM & Connectivity",
    "Hardware Defects",
    "Product Complaint",
    # Warranty & returns
    "Claim Status",
    "Coverage Inquiry",
    "Return Process",
    "Refund Processing",
    # Account & platform
    "Account Registration",
    "Login Issues",
    "Video Upload Issue",
    # Purchase
    "Trade-In",
    "Store Location",
    "Product Research",
    # Cross-channel
    "Chatbot Handoff",
    "Website Couldn't Find Answer",
    "App Problem",
    # Misc
    "Cancellation",
    "Order Modification",
    "Wrong or Missing Item",
    "Supervisor Request",
    "Initial or Unclear Contact",
    # Catch-all
    "Other",
]


class Extraction(BaseModel):
    call_summary: str
    intent_action: ACTIONS
    intent_object: str
    intent_qualifier: str
    qualifier_theme: THEMES
    sentiment: SENTIMENTS
    resolution: RESOLUTIONS
    partial_reason: Optional[PARTIAL_REASONS] = None
    escalation_requested: bool


PROMPT = """You are analyzing a transcript of an inbound customer-support phone call to Revibe, a refurbished-electronics retailer operating in the UAE, Saudi Arabia, and South Africa.

The transcript is in English. It was produced by Gemini from the original call audio; the call itself was spoken in Egyptian Arabic, English, or a mix. Speakers are labeled "Agent:", "Customer:", or (when the side could not be determined) "Speaker:". Bracketed markers like [Hold music] and [unintelligible] may appear — treat them as noise, never as speech.

Extract nine fields. Return all values in English.

1) call_summary — 2 sentences maximum. Past-tense narrative describing (a) what the customer called about and (b) the outcome of the call (resolved, escalated, callback promised, hung up, disconnected, etc.). Neutral, factual tone. Do NOT quote the customer directly. Do NOT start with "This call is about…" or "In this call…". Examples:
   · "Customer was confused by QuickUp tracking status reading 'Order Cancelled' on a non-cancelled order. Agent clarified this reflects a shipping provider state and customer accepted the explanation."
   · "Customer reported a cracked screen on an iPhone purchased two weeks ago. Agent opened a warranty claim and promised a callback within 48 hours."

2) intent_action — the customer's CONCRETE REQUEST. Pick EXACTLY ONE from:
   Return, Refund, Inquiry, Track Order, Complaint, Purchase, Technical Support, Cancellation, Sell Device, Warranty Claim, Payment Issue, Account Issue, Website/App Issue

   Important guidance:
   - "Complaint" is a RESIDUAL action. Use it ONLY when the customer is expressing dissatisfaction with NO concrete request underneath. If the customer is upset AND asking for something specific (a refund, a return, a warranty fix, a tech fix, etc.), use that concrete action — not "Complaint". Sentiment captures the emotional register separately.
   - "Inquiry" means the customer is asking for INFORMATION. If the customer has an active problem (e.g., a delayed delivery, a defective device), prefer the action that matches what they want done about it (Track Order, Warranty Claim, Technical Support, Refund, etc.) over the catch-all "Inquiry".
   - Asking for a supervisor/manager is NOT an action — it's captured separately in escalation_requested.

3) intent_object — what the request is about. A short English noun phrase (e.g., "iPhone", "Payment", "Voucher", "Order", "Delivery", "Account"). If genuinely unclear, use "General".

4) intent_qualifier — 2-4 words of additional English context, descriptive and human-readable (e.g., "Cracked Screen", "Delivery Delay", "Tracking Status Confusion", "Missing Cashback", "Wrong Item Received", "Installment Rejected"). This is for human display; it does NOT need to match any enum. Prefer reusable noun-phrase forms over sentence fragments. If no meaningful qualifier exists, use "General".

5) qualifier_theme — pick EXACTLY ONE theme from the list below that best describes the call's root topic. Use "Other" ONLY when no theme applies:
   Delivery Delays, Address Issues, Quality Check Wait, Status Inquiry, Shipping Provider Issue,
   Missing Cashback, Promotions & Vouchers,
   Installment Payments, Payment Failures, Cash on Delivery, Pricing Inquiry,
   Battery & Charging, Screen Issues, SIM & Connectivity, Hardware Defects, Product Complaint,
   Claim Status, Coverage Inquiry, Return Process, Refund Processing,
   Account Registration, Login Issues, Video Upload Issue,
   Trade-In, Store Location, Product Research,
   Chatbot Handoff, Website Couldn't Find Answer, App Problem,
   Cancellation, Order Modification, Wrong or Missing Item, Supervisor Request, Initial or Unclear Contact,
   Other

6) sentiment — the customer's overall emotional state across the call. Pick EXACTLY ONE from:
   Frustrated, Neutral, Inquisitive, Satisfied, Angry

7) resolution — did the agent deliver a definitive outcome on THIS call? This measures information/action finality, NOT customer happiness. Pick EXACTLY ONE:
   - "Yes" — a final answer or action was delivered. This includes:
     · Explicit confirmation — customer confirms the need was met or has no further questions.
     · Firm policy decision — agent gives a final, policy-grounded answer (e.g., "we cannot waive this fee per our Terms"). Customer disagreement does NOT change this.
     · Specific status update — concrete verified data given (e.g., "courier confirms delivery Friday 5 PM", "payment cleared at 10 AM").
     · Intra-session success — inquiry closed on this call, including after internal transfers or immediate supervisor intervention.
     · Resolved but escalated — a final answer WAS given AND the customer still demanded a manager. Mark "Yes" and capture the demand via escalation_requested.
     · Pure information request answered with specifics (store hours, return policy, specific tracking state).
   - "Partial" — agent engaged with the inquiry but it is NOT concluded on this call; work remains. Always set partial_reason when using "Partial".
   - "No" — the call ended with no useful outcome at all: technical disconnect, customer hung up during hold, customer gave up waiting ("I'll call back later"), or agent never meaningfully engaged with the inquiry. Also mark "No" if the transcript consists mostly of [Hold music], [unintelligible], or silence with no substantive exchange.

8) partial_reason — REQUIRED when resolution="Partial", otherwise null. Pick EXACTLY ONE:
   - "callback_promised" — agent or back-office committed to a specific follow-up (return call, email, ticket). Cues: "we'll call you back", "someone from the team will get in touch", "I'll email you the details", "we'll send you a tracking link".
   - "vague_guidance" — agent only gave a range or generic statement with no concrete commitment. Cues: "it should arrive in 3–5 business days", "the team is working on it", "please wait a bit longer", "it usually takes some time".
   - "system_or_knowledge_gap" — agent/supervisor could not answer due to system outage, lack of access, or insufficient knowledge, AND did not commit to a specific callback. Cues: "the system is down", "I can't see that on my end", "I'm not sure, let me check" (without resolution).
   - "customer_action_required" — agent's side is complete but the customer must do something to finalize. Cues: "click the verification link we sent", "visit the warehouse with your ID", "reply to our email with the invoice", "please complete the form".
   - "handoff_to_other_team" — customer was routed to another team/channel and the current leg ended without resolution here. Cues: "I'll transfer you to the warranty team", "please contact our sales department directly".

9) escalation_requested — boolean (true/false). True if the customer EXPLICITLY asks for a supervisor, manager, team lead, or any higher authority during the call. False otherwise. This is orthogonal to topic — a refund call where the customer demands a supervisor sets escalation_requested=true while keeping intent_action="Refund".

Base your judgment ONLY on what is in the transcript below. Do not use outside context, do not infer from queue metadata.

Transcript:
---
{transcript}
---"""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        if code == 429:
            return True
        if isinstance(code, int) and 500 <= code < 600:
            return True
    return False


def _extract_one(client: genai.Client, call_id: int, transcript: str) -> dict:
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=PROMPT.format(transcript=transcript),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Extraction,
                temperature=0.1,
            ),
        )
        parsed: Extraction | None = response.parsed
        if parsed is None:
            raise RuntimeError(
                f"unparseable response: {(response.text or '')[:200]!r}"
            )
        return {"call_id": call_id, "ok": True, "parsed": parsed}
    except Exception as e:
        return {
            "call_id": call_id,
            "ok": False,
            "error": f"extract: {e}",
            "retryable": _is_retryable(e),
        }


def extract_transcribed(limit: int | None = None) -> tuple[int, int]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=GEMINI_API_KEY)
    done = failed = 0

    with connect() as conn:
        rows = list_by_status(conn, "transcribed", limit=limit)
        if not rows:
            return 0, 0

        pending = []
        for row in rows:
            call_id = row["id"]
            transcript = row["transcript"]
            if not transcript or not transcript.strip():
                update_status(
                    conn, call_id, "failed", error_message="extract: empty transcript"
                )
                failed += 1
                conn.commit()
                continue
            pending.append((call_id, transcript))

        if not pending:
            return done, failed

        workers = max(1, EXTRACT_CONCURRENCY)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_extract_one, client, cid, tx)
                for cid, tx in pending
            ]
            for future in tqdm(
                as_completed(futures),
                total=len(futures),
                desc="extracting",
                unit="call",
            ):
                result = future.result()
                call_id = result["call_id"]
                if result["ok"]:
                    parsed: Extraction = result["parsed"]
                    update_status(
                        conn,
                        call_id,
                        "extracted",
                        call_summary=parsed.call_summary,
                        intent_action=parsed.intent_action,
                        intent_object=parsed.intent_object,
                        intent_qualifier=parsed.intent_qualifier,
                        qualifier_theme=parsed.qualifier_theme,
                        sentiment=parsed.sentiment,
                        resolution=parsed.resolution,
                        partial_reason=parsed.partial_reason,
                        escalation_requested=int(parsed.escalation_requested),
                        extracted_at=datetime.utcnow().isoformat(timespec="seconds"),
                        error_message=None,
                    )
                    done += 1
                elif result.get("retryable"):
                    # Leave at 'transcribed' so the next run re-picks it up.
                    update_status(
                        conn,
                        call_id,
                        "transcribed",
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
