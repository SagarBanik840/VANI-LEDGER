# VANI-LEDGER — Beginner-to-Judge Guide: Supervised ML, XAI, Amplification & API Security

*Written so any team member—even if you have never built an ML model before—can understand every step of VANI-LEDGER and answer tough technical judge questions confidently (AARAMBHATHON 2.0 Rules 30 & 35).*

---

## Part 1: How We Take Raw Shop Data and Train a Supervised ML Model (Step by Step)

### What Does "Supervised ML" Mean Here?
In **Supervised Machine Learning**, you show a computer thousands of historical examples where you already know BOTH:
- **`X` (Inputs / Features):** Everything you knew *before* a day happened (recent sales velocity, day of the week, whether Diwali is 8 days away, whether it's payday week, item price, etc.).
- **`y` (Target / Label):** The true answer that actually happened on that day — **how many units sold** (a number like `6`, `0`, or `14`).

Because `y` is a **quantity (number of units)** rather than a Yes/No category, this is **Supervised Regression** (specifically count/demand regression), NOT classification.

---

### Step 1 — Start With Raw Messy Sales Data
Imagine a local Kirana store selling **Festive Mustard Oil 1L** (`Sarso Tel 1L`). In our database (`sales` table), we have **40 SKUs × 730 days (2 years) = 29,200 daily rows**:

| `sku_id` | `sold_date` | `qty` (units sold) | `unit_price` | `promo_flag` | `stockout_flag` |
|---|---|---:|---:|---|---|
| `SKU-OIL-001` | `2026-10-01` | `4` | `₹220` | `False` | `False` |
| `SKU-OIL-001` | `2026-10-02` | `3` | `₹220` | `False` | `False` |
| `SKU-OIL-001` | `2026-10-03` | `0` | `₹220` | `False` | **`True`** *(Shop ran out!)* |
| `SKU-OIL-001` | `2026-10-04` | `5` | `₹220` | `False` | `False` |
| … | … | … | … | … | … |
| `SKU-OIL-001` | `2026-10-25` | `7` | `₹220` | `True` | `False` *(Diwali in 7d)* |

#### Crucial Cleaning Step: Stockout Censoring
Look at `2026-10-03`: the shop sold `0` units **not** because customers didn't want Mustard Oil, but because the shelf was empty (`stockout_flag = True`)!
- **Naive ML mistake:** Treat `0` as true demand → model learns *"demand dropped!"* → recommends ordering *less* → causes a death spiral of future stockouts.
- **VANI-LEDGER fix:** We flag stockout days, **mask them out of training loss**, and impute them with the uncensored same-weekday rolling median when building lag features.

---

### Step 2 — Turn Time-Series Rows Into a Supervised Training Table (`X → y`)
A tree model like **LightGBM** doesn't magically read calendars; we give it **engineered features (`X`)** for every single `(sku, date)` row:

| Date (`t`) | `lag_1` (yesterday) | `lag_7` (last wk) | `roll_mean_7` | `roll_mean_28` | `dow` | `is_payday` | `days_to_festival` | `festival_weight` | `lead_time` | **`y` (Target: units sold on day `t`)** |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Oct 22 | 5 | 4 | 4.6 | 3.5 | Thu | 0 | **10** | 0.42 | 3 | **6** |
| Oct 23 | 6 | 5 | 4.9 | 3.6 | Fri | 0 | **9** | 0.45 | 3 | **7** |
| Oct 24 | 7 | 6 | 5.1 | 3.7 | Sat | 0 | **8** | 0.48 | 3 | **8** |

#### Crucial Anti-Leakage Rule: Always `.shift(1)` Before `.rolling()`
If you compute `roll_mean_7` using today's own sale `y`, the model **cheats during training** (data leakage!) and collapses in real life because you never know today's sales before today happens.
- Calling `.shift(1)` moves the entire column down by 1 day first, guaranteeing that row `t` **only ever sees days `t-1, t-2, ...`** — never `t` or the future!
- We use the **exact same `build_features()` Python function** for training, backtesting, and live prediction so training and production can never drift apart.

---

### Step 3 — Split Strictly by Time (Never Random Shuffle!)
In normal ML tutorials, people call `train_test_split(..., shuffle=True)`. **In time-series forecasting, random shuffling is illegal** because the model would train on November sales to predict October!

Instead, we slice chronologically along the calendar:

```text
|<-------------- TRAIN (Days 1 .. 674) -------------->|<-- VAL (28d) -->|<-- TEST (28d) -->|
   Model learns patterns from ~22 months of history     Tune weights &     3 walk-forward
   (all 40 SKUs pooled together in Global LightGBM)     Conformal errors   unseen 7d folds
```

- **Why Global LightGBM (All 40 SKUs Pooled)?** One single SKU only has ~670 rows, so it only sees Diwali twice. Pooling all 40 SKUs gives LightGBM **~27,000 rows** so it learns shared festival, payday, and weekend curves across the entire store — even for newer SKUs!

---

### Step 4 — Train Multiple Models (`Tweedie`, `Quantile`, `ETS/STL`, `Naive`)
How does **LightGBM** actually train?
1. It builds hundreds of small **Decision Trees** one after another (**Gradient Boosting**).
2. Tree #1 makes rough predictions (e.g., *"If `roll_mean_28 > 3` and `days_to_festival <= 10`, predict 6.5"*).
3. Tree #2 looks at the **errors (residuals)** Tree #1 made and tries to correct them.
4. Tree #3 corrects the remaining errors, and so on for ~200 trees, stopping early if Validation error stops improving.
5. **Why `objective="tweedie"`?** Retail demand is always `≥ 0` and often has zeros (slow-moving items). Standard squared-error assumes bell-curve errors that can go negative. **Tweedie regression** is specifically designed for non-negative count + zero-inflated retail demand (it powered the winning models of the Kaggle M5 Walmart forecasting competition).
6. **Why separate `quantile` models (`α=0.50` and `α=0.90`)?** The seller doesn't just want the average (`p50`); they need to know the **worst-case peak demand (`p90`)** so they don't stock out during Diwali!

We also train two statistical baselines:
- **Seasonal Naive** (average of the same weekday over the last 4 weeks) — the yardstick to prove ML actually adds value.
- **STL / Holt-Winters ETS Decomposition** — smooth statistical baseline that acts as an ensemble partner and automatic fallback.

---

### Step 5 — How We "Amplify" Predictions (5-Stage Pipeline)
A single raw ML model is good; an **amplified forecasting pipeline** turns a good model into an industry-grade one:

1. **Stage 1 — Weighted Ensemble:** Combine `LightGBM + ETS/STL + Seasonal Naive` using inverse-WAPE weights learned strictly on the Validation window (clamped between `0.10` and `0.80`). Combining tree models with statistical trend models reduces variance and stabilizes noisy SKUs.
2. **Stage 2 — Per-Category Bias Correction:** Check if a category (e.g., Dairy) was systematically over-predicted by `+4%` on Validation; subtract that bias *only if* it lowers Validation WAPE.
3. **Stage 3 — Hierarchical Reconciliation:** Ensure individual SKU forecasts reconcile coherently with their Category total forecast (so SKU-level noise cancels out).
4. **Stage 4 — Split Conformal Prediction Intervals:** Take the real forecast errors $r_i = y_{\text{actual}} - \hat{y}_{\text{predicted}}$ from the Validation window. Instead of assuming a textbook bell curve, use the **actual empirical 10th and 90th percentiles** of those real errors to compute `p10` and `p90` AND the live stockout probability curve!
5. **Stage 5 — Human-in-the-Loop Override Learning:** When a seller **commits** an override (e.g., *"Local wedding nearby, ordering +10 more"*), we log it cleanly so future cycles can incorporate a bounded per-SKU adjustment (never learning from unaccepted drafts).

---

### Step 6 — How Explainable AI (XAI) & Answer Amplification Work in the Output
Instead of dumping a black-box number (`"Order 48"`), VANI-LEDGER amplifies every answer into **five transparent layers**:

1. **SHAP (`shap.TreeExplainer`):** Game-theoretic math that walks down every LightGBM decision tree and calculates the exact credit (in units) each feature contributed to pushing the prediction up or down from the store baseline.
2. **Economic Grouping + Policy Buffer:** We group raw technical features into three seller-friendly drivers + the deterministic safety buffer:
   - `Base weekly velocity` (baseline + `lag_*` + `roll_mean_*`) → **`+24 units`**
   - `Diwali pre-surge` (`days_to_festival` + `festival_weight` + `payday`) → **`+16 units`**
   - `3-day supplier lead buffer` (exact safety stock for 3-day delivery time) → **`+8 units`**
   - Reconciled with largest-remainder rounding so **`24 + 16 + 8 = 48` ALWAYS matches the headline order!**
3. **Honest Uncertainty:** Shows **`91%` In-Stock Service Level** (`1 − 9% stockout risk`) + conformal interval (`p10–p90`) + `Data Sufficiency` pill (`High / Medium / Low`).
4. **Counterfactual Override Amplification (`< 50 ms`):** Dragging the slider from `48 → 35` instantly calculates from the cached conformal error distribution: **`₹1,950 Cash Freed` · `+14% Stockout Risk (9% → 23%)` · `5.2 d Shelf Cover`**.
5. **Grounded Plain-Language Narrative:** Generated from a deterministic template offline (or optionally phrased by Gemini Flash), then checked by a **Python Regex Numeric Verifier** — if an LLM invents ANY number not present in Python's JSON payload, it is automatically rejected!

---

### Step 7 — How We Prove the Model Works (Honest Backtest)
We run `eval/backtest.py --gate` on the **unseen 28-day holdout test window** across 3 rolling 7-day folds and measure:
- **WAPE** (`Sum of |Actual - Predicted| / Sum of Actual`) — much more honest than MAPE because zero-sale days don't cause divide-by-zero explosions! Reported overall (revenue-weighted) AND split by **A / B / C** SKU tiers.
- **MASE** (`Model Error / Naive Error`) — anything `< 1.0` mathematically beats naive guessing.
- **Pinball Loss** at `p50` and `p90` — measures how accurate the upper risk band is.
- **Bias** — confirms we aren't systematically overstocking or understocking.

---

## Part 2: Endpoint API & Input Sanitization (SQL as Constant Strings, NOT Variables!)

Why do judges and production engineers care so much about *"database queries as constant strings, not variables"*?

### The Attack (SQL Injection)
If a route takes `sku_id` from a URL and glues it into SQL with an f-string:
```python
# DANGEROUS — NEVER DO THIS!
query = f"SELECT * FROM sales WHERE sku_id = '{sku_id}'"
session.execute(text(query))
```
An attacker sends `sku_id = "' OR 1=1; DROP TABLE sales; --"` and reads every merchant's data or deletes the ledger!

### How VANI-LEDGER Prevents It (3 Layers of Defense)
1. **Constant SQL Strings + Bound Parameters (SQLAlchemy 2.0):**
   ```python
   # SAFE (Option A: SQLAlchemy ORM — automatically parameterized)
   stmt = select(Sale).where(
       Sale.sku_id == sku_id,
       Sale.merchant_id == merchant_id,  # comes from API key, NEVER request body
   )

   # SAFE (Option B: Constant raw SQL string + separate bound dictionary)
   session.execute(
       text("SELECT * FROM sales WHERE sku_id = :sku_id AND merchant_id = :mid"),
       {"sku_id": sku_id, "mid": merchant_id},
   )
   ```
   The database driver compiles the SQL structure **first** from the constant string, and treats `:sku_id` strictly as inert data — it can NEVER execute as SQL code.
2. **Strict Input Sanitization (Pydantic v2):**
   - `ConfigDict(extra="forbid", str_strip_whitespace=True)` blocks unexpected fields (stops mass-assignment attacks like sneaking `"merchant_id": "other-shop"` into JSON).
   - Strict bounds (`0 <= qty <= 100_000`), string `max_length`, Unicode NFKC normalization, and binary magic-byte + Pillow re-encoding on every uploaded bill image.
3. **Rate Limiting & Tenant Scoping:**
   - `slowapi` enforces per-merchant limits (`429 Too Many Requests` + `Retry-After`).
   - Requesting another merchant's SKU returns `404 Not Found` (never leaking whether that SKU exists).

---

## Part 3: Cheat-Sheet Answers for Judges

| Judge Question | Confident, Honest Answer |
|---|---|
| *"Isn't this just an LLM wrapper?"* | "No — LLMs never touch a single number. `forecast_core/` is pure Python (LightGBM + STL + SHAP + Conformal Prediction), and we have an automated AST unit test (`test_no_llm_in_core.py`) that fails CI if any LLM SDK is imported into it." |
| *"How did you avoid data leakage in your ML model?"* | "Strict chronological splits (Train → 28d Validation → 3-fold Walk-Forward Test), a single shared `build_features()` pipeline, `.shift(1)` before every rolling statistic, and explicit masking of stockout-censored days." |
| *"How do you know your 91% confidence isn't made up?"* | "It isn't an LLM guess — it's the calibrated **In-Stock Service Level** `P(D ≤ S*)` computed from our Split Conformal validation residuals, shown right next to the empirical `p10–p90` interval and our real held-out WAPE/MASE backtest." |
| *"Why SQLite tonight instead of Postgres + Redis + Docker?"* | "Everything runs behind SQLAlchemy 2.0 repositories and clean adapter interfaces so tonight's 5-hour live demo works 100% locally and offline with zero daemon failure risk. If selected for Finals, swapping to Postgres 17 + `pgvector`, Redis, Docling/Qwen-VL, and Next.js 16 requires changing configuration and adapters — zero rewrite of the ML core or API contracts." |
