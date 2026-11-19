"""Deterministic Hinglish + English voice/text intent parser for seller logs."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from typing import Any, Literal

from sqlalchemy.orm import Session

from vani_ledger.schemas import VoiceIntentPayload, sanitize_text
from vani_ledger.storage.models import Sku
from vani_ledger.storage.repository import list_skus, write_audit_log

MAX_QTY_BOUND = 100_000
MAX_SHELF_LIFE_DAYS = 730

FESTIVAL_KEYWORDS = (
    "diwali",
    "deepawali",
    "holi",
    "navratri",
    "eid",
    "pongal",
    "sankranti",
    "rakhi",
    "raksha bandhan",
    "christmas",
    "festival",
    "festive",
    "tyohar",
    "tyohaar",
)
SPOILAGE_KEYWORDS = (
    "expire",
    "expired",
    "expiry",
    "spoil",
    "spoiled",
    "spoilage",
    "kharab",
    "kharaab",
    "waste",
    "wasted",
    "barbad",
    "phat gaya",
    "damage",
    "damaged",
)
OVERRIDE_KEYWORDS = (
    "override",
    "change order",
    "reduce order",
    "kam karo",
    "kam kar",
    "set order",
    "order to",
    "kar do",
    "kardo",
)
STOCK_LOG_KEYWORDS = (
    "bika",
    "biki",
    "becha",
    "sold",
    "sale",
    "sales",
    "stock",
    "add karo",
    "add",
    "order",
    "aaya",
    "mangwa",
    "crate",
    "bottle",
    "packet",
    "pack",
    "pouch",
    "bag",
    "box",
)

# Curated Hinglish/English retail synonyms -> canonical tokens that match SKU catalog fields
HINGLISH_SKU_HINTS: dict[str, tuple[str, ...]] = {
    "doodh": ("taza doodh", "full-cream milk", "milk"),
    "dudh": ("taza doodh", "full-cream milk", "milk"),
    "milk": ("full-cream milk", "taza doodh"),
    "sarso": ("sarso tel", "mustard oil"),
    "sarson": ("sarso tel", "mustard oil"),
    "mustard": ("mustard oil", "sarso tel"),
    "tel": ("sarso tel", "mustard oil"),
    "ghee": ("desi ghee", "cow ghee"),
    "atta": ("gehu atta", "chakki fresh atta"),
    "chawal": ("basmati chawal", "basmati aged rice"),
    "rice": ("basmati aged rice",),
    "cheeni": ("cheeni", "white sugar"),
    "sugar": ("white sugar", "cheeni"),
    "dal": ("arhar dal", "toor"),
    "besan": ("besan",),
    "chai": ("chai patti", "tea"),
    "namak": ("namak", "salt"),
    "paneer": ("paneer",),
    "dahi": ("dahi", "curd"),
    "curd": ("curd", "dahi"),
    "makhan": ("makhan", "butter"),
    "butter": ("butter", "makhan"),
    "mawa": ("mawa", "khoya"),
    "badam": ("badam tel", "almond hair oil"),
    "almond": ("almond hair oil", "badam tel"),
    "serum": ("kumkumadi serum", "kesar serum"),
    "diya": ("brass diya", "pital diya"),
    "candle": ("mogra", "candle"),
    "soan papdi": ("soan papdi",),
    "bhujia": ("bhujia", "namkeen"),
    "kaju": ("kaju", "cashews"),
}

_PROMPT_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"drop\s+table", re.IGNORECASE),
)

_SHELF_LIFE_RE = re.compile(
    r"(?:shelf[\s_-]*life|expiry|valid(?:ity)?)\s*(?:is|of|:|=)?\s*(\d{1,4})\s*(?:d|day|days|din)\b"
    r"|\b(\d{1,4})\s*(?:d|day|days|din)\s*(?:shelf[\s_-]*life|expiry)\b",
    re.IGNORECASE,
)
_SPOILAGE_QTY_RE = re.compile(
    r"\b(\d{1,6})\s*(?:packet|packets|pack|packs|crate|crates|unit|units|bottle|bottles|pcs|piece|pieces|kg|ltr|l)?\s*"
    r"(?:expire|expired|spoil|spoiled|kharab|kharaab|waste|wasted|barbad|damaged?)\b"
    r"|\b(?:expire|expired|spoil|spoiled|kharab|kharaab|waste|wasted|damaged?)\s*[:=-]?\s*(\d{1,6})\b",
    re.IGNORECASE,
)
_STOCK_QTY_RE = re.compile(
    r"\b(\d{1,6})\s*(?:crate|crates|bottle|bottles|packet|packets|pack|packs|unit|units|bag|bags|box|boxes|jar|jars|tin|tins|cup|cups|pouch|pouches|pcs)\b"
    r"|\b(?:add|order|stock|qty|quantity|set|override|to|bika|sold)\s*(?:to|of|:|=)?\s*(\d{1,6})\b"
    r"|\b(\d{1,6})\s*(?:bika|biki|becha|sold|add|order)\b",
    re.IGNORECASE,
)
_SKU_CODE_RE = re.compile(r"\b([A-Z]{2,8}-[A-Z0-9]{2,8}-\d{2,4})\b", re.IGNORECASE)


def _clamp_int(raw: str | None, *, low: int = 0, high: int = MAX_QTY_BOUND) -> int | None:
    if raw is None:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    if val < low or val > high:
        return None
    return val


def _strip_injection_clauses(text: str) -> tuple[str, bool]:
    """Detect and neutralize prompt-injection directives before quantity parsing."""
    flagged = False
    cleaned = text
    for pattern in _PROMPT_INJECTION_PATTERNS:
        if pattern.search(cleaned):
            flagged = True
            cleaned = pattern.sub("", cleaned)
    # Remove HTML/script tags so attacker markup cannot influence extraction
    if "<" in cleaned or ">" in cleaned:
        cleaned = re.sub(r"<[^>]{0,200}>", " ", cleaned)
    return " ".join(cleaned.split()), flagged


def _match_sku(
    lower_text: str,
    skus: Sequence[Sku],
    *,
    shelf_life_days: int | None,
    festival_matched: str | None,
) -> Sku | None:
    """Deterministically match the best tenant SKU by sku_code, hinglish_alias, or name."""
    if not skus:
        return None

    # 1. Exact SKU code match (highest confidence)
    code_match = _SKU_CODE_RE.search(lower_text)
    if code_match:
        target_code = code_match.group(1).upper()
        for sku in skus:
            if sku.sku_code.upper() == target_code:
                return sku

    tokens = set(re.findall(r"[a-z0-9]+", lower_text))
    best_sku: Sku | None = None
    best_score = 0

    for sku in skus:
        score = 0
        name_l = sku.name.lower()
        alias_l = (sku.hinglish_alias or "").lower()
        code_l = sku.sku_code.lower()

        if code_l and code_l in lower_text:
            score += 100
        if alias_l and alias_l in lower_text:
            score += 80
        if name_l and name_l in lower_text:
            score += 75

        # Keyword overlap with alias & name
        alias_words = {w for w in re.findall(r"[a-z0-9]+", alias_l) if len(w) >= 3}
        name_words = {w for w in re.findall(r"[a-z0-9]+", name_l) if len(w) >= 3}
        generic_units = {
            "1kg",
            "5kg",
            "1l",
            "500ml",
            "250g",
            "200g",
            "200ml",
            "100g",
            "500g",
            "pack",
            "fresh",
            "pure",
        }
        alias_hits = (alias_words - generic_units) & tokens
        name_hits = (name_words - generic_units) & tokens
        score += len(alias_hits) * 28 + len(name_hits) * 20

        # Curated Hinglish synonym hints
        for hint_word, targets in HINGLISH_SKU_HINTS.items():
            if hint_word in lower_text:
                if any(t in alias_l or t in name_l for t in targets):
                    score += 35
                    # Prefer hero primary SKU (001) on ties like "doodh" -> SKU-DAIRY-001
                    if sku.sku_code.endswith("-001"):
                        score += 10

        # Secondary contextual boost for shelf-life / festival hero alignment
        if score > 0 and shelf_life_days is not None and sku.shelf_life_days == shelf_life_days:
            score += 8
        if score == 0 and festival_matched and shelf_life_days == sku.shelf_life_days:
            if "festive" in name_l or sku.sku_code == "SKU-OIL-001":
                score += 18

        if score > best_score:
            best_score = score
            best_sku = sku

    return best_sku if best_score >= 18 else None


def parse_voice_transcript(transcript: str, skus: Sequence[Sku]) -> VoiceIntentPayload:
    """Parse a Hinglish or English seller voice transcript into a strict VoiceIntentPayload."""
    cleaned_raw = sanitize_text(transcript) or ""
    if not cleaned_raw:
        raise ValueError("Transcript cannot be empty")

    safe_text, injection_detected = _strip_injection_clauses(cleaned_raw)
    lower_text = safe_text.lower()

    # 1. Extract shelf_life_days
    shelf_life_days: int | None = None
    shelf_match = _SHELF_LIFE_RE.search(safe_text)
    if shelf_match:
        raw_days = shelf_match.group(1) or shelf_match.group(2)
        shelf_life_days = _clamp_int(raw_days, low=1, high=MAX_SHELF_LIFE_DAYS)

    # 2. Extract spoilage_qty
    spoilage_qty: int | None = None
    spoilage_match = _SPOILAGE_QTY_RE.search(safe_text)
    if spoilage_match:
        raw_spoil = spoilage_match.group(1) or spoilage_match.group(2)
        spoilage_qty = _clamp_int(raw_spoil, low=0, high=MAX_QTY_BOUND)

    # 3. Extract primary stock/sales/override quantity (excluding shelf_life and spoilage spans)
    masked_text = safe_text
    if shelf_match:
        masked_text = masked_text[: shelf_match.start()] + " " + masked_text[shelf_match.end() :]
    if spoilage_match:
        # Remove matched spoilage phrase so spoilage count isn't double-counted as sold qty
        masked_text = masked_text.replace(spoilage_match.group(0), " ", 1)

    extracted_qty: int | None = None
    if not injection_detected:
        qty_match = _STOCK_QTY_RE.search(masked_text)
        if qty_match:
            raw_qty = qty_match.group(1) or qty_match.group(2) or qty_match.group(3)
            extracted_qty = _clamp_int(raw_qty, low=0, high=MAX_QTY_BOUND)
        else:
            # Fallback: standalone integer if override/stock phrase lacks unit word
            standalone = re.search(r"\b(\d{1,5})\b", masked_text)
            if standalone and any(
                k in lower_text for k in (*OVERRIDE_KEYWORDS, *STOCK_LOG_KEYWORDS)
            ):
                extracted_qty = _clamp_int(standalone.group(1), low=0, high=MAX_QTY_BOUND)

    # 4. Classify intent deterministically
    festival_matched = next((kw for kw in FESTIVAL_KEYWORDS if kw in lower_text), None)
    has_spoilage = spoilage_qty is not None or any(kw in lower_text for kw in SPOILAGE_KEYWORDS)
    has_override = any(kw in lower_text for kw in OVERRIDE_KEYWORDS)
    has_stock_log = extracted_qty is not None or any(kw in lower_text for kw in STOCK_LOG_KEYWORDS)

    intent: Literal["stock_log", "spoilage_report", "festival_note", "override_query", "unknown"]
    if has_spoilage:
        intent = "spoilage_report"
    elif festival_matched is not None:
        intent = "festival_note"
    elif has_override:
        intent = "override_query"
    elif has_stock_log:
        intent = "stock_log"
    else:
        intent = "unknown"

    # 5. Match tenant SKU
    matched_sku = _match_sku(
        lower_text,
        skus,
        shelf_life_days=shelf_life_days,
        festival_matched=festival_matched,
    )

    # 6. Escape notes for XSS-safe storage/rendering
    safe_notes = html.escape(cleaned_raw, quote=True)
    raw_fields: dict[str, Any] = {
        "festival_detected": festival_matched,
        "secondary_intent": "stock_log" if (has_spoilage and extracted_qty is not None) else None,
        "prompt_injection_flagged": injection_detected,
    }
    if matched_sku is not None:
        raw_fields["matched_sku_id"] = matched_sku.id
        raw_fields["matched_hinglish_alias"] = matched_sku.hinglish_alias

    return VoiceIntentPayload(
        intent=intent,
        matched_sku_code=matched_sku.sku_code if matched_sku else None,
        matched_sku_name=matched_sku.name if matched_sku else None,
        extracted_qty=extracted_qty,
        spoilage_qty=spoilage_qty,
        shelf_life_days=shelf_life_days,
        notes=safe_notes,
        requires_confirmation=True,
        raw_fields=raw_fields,
    )


def process_voice_note(
    db: Session,
    merchant_id: str,
    transcript: str,
    request_id: str = "req_voice",
) -> VoiceIntentPayload:
    """Tenant-scoped Hinglish/English voice note parsing with audit logging (never auto-commits)."""
    skus, _total = list_skus(db, merchant_id, limit=100, offset=0)
    payload = parse_voice_transcript(transcript, skus)

    write_audit_log(
        db,
        merchant_id=merchant_id,
        action="VOICE_NOTE_PARSED",
        entity_type="voice_note",
        entity_id=payload.matched_sku_code or "unmatched",
        payload={
            "intent": payload.intent,
            "matched_sku_code": payload.matched_sku_code,
            "extracted_qty": payload.extracted_qty,
            "spoilage_qty": payload.spoilage_qty,
            "shelf_life_days": payload.shelf_life_days,
            "requires_confirmation": payload.requires_confirmation,
            "prompt_injection_flagged": payload.raw_fields.get("prompt_injection_flagged", False),
        },
        request_id=request_id,
    )
    db.commit()
    return payload
