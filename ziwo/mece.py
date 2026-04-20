"""Deterministic MECE category/subcategory mapping, object bucketing, friction scoring.

Mapping is theme-primary in four tiers (see methodology.md § Mapping Logic):
  Tier 1 — theme alone determines (category, subcategory)
  Tier 2 — theme + object_bucket disambiguates
  Tier 3 — theme + intent_action disambiguates (device-issue themes)
  Tier 4 — Other → action-based fallback
Unknown themes raise to surface enum drift early.
"""

from __future__ import annotations

from .db import connect
from .queues import queue_matches_category

# ----------------------------------------------------------------------------
# Object bucketing: free-text intent_object → small closed enum.
# Rules evaluated in order; first match wins. Specific buckets must precede
# generic ones (refund before payment, warranty before claim, etc.).
# ----------------------------------------------------------------------------

OBJECT_BUCKET_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("refund", ("refund",)),
    ("warranty", ("warranty", "claim")),
    ("trade-in", ("trade", "reseller", "sell")),
    ("delivery", ("delivery", "shipping", "shipment", "courier", "waybill")),
    ("order", ("order", "purchase order")),
    ("promotion", ("voucher", "promotion", "promo", "cashback", "gift card", "discount")),
    ("payment", ("payment", "installment", "invoice", "price", "wallet", "balance", "billing")),
    ("account", ("account", "login", "email", "password", "registration", "profile")),
    ("agent", ("agent", "manager", "supervisor", "team lead")),
    ("product", (
        "iphone", "ipad", "macbook", "laptop", "phone", "device", "charger",
        "cable", "adapter", "screen", "battery", "sim", "computer", "mobile",
        "s23", "s22", "s21", "samsung", "tablet", "headphone", "earphone",
        "watch", "airpod",
    )),
]


def bucket_object(intent_object: str | None) -> str:
    if not intent_object:
        return "unknown"
    needle = intent_object.lower()
    for bucket, keywords in OBJECT_BUCKET_RULES:
        for kw in keywords:
            if kw in needle:
                return bucket
    return "unknown"


# ----------------------------------------------------------------------------
# Tier 1 — theme alone determines (category, subcategory).
# ----------------------------------------------------------------------------

THEME_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    # Delivery & logistics
    "Delivery Delays": ("Order Management", "Delivery Issues"),
    "Address Issues": ("Order Management", "Delivery Issues"),
    "Shipping Provider Issue": ("Order Management", "Delivery Issues"),
    "Quality Check Wait": ("Order Management", "Delivery Issues"),
    "Order Modification": ("Order Management", "Order Modification"),
    # Promotions / pricing
    "Promotions & Vouchers": ("Pre-Purchase", "Promotions & Vouchers"),
    "Missing Cashback": ("Pre-Purchase", "Promotions & Vouchers"),
    "Installment Payments": ("Pre-Purchase", "Pricing & Installments"),
    "Cash on Delivery": ("Pre-Purchase", "Pricing & Installments"),
    "Pricing Inquiry": ("Pre-Purchase", "Pricing & Installments"),
    # Pre-purchase product info
    "Store Location": ("Pre-Purchase", "Product Information"),
    "Product Research": ("Pre-Purchase", "Product Information"),
    # Payment platform
    "Payment Failures": ("Account & Platform", "Payment Issues"),
    # Returns & warranty
    "Return Process": ("After-Sales Support", "Returns & Refunds"),
    "Refund Processing": ("After-Sales Support", "Returns & Refunds"),
    "Claim Status": ("After-Sales Support", "Warranty Claims"),
    "Coverage Inquiry": ("After-Sales Support", "Warranty Claims"),
    # Account / platform
    "Account Registration": ("Account & Platform", "Account Management"),
    "Login Issues": ("Account & Platform", "Account Management"),
    "Video Upload Issue": ("Account & Platform", "Website/App Issues"),
    "Chatbot Handoff": ("Account & Platform", "Website/App Issues"),
    "Website Couldn't Find Answer": ("Account & Platform", "Website/App Issues"),
    "App Problem": ("Account & Platform", "Website/App Issues"),
    # Trade-in
    "Trade-In": ("Trade-In Program", "Device Trade-In"),
    # Product complaint (after-sales topic)
    "Product Complaint": ("After-Sales Support", "Product Defects"),
}

# ----------------------------------------------------------------------------
# Tier 2 — theme + object_bucket. Each entry is a dict mapping bucket → target,
# with a special "_default" key for unmatched buckets.
# ----------------------------------------------------------------------------

TIER2_THEMES: dict[str, dict[str, tuple[str, str]]] = {
    "Status Inquiry": {
        "order": ("Order Management", "Order Tracking"),
        "delivery": ("Order Management", "Order Tracking"),
        "warranty": ("After-Sales Support", "Warranty Claims"),
        "refund": ("After-Sales Support", "Returns & Refunds"),
        "payment": ("Account & Platform", "Payment Issues"),
        "_default": ("Order Management", "Order Tracking"),
    },
    "Cancellation": {
        "order": ("Order Management", "Order Cancellation"),
        "delivery": ("Order Management", "Order Cancellation"),
        "account": ("Account & Platform", "Account Management"),
        "trade-in": ("Trade-In Program", "Device Trade-In"),
        "_default": ("Order Management", "Order Cancellation"),
    },
    "Initial or Unclear Contact": {
        "order": ("Order Management", "Order Tracking"),
        "delivery": ("Order Management", "Order Tracking"),
        "warranty": ("After-Sales Support", "Warranty Claims"),
        "account": ("Account & Platform", "Account Management"),
        "product": ("Pre-Purchase", "Product Information"),
        "promotion": ("Pre-Purchase", "Promotions & Vouchers"),
        "payment": ("Pre-Purchase", "Pricing & Installments"),
        "_default": ("Pre-Purchase", "Product Information"),
    },
    "Supervisor Request": {
        "order": ("Order Management", "Order Tracking"),
        "delivery": ("Order Management", "Delivery Issues"),
        "warranty": ("After-Sales Support", "Warranty Claims"),
        "refund": ("After-Sales Support", "Returns & Refunds"),
        "product": ("After-Sales Support", "Product Defects"),
        "payment": ("Account & Platform", "Payment Issues"),
        "account": ("Account & Platform", "Account Management"),
        "_default": ("Account & Platform", "Account Management"),
    },
}

# ----------------------------------------------------------------------------
# Tier 3 — theme + intent_action. Device-issue and missing-item themes where
# the customer's concrete ask determines the bucket.
# ----------------------------------------------------------------------------

DEVICE_ISSUE_THEMES = {
    "Battery & Charging",
    "Screen Issues",
    "SIM & Connectivity",
    "Hardware Defects",
}

DEVICE_ISSUE_ACTION_MAP: dict[str, tuple[str, str]] = {
    "Return": ("After-Sales Support", "Returns & Refunds"),
    "Refund": ("After-Sales Support", "Returns & Refunds"),
    "Warranty Claim": ("After-Sales Support", "Warranty Claims"),
    "Technical Support": ("After-Sales Support", "Technical Support"),
    "Complaint": ("After-Sales Support", "Product Defects"),
}
DEVICE_ISSUE_DEFAULT = ("After-Sales Support", "Technical Support")

WRONG_ITEM_RETURN_ACTIONS = {"Return", "Refund"}
WRONG_ITEM_RETURN_TARGET = ("After-Sales Support", "Returns & Refunds")
WRONG_ITEM_DEFAULT = ("Order Management", "Delivery Issues")

# ----------------------------------------------------------------------------
# Tier 4 — Other theme: fall back to action default.
# ----------------------------------------------------------------------------

ACTION_FALLBACK_DEFAULTS: dict[str, tuple[str, str]] = {
    "Return": ("After-Sales Support", "Returns & Refunds"),
    "Refund": ("After-Sales Support", "Returns & Refunds"),
    "Track Order": ("Order Management", "Order Tracking"),
    "Inquiry": ("Pre-Purchase", "Product Information"),
    "Complaint": ("After-Sales Support", "Product Defects"),
    "Purchase": ("Pre-Purchase", "Purchase Intent"),
    "Technical Support": ("After-Sales Support", "Technical Support"),
    "Cancellation": ("Order Management", "Order Cancellation"),
    "Sell Device": ("Trade-In Program", "Device Trade-In"),
    "Warranty Claim": ("After-Sales Support", "Warranty Claims"),
    "Payment Issue": ("Account & Platform", "Payment Issues"),
    "Account Issue": ("Account & Platform", "Account Management"),
    "Website/App Issue": ("Account & Platform", "Website/App Issues"),
}


class UnknownThemeError(ValueError):
    pass


def map_category(
    qualifier_theme: str | None,
    intent_action: str | None,
    object_bucket: str | None,
) -> tuple[str | None, str | None]:
    """Return (category, subcategory). Raises on unknown theme to surface drift."""
    if not qualifier_theme:
        return None, None

    # Tier 1
    if qualifier_theme in THEME_CATEGORY_MAP:
        return THEME_CATEGORY_MAP[qualifier_theme]

    # Tier 2
    if qualifier_theme in TIER2_THEMES:
        bucket_map = TIER2_THEMES[qualifier_theme]
        bucket = object_bucket or "unknown"
        return bucket_map.get(bucket, bucket_map["_default"])

    # Tier 3
    if qualifier_theme in DEVICE_ISSUE_THEMES:
        return DEVICE_ISSUE_ACTION_MAP.get(intent_action or "", DEVICE_ISSUE_DEFAULT)
    if qualifier_theme == "Wrong or Missing Item":
        if intent_action in WRONG_ITEM_RETURN_ACTIONS:
            return WRONG_ITEM_RETURN_TARGET
        return WRONG_ITEM_DEFAULT

    # Tier 4
    if qualifier_theme == "Other":
        if intent_action and intent_action in ACTION_FALLBACK_DEFAULTS:
            return ACTION_FALLBACK_DEFAULTS[intent_action]
        return None, None

    raise UnknownThemeError(
        f"qualifier_theme={qualifier_theme!r} not in any tier — add it to "
        "THEME_CATEGORY_MAP, TIER2_THEMES, DEVICE_ISSUE_THEMES, or handle "
        "explicitly in mece.map_category()."
    )


def friction_score(sentiment: str | None, resolution: str | None) -> int:
    score = 0
    if sentiment in {"Frustrated", "Angry"}:
        score += 1
    if resolution == "No":
        score += 1
    return score


def classify_mece() -> int:
    """Populate object_bucket, category, subcategory, friction_score, queue_matches_category."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, intent_action, intent_object, qualifier_theme, "
            "sentiment, resolution, queue_intent FROM calls "
            "WHERE status = 'extracted'"
        ).fetchall()
        for row in rows:
            ob = bucket_object(row["intent_object"])
            category, subcategory = map_category(
                row["qualifier_theme"], row["intent_action"], ob
            )
            fs = friction_score(row["sentiment"], row["resolution"])
            qmc = queue_matches_category(row["queue_intent"], category)
            conn.execute(
                "UPDATE calls SET object_bucket = ?, category = ?, "
                "subcategory = ?, friction_score = ?, "
                "queue_matches_category = ? WHERE id = ?",
                (ob, category, subcategory, fs, qmc, row["id"]),
            )
    return len(rows)
