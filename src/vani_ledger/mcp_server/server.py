"""FastMCP server ('vani-ledger-mcp') exposing the 4 frozen deterministic Slide 7 tools.

ARCHITECTURAL GUARANTEE (ADR-003 / AGENTS.md Rule 2):
- Shares the exact same deterministic domain functions as FastAPI — zero duplicated math.
- All 4 tools record sanitized telemetry into an in-memory ring buffer exposed to the
  UI FastMCP Inspector via `get_mcp_tool_catalog()` and `get_recent_mcp_calls()`.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.orm import Session

from vani_ledger.capture.bill_extractor import (
    DEMO_BILL_IMAGE_PATH,
    DemoFixtureExtractor,
    extract_and_audit_bill_file,
)
from vani_ledger.capture.math_audit import audit_bill_line_items
from vani_ledger.config import PROJECT_ROOT, get_settings, load_demo_config
from vani_ledger.forecast_core.policy import quantize_money
from vani_ledger.forecast_core.simulator import (
    DEFAULT_WEEKLY_BUDGET,
    ForecastSnapshot,
    build_forecast_snapshot,
    get_snapshot_cache,
    simulate_override,
)
from vani_ledger.schemas import DriverItem, ForecastPayload
from vani_ledger.services.narrative import generate_grounded_narrative
from vani_ledger.storage.db import SessionLocal
from vani_ledger.storage.models import Forecast, Merchant, Sku
from vani_ledger.storage.repository import (
    get_latest_forecast,
    get_sku_by_code,
    get_sku_for_merchant,
)

SERVER_NAME = "vani-ledger-mcp"
FROZEN_TOOL_NAMES: tuple[str, ...] = (
    "ingest_bill_ledger",
    "forecast_sku_demand",
    "explain_forecast_drivers",
    "simulate_manual_override",
)

mcp = FastMCP(
    SERVER_NAME,
    instructions=(
        "Deterministic demand forecasting, explainability, bill math audit, and sub-50ms "
        "manual override simulation tools for VANI-LEDGER (zero LLM arithmetic)."
    ),
)

_CALL_LOCK = threading.Lock()
_RECENT_CALLS: deque[dict[str, Any]] = deque(maxlen=50)


def _default_merchant_id() -> str:
    cfg = load_demo_config()
    return str(cfg["merchant"]["id"])


def _safe_json_preview(data: Mapping[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    safe: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[str(key)] = value if not isinstance(value, str) else value[:120]
        elif isinstance(value, (Decimal, UUID)):
            safe[str(key)] = str(value)
        elif isinstance(value, list):
            safe[str(key)] = f"[{len(value)} items]"
        elif isinstance(value, Mapping):
            safe[str(key)] = f"{{{len(value)} keys}}"
        else:
            safe[str(key)] = str(value)[:120]
    return safe


def record_mcp_invocation(
    tool_name: str,
    *,
    arguments: Mapping[str, Any] | None = None,
    latency_ms: float = 0.0,
    ok: bool = True,
    result_summary: Mapping[str, Any] | None = None,
    error: str | None = None,
    caller: str = "mcp",
) -> dict[str, Any]:
    """Record a sanitized MCP tool invocation in the thread-safe telemetry ring buffer."""
    entry: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "tool_name": str(tool_name),
        "caller": str(caller),
        "arguments": _safe_json_preview(arguments),
        "latency_ms": round(float(latency_ms), 3),
        "ok": bool(ok),
        "result_summary": _safe_json_preview(result_summary),
        "error": error[:200] if isinstance(error, str) else None,
    }
    with _CALL_LOCK:
        _RECENT_CALLS.appendleft(entry)
    return entry


def get_recent_mcp_calls(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent MCP tool invocations (newest first)."""
    capped = max(1, min(int(limit), 50))
    with _CALL_LOCK:
        return list(_RECENT_CALLS)[:capped]


def clear_mcp_call_history() -> None:
    """Clear recorded MCP telemetry entries (used during resets and tests)."""
    with _CALL_LOCK:
        _RECENT_CALLS.clear()


def _resolve_registered_tools() -> list[Any]:
    """Read registered FunctionTool instances synchronously from the FastMCP server."""
    local_provider = getattr(mcp, "local_provider", None)
    components = getattr(local_provider, "_components", None)
    if isinstance(components, Mapping):
        tools = [
            comp
            for key, comp in components.items()
            if str(key).startswith("tool:") and hasattr(comp, "to_mcp_tool")
        ]
        if tools:
            return tools
    return []


def get_mcp_tool_catalog() -> list[dict[str, Any]]:
    """Return real FastMCP tool schemas registered on `vani-ledger-mcp`."""
    order_index = {name: idx for idx, name in enumerate(FROZEN_TOOL_NAMES)}
    catalog: list[dict[str, Any]] = []

    for tool in _resolve_registered_tools():
        mcp_tool = tool.to_mcp_tool()
        catalog.append(
            {
                "name": str(mcp_tool.name),
                "description": str(mcp_tool.description or "").strip(),
                "input_schema": dict(mcp_tool.inputSchema or {}),
                "output_schema": dict(mcp_tool.outputSchema or {}),
                "server_name": mcp.name,
                "deterministic": True,
            }
        )

    catalog.sort(key=lambda item: (order_index.get(item["name"], 999), item["name"]))
    return catalog


def get_mcp_inspector_payload(limit: int = 20) -> dict[str, Any]:
    """Return combined FastMCP tool catalog and recent invocation telemetry for the UI."""
    tools = get_mcp_tool_catalog()
    return {
        "server_name": mcp.name,
        "tool_count": len(tools),
        "tools": tools,
        "recent_calls": get_recent_mcp_calls(limit=limit),
    }


def _parse_drivers(raw_json: str) -> list[DriverItem]:
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [DriverItem.model_validate(item) for item in parsed]


def build_forecast_payload(sku: Sku, forecast: Forecast) -> ForecastPayload:
    """Build canonical ForecastPayload from ORM Sku + Forecast (shared by FastAPI & FastMCP)."""
    outlay = quantize_money(Decimal(int(forecast.recommended_qty)) * quantize_money(sku.unit_cost))
    return ForecastPayload(
        forecast_id=UUID(str(forecast.id)),
        sku_id=UUID(str(sku.id)),
        sku_code=str(sku.sku_code),
        sku_name=str(sku.name),
        as_of_date=forecast.as_of_date,
        horizon_days=int(forecast.horizon_days),
        recommended_qty=int(forecast.recommended_qty),
        outlay_amount=outlay,
        p10_total=quantize_money(forecast.p10_total),
        p50_total=quantize_money(forecast.p50_total),
        p90_total=quantize_money(forecast.p90_total),
        service_level=round(float(forecast.service_level), 4),
        data_sufficiency=str(forecast.data_sufficiency),  # type: ignore[arg-type]
        model_used=str(forecast.model_used),
        model_version=str(forecast.model_version),
        feature_hash=str(forecast.feature_hash),
        drivers=_parse_drivers(forecast.drivers_json),
    )


def _resolve_sku_for_merchant(db: Session, merchant_id: str, sku_ref: str) -> Sku | None:
    cleaned = sku_ref.strip()
    if not cleaned:
        return None
    sku = get_sku_for_merchant(db, merchant_id, cleaned)
    if sku is not None:
        return sku
    return get_sku_by_code(db, merchant_id, cleaned)


def _validate_safe_image_path(image_path: str) -> Path:
    """Restrict MCP file reads strictly to project fixtures or configured upload_dir."""
    raw = Path(image_path)
    candidate = (PROJECT_ROOT / raw).resolve() if not raw.is_absolute() else raw.resolve()
    allowed_roots = [
        (PROJECT_ROOT / "data").resolve(),
        get_settings().upload_dir.resolve(),
    ]
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        raise ValueError("image_path must reside inside data/ or uploads/ directory")
    if not candidate.is_file():
        raise ValueError("Bill image file not found")
    return candidate


# ── Four Frozen Slide 7 FastMCP Tools ──────────────────────────────────────────


@mcp.tool()
def ingest_bill_ledger(
    source: str,
    image_path: str | None = None,
    line_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and audit bill line items in Python Decimal arithmetic. Flags overcharges."""
    t0 = time.perf_counter()
    args_preview = {
        "source": source,
        "image_path": image_path,
        "line_items": line_items,
    }
    try:
        source_clean = source.strip() if source else "manual"
        if line_items:
            audited_items, math_check = audit_bill_line_items(line_items)
            payload = {
                "source": source_clean,
                "source_adapter": "direct_line_items",
                "demo_document": source_clean.lower() == "demo_fixture",
                "confidence": 1.0,
                "line_items": [it.model_dump(mode="json") for it in audited_items],
                "math_check": math_check.model_dump(mode="json"),
                "needs_review": not math_check.ok,
            }
        elif image_path:
            safe_path = _validate_safe_image_path(image_path)
            raw_ext, audited_items, math_check = extract_and_audit_bill_file(safe_path)
            payload = {
                "source": source_clean,
                "source_adapter": raw_ext.source_adapter,
                "demo_document": raw_ext.demo_document,
                "confidence": round(float(raw_ext.confidence), 4),
                "line_items": [it.model_dump(mode="json") for it in audited_items],
                "math_check": math_check.model_dump(mode="json"),
                "needs_review": (
                    (not math_check.ok)
                    or raw_ext.confidence < 0.90
                    or raw_ext.demo_document
                ),
            }
        else:
            raw_ext = DemoFixtureExtractor().extract(DEMO_BILL_IMAGE_PATH, b"")
            audited_items, math_check = audit_bill_line_items(
                raw_ext.line_items,
                raw_ext.written_total,
            )
            payload = {
                "source": source_clean,
                "source_adapter": raw_ext.source_adapter,
                "demo_document": raw_ext.demo_document,
                "confidence": round(float(raw_ext.confidence), 4),
                "line_items": [it.model_dump(mode="json") for it in audited_items],
                "math_check": math_check.model_dump(mode="json"),
                "needs_review": True,
            }

        latency_ms = (time.perf_counter() - t0) * 1000.0
        record_mcp_invocation(
            "ingest_bill_ledger",
            arguments=args_preview,
            latency_ms=latency_ms,
            ok=True,
            result_summary={
                "line_item_count": len(payload["line_items"]),
                "math_ok": payload["math_check"]["ok"],
                "delta": payload["math_check"]["delta"],
                "demo_document": payload["demo_document"],
            },
        )
        return payload
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        record_mcp_invocation(
            "ingest_bill_ledger",
            arguments=args_preview,
            latency_ms=latency_ms,
            ok=False,
            error=str(exc),
        )
        raise


@mcp.tool()
def forecast_sku_demand(
    sku_id: str,
    as_of: str | None = None,
    horizon_days: int = 7,
) -> dict[str, Any]:
    """Return deterministic p10/p50/p90 forecast, recommended order qty, service level & drivers."""
    t0 = time.perf_counter()
    args_preview = {"sku_id": sku_id, "as_of": as_of, "horizon_days": horizon_days}
    try:
        if horizon_days < 1 or horizon_days > 30:
            raise ValueError("horizon_days must be between 1 and 30")

        merchant_id = _default_merchant_id()
        with SessionLocal() as db:
            sku = _resolve_sku_for_merchant(db, merchant_id, sku_id)
            if sku is None:
                raise ValueError(f"SKU not found: {sku_id}")

            forecast = get_latest_forecast(db, merchant_id, sku.id)
            if forecast is None:
                raise ValueError(f"No forecast available for SKU: {sku.sku_code}")

            payload = build_forecast_payload(sku, forecast)
            result = payload.model_dump(mode="json")

        latency_ms = (time.perf_counter() - t0) * 1000.0
        record_mcp_invocation(
            "forecast_sku_demand",
            arguments=args_preview,
            latency_ms=latency_ms,
            ok=True,
            result_summary={
                "sku_code": result["sku_code"],
                "recommended_qty": result["recommended_qty"],
                "outlay_amount": result["outlay_amount"],
                "service_level": result["service_level"],
            },
        )
        return result
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        record_mcp_invocation(
            "forecast_sku_demand",
            arguments=args_preview,
            latency_ms=latency_ms,
            ok=False,
            error=str(exc),
        )
        raise


@mcp.tool()
def explain_forecast_drivers(
    sku_id: str,
    forecast_id: str | None = None,
) -> dict[str, Any]:
    """Return exact additive driver breakdown and number-verified plain-language narrative."""
    t0 = time.perf_counter()
    args_preview = {"sku_id": sku_id, "forecast_id": forecast_id}
    try:
        merchant_id = _default_merchant_id()
        with SessionLocal() as db:
            sku = _resolve_sku_for_merchant(db, merchant_id, sku_id)
            if sku is None:
                raise ValueError(f"SKU not found: {sku_id}")

            forecast: Forecast | None = None
            if forecast_id:
                stmt = select(Forecast).where(
                    Forecast.id == forecast_id.strip(),
                    Forecast.merchant_id == merchant_id,
                    Forecast.sku_id == sku.id,
                )
                forecast = db.scalar(stmt)
            if forecast is None:
                forecast = get_latest_forecast(db, merchant_id, sku.id)
            if forecast is None:
                raise ValueError(f"No forecast available for SKU: {sku.sku_code}")

            forecast_payload = build_forecast_payload(sku, forecast)

        explain_payload = generate_grounded_narrative(forecast_payload, language="en")
        result = explain_payload.model_dump(mode="json")

        latency_ms = (time.perf_counter() - t0) * 1000.0
        record_mcp_invocation(
            "explain_forecast_drivers",
            arguments=args_preview,
            latency_ms=latency_ms,
            ok=True,
            result_summary={
                "recommended_qty": result["recommended_qty"],
                "driver_count": len(result["drivers"]),
                "grounded": result["grounded"],
                "source": result["source"],
            },
        )
        return result
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        record_mcp_invocation(
            "explain_forecast_drivers",
            arguments=args_preview,
            latency_ms=latency_ms,
            ok=False,
            error=str(exc),
        )
        raise


@mcp.tool()
def simulate_manual_override(
    sku_id: str,
    manual_qty: int,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Sub-50ms deterministic simulation of cash_delta, stockout_risk, days_cover & spoilage."""
    t0 = time.perf_counter()
    args_preview = {"sku_id": sku_id, "manual_qty": manual_qty, "as_of": as_of}
    try:
        merchant_id = _default_merchant_id()
        cache = get_snapshot_cache()

        snapshot: ForecastSnapshot | None = (
            cache.get(merchant_id, sku_id) or cache.get_by_code(merchant_id, sku_id)
        )
        weekly_budget = cache.get_merchant_budget(merchant_id, DEFAULT_WEEKLY_BUDGET)

        if snapshot is None:
            with SessionLocal() as db:
                cache.warm_from_db(db, merchant_id=merchant_id)
                snapshot = (
                    cache.get(merchant_id, sku_id) or cache.get_by_code(merchant_id, sku_id)
                )
                weekly_budget = cache.get_merchant_budget(merchant_id, DEFAULT_WEEKLY_BUDGET)

                if snapshot is None:
                    sku = _resolve_sku_for_merchant(db, merchant_id, sku_id)
                    if sku is None:
                        raise ValueError(f"SKU not found: {sku_id}")
                    forecast = get_latest_forecast(db, merchant_id, sku.id)
                    if forecast is None:
                        raise ValueError(f"No forecast available for SKU: {sku.sku_code}")
                    merchant = db.scalar(select(Merchant).where(Merchant.id == merchant_id))
                    if merchant is not None:
                        weekly_budget = quantize_money(merchant.weekly_budget)
                        cache.set_merchant_budget(merchant_id, weekly_budget)
                    snapshot = build_forecast_snapshot(sku, forecast)
                    cache.put(snapshot)

        payload = simulate_override(snapshot, manual_qty=manual_qty, weekly_budget=weekly_budget)
        result = payload.model_dump(mode="json")

        latency_ms = (time.perf_counter() - t0) * 1000.0
        record_mcp_invocation(
            "simulate_manual_override",
            arguments=args_preview,
            latency_ms=latency_ms,
            ok=True,
            result_summary={
                "suggested_qty": result["suggested_qty"],
                "manual_qty": result["manual_qty"],
                "cash_freed": result["cash_freed"],
                "stockout_risk_delta": result["stockout_risk_delta"],
                "days_cover": result["days_cover"],
            },
        )
        return result
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        record_mcp_invocation(
            "simulate_manual_override",
            arguments=args_preview,
            latency_ms=latency_ms,
            ok=False,
            error=str(exc),
        )
        raise


if __name__ == "__main__":
    mcp.run()
