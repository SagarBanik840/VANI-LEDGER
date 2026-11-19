# VANI-LEDGER — Build Proposal & Implementation Plan

**Problem Statement:** AARAMBHATHON 2.0 · PS 01 — Small Seller Demand Forecasting
(FinTech, Commerce & Financial Inclusion)

**Status:** Proposal for implementation. Deck submitted; this document is the engineering starting point.

**Date:** 19 November 2026

---

## 1. What we are actually solving

PS 01 asks for: *a forecasting and inventory recommendation system using historical sales, seasonality and basic business inputs, with explanations and manual overrides.*

The real job-to-be-done is sharper than "forecast demand":

> **Every reorder cycle, a small seller must decide how much of each SKU to buy — with incomplete records, thin cash, and seasonal/perishable demand. Today that decision is gut feel, a paper ledger, or a spreadsheet.**

Three failures make that decision bad:

| Failure | Consequence |
|---|---|
| **Capture friction** — sales live in handwritten *kaccha parchis*, UPI alerts, voice notes, cash | 85% of history never becomes clean time-series data |
| **Black-box tools** — ERP requires rigid manual entry; generic "AI" gives numbers with no reasoning | Seller can't trust or act on the output; 32% capital sits in deadstock |
| **No safe override** — no way to test "what if I order less?" | 18% revenue lost to peak/seasonal stockouts; seller ignores the tool |

So the product is not a forecasting model. It is a **decision loop**: capture → forecast → explain → simulate override → commit.

**In scope:** demand forecasting, reorder quantity, constrained ordering (budget/shelf life/lead time), explanations, override simulation, multimodal capture, offline operation.

**Out of scope (say it explicitly):** executing UPI payments, lending/credit scoring, GST filing, full accounting, multi-store ERP. Keeping this boundary is what makes 30 hours survivable.

---

## 2. The thesis: separate perception from calculation

This is the technical moat and the trust story:

- **LLMs do perception and language only** — read bills, transcribe Hinglish voice, phrase explanations.
- **Python does every number** — forecasts, safety stock, cash impact, risk. Deterministic, versioned, testable.
- **The seller is the final authority** — every recommendation is explainable and overridable, and every override is simulated before commit.

A generic LLM wrapper collapses these into one prompt, which is exactly why it hallucinates quantities. VANI-LEDGER keeps them physically separate (separate services, separate contracts), and that separation is demoable.

---

## 3. The solution, end to end

```
Bill photo / voice / CSV / UPI alert
        │
        ▼
[Capture]  Docling + VLM (vision) · faster-whisper (voice) · column mapper (CSV)
        │  → structured JSON, confidence, arithmetic check
        ▼
[Normalize]  Pydantic schemas · SKU/supplier entity resolution (pgvector + BM25 + fuzzy)
        │
        ▼
[Forecast]  STL / Holt-Winters + LightGBM quantiles + festival calendar
        │  → p50 / p90 demand, top drivers
        ▼
[Explain]  SHAP contributions → plain-language "why this order"
        │
        ▼
[Decide]  constrained order qty + override simulator (<50 ms)
        │  → cash locked, stockout risk, days cover, spoilage risk
        ▼
[Act]  seller confirms / overrides → ledger write → re-forecast
        │
        ▼
FastMCP v4 tools ←→ Next.js PWA (online) ←→ SQLite + Ollama (offline)
```

### The four MCP tools (freeze these names — they're in the deck)

```python
ingest_bill_ledger(source, image_ref=None, line_items=None) -> {
  document_id, line_items[], math_check{ok, delta}, confidence, needs_review
}

forecast_sku_demand(sku_id, as_of, horizon_days=14) -> {
  p50[], p90[], model_version, drivers[]
}

explain_forecast_drivers(sku_id, forecast_id) -> {
  top_drivers[{name, contribution_units, direction}], narrative
}

simulate_manual_override(sku_id, qty, as_of) -> {
  cash_locked_delta, stockout_risk, days_cover, spoilage_risk, within_budget
}
```

Versioned (`v2.0.0`), idempotent, typed, no LLM inside them.

---

## 4. The math you need to research and implement

This is the part most hackathon teams hand-wave. Don't.

### 4.1 Forecast core

1. **Clean:** daily SKU series, zero-fill, outlier handling, and **stockout censoring** — observed sales = min(demand, stock), so flag stockout days or you'll systematically under-forecast.
2. **Baselines:** seasonal naive (same weekday last 4 weeks) and STL decomposition (trend + weekly seasonality + residual). Holt-Winters/ETS for smooth SKUs.
3. **ML:** LightGBM on `log1p(qty)` with:
   - lags 1/7/14/28, rolling mean/std 7/14/28
   - day-of-week, month, payday window (1–5, 25–31)
   - festival flag + **distance-to-festival** (not just the flag)
   - price/promo, lead time, shelf life
   - **quantile objectives** (α = 0.5, 0.9) or mean + residual quantiles
4. **Reconcile** SKU → category if time allows (M5-style hierarchical).

### 4.2 Order policy

- Safety stock: `SS = z_α · σ_d · √L` (or quantile forecast over lead time)
- Reorder point: `ROP = d̄·L + SS`
- Order-up-to: `S* = forecast(L + R) + SS`
- Order qty: `q = clamp(S* − on_hand − on_order, 0, …)`
- **Perishable cap:** `q ≤ forecast demand over shelf life` (or newsvendor with salvage)
- **Budget cap:** `Σ c_s·q_s ≤ B` → greedy by benefit-per-rupee or `scipy.optimize.linprog` for ≤50 SKUs
- **Newsvendor critical ratio:** `CR = (p−c)/((p−c)+(c−salvage))` → order the CR quantile

### 4.3 Override metrics (must be exact and fast)

- `cash_locked_delta = (q_manual − q*) · unit_cost`
- `stockout_risk = P(D_{L+R} > on_hand + q_manual)` from the predictive distribution
- `days_cover = (on_hand + q_manual) / forecast_daily_rate`
- `spoilage_risk = P(D_{shelf_life} < on_hand + q_manual)` for perishables
- Precompute quantile grids in memory; **no DB call on the hot path** → p95 < 50 ms is realistic.

### 4.4 Explanation

SHAP on the LightGBM model gives additive contributions. Map the top 3 to templates: *base velocity + festival uplift + lead-time buffer* (exactly the +24 / +16 / +8 story in the deck). The LLM only rephrases numbers it is given — it never computes them.

### 4.5 Evaluation

Rolling-origin backtest, last 8 weeks holdout:

- **WAPE** (primary), MASE, pinball loss at p50/p90
- service level achieved, forecast bias
- compare: seasonal naive → ETS → LightGBM
- target: **WAPE ≤ 15%** on top SKUs, or ≥20% better than seasonal naive. The deck's 36% → 14% is simulated; a real backtest is what makes it defensible.

---

## 5. Data plan

| Source | Use |
|---|---|
| Kaggle **Store Item Demand** (5 yrs, 10 stores × 50 items) | main backtest, daily seasonality |
| **UCI Online Retail II** | SKU/category realism, returns, price |
| **Indian festival calendar** (Diwali, Holi, Eid, Navratri, Pongal, Onam, Raksha Bandhan, Christmas + local holidays) | uplift features |
| **Synthetic kirana generator** (you write) | the actual demo: 40–60 SKUs, Hinglish names, perishables, lead times, budget cap |

Schema (minimum):
`sku`, `sales` (with `stockout_flag`, `source_doc_id`), `inventory`, `supplier`, `calendar`, `documents`, `forecasts`, `overrides`.

Demo fixtures to prepare **before** the event: 3 bill images (2 printed, 1 handwritten), 2 Hinglish voice clips, and the exact expected JSON — so capture can be tested without live improv.

---

## 6. Tech stack: minimum vs full

| Layer | Minimum demo stack (must) | Full stack (if time) | Cost |
|---|---|---|---|
| Web | Next.js 16 + TS + Tailwind v4 | + React Flow driver graph, Recharts | ₹0 |
| API | FastAPI + Pydantic | LangGraph state machine | ₹0 |
| Tools | **FastMCP v4**, 4 tools | + `get_reorder_plan`, `reconcile_ledger` | ₹0 |
| Forecast | pandas/NumPy, statsmodels (STL/ETS), LightGBM, SHAP | hierarchical reconciliation, conformal intervals | ₹0 |
| Vision | Docling + **Gemini Flash** structured output | Qwen2.5-VL via Ollama (offline) | ₹0–1,500 |
| Voice | browser MediaRecorder → faster-whisper | LiveKit WebRTC + Kokoro TTS | ₹0 |
| Data | Postgres 17 + pgvector (Docker) | + SQLite offline mirror | ₹0 |
| LLM routing | EasyCLIProxy → Gemini Flash | + local Ollama fallback | ₹0–1,500 |
| Infra | Docker Compose, uv, pnpm, pytest, Playwright | CI, seeded demo mode | ₹0 |

**Realistic total: ₹0–₹1,500.** No paid blocker. The only hardware constraint is local VLM: 8–12 GB VRAM for Qwen2.5-VL 7B; otherwise use cloud VLM for the demo and keep Ollama for text-only fallback.

**Effort estimate (hand-written equivalent):**

| Module | LOC | AI-agent time |
|---|---|---|
| `forecast-core` (math, backtest, SHAP) | ~1,500 | 8–10 h |
| `mcp-ledger` (4 tools + schemas) | ~800 | 4–5 h |
| `capture` (vision + voice) | ~1,200 | 5–6 h |
| `api` gateway | ~600 | 3 h |
| `web` (decision card, override, dashboard) | ~2,500 | 8–10 h |
| eval/infra/seed | ~700 | 3–4 h |

~7–8k LOC equivalent; AI generates 2–3× that including tests. Four builders + agents in parallel makes 30 hours feasible **only if contracts are frozen by hour 4**.

---

## 7. When: prep + 30-hour plan

Event window is **Nov 20–21** (30 hours). Today is Nov 18 — so:

### T-2 prep (Nov 19, ~1hour)

1. Create repo, `AGENTS.md`, `docker-compose`, `.env.example`; verify Python 3.12 + uv, Node 20 + pnpm, Docker, Postgres+pgvector.
2. Freeze JSON contracts for the 4 MCP tools; generate Python + TS types.
3. Download Kaggle + UCI data; write loaders + synthetic kirana generator.
4. Build the 5 golden fixtures (3 bills, 2 audio) with expected JSON.
5. Get a **seasonal-naive + ETS baseline + backtest** running end to end — no UI.
6. UI shell with mock data and the Seller Decision Card component.
7. Write the demo script and record a fallback video.

### Event (24 h, but ~8h build time ) — aligns with the deck's six phases

| Phase | Hours | Deliverable | Exit criterion |
|---|---|---|---|
| 1 Schema lock | 0–4 | migrations, contracts, 4 tool stubs on fixtures | `curl` returns valid JSON for all 4 tools |
| 2 Forecast core | 4–10 | STL/ETS + LightGBM + backtest + SHAP | WAPE report on held-out weeks |
| 3 Capture | 10–16 | bill → JSON, voice → JSON, math check | 20-bill eval set scored |
| 4 Product | 16–22 | dashboard, decision card, override sandbox, voice loop | full loop clickable |
| 5 Offline | 22–27 | SQLite mirror, Ollama fallback, error states | demo runs with Wi-Fi off |
| 6 Freeze | 27–30 | benchmarks, rehearsal, backup video | 3 clean demo runs |

**Parallel ownership (4 people):**

- **P1 Platform:** contracts, DB, MCP server, offline, Docker
- **P2 Math:** forecast, backtest, SHAP, override simulator
- **P3 Capture:** Docling/VLM, Whisper, entity resolution, review UI
- **P4 Product:** Next.js dashboard, decision card, demo, e2e

Integration gates at hours 10, 16, 22, 27. Freeze `main` at hour 27.

---

## 8. Repo blueprint

```
vani-ledger/
  AGENTS.md                  # contracts, commands, non-negotiables
  README.md
  docker-compose.yml         # postgres+pgvector, api, mcp, web
  apps/
    web/                     # Next.js 16 PWA
    api/                     # FastAPI gateway
  services/
    mcp-ledger/              # FastMCP v4 — the 4 tools
    forecast-core/           # pure Python; no LLM imports allowed
    capture/                 # docling, vlm, whisper, kokoro
  packages/
    contracts/               # JSON Schema → py/ts generated types
  data/
    seed/                    # synthetic kirana + public dataset loaders
    fixtures/                # bills, audio, expected JSON
  eval/
    backtest.py
    extraction_eval.py
  docs/
    adr/                     # 3–5 short decisions
    demo-script.md
```

Rules that prevent merge chaos:

- `forecast-core` must not import any LLM SDK. Enforce with a test.
- All cross-service calls go through `packages/contracts`.
- One owner per directory; agents work only inside their directory.
- `AGENTS.md` states the rebuild/test commands and the "no numbers from LLMs" rule.

**First 12 issues:** scaffold+CI · contracts · migrations+seed · synthetic generator · baseline forecast · LightGBM+backtest · SHAP+narrative · MCP tools · override simulator+latency bench · bill pipeline+eval · voice loop · dashboard+offline.

---

## 9. Definition of done (what "it works" means)

- Backtest WAPE ≤ 15% on top 20 SKUs, better than seasonal naive by ≥20%
- Extraction field F1 ≥ 0.90 on the 20-bill eval set
- Override simulation p95 < 50 ms, with unit tests on cash/risk math
- Full demo runs with network disabled
- Every recommendation has ≥3 numeric drivers + one plain-language sentence
- Same inputs → same outputs (pinned model version, seeded)
- No real payment execution, no real merchant data

**3-minute demo:** crumpled bill on camera → structured line items with confidence → 48-unit forecast with +24/+16/+8 drivers → override to 35 → ₹1,950 freed, +14% stockout risk, 5.2 days cover → Wi-Fi off → same query via local Ollama/SQLite.

---

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| VLM fails on handwriting | constrain to printed bills + 1 handwritten with human confirm; pre-extracted fixture fallback; confidence + review step |
| 50 ms target missed | precomputed quantile grid, NumPy only, no DB on hot path; measure p95 in CI |
| Forecast not credible | real rolling-origin backtest on public data; label simulated deck numbers as simulated |
| Scope creep into payments/credit | explicit out-of-scope list in README and demo |
| Venue Wi-Fi | local-first, Ollama, SQLite, cached demo state, backup video |
| Integration chaos | contracts frozen at hour 4, one owner per directory |
| Privacy | synthetic/consented data only; no real UPI IDs or merchant records |

---

## 11. Research list

- **Forecasting:** Hyndman & Athanasopoulos *FPP3* (STL, ETS, quantiles); M5 Accuracy competition findings (hierarchical, Tweedie/Poisson); WAPE/MASE/pinball loss.
- **Inventory:** newsvendor critical ratio, (R,S) policy, safety stock with lead-time variability, perishable inventory models.
- **ML:** LightGBM quantile objective docs; SHAP TreeExplainer; conformal prediction for intervals.
- **Agent/protocol:** MCP spec + FastMCP v4; PydanticAI structured outputs; LangGraph state machines (only if needed).
- **Capture:** Docling docs; Qwen2.5-VL; Gemini structured output; faster-whisper; Kokoro TTS.
- **Domain:** Indian festival calendar datasets; kirana SKU taxonomy; UPI collect-request scam patterns (for the out-of-scope boundary).

