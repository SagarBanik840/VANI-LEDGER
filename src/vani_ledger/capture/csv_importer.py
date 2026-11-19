"""Validated, tenant-scoped CSV sales importer with deduplication and Decimal arithmetic."""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from vani_ledger.capture.sanitizer import (
    InvalidUploadError,
    UploadTooLargeError,
    compute_sha256,
)
from vani_ledger.config import get_settings
from vani_ledger.schemas import StrictResponseModel, sanitize_text
from vani_ledger.storage.models import Document, Sale, Sku
from vani_ledger.storage.repository import get_document_by_sha256, write_audit_log

REQUIRED_CSV_HEADERS: frozenset[str] = frozenset({"sku_code", "sold_date", "qty", "unit_price"})
OPTIONAL_CSV_HEADERS: frozenset[str] = frozenset({"promo_flag", "stockout_flag", "channel"})
ALLOWED_CSV_HEADERS: frozenset[str] = REQUIRED_CSV_HEADERS | OPTIONAL_CSV_HEADERS
ALLOWED_CHANNELS: frozenset[str] = frozenset(
    {"online", "offline", "d2c", "marketplace", "voice", "bill"}
)
MAX_CSV_ROWS: int = 5000
MAX_ROW_QTY = Decimal("100000")
MAX_ROW_PRICE = Decimal("10000000")
MONEY_QUANT = Decimal("0.01")
QTY_QUANT = Decimal("0.001")


class CsvImportPayload(StrictResponseModel):
    """Typed response payload returned by CSV sales ingestion."""

    document_id: UUID
    source_adapter: str = "csv_parser"
    rows_processed: int
    rows_inserted: int
    duplicate_upload: bool = False
    total_revenue: Decimal
    sku_codes_updated: list[str]
    warnings: list[str] = Field(default_factory=list)


def _parse_bool(raw: str | None, *, field_name: str, row_num: int) -> bool:
    if raw is None:
        return False
    token = raw.strip().lower()
    if token in {"", "false", "0", "no", "n"}:
        return False
    if token in {"true", "1", "yes", "y"}:
        return True
    raise InvalidUploadError(f"Row {row_num}: invalid boolean value for {field_name}")


def _parse_decimal(
    raw: str | None,
    *,
    field_name: str,
    row_num: int,
    quant: Decimal,
    max_val: Decimal,
) -> Decimal:
    if raw is None or not raw.strip():
        raise InvalidUploadError(f"Row {row_num}: missing required field {field_name}")
    try:
        dec = Decimal(raw.strip().replace(",", ""))
    except InvalidOperation as exc:
        raise InvalidUploadError(f"Row {row_num}: invalid decimal for {field_name}") from exc
    if not dec.is_finite():
        raise InvalidUploadError(f"Row {row_num}: non-finite value for {field_name}")
    if dec < Decimal("0"):
        raise InvalidUploadError(f"Row {row_num}: {field_name} cannot be negative")
    if dec > max_val:
        raise InvalidUploadError(f"Row {row_num}: {field_name} exceeds maximum allowed bound")
    return dec.quantize(quant, rounding=ROUND_HALF_UP)


def import_sales_csv(
    db: Session,
    merchant_id: str,
    raw_bytes: bytes,
    filename: str | None = None,
    request_id: str = "req_csv",
    *,
    max_bytes: int | None = None,
    max_rows: int = MAX_CSV_ROWS,
    reference_date: date | None = None,
) -> CsvImportPayload:
    """Validate and ingest a tenant-scoped sales CSV, persisting Sale + Document + AuditLog."""
    _ = filename  # Never trusted in paths or SQL.
    settings = get_settings()
    byte_limit = max_bytes if max_bytes is not None else settings.max_upload_bytes

    if len(raw_bytes) > byte_limit:
        raise UploadTooLargeError(f"CSV exceeds maximum allowed size of {byte_limit} bytes")
    if not raw_bytes:
        raise InvalidUploadError("Uploaded CSV file is empty")
    if b"\x00" in raw_bytes:
        raise InvalidUploadError("CSV file contains disallowed null bytes")

    try:
        decoded = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidUploadError("CSV file must be valid UTF-8 text") from exc

    sha256_hex = compute_sha256(raw_bytes)
    existing_doc = get_document_by_sha256(db, merchant_id, sha256_hex)
    if existing_doc is not None and existing_doc.kind == "csv":
        stored = json.loads(existing_doc.extracted_json)
        return CsvImportPayload(
            document_id=UUID(existing_doc.id),
            source_adapter=existing_doc.source_adapter,
            rows_processed=int(stored.get("rows_processed", 0)),
            rows_inserted=0,
            duplicate_upload=True,
            total_revenue=Decimal(str(stored.get("total_revenue", "0.00"))),
            sku_codes_updated=list(stored.get("sku_codes_updated", [])),
            warnings=["Duplicate CSV upload detected via SHA-256; skipped duplicate inserts."],
        )

    reader = csv.DictReader(io.StringIO(decoded))
    if not reader.fieldnames:
        raise InvalidUploadError("CSV is missing a header row")

    normalized_headers = [h.strip().lower() for h in reader.fieldnames if h is not None]
    header_set = set(normalized_headers)
    missing_headers = sorted(REQUIRED_CSV_HEADERS - header_set)
    if missing_headers:
        raise InvalidUploadError(f"CSV missing required headers: {', '.join(missing_headers)}")

    unknown_headers = sorted(header_set - ALLOWED_CSV_HEADERS)
    if unknown_headers:
        raise InvalidUploadError(f"CSV contains unsupported headers: {', '.join(unknown_headers)}")

    # Pre-fetch tenant's active SKUs cleanly via ORM (strict tenant isolation)
    tenant_skus = db.scalars(select(Sku).where(Sku.merchant_id == merchant_id)).all()
    sku_by_code = {sku.sku_code: sku for sku in tenant_skus}

    max_allowed_date = reference_date or date.today()
    doc_id = str(uuid4())
    pending_sales: list[Sale] = []
    updated_codes: set[str] = set()
    total_revenue = Decimal("0.00")
    row_count = 0

    for row_num, raw_row in enumerate(reader, start=2):
        # Skip trailing completely empty lines
        if all((v is None or not str(v).strip()) for v in raw_row.values()):
            continue
        row_count += 1
        if row_count > max_rows:
            raise InvalidUploadError(f"CSV exceeds maximum row limit of {max_rows}")

        row = {(k.strip().lower() if k else ""): (v or "") for k, v in raw_row.items()}
        try:
            sku_code = sanitize_text(row.get("sku_code"))
        except ValueError as exc:
            raise InvalidUploadError(f"Row {row_num}: invalid sku_code") from exc

        if not sku_code:
            raise InvalidUploadError(f"Row {row_num}: missing sku_code")

        sku = sku_by_code.get(sku_code)
        if sku is None:
            raise InvalidUploadError(
                f"Row {row_num}: sku_code '{sku_code}' not found in merchant catalog"
            )

        raw_date = row.get("sold_date", "").strip()
        try:
            sold_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise InvalidUploadError(
                f"Row {row_num}: sold_date must be ISO YYYY-MM-DD"
            ) from exc
        if sold_date > max_allowed_date:
            raise InvalidUploadError(
                f"Row {row_num}: sold_date {sold_date.isoformat()} cannot be in the future"
            )

        qty = _parse_decimal(
            row.get("qty"),
            field_name="qty",
            row_num=row_num,
            quant=QTY_QUANT,
            max_val=MAX_ROW_QTY,
        )
        unit_price = _parse_decimal(
            row.get("unit_price"),
            field_name="unit_price",
            row_num=row_num,
            quant=MONEY_QUANT,
            max_val=MAX_ROW_PRICE,
        )
        promo_flag = _parse_bool(row.get("promo_flag"), field_name="promo_flag", row_num=row_num)
        stockout_flag = _parse_bool(
            row.get("stockout_flag"), field_name="stockout_flag", row_num=row_num
        )
        channel = (row.get("channel") or "online").strip().lower()
        if channel not in ALLOWED_CHANNELS:
            raise InvalidUploadError(f"Row {row_num}: unsupported channel '{channel}'")

        pending_sales.append(
            Sale(
                merchant_id=merchant_id,
                sku_id=sku.id,
                sold_date=sold_date,
                qty=qty,
                unit_price=unit_price,
                promo_flag=promo_flag,
                stockout_flag=stockout_flag,
                channel=channel,
                source_doc_id=doc_id,
            )
        )
        updated_codes.add(sku.sku_code)
        total_revenue += (qty * unit_price).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)

    if row_count == 0:
        raise InvalidUploadError("CSV contains zero data rows")

    total_revenue = total_revenue.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    sorted_codes = sorted(updated_codes)

    summary_payload = CsvImportPayload(
        document_id=UUID(doc_id),
        source_adapter="csv_parser",
        rows_processed=row_count,
        rows_inserted=len(pending_sales),
        duplicate_upload=False,
        total_revenue=total_revenue,
        sku_codes_updated=sorted_codes,
        warnings=[],
    )

    document = Document(
        id=doc_id,
        merchant_id=merchant_id,
        kind="csv",
        content_sha256=sha256_hex,
        source_adapter="csv_parser",
        extracted_json=summary_payload.model_dump_json(),
        math_ok=True,
        math_delta=Decimal("0.00"),
        confidence=Decimal("1.0000"),
        status="committed",
    )
    db.add(document)
    db.flush()  # Ensure parent Document row exists for Sale.source_doc_id FK
    db.add_all(pending_sales)

    write_audit_log(
        db,
        merchant_id=merchant_id,
        action="CSV_IMPORTED",
        entity_type="document",
        entity_id=doc_id,
        payload={
            "content_sha256": sha256_hex,
            "rows_inserted": len(pending_sales),
            "sku_count": len(sorted_codes),
            "total_revenue": str(total_revenue),
        },
        request_id=request_id,
    )
    db.commit()
    return summary_payload
