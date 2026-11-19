# AGENTS.md — VANI-LEDGER Build Rules

Canonical engineering guardrails for building **VANI-LEDGER** (AARAMBHATHON 2.0 · FinTech, Commerce & Financial Inclusion · PS 01).  
Authoritative design specification: `docs/ARCHITECTURE_AND_EXECUTION_PLAN.md`.

---

## Non-Negotiable Engineering Rules

1. **Separate perception from calculation.** LLMs/VLMs handle capture & phrasing only; **100% of forecasts, risk math, and money arithmetic run deterministically in Python.**
2. **Zero LLM imports in `src/vani_ledger/forecast_core/`.** Enforced automatically by `tests/unit/test_no_llm_in_core.py`. Both FastAPI and FastMCP import the exact same domain functions — never duplicate formulas.
3. **Money is `Decimal` / `NUMERIC(12, 2)`, never `float`.** All DB timestamps are UTC; display in IST (`Asia/Kolkata`).
4. **Zero SQL string interpolation.** Use SQLAlchemy 2.0 ORM or parameterized `text()` binds only. Allowlist any dynamic sort keys.
5. **Security on every route from Block 1.** Pydantic v2 `ConfigDict(extra="forbid", str_strip_whitespace=True)`, `X-API-Key` auth, server-side `merchant_id` scoping (IDOR returns `404`), `slowapi` rate limits (`429` + `Retry-After`), magic-byte + Pillow re-encode for image uploads, and generic error envelopes with `request_id`.
6. **Zero time-series leakage.** Temporal splits only (never random shuffle). Shared `build_features()` for train/eval/serve. Apply `.shift(1)` before every rolling statistic. Handle stockout censoring explicitly.
7. **Honest metrics (Rule 39).** Report real rolling-origin holdout **WAPE** (revenue-weighted + A/B/C segments), **MASE**, **Pinball Loss (p50/p90)**, and **Bias**. Label any fixture-backed bill result `DEMO DOCUMENT` and any simulated deck projection as simulated.
8. **Grounded explanations.** Every number in a generated narrative must be verified against the deterministic forecast JSON payload; on any mismatch, fall back to the deterministic template.
9. **Local-first & offline-proof.** Pin Python `3.12` via `uv`. Zero required external daemons (no Docker, Postgres, Redis, pnpm, or Ollama required to run the demo). Zero external CDN fonts or scripts in the UI.
10. **Strict PS 01 scope.** In: demand forecasting, reorder recommendations, budget/shelf-life/lead-time constraints, XAI drivers, `< 50 ms` manual overrides, bill/CSV/Hinglish capture, offline operation. Out: UPI payment execution, lending/credit scoring, GST filing, full ERP.

---

## Standard Verification Commands (Once Scaffolded)

```bash
uv python pin 3.12
uv sync
uv run python scripts/reset_demo.py
uv run python eval/backtest.py --gate
uv run ruff check .
uv run pytest -q
uv run uvicorn vani_ledger.api.main:app --port 8000
```
