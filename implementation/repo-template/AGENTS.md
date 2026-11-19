# AGENTS.md — VANI-LEDGER

Canonical project rules for any coding agent. `GEMINI.md` imports this file.

**Product:** explainable demand forecasting + multimodal inventory copilot for small sellers (AARAMBHATHON 2.0, PS 01).

**Scope:** forecasting, reorder quantity, budget/shelf-life/lead-time constraints, explanations, override simulation, multimodal capture, offline mode. **Not** payments, credit scoring, GST, accounting, multi-store ERP.

---

## Golden rules

1. **LLMs do perception and language only.** No LLM computes, adjusts, or invents a number.
2. **`services/forecast-core` must not import any LLM SDK.** A test enforces this. It is the only implementation of the math.
3. **SQL text is constant; values are bound parameters.** No f-strings, `%`, `.format()`, or concatenation. Dynamic identifiers are allowlisted.
4. **Every route needs auth, tenant scoping, a strict schema, and a rate limit.** No exceptions for "internal" routes.
5. **Time-ordered splits only.** Never shuffle time-series data. No feature may use future information.
6. **Report WAPE, MASE, and pinball loss.** "90% accuracy" is only valid with a named segment and metric (`1 − WAPE`).
7. **The seller is the final authority.** Every recommendation has drivers + a narrative + a simulated override.
8. **No secrets, raw merchant data, or model binaries in git.**
9. **Contracts freeze by hour 4.** Generate Python and TypeScript types from one schema.
10. **One owner per directory.** Agents work only inside their assigned directory.

---

## Commands

```bash
# setup (Python 3.12 — several ML wheels lag 3.14)
uv python pin 3.12
uv sync
cp .env.example .env
docker compose up -d postgres redis
uv run alembic upgrade head
uv run python -m scripts.seed

# backend
uv run uvicorn apps.api.main:app --reload --port 8000
uv run python -m services.mcp_ledger.server
uv run python -m services.worker.main

# frontend
pnpm --dir apps/web install
pnpm --dir apps/web dev

# verification (run after every change)
uv run pytest -q
uv run pytest tests/security -q
uv run ruff check . && uv run mypy .
uv run python eval/backtest.py --gate
pnpm --dir apps/web lint && pnpm --dir apps/web typecheck

# single test
uv run pytest tests/unit/test_policy.py -q
```

---

## Architecture

```
apps/web        Next.js 16 PWA — the only public UI
apps/api        FastAPI gateway — auth, rate limits, tenant scoping
services/forecast-core   pure Python: features, models, policy, explain, backtest
services/mcp-ledger      FastMCP v4; imports forecast-core; internal only
services/capture         Docling/VLM, faster-whisper, validation
services/worker          async jobs: capture, nightly forecast
packages/contracts       JSON Schema → generated py/ts types
data/seed, data/fixtures seed generator, bills/audio, expected JSON
eval/                    backtest + extraction eval + gates
```

- The browser **never** calls the MCP server or the database.
- API and MCP both import `forecast-core`; never duplicate formulas.
- Postgres is the source of truth; SQLite is an offline mirror for the demo.
- Redis: rate limits, cache, quotas. Not a source of truth.

### Frozen tool contracts

```python
ingest_bill_ledger(source, image_ref=None, line_items=None) -> BillResult
forecast_sku_demand(sku_id, as_of, horizon_days=14) -> ForecastResult
explain_forecast_drivers(sku_id, forecast_id) -> ExplanationResult
simulate_manual_override(sku_id, qty, as_of) -> OverrideResult
```

Versioned, idempotent, typed, no LLM inside.

---

## Database

- Money is `NUMERIC`, never `float`. Timestamps are UTC; display IST.
- Every tenant table has `merchant_id`; every query filters it from the auth context, never the body.
- Indexes: `sales(sku_id, sold_at)`, `forecasts(sku_id, as_of desc)`, `overrides(sku_id, created_at desc)`.
- Alembic migrations only; no `create_all()` outside tests.
- App role has DML only; the MCP/analytics role is `SELECT` only.

---

## ML

- Target: daily units per SKU. Horizon 14 days, direct multi-horizon rows.
- Global LightGBM: `tweedie` for the mean, `quantile` (α = 0.5, 0.9) for intervals.
- Baselines: seasonal naive + Holt-Winters/ETS. Serve the baseline if ML loses on validation.
- Features: lags 1/7/14/28, rolling 7/14/28, DOW, month, payday window, festival flag + signed distance, price/promo, lead time, shelf life, category, SKU id, stockout history, ADI/CV².
- Stockout censoring: flag stockout days; observed sales = min(demand, stock).
- Ensemble → bias correction → hierarchical reconciliation → conformal intervals → human feedback (in that order).
- Gates: A-items WAPE ≤ 12%, aggregate ≤ 15%, ≥ 20% better than naive. Report per segment.
- Pin `model_version`, feature hash, and training window with every forecast.

---

## Security

- Pydantic v2: `extra="forbid"`, explicit bounds, `UUID`/`date`/`Decimal` types, `max_length` on every string.
- `merchant_id` from the token only. IDOR returns 404.
- Uploads: allowlist MIME + magic bytes, ≤ 10 MB, ≤ 36 MP, re-encode with Pillow, UUID filename outside web root.
- OCR/ASR text is untrusted data. Fixed system prompt, delimited document block, schema-validated output, arithmetic recomputed in Python.
- Rate limits: read 120/min, write 30/min, capture 6/min + 60/hour + 200/day, auth 10/min. Redis-backed. Return 429 + `Retry-After`.
- LLM/vision: 20 s timeout, 1 retry, circuit breaker, per-merchant daily budget, cache by file hash, max 4 concurrent vision calls.
- Errors: generic client message + `request_id`; no stack traces or SQL.
- Secrets in `.env`; `gitleaks` clean; `pip-audit`/`npm audit` in CI.

Full detail: `docs/SECURITY.md` and `SECURITY_CHECKLIST.md`.

---

## Testing gates

A merge is blocked if any of these fail:

```bash
uv run ruff check . && uv run mypy .
uv run pytest -q
uv run pytest tests/security -q
uv run python eval/backtest.py --gate
pnpm --dir apps/web lint && pnpm --dir apps/web typecheck
```

Required security tests: SQLi, IDOR, oversized upload, wrong magic bytes, prompt injection, 429 threshold, mass assignment.

---

## Phase discipline (30 h)

| Phase | Hours | Focus |
|---|---|---|
| 1 | 0–4 | schema, contracts, auth, rate limits, tool stubs |
| 2 | 4–10 | features, LightGBM, ETS, SHAP, backtest |
| 3 | 10–16 | bill/voice capture, validation, review UI |
| 4 | 16–22 | dashboard, explanation, override sandbox |
| 5 | 22–27 | offline, load test, security tests, quotas |
| 6 | 27–30 | freeze, benchmark, rehearsal, backup video |

**Cut lines:** LiveKit → MediaRecorder; React Flow → static bars; local VLM → cloud + fixtures; multi-store → single store. **Never cut:** deterministic math, explanation, override, offline demo, input validation.

---

## Pointers

- `ENGINEERING_HANDOFF.md` — full design, ML, security, plan.
- `SECURITY_CHECKLIST.md` — code-level checklist.
- `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/DEMO.md` — keep in sync with code.
