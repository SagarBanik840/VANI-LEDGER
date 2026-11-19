# VANI—LEDGER

**Explainable Demand Forecasting & Multimodal Inventory Copilot for Small Sellers**  
*AARAMBHATHON 2.0 · Theme: FinTech, Commerce & Financial Inclusion · Problem Statement 01*

---

## Core Thesis: Separate Perception from Calculation

Small Indian sellers (kirana stores, perishable dairy booths, micro-D2C merchants) lose working capital to slow-moving deadstock and lose peak revenue to seasonal stockouts because their records live in handwritten *kaccha parchis*, voice logs, and messy sheets — while existing tools are either rigid ERPs or opaque LLM wrappers that hallucinate arithmetic.

**VANI-LEDGER enforces a strict architectural boundary:**
- **Perception & Language Only (Adapters):** Vision/OCR bill capture, Hinglish voice/text intent parsing, and narrative phrasing.
- **Deterministic Python Math (`forecast_core`):** 100% of time-series feature engineering, STL/LightGBM forecasting, SHAP driver attribution, conformal uncertainty intervals, budget/perishable optimization, and `< 50 ms` what-if override math execute in pure, versioned, tested Python — **zero LLM arithmetic**.
- **Seller as Final Authority:** Every reorder recommendation exposes its exact additive drivers and lets the seller simulate manual overrides before committing.

---

## Authoritative Architecture & Execution Plan

Read the single source of truth for the system design, ML design, verified local environment audit, security baseline, and 5-hour execution schedule:

- **[`docs/ARCHITECTURE_AND_EXECUTION_PLAN.md`](docs/ARCHITECTURE_AND_EXECUTION_PLAN.md)** — Complete System Design, ML Design, ADRs, API/FastMCP Contracts & 5-Hour Build Plan
- **[`AGENTS.md`](AGENTS.md)** — Canonical Engineering & Security Guardrails
- **[`ppt/VANI_LEDGER.pptx`](ppt/VANI_LEDGER.pptx)** — Submitted 12-Slide Presentation Deck

### Chosen Industry-Standard Hackathon Stack (Local-First, Zero External Daemons)
| Layer | Technology | Production Upgrade Path |
|---|---|---|
| **Runtime** | Python `3.12` via `uv` | Same (`uv` lockfile + slim container) |
| **ML & Math Core** | `numpy`, `pandas`, `scipy`, `statsmodels`, `lightgbm`, `shap` | Same pure-Python `forecast_core` package |
| **API & Contracts** | FastAPI + Pydantic v2 (`extra="forbid"`) + `slowapi` | Behind Caddy TLS + Redis rate-limit store |
| **MCP Tool Plane** | FastMCP (4 frozen deterministic tools sharing `forecast_core`) | Same internal Streamable HTTP service |
| **Persistence** | SQLAlchemy 2.0 + SQLite (WAL mode) | 1-line `DATABASE_URL` swap to PostgreSQL 17 |
| **Multimodal Capture** | Adapter interface (Gemini Flash VLM / Local Tesseract `eng` / Golden Demo Fixture) + CSV + Web Speech/Hinglish parser + Python `Decimal` math audit | Swap in Docling + Qwen2.5-VL + faster-whisper |
| **Seller Cockpit UI** | FastAPI + Jinja2 + Zero-CDN Modular Vanilla JS/CSS (Obsidian & Gold editorial theme) | Port to Next.js 16 against frozen `/v1/*` OpenAPI |

---

## Four Frozen FastMCP Deterministic Tools
1. `ingest_bill_ledger()` — validates & audits structured bill line items in exact `Decimal` arithmetic
2. `forecast_sku_demand()` — deterministic p10/p50/p90 seasonal demand forecast + driver breakdown
3. `explain_forecast_drivers()` — reconciled SHAP/policy unit drivers + number-verified plain-language narrative
4. `simulate_manual_override()` — sub-50ms in-memory cash-freed, stockout-risk, days-cover, spoilage & budget simulation

---

## Repository Map

```text
VANI-LEDGER/
├── README.md                                 # Project overview & Rule 38 credits
├── AGENTS.md                                 # Canonical build & security guardrails
├── docs/
│   └── ARCHITECTURE_AND_EXECUTION_PLAN.md    # Authoritative architecture, ML & 5h build plan
├── ppt/
│   └── VANI_LEDGER.pptx                      # Official 12-slide hackathon deck
├── Event/                                    # Official Aarambhathon 2.0 Rule Book & PPT format
├── Themes_& Problem_Statements/              # Official PS 01 specification
├── Our_Project/                              # Archived early proposals & design references
├── Gemini_Research/                          # Archived pre-PS01 architectural brainstorm
└── implementation/                           # Archived 30h handoff, security reference & draft session plan
```

---

## Open-Source Attribution (Rule 38 Compliance)

In compliance with **AARAMBHATHON 2.0 Rule 38**, VANI-LEDGER credits all external libraries, protocols, tools, and datasets:
- **Forecasting, Optimization & XAI:** Python 3.12, NumPy, pandas, SciPy, statsmodels (STL/ETS), LightGBM, SHAP
- **API, Validation & Protocol:** FastAPI, Uvicorn, Pydantic v2, FastMCP (Model Context Protocol SDK), SQLAlchemy 2.0, slowapi
- **Capture & Sanitization:** Pillow, Tesseract OCR, optional Google Gemini Flash API
- **Data & Calendar Benchmarks:** Synthetic Indian retail/kirana generator (deterministic seeded), Indian festival & holiday calendars, Kaggle Store Item Demand / UCI Online Retail II reference benchmarks
- **Compliance Notice (Rule 39):** Slide 9 projected operational gains (`32% → 12%` deadstock, `18% → 5%` stockout, `36% → 14%` MAPE) are explicitly labeled as simulated retail projections. Runtime backtest metrics reported by `eval/backtest.py` are strictly measured on held-out rolling time-series splits, and fixture-backed demo bills are explicitly badged `DEMO DOCUMENT`.
