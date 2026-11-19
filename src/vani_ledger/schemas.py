"""Frozen Pydantic v2 request/response contracts shared by FastAPI and FastMCP."""

from __future__ import annotations

import unicodedata
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


def sanitize_text(value: str | None) -> str | None:
    """Normalize Unicode (NFKC) and reject embedded null bytes or control chars."""
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    if "\x00" in normalized:
        raise ValueError("Null bytes are not permitted in text input")
    cleaned = "".join(ch for ch in normalized if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return cleaned


class StrictRequestModel(BaseModel):
    """Base for every API/MCP request model — forbids unknown fields (anti mass-assignment)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StrictResponseModel(BaseModel):
    """Base for typed API/MCP response payloads."""

    model_config = ConfigDict(from_attributes=True)


class ApiMeta(StrictResponseModel):
    request_id: str
    model_version: str | None = None
    latency_ms: float | None = None


class ApiError(StrictResponseModel):
    code: str
    message: str


class ApiResponse(StrictResponseModel, Generic[T]):
    data: T | None = None
    meta: ApiMeta
    error: ApiError | None = None


# ── Health ─────────────────────────────────────────────────────────────────────


class HealthPayload(StrictResponseModel):
    ok: bool
    status: Literal["healthy", "degraded"]
    database: Literal["ok", "error"]
    sku_count: int
    sales_count: int
    model_version: str
    offline_ready: bool = True


# ── SKUs ───────────────────────────────────────────────────────────────────────


class SkuSummary(StrictResponseModel):
    id: UUID
    sku_code: str
    name: str
    hinglish_alias: str | None = None
    category: str
    unit: str
    unit_cost: Decimal
    unit_price: Decimal
    salvage_value: Decimal
    lead_time_days: int
    shelf_life_days: int
    moq: int
    pack_size: int
    perishable: bool
    on_hand: Decimal
    on_order: Decimal
    abc_segment: Literal["A", "B", "C"]
    active: bool


class SkuListPayload(StrictResponseModel):
    items: list[SkuSummary]
    total: int
    limit: int
    offset: int


# ── Forecast & Explainability ──────────────────────────────────────────────────


class DriverItem(StrictResponseModel):
    name: str
    units: int
    share_pct: float
    direction: Literal["positive", "negative", "neutral"]


class ForecastPayload(StrictResponseModel):
    forecast_id: UUID
    sku_id: UUID
    sku_code: str
    sku_name: str
    as_of_date: date
    horizon_days: int
    recommended_qty: int
    outlay_amount: Decimal
    p10_total: Decimal
    p50_total: Decimal
    p90_total: Decimal
    service_level: float = Field(ge=0.0, le=1.0)
    data_sufficiency: Literal["high", "medium", "low"]
    model_used: str
    model_version: str
    feature_hash: str
    drivers: list[DriverItem]


class ExplainRequest(StrictRequestModel):
    language: Literal["en", "hinglish"] = "en"


class ExplainPayload(StrictResponseModel):
    sku_id: UUID
    forecast_id: UUID
    recommended_qty: int
    drivers: list[DriverItem]
    narrative: str
    grounded: bool
    source: Literal["deterministic_template", "llm_verified"]


# ── Override Simulation & Commit ───────────────────────────────────────────────


class OverrideSimulateRequest(StrictRequestModel):
    sku_id: UUID
    manual_qty: int = Field(ge=0, le=100_000)
    as_of: date | None = None


class OverrideSimulatePayload(StrictResponseModel):
    sku_id: UUID
    suggested_qty: int
    manual_qty: int
    cash_delta: Decimal
    cash_freed: Decimal
    stockout_risk_before: float = Field(ge=0.0, le=1.0)
    stockout_risk_after: float = Field(ge=0.0, le=1.0)
    stockout_risk_delta: float
    days_cover: float = Field(ge=0.0)
    spoilage_risk: float = Field(ge=0.0, le=1.0)
    within_budget: bool
    outlay_amount: Decimal


class OverrideCommitRequest(StrictRequestModel):
    sku_id: UUID
    manual_qty: int = Field(ge=0, le=100_000)
    reason: str | None = Field(default=None, max_length=280)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("reason", "idempotency_key")
    @classmethod
    def _clean_text(cls, value: str | None) -> str | None:
        return sanitize_text(value)


class OverrideCommitPayload(StrictResponseModel):
    override_id: UUID
    sku_id: UUID
    suggested_qty: int
    manual_qty: int
    cash_delta: Decimal
    stockout_risk_before: float
    stockout_risk_after: float
    days_cover: float
    spoilage_risk: float
    within_budget: bool
    reason: str | None = None
    created_at: datetime


# ── Multi-SKU Budget Reorder Plan (Case 03) ────────────────────────────────────


class ReorderPlanItem(StrictResponseModel):
    sku_id: UUID
    sku_code: str
    sku_name: str
    category: str
    recommended_qty: int
    allocated_qty: int
    unit_cost: Decimal
    allocated_cost: Decimal
    margin_per_rupee: float
    stockout_urgency: float
    capped_by_budget: bool
    capped_by_shelf_life: bool


class ReorderPlanPayload(StrictResponseModel):
    weekly_budget: Decimal
    total_allocated_cost: Decimal
    budget_remaining: Decimal
    items: list[ReorderPlanItem]


# ── Multimodal Capture (Bill / CSV / Voice) ────────────────────────────────────


class BillLineItem(StrictResponseModel):
    item_name: str = Field(max_length=160)
    qty: Decimal = Field(ge=Decimal("0"))
    unit_price: Decimal = Field(ge=Decimal("0"))
    written_line_total: Decimal = Field(ge=Decimal("0"))
    computed_line_total: Decimal = Field(ge=Decimal("0"))
    line_math_ok: bool


class MathCheckResult(StrictResponseModel):
    ok: bool
    written_total: Decimal
    computed_total: Decimal
    delta: Decimal


class BillCapturePayload(StrictResponseModel):
    document_id: UUID
    source_adapter: str
    demo_document: bool
    confidence: float = Field(ge=0.0, le=1.0)
    line_items: list[BillLineItem]
    math_check: MathCheckResult
    needs_review: bool


class VoiceNoteRequest(StrictRequestModel):
    transcript: str = Field(min_length=2, max_length=600)

    @field_validator("transcript")
    @classmethod
    def _clean_transcript(cls, value: str) -> str:
        cleaned = sanitize_text(value)
        if not cleaned or len(cleaned) < 2:
            raise ValueError("transcript must contain non-whitespace text")
        return cleaned


class VoiceIntentPayload(StrictResponseModel):
    intent: Literal["stock_log", "spoilage_report", "festival_note", "override_query", "unknown"]
    matched_sku_code: str | None = None
    matched_sku_name: str | None = None
    extracted_qty: int | None = None
    spoilage_qty: int | None = None
    shelf_life_days: int | None = None
    notes: str
    requires_confirmation: bool = True
    raw_fields: dict[str, Any] = Field(default_factory=dict)
