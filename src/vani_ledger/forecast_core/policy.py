"""Deterministic inventory policy engine (Order-Up-To, Newsvendor perishable cap, LP budget plan).

Pure Python + NumPy/SciPy only — ZERO LLM or HTTP imports (AGENTS.md Rule 2).
All money calculations use Python Decimal quantized to 0.01 with ROUND_HALF_UP (Rule 3).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import numpy as np
from scipy.optimize import linprog
from scipy.special import ndtri

from vani_ledger.forecast_core.conformal import (
    compute_empirical_stockout_risk,
    compute_service_level,
    demand_dispersion_scale,
)
from vani_ledger.schemas import ReorderPlanItem, ReorderPlanPayload

logger = logging.getLogger("vani_ledger.forecast_core.policy")

MONEY_QUANT = Decimal("0.01")

SEGMENT_TARGET_SERVICE_LEVELS: dict[str, float] = {
    "A": 0.91,
    "B": 0.85,
    "C": 0.78,
}

SEGMENT_PRIORITY_WEIGHTS: dict[str, float] = {
    "A": 1.15,
    "B": 1.00,
    "C": 0.85,
}


def quantize_money(value: Decimal | int | str) -> Decimal:
    """Quantize a monetary amount to 2 decimal places using ROUND_HALF_UP."""
    if isinstance(value, bool):
        raise ValueError("Monetary value cannot be boolean")
    if isinstance(value, Decimal):
        dec = value
    else:
        try:
            dec = Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid monetary amount: {value!r}") from exc
    if not dec.is_finite():
        raise ValueError("Monetary value must be finite")
    return dec.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def compute_newsvendor_critical_ratio(
    unit_price: Decimal,
    unit_cost: Decimal,
    salvage_value: Decimal = Decimal("0.00"),
) -> float:
    """Compute Newsvendor critical ratio CR = (p - c) / ((p - c) + (c - salvage)).

    Uses exact Decimal arithmetic for underage (p - c) and overage (c - salvage)
    before converting the dimensionless probability ratio to float.
    """
    price = quantize_money(unit_price)
    cost = quantize_money(unit_cost)
    salvage = quantize_money(salvage_value)

    underage = price - cost
    overage = cost - salvage

    if underage <= Decimal("0.00"):
        return 0.05
    if overage <= Decimal("0.00"):
        return 0.95

    denom = underage + overage
    if denom <= Decimal("0.00"):
        return 0.50

    ratio = float(underage / denom)
    return float(np.clip(ratio, 0.01, 0.99))


def apply_moq_and_pack_size(
    qty: int,
    *,
    moq: int = 1,
    pack_size: int = 1,
    max_qty: int | None = None,
) -> int:
    """Snap a positive order quantity to pack_size multiples while honoring MOQ and optional cap."""
    if qty <= 0:
        return 0

    eff_moq = max(1, int(moq))
    eff_pack = max(1, int(pack_size))

    rounded = int(math.ceil(qty / eff_pack) * eff_pack)
    if rounded < eff_moq:
        rounded = int(math.ceil(eff_moq / eff_pack) * eff_pack)

    if max_qty is not None:
        if max_qty <= 0:
            return 0
        capped = (int(max_qty) // eff_pack) * eff_pack
        if capped < eff_moq:
            return 0
        rounded = min(rounded, capped)

    return max(0, rounded)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Deterministic output of the single-SKU inventory order policy."""

    recommended_qty: int
    uncapped_qty: int
    lead_time_buffer: int
    safety_stock: float
    target_service_level: float
    achieved_service_level: float
    newsvendor_cr: float
    capped_by_shelf_life: bool
    shelf_life_cap: int | None
    outlay_amount: Decimal


def _coerce_residuals(residuals: Sequence[float] | np.ndarray | str | None) -> np.ndarray:
    """Parse residual sequence or JSON string into a finite float64 NumPy array."""
    if residuals is None:
        return np.empty(0, dtype=np.float64)
    if isinstance(residuals, str):
        try:
            parsed = json.loads(residuals)
        except json.JSONDecodeError:
            return np.empty(0, dtype=np.float64)
        arr = np.asarray(parsed, dtype=np.float64)
    else:
        arr = np.asarray(residuals, dtype=np.float64)
    if arr.ndim == 0:
        return np.empty(0, dtype=np.float64)
    return arr[np.isfinite(arr)]


def compute_order_policy(
    *,
    p50_total: Decimal | float,
    p90_total: Decimal | float,
    residuals: Sequence[float] | np.ndarray | None,
    unit_cost: Decimal,
    unit_price: Decimal,
    salvage_value: Decimal = Decimal("0.00"),
    lead_time_days: int = 1,
    shelf_life_days: int = 90,
    horizon_days: int = 7,
    on_hand: Decimal | float = Decimal("0"),
    on_order: Decimal | float = Decimal("0"),
    moq: int = 1,
    pack_size: int = 1,
    perishable: bool = False,
    abc_segment: str = "B",
    residuals_scaled_for_lead_time: bool = True,
) -> PolicyDecision:
    """Compute deterministic reorder quantity with lead-time buffer, Newsvendor cap, and MOQ.

    - Non-perishable SKUs follow periodic-review Order-Up-To targeting ABC/CR service level
      using empirical conformal safety stock.
    - Perishable / short-shelf-life SKUs apply the Newsvendor critical ratio over the
      shelf-life cycle and enforce the spoilage cap `q <= D_shelf_life - on_hand`.
    - Positive orders are snapped to `pack_size` multiples and minimum `moq`.
    """
    cost = quantize_money(unit_cost)
    cr = compute_newsvendor_critical_ratio(unit_price, cost, salvage_value)

    p50 = max(0.0, float(p50_total))
    p90 = max(p50, float(p90_total))
    oh = max(0.0, float(on_hand))
    oo = max(0.0, float(on_order))
    horizon = max(1, int(horizon_days))
    lead_time = max(0, int(lead_time_days))
    shelf_life = max(1, int(shelf_life_days))

    daily_rate = p50 / horizon
    raw_residuals = _coerce_residuals(residuals)
    lead_scale = math.sqrt((horizon + lead_time) / horizon)

    if len(raw_residuals) > 0:
        eff_residuals = (
            raw_residuals if residuals_scaled_for_lead_time else raw_residuals * lead_scale
        )
    else:
        eff_residuals = raw_residuals

    # Uncapped non-perishable Order-Up-To baseline (used both directly and for cap detection)
    seg_target = SEGMENT_TARGET_SERVICE_LEVELS.get(str(abc_segment).upper(), 0.85)
    non_perish_alpha = float(np.clip(max(seg_target, cr), 0.55, 0.96))

    if len(eff_residuals) > 0:
        uncapped_ss = max(0.0, float(np.quantile(eff_residuals, non_perish_alpha)))
    else:
        implied_sigma = max(
            (p90 - p50) / 1.28155,
            float(demand_dispersion_scale(max(p50, 1.0))),
        )
        uncapped_ss = max(0.0, float(ndtri(non_perish_alpha)) * implied_sigma * lead_scale)

    uncapped_target = p50 + uncapped_ss
    uncapped_raw = max(0, int(round(uncapped_target - oh - oo)))
    uncapped_qty = apply_moq_and_pack_size(uncapped_raw, moq=moq, pack_size=pack_size)

    is_short_life = bool(perishable) or shelf_life < horizon
    shelf_life_cap: int | None = None
    capped_by_shelf_life = False

    if is_short_life:
        # Shelf-life hard spoilage cap: q <= D_shelf_life - on_hand
        d_shelf = daily_rate * shelf_life
        shelf_life_cap = max(0, int(math.floor(d_shelf - oh)))

        # Newsvendor optimal target over effective perishable replenishment cycle
        eff_cycle = min(horizon, shelf_life)
        d_cycle = daily_rate * eff_cycle
        cycle_scale = math.sqrt(eff_cycle / horizon)
        target_alpha = float(np.clip(cr, 0.15, 0.95))

        if len(raw_residuals) > 0:
            scaled_cycle_res = raw_residuals * cycle_scale
            q_cr = float(np.quantile(scaled_cycle_res, target_alpha))
        else:
            cycle_sigma = float(demand_dispersion_scale(max(d_cycle, 1.0)))
            q_cr = float(ndtri(target_alpha)) * cycle_sigma

        newsvendor_target = max(0.0, d_cycle + q_cr)
        perishable_raw = max(0, int(round(newsvendor_target - oh - oo)))
        capped_raw = min(perishable_raw, shelf_life_cap)

        recommended_qty = apply_moq_and_pack_size(
            capped_raw,
            moq=moq,
            pack_size=pack_size,
            max_qty=shelf_life_cap,
        )
        capped_by_shelf_life = recommended_qty < uncapped_qty or perishable_raw > shelf_life_cap
        safety_stock = round(float(q_cr), 2)
        achieved_sl = compute_service_level(
            d_cycle,
            oh + oo + recommended_qty,
            raw_residuals * cycle_scale if len(raw_residuals) > 0 else [],
        )
        lead_time_buffer = max(0, recommended_qty - max(0, int(round(d_cycle - oh - oo))))
    else:
        target_alpha = non_perish_alpha
        recommended_qty = uncapped_qty
        safety_stock = round(float(uncapped_ss), 2)
        achieved_sl = compute_service_level(p50, oh + oo + recommended_qty, eff_residuals)
        # Net buffer units above 7-day expected demand (e.g., +8 for SKU-OIL-001 -> 40 + 8 = 48)
        p50_rounded = int(round(p50))
        if recommended_qty >= p50_rounded:
            lead_time_buffer = recommended_qty - p50_rounded
        else:
            lead_time_buffer = max(0, recommended_qty - max(0, int(round(p50 - oh - oo))))

    outlay = quantize_money(Decimal(recommended_qty) * cost)
    return PolicyDecision(
        recommended_qty=recommended_qty,
        uncapped_qty=uncapped_qty,
        lead_time_buffer=lead_time_buffer,
        safety_stock=safety_stock,
        target_service_level=round(target_alpha, 4),
        achieved_service_level=round(achieved_sl, 4),
        newsvendor_cr=round(cr, 4),
        capped_by_shelf_life=capped_by_shelf_life,
        shelf_life_cap=shelf_life_cap,
        outlay_amount=outlay,
    )


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


@dataclass(slots=True)
class _SkuBudgetCandidate:
    sku_id: UUID
    sku_code: str
    sku_name: str
    category: str
    recommended_qty: int
    effective_target_qty: int
    unit_cost: Decimal
    unit_price: Decimal
    moq: int
    pack_size: int
    margin_per_rupee: float
    stockout_urgency: float
    priority_per_rupee: float
    unit_value: float
    capped_by_shelf_life: bool


def _solve_fractional_budget_lp(
    candidates: list[_SkuBudgetCandidate],
    weekly_budget: Decimal,
    *,
    force_greedy: bool = False,
) -> list[float]:
    """Solve continuous budget allocation LP via HiGHS with automatic greedy fallback."""
    n = len(candidates)
    if n == 0 or weekly_budget <= Decimal("0.00"):
        return [0.0] * n

    total_max_cost = sum(
        (Decimal(c.effective_target_qty) * c.unit_cost for c in candidates),
        Decimal("0.00"),
    )
    if total_max_cost <= weekly_budget:
        return [float(c.effective_target_qty) for c in candidates]

    if not force_greedy:
        try:
            c_obj = np.array([-c.unit_value for c in candidates], dtype=np.float64)
            a_ub = np.array([[float(c.unit_cost) for c in candidates]], dtype=np.float64)
            b_ub = np.array([float(weekly_budget)], dtype=np.float64)
            bounds = [(0.0, float(c.effective_target_qty)) for c in candidates]
            res = linprog(c=c_obj, A_ub=a_ub, b_ub=b_ub, bounds=bounds, method="highs")
            if res.success and res.x is not None and len(res.x) == n:
                return [
                    max(0.0, min(float(val), float(candidates[i].effective_target_qty)))
                    for i, val in enumerate(res.x)
                ]
        except Exception as exc:
            logger.warning(
                "linprog failed (%s); falling back to greedy ranking", type(exc).__name__
            )

    # Clean greedy fallback ranked strictly by priority_per_rupee descending
    alloc = [0.0] * n
    remaining = float(weekly_budget)
    order = sorted(
        range(n),
        key=lambda i: (
            -candidates[i].priority_per_rupee,
            -candidates[i].stockout_urgency,
            candidates[i].sku_code,
        ),
    )
    for idx in order:
        cand = candidates[idx]
        cost_f = float(cand.unit_cost)
        if cost_f <= 0.0 or remaining <= 0.0:
            continue
        take = min(float(cand.effective_target_qty), remaining / cost_f)
        alloc[idx] = max(0.0, take)
        remaining -= alloc[idx] * cost_f
    return alloc


def compute_budget_reorder_plan(
    skus: Sequence[Any],
    forecasts_by_sku_id: Mapping[Any, Any],
    weekly_budget: Decimal,
    *,
    force_greedy: bool = False,
) -> ReorderPlanPayload:
    """Allocate weekly working capital across SKUs using scipy.optimize.linprog + greedy fallback.

    Ranks SKUs by stockout urgency and gross margin per rupee locked, enforces perishable
    shelf-life caps, snaps orders to MOQ/pack_size multiples, and strictly guarantees
    `total_allocated_cost <= weekly_budget` in exact Decimal arithmetic.
    """
    budget = quantize_money(weekly_budget)
    if budget < Decimal("0.00"):
        raise ValueError("weekly_budget must be non-negative")

    candidates: list[_SkuBudgetCandidate] = []
    zero_items: list[ReorderPlanItem] = []

    for sku in skus:
        if not bool(_field(sku, "active", True)):
            continue
        raw_sku_id = _field(sku, "id")
        sku_uuid = raw_sku_id if isinstance(raw_sku_id, UUID) else UUID(str(raw_sku_id))
        forecast = forecasts_by_sku_id.get(str(sku_uuid)) or forecasts_by_sku_id.get(sku_uuid)
        if forecast is None:
            continue

        sku_code = str(_field(sku, "sku_code"))
        sku_name = str(_field(sku, "name"))
        category = str(_field(sku, "category"))
        unit_cost = quantize_money(_field(sku, "unit_cost"))
        unit_price = quantize_money(_field(sku, "unit_price"))
        on_hand = float(_field(sku, "on_hand", Decimal("0")))
        on_order = float(_field(sku, "on_order", Decimal("0")))
        shelf_life_days = max(1, int(_field(sku, "shelf_life_days", 90)))
        perishable = bool(_field(sku, "perishable", False))
        moq = max(1, int(_field(sku, "moq", 1)))
        pack_size = max(1, int(_field(sku, "pack_size", 1)))
        abc_segment = str(_field(sku, "abc_segment", "B")).upper()

        rec_qty = max(0, int(_field(forecast, "recommended_qty", 0)))
        p50_total = max(0.0, float(_field(forecast, "p50_total", Decimal("0"))))
        horizon_days = max(1, int(_field(forecast, "horizon_days", 7)))
        residuals = _coerce_residuals(
            _field(forecast, "residuals_json", _field(forecast, "residuals", None))
        )

        # Detect and enforce perishable shelf-life cap
        is_short_life = perishable or shelf_life_days < horizon_days
        capped_by_shelf_life = False
        effective_target_qty = rec_qty
        if is_short_life:
            daily_rate = p50_total / horizon_days
            shelf_cap = max(0, int(math.floor(daily_rate * shelf_life_days - on_hand)))
            unconstrained_net = max(0, int(round(p50_total - on_hand - on_order)))
            if rec_qty > shelf_cap or unconstrained_net > shelf_cap:
                capped_by_shelf_life = True
                effective_target_qty = min(rec_qty, shelf_cap)

        # Ensure target obeys pack_size / MOQ feasibility under shelf-life cap
        if effective_target_qty > 0:
            effective_target_qty = apply_moq_and_pack_size(
                effective_target_qty,
                moq=moq,
                pack_size=pack_size,
                max_qty=effective_target_qty if capped_by_shelf_life else None,
            )

        unit_margin = max(Decimal("0.01"), unit_price - unit_cost)
        margin_per_rupee = round(
            float(unit_margin / unit_cost) if unit_cost > Decimal("0.00") else 0.0,
            4,
        )
        # Stockout urgency = probability of stocking out if zero units are ordered
        stockout_urgency = round(
            compute_empirical_stockout_risk(p50_total, on_hand + on_order, residuals),
            4,
        )

        if effective_target_qty <= 0 or unit_cost <= Decimal("0.00"):
            zero_items.append(
                ReorderPlanItem(
                    sku_id=sku_uuid,
                    sku_code=sku_code,
                    sku_name=sku_name,
                    category=category,
                    recommended_qty=rec_qty,
                    allocated_qty=0,
                    unit_cost=unit_cost,
                    allocated_cost=Decimal("0.00"),
                    margin_per_rupee=margin_per_rupee,
                    stockout_urgency=stockout_urgency,
                    capped_by_budget=False,
                    capped_by_shelf_life=capped_by_shelf_life,
                )
            )
            continue

        abc_weight = SEGMENT_PRIORITY_WEIGHTS.get(abc_segment, 1.0)
        priority_per_rupee = margin_per_rupee * (0.35 + 0.65 * stockout_urgency) * abc_weight
        unit_value = float(unit_cost) * max(priority_per_rupee, 1e-6)

        candidates.append(
            _SkuBudgetCandidate(
                sku_id=sku_uuid,
                sku_code=sku_code,
                sku_name=sku_name,
                category=category,
                recommended_qty=rec_qty,
                effective_target_qty=effective_target_qty,
                unit_cost=unit_cost,
                unit_price=unit_price,
                moq=moq,
                pack_size=pack_size,
                margin_per_rupee=margin_per_rupee,
                stockout_urgency=stockout_urgency,
                priority_per_rupee=priority_per_rupee,
                unit_value=unit_value,
                capped_by_shelf_life=capped_by_shelf_life,
            )
        )

    lp_solution = _solve_fractional_budget_lp(candidates, budget, force_greedy=force_greedy)

    # Project continuous LP solution onto feasible integer pack_size & MOQ grid
    allocated: list[int] = []
    for cand, raw_val in zip(candidates, lp_solution, strict=True):
        floored_units = int(math.floor(raw_val + 1e-9))
        snapped = (floored_units // cand.pack_size) * cand.pack_size
        if snapped < cand.moq:
            snapped = 0
        snapped = min(snapped, cand.effective_target_qty)
        allocated.append(snapped)

    # Verify exact Decimal budget compliance (trim lowest-priority units if floating LP overshot)
    priority_order = sorted(
        range(len(candidates)),
        key=lambda i: (
            -candidates[i].priority_per_rupee,
            -candidates[i].stockout_urgency,
            candidates[i].sku_code,
        ),
    )

    def _current_spend() -> Decimal:
        return quantize_money(
            sum(
                (Decimal(allocated[i]) * candidates[i].unit_cost for i in range(len(candidates))),
                Decimal("0.00"),
            )
        )

    spent = _current_spend()
    if spent > budget:
        for idx in reversed(priority_order):
            cand = candidates[idx]
            while allocated[idx] > 0 and spent > budget:
                next_qty = allocated[idx] - cand.pack_size
                if next_qty < cand.moq:
                    next_qty = 0
                delta_units = allocated[idx] - next_qty
                allocated[idx] = next_qty
                spent = quantize_money(spent - Decimal(delta_units) * cand.unit_cost)
            if spent <= budget:
                break

    # Greedy top-up pass in priority order using any leftover budget after pack rounding
    remaining_budget = quantize_money(budget - spent)
    for idx in priority_order:
        cand = candidates[idx]
        if allocated[idx] >= cand.effective_target_qty or remaining_budget <= Decimal("0.00"):
            continue
        if allocated[idx] == 0:
            first_step = apply_moq_and_pack_size(1, moq=cand.moq, pack_size=cand.pack_size)
            if first_step > cand.effective_target_qty:
                continue
            step_cost = quantize_money(Decimal(first_step) * cand.unit_cost)
            if step_cost <= remaining_budget:
                allocated[idx] = first_step
                remaining_budget = quantize_money(remaining_budget - step_cost)
            else:
                continue

        # Add full pack_size increments while within target and remaining budget
        pack_cost = quantize_money(Decimal(cand.pack_size) * cand.unit_cost)
        if pack_cost > Decimal("0.00"):
            max_packs_by_target = (cand.effective_target_qty - allocated[idx]) // cand.pack_size
            max_packs_by_budget = int(remaining_budget // pack_cost)
            add_packs = min(max_packs_by_target, max_packs_by_budget)
            if add_packs > 0:
                add_units = add_packs * cand.pack_size
                allocated[idx] += add_units
                remaining_budget = quantize_money(
                    remaining_budget - Decimal(add_units) * cand.unit_cost
                )

    plan_items: list[ReorderPlanItem] = []
    for idx in priority_order:
        cand = candidates[idx]
        alloc_qty = allocated[idx]
        alloc_cost = quantize_money(Decimal(alloc_qty) * cand.unit_cost)
        plan_items.append(
            ReorderPlanItem(
                sku_id=cand.sku_id,
                sku_code=cand.sku_code,
                sku_name=cand.sku_name,
                category=cand.category,
                recommended_qty=cand.recommended_qty,
                allocated_qty=alloc_qty,
                unit_cost=cand.unit_cost,
                allocated_cost=alloc_cost,
                margin_per_rupee=cand.margin_per_rupee,
                stockout_urgency=cand.stockout_urgency,
                capped_by_budget=alloc_qty < cand.effective_target_qty,
                capped_by_shelf_life=cand.capped_by_shelf_life,
            )
        )

    plan_items.extend(sorted(zero_items, key=lambda it: it.sku_code))
    total_allocated = quantize_money(
        sum((it.allocated_cost for it in plan_items), Decimal("0.00"))
    )
    budget_remaining = quantize_money(budget - total_allocated)

    return ReorderPlanPayload(
        weekly_budget=budget,
        total_allocated_cost=total_allocated,
        budget_remaining=budget_remaining,
        items=plan_items,
    )
