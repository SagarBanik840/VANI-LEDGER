"""Global LightGBM + Syntetos-Boylan Routing + 5-Stage Prediction Amplification Pipeline.

Implements:
- Global LightGBM across all SKUs: Tweedie (variance_power=1.25) + Quantile (alpha=0.50, 0.90)
- Exact Law-of-Total-Expectation promo marginalization from strictly past promo frequencies
- Syntetos-Boylan ADI/CV^2 regime routing (smooth/erratic/intermittent/lumpy/cold-start)
- Stage 1: Inverse-WAPE weighted ensemble (weights clamped [0.10, 0.80]) + champion-challenger gate
- Stage 2: Per-category bias correction (kept ONLY if it strictly lowers validation WAPE)
- Stage 3: Bottom-up hierarchical reconciliation (SKU -> Category coherence)
- Stage 4: Split Conformal intervals & empirical residuals (delegated to conformal.py)
- Stage 5: Bounded per-SKU human override feedback adjustment reading committed overrides

Pure Python / NumPy / pandas / LightGBM only — zero LLM or HTTP imports.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from vani_ledger.forecast_core.baselines import (
    predict_croston_sba_batch,
    predict_seasonal_naive_batch,
    predict_stl_calendar_batch,
)
from vani_ledger.forecast_core.conformal import calibrate_sku_residuals, conformal_interval
from vani_ledger.forecast_core.features import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    build_dense_sales_grid,
    build_features,
    build_horizon_calendar_dates,
    compute_feature_hash,
    compute_sku_regime_stats,
    normalize_skus_df,
)

MODEL_VERSION = "lgbm-conformal-v1.0"


def compute_wape(
    actuals: np.ndarray, preds: np.ndarray, weights: np.ndarray | None = None
) -> float:
    """Weighted Absolute Percentage Error (optionally revenue-weighted)."""
    y = np.asarray(actuals, dtype=float)
    yhat = np.asarray(preds, dtype=float)
    if weights is not None:
        w = np.asarray(weights, dtype=float)
        num = float(np.sum(np.abs(y - yhat) * w))
        den = float(np.sum(np.abs(y) * w))
    else:
        num = float(np.sum(np.abs(y - yhat)))
        den = float(np.sum(np.abs(y)))
    if den <= 1e-9:
        return 0.0 if num <= 1e-9 else 1.0
    return num / den


def compute_pinball_loss(actuals: np.ndarray, preds: np.ndarray, alpha: float) -> float:
    """Quantile pinball loss at level `alpha` in `(0, 1)`."""
    y = np.asarray(actuals, dtype=float)
    yhat = np.asarray(preds, dtype=float)
    diff = y - yhat
    loss = np.maximum(alpha * diff, (alpha - 1.0) * diff)
    return float(np.mean(loss)) if len(loss) > 0 else 0.0


def compute_bias(actuals: np.ndarray, preds: np.ndarray) -> float:
    """Signed relative forecast bias: `sum(pred - actual) / sum(actual)`."""
    y = np.asarray(actuals, dtype=float)
    yhat = np.asarray(preds, dtype=float)
    den = float(np.sum(np.abs(y)))
    if den <= 1e-9:
        return 0.0
    return float(np.sum(yhat - y) / den)


@dataclass
class GlobalLightGBMModel:
    """Pooled Global LightGBM suite: Tweedie mean + Quantile p50 + Quantile p90."""

    tweedie_model: lgb.LGBMRegressor
    q50_model: lgb.LGBMRegressor
    q90_model: lgb.LGBMRegressor
    feature_columns: list[str] = field(default_factory=lambda: list(FEATURE_COLUMNS))
    categorical_features: list[str] = field(default_factory=lambda: list(CATEGORICAL_FEATURES))

    @classmethod
    def fit(
        cls,
        train_feat_df: pd.DataFrame,
        *,
        seed: int = 42,
        n_estimators: int = 260,
        learning_rate: float = 0.04,
    ) -> GlobalLightGBMModel:
        """Fit Global Tweedie + Quantile p50/p90 LightGBM on strictly uncensored train rows."""
        df = train_feat_df.copy()
        if "is_censored" in df.columns:
            df = df[~df["is_censored"].astype(bool)].copy()
        if len(df) > 1000:
            min_date = df["sold_date"].min()
            df = df[df["sold_date"] >= min_date + pd.Timedelta(days=14)].copy()

        x_train = df[FEATURE_COLUMNS].copy()
        for cat_col in CATEGORICAL_FEATURES:
            x_train[cat_col] = x_train[cat_col].astype(int)
        y_train = np.maximum(df["qty"].to_numpy(dtype=float), 0.0)

        common_params: dict[str, Any] = {
            "n_estimators": n_estimators,
            "learning_rate": learning_rate,
            "max_depth": 5,
            "num_leaves": 23,
            "min_child_samples": 35,
            "subsample": 0.80,
            "subsample_freq": 1,
            "colsample_bytree": 0.80,
            "reg_alpha": 0.4,
            "reg_lambda": 4.0,
            "random_state": seed,
            "n_jobs": -1,
            "verbose": -1,
        }

        tweedie = lgb.LGBMRegressor(
            objective="tweedie",
            tweedie_variance_power=1.25,
            **common_params,
        )
        tweedie.fit(x_train, y_train, categorical_feature=CATEGORICAL_FEATURES)

        q50 = lgb.LGBMRegressor(
            objective="quantile",
            alpha=0.50,
            **common_params,
        )
        q50.fit(x_train, y_train, categorical_feature=CATEGORICAL_FEATURES)

        q90 = lgb.LGBMRegressor(
            objective="quantile",
            alpha=0.90,
            **common_params,
        )
        q90.fit(x_train, y_train, categorical_feature=CATEGORICAL_FEATURES)

        return cls(tweedie_model=tweedie, q50_model=q50, q90_model=q90)

    def predict_frame(self, feat_df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Predict `(tweedie_mean, q50, q90)` on a pre-built feature DataFrame."""
        x_eval = feat_df[self.feature_columns].copy()
        for cat_col in self.categorical_features:
            x_eval[cat_col] = x_eval[cat_col].astype(int)

        pred_mean = np.maximum(0.0, np.asarray(self.tweedie_model.predict(x_eval), dtype=float))
        pred_q50 = np.maximum(0.0, np.asarray(self.q50_model.predict(x_eval), dtype=float))
        pred_q90 = np.maximum(
            pred_q50,
            np.asarray(self.q90_model.predict(x_eval), dtype=float),
        )
        return pred_mean, pred_q50, pred_q90


def predict_lgbm_horizon(
    model: GlobalLightGBMModel,
    sales_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    skus_df: pd.DataFrame,
    as_of_date: date,
    horizon_days: int = 7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Recursive multi-step horizon forecast reusing the exact shared `build_features()`.

    Marginalizes over empirical past promo frequency (`E[Y] = (1-p)*f(promo=0) + p*f(promo=1)`)
    computed strictly on `sold_date <= as_of_date` so future festival forecasts stay unbiased.

    Returns `(daily_predictions_df, horizon_features_df)`.
    """
    skus_norm = normalize_skus_df(skus_df)
    dense = build_dense_sales_grid(sales_df, skus_norm)
    cutoff = pd.Timestamp(as_of_date)
    hist = dense[dense["sold_date"] <= cutoff].copy()

    # Keep last 95 days per SKU so base_roll_mean_90 & lags stay exact while running fast
    min_tail = cutoff - pd.Timedelta(days=95)
    working = hist[hist["sold_date"] >= min_tail].copy()

    # Anchor full-history causal states strictly from <= as_of_date
    full_hist_feat = build_features(hist, cal_df, skus_norm, fill_dense=False)
    last_hist_state = (
        full_hist_feat.sort_values(["sku_id", "sold_date"])
        .groupby("sku_id", as_index=False)
        .last()[["sku_id", "base_roll_mean_90", "past_fest_sens", "adi", "cv2"]]
    )
    base90_map = dict(
        zip(last_hist_state["sku_id"], last_hist_state["base_roll_mean_90"], strict=False)
    )
    sens_map = dict(
        zip(last_hist_state["sku_id"], last_hist_state["past_fest_sens"], strict=False)
    )
    adi_map = dict(zip(last_hist_state["sku_id"], last_hist_state["adi"], strict=False))
    cv2_map = dict(zip(last_hist_state["sku_id"], last_hist_state["cv2"], strict=False))

    # Empirical past promo probabilities strictly <= as_of_date (festival vs non-festival)
    fest_hist = full_hist_feat[full_hist_feat["is_festival_window"] >= 0.5]
    norm_hist = full_hist_feat[full_hist_feat["is_festival_window"] < 0.5]
    p_promo_fest = float(fest_hist["promo_flag"].mean()) if len(fest_hist) > 0 else 0.0
    p_promo_norm = float(norm_hist["promo_flag"].mean()) if len(norm_hist) > 0 else 0.0

    price_map = dict(zip(skus_norm["sku_id"], skus_norm["unit_price"], strict=False))
    all_sku_ids = skus_norm["sku_id"].astype(str).tolist()
    horizon_dates = [
        pd.Timestamp(d) for d in build_horizon_calendar_dates(as_of_date, horizon_days)
    ]

    pred_rows: list[dict[str, Any]] = []
    feat_frames: list[pd.DataFrame] = []

    for dt in horizon_dates:
        step_stub = pd.DataFrame(
            {
                "sku_id": all_sku_ids,
                "sold_date": [dt] * len(all_sku_ids),
                "qty": [0.0] * len(all_sku_ids),
                "unit_price": [float(price_map.get(s, 100.0)) for s in all_sku_ids],
                "promo_flag": [False] * len(all_sku_ids),
                "stockout_flag": [False] * len(all_sku_ids),
            }
        )
        combined = pd.concat([working, step_stub], ignore_index=True)
        feat_all = build_features(combined, cal_df, skus_norm, fill_dense=False)
        step_feat = feat_all[feat_all["sold_date"] == dt].copy().reset_index(drop=True)

        # Keep full-history causal anchors consistent across recursive steps
        step_feat["base_roll_mean_90"] = (
            step_feat["sku_id"].map(base90_map).fillna(step_feat["base_roll_mean_90"]).astype(float)
        )
        step_feat["past_fest_sens"] = (
            step_feat["sku_id"].map(sens_map).fillna(0.50).astype(float)
        )
        step_feat["adi"] = step_feat["sku_id"].map(adi_map).fillna(1.0).astype(float)
        step_feat["cv2"] = step_feat["sku_id"].map(cv2_map).fillna(0.20).astype(float)
        step_feat["festival_demand_signal"] = (
            step_feat["base_roll_mean_90"]
            * step_feat["festival_pre_surge_weight"]
            * step_feat["past_fest_sens"]
        )

        # Exact Law-of-Total-Expectation marginalization over unscheduled future promos
        step_feat["promo_flag"] = 0.0
        mean_0, q50_0, q90_0 = model.predict_frame(step_feat)

        promo_weights = np.where(
            step_feat["is_festival_window"].to_numpy(dtype=float) >= 0.5,
            p_promo_fest,
            p_promo_norm,
        )
        if float(np.max(promo_weights)) > 1e-4:
            step_feat_p1 = step_feat.copy()
            step_feat_p1["promo_flag"] = 1.0
            mean_1, q50_1, q90_1 = model.predict_frame(step_feat_p1)
            pred_mean = (1.0 - promo_weights) * mean_0 + promo_weights * mean_1
            pred_q50 = (1.0 - promo_weights) * q50_0 + promo_weights * q50_1
            pred_q90 = (1.0 - promo_weights) * q90_0 + promo_weights * q90_1
        else:
            pred_mean, pred_q50, pred_q90 = mean_0, q50_0, q90_0

        feat_frames.append(step_feat)

        next_rows: list[dict[str, Any]] = []
        for idx, row in step_feat.iterrows():
            sid = str(row["sku_id"])
            p_mean = float(pred_mean[idx])
            p_q50 = float(pred_q50[idx])
            p_q90 = float(pred_q90[idx])
            pred_rows.append(
                {
                    "sku_id": sid,
                    "sold_date": dt,
                    "pred_lgbm": p_mean,
                    "pred_lgbm_q50": p_q50,
                    "pred_lgbm_q90": p_q90,
                }
            )
            next_rows.append(
                {
                    "sku_id": sid,
                    "sold_date": dt,
                    "qty": p_mean,
                    "unit_price": float(price_map.get(sid, 100.0)),
                    "promo_flag": False,
                    "stockout_flag": False,
                }
            )
        working = pd.concat([working, pd.DataFrame(next_rows)], ignore_index=True)

    return pd.DataFrame(pred_rows), pd.concat(feat_frames, ignore_index=True)


def compute_inverse_wape_weights(
    wapes: dict[str, float],
    *,
    min_weight: float = 0.10,
    max_weight: float = 0.80,
    power: float = 2.0,
) -> dict[str, float]:
    """Stage 1: Inverse-WAPE weights clamped to `[min_weight, max_weight]` and normalized."""
    inv = {name: 1.0 / (max(float(val), 0.015) ** power) for name, val in wapes.items()}
    total = sum(inv.values())
    if total <= 0.0:
        n = max(len(wapes), 1)
        return {k: 1.0 / n for k in wapes}

    raw = {k: v / total for k, v in inv.items()}
    weights = dict(raw)
    for _ in range(12):
        clamped = {k: float(np.clip(v, min_weight, max_weight)) for k, v in weights.items()}
        s = sum(clamped.values())
        if abs(s - 1.0) < 1e-9:
            weights = clamped
            break
        if s > 1.0:
            free = [k for k, v in clamped.items() if v > min_weight + 1e-9]
            if not free:
                weights = {k: v / s for k, v in clamped.items()}
                break
            excess = s - 1.0
            free_sum = sum(clamped[k] - min_weight for k in free)
            for k in free:
                clamped[k] -= excess * ((clamped[k] - min_weight) / max(free_sum, 1e-9))
        else:
            free = [k for k, v in clamped.items() if v < max_weight - 1e-9]
            if not free:
                weights = {k: v / s for k, v in clamped.items()}
                break
            deficit = 1.0 - s
            free_sum = sum(max_weight - clamped[k] for k in free)
            for k in free:
                clamped[k] += deficit * ((max_weight - clamped[k]) / max(free_sum, 1e-9))
        weights = clamped

    norm = sum(weights.values())
    return {k: round(v / max(norm, 1e-9), 4) for k, v in weights.items()}


def compute_override_feedback_adjustment(
    committed_overrides: Sequence[dict[str, Any] | Any],
    sku_id: str,
    *,
    max_adj_pct: float = 0.15,
    decay: float = 0.70,
) -> float:
    """Stage 5: Bounded per-SKU override feedback multiplier from committed seller overrides.

    Reads ONLY committed overrides for `sku_id`, computes exponentially recency-weighted
    relative adjustment `(manual_qty - suggested_qty) / suggested_qty`, applies empirical
    Bayes shrinkage `n / (n + 3)`, and strictly clamps within `[1 - max_adj_pct, 1 + max_adj_pct]`.
    Returns `1.0` when no committed overrides exist.
    """
    relevant: list[tuple[int, int]] = []
    target_id = str(sku_id)
    for ov in committed_overrides:
        ov_sku = str(ov["sku_id"] if isinstance(ov, dict) else getattr(ov, "sku_id", ""))
        if ov_sku != target_id:
            continue
        sug = int(ov["suggested_qty"] if isinstance(ov, dict) else getattr(ov, "suggested_qty", 0))
        man = int(ov["manual_qty"] if isinstance(ov, dict) else getattr(ov, "manual_qty", 0))
        if sug > 0:
            relevant.append((sug, man))

    if not relevant:
        return 1.0

    weighted_sum = 0.0
    weight_total = 0.0
    for age, (sug, man) in enumerate(reversed(relevant)):
        rel_delta = (man - sug) / max(sug, 1)
        rel_delta = float(np.clip(rel_delta, -0.50, 0.50))
        w = decay**age
        weighted_sum += w * rel_delta
        weight_total += w

    raw_delta = weighted_sum / max(weight_total, 1e-9)
    shrunk_delta = raw_delta * (len(relevant) / (len(relevant) + 3.0))
    clamped_delta = float(np.clip(shrunk_delta, -max_adj_pct, max_adj_pct))
    return round(1.0 + clamped_delta, 4)


@dataclass
class AmplifiedForecastPipeline:
    """Full 5-Stage Prediction Amplification Pipeline."""

    lgbm_model: GlobalLightGBMModel
    segment_weights: dict[str, dict[str, float]]
    segment_champions: dict[str, str]
    intermittent_winner: str
    category_bias_factors: dict[str, float]
    reconciliation_alpha: float
    val_residuals_df: pd.DataFrame
    model_version: str = MODEL_VERSION
    _last_daily_preds: pd.DataFrame | None = field(default=None, repr=False)

    @classmethod
    def fit(
        cls,
        sales_df: pd.DataFrame,
        cal_df: pd.DataFrame,
        skus_df: pd.DataFrame,
        *,
        train_end_date: date,
        val_end_date: date,
        horizon_days: int = 7,
        seed: int = 42,
    ) -> AmplifiedForecastPipeline:
        """Fit LightGBM on `<= train_end_date`; calibrate Stages 1-4 on `(train_end, val_end]`."""
        skus_norm = normalize_skus_df(skus_df)
        dense = build_dense_sales_grid(sales_df, skus_norm)
        train_cutoff = pd.Timestamp(train_end_date)

        # 1. Fit Global LightGBM strictly on train window (<= train_end_date)
        train_sales = dense[dense["sold_date"] <= train_cutoff].copy()
        train_feat = build_features(train_sales, cal_df, skus_norm, fill_dense=False)
        lgbm_model = GlobalLightGBMModel.fit(train_feat, seed=seed)

        # 2. Evaluate rolling horizon origins strictly inside (train_end_date, val_end_date]
        val_origins: list[date] = []
        val_span_days = (val_end_date - train_end_date).days
        step_days = 7 if val_span_days >= horizon_days + 7 else max(1, horizon_days // 2)
        cur_origin = train_end_date
        while cur_origin + timedelta(days=horizon_days) <= val_end_date:
            val_origins.append(cur_origin)
            cur_origin += timedelta(days=step_days)
        if not val_origins:
            val_origins = [train_end_date]

        sku_seg_map = dict(zip(skus_norm["sku_id"], skus_norm["abc_segment"], strict=False))
        sku_cat_map = dict(zip(skus_norm["sku_id"], skus_norm["category"], strict=False))
        sku_price_map = dict(zip(skus_norm["sku_id"], skus_norm["unit_price"], strict=False))

        val_horizon_frames: list[pd.DataFrame] = []
        for origin in val_origins:
            hist_slice = dense[dense["sold_date"] <= pd.Timestamp(origin)].copy()
            lgb_daily, _ = predict_lgbm_horizon(
                lgbm_model, hist_slice, cal_df, skus_norm, origin, horizon_days
            )
            stl_daily = predict_stl_calendar_batch(
                hist_slice, cal_df, skus_norm, origin, horizon_days
            )
            naive_daily = predict_seasonal_naive_batch(
                hist_slice, cal_df, skus_norm, origin, horizon_days
            )
            croston_daily = predict_croston_sba_batch(
                hist_slice, cal_df, skus_norm, origin, horizon_days
            )

            merged = (
                lgb_daily.merge(
                    stl_daily[["sku_id", "sold_date", "pred_stl"]], on=["sku_id", "sold_date"]
                )
                .merge(naive_daily, on=["sku_id", "sold_date"])
                .merge(croston_daily, on=["sku_id", "sold_date"])
            )

            actual_slice = dense[
                (dense["sold_date"] > pd.Timestamp(origin))
                & (dense["sold_date"] <= pd.Timestamp(origin + timedelta(days=horizon_days)))
            ][["sku_id", "sold_date", "qty", "stockout_flag"]].copy()
            merged = merged.merge(actual_slice, on=["sku_id", "sold_date"], how="inner")

            agg = merged.groupby("sku_id", as_index=False).agg(
                actual_total=("qty", "sum"),
                any_stockout=("stockout_flag", "max"),
                pred_lgbm=("pred_lgbm", "sum"),
                pred_lgbm_q50=("pred_lgbm_q50", "sum"),
                pred_lgbm_q90=("pred_lgbm_q90", "sum"),
                pred_stl=("pred_stl", "sum"),
                pred_naive=("pred_naive", "sum"),
                pred_croston=("pred_croston", "sum"),
            )
            agg["origin_date"] = origin
            agg["abc_segment"] = agg["sku_id"].map(sku_seg_map).fillna("B")
            agg["category"] = agg["sku_id"].map(sku_cat_map).fillna("Staples")
            agg["unit_price"] = agg["sku_id"].map(sku_price_map).fillna(100.0)
            val_horizon_frames.append(agg)

        val_df = pd.concat(val_horizon_frames, ignore_index=True)
        val_clean = val_df[~val_df["any_stockout"].astype(bool)].copy()
        if len(val_clean) < 20:
            val_clean = val_df.copy()

        # Stage 1: Inverse-WAPE weights per segment + champion-challenger gate
        regime_stats = compute_sku_regime_stats(train_sales, train_end_date)
        val_clean["regime"] = val_clean["sku_id"].map(
            lambda s: regime_stats.get(str(s), {}).get("regime", "smooth")
        )

        interm = val_clean[val_clean["regime"].isin(["intermittent", "lumpy"])]
        if not interm.empty:
            w_lgb_i = compute_wape(
                interm["actual_total"].to_numpy(), interm["pred_lgbm"].to_numpy()
            )
            w_cro_i = compute_wape(
                interm["actual_total"].to_numpy(), interm["pred_croston"].to_numpy()
            )
            intermittent_winner = "lgbm_tweedie" if w_lgb_i <= w_cro_i else "croston_sba"
        else:
            intermittent_winner = "lgbm_tweedie"

        segment_weights: dict[str, dict[str, float]] = {}
        segment_champions: dict[str, str] = {}
        for seg in ("A", "B", "C"):
            sub = val_clean[val_clean["abc_segment"] == seg]
            if sub.empty:
                segment_weights[seg] = {"lgbm": 0.45, "stl": 0.45, "naive": 0.10}
                segment_champions[seg] = "amplified_ensemble"
                continue

            y_s = sub["actual_total"].to_numpy(dtype=float)
            rev_w = sub["unit_price"].to_numpy(dtype=float)
            p_lgb = sub["pred_lgbm"].to_numpy(dtype=float)
            p_stl = sub["pred_stl"].to_numpy(dtype=float)
            p_naive = sub["pred_naive"].to_numpy(dtype=float)

            w_lgb = compute_wape(y_s, p_lgb, rev_w)
            w_stl = compute_wape(y_s, p_stl, rev_w)
            w_naive = compute_wape(y_s, p_naive, rev_w)

            # Inverse-WAPE^2 starting weights clamped to [0.10, 0.80]
            inv_weights = compute_inverse_wape_weights(
                {"lgbm": w_lgb, "stl": w_stl, "naive": w_naive},
                min_weight=0.10,
                max_weight=0.80,
                power=2.0,
            )
            # Refine on validation simplex within [0.10, 0.80] preferring calendar-aware models
            best_w = inv_weights
            best_ens_wape = compute_wape(
                y_s,
                inv_weights["lgbm"] * p_lgb
                + inv_weights["stl"] * p_stl
                + inv_weights["naive"] * p_naive,
                rev_w,
            )
            for wl in (0.10, 0.25, 0.40, 0.50, 0.60, 0.70, 0.80):
                for ws in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80):
                    wn = round(1.0 - wl - ws, 4)
                    if wn < 0.10 - 1e-9 or wn > 0.15 + 1e-9:
                        continue
                    cand_p = wl * p_lgb + ws * p_stl + wn * p_naive
                    cand_wape = compute_wape(y_s, cand_p, rev_w)
                    if cand_wape < best_ens_wape - 1e-4:
                        best_ens_wape = cand_wape
                        best_w = {"lgbm": round(wl, 4), "stl": round(ws, 4), "naive": round(wn, 4)}

            best_stat_name = "stl_calendar" if w_stl <= w_naive else "seasonal_naive"
            best_stat_wape = min(w_stl, w_naive)
            if best_ens_wape <= best_stat_wape + 0.004:
                segment_weights[seg] = best_w
                segment_champions[seg] = "amplified_ensemble"
            else:
                segment_weights[seg] = (
                    {"lgbm": 0.10, "stl": 0.80, "naive": 0.10}
                    if best_stat_name == "stl_calendar"
                    else {"lgbm": 0.10, "stl": 0.10, "naive": 0.80}
                )
                segment_champions[seg] = best_stat_name

        val_clean["pred_stage1"] = [
            _combine_stage1_row(row, segment_weights, segment_champions, intermittent_winner)
            for _, row in val_clean.iterrows()
        ]

        # Stage 2: Per-category bias correction (ONLY if meaningful bias & strictly improves WAPE)
        category_bias_factors: dict[str, float] = {}
        for cat, cgrp in val_clean.groupby("category"):
            y_c = cgrp["actual_total"].to_numpy(dtype=float)
            p_c = cgrp["pred_stage1"].to_numpy(dtype=float)
            rev_c = cgrp["unit_price"].to_numpy(dtype=float)
            raw_wape = compute_wape(y_c, p_c, rev_c)
            sum_p = float(np.sum(p_c))
            if sum_p > 1e-6 and len(cgrp) >= 12:
                raw_factor = float(np.clip(np.sum(y_c) / sum_p, 0.92, 1.08))
                candidate = 1.0 + 0.50 * (raw_factor - 1.0)
                corr_wape = compute_wape(y_c, p_c * candidate, rev_c)
                if abs(raw_factor - 1.0) >= 0.03 and corr_wape < raw_wape - 0.005:
                    category_bias_factors[str(cat)] = round(candidate, 4)
                else:
                    category_bias_factors[str(cat)] = 1.0
            else:
                category_bias_factors[str(cat)] = 1.0

        val_clean["pred_stage2"] = val_clean.apply(
            lambda r: float(r["pred_stage1"]) * category_bias_factors.get(str(r["category"]), 1.0),
            axis=1,
        )

        # Stage 3: Bottom-up hierarchical reconciliation (SKU -> Category coherence)
        val_clean["cat_bu"] = val_clean.groupby(["origin_date", "category"])[
            "pred_stage2"
        ].transform("sum")
        val_clean["cat_stl"] = val_clean.groupby(["origin_date", "category"])[
            "pred_stl"
        ].transform("sum")

        best_alpha = 1.0
        best_val_wape = compute_wape(
            val_clean["actual_total"].to_numpy(dtype=float),
            val_clean["pred_stage2"].to_numpy(dtype=float),
            val_clean["unit_price"].to_numpy(dtype=float),
        )
        for alpha in (0.90, 0.80, 0.70):
            cat_target = alpha * val_clean["cat_bu"] + (1.0 - alpha) * val_clean["cat_stl"]
            scale = np.where(val_clean["cat_bu"] > 1e-6, cat_target / val_clean["cat_bu"], 1.0)
            cand = val_clean["pred_stage2"].to_numpy(dtype=float) * scale
            w_cand = compute_wape(
                val_clean["actual_total"].to_numpy(dtype=float),
                cand,
                val_clean["unit_price"].to_numpy(dtype=float),
            )
            if w_cand < best_val_wape - 0.005:
                best_val_wape = w_cand
                best_alpha = alpha

        cat_target_final = (
            best_alpha * val_clean["cat_bu"] + (1.0 - best_alpha) * val_clean["cat_stl"]
        )
        scale_final = np.where(
            val_clean["cat_bu"] > 1e-6, cat_target_final / val_clean["cat_bu"], 1.0
        )
        val_clean["pred_amplified"] = np.maximum(
            0.0, val_clean["pred_stage2"].to_numpy(dtype=float) * scale_final
        )
        val_clean["residual"] = val_clean["actual_total"] - val_clean["pred_amplified"]

        return cls(
            lgbm_model=lgbm_model,
            segment_weights=segment_weights,
            segment_champions=segment_champions,
            intermittent_winner=intermittent_winner,
            category_bias_factors=category_bias_factors,
            reconciliation_alpha=best_alpha,
            val_residuals_df=val_clean[
                [
                    "sku_id",
                    "abc_segment",
                    "category",
                    "origin_date",
                    "actual_total",
                    "pred_amplified",
                    "residual",
                ]
            ].copy(),
        )

    def predict_horizon_all_skus(
        self,
        sales_df: pd.DataFrame,
        cal_df: pd.DataFrame,
        skus_df: pd.DataFrame,
        *,
        as_of_date: date,
        horizon_days: int = 7,
        committed_overrides: Sequence[dict[str, Any] | Any] | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, list[float]]]:
        """Run full 5-Stage Amplified forecast for all SKUs from `as_of_date`."""
        skus_norm = normalize_skus_df(skus_df)
        dense = build_dense_sales_grid(sales_df, skus_norm)
        hist = dense[dense["sold_date"] <= pd.Timestamp(as_of_date)].copy()
        regime_stats = compute_sku_regime_stats(hist, as_of_date)

        lgb_daily, horizon_feat_df = predict_lgbm_horizon(
            self.lgbm_model, hist, cal_df, skus_norm, as_of_date, horizon_days
        )
        stl_daily = predict_stl_calendar_batch(hist, cal_df, skus_norm, as_of_date, horizon_days)
        naive_daily = predict_seasonal_naive_batch(
            hist, cal_df, skus_norm, as_of_date, horizon_days
        )
        croston_daily = predict_croston_sba_batch(hist, cal_df, skus_norm, as_of_date, horizon_days)

        daily = (
            lgb_daily.merge(stl_daily, on=["sku_id", "sold_date"])
            .merge(naive_daily, on=["sku_id", "sold_date"])
            .merge(croston_daily, on=["sku_id", "sold_date"])
        )

        summary = (
            daily.groupby("sku_id", as_index=False)
            .agg(
                pred_lgbm=("pred_lgbm", "sum"),
                pred_lgbm_q50=("pred_lgbm_q50", "sum"),
                pred_lgbm_q90=("pred_lgbm_q90", "sum"),
                pred_stl=("pred_stl", "sum"),
                stl_base=("stl_base", "sum"),
                stl_festival=("stl_festival", "sum"),
                pred_naive=("pred_naive", "sum"),
                pred_croston=("pred_croston", "sum"),
            )
            .merge(
                skus_norm[
                    [
                        "sku_id",
                        "sku_code",
                        "category",
                        "abc_segment",
                        "unit_cost",
                        "unit_price",
                        "lead_time_days",
                        "shelf_life_days",
                        "perishable",
                    ]
                ],
                on="sku_id",
                how="left",
            )
        )

        cat_prior = summary.groupby("category")["pred_stl"].mean().to_dict()

        summary["regime"] = summary["sku_id"].map(
            lambda s: regime_stats.get(str(s), {}).get("regime", "smooth")
        )
        summary["data_sufficiency"] = summary["sku_id"].map(
            lambda s: regime_stats.get(str(s), {}).get("data_sufficiency", "low")
        )
        summary["adi"] = summary["sku_id"].map(
            lambda s: regime_stats.get(str(s), {}).get("adi", 1.0)
        )
        summary["cv2"] = summary["sku_id"].map(
            lambda s: regime_stats.get(str(s), {}).get("cv2", 0.2)
        )

        # Stage 1: Inverse-WAPE weighted ensemble + Syntetos-Boylan routing
        stage1_preds: list[float] = []
        models_used: list[str] = []
        for _, row in summary.iterrows():
            regime = str(row["regime"])
            seg = str(row["abc_segment"])
            if regime == "cold_start":
                stage1_preds.append(float(cat_prior.get(str(row["category"]), 5.0)))
                models_used.append("category_prior")
            else:
                p1 = _combine_stage1_row(
                    row, self.segment_weights, self.segment_champions, self.intermittent_winner
                )
                stage1_preds.append(p1)
                is_sparse_regime = regime in {"intermittent", "lumpy"}
                if is_sparse_regime and self.intermittent_winner == "croston_sba":
                    models_used.append("croston_sba")
                else:
                    models_used.append(self.segment_champions.get(seg, "amplified_ensemble"))

        summary["pred_stage1"] = stage1_preds
        summary["model_used"] = models_used

        # Stage 2: Per-category bias correction
        summary["pred_stage2"] = summary.apply(
            lambda r: float(r["pred_stage1"])
            * self.category_bias_factors.get(str(r["category"]), 1.0),
            axis=1,
        )

        # Stage 3: Bottom-up hierarchical reconciliation (SKU -> Category)
        summary["cat_bu"] = summary.groupby("category")["pred_stage2"].transform("sum")
        summary["cat_stl"] = summary.groupby("category")["pred_stl"].transform("sum")
        cat_target = (
            self.reconciliation_alpha * summary["cat_bu"]
            + (1.0 - self.reconciliation_alpha) * summary["cat_stl"]
        )
        rec_scale = np.where(summary["cat_bu"] > 1e-6, cat_target / summary["cat_bu"], 1.0)
        summary["pred_amplified"] = np.maximum(
            0.0, summary["pred_stage2"].to_numpy(dtype=float) * rec_scale
        )

        # Stage 5: Optional bounded per-SKU feedback from committed overrides
        if committed_overrides:
            adj_factors = [
                compute_override_feedback_adjustment(committed_overrides, str(sid))
                for sid in summary["sku_id"]
            ]
            summary["override_feedback_factor"] = adj_factors
            summary["pred_amplified"] = summary["pred_amplified"].to_numpy(
                dtype=float
            ) * np.asarray(adj_factors, dtype=float)
        else:
            summary["override_feedback_factor"] = 1.0

        # Stage 4: Split Conformal intervals from empirical validation residuals
        target_pred_map = dict(
            zip(
                summary["sku_id"].astype(str),
                summary["pred_amplified"].astype(float),
                strict=False,
            )
        )
        seg_map = dict(
            zip(summary["sku_id"].astype(str), summary["abc_segment"].astype(str), strict=False)
        )
        lt_map = dict(
            zip(summary["sku_id"].astype(str), summary["lead_time_days"].astype(int), strict=False)
        )
        residuals_by_sku = calibrate_sku_residuals(
            val_actuals=self.val_residuals_df["actual_total"].to_numpy(dtype=float),
            val_preds=self.val_residuals_df["pred_amplified"].to_numpy(dtype=float),
            val_sku_ids=self.val_residuals_df["sku_id"].astype(str).tolist(),
            val_segments=self.val_residuals_df["abc_segment"].astype(str).tolist(),
            target_pred_by_sku=target_pred_map,
            segment_by_sku=seg_map,
            lead_time_by_sku=lt_map,
            horizon_days=horizon_days,
        )

        p10_list: list[float] = []
        p50_list: list[float] = []
        p90_list: list[float] = []
        hash_list: list[str] = []
        for _, row in summary.iterrows():
            sid = str(row["sku_id"])
            p50 = round(float(row["pred_amplified"]), 2)
            p10, p90 = conformal_interval(p50, residuals_by_sku.get(sid, []))
            p10_list.append(p10)
            p50_list.append(p50)
            p90_list.append(p90)
            hash_list.append(compute_feature_hash(horizon_feat_df, sid))

        summary["p10_total"] = p10_list
        summary["p50_total"] = p50_list
        summary["p90_total"] = p90_list
        summary["feature_hash"] = hash_list
        summary["model_version"] = self.model_version

        # Attach daily-level amplified predictions proportionally matching horizon totals
        # (using L1/quantile median blend on intermittent C-items for daily MAE optimality)
        seg_lookup = dict(
            zip(summary["sku_id"].astype(str), summary["abc_segment"].astype(str), strict=False)
        )
        scale_lookup = dict(
            zip(
                summary["sku_id"].astype(str),
                summary["pred_amplified"].to_numpy(dtype=float)
                / np.maximum(summary["pred_lgbm"].to_numpy(dtype=float), 1e-6),
                strict=False,
            )
        )
        daily["abc_segment"] = daily["sku_id"].map(seg_lookup).fillna("B")
        daily["pred_amplified"] = np.where(
            daily["abc_segment"] == "C",
            0.40 * daily["pred_lgbm_q50"].to_numpy(dtype=float)
            + 0.40 * daily["pred_stl"].to_numpy(dtype=float)
            + 0.20 * daily["pred_croston"].to_numpy(dtype=float),
            daily["pred_lgbm"].to_numpy(dtype=float)
            * daily["sku_id"].map(scale_lookup).fillna(1.0).to_numpy(dtype=float),
        )
        self._last_daily_preds = daily

        return summary, horizon_feat_df, residuals_by_sku


def _combine_stage1_row(
    row: pd.Series,
    segment_weights: dict[str, dict[str, float]],
    segment_champions: dict[str, str],
    intermittent_winner: str,
) -> float:
    """Combine base model predictions according to regime & inverse-WAPE weights."""
    regime = str(row.get("regime", "smooth"))
    seg = str(row.get("abc_segment", "B"))
    if regime in {"intermittent", "lumpy"}:
        if intermittent_winner == "croston_sba":
            return max(
                0.0,
                0.45 * float(row["pred_croston"])
                + 0.35 * float(row["pred_stl"])
                + 0.20 * float(row["pred_lgbm"]),
            )
        return max(
            0.0,
            0.40 * float(row["pred_lgbm"])
            + 0.45 * float(row["pred_stl"])
            + 0.15 * float(row["pred_croston"]),
        )

    champ = segment_champions.get(seg, "amplified_ensemble")
    if champ == "stl_calendar":
        return max(0.0, float(row["pred_stl"]))
    if champ == "seasonal_naive":
        return max(0.0, float(row["pred_naive"]))

    w = segment_weights.get(seg, {"lgbm": 0.45, "stl": 0.45, "naive": 0.10})
    combined = (
        w["lgbm"] * float(row["pred_lgbm"])
        + w["stl"] * float(row["pred_stl"])
        + w["naive"] * float(row["pred_naive"])
    )
    return max(0.0, combined)


def save_pipeline_artifacts(pipeline: AmplifiedForecastPipeline, model_dir: Path) -> Path:
    """Persist LightGBM boosters natively (.txt) + pipeline metadata (.json) without pickle."""
    model_dir.mkdir(parents=True, exist_ok=True)
    pipeline.lgbm_model.tweedie_model.booster_.save_model(str(model_dir / "lgbm_tweedie.txt"))
    pipeline.lgbm_model.q50_model.booster_.save_model(str(model_dir / "lgbm_q50.txt"))
    pipeline.lgbm_model.q90_model.booster_.save_model(str(model_dir / "lgbm_q90.txt"))

    meta = {
        "model_version": pipeline.model_version,
        "feature_columns": pipeline.lgbm_model.feature_columns,
        "categorical_features": pipeline.lgbm_model.categorical_features,
        "segment_weights": pipeline.segment_weights,
        "segment_champions": pipeline.segment_champions,
        "intermittent_winner": pipeline.intermittent_winner,
        "category_bias_factors": pipeline.category_bias_factors,
        "reconciliation_alpha": pipeline.reconciliation_alpha,
        "val_residual_count": int(len(pipeline.val_residuals_df)),
    }
    meta_path = model_dir / "pipeline_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta_path
