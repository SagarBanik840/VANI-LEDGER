"""Unit tests for Block 3: Inventory Policy, <50ms Override Simulator, and FastMCP 4 Tools."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker

from vani_ledger.forecast_core.conformal import calibrate_protection_residuals_for_sku
from vani_ledger.forecast_core.policy import (
    apply_moq_and_pack_size,
    compute_budget_reorder_plan,
    compute_newsvendor_critical_ratio,
    compute_order_policy,
    quantize_money,
)
from vani_ledger.forecast_core.simulator import (
    ForecastSnapshot,
    ForecastSnapshotCache,
    build_forecast_snapshot,
    get_snapshot_cache,
    simulate_override,
)
from vani_ledger.mcp_server.server import (
    FROZEN_TOOL_NAMES,
    build_forecast_payload,
    clear_mcp_call_history,
    get_mcp_inspector_payload,
    get_mcp_tool_catalog,
    get_recent_mcp_calls,
    ingest_bill_ledger,
    mcp,
    record_mcp_invocation,
    simulate_manual_override,
)
from vani_ledger.storage.db import create_db_engine, init_db
from vani_ledger.storage.models import Forecast, Merchant, Sku

HERO_OIL_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-000000000001")
HERO_DAIRY_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-000000000002")
HERO_D2C_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-000000000003")
PRIMARY_MERCHANT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_MERCHANT_ID = "22222222-2222-4222-8222-222222222222"


def _build_calibrated_oil_residuals() -> list[float]:
    """Build realistic Gamma-Poisson calibrated empirical residuals for Hero SKU-OIL-001."""
    rng = np.random.default_rng(42)
    lam = rng.gamma(shape=25.0, scale=40.0 / 25.0, size=96)
    base_emp = rng.poisson(lam) - 40.0
    return calibrate_protection_residuals_for_sku(
        base_emp,
        target_p50=40.0,
        on_hand=10.0,
        recommended_qty=48,
        target_service_level=0.91,
        alt_qty=35,
        alt_stockout_risk=0.23,
    )


def _build_oil_snapshot() -> ForecastSnapshot:
    residuals = _build_calibrated_oil_residuals()
    sku = {
        "id": HERO_OIL_ID,
        "merchant_id": PRIMARY_MERCHANT_ID,
        "sku_code": "SKU-OIL-001",
        "name": "Festive Mustard Oil 1L",
        "category": "Cooking Oil",
        "unit_cost": Decimal("150.00"),
        "unit_price": Decimal("220.00"),
        "salvage_value": Decimal("120.00"),
        "on_hand": Decimal("10.000"),
        "on_order": Decimal("0.000"),
        "lead_time_days": 3,
        "shelf_life_days": 90,
        "moq": 1,
        "pack_size": 1,
        "perishable": False,
    }
    forecast = {
        "id": uuid4(),
        "merchant_id": PRIMARY_MERCHANT_ID,
        "sku_id": HERO_OIL_ID,
        "as_of_date": date(2026, 10, 31),
        "horizon_days": 7,
        "recommended_qty": 48,
        "p10_total": Decimal("32.00"),
        "p50_total": Decimal("40.00"),
        "p90_total": Decimal("57.50"),
        "service_level": Decimal("0.9100"),
        "residuals": residuals,
        "model_version": "lgbm-conformal-v1.0",
    }
    return build_forecast_snapshot(sku, forecast)


def _build_dairy_snapshot() -> ForecastSnapshot:
    rng = np.random.default_rng(7)
    residuals = rng.normal(loc=0.0, scale=12.0, size=80).tolist()
    sku = {
        "id": HERO_DAIRY_ID,
        "merchant_id": PRIMARY_MERCHANT_ID,
        "sku_code": "SKU-DAIRY-001",
        "name": "Fresh Full-Cream Milk 1L",
        "category": "Dairy & Bakery",
        "unit_cost": Decimal("54.00"),
        "unit_price": Decimal("66.00"),
        "salvage_value": Decimal("10.00"),
        "on_hand": Decimal("6.000"),
        "on_order": Decimal("0.000"),
        "lead_time_days": 1,
        "shelf_life_days": 3,
        "moq": 1,
        "pack_size": 1,
        "perishable": True,
    }
    forecast = {
        "id": uuid4(),
        "merchant_id": PRIMARY_MERCHANT_ID,
        "sku_id": HERO_DAIRY_ID,
        "as_of_date": date(2026, 10, 31),
        "horizon_days": 7,
        "recommended_qty": 42,
        "p10_total": Decimal("102.00"),
        "p50_total": Decimal("119.00"),
        "p90_total": Decimal("136.00"),
        "service_level": Decimal("0.7800"),
        "residuals": residuals,
    }
    return build_forecast_snapshot(sku, forecast)


# ── 1. Exact Slide 5 Override Math & Decimal Precision ─────────────────────────


def test_decimal_quantization_half_up_and_guards() -> None:
    assert quantize_money(Decimal("12.345")) == Decimal("12.35")
    assert quantize_money(Decimal("12.344")) == Decimal("12.34")
    assert quantize_money("1950") == Decimal("1950.00")
    assert quantize_money(Decimal("0.005")) == Decimal("0.01")

    with pytest.raises(ValueError):
        quantize_money(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        quantize_money("not-a-number")
    with pytest.raises(ValueError):
        quantize_money(Decimal("NaN"))


def test_slide5_exact_override_math_and_smooth_empirical_cdf() -> None:
    snap = _build_oil_snapshot()
    result = simulate_override(snap, manual_qty=35, weekly_budget=Decimal("50000.00"))

    # Exact Decimal financial values (Slide 5 canonical demo)
    assert isinstance(result.cash_delta, Decimal)
    assert isinstance(result.cash_freed, Decimal)
    assert isinstance(result.outlay_amount, Decimal)
    assert result.suggested_qty == 48
    assert result.manual_qty == 35
    assert result.cash_delta == Decimal("-1950.00")
    assert result.cash_freed == Decimal("1950.00")
    assert result.outlay_amount == Decimal("5250.00")
    assert result.within_budget is True

    # Smooth conformal CDF stockout risk (~0.09 -> ~0.23, delta ~+0.14)
    assert 0.06 <= result.stockout_risk_before <= 0.13
    assert 0.18 <= result.stockout_risk_after <= 0.28
    assert 0.10 <= result.stockout_risk_delta <= 0.18
    raw_delta = result.stockout_risk_after - result.stockout_risk_before
    assert abs(raw_delta - result.stockout_risk_delta) < 1e-6

    # Shelf cover ~5.2 days and zero spoilage for 90-day cooking oil
    assert 4.9 <= result.days_cover <= 5.5
    assert result.spoilage_risk == 0.0

    # Baseline equality when manual_qty == suggested_qty
    same = simulate_override(snap, manual_qty=48, weekly_budget=Decimal("50000.00"))
    assert same.cash_delta == Decimal("0.00")
    assert same.cash_freed == Decimal("0.00")
    assert same.outlay_amount == Decimal("7200.00")
    assert same.stockout_risk_delta == 0.0

    # Strict monotonic increase in stockout risk as quantity decreases
    risks = [
        simulate_override(snap, manual_qty=q).stockout_risk_after
        for q in (60, 48, 40, 35, 20, 0)
    ]
    for earlier, later in itertools.pairwise(risks):
        assert later >= earlier
    assert risks[-1] > risks[0]

    # Budget breach detection
    over_budget = simulate_override(snap, manual_qty=48, weekly_budget=Decimal("5000.00"))
    assert over_budget.within_budget is False


# ── 2. Order-Up-To, Newsvendor Perishable Cap, and MOQ/Pack Rounding ──────────


def test_order_up_to_policy_for_hero_oil_and_lead_time_sensitivity() -> None:
    residuals = _build_calibrated_oil_residuals()
    decision = compute_order_policy(
        p50_total=Decimal("40.00"),
        p90_total=Decimal("57.50"),
        residuals=residuals,
        unit_cost=Decimal("150.00"),
        unit_price=Decimal("220.00"),
        salvage_value=Decimal("120.00"),
        lead_time_days=3,
        shelf_life_days=90,
        horizon_days=7,
        on_hand=Decimal("10.000"),
        on_order=Decimal("0.000"),
        moq=1,
        pack_size=1,
        perishable=False,
        abc_segment="A",
    )

    assert decision.recommended_qty == 48
    assert decision.lead_time_buffer == 8
    assert decision.outlay_amount == Decimal("7200.00")
    assert decision.capped_by_shelf_life is False
    assert 0.87 <= decision.achieved_service_level <= 0.95

    # Longer supplier lead time increases safety stock when scaling horizon residuals
    base_res = np.linspace(-6.0, 8.0, 40)
    short_lt = compute_order_policy(
        p50_total=Decimal("40.00"),
        p90_total=Decimal("50.00"),
        residuals=base_res,
        unit_cost=Decimal("100.00"),
        unit_price=Decimal("150.00"),
        lead_time_days=1,
        on_hand=Decimal("5"),
        abc_segment="A",
        residuals_scaled_for_lead_time=False,
    )
    long_lt = compute_order_policy(
        p50_total=Decimal("40.00"),
        p90_total=Decimal("50.00"),
        residuals=base_res,
        unit_cost=Decimal("100.00"),
        unit_price=Decimal("150.00"),
        lead_time_days=5,
        on_hand=Decimal("5"),
        abc_segment="A",
        residuals_scaled_for_lead_time=False,
    )
    assert long_lt.safety_stock > short_lt.safety_stock
    assert long_lt.recommended_qty >= short_lt.recommended_qty


def test_newsvendor_critical_ratio_and_perishable_spoilage_cap_case02() -> None:
    # SKU-DAIRY-001: price=66, cost=54, salvage=10 -> CR = 12 / (12 + 44) = 0.2143
    cr = compute_newsvendor_critical_ratio(
        unit_price=Decimal("66.00"),
        unit_cost=Decimal("54.00"),
        salvage_value=Decimal("10.00"),
    )
    assert abs(cr - (12.0 / 56.0)) < 1e-6

    rng = np.random.default_rng(11)
    dairy_res = rng.normal(loc=0.0, scale=11.0, size=64)
    decision = compute_order_policy(
        p50_total=Decimal("119.00"),  # ~17 packs/day over 7d
        p90_total=Decimal("136.00"),
        residuals=dairy_res,
        unit_cost=Decimal("54.00"),
        unit_price=Decimal("66.00"),
        salvage_value=Decimal("10.00"),
        lead_time_days=1,
        shelf_life_days=3,
        horizon_days=7,
        on_hand=Decimal("6.000"),
        perishable=True,
        abc_segment="A",
    )

    # 3-day shelf life cap: floor(17 * 3 - 6) = 45 packs (vs uncapped 7d ~125 packs)
    assert decision.capped_by_shelf_life is True
    assert decision.shelf_life_cap == 45
    assert 0 < decision.recommended_qty <= decision.shelf_life_cap
    assert decision.recommended_qty < decision.uncapped_qty

    # Simulator spoilage risk increases sharply when ordering past 3-day shelf life
    dairy_snap = _build_dairy_snapshot()
    safe_sim = simulate_override(dairy_snap, manual_qty=30)
    excess_sim = simulate_override(dairy_snap, manual_qty=75)
    assert excess_sim.spoilage_risk > safe_sim.spoilage_risk
    assert excess_sim.spoilage_risk > 0.80


def test_moq_and_pack_size_rounding_case03() -> None:
    # SKU-D2C-001: moq=10, pack_size=5
    assert apply_moq_and_pack_size(0, moq=10, pack_size=5) == 0
    assert apply_moq_and_pack_size(3, moq=10, pack_size=5) == 10
    assert apply_moq_and_pack_size(10, moq=10, pack_size=5) == 10
    assert apply_moq_and_pack_size(11, moq=10, pack_size=5) == 15
    assert apply_moq_and_pack_size(17, moq=10, pack_size=5, max_qty=14) == 10
    assert apply_moq_and_pack_size(17, moq=10, pack_size=5, max_qty=8) == 0

    d2c_decision = compute_order_policy(
        p50_total=Decimal("42.00"),
        p90_total=Decimal("55.00"),
        residuals=np.linspace(-5.0, 9.0, 48),
        unit_cost=Decimal("180.00"),
        unit_price=Decimal("399.00"),
        salvage_value=Decimal("90.00"),
        lead_time_days=5,
        shelf_life_days=180,
        horizon_days=7,
        on_hand=Decimal("14.000"),
        moq=10,
        pack_size=5,
        perishable=False,
        abc_segment="A",
    )
    assert d2c_decision.recommended_qty >= 10
    assert d2c_decision.recommended_qty % 5 == 0


# ── 3. ₹50,000 Weekly Budget Reorder Plan (scipy.optimize.linprog + greedy) ───


def test_linprog_weekly_budget_reorder_plan_cap_and_ranking() -> None:
    skus: list[dict[str, object]] = [
        # High-margin urgent D2C SKU (margin/rupee = 1.2167, moq=10, pack=5)
        {
            "id": HERO_D2C_ID,
            "sku_code": "SKU-D2C-001",
            "name": "Cold-Pressed Almond Hair Oil 200ml",
            "category": "Micro-D2C",
            "unit_cost": Decimal("180.00"),
            "unit_price": Decimal("399.00"),
            "on_hand": Decimal("4.000"),
            "on_order": Decimal("0.000"),
            "shelf_life_days": 180,
            "perishable": False,
            "moq": 10,
            "pack_size": 5,
            "abc_segment": "A",
            "active": True,
        },
        # Hero Oil (margin/rupee = 0.4667)
        {
            "id": HERO_OIL_ID,
            "sku_code": "SKU-OIL-001",
            "name": "Festive Mustard Oil 1L",
            "category": "Cooking Oil",
            "unit_cost": Decimal("150.00"),
            "unit_price": Decimal("220.00"),
            "on_hand": Decimal("10.000"),
            "on_order": Decimal("0.000"),
            "shelf_life_days": 90,
            "perishable": False,
            "moq": 1,
            "pack_size": 1,
            "abc_segment": "A",
            "active": True,
        },
        # Perishable Dairy (3-day shelf-life cap)
        {
            "id": HERO_DAIRY_ID,
            "sku_code": "SKU-DAIRY-001",
            "name": "Fresh Full-Cream Milk 1L",
            "category": "Dairy & Bakery",
            "unit_cost": Decimal("54.00"),
            "unit_price": Decimal("66.00"),
            "on_hand": Decimal("6.000"),
            "on_order": Decimal("0.000"),
            "shelf_life_days": 3,
            "perishable": True,
            "moq": 1,
            "pack_size": 1,
            "abc_segment": "A",
            "active": True,
        },
    ]

    # Add 12 expensive SKUs so unconstrained outlay easily exceeds ₹50,000
    forecasts: dict[str, dict[str, object]] = {
        str(HERO_D2C_ID): {
            "recommended_qty": 60,
            "p50_total": Decimal("50.00"),
            "horizon_days": 7,
            "residuals_json": "[-4, -2, 0, 2, 5, 8]",
        },
        str(HERO_OIL_ID): {
            "recommended_qty": 48,
            "p50_total": Decimal("40.00"),
            "horizon_days": 7,
            "residuals_json": "[-5, -2, 0, 4, 10, 18]",
        },
        str(HERO_DAIRY_ID): {
            "recommended_qty": 90,  # Intentionally > 3d shelf cap to verify hard cap enforcement
            "p50_total": Decimal("119.00"),
            "horizon_days": 7,
            "residuals_json": "[-6, -3, 0, 3, 6, 10]",
        },
    }

    for idx in range(10, 22):
        sid = UUID(f"aaaaaaaa-aaaa-4aaa-8aaa-{idx:012d}")
        skus.append(
            {
                "id": sid,
                "sku_code": f"SKU-BLK-{idx:03d}",
                "name": f"Bulk Item {idx}",
                "category": "Staples",
                "unit_cost": Decimal("250.00"),
                "unit_price": Decimal("300.00"),  # Lower margin/rupee = 0.20
                "on_hand": Decimal("12.000"),
                "on_order": Decimal("0.000"),
                "shelf_life_days": 180,
                "perishable": False,
                "moq": 1,
                "pack_size": 1,
                "abc_segment": "B",
                "active": True,
            }
        )
        forecasts[str(sid)] = {
            "recommended_qty": 30,  # 30 * 250 = ₹7,500 each -> 12 * 7,500 = ₹90,000 alone
            "p50_total": Decimal("25.00"),
            "horizon_days": 7,
            "residuals_json": "[-3, -1, 0, 2, 4]",
        }

    budget = Decimal("50000.00")
    lp_plan = compute_budget_reorder_plan(skus, forecasts, budget)
    greedy_plan = compute_budget_reorder_plan(skus, forecasts, budget, force_greedy=True)

    for plan in (lp_plan, greedy_plan):
        assert plan.weekly_budget == budget
        assert plan.total_allocated_cost <= budget
        assert plan.budget_remaining == budget - plan.total_allocated_cost
        assert plan.budget_remaining >= Decimal("0.00")
        assert isinstance(plan.total_allocated_cost, Decimal)
        assert isinstance(plan.budget_remaining, Decimal)

        by_code = {item.sku_code: item for item in plan.items}
        # High-margin urgent D2C SKU gets full allocation & respects MOQ/pack_size
        d2c_item = by_code["SKU-D2C-001"]
        assert d2c_item.allocated_qty == 60
        assert d2c_item.allocated_qty % 5 == 0
        assert plan.items[0].sku_code == "SKU-D2C-001"

        # Dairy SKU is strictly capped by 3-day shelf life (floor(119/7 * 3 - 6) = 45)
        dairy_item = by_code["SKU-DAIRY-001"]
        assert dairy_item.capped_by_shelf_life is True
        assert dairy_item.allocated_qty <= 45

        # At least some lower-priority bulk SKUs are capped by the ₹50k budget
        assert any(item.capped_by_budget for item in plan.items)


# ── 4. In-Memory Cache Tenant Isolation & 500-Call <50ms Latency Benchmark ────


def test_snapshot_cache_isolation_and_500_call_sub50ms_latency() -> None:
    cache = ForecastSnapshotCache()
    snap = _build_oil_snapshot()
    cache.put(snap)
    cache.set_merchant_budget(PRIMARY_MERCHANT_ID, Decimal("50000.00"))

    # Tenant isolation (IDOR returns None in-memory)
    assert cache.get(PRIMARY_MERCHANT_ID, HERO_OIL_ID) is not None
    assert cache.get(OTHER_MERCHANT_ID, HERO_OIL_ID) is None
    assert cache.get_by_code(PRIMARY_MERCHANT_ID, "sku-oil-001") is not None
    assert cache.get_by_code(OTHER_MERCHANT_ID, "SKU-OIL-001") is None

    # 500-call latency benchmark on hot path
    latencies_ms: list[float] = []
    for i in range(500):
        q = i % 81
        t0 = time.perf_counter()
        cached_snap = cache.get(PRIMARY_MERCHANT_ID, HERO_OIL_ID)
        assert cached_snap is not None
        budget = cache.get_merchant_budget(PRIMARY_MERCHANT_ID)
        _ = simulate_override(cached_snap, manual_qty=q, weekly_budget=budget)
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    p95_ms = float(np.percentile(latencies_ms, 95))
    p99_ms = float(np.percentile(latencies_ms, 99))
    assert p95_ms < 50.0, f"Override simulator p95 latency {p95_ms:.2f} ms exceeded 50 ms SLA"
    assert p99_ms < 50.0, f"Override simulator p99 latency {p99_ms:.2f} ms exceeded 50 ms SLA"


# ── 5. FastMCP Server Exposes All 4 Frozen Slide 7 Tools & Telemetry ──────────


def test_fastmcp_exposes_all_four_frozen_tools_and_executes_deterministically() -> None:
    clear_mcp_call_history()

    # Verify via FastMCP async introspection AND sync UI catalog export
    async_tools = asyncio.run(mcp.list_tools())
    async_names = {t.name for t in async_tools}
    assert set(FROZEN_TOOL_NAMES) == async_names
    assert len(async_tools) == 4

    catalog = get_mcp_tool_catalog()
    assert [t["name"] for t in catalog] == list(FROZEN_TOOL_NAMES)
    for entry in catalog:
        assert entry["server_name"] == "vani-ledger-mcp"
        assert entry["deterministic"] is True
        assert isinstance(entry["input_schema"], dict)
        assert "properties" in entry["input_schema"]

    # Tool 1: ingest_bill_ledger catches deterministic ₹300 overcharge
    bill_res = ingest_bill_ledger(
        source="manual_test",
        line_items=[
            {
                "item_name": "Sarso Tel 1L",
                "qty": 24,
                "unit_price": "150.00",
                "written_line_total": "3900.00",  # True = 3600 -> ₹300 overcharge
            }
        ],
    )
    assert bill_res["math_check"]["ok"] is False
    assert bill_res["math_check"]["computed_total"] == "3600.00"
    assert bill_res["math_check"]["written_total"] == "3900.00"
    assert bill_res["math_check"]["delta"] == "300.00"
    assert bill_res["needs_review"] is True

    # Path traversal blocked on ingest_bill_ledger
    with pytest.raises(ValueError, match="data/ or uploads/"):
        ingest_bill_ledger(source="upload", image_path="../../etc/passwd")

    # Tool 4: simulate_manual_override shares in-memory ForecastSnapshotCache
    global_cache = get_snapshot_cache()
    global_cache.clear()
    global_cache.put(_build_oil_snapshot())
    global_cache.set_merchant_budget(PRIMARY_MERCHANT_ID, Decimal("50000.00"))

    sim_res = simulate_manual_override(sku_id=str(HERO_OIL_ID), manual_qty=35)
    assert sim_res["cash_freed"] == "1950.00"
    assert sim_res["cash_delta"] == "-1950.00"
    assert 0.09 <= sim_res["stockout_risk_delta"] <= 0.19
    assert 4.9 <= sim_res["days_cover"] <= 5.5

    # Telemetry & Inspector payload capture real calls
    record_mcp_invocation(
        "forecast_sku_demand",
        arguments={"sku_id": str(HERO_OIL_ID)},
        latency_ms=2.1,
        ok=True,
        caller="api",
    )
    recent = get_recent_mcp_calls(limit=10)
    assert len(recent) >= 3
    assert any(c["tool_name"] == "ingest_bill_ledger" and c["ok"] for c in recent)
    assert any(c["tool_name"] == "simulate_manual_override" and c["ok"] for c in recent)

    inspector = get_mcp_inspector_payload(limit=10)
    assert inspector["server_name"] == "vani-ledger-mcp"
    assert inspector["tool_count"] == 4
    assert len(inspector["recent_calls"]) >= 3

    # Golden fixture path via ingest_bill_ledger flags DEMO DOCUMENT and ₹300 overcharge
    fixture_res = ingest_bill_ledger(source="demo_fixture")
    assert fixture_res["demo_document"] is True
    assert fixture_res["source_adapter"] == "demo_fixture"
    assert len(fixture_res["line_items"]) == 4
    assert fixture_res["math_check"]["ok"] is False
    assert fixture_res["math_check"]["computed_total"] == "3900.00"
    assert fixture_res["math_check"]["written_total"] == "4200.00"
    assert fixture_res["math_check"]["delta"] == "300.00"


def test_cache_warm_from_db_and_forecast_payload_builder(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'policy_test.db'}"
    eng = create_db_engine(db_url)
    init_db(eng)
    session_factory = sessionmaker(bind=eng, autoflush=False, autocommit=False)

    residuals = _build_calibrated_oil_residuals()
    drivers = [
        {"name": "Base weekly velocity", "units": 24, "share_pct": 50.0, "direction": "positive"},
        {"name": "Diwali pre-surge", "units": 16, "share_pct": 33.33, "direction": "positive"},
        {
            "name": "3-day supplier lead buffer",
            "units": 8,
            "share_pct": 16.67,
            "direction": "positive",
        },
    ]

    with session_factory() as db:
        db.add_all(
            [
                Merchant(
                    id=PRIMARY_MERCHANT_ID,
                    name="Sharma Kirana",
                    api_key_hash="a" * 64,
                    weekly_budget=Decimal("50000.00"),
                ),
                Merchant(
                    id=OTHER_MERCHANT_ID,
                    name="Verma Store",
                    api_key_hash="b" * 64,
                    weekly_budget=Decimal("35000.00"),
                ),
            ]
        )
        db.flush()
        oil_sku = Sku(
            id=str(HERO_OIL_ID),
            merchant_id=PRIMARY_MERCHANT_ID,
            sku_code="SKU-OIL-001",
            name="Festive Mustard Oil 1L",
            hinglish_alias="Sarso Tel 1L",
            category="Cooking Oil",
            unit="bottle",
            unit_cost=Decimal("150.00"),
            unit_price=Decimal("220.00"),
            salvage_value=Decimal("120.00"),
            lead_time_days=3,
            shelf_life_days=90,
            moq=1,
            pack_size=1,
            perishable=False,
            on_hand=Decimal("10.000"),
            on_order=Decimal("0.000"),
            abc_segment="A",
            active=True,
        )
        db.add(oil_sku)
        db.flush()
        oil_forecast = Forecast(
            id=str(uuid4()),
            merchant_id=PRIMARY_MERCHANT_ID,
            sku_id=str(HERO_OIL_ID),
            as_of_date=date(2026, 10, 31),
            horizon_days=7,
            recommended_qty=48,
            p10_total=Decimal("32.00"),
            p50_total=Decimal("40.00"),
            p90_total=Decimal("57.50"),
            service_level=Decimal("0.9100"),
            data_sufficiency="high",
            model_used="amplified_ensemble",
            model_version="lgbm-conformal-v1.0",
            feature_hash="abc123",
            drivers_json=json.dumps(drivers),
            residuals_json=json.dumps(residuals),
        )
        db.add(oil_forecast)
        db.commit()

        # Verify shared ForecastPayload builder
        payload = build_forecast_payload(oil_sku, oil_forecast)
        assert payload.recommended_qty == 48
        assert payload.outlay_amount == Decimal("7200.00")
        assert sum(d.units for d in payload.drivers) == 48

        # Verify DB cache warmup + tenant isolation
        cache = ForecastSnapshotCache()
        warmed = cache.warm_from_db(db)
        assert warmed == 1
        assert cache.get_merchant_budget(PRIMARY_MERCHANT_ID) == Decimal("50000.00")
        assert cache.get_merchant_budget(OTHER_MERCHANT_ID) == Decimal("35000.00")
        assert cache.get(OTHER_MERCHANT_ID, HERO_OIL_ID) is None

        snap = cache.get(PRIMARY_MERCHANT_ID, HERO_OIL_ID)
        assert snap is not None
        sim = simulate_override(snap, manual_qty=35, weekly_budget=Decimal("50000.00"))
        assert sim.cash_freed == Decimal("1950.00")
        assert 4.9 <= sim.days_cover <= 5.5
    eng.dispose()

