"""Explainable AI (SHAP TreeExplainer / TreeSHAP), additive driver reconciliation,
deterministic narrative builder, and strict numeric grounding verifier.

NON-NEGOTIABLE GUARDRAILS:
- Pure Python / NumPy / pandas / SHAP only — ZERO LLM or HTTP imports (AGENTS.md Rule 2).
- Largest-remainder integer rounding guarantees `sum(d.units for d in drivers) == recommended_qty`.
- `verify_numeric_grounding()` rejects any narrative containing invented/drifted numbers (> ±0.1)
  so callers immediately fall back to `build_deterministic_narrative()`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal

import numpy as np
import pandas as pd
import shap

from vani_ledger.forecast_core.features import (
    BASE_VELOCITY_FEATURES,
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    FESTIVAL_CALENDAR_FEATURES,
    TREND_PROMO_FEATURES,
)
from vani_ledger.schemas import DriverItem, ForecastPayload

# Extract numbers while ignoring digits embedded inside alphanumeric tokens like `p10` or `v1`.
# A leading `-` is only treated as a negative sign when not immediately preceded by a digit
# (so hyphenated ranges like `30-57` yield `30` and `57` rather than `30` and `-57`).
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?<!\d)[+-])?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
)

# Fields whose raw strings (UUIDs, hashes, ISO dates, versions) must NEVER authorize numbers
_EXCLUDED_PAYLOAD_KEYS: set[str] = {
    "id",
    "sku_id",
    "forecast_id",
    "merchant_id",
    "feature_hash",
    "model_version",
    "model_used",
    "as_of_date",
    "created_at",
}

# String label keys whose embedded product/unit numbers (e.g. "1L", "3-day") are legitimate
_LABEL_TEXT_KEYS: set[str] = {"sku_name", "name", "sku_code", "hinglish_alias"}

# Probability fields in [0, 1] whose percentage form (x100) is also grounded
_PROBABILITY_KEYS: set[str] = {
    "service_level",
    "confidence",
    "stockout_risk_before",
    "stockout_risk_after",
    "stockout_risk_delta",
    "spoilage_risk",
}


def compute_shap_horizon_attribution(
    tweedie_model: Any,
    horizon_feat_df: pd.DataFrame,
    sku_id: str,
    *,
    feature_columns: Sequence[str] = FEATURE_COLUMNS,
    stl_base_total: float | None = None,
    stl_festival_total: float | None = None,
) -> dict[str, float]:
    """Compute exact TreeSHAP attribution for a SKU's forecast horizon grouped into seller drivers.

    Uses `shap.TreeExplainer` (falling back to LightGBM native `pred_contrib=True` exact TreeSHAP)
    and converts log-link Tweedie SHAP contributions into additive unit-space shares for:
    - `base_velocity`
    - `festival_surge`
    - `trend_shift`
    """
    sub = horizon_feat_df[horizon_feat_df["sku_id"].astype(str) == str(sku_id)].copy()
    if sub.empty:
        return {
            "base_velocity": float(stl_base_total or 0.0),
            "festival_surge": float(stl_festival_total or 0.0),
            "trend_shift": 0.0,
        }

    cols = list(feature_columns)
    x_mat = sub[cols].copy()
    for cat_col in CATEGORICAL_FEATURES:
        if cat_col in x_mat.columns:
            x_mat[cat_col] = x_mat[cat_col].astype(int)

    preds = np.maximum(0.0, np.asarray(tweedie_model.predict(x_mat), dtype=float))
    total_pred = float(np.sum(preds))

    base_margin = 0.0
    fest_margin = 0.0
    trend_margin = 0.0
    expected_margin = 0.0

    try:
        explainer = shap.TreeExplainer(tweedie_model)
        shap_vals = np.asarray(explainer.shap_values(x_mat), dtype=float)
        ev = explainer.expected_value
        expected_margin = float(ev[0] if isinstance(ev, (list, np.ndarray)) else ev)
    except Exception:
        # Native LightGBM exact TreeSHAP fallback (last column is expected value)
        contrib = np.asarray(tweedie_model.predict(x_mat, pred_contrib=True), dtype=float)
        shap_vals = contrib[:, :-1]
        expected_margin = float(np.mean(contrib[:, -1]))

    # Aggregate raw TreeSHAP margins across the horizon
    n_rows = len(x_mat)
    for col_idx, col_name in enumerate(cols):
        col_sum = float(np.sum(shap_vals[:, col_idx]))
        if col_name in FESTIVAL_CALENDAR_FEATURES:
            fest_margin += col_sum
        elif col_name in TREND_PROMO_FEATURES:
            trend_margin += col_sum
        elif col_name in BASE_VELOCITY_FEATURES:
            base_margin += col_sum
        else:
            base_margin += col_sum

    # Convert log-link Tweedie margins to unit-space additive decomposition via
    # exact counterfactual baseline (`exp(margin - fest_margin)` vs `exp(margin)`).
    row_margins = expected_margin + np.sum(shap_vals, axis=1)
    fest_col_indices = [i for i, c in enumerate(cols) if c in FESTIVAL_CALENDAR_FEATURES]
    trend_col_indices = [i for i, c in enumerate(cols) if c in TREND_PROMO_FEATURES]

    row_fest_m = (
        np.sum(shap_vals[:, fest_col_indices], axis=1)
        if fest_col_indices
        else np.zeros(n_rows, dtype=float)
    )
    row_trend_m = (
        np.sum(shap_vals[:, trend_col_indices], axis=1)
        if trend_col_indices
        else np.zeros(n_rows, dtype=float)
    )

    # For Tweedie log-link, exp(row_margins) == preds; compute exact unit-space festival lift
    no_fest_preds = np.exp(np.clip(row_margins - row_fest_m, -10.0, 10.0))
    fest_units_shap = float(np.sum(preds - no_fest_preds))
    no_trend_preds = np.exp(np.clip(row_margins - row_trend_m, -10.0, 10.0))
    trend_units_shap = float(np.sum(preds - no_trend_preds))

    # Blend with STL calendar decomposition when provided so ensemble attribution mirrors ensemble
    if stl_festival_total is not None and stl_base_total is not None:
        fest_units = 0.60 * max(0.0, fest_units_shap) + 0.40 * max(0.0, float(stl_festival_total))
    else:
        fest_units = max(0.0, fest_units_shap)

    fest_units = min(fest_units, total_pred * 0.60)
    trend_units = float(np.clip(trend_units_shap, -0.15 * total_pred, 0.15 * total_pred))
    base_units = max(0.0, total_pred - fest_units - trend_units)

    return {
        "base_velocity": round(base_units, 4),
        "festival_surge": round(fest_units, 4),
        "trend_shift": round(trend_units, 4),
        "raw_shap_base_margin": round(base_margin + expected_margin * n_rows, 4),
        "raw_shap_festival_margin": round(fest_margin, 4),
    }


def reconcile_driver_units(
    raw_drivers: Sequence[tuple[str, float]],
    target_qty: int,
) -> list[DriverItem]:
    """Reconcile floating driver contributions into exact integers summing to `target_qty`.

    Uses largest-remainder apportionment so `sum(d.units for d in drivers) == target_qty`
    ALWAYS holds, with accurate `share_pct` and `direction`.
    """
    target = int(target_qty)
    entries = [(str(name), float(val)) for name, val in raw_drivers if str(name).strip()]
    if not entries:
        return [
            DriverItem(
                name="Base weekly velocity",
                units=target,
                share_pct=100.0 if target > 0 else 0.0,
                direction="positive" if target > 0 else "neutral",
            )
        ]

    if target == 0:
        return [
            DriverItem(name=name, units=0, share_pct=0.0, direction="neutral")
            for name, _ in entries
        ]

    # Reconcile raw contributions to sum to target_qty without blowing up mixed-sign drivers
    raw_vals = np.array([v for _, v in entries], dtype=float)
    raw_sum = float(np.sum(raw_vals))
    has_mixed_signs = bool(np.any(raw_vals > 0.0) and np.any(raw_vals < 0.0))
    if abs(raw_sum) < 1e-9:
        scaled = np.zeros(len(entries), dtype=float)
        scaled[0] = float(target)
    elif has_mixed_signs:
        # Preserve physical magnitudes of positive/negative drivers; absorb residual on largest
        residual = float(target) - raw_sum
        scaled = raw_vals.copy()
        abs_total = float(np.sum(np.abs(scaled)))
        if abs_total > 1e-9:
            scaled = scaled + residual * (np.abs(scaled) / abs_total)
        else:
            scaled[0] += residual
    elif raw_sum * target > 0:
        scaled = raw_vals * (float(target) / raw_sum)
    else:
        abs_sum = float(np.sum(np.abs(raw_vals)))
        scaled = (np.abs(raw_vals) / max(abs_sum, 1e-9)) * float(target)

    # Largest-remainder integer rounding
    floors = np.floor(scaled).astype(int)
    remainders = scaled - floors
    deficit = target - int(np.sum(floors))

    if deficit > 0:
        order = np.argsort(-remainders)
        for idx in order[:deficit]:
            floors[int(idx)] += 1
    elif deficit < 0:
        order = np.argsort(remainders)
        for idx in order[: abs(deficit)]:
            floors[int(idx)] -= 1

    # Build DriverItem list with exact share percentages
    denom = float(sum(abs(int(u)) for u in floors)) or 1.0
    result: list[DriverItem] = []
    for (name, _), units_int in zip(entries, floors.tolist(), strict=True):
        direction: Literal["positive", "negative", "neutral"]
        if units_int > 0:
            direction = "positive"
        elif units_int < 0:
            direction = "negative"
        else:
            direction = "neutral"
        share = round((abs(units_int) / denom) * 100.0, 1)
        result.append(
            DriverItem(
                name=name,
                units=int(units_int),
                share_pct=share,
                direction=direction,
            )
        )
    return result


def build_seller_drivers(
    *,
    base_units: float,
    festival_units: float,
    lead_buffer_units: float,
    recommended_qty: int,
    lead_time_days: int = 3,
    festival_label: str = "Diwali pre-surge",
    trend_units: float = 0.0,
    perishable_cap_units: float = 0.0,
) -> list[DriverItem]:
    """Assemble seller-friendly additive drivers reconciled to exact `recommended_qty`."""
    lead_label = f"{max(1, int(lead_time_days))}-day supplier lead buffer"
    raw: list[tuple[str, float]] = [
        ("Base weekly velocity", max(0.0, float(base_units))),
    ]
    if abs(float(festival_units)) >= 0.5:
        raw.append((festival_label, float(festival_units)))
    if abs(float(trend_units)) >= 1.0:
        raw.append(("Recent trend & promo shift", float(trend_units)))
    if abs(float(perishable_cap_units)) >= 0.5:
        raw.append(("Shelf-life spoilage cap", -abs(float(perishable_cap_units))))
    else:
        raw.append((lead_label, float(lead_buffer_units)))

    return reconcile_driver_units(raw, recommended_qty)


def _to_payload_dict(payload: ForecastPayload | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, ForecastPayload):
        return payload.model_dump(mode="json")
    return dict(payload)


def _format_inr_plain(amount: Any) -> str:
    """Format Decimal/number cleanly (e.g. `7200.00` -> `7,200`, `150.50` -> `150.50`)."""
    dec = Decimal(str(amount))
    if dec == dec.to_integral_value():
        return f"{int(dec):,}"
    return f"{dec:,.2f}"


def _format_num_plain(value: Any) -> str:
    dec = Decimal(str(value))
    if dec == dec.to_integral_value():
        return str(int(dec))
    return f"{float(dec):.1f}".rstrip("0").rstrip(".")


def build_deterministic_narrative(
    payload: ForecastPayload | Mapping[str, Any],
    language: Literal["en", "hinglish"] = "en",
) -> str:
    """Build a 100% grounded, plain-language 2-sentence explanation strictly from payload values."""
    data = _to_payload_dict(payload)
    sku_name = str(data.get("sku_name", "this item"))
    rec_qty = int(data.get("recommended_qty", 0))
    horizon = int(data.get("horizon_days", 7))
    outlay_str = _format_inr_plain(data.get("outlay_amount", "0"))
    service_pct = round(float(data.get("service_level", 0.90)) * 100.0)
    p10_str = _format_num_plain(data.get("p10_total", 0))
    p90_str = _format_num_plain(data.get("p90_total", 0))

    raw_drivers = data.get("drivers", [])
    driver_parts: list[str] = []
    for drv in raw_drivers[:4]:
        d_name = str(drv.get("name", "Driver") if isinstance(drv, Mapping) else drv.name)
        d_units = int(drv.get("units", 0) if isinstance(drv, Mapping) else drv.units)
        sign = "+" if d_units >= 0 else ""
        driver_parts.append(f"{sign}{d_units} {d_name}")
    drivers_summary = (
        ", ".join(driver_parts) if driver_parts else f"+{rec_qty} Base weekly velocity"
    )

    if language == "hinglish":
        return (
            f"{sku_name} ke liye agle {horizon} din ka recommended order {rec_qty} units hai "
            f"(kul outlay Rs {outlay_str}, {service_pct}% in-stock confidence, "
            f"demand range {p10_str} se {p90_str}). "
            f"Is order ka exact breakdown hai: {drivers_summary}."
        )

    return (
        f"Order {rec_qty} units of {sku_name} for the next {horizon} days "
        f"(Rs {outlay_str} outlay, {service_pct}% in-stock service level, "
        f"conformal demand range {p10_str} to {p90_str}). "
        f"Exact additive drivers: {drivers_summary}."
    )


def _extract_numbers_from_text(text: str) -> list[float]:
    """Extract all numeric claims from natural-language text."""
    nums: list[float] = []
    for match in _NUMBER_RE.finditer(text):
        token = match.group(0).replace(",", "")
        try:
            val = float(token)
        except ValueError:
            continue
        if math.isfinite(val):
            nums.append(val)
    return nums


def _collect_allowed_numbers(obj: Any, parent_key: str | None = None) -> set[float]:
    """Recursively collect every authorized numeric value from a deterministic forecast payload."""
    allowed: set[float] = set()

    if isinstance(obj, ForecastPayload):
        obj = obj.model_dump(mode="json")

    if isinstance(obj, Mapping):
        for key, value in obj.items():
            key_str = str(key)
            if key_str in _EXCLUDED_PAYLOAD_KEYS:
                continue
            allowed.update(_collect_allowed_numbers(value, parent_key=key_str))
        return allowed

    if isinstance(obj, (list, tuple)):
        for item in obj:
            allowed.update(_collect_allowed_numbers(item, parent_key=parent_key))
        return allowed

    if isinstance(obj, bool) or obj is None:
        return allowed

    if isinstance(obj, (int, float, Decimal)):
        val = float(obj)
        if math.isfinite(val):
            allowed.add(val)
            allowed.add(abs(val))
            if parent_key in _PROBABILITY_KEYS:
                pct = val * 100.0
                allowed.add(pct)
                allowed.add(round(pct, 1))
                allowed.add(float(round(pct)))
        return allowed

    if isinstance(obj, str):
        stripped = obj.strip()
        # Try parsing pure numeric strings (e.g. JSON-serialized Decimals like "7200.00")
        try:
            val = float(stripped.replace(",", ""))
            if math.isfinite(val):
                allowed.add(val)
                allowed.add(abs(val))
                if parent_key in _PROBABILITY_KEYS:
                    pct = val * 100.0
                    allowed.add(pct)
                    allowed.add(round(pct, 1))
                    allowed.add(float(round(pct)))
                return allowed
        except ValueError:
            pass

        # Allow numbers embedded in product or driver labels (e.g. "1L", "3-day buffer")
        if parent_key in _LABEL_TEXT_KEYS or parent_key == "name":
            for num in _extract_numbers_from_text(stripped):
                allowed.add(num)
                allowed.add(abs(num))

    return allowed


def verify_numeric_grounding(
    text: str,
    payload: ForecastPayload | Mapping[str, Any],
    *,
    tolerance: float = 0.1,
) -> bool:
    """Verify EVERY numeric claim in `text` appears in `payload` within `±tolerance`.

    Returns `False` if:
    - `text` is empty or contains no numbers at all (when payload carries numeric fields), OR
    - ANY number extracted from `text` misses every authorized payload value beyond `tolerance`.
    """
    if not text or not text.strip():
        return False

    extracted = _extract_numbers_from_text(text)
    allowed = _collect_allowed_numbers(payload)

    if not extracted:
        return len(allowed) == 0

    if not allowed:
        return False

    for claimed in extracted:
        matched = any(
            abs(claimed - ref) <= tolerance or abs(abs(claimed) - abs(ref)) <= tolerance
            for ref in allowed
        )
        if not matched:
            return False

    return True
