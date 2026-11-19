"""Pure-Python Decimal arithmetic verification for wholesale supplier bills.

Zero LLM/float math: every line item (qty * unit_price) and grand total is recomputed
using Decimal quantized to 0.01 with ROUND_HALF_UP so overcharges are caught deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from vani_ledger.schemas import BillLineItem, MathCheckResult, sanitize_text

MONEY_QUANT = Decimal("0.01")
QTY_QUANT = Decimal("0.001")
MAX_LINE_ITEMS = 200
MAX_QTY = Decimal("100000")
MAX_UNIT_PRICE = Decimal("10000000")


def _to_non_negative_decimal(
    value: Decimal | int | str | float,
    *,
    field_name: str,
    quant: Decimal | None = None,
    max_value: Decimal | None = None,
) -> Decimal:
    """Convert raw numeric value to a finite, non-negative Decimal."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} cannot be a boolean")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal value for {field_name}") from exc

    if not dec.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if dec < Decimal("0"):
        raise ValueError(f"{field_name} must be non-negative")
    if max_value is not None and dec > max_value:
        raise ValueError(f"{field_name} exceeds maximum allowed bound ({max_value})")
    if quant is not None:
        dec = dec.quantize(quant, rounding=ROUND_HALF_UP)
    return dec


def _normalize_qty(value: Decimal | int | str | float) -> Decimal:
    dec = _to_non_negative_decimal(
        value, field_name="qty", quant=QTY_QUANT, max_value=MAX_QTY
    )
    # Keep clean representation for exact integers (e.g. Decimal("24")) while allowing decimals.
    if dec == dec.to_integral_value():
        return dec.quantize(Decimal("1"))
    return dec.normalize()


def audit_bill_line_items(
    raw_items: Sequence[Mapping[str, Any]],
    written_total: Decimal | int | str | float | None = None,
    *,
    tolerance: Decimal = Decimal("0.01"),
) -> tuple[list[BillLineItem], MathCheckResult]:
    """Deterministically audit every bill line item and grand total using Decimal arithmetic.

    Returns ``(audited_line_items, math_check)`` where ``math_check.delta`` equals
    ``written_total - computed_total`` (positive delta = supplier overcharge).
    """
    if not raw_items:
        raise ValueError("Bill must contain at least one line item")
    if len(raw_items) > MAX_LINE_ITEMS:
        raise ValueError(f"Bill exceeds maximum of {MAX_LINE_ITEMS} line items")

    audited: list[BillLineItem] = []
    computed_grand = Decimal("0.00")
    summed_written_lines = Decimal("0.00")

    for idx, raw in enumerate(raw_items, start=1):
        raw_name = raw.get("item_name") or raw.get("name") or raw.get("description")
        cleaned_name = sanitize_text(str(raw_name)) if raw_name is not None else None
        if not cleaned_name:
            raise ValueError(f"Line item {idx} is missing item_name")
        cleaned_name = cleaned_name[:160]

        if "qty" not in raw:
            raise ValueError(f"Line item {idx} is missing qty")
        if "unit_price" not in raw and "rate" not in raw:
            raise ValueError(f"Line item {idx} is missing unit_price")

        qty = _normalize_qty(raw["qty"])
        price_raw = raw["unit_price"] if "unit_price" in raw else raw["rate"]
        unit_price = _to_non_negative_decimal(
            price_raw,
            field_name="unit_price",
            quant=MONEY_QUANT,
            max_value=MAX_UNIT_PRICE,
        )
        computed_line = (qty * unit_price).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)

        written_line_raw = raw.get("written_line_total")
        if written_line_raw is None:
            written_line_raw = raw.get("line_total", raw.get("amount"))
        if written_line_raw is None:
            written_line = computed_line
        else:
            written_line = _to_non_negative_decimal(
                written_line_raw,
                field_name="written_line_total",
                quant=MONEY_QUANT,
                max_value=MAX_UNIT_PRICE * MAX_QTY,
            )

        line_ok = abs(written_line - computed_line) <= tolerance
        audited.append(
            BillLineItem(
                item_name=cleaned_name,
                qty=qty,
                unit_price=unit_price,
                written_line_total=written_line,
                computed_line_total=computed_line,
                line_math_ok=line_ok,
            )
        )
        computed_grand += computed_line
        summed_written_lines += written_line

    computed_total = computed_grand.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    if written_total is None:
        written_grand = summed_written_lines.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    else:
        written_grand = _to_non_negative_decimal(
            written_total,
            field_name="written_total",
            quant=MONEY_QUANT,
            max_value=MAX_UNIT_PRICE * MAX_QTY,
        )

    delta = (written_grand - computed_total).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    all_lines_ok = all(item.line_math_ok for item in audited)
    total_ok = abs(delta) <= tolerance

    return audited, MathCheckResult(
        ok=all_lines_ok and total_ok,
        written_total=written_grand,
        computed_total=computed_total,
        delta=delta,
    )


# Alias matching architecture documentation naming
verify_bill_math = audit_bill_line_items
