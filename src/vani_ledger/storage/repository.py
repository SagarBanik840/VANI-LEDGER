"""Tenant-scoped SQLAlchemy 2.0 repository functions.

SECURITY RULE (AGENTS.md Rule 4):
Zero SQL string interpolation. Every query uses SQLAlchemy ORM expressions or
constant parameterized text() binds, and dynamic sort columns are allowlisted.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session

from vani_ledger.storage.models import (
    AuditLog,
    CalendarEvent,
    Document,
    Forecast,
    Merchant,
    Override,
    Sale,
    Sku,
)

# Strict allowlist for dynamic sorting (prevents SQLi via sort parameter)
SKU_SORT_COLUMNS = {
    "sku_code": Sku.sku_code,
    "name": Sku.name,
    "category": Sku.category,
    "unit_price": Sku.unit_price,
    "abc_segment": Sku.abc_segment,
}
SORT_DIRECTIONS = {"asc": asc, "desc": desc}


def authenticate_merchant_by_key_hash(db: Session, candidate_hash: str) -> Merchant | None:
    """Resolve merchant using constant-time digest comparison against stored key hashes."""
    merchants = db.scalars(select(Merchant)).all()
    for merchant in merchants:
        if hmac.compare_digest(merchant.api_key_hash, candidate_hash):
            return merchant
    return None


def get_health_counts(db: Session) -> tuple[int, int]:
    """Return global (sku_count, sales_count) via parameterized aggregate queries."""
    sku_count = db.scalar(select(func.count(Sku.id))) or 0
    sales_count = db.scalar(select(func.count(Sale.id))) or 0
    return int(sku_count), int(sales_count)


def list_skus(
    db: Session,
    merchant_id: str,
    *,
    category: str | None = None,
    search: str | None = None,
    sort_by: str = "sku_code",
    order: str = "asc",
    limit: int = 50,
    offset: int = 0,
) -> tuple[Sequence[Sku], int]:
    """List tenant-scoped SKUs with allowlisted sorting and bounded pagination."""
    base_stmt = select(Sku).where(Sku.merchant_id == merchant_id, Sku.active.is_(True))

    if category:
        base_stmt = base_stmt.where(Sku.category == category)

    if search:
        pattern = f"%{search.strip()}%"
        base_stmt = base_stmt.where(
            or_(
                Sku.name.ilike(pattern),
                Sku.sku_code.ilike(pattern),
                Sku.hinglish_alias.ilike(pattern),
            )
        )

    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = int(db.scalar(count_stmt) or 0)

    sort_col = SKU_SORT_COLUMNS.get(sort_by, Sku.sku_code)
    direction = SORT_DIRECTIONS.get(order, asc)
    items = db.scalars(
        base_stmt.order_by(direction(sort_col)).offset(offset).limit(min(limit, 100))
    ).all()
    return items, total


def get_sku_for_merchant(db: Session, merchant_id: str, sku_id: str) -> Sku | None:
    """Fetch a single SKU strictly scoped to merchant_id (None triggers 404 for IDOR)."""
    stmt = select(Sku).where(Sku.id == sku_id, Sku.merchant_id == merchant_id)
    return db.scalar(stmt)


def get_sku_by_code(db: Session, merchant_id: str, sku_code: str) -> Sku | None:
    """Fetch a single SKU by sku_code strictly scoped to merchant_id."""
    stmt = select(Sku).where(Sku.sku_code == sku_code, Sku.merchant_id == merchant_id)
    return db.scalar(stmt)


def list_sales_for_merchant(
    db: Session, merchant_id: str, *, sku_id: str | None = None
) -> Sequence[Sale]:
    """Load chronological sales rows scoped to merchant_id (and optional sku_id)."""
    stmt = select(Sale).where(Sale.merchant_id == merchant_id)
    if sku_id is not None:
        stmt = stmt.where(Sale.sku_id == sku_id)
    stmt = stmt.order_by(Sale.sku_id.asc(), Sale.sold_date.asc())
    return db.scalars(stmt).all()


def list_calendar_events(db: Session) -> Sequence[CalendarEvent]:
    """Return ordered Indian festival & payday calendar rows."""
    return db.scalars(select(CalendarEvent).order_by(CalendarEvent.event_date.asc())).all()


def get_latest_forecast(db: Session, merchant_id: str, sku_id: str) -> Forecast | None:
    """Return most recent versioned forecast for a tenant's SKU."""
    stmt = (
        select(Forecast)
        .where(Forecast.merchant_id == merchant_id, Forecast.sku_id == sku_id)
        .order_by(Forecast.as_of_date.desc(), Forecast.created_at.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def list_latest_forecasts_for_merchant(db: Session, merchant_id: str) -> dict[str, Forecast]:
    """Return latest Forecast per sku_id for a tenant."""
    stmt = (
        select(Forecast)
        .where(Forecast.merchant_id == merchant_id)
        .order_by(Forecast.as_of_date.desc(), Forecast.created_at.desc())
    )
    result: dict[str, Forecast] = {}
    for row in db.scalars(stmt).all():
        if row.sku_id not in result:
            result[row.sku_id] = row
    return result


def get_override_by_idempotency_key(
    db: Session, merchant_id: str, idempotency_key: str
) -> Override | None:
    """Return an existing committed override if replayed with the same Idempotency-Key."""
    stmt = select(Override).where(
        Override.merchant_id == merchant_id,
        Override.idempotency_key == idempotency_key,
    )
    return db.scalar(stmt)


def get_document_by_sha256(db: Session, merchant_id: str, sha256_hex: str) -> Document | None:
    """Return cached document extraction for duplicate uploads by the same tenant."""
    stmt = select(Document).where(
        Document.merchant_id == merchant_id,
        Document.content_sha256 == sha256_hex,
    )
    return db.scalar(stmt)


def write_audit_log(
    db: Session,
    *,
    merchant_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    request_id: str,
) -> AuditLog:
    """Append an immutable audit record within the caller's transaction."""
    entry = AuditLog(
        merchant_id=merchant_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        payload_json=json.dumps(payload, default=str),
        request_id=request_id,
    )
    db.add(entry)
    return entry
