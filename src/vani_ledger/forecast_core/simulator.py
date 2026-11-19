"""Sub-50ms in-memory manual override simulator and ForecastSnapshotCache.

HOT PATH GUARANTEE:
- Zero DB queries during `simulate_override()` — reads immutable in-memory ForecastSnapshot.
- Zero LLM or HTTP imports (AGENTS.md Rule 2).
- Exact Python Decimal money arithmetic quantized to 0.01 ROUND_HALF_UP (Rule 3).
- Smooth empirical conformal residual CDF for stockout & spoilage probabilities (zero hardcoding).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from vani_ledger.forecast_core.conformal import (
    compute_empirical_stockout_risk,
    compute_spoilage_risk,
)
from vani_ledger.forecast_core.policy import quantize_money
from vani_ledger.schemas import OverrideSimulatePayload
from vani_ledger.storage.models import Forecast, Merchant, Sku
from vani_ledger.storage.repository import list_latest_forecasts_for_merchant

DEFAULT_WEEKLY_BUDGET = Decimal("50000.00")
MAX_MANUAL_QTY = 100_000


def compute_effective_daily_demand(
    p50_total: Decimal | float,
    p90_total: Decimal | float,
    horizon_days: int,
) -> float:
    """Compute risk-adjusted effective daily demand rate for conservative shelf-cover calculation.

    Uses the upper-tail planning demand `p50 + 0.40 * max(0, p90 - p50)` (~p70 rate)
    divided by horizon days so sellers do not overestimate cover during festival surges.
    """
    p50 = max(0.0, float(p50_total))
    p90 = max(p50, float(p90_total))
    horizon = max(1, int(horizon_days))
    planning_total = p50 + 0.40 * max(0.0, p90 - p50)
    return max(0.05, planning_total / horizon)


def _parse_residuals_tuple(raw: Any) -> tuple[float, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        seq: Sequence[Any] = parsed if isinstance(parsed, Sequence) else ()
    elif isinstance(raw, np.ndarray):
        seq = raw.tolist()
    elif isinstance(raw, Sequence):
        seq = raw
    else:
        return ()

    out: list[float] = []
    for item in seq:
        try:
            val = float(item)
        except (TypeError, ValueError):
            continue
        if np.isfinite(val):
            out.append(val)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ForecastSnapshot:
    """Immutable in-memory forecast + SKU state used by the <50ms override simulator."""

    forecast_id: UUID
    merchant_id: str
    sku_id: UUID
    sku_code: str
    sku_name: str
    category: str
    as_of_date: date
    horizon_days: int
    recommended_qty: int
    p10_total: Decimal
    p50_total: Decimal
    p90_total: Decimal
    service_level: float
    unit_cost: Decimal
    unit_price: Decimal
    salvage_value: Decimal
    on_hand: Decimal
    on_order: Decimal
    lead_time_days: int
    shelf_life_days: int
    moq: int
    pack_size: int
    perishable: bool
    residuals: tuple[float, ...]
    residuals_array: np.ndarray
    effective_daily_demand: float
    model_version: str = "lgbm-conformal-v1.0"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def build_forecast_snapshot(sku: Any, forecast: Any) -> ForecastSnapshot:
    """Construct an immutable ForecastSnapshot from ORM models or mapping dicts."""
    raw_fid = _attr(forecast, "id", _attr(forecast, "forecast_id"))
    raw_sid = _attr(sku, "id", _attr(forecast, "sku_id"))
    forecast_uuid = raw_fid if isinstance(raw_fid, UUID) else UUID(str(raw_fid))
    sku_uuid = raw_sid if isinstance(raw_sid, UUID) else UUID(str(raw_sid))

    merchant_id = str(_attr(sku, "merchant_id", _attr(forecast, "merchant_id", "")))
    as_of_raw = _attr(forecast, "as_of_date")
    as_of = date.fromisoformat(as_of_raw) if isinstance(as_of_raw, str) else as_of_raw
    horizon_days = max(1, int(_attr(forecast, "horizon_days", 7)))

    p10 = quantize_money(_attr(forecast, "p10_total", Decimal("0.00")))
    p50 = quantize_money(_attr(forecast, "p50_total", Decimal("0.00")))
    p90 = quantize_money(_attr(forecast, "p90_total", p50))

    raw_residuals = _attr(forecast, "residuals_json", _attr(forecast, "residuals", ()))
    residuals = _parse_residuals_tuple(raw_residuals)
    residuals_array = np.asarray(residuals, dtype=np.float64)

    eff_daily = compute_effective_daily_demand(p50, p90, horizon_days)

    return ForecastSnapshot(
        forecast_id=forecast_uuid,
        merchant_id=merchant_id,
        sku_id=sku_uuid,
        sku_code=str(_attr(sku, "sku_code")),
        sku_name=str(_attr(sku, "name", _attr(sku, "sku_name", ""))),
        category=str(_attr(sku, "category", "")),
        as_of_date=as_of,
        horizon_days=horizon_days,
        recommended_qty=max(0, int(_attr(forecast, "recommended_qty", 0))),
        p10_total=p10,
        p50_total=p50,
        p90_total=p90,
        service_level=float(_attr(forecast, "service_level", 0.90)),
        unit_cost=quantize_money(_attr(sku, "unit_cost")),
        unit_price=quantize_money(_attr(sku, "unit_price")),
        salvage_value=quantize_money(_attr(sku, "salvage_value", Decimal("0.00"))),
        on_hand=Decimal(str(_attr(sku, "on_hand", Decimal("0")))),
        on_order=Decimal(str(_attr(sku, "on_order", Decimal("0")))),
        lead_time_days=max(0, int(_attr(sku, "lead_time_days", 1))),
        shelf_life_days=max(1, int(_attr(sku, "shelf_life_days", 90))),
        moq=max(1, int(_attr(sku, "moq", 1))),
        pack_size=max(1, int(_attr(sku, "pack_size", 1))),
        perishable=bool(_attr(sku, "perishable", False)),
        residuals=residuals,
        residuals_array=residuals_array,
        effective_daily_demand=eff_daily,
        model_version=str(_attr(forecast, "model_version", "lgbm-conformal-v1.0")),
    )


def simulate_override(
    snapshot: ForecastSnapshot,
    manual_qty: int,
    weekly_budget: Decimal = DEFAULT_WEEKLY_BUDGET,
) -> OverrideSimulatePayload:
    """Pure in-memory deterministic override simulation (<1 ms typical, <50 ms SLA).

    Computes exact Decimal cash delta / cash freed / outlay, smooth empirical conformal
    stockout risk before & after override, conservative shelf days cover, perishable
    spoilage risk, and budget compliance.
    """
    if isinstance(manual_qty, bool) or not isinstance(manual_qty, int):
        raise ValueError("manual_qty must be an integer")
    if manual_qty < 0 or manual_qty > MAX_MANUAL_QTY:
        raise ValueError(f"manual_qty must be between 0 and {MAX_MANUAL_QTY}")

    unit_cost = quantize_money(snapshot.unit_cost)
    budget = quantize_money(weekly_budget)
    suggested_qty = int(snapshot.recommended_qty)

    # Exact Decimal monetary arithmetic (zero float money)
    cash_delta = quantize_money(Decimal(manual_qty - suggested_qty) * unit_cost)
    cash_freed = quantize_money(max(Decimal("0.00"), -cash_delta))
    outlay_amount = quantize_money(Decimal(manual_qty) * unit_cost)
    within_budget = outlay_amount <= budget

    # Smooth empirical conformal residual CDF stockout risk (zero hardcoding)
    p50_f = float(snapshot.p50_total)
    base_inventory = float(snapshot.on_hand) + float(snapshot.on_order)
    stockout_before = round(
        compute_empirical_stockout_risk(
            p50_f,
            base_inventory + suggested_qty,
            snapshot.residuals_array,
        ),
        4,
    )
    stockout_after = round(
        compute_empirical_stockout_risk(
            p50_f,
            base_inventory + manual_qty,
            snapshot.residuals_array,
        ),
        4,
    )
    stockout_delta = round(stockout_after - stockout_before, 4)

    # Order / shelf days cover
    days_cover = (
        round(float(manual_qty) / snapshot.effective_daily_demand, 2) if manual_qty > 0 else 0.0
    )

    # Perishable spoilage risk: P(D_shelf_life < on_hand + manual_qty)
    spoilage_risk = round(
        compute_spoilage_risk(
            p50_f,
            float(snapshot.on_hand) + float(manual_qty),
            snapshot.residuals_array,
            horizon_days=snapshot.horizon_days,
            shelf_life_days=snapshot.shelf_life_days,
            perishable=snapshot.perishable,
        ),
        4,
    )

    return OverrideSimulatePayload(
        sku_id=snapshot.sku_id,
        suggested_qty=suggested_qty,
        manual_qty=manual_qty,
        cash_delta=cash_delta,
        cash_freed=cash_freed,
        stockout_risk_before=stockout_before,
        stockout_risk_after=stockout_after,
        stockout_risk_delta=stockout_delta,
        days_cover=days_cover,
        spoilage_risk=spoilage_risk,
        within_budget=within_budget,
        outlay_amount=outlay_amount,
    )


class ForecastSnapshotCache:
    """Thread-safe, tenant-scoped in-memory cache of latest ForecastSnapshots.

    Warmed on FastAPI startup and after demo resets so `POST /v1/overrides/simulate`
    executes with zero database hits while enforcing strict `(merchant_id, sku_id)` isolation.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[tuple[str, str], ForecastSnapshot] = {}
        self._by_code: dict[tuple[str, str], ForecastSnapshot] = {}
        self._merchant_budgets: dict[str, Decimal] = {}

    def warm_from_db(self, db: Session, merchant_id: str | None = None) -> int:
        """Load latest forecasts & active SKUs from DB into memory atomically."""
        merchant_stmt = select(Merchant)
        if merchant_id is not None:
            merchant_stmt = merchant_stmt.where(Merchant.id == merchant_id)
        merchants = db.scalars(merchant_stmt).all()

        new_by_id: dict[tuple[str, str], ForecastSnapshot] = {}
        new_by_code: dict[tuple[str, str], ForecastSnapshot] = {}
        new_budgets: dict[str, Decimal] = {}

        for merchant in merchants:
            new_budgets[merchant.id] = quantize_money(merchant.weekly_budget)
            skus = db.scalars(
                select(Sku).where(Sku.merchant_id == merchant.id, Sku.active.is_(True))
            ).all()
            latest_forecasts: dict[str, Forecast] = list_latest_forecasts_for_merchant(
                db, merchant.id
            )
            for sku in skus:
                forecast = latest_forecasts.get(sku.id)
                if forecast is None:
                    continue
                snap = build_forecast_snapshot(sku, forecast)
                new_by_id[(merchant.id, str(snap.sku_id))] = snap
                new_by_code[(merchant.id, snap.sku_code.upper())] = snap

        with self._lock:
            if merchant_id is None:
                self._by_id = new_by_id
                self._by_code = new_by_code
                self._merchant_budgets = new_budgets
            else:
                self._by_id = {
                    k: v for k, v in self._by_id.items() if k[0] != merchant_id
                }
                self._by_code = {
                    k: v for k, v in self._by_code.items() if k[0] != merchant_id
                }
                self._by_id.update(new_by_id)
                self._by_code.update(new_by_code)
                self._merchant_budgets.update(new_budgets)

        return len(new_by_id)

    def put(self, snapshot: ForecastSnapshot) -> None:
        """Insert or replace a single tenant-scoped ForecastSnapshot."""
        with self._lock:
            self._by_id[(snapshot.merchant_id, str(snapshot.sku_id))] = snapshot
            self._by_code[(snapshot.merchant_id, snapshot.sku_code.upper())] = snapshot

    def get(self, merchant_id: str, sku_id: str | UUID) -> ForecastSnapshot | None:
        """Lookup snapshot strictly scoped to `merchant_id` (zero DB queries)."""
        key = (merchant_id, str(sku_id))
        with self._lock:
            return self._by_id.get(key)

    def get_by_code(self, merchant_id: str, sku_code: str) -> ForecastSnapshot | None:
        """Lookup snapshot by SKU code strictly scoped to `merchant_id`."""
        key = (merchant_id, sku_code.strip().upper())
        with self._lock:
            return self._by_code.get(key)

    def get_merchant_budget(
        self,
        merchant_id: str,
        default: Decimal = DEFAULT_WEEKLY_BUDGET,
    ) -> Decimal:
        """Return cached weekly budget for merchant_id."""
        with self._lock:
            return self._merchant_budgets.get(merchant_id, quantize_money(default))

    def set_merchant_budget(self, merchant_id: str, weekly_budget: Decimal) -> None:
        """Cache weekly budget for merchant_id."""
        with self._lock:
            self._merchant_budgets[merchant_id] = quantize_money(weekly_budget)

    def list_for_merchant(self, merchant_id: str) -> list[ForecastSnapshot]:
        """Return all cached snapshots belonging to merchant_id."""
        with self._lock:
            return [snap for (mid, _), snap in self._by_id.items() if mid == merchant_id]

    def clear(self) -> None:
        """Empty the in-memory cache."""
        with self._lock:
            self._by_id.clear()
            self._by_code.clear()
            self._merchant_budgets.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._by_id)

    def __len__(self) -> int:
        return self.size()


snapshot_cache = ForecastSnapshotCache()


def get_snapshot_cache() -> ForecastSnapshotCache:
    """Return the process-wide in-memory ForecastSnapshotCache singleton."""
    return snapshot_cache
