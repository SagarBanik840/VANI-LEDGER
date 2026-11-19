"""Shared zero-leakage feature engineering pipeline for VANI-LEDGER.

NON-NEGOTIABLE GUARDRAILS:
- Pure Python / NumPy / pandas only (zero LLM or HTTP imports).
- Single shared `build_features()` used identically for train, validation,
  walk-forward backtest, and serving (zero train/serve skew).
- Every lag, rolling statistic, demand-regime metric (ADI/CV^2), baseline velocity,
  weekday profile, and historical festival elasticity is strictly causal via
  `.shift(1)` BEFORE any aggregation so future targets can NEVER leak backward.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from vani_ledger.storage.repository import (
    list_calendar_events,
    list_sales_for_merchant,
    list_skus,
)

CATEGORY_CODES: dict[str, int] = {
    "Cooking Oil": 0,
    "Dairy & Bakery": 1,
    "Staples": 2,
    "Snacks": 3,
    "Micro-D2C": 4,
}

CATEGORICAL_FEATURES: list[str] = ["sku_cat_code", "category_code"]

FEATURE_COLUMNS: list[str] = [
    # Lags (4) — strictly t - k
    "lag_1",
    "lag_7",
    "lag_14",
    "lag_28",
    # Rolling stats (9) — strictly on shift(1) uncensored/imputed series
    "roll_mean_7",
    "roll_mean_14",
    "roll_mean_28",
    "base_roll_mean_90",
    "roll_std_14",
    "roll_std_28",
    "roll_max_14",
    "roll_zero_rate_28",
    "ewm_mean_14",
    # Causal weekday & historical festival elasticity signals (3)
    "causal_dow_ratio",
    "past_fest_sens",
    "festival_demand_signal",
    # Calendar & Indian Seasonality (10) — deterministically known in advance
    "dow",
    "dow_sin",
    "dow_cos",
    "month",
    "is_weekend",
    "is_payday_window",
    "is_festival_window",
    "days_to_festival",
    "festival_pre_surge_weight",
    "time_trend_idx",
    # SKU & Business Inputs (7)
    "sku_cat_code",
    "category_code",
    "unit_price",
    "promo_flag",
    "perishable",
    "lead_time_days",
    "shelf_life_days",
    # Demand Regime — quantized Syntetos-Boylan causal stats (2)
    "adi",
    "cv2",
]

BASE_VELOCITY_FEATURES: set[str] = {
    "lag_1",
    "lag_7",
    "lag_14",
    "lag_28",
    "roll_mean_7",
    "roll_mean_14",
    "roll_mean_28",
    "base_roll_mean_90",
    "roll_std_14",
    "roll_std_28",
    "roll_max_14",
    "roll_zero_rate_28",
    "ewm_mean_14",
    "causal_dow_ratio",
    "past_fest_sens",
    "sku_cat_code",
    "category_code",
    "unit_price",
    "perishable",
    "lead_time_days",
    "shelf_life_days",
    "adi",
    "cv2",
}

FESTIVAL_CALENDAR_FEATURES: set[str] = {
    "dow",
    "dow_sin",
    "dow_cos",
    "month",
    "is_weekend",
    "is_payday_window",
    "is_festival_window",
    "days_to_festival",
    "festival_pre_surge_weight",
    "festival_demand_signal",
}

TREND_PROMO_FEATURES: set[str] = {"promo_flag", "time_trend_idx"}


def normalize_skus_df(skus_df: pd.DataFrame) -> pd.DataFrame:
    """Normalize SKU metadata DataFrame so `sku_id` and typed numeric columns exist."""
    df = skus_df.copy()
    if "sku_id" not in df.columns and "id" in df.columns:
        df = df.rename(columns={"id": "sku_id"})
    df["sku_id"] = df["sku_id"].astype(str)
    if "sku_code" not in df.columns:
        df["sku_code"] = df["sku_id"]
    if "category" not in df.columns:
        df["category"] = "Staples"
    if "abc_segment" not in df.columns:
        df["abc_segment"] = "B"
    if "unit_price" not in df.columns:
        df["unit_price"] = 100.0
    if "unit_cost" not in df.columns:
        df["unit_cost"] = 70.0
    if "lead_time_days" not in df.columns:
        df["lead_time_days"] = 2
    if "shelf_life_days" not in df.columns:
        df["shelf_life_days"] = 90
    if "perishable" not in df.columns:
        df["perishable"] = False

    df["unit_price"] = pd.to_numeric(df["unit_price"], errors="coerce").fillna(100.0).astype(float)
    df["unit_cost"] = pd.to_numeric(df["unit_cost"], errors="coerce").fillna(70.0).astype(float)
    df["lead_time_days"] = (
        pd.to_numeric(df["lead_time_days"], errors="coerce").fillna(2).astype(int)
    )
    df["shelf_life_days"] = (
        pd.to_numeric(df["shelf_life_days"], errors="coerce").fillna(90).astype(int)
    )
    df["perishable"] = df["perishable"].astype(bool)

    df = df.sort_values("sku_code").reset_index(drop=True)
    if "sku_cat_code" not in df.columns:
        df["sku_cat_code"] = np.arange(len(df), dtype=int)
    df["category_code"] = (
        df["category"].map(CATEGORY_CODES).fillna(len(CATEGORY_CODES)).astype(int)
    )
    return df


def build_dense_sales_grid(sales_df: pd.DataFrame, skus_df: pd.DataFrame) -> pd.DataFrame:
    """Zero-fill missing SKU-days while explicitly preserving stockout and promo flags."""
    skus_norm = normalize_skus_df(skus_df)
    if sales_df.empty:
        return pd.DataFrame(
            columns=["sku_id", "sold_date", "qty", "unit_price", "promo_flag", "stockout_flag"]
        )

    df = sales_df.copy()
    df["sku_id"] = df["sku_id"].astype(str)
    df["sold_date"] = pd.to_datetime(df["sold_date"]).dt.normalize()
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0.0).astype(float)
    df["promo_flag"] = df.get("promo_flag", False)
    df["promo_flag"] = pd.Series(df["promo_flag"], index=df.index).fillna(False).astype(bool)
    df["stockout_flag"] = df.get("stockout_flag", False)
    df["stockout_flag"] = pd.Series(df["stockout_flag"], index=df.index).fillna(False).astype(bool)
    if "unit_price" not in df.columns:
        price_map = dict(zip(skus_norm["sku_id"], skus_norm["unit_price"], strict=False))
        df["unit_price"] = df["sku_id"].map(price_map).fillna(100.0).astype(float)
    else:
        df["unit_price"] = pd.to_numeric(df["unit_price"], errors="coerce").astype(float)

    agg = (
        df.groupby(["sku_id", "sold_date"], as_index=False)
        .agg(
            qty=("qty", "sum"),
            unit_price=("unit_price", "last"),
            promo_flag=("promo_flag", "max"),
            stockout_flag=("stockout_flag", "max"),
        )
        .sort_values(["sku_id", "sold_date"])
    )

    price_lookup = dict(zip(skus_norm["sku_id"], skus_norm["unit_price"], strict=False))
    parts: list[pd.DataFrame] = []
    for sku_id, grp in agg.groupby("sku_id", sort=False):
        min_d = grp["sold_date"].min()
        max_d = grp["sold_date"].max()
        full_dates = pd.date_range(min_d, max_d, freq="D")
        reindexed = grp.set_index("sold_date").reindex(full_dates)
        reindexed.index.name = "sold_date"
        reindexed = reindexed.reset_index()
        reindexed["sku_id"] = str(sku_id)
        reindexed["qty"] = reindexed["qty"].fillna(0.0).astype(float)
        reindexed["promo_flag"] = reindexed["promo_flag"].fillna(False).astype(bool)
        reindexed["stockout_flag"] = reindexed["stockout_flag"].fillna(False).astype(bool)
        default_price = float(price_lookup.get(str(sku_id), 100.0))
        reindexed["unit_price"] = (
            reindexed["unit_price"].ffill().bfill().fillna(default_price).astype(float)
        )
        parts.append(reindexed)

    dense = pd.concat(parts, ignore_index=True)
    return dense.sort_values(["sku_id", "sold_date"]).reset_index(drop=True)


def impute_censored_demand(df: pd.DataFrame) -> pd.Series:
    """Strictly causal stockout censoring imputation using past uncensored same-weekday median."""
    ordered = df.sort_values(["sku_id", "sold_date"]).copy()
    dow = pd.to_datetime(ordered["sold_date"]).dt.dayofweek
    uncensored = ordered["qty"].astype(float).where(~ordered["stockout_flag"].astype(bool), np.nan)

    dow_median = uncensored.groupby([ordered["sku_id"], dow], sort=False).transform(
        lambda s: s.shift(1).rolling(6, min_periods=1).median()
    )
    sku_fallback = uncensored.groupby(ordered["sku_id"], sort=False).transform(
        lambda s: s.shift(1).rolling(28, min_periods=1).mean()
    )
    est = dow_median.fillna(sku_fallback).fillna(ordered["qty"].astype(float))
    imputed = np.where(
        ordered["stockout_flag"].astype(bool),
        np.maximum(ordered["qty"].astype(float), est.to_numpy(dtype=float)),
        ordered["qty"].astype(float),
    )
    return pd.Series(imputed, index=ordered.index, name="demand_imputed", dtype=float)


def classify_demand_regime(adi: float, cv2: float, history_days: int) -> str:
    """Syntetos-Boylan demand regime classification."""
    if history_days < 14:
        return "cold_start"
    if adi < 1.32 and cv2 < 0.49:
        return "smooth"
    if adi < 1.32 and cv2 >= 0.49:
        return "erratic"
    if adi >= 1.32 and cv2 < 0.49:
        return "intermittent"
    return "lumpy"


def compute_data_sufficiency(
    history_days: int, stockout_rate: float
) -> Literal["high", "medium", "low"]:
    """Honest data-sufficiency badge based on history depth and censoring rate."""
    if history_days >= 180 and stockout_rate <= 0.10:
        return "high"
    if history_days >= 45 and stockout_rate <= 0.25:
        return "medium"
    return "low"


def compute_sku_regime_stats(sales_df: pd.DataFrame, as_of_date: date) -> dict[str, dict[str, Any]]:
    """Compute strict past-only (<= as_of_date) ADI, CV^2, stockout rate, and regime per SKU."""
    if sales_df.empty:
        return {}
    df = sales_df.copy()
    df["sku_id"] = df["sku_id"].astype(str)
    df["sold_date"] = pd.to_datetime(df["sold_date"]).dt.normalize()
    cutoff = pd.Timestamp(as_of_date)
    hist = df[df["sold_date"] <= cutoff]

    stats: dict[str, dict[str, Any]] = {}
    for sku_id, grp in hist.groupby("sku_id", sort=False):
        n_days = len(grp)
        qty = pd.to_numeric(grp["qty"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        stockouts = grp["stockout_flag"].astype(bool).to_numpy()
        uncensored = qty[~stockouts]
        nz = uncensored[uncensored > 0.0]

        n_unc = max(len(uncensored), 1)
        adi = float(np.clip(n_unc / max(len(nz), 1), 1.0, 30.0))
        if len(nz) >= 3 and float(np.mean(nz)) > 1e-6:
            cv2 = float(np.clip((np.std(nz, ddof=1) / np.mean(nz)) ** 2, 0.0, 5.0))
        else:
            cv2 = 0.25
        so_rate = float(np.mean(stockouts)) if n_days > 0 else 0.0
        regime = classify_demand_regime(adi, cv2, n_days)
        stats[str(sku_id)] = {
            "history_days": n_days,
            "adi": round(adi, 4),
            "cv2": round(cv2, 4),
            "stockout_rate": round(so_rate, 4),
            "regime": regime,
            "data_sufficiency": compute_data_sufficiency(n_days, so_rate),
        }
    return stats


def _causal_adi_cv2(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """Compute strictly causal expanding ADI and CV^2 (quantized to 0.1 to curb overfitting)."""
    vals = series.to_numpy(dtype=float)
    n = len(vals)
    adi = np.ones(n, dtype=float)
    cv2 = np.full(n, 0.20, dtype=float)

    valid_cnt = 0
    nz_cnt = 0
    nz_sum = 0.0
    nz_sq_sum = 0.0

    for i in range(n):
        v = vals[i]
        if not np.isnan(v):
            valid_cnt += 1
            if v > 0.0:
                nz_cnt += 1
                nz_sum += v
                nz_sq_sum += v * v
        if valid_cnt > 0:
            adi[i] = round(min(30.0, max(1.0, valid_cnt / max(nz_cnt, 1))), 1)
        if nz_cnt >= 3:
            mean_nz = nz_sum / nz_cnt
            var_nz = max(0.0, (nz_sq_sum / nz_cnt) - mean_nz * mean_nz)
            if mean_nz > 1e-6:
                cv2[i] = round(min(5.0, var_nz / (mean_nz * mean_nz)), 1)
    return adi, cv2


def build_features(
    sales_df: pd.DataFrame,
    cal_df: pd.DataFrame,
    skus_df: pd.DataFrame,
    *,
    fill_dense: bool = True,
) -> pd.DataFrame:
    """Single shared zero-leakage feature builder for training, backtesting, and serving.

    Guarantee: For every row `(sku_id, sold_date = t)`, EVERY column in `FEATURE_COLUMNS`
    depends strictly on sales observed at dates `< t` (via `.shift(1)`) plus deterministic
    calendar and SKU attributes known in advance.
    """
    skus_norm = normalize_skus_df(skus_df)
    base = build_dense_sales_grid(sales_df, skus_norm) if fill_dense else sales_df.copy()
    if base.empty:
        return pd.DataFrame(columns=["sku_id", "sold_date", "qty", "is_censored", *FEATURE_COLUMNS])

    base["sku_id"] = base["sku_id"].astype(str)
    base["sold_date"] = pd.to_datetime(base["sold_date"]).dt.normalize()
    base = base.sort_values(["sku_id", "sold_date"]).reset_index(drop=True)

    # 1. Causal stockout censoring imputation
    base["is_censored"] = base["stockout_flag"].astype(bool)
    base["demand_imputed"] = impute_censored_demand(base)

    # 2. Merge deterministic Indian festival & payday calendar
    cal = cal_df.copy()
    date_col = "event_date" if "event_date" in cal.columns else "sold_date"
    cal["sold_date"] = pd.to_datetime(cal[date_col]).dt.normalize()
    cal["days_to_festival"] = (
        pd.to_numeric(cal["days_to_nearest_festival"], errors="coerce").fillna(30).astype(float)
    )
    cal["festival_pre_surge_weight"] = (
        pd.to_numeric(cal["festival_pre_surge_weight"], errors="coerce").fillna(0.0).astype(float)
    )
    cal["is_festival_window"] = cal["is_festival_window"].astype(bool).astype(float)
    cal["is_payday_window"] = cal["is_payday_window"].astype(bool).astype(float)
    cal_cols = [
        "sold_date",
        "days_to_festival",
        "festival_pre_surge_weight",
        "is_festival_window",
        "is_payday_window",
    ]
    if "festival_name" in cal.columns:
        cal_cols.append("festival_name")

    merged = base.merge(cal[cal_cols].drop_duplicates("sold_date"), on="sold_date", how="left")
    merged["days_to_festival"] = merged["days_to_festival"].fillna(30.0)
    merged["festival_pre_surge_weight"] = merged["festival_pre_surge_weight"].fillna(0.0)
    merged["is_festival_window"] = merged["is_festival_window"].fillna(0.0)
    merged["is_payday_window"] = (
        merged["is_payday_window"]
        .fillna(
            ((merged["sold_date"].dt.day <= 5) | (merged["sold_date"].dt.day >= 25)).astype(float)
        )
        .astype(float)
    )

    # 3. Merge SKU metadata
    sku_meta_cols = [
        "sku_id",
        "sku_code",
        "category",
        "abc_segment",
        "sku_cat_code",
        "category_code",
        "unit_cost",
        "lead_time_days",
        "shelf_life_days",
        "perishable",
    ]
    merged = merged.merge(skus_norm[sku_meta_cols], on="sku_id", how="left")
    merged["perishable"] = merged["perishable"].fillna(False).astype(float)
    merged["promo_flag"] = merged["promo_flag"].astype(float)
    merged["lead_time_days"] = merged["lead_time_days"].fillna(2).astype(float)
    merged["shelf_life_days"] = merged["shelf_life_days"].fillna(90).astype(float)
    merged["sku_cat_code"] = merged["sku_cat_code"].fillna(0).astype(int)
    merged["category_code"] = merged["category_code"].fillna(0).astype(int)

    # 4. Deterministic calendar encodings (origin anchored at 2024-11-01 so trend index is stable)
    dow = merged["sold_date"].dt.dayofweek.astype(float)
    merged["dow"] = dow
    merged["dow_sin"] = np.sin(2.0 * math.pi * dow / 7.0)
    merged["dow_cos"] = np.cos(2.0 * math.pi * dow / 7.0)
    merged["month"] = merged["sold_date"].dt.month.astype(float)
    merged["is_weekend"] = (dow >= 5.0).astype(float)
    merged["time_trend_idx"] = (
        (merged["sold_date"] - pd.Timestamp("2024-11-01")).dt.days.astype(float) / 730.0
    )

    # 5. Strictly causal lags, rolling stats, non-festival baseline, DOW ratio, and festival sens
    merged = merged.sort_values(["sku_id", "sold_date"]).reset_index(drop=True)
    grp = merged.groupby("sku_id", sort=False)["demand_imputed"]

    # Shift(1) BEFORE every rolling calculation — hard anti-leakage guarantee
    shifted_1 = grp.shift(1)
    merged["lag_1"] = shifted_1
    merged["lag_7"] = grp.shift(7)
    merged["lag_14"] = grp.shift(14)
    merged["lag_28"] = grp.shift(28)

    s_grp = shifted_1.groupby(merged["sku_id"], sort=False)
    merged["roll_mean_7"] = s_grp.transform(lambda s: s.rolling(7, min_periods=1).mean())
    merged["roll_mean_14"] = s_grp.transform(lambda s: s.rolling(14, min_periods=1).mean())
    merged["roll_mean_28"] = s_grp.transform(lambda s: s.rolling(28, min_periods=1).mean())
    merged["roll_std_14"] = s_grp.transform(lambda s: s.rolling(14, min_periods=2).std()).fillna(
        0.0
    )
    merged["roll_std_28"] = s_grp.transform(lambda s: s.rolling(28, min_periods=2).std()).fillna(
        0.0
    )
    merged["roll_max_14"] = s_grp.transform(lambda s: s.rolling(14, min_periods=1).max())
    merged["roll_zero_rate_28"] = s_grp.transform(
        lambda s: (s <= 0.0).astype(float).rolling(28, min_periods=1).mean()
    )
    merged["ewm_mean_14"] = s_grp.transform(lambda s: s.ewm(span=14, min_periods=1).mean())

    # Strictly causal non-festival baseline velocity (rolling 90d of non-festival shifted_1)
    non_fest_shifted = shifted_1.where(
        merged.groupby("sku_id", sort=False)["is_festival_window"].shift(1).fillna(0.0) < 0.5,
        np.nan,
    )
    base_roll_90 = non_fest_shifted.groupby(merged["sku_id"], sort=False).transform(
        lambda s: s.rolling(90, min_periods=1).mean()
    )
    merged["base_roll_mean_90"] = base_roll_90.fillna(merged["roll_mean_28"]).fillna(0.0)

    # Strictly causal same-weekday multiplier (past 8 occurrences of same dow / past 56d mean)
    dow_past_mean = merged.groupby(["sku_id", "dow"], sort=False)["demand_imputed"].transform(
        lambda s: s.shift(1).rolling(8, min_periods=1).mean()
    )
    overall_past_56 = s_grp.transform(lambda s: s.rolling(56, min_periods=1).mean())
    raw_dow_ratio = (dow_past_mean + 2.0 * overall_past_56) / (
        3.0 * np.maximum(overall_past_56, 0.10)
    )
    merged["causal_dow_ratio"] = raw_dow_ratio.fillna(1.0).clip(0.70, 1.35)

    # Strictly causal expanding ridge festival sensitivity + quantized ADI/CV^2
    adi_arr = np.ones(len(merged), dtype=float)
    cv2_arr = np.full(len(merged), 0.20, dtype=float)
    fest_sens_arr = np.full(len(merged), 0.50, dtype=float)

    for _sku_id, idx in merged.groupby("sku_id", sort=False).groups.items():
        s_prev = shifted_1.loc[idx]
        fw_prev = (
            merged.loc[idx, "festival_pre_surge_weight"].shift(1).fillna(0.0).to_numpy(dtype=float)
        )
        base_prev = (
            merged.loc[idx, "base_roll_mean_90"].to_numpy(dtype=float)
        )
        vals = s_prev.fillna(0.0).to_numpy(dtype=float)
        valid_mask = (~s_prev.isna()).to_numpy(dtype=float)

        # Ridge regression of (y_prev / base_prev - 1) on fw_prev strictly over past days
        safe_base = np.maximum(base_prev, 0.15)
        rel_excess = np.clip((vals / safe_base) - 1.0, -1.0, 4.0)
        num = np.cumsum(valid_mask * fw_prev * rel_excess)
        den = np.cumsum(valid_mask * fw_prev * fw_prev)
        # Prior sensitivity 0.50 with ridge weight 4.0 stabilizes sparse/early rows
        sens = np.clip((num + 4.0 * 0.50) / (den + 4.0), 0.0, 2.2)
        fest_sens_arr[idx] = np.round(sens, 2)

        sku_adi, sku_cv2 = _causal_adi_cv2(s_prev)
        adi_arr[idx] = sku_adi
        cv2_arr[idx] = sku_cv2

    merged["past_fest_sens"] = fest_sens_arr
    merged["adi"] = adi_arr
    merged["cv2"] = cv2_arr

    for col in (
        "lag_1",
        "lag_7",
        "lag_14",
        "lag_28",
        "roll_mean_7",
        "roll_mean_14",
        "roll_mean_28",
    ):
        merged[col] = merged[col].fillna(merged["base_roll_mean_90"]).fillna(0.0)
    merged["roll_max_14"] = merged["roll_max_14"].fillna(0.0)
    merged["roll_zero_rate_28"] = merged["roll_zero_rate_28"].fillna(0.0)
    merged["ewm_mean_14"] = merged["ewm_mean_14"].fillna(0.0)

    merged["festival_demand_signal"] = (
        merged["base_roll_mean_90"]
        * merged["festival_pre_surge_weight"]
        * merged["past_fest_sens"]
    )

    return merged


def build_horizon_calendar_dates(as_of_date: date, horizon_days: int) -> list[date]:
    """Return ordered forecast target dates `[as_of_date + 1, ..., as_of_date + horizon_days]`."""
    return [as_of_date + timedelta(days=step) for step in range(1, horizon_days + 1)]


def compute_feature_hash(feature_df: pd.DataFrame, sku_id: str | None = None) -> str:
    """Deterministic SHA-256 hex digest of the feature snapshot for forecast reproducibility."""
    sub = feature_df if sku_id is None else feature_df[feature_df["sku_id"] == str(sku_id)]
    if sub.empty:
        return hashlib.sha256(b"empty-feature-snapshot").hexdigest()
    cols = [c for c in FEATURE_COLUMNS if c in sub.columns]
    arr = np.round(sub[cols].to_numpy(dtype=float), 6)
    payload = "|".join(cols).encode("utf-8") + arr.tobytes()
    return hashlib.sha256(payload).hexdigest()


def load_dataframes_from_db(
    db: Session, merchant_id: str
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load tenant-scoped `(sales_df, cal_df, skus_df)` from SQLAlchemy ORM repositories."""
    sku_rows, _total = list_skus(db, merchant_id, limit=100, offset=0)
    sale_rows = list_sales_for_merchant(db, merchant_id)
    cal_rows = list_calendar_events(db)

    skus_df = pd.DataFrame(
        [
            {
                "sku_id": str(s.id),
                "sku_code": s.sku_code,
                "name": s.name,
                "hinglish_alias": s.hinglish_alias,
                "category": s.category,
                "unit": s.unit,
                "unit_cost": float(s.unit_cost),
                "unit_price": float(s.unit_price),
                "salvage_value": float(s.salvage_value),
                "lead_time_days": int(s.lead_time_days),
                "shelf_life_days": int(s.shelf_life_days),
                "moq": int(s.moq),
                "pack_size": int(s.pack_size),
                "perishable": bool(s.perishable),
                "on_hand": float(s.on_hand),
                "on_order": float(s.on_order),
                "abc_segment": str(s.abc_segment),
            }
            for s in sku_rows
        ]
    )

    sales_df = pd.DataFrame(
        [
            {
                "sku_id": str(r.sku_id),
                "sold_date": r.sold_date,
                "qty": float(r.qty),
                "unit_price": float(r.unit_price),
                "promo_flag": bool(r.promo_flag),
                "stockout_flag": bool(r.stockout_flag),
            }
            for r in sale_rows
        ]
    )

    cal_df = pd.DataFrame(
        [
            {
                "event_date": c.event_date,
                "festival_name": c.festival_name,
                "days_to_nearest_festival": int(c.days_to_nearest_festival),
                "festival_pre_surge_weight": float(c.festival_pre_surge_weight),
                "is_festival_window": bool(c.is_festival_window),
                "is_payday_window": bool(c.is_payday_window),
                "is_holiday": bool(c.is_holiday),
            }
            for c in cal_rows
        ]
    )

    return sales_df, cal_df, skus_df
