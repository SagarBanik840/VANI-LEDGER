"""Portable SQLAlchemy 2.0 ORM models (NUMERIC money, UTC timestamps, tenant scoping)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from vani_ledger.storage.db import Base


def utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    weekly_budget: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("50000.00")
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Kolkata")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Sku(Base):
    __tablename__ = "skus"
    __table_args__ = (
        UniqueConstraint("merchant_id", "sku_code", name="uq_skus_merchant_code"),
        Index("ix_skus_merchant_category", "merchant_id", "category"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku_code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    hinglish_alias: Mapped[str | None] = mapped_column(String(160), nullable=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False, default="unit")
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    salvage_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False)
    shelf_life_days: Mapped[int] = mapped_column(Integer, nullable=False)
    moq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    pack_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    perishable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    on_hand: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    on_order: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, default=Decimal("0"))
    abc_segment: Mapped[str] = mapped_column(String(1), nullable=False, default="B")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (Index("ix_sales_merchant_sku_date", "merchant_id", "sku_id", "sold_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    sku_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skus.id", ondelete="CASCADE"), nullable=False
    )
    sold_date: Mapped[date] = mapped_column(Date, nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    promo_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stockout_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    source_doc_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    event_date: Mapped[date] = mapped_column(Date, primary_key=True)
    festival_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    days_to_nearest_festival: Mapped[int] = mapped_column(Integer, nullable=False)
    festival_pre_surge_weight: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=Decimal("0.0000")
    )
    is_festival_window: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_payday_window: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_holiday: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Forecast(Base):
    __tablename__ = "forecasts"
    __table_args__ = (Index("ix_forecasts_sku_asof", "merchant_id", "sku_id", "as_of_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    sku_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skus.id", ondelete="CASCADE"), nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    recommended_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    p10_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    p50_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    p90_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    service_level: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    data_sufficiency: Mapped[str] = mapped_column(String(16), nullable=False)
    model_used: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    drivers_json: Mapped[str] = mapped_column(Text, nullable=False)
    residuals_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Override(Base):
    __tablename__ = "overrides"
    __table_args__ = (Index("ix_overrides_sku_created", "merchant_id", "sku_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    sku_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("skus.id", ondelete="CASCADE"), nullable=False
    )
    forecast_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("forecasts.id", ondelete="CASCADE"), nullable=False
    )
    suggested_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    manual_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_delta: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    stockout_risk_before: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    stockout_risk_after: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    days_cover: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    spoilage_risk: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    within_budget: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(280), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("merchant_id", "content_sha256", name="uq_documents_merchant_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    extracted_json: Mapped[str] = mapped_column(Text, nullable=False)
    math_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    math_delta: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00")
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_review")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_merchant_created", "merchant_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
