"""Statistical baselines: Seasonal Naive, STL/ETS Calendar Decomposition, and Croston-SBA.

All baselines strictly observe data with `sold_date <= as_of_date` and exclude or
causally impute stockout-censored observations. Zero LLM or HTTP imports.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.seasonal import STL

from vani_ledger.forecast_core.features import (
    build_dense_sales_grid,
    build_horizon_calendar_dates,
    impute_censored_demand,
    normalize_skus_df,
)


def _prepare_history_and_calendar(
    sales_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    skus_df: pd.DataFrame,
    as_of_date: date,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build dense strictly-past history (`<= as_of_date`) and normalized calendar."""
    skus_norm = normalize_skus_df(skus_df)
    dense = build_dense_sales_grid(sales_df, skus_norm)
    cutoff = pd.Timestamp(as_of_date)
    hist = dense[dense["sold_date"] <= cutoff].copy()
    if not hist.empty:
        hist["demand_imputed"] = impute_censored_demand(hist)

    cal = cal_df.copy()
    date_col = "event_date" if "event_date" in cal.columns else "sold_date"
    cal["sold_date"] = pd.to_datetime(cal[date_col]).dt.normalize()
    cal["festival_pre_surge_weight"] = (
        pd.to_numeric(cal["festival_pre_surge_weight"], errors="coerce").fillna(0.0).astype(float)
    )
    cal["is_payday_window"] = cal["is_payday_window"].astype(bool).astype(float)
    cal["is_festival_window"] = cal["is_festival_window"].astype(bool).astype(float)
    return hist, cal, skus_norm


def predict_seasonal_naive_batch(
    sales_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    skus_df: pd.DataFrame,
    as_of_date: date,
    horizon_days: int = 7,
) -> pd.DataFrame:
    """Baseline 1: 4-week uncensored same-weekday mean strictly prior to `as_of_date`."""
    hist, _cal, skus_norm = _prepare_history_and_calendar(sales_df, cal_df, skus_df, as_of_date)
    horizon_dates = [
        pd.Timestamp(d) for d in build_horizon_calendar_dates(as_of_date, horizon_days)
    ]

    records: list[dict[str, Any]] = []
    grouped = {str(k): v for k, v in hist.groupby("sku_id", sort=False)} if not hist.empty else {}

    for sku_id in skus_norm["sku_id"].astype(str):
        grp = grouped.get(sku_id)
        if grp is None or grp.empty:
            for dt in horizon_dates:
                records.append({"sku_id": sku_id, "sold_date": dt, "pred_naive": 0.0})
            continue

        unc = grp[~grp["stockout_flag"].astype(bool)].copy()
        if unc.empty:
            unc = grp.copy()
        unc["dow"] = unc["sold_date"].dt.dayofweek
        fallback_mean = float(unc["qty"].tail(28).mean()) if len(unc) > 0 else 0.0

        dow_means: dict[int, float] = {}
        for dow_val, dgrp in unc.groupby("dow"):
            dow_means[int(dow_val)] = float(dgrp["qty"].tail(4).mean())

        for dt in horizon_dates:
            pred = max(0.0, dow_means.get(int(dt.dayofweek), fallback_mean))
            records.append({"sku_id": sku_id, "sold_date": dt, "pred_naive": pred})

    return pd.DataFrame(records)


def _fit_multiplicative_calendar_params(
    y: np.ndarray,
    fest_w: np.ndarray,
    payday: np.ndarray,
    fest_win: np.ndarray,
    promo: np.ndarray,
    uncensored_mask: np.ndarray,
) -> dict[str, float]:
    """Estimate multiplicative calendar, festival, payday, and promo effects strictly on past data.

    Uses regularized ratio estimation directly on the uncensored count scale (avoiding
    log(y + 0.5) small-count distortion).
    """
    mask = uncensored_mask & np.isfinite(y)
    if int(mask.sum()) < 28 or float(np.mean(y[mask])) < 0.05:
        return {
            "fest_sens": 0.0,
            "payday_hi": 1.0,
            "payday_lo": 1.0,
            "promo_lift": 0.0,
            "p_promo_fest": 0.0,
            "p_promo_norm": 0.0,
        }

    y_m = y[mask]
    fw_m = fest_w[mask]
    pd_m = payday[mask]
    fwin_m = fest_win[mask]
    pr_m = promo[mask]

    # Non-festival, non-promo baseline mean (with gentle linear drift)
    quiet_mask = (fwin_m < 0.5) & (pr_m < 0.5)
    if int(quiet_mask.sum()) >= 14:
        base_level = float(np.mean(y_m[quiet_mask]))
    else:
        base_level = float(np.mean(y_m))
    base_level = max(base_level, 0.10)

    # Payday multipliers (shrunk toward 1.0 with empirical Bayes prior weight m=20)
    pay_hi_mask = quiet_mask & (pd_m >= 0.5)
    pay_lo_mask = quiet_mask & (pd_m < 0.5)
    n_hi = int(pay_hi_mask.sum())
    n_lo = int(pay_lo_mask.sum())
    mean_hi = float(np.mean(y_m[pay_hi_mask])) if n_hi > 0 else base_level
    mean_lo = float(np.mean(y_m[pay_lo_mask])) if n_lo > 0 else base_level
    payday_hi = float(np.clip((n_hi * (mean_hi / base_level) + 20.0) / (n_hi + 20.0), 0.90, 1.20))
    payday_lo = float(np.clip((n_lo * (mean_lo / base_level) + 20.0) / (n_lo + 20.0), 0.85, 1.10))

    pay_factor = np.where(pd_m >= 0.5, payday_hi, payday_lo)

    # Promo lift strictly on past uncensored non-festival rows (shrunk toward 0.12)
    promo_mask = (fwin_m < 0.5) & (pr_m >= 0.5)
    n_pr = int(promo_mask.sum())
    if n_pr >= 5:
        raw_pr_lift = float(np.mean(y_m[promo_mask] / (base_level * pay_factor[promo_mask]))) - 1.0
        promo_lift = float(np.clip((n_pr * raw_pr_lift + 10.0 * 0.12) / (n_pr + 10.0), 0.0, 0.30))
    else:
        promo_lift = 0.12

    promo_factor = np.where(pr_m >= 0.5, 1.0 + promo_lift, 1.0)

    # Empirical past promo probabilities (for unbiased expectation during unscheduled future days)
    p_promo_fest = float(np.mean(pr_m[fwin_m >= 0.5])) if np.any(fwin_m >= 0.5) else 0.0
    p_promo_norm = float(np.mean(pr_m[fwin_m < 0.5])) if np.any(fwin_m < 0.5) else 0.0

    # Closed-form ridge regression for multiplicative festival sensitivity:
    # y / (base_level * pay_factor * promo_factor) - 1 ≈ fest_sens * fest_w
    expected_no_fest = np.maximum(base_level * pay_factor * promo_factor, 0.10)
    excess = np.clip((y_m / expected_no_fest) - 1.0, -1.0, 4.0)
    num = float(np.sum(fw_m * excess))
    den = float(np.sum(fw_m * fw_m))
    fest_sens = float(np.clip((num + 2.0 * 0.50) / (den + 2.0), 0.0, 2.2))

    return {
        "fest_sens": fest_sens,
        "payday_hi": payday_hi,
        "payday_lo": payday_lo,
        "promo_lift": promo_lift,
        "p_promo_fest": p_promo_fest,
        "p_promo_norm": p_promo_norm,
    }


def _stl_or_ets_base_forecast(base_series: np.ndarray, horizon_days: int) -> np.ndarray:
    """Decompose deseasonalized series via smooth statsmodels STL (with ETS/mean fallback)."""
    n = len(base_series)
    if n < 21:
        lvl = float(np.mean(base_series)) if n > 0 else 0.0
        return np.full(horizon_days, max(0.0, lvl), dtype=float)

    # Use up to 365 days with smooth seasonal & trend Loess spans so weekly noise is filtered out
    tail = np.asarray(base_series[-min(n, 365) :], dtype=float)
    if float(np.std(tail)) < 1e-6:
        return np.full(horizon_days, max(0.0, float(np.mean(tail))), dtype=float)

    try:
        zero_rate = float(np.mean(tail <= 0.0))
        # On sparse/intermittent series, robust=True treats non-zero sales as outliers and
        # collapses the trend to 0; use non-robust damped mean/ETS on sparse series instead.
        if zero_rate >= 0.25:
            lvl = float(np.mean(tail[-90:])) if len(tail) >= 90 else float(np.mean(tail))
            return np.full(horizon_days, max(0.0, lvl), dtype=float)

        # Smooth seasonal span (odd integer >= 29) prevents overfitting recent Poisson spikes
        seas_span = min(55, max(15, (len(tail) // 7) | 1))
        if seas_span % 2 == 0:
            seas_span += 1
        trend_span = min(61, max(15, (len(tail) // 5) | 1))
        if trend_span % 2 == 0:
            trend_span += 1
        stl_res = STL(tail, period=7, seasonal=seas_span, trend=trend_span, robust=True).fit()
        trend = np.asarray(stl_res.trend, dtype=float)
        seasonal = np.asarray(stl_res.seasonal, dtype=float)

        # Robust level from last 56 days of smooth trend + gentle 180d damped slope
        level = float(np.mean(trend[-min(len(trend), 56) :]))
        if len(trend) >= 180:
            slope = float((np.mean(trend[-56:]) - np.mean(trend[-180:-124])) / 124.0)
            slope = float(np.clip(slope, -0.0015 * max(level, 1.0), 0.0015 * max(level, 1.0)))
        else:
            slope = 0.0

        # Average last 8 cycles of seasonal component for variance-stabilized weekday shape
        n_cycles = min(8, len(seasonal) // 7)
        cyc = seasonal[-(n_cycles * 7) :].reshape(n_cycles, 7)
        mean_cycle = np.mean(cyc, axis=0)
        mean_cycle = mean_cycle - float(np.mean(mean_cycle))

        preds = np.empty(horizon_days, dtype=float)
        for h in range(horizon_days):
            damped_step = sum(0.90**k for k in range(1, h + 2))
            seas_val = float(mean_cycle[h % 7])
            preds[h] = max(0.0, level + slope * damped_step + seas_val)
        return preds
    except Exception:
        try:
            ets = ExponentialSmoothing(
                tail,
                trend="add",
                damped_trend=True,
                seasonal="add",
                seasonal_periods=7,
            ).fit(optimized=True)
            return np.maximum(0.0, np.asarray(ets.forecast(horizon_days), dtype=float))
        except Exception:
            lvl = float(np.mean(tail[-56:]))
            return np.full(horizon_days, max(0.0, lvl), dtype=float)


def predict_stl_calendar_batch(
    sales_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    skus_df: pd.DataFrame,
    as_of_date: date,
    horizon_days: int = 7,
) -> pd.DataFrame:
    """Baseline 2: Smooth Statsmodels STL/ETS + Indian Festival & Payday Calendar Decomposition."""
    hist, cal, skus_norm = _prepare_history_and_calendar(sales_df, cal_df, skus_df, as_of_date)
    horizon_dates = [
        pd.Timestamp(d) for d in build_horizon_calendar_dates(as_of_date, horizon_days)
    ]

    cal_lookup = cal.set_index("sold_date")[
        ["festival_pre_surge_weight", "is_payday_window", "is_festival_window"]
    ].to_dict(orient="index")

    grouped = {str(k): v for k, v in hist.groupby("sku_id", sort=False)} if not hist.empty else {}
    records: list[dict[str, Any]] = []

    for sku_id in skus_norm["sku_id"].astype(str):
        grp = grouped.get(sku_id)
        if grp is None or grp.empty:
            for dt in horizon_dates:
                records.append(
                    {
                        "sku_id": sku_id,
                        "sold_date": dt,
                        "pred_stl": 0.0,
                        "stl_base": 0.0,
                        "stl_festival": 0.0,
                    }
                )
            continue

        g = grp.sort_values("sold_date").reset_index(drop=True)
        dates = g["sold_date"].tolist()
        fest_w = np.array(
            [float(cal_lookup.get(d, {}).get("festival_pre_surge_weight", 0.0)) for d in dates],
            dtype=float,
        )
        payday = np.array(
            [float(cal_lookup.get(d, {}).get("is_payday_window", 0.0)) for d in dates],
            dtype=float,
        )
        fest_win = np.array(
            [float(cal_lookup.get(d, {}).get("is_festival_window", 0.0)) for d in dates],
            dtype=float,
        )
        promo = g["promo_flag"].astype(float).to_numpy(dtype=float)
        unc_mask = ~g["stockout_flag"].astype(bool).to_numpy()
        y_imp = g["demand_imputed"].to_numpy(dtype=float)

        params = _fit_multiplicative_calendar_params(
            y_imp, fest_w, payday, fest_win, promo, unc_mask
        )

        # Deseasonalize multiplicative festival, payday, and promo shocks before STL
        pay_hist = np.where(payday >= 0.5, params["payday_hi"], params["payday_lo"])
        fest_hist = 1.0 + params["fest_sens"] * fest_w
        promo_hist = np.where(promo >= 0.5, 1.0 + params["promo_lift"], 1.0)
        cal_mult_hist = np.maximum(pay_hist * fest_hist * promo_hist, 0.35)

        base_series = y_imp / cal_mult_hist
        base_fc = _stl_or_ets_base_forecast(base_series, horizon_days)

        for idx, dt in enumerate(horizon_dates):
            c_info = cal_lookup.get(dt, {})
            fw = float(c_info.get("festival_pre_surge_weight", 0.0))
            pd_flag = float(
                c_info.get("is_payday_window", 1.0 if (dt.day <= 5 or dt.day >= 25) else 0.0)
            )
            fwin_flag = float(c_info.get("is_festival_window", 1.0 if fw > 0.05 else 0.0))

            base_day = float(base_fc[idx])
            payday_mult = params["payday_hi"] if pd_flag >= 0.5 else params["payday_lo"]
            exp_promo_rate = params["p_promo_fest"] if fwin_flag >= 0.5 else params["p_promo_norm"]
            promo_mult = 1.0 + exp_promo_rate * params["promo_lift"]
            fest_mult = 1.0 + params["fest_sens"] * fw

            base_with_payday = base_day * payday_mult * promo_mult
            total_pred = max(0.0, base_with_payday * fest_mult)
            fest_delta = max(0.0, total_pred - base_with_payday)
            records.append(
                {
                    "sku_id": sku_id,
                    "sold_date": dt,
                    "pred_stl": total_pred,
                    "stl_base": base_with_payday,
                    "stl_festival": fest_delta,
                }
            )

    return pd.DataFrame(records)


def croston_sba_series_forecast(
    y: np.ndarray, alpha: float = 0.15, horizon_days: int = 7
) -> np.ndarray:
    """Syntetos-Boylan Approximation (Croston-SBA) for intermittent/lumpy demand series."""
    vals = np.asarray(y, dtype=float)
    nz_indices = np.flatnonzero(vals > 0.0)
    if len(nz_indices) == 0:
        return np.zeros(horizon_days, dtype=float)
    if len(nz_indices) == 1:
        rate = float(vals[nz_indices[0]]) / max(len(vals), 1)
        return np.full(horizon_days, max(0.0, rate), dtype=float)

    z = float(vals[nz_indices[0]])
    p = float(max(1, nz_indices[0] + 1))
    last_idx = int(nz_indices[0])

    for idx in nz_indices[1:]:
        interval = float(int(idx) - last_idx)
        z = alpha * float(vals[int(idx)]) + (1.0 - alpha) * z
        p = alpha * interval + (1.0 - alpha) * p
        last_idx = int(idx)

    daily_rate = max(0.0, (1.0 - alpha / 2.0) * (z / max(p, 1.0)))
    return np.full(horizon_days, daily_rate, dtype=float)


def predict_croston_sba_batch(
    sales_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    skus_df: pd.DataFrame,
    as_of_date: date,
    horizon_days: int = 7,
) -> pd.DataFrame:
    """Batch Croston-SBA forecast modulated by learned calendar festival lift."""
    hist, cal, skus_norm = _prepare_history_and_calendar(sales_df, cal_df, skus_df, as_of_date)
    horizon_dates = [
        pd.Timestamp(d) for d in build_horizon_calendar_dates(as_of_date, horizon_days)
    ]

    cal_lookup = cal.set_index("sold_date")[
        ["festival_pre_surge_weight", "is_payday_window", "is_festival_window"]
    ].to_dict(orient="index")

    grouped = {str(k): v for k, v in hist.groupby("sku_id", sort=False)} if not hist.empty else {}
    records: list[dict[str, Any]] = []

    for sku_id in skus_norm["sku_id"].astype(str):
        grp = grouped.get(sku_id)
        if grp is None or grp.empty:
            for dt in horizon_dates:
                records.append({"sku_id": sku_id, "sold_date": dt, "pred_croston": 0.0})
            continue

        g = grp.sort_values("sold_date").reset_index(drop=True)
        dates = g["sold_date"].tolist()
        fest_w = np.array(
            [float(cal_lookup.get(d, {}).get("festival_pre_surge_weight", 0.0)) for d in dates],
            dtype=float,
        )
        payday = np.array(
            [float(cal_lookup.get(d, {}).get("is_payday_window", 0.0)) for d in dates],
            dtype=float,
        )
        fest_win = np.array(
            [float(cal_lookup.get(d, {}).get("is_festival_window", 0.0)) for d in dates],
            dtype=float,
        )
        promo = g["promo_flag"].astype(float).to_numpy(dtype=float)
        unc_mask = ~g["stockout_flag"].astype(bool).to_numpy()
        y_imp = g["demand_imputed"].to_numpy(dtype=float)

        params = _fit_multiplicative_calendar_params(
            y_imp, fest_w, payday, fest_win, promo, unc_mask
        )
        base_fc = croston_sba_series_forecast(y_imp, alpha=0.12, horizon_days=horizon_days)

        for idx, dt in enumerate(horizon_dates):
            fw = float(cal_lookup.get(dt, {}).get("festival_pre_surge_weight", 0.0))
            pred = max(0.0, float(base_fc[idx]) * (1.0 + params["fest_sens"] * fw))
            records.append({"sku_id": sku_id, "sold_date": dt, "pred_croston": pred})

    return pd.DataFrame(records)
