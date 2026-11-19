# VANI—LEDGER · 3-Minute Live Stage Demo Runbook & Q&A Cheatsheet

**Event:** AARAMBHATHON 2.0 · FinTech, Commerce & Financial Inclusion (**PS 01: Small Seller Demand Forecasting**)  
**Core Thesis:** *Separate Perception from Calculation — LLMs/VLMs handle capture and phrasing only; 100% of forecasts, attributions, and money arithmetic run deterministically in Python.*

---

## Pre-Stage Checklist (30 Seconds Before Going Live)

1. Run the one-command clean deterministic reset (< 15 s):
   ```bash
   uv run python scripts/reset_demo.py
   ```
   Confirm terminal output shows:
   - `40` primary SKUs × `730` days (`29,200` primary sales rows, ~3% stockout censoring) + Indian festival calendar (`2024–2026`, `as_of = 2026-10-31`, 8 days before Diwali 2026).
   - Trained forecasts persisted for all 40 SKUs; Hero SKU `SKU-OIL-001` (`Festive Mustard Oil 1L`) = **`48 units`** (`+24` base · `+16` Diwali pre-surge · `+8` 3-day lead buffer, `91%` service level).
   - Backtest gate **`PASS`** written to `eval/latest_backtest.json`.
2. Start the local server (zero external daemons required):
   ```bash
   uv run uvicorn vani_ledger.api.main:app --host 127.0.0.1 --port 8000
   ```
3. Open `http://127.0.0.1:8000` in Chrome/Firefox at 100% zoom. Verify the emerald **`OFFLINE READY`** pill is live and **Case 01 · Mustard Oil** loads `48` in antique gold.

---

## Exact 3-Minute Live Demo Script

### 0:00 – 0:30 · Opening & Seller Decision Card (Slide 5 Live)
- **On screen:** Obsidian & gold cockpit at `http://127.0.0.1:8000`, hero SKU **`Festive Mustard Oil 1L`** (`Sarso Tel 1L`).
- **Say:**
  > "Small sellers don't reject spreadsheets because they lack intelligence — they reject black boxes that can't explain *why* to lock up ₹7,200 of cash right before Diwali. VANI-LEDGER separates perception from calculation: AI reads bills and Hinglish notes, while 100% of forecasting and rupee math runs deterministically in Python."
- **Point out on the left Decision Card:**
  - `01 / CAPTURE`: `Sarso Tel 1L · FastMCP Verified`, cost `₹150`, lead time `3d`, shelf life `90d`, Hinglish note `"Diwali week stock add karo, shelf life 90d"`.
  - `02 / FORECAST`: **`48 bottles`**, **`₹7,200` outlay**, **`91%` in-stock service level**, conformal **`p10–p90`** interval, and **`HIGH DATA`** sufficiency badge.
  - **Additive XAI Driver Bars (exact sum = 48):**
    - `Base weekly velocity`: **`+24`**
    - `Diwali pre-surge`: **`+16`**
    - `3-day supplier lead buffer`: **`+8`**

### 0:30 – 1:00 · Glass-Box XAI, Real Backtest & 4 Frozen FastMCP Tools
- **Click:** **`Why this order?`** → toggle **`EN` / `Hinglish`**.
  - Point to **`NUMBERS VERIFIED`** badge: every single number in the narrative is regex-checked against the deterministic forecast JSON payload; any invented number triggers automatic fallback.
- **Click Tab 03:** **`ML Backtest & FastMCP Inspector`**.
  - Show real 3-fold chronological walk-forward holdout metrics (Rule 39 honest reporting — distinct from deck simulations): **Seasonal Naive vs STL/Calendar vs Global LightGBM vs 5-Stage Amplified Ensemble**, A/B/C segment WAPE, MASE `< 1.0`, Pinball p50/p90, and conformal p90 coverage.
  - Point to the **4 Frozen FastMCP Tools** (`ingest_bill_ledger`, `forecast_sku_demand`, `explain_forecast_drivers`, `simulate_manual_override`) sharing the exact same Python domain core as FastAPI (zero duplicated math; enforced by AST test `test_no_llm_in_core.py`).

### 1:00 – 1:45 · Sub-50ms Override Sandbox + Perishable & Budget Constraints (Slide 6)
- **On the right `03 / OVERRIDE` panel:** Drag the gold slider from **`48` → `35`** (or click **`Demo: 35 (-13)`**).
- **Point out instantly:**
  - Live latency pill: **`< 10 ms · <50ms SLA`** (in-memory conformal residual cache, zero DB hits).
  - **`₹1,950` CASH FREED** (exact Python `Decimal`: `(35 - 48) × ₹150`).
  - **`+14%` STOCKOUT RISK** (`9% → 23%`, computed smoothly from empirical conformal residuals).
  - **`5.2 d` SHELF COVER**.
- **Type a reason** (e.g., `"Holding cash for dairy delivery"`) and click **`Commit Override`** → atomic DB write to `overrides` + immutable `audit_logs` (idempotent via `Idempotency-Key`).
- **Click Hero Chip `02 · Fresh Milk`** (`SKU-DAIRY-001`, 3-day shelf life): drag slider up and watch the **Spoilage Risk** guard turn amber/crimson when exceeding 3-day perishable cover (Newsvendor critical ratio + shelf-life cap).
- **Click Tab 02 (`₹50k Weekly Budget Reorder Plan`)**: show all 40 SKUs ranked by margin-per-rupee & stockout urgency via `scipy.optimize.linprog` under `₹50,000` (change budget to `₹25,000` and click **Optimize Plan** to show live re-allocation & `BUDGET CAP` pills).

### 1:45 – 2:30 · Multimodal Capture & Deterministic ₹300 Overcharge Catch
- **Click Tab 01 (`Multimodal Capture`)**.
- **Click `Load Sample Crumpled Bill`** (uploads real `data/fixtures/demo_bill.png` bytes to `POST /v1/capture/bill`):
  - Pipeline validates magic bytes, re-encodes via Pillow to strip EXIF/polyglots, extracts 4 line items, and verifies arithmetic in Python `Decimal`.
  - Immediately flashes the pulsing red **`₹300 OVERCHARGE CAUGHT`** alert (`₹4,200.00` billed vs `₹3,900.00` verified) + amber **`DEMO DOCUMENT`** badge (Rule 39 compliance).
- **Click preset Hinglish chip** `"Aaj 12 crate doodh bika, 2 packet expire"` → parses structured `VoiceIntentPayload` (`spoilage_report`, `SKU-DAIRY-001`, `qty=12`, `spoilage=2`, `REQUIRES CONFIRMATION` guard — never auto-mutates DB).
- *(Optional 5 sec)* Click **`Load Sample D2C CSV`** to show Case 03 CSV ledger ingestion.

### 2:30 – 3:00 · True Offline Proof & Close
- **Turn laptop Wi-Fi OFF (or enable Airplane Mode).**
- **Hard-reload `http://127.0.0.1:8000` (`Ctrl+Shift+R`)**: zero CDN fonts, zero external scripts, zero cloud latency.
- Drag the override slider again (`48 → 35`) — still `< 10 ms`, still `₹1,950 Cash Freed`, still `NUMBERS VERIFIED`.
- **Closing line:**
  > "VANI-LEDGER: perception separated from calculation — explainable, overridable in under 50 milliseconds, audited in Python Decimal, and 100% offline-first for every Indian small seller."

---

## Exact Canonical Numbers Reference

| Scenario | Metric | Canonical Value | Source |
|---|---|---|---|
| **Case 01 (`SKU-OIL-001`)** | Recommended 7-day order | **`48 bottles`** (`₹7,200` outlay) | `GET /v1/forecasts/aaaaaaaa-aaaa-4aaa-8aaa-000000000001` |
| **Case 01 Drivers** | Additive SHAP + Policy | **`+24` base · `+16` Diwali · `+8` 3d lead** (`sum = 48`) | `forecast_core/explain.py` |
| **Case 01 Confidence** | In-stock service level | **`~91%`** (`p10–p90` split conformal interval) | `forecast_core/conformal.py` |
| **Case 01 Override (`35`)** | Cash Freed / Risk / Cover | **`₹1,950` · `+14%` (`9% → 23%`) · `5.2 d`** | `POST /v1/overrides/simulate` (`< 50 ms`) |
| **Case 02 (`SKU-DAIRY-001`)** | Perishable guard | `3-day` shelf life, Newsvendor critical ratio cap | `forecast_core/policy.py` |
| **Case 03 (`SKU-D2C-001`+)** | Budget optimization | Total allocated `<= ₹50,000.00` via `linprog` | `GET /v1/reorder-plan` |
| **Bill Audit (`demo_bill.png`)** | Arithmetic check | **`4` items · `₹4,200` billed vs `₹3,900` computed → `₹300` delta** (`DEMO DOCUMENT`) | `POST /v1/capture/bill` |

---

## Judge Q&A Cheatsheet

1. **Q: Isn't this just an LLM wrapper calling Gemini/GPT to predict demand?**
   - **A:** No — `src/vani_ledger/forecast_core/` has **zero LLM or HTTP SDK imports**, enforced automatically on every test run by an AST guard (`tests/unit/test_no_llm_in_core.py`). Forecasting uses a real Global LightGBM (Tweedie + Quantile α=0.50/0.90) + `statsmodels` STL/ETS + Seasonal Naive 5-stage amplified ensemble with split conformal intervals and TreeSHAP. Even with Wi-Fi off and no API key, 100% of forecasting, explanations, and overrides run locally.

2. **Q: How do you prevent time-series data leakage and stockout bias?**
   - **A:** One shared `build_features()` function is used identically for train, backtest, and serving. Every rolling statistic applies `.shift(1)` strictly before `.rolling()`. Stockout days (`stockout_flag=True`, ~3% of history) are explicitly censored from target loss and imputed with uncensored same-weekday medians for lags. Splits are strictly chronological (`Train → 28d Conformal Calibration → 3-fold Walk-Forward Holdout`), verified by `tests/ml/test_leakage_and_gate.py`.

3. **Q: Why report WAPE, MASE, and Pinball Loss instead of plain MAPE?**
   - **A:** Unweighted MAPE explodes on intermittent C-segment SKUs when daily sales are 0 or 1. Revenue-weighted WAPE weights accuracy by real rupees at stake, MASE proves we beat a seasonal-naive baseline (`< 1.0`), and Pinball Loss (p50/p90) proves our conformal uncertainty bounds are genuinely calibrated. Per Rule 39, our UI separates real held-out backtest numbers from simulated deck benchmarks and badges demo fixtures `DEMO DOCUMENT`.

4. **Q: How is the override slider under 50 ms without hardcoding numbers?**
   - **A:** During startup, empirical conformal residuals (`forecasts.residuals_json`) and SKU economics are warmed into an in-memory `ForecastSnapshotCache`. `POST /v1/overrides/simulate` evaluates a smooth empirical CDF over those real residuals in NumPy plus exact Python `Decimal` cash math with **zero database queries** — verified by a 500-call latency benchmark in `tests/unit/test_math_and_policy.py`.

5. **Q: What security controls are actually implemented (not just slides)?**
   - **A:** Every route enforces `X-API-Key` constant-time SHA-256 verification, server-side `merchant_id` tenant scoping (cross-tenant IDOR returns `404`), Pydantic v2 `extra="forbid"` anti-mass-assignment, zero SQL string interpolation (SQLAlchemy 2.0 ORM only), `slowapi` rate limiting (`429` + `Retry-After`), magic-byte + Pillow re-encoding upload sanitization (`413` >10 MB, `422` fake extensions, UUID filenames blocking path traversal), strict local CSP (`script-src 'self'`), and automated verification in `tests/security/test_security.py`.

6. **Q: How does this scale past the hackathon without a rewrite?**
   - **A:** Clean modular monolith interfaces: change `DATABASE_URL` from SQLite WAL to PostgreSQL 17, point `slowapi` storage to Redis, plug Gemini 2.5 Flash / Qwen2.5-VL into the existing `BillExtractor` adapter protocol, and point a Next.js PWA at the exact same frozen `/v1/*` OpenAPI contract and 4 FastMCP tools.

---

## Recovery Commands (If Anything Goes Sideways on Stage)

- **Reset DB + retrain + reload cache via UI:** Click **`Reset Demo`** in top-right bar.
- **Reset via terminal:** `uv run python scripts/reset_demo.py`
- **Verify all tests & lint in front of judges:**
  ```bash
  uv run ruff check .
  uv run pytest -q
  uv run python eval/backtest.py --gate
  ```
