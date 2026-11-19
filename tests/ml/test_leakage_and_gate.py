"""ML rigor tests: zero future leakage, stockout censoring, regime routing, hero & gate."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from eval.backtest import REPORT_PATH, run_backtest
from scripts.seed import seed_database
from scripts.train import train_and_populate
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from vani_ledger.config import load_demo_config
from vani_ledger.forecast_core.features import (
    FEATURE_COLUMNS,
    build_features,
    classify_demand_regime,
    impute_censored_demand,
    load_dataframes_from_db,
)
from vani_ledger.forecast_core.ml_model import (
    compute_inverse_wape_weights,
    compute_override_feedback_adjustment,
)
from vani_ledger.forecast_core.simulator import ForecastSnapshotCache, simulate_override
from vani_ledger.storage.db import create_db_engine
from vani_ledger.storage.models import Forecast, Sku


@pytest.fixture(scope="module")
def isolated_ml_db(tmp_path_factory: pytest.TempPathFactory):
    """Build an isolated seeded + trained SQLite WAL database for ML & gate tests."""
    tmp_dir = tmp_path_factory.mktemp("vani_ml_test")
    db_file = tmp_dir / "ml_test.db"
    eng = create_db_engine(f"sqlite:///{db_file}")
    seed_database(target_engine=eng, reset=True)
    train_summary = train_and_populate(target_engine=eng, save_artifacts=False)
    yield eng, train_summary
    eng.dispose()


def test_zero_future_leakage_in_build_features(isolated_ml_db) -> None:
    """Mutating future sales (`>= cutoff`) must leave EVERY feature at `<= cutoff` bit-identical."""
    eng, _ = isolated_ml_db
    cfg = load_demo_config()
    merchant_id = str(cfg["merchant"]["id"])
    session_factory = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    with session_factory() as db:
        sales_df, cal_df, skus_df = load_dataframes_from_db(db, merchant_id)

    # Subset to 5 diverse SKUs for fast exact bit-for-bit verification
    sample_skus = skus_df.head(5).copy()
    sample_ids = set(sample_skus["sku_id"].astype(str))
    sample_sales = sales_df[sales_df["sku_id"].astype(str).isin(sample_ids)].copy()

    cutoff = pd.Timestamp("2026-09-15")
    feat_orig = build_features(sample_sales, cal_df, sample_skus)

    mutated_sales = sample_sales.copy()
    future_mask = pd.to_datetime(mutated_sales["sold_date"]) >= cutoff
    mutated_sales.loc[future_mask, "qty"] = mutated_sales.loc[future_mask, "qty"] * 50.0 + 999.0
    mutated_sales.loc[future_mask, "stockout_flag"] = ~mutated_sales.loc[
        future_mask, "stockout_flag"
    ]
    feat_mut = build_features(mutated_sales, cal_df, sample_skus)

    # Compare every single feature column on all dates <= cutoff (including cutoff itself!)
    past_orig = feat_orig[feat_orig["sold_date"] <= cutoff].reset_index(drop=True)
    past_mut = feat_mut[feat_mut["sold_date"] <= cutoff].reset_index(drop=True)
    assert len(past_orig) == len(past_mut) and len(past_orig) > 0

    for col in FEATURE_COLUMNS:
        np.testing.assert_allclose(
            past_orig[col].to_numpy(dtype=float),
            past_mut[col].to_numpy(dtype=float),
            rtol=0.0,
            atol=0.0,
            err_msg=f"Future leakage detected in feature column '{col}'!",
        )


def test_stockout_censoring_imputation_is_causal() -> None:
    """Stockout day is causally imputed from past same-weekday median, never future sales."""
    start = date(2026, 1, 5)  # Monday
    rows = []
    for day_offset in range(42):
        d = start + timedelta(days=day_offset)
        is_so = day_offset == 28  # 5th Monday
        qty = 0.0 if is_so else (10.0 if d.weekday() == 0 else 4.0)
        rows.append(
            {
                "sku_id": "sku-1",
                "sold_date": pd.Timestamp(d),
                "qty": qty,
                "stockout_flag": is_so,
            }
        )
    df = pd.DataFrame(rows)
    imp = impute_censored_demand(df)
    assert float(imp.iloc[28]) == 10.0

    # Mutate future Mondays (> day 28) wildly and ensure day 28 imputation never changes
    df_mut = df.copy()
    df_mut.loc[df_mut.index > 28, "qty"] = 5000.0
    imp_mut = impute_censored_demand(df_mut)
    assert float(imp_mut.iloc[28]) == 10.0


def test_regime_routing_inverse_wape_weights_and_override_feedback() -> None:
    assert classify_demand_regime(adi=1.05, cv2=0.20, history_days=10) == "cold_start"
    assert classify_demand_regime(adi=1.05, cv2=0.20, history_days=180) == "smooth"
    assert classify_demand_regime(adi=1.10, cv2=0.75, history_days=180) == "erratic"
    assert classify_demand_regime(adi=1.80, cv2=0.25, history_days=180) == "intermittent"
    assert classify_demand_regime(adi=2.10, cv2=0.80, history_days=180) == "lumpy"

    # Stage 1 inverse-WAPE weights strictly clamped to [0.10, 0.80] and sum to 1.0
    weights = compute_inverse_wape_weights({"lgbm": 0.05, "stl": 0.15, "naive": 0.90})
    assert abs(sum(weights.values()) - 1.0) <= 1e-3
    for w in weights.values():
        assert 0.10 - 1e-4 <= w <= 0.80 + 1e-4

    # Stage 5 bounded per-SKU override feedback adjustment
    assert compute_override_feedback_adjustment([], "sku-a") == 1.0
    extreme_up = [
        {"sku_id": "sku-a", "suggested_qty": 20, "manual_qty": 200} for _ in range(10)
    ]
    extreme_down = [
        {"sku_id": "sku-a", "suggested_qty": 100, "manual_qty": 1} for _ in range(10)
    ]
    assert 1.0 < compute_override_feedback_adjustment(extreme_up, "sku-a") <= 1.15
    assert 0.85 <= compute_override_feedback_adjustment(extreme_down, "sku-a") < 1.0


def test_trained_forecasts_hero_calibration_and_driver_invariants(isolated_ml_db) -> None:
    """Verify Slide 5/6 hero SKU invariants and exact additive drivers across all 40 SKUs."""
    eng, summary = isolated_ml_db
    assert summary["total_forecasts"] == 43

    cfg = load_demo_config()
    merchant_id = str(cfg["merchant"]["id"])
    session_factory = sessionmaker(bind=eng, autoflush=False, autocommit=False)

    with session_factory() as db:
        skus = db.scalars(select(Sku).where(Sku.merchant_id == merchant_id)).all()
        forecasts = db.scalars(select(Forecast).where(Forecast.merchant_id == merchant_id)).all()
        assert len(skus) == 40
        assert len(forecasts) == 40

        by_sku = {f.sku_id: f for f in forecasts}
        for sku in skus:
            fc = by_sku[sku.id]
            drivers = json.loads(fc.drivers_json)
            assert sum(int(d["units"]) for d in drivers) == fc.recommended_qty
            assert float(fc.p10_total) <= float(fc.p50_total) <= float(fc.p90_total)
            assert 0.0 <= float(fc.service_level) <= 1.0
            residuals = json.loads(fc.residuals_json)
            assert len(residuals) >= 20

            if sku.sku_code == "SKU-OIL-001":
                assert fc.recommended_qty == 48
                assert 0.89 <= float(fc.service_level) <= 0.93
                drv_map = {d["name"]: int(d["units"]) for d in drivers}
                assert drv_map["Base weekly velocity"] == 24
                assert drv_map["Diwali pre-surge"] == 16
                assert drv_map["3-day supplier lead buffer"] == 8

            if sku.sku_code == "SKU-DAIRY-001":
                assert fc.recommended_qty < float(fc.p50_total)

            if sku.sku_code == "SKU-D2C-001":
                assert fc.recommended_qty >= sku.moq
                assert fc.recommended_qty % sku.pack_size == 0

        cache = ForecastSnapshotCache()
        cache.warm_from_db(db, merchant_id=merchant_id)
        hero_snap = cache.get(merchant_id, "aaaaaaaa-aaaa-4aaa-8aaa-000000000001")
        assert hero_snap is not None
        sim = simulate_override(hero_snap, 35, Decimal("50000.00"))
        assert sim.cash_delta == Decimal("-1950.00")
        assert sim.cash_freed == Decimal("1950.00")
        assert 0.07 <= sim.stockout_risk_before <= 0.11
        assert 0.21 <= sim.stockout_risk_after <= 0.26
        assert 0.12 <= sim.stockout_risk_delta <= 0.16
        assert 4.9 <= sim.days_cover <= 5.5
        assert sim.spoilage_risk == 0.0
        assert sim.within_budget is True


def test_walk_forward_backtest_passes_all_gates(isolated_ml_db, tmp_path: Path) -> None:
    """Run real chronological walk-forward backtest and assert all accuracy & calibration gates."""
    eng, _ = isolated_ml_db
    out_file = tmp_path / "backtest_test.json"
    report = run_backtest(target_engine=eng, save_report=True, gate=True, report_path=out_file)
    assert out_file.exists()
    assert report["gates"]["all_passed"] is True

    amp = report["models"]["amplified_ensemble"]
    assert amp["segment_wape"]["A"] <= 0.12
    assert amp["segment_wape"]["B"] <= 0.18
    assert amp["revenue_weighted_wape"] <= 0.15
    assert report["lift_over_seasonal_naive_pct"] >= 20.0
    assert 0.85 <= amp["p90_empirical_coverage"] <= 0.95
    assert amp["mase"] < 1.0
    assert REPORT_PATH.name == "latest_backtest.json"
