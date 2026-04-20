"""LLM feature extraction: summary, intent, theme, sentiment, resolution."""

from datetime import datetime
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel
from tqdm import tqdm

from .config import GEMINI_API_KEY, GEMINI_MODEL
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
    resolution: Literal["Yes", "No"]
    escalation_requested: bool


PROMPT = """You are analyzing a transcript of an inbound customer-support phone call to Revibe, a refurbished-electronics retailer operating in the UAE, Saudi Arabia, and South Africa.

The transcript is in the original spoken language (typically Egyptian Arabic, sometimes with English code-switching). Speakers are labeled "Agent:" and "Customer:".

Extract eight fields. ALL RETURNED VALUES MUST BE IN ENGLISH regardless of the transcript language.

1) call_summary — 2 sentences maximum. Describe what the customer called about AND the outcome of the call (resolved, escalated, callback promised, hung up, etc.). Neutral tone, factual. Do NOT quote the customer directly. Example: "Customer confused by QuickUp tracking status reading 'Order Cancelled' on a non-cancelled order. Agent clarified that this reflects a shipping provider state and customer accepted the explanation."

2) intent_action — the customer's CONCRETE REQUEST. Pick EXACTLY ONE from:
   Return, Refund, Inquiry, Track Order, Complaint, Purchase, Technical Support, Cancellation, Sell Device, Warranty Claim, Payment Issue, Account Issue, Website/App Issue

   Important guidance:
   - "Complaint" is a RESIDUAL action. Use it ONLY when the customer is expressing dissatisfaction with NO concrete request underneath. If the customer is upset AND asking for something specific (a refund, a return, a warranty fix, a tech fix, etc.), use that concrete action — not "Complaint". Sentiment captures the emotional register separately.
   - "Inquiry" means the customer is asking for INFORMATION. If the customer has an active problem (e.g., a delayed delivery, a defective device), prefer the action that matches what they want done about it (Track Order, Warranty Claim, Technical Support, Refund, etc.) over the catch-all "Inquiry".
   - Asking for a supervisor/manager is NOT an action — it's captured separately in escalation_requested.

3) intent_object — what the request is about. A short English noun phrase (e.g., "iPhone", "Payment", "Voucher", "Order", "Delivery", "Account"). If genuinely unclear, use "General".

4) intent_qualifier — 2-4 words of additional English context, descriptive and human-readable (e.g., "Cracked Screen", "Delivery Delay", "Tracking Status Confusion", "Missing Cashback"). This is for human display; it does NOT need to match any enum. If no meaningful qualifier exists, use "General".

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

7) resolution — did the agent resolve the caller's need on THIS call?
   - "Yes" if the customer confirmed the solution, expressed satisfaction, or had no further questions after a clear resolution.
   - "No" if the customer requested a supervisor, was promised a callback, was transferred multiple times, hung up before resolution, or the agent could not provide an answer on the call.
   A promised callback counts as "No" — the issue was not resolved on this call.

8) escalation_requested — boolean (true/false). True if the customer EXPLICITLY asks for a supervisor, manager, team lead, or any higher authority during the call. False otherwise. This is orthogonal to topic — a refund call where the customer demands a supervisor sets escalation_requested=true while keeping intent_action="Refund".

Base your judgment ONLY on what is in the transcript below. Do not use outside context, do not infer from queue metadata.

Transcript:
---
{transcript}
---"""


def extract_transcribed(limit: int | None = None) -> tuple[int, int]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")

    client = genai.Client(api_key=GEMINI_API_KEY)
    done = failed = 0

    with connect() as conn:
        rows = list_by_status(conn, "transcribed", limit=limit)
        if not rows:
            return 0, 0

        for row in tqdm(rows, desc="extracting", unit="call"):
            call_id = row["id"]
            transcript = row["transcript"]
            if not transcript or not transcript.strip():
                update_status(
                    conn, call_id, "failed", error_message="extract: empty transcript"
                )
                failed += 1
                conn.commit()
                continue

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
                    escalation_requested=int(parsed.escalation_requested),
                    extracted_at=datetime.utcnow().isoformat(timespec="seconds"),
                    error_message=None,
                )
                done += 1
            except Exception as e:
                update_status(
                    conn, call_id, "failed", error_message=f"extract: {e}"
                )
                failed += 1
            conn.commit()

    return done, failed
