# VANI-LEDGER — Engineering Handoff & Build Plan v2

**For:** implementation phase, AARAMBHATHON 2.0 · PS 01
**Tooling:** Gemini CLI (migrating from OpenCode)
**Date:** 19 November 2026

This document is the single context pack for the build. It assumes the submitted deck is frozen and the repo starts fresh.

---

## 0. Read this first

Three corrections to the original plan before any code is written:

1. **"90% accurate" is the wrong target as stated.** Forecast accuracy is `1 − WAPE` (or `1 − MAPE`). A single global "90%" number is not honest: stable A-items can hit 90–92%, sparse/intermittent C-items cannot. Use tiered gates in §4.7 and report weighted WAPE. Never report "accuracy" without defining the metric.
2. **This is supervised regression, not classification.** Daily demand per SKU is a continuous/count target. Use LightGBM regression (Tweedie or quantile objectives), trained on time-ordered data. A separate binary model for stockout risk is optional, not the core.
3. **Security is a Phase 1 deliverable, not a cleanup task.** Rate limits, strict schemas, bound SQL parameters, and tenant scoping must exist before the first endpoint is exposed. Retrofitting them at hour 26 is how demos die.

---

## 1. Project context

### 1.1 Problem statement (PS 01)

> Small online and offline sellers overstock slow-moving items or run out of popular products because they lack data-driven forecasting tools. Build a simple forecasting and inventory recommendation system using historical sales, seasonality and basic business inputs. Provide explanations for recommendations and allow manual overrides.

### 1.2 Product

**VANI-LEDGER** — explainable demand forecasting and a multimodal inventory copilot for small sellers.

The decision loop:

```
Capture → Normalize → Forecast → Explain → Simulate override → Commit → Learn
```

### 1.3 Thesis (do not violate)

- **LLMs do perception and language only.** Bill reading, voice transcription, narrative phrasing.
- **Python does every number.** Forecasts, safety stock, cash impact, risk. Deterministic, versioned, tested.
- **The seller is the final authority.** Every recommendation is explainable and overridable; every override is simulated first.

A wrapper collapses these into one prompt. VANI-LEDGER keeps them physically separate: separate packages, separate contracts, a test that forbids LLM imports inside `forecast-core`.

### 1.4 Scope

**In:** forecasting, reorder quantity, budget/shelf-life/lead-time constraints, explanations, override simulation, multimodal capture, offline mode.

**Out:** UPI payment execution, lending/credit scoring, GST filing, full accounting, multi-store ERP, real merchant data.

### 1.5 Demo facts to preserve

- Demo SKU: Festive Mustard Oil 1L → recommended 48 units, drivers +24 base / +16 Diwali / +8 lead buffer.
- Override to 35 → ₹1,950 cash freed, +14% stockout risk, 5.2 days cover.
- Benchmarks in the deck are **simulated** (deadstock 32→12%, stockout 18→5%, MAPE 36→14%). The repo must produce a real backtest and label simulated numbers as simulated.

---

## 2. Architecture

### 2.1 Shape for the 30-hour build: modular monolith + internal MCP

Do **not** build microservices. Build one deployable API with clean internal packages, plus one internal MCP process that imports the same math library.

```
Browser (Next.js PWA)
        │ HTTPS, JWT/API key
        ▼
FastAPI gateway  ── Redis (rate limit, cache, quotas)
        │
        ├── forecast-core (pure Python library: math, no LLM)
        ├── capture (Docling/VLM, faster-whisper, validation)
        └── Postgres 17 + pgvector
        │
        └── MCP server (FastMCP v4, internal only) ── same forecast-core
```

Rules:

- **The browser never talks to the MCP server.** MCP is an internal/agent-facing surface.
- **`forecast-core` is the only implementation of the math.** FastAPI and MCP both import it; no duplicated formulas.
- **One Postgres.** SQLite is an offline mirror for the demo, not a second source of truth.
- **Synchronous fast path:** forecast read and override simulation. **Asynchronous slow path:** bill/voice capture, batch retraining.

### 2.2 Request paths

| Path | Flow | Target latency |
|---|---|---|
| Read forecast | API → Redis → `forecasts` table | p95 < 80 ms |
| Override simulation | API → NumPy quantile grid (in memory) | p95 < 50 ms |
| Bill capture | API → job row → worker → VLM → validate → review | 2–6 s, `202 Accepted` |
| Voice capture | API → upload → faster-whisper → intent parse → confirm | 2–5 s |
| Reorder plan | API → `forecast-core` → budget allocation | p95 < 300 ms |
| Nightly forecast | worker → all SKUs → upsert `forecasts` | < 5 min for demo |

### 2.3 Data model (minimum)

```sql
merchant(id uuid pk, name text, timezone text default 'Asia/Kolkata', created_at timestamptz)
sku(id uuid pk, merchant_id uuid fk, name text, category text, unit text,
    unit_cost numeric(12,2), price numeric(12,2), shelf_life_days int,
    lead_time_days int, moq int, pack_size int, perishable bool, active bool)
supplier(id uuid pk, merchant_id uuid fk, name text, lead_time_days int)
sales(id bigserial pk, merchant_id uuid, sku_id uuid, sold_at timestamptz,
      qty numeric(12,3), unit_price numeric(12,2), channel text,
      stockout_flag bool default false, source_doc_id uuid, created_at timestamptz)
inventory(sku_id uuid, as_of date, on_hand numeric(12,3), on_order numeric(12,3),
          primary key (sku_id, as_of))
calendar(date date pk, festival text, festival_distance int, payday_window bool, holiday bool)
documents(id uuid pk, merchant_id uuid, kind text, storage_key text,
          extracted_json jsonb, confidence numeric(4,3), status text, created_at timestamptz)
forecasts(id uuid pk, sku_id uuid, as_of date, horizon_days int,
          p50 numeric[], p90 numeric[], model_version text, drivers_json jsonb,
          created_at timestamptz)
overrides(id uuid pk, merchant_id uuid, sku_id uuid, created_at timestamptz,
          suggested_qty numeric, manual_qty numeric, cash_delta numeric,
          stockout_risk numeric, days_cover numeric, accepted bool)
audit_log(id bigserial pk, merchant_id uuid, actor_id uuid, action text,
          entity text, entity_id text, payload jsonb, request_id text, created_at timestamptz)
```

Conventions:

- Money is `NUMERIC`, never `float`.
- Timestamps are UTC in the DB; convert to IST at the edge.
- Every tenant-owned table carries `merchant_id`; every query scopes by it from the auth context, never from the request body.
- Indexes: `sales(sku_id, sold_at)`, `forecasts(sku_id, as_of desc)`, `overrides(sku_id, created_at desc)`.
- Migrations with Alembic; no `create_all()` in production paths.

### 2.4 Caching, idempotency, jobs

- **Cache:** Redis key `forecast:{sku}:{as_of}:{model_version}`, TTL 1 h. Invalidate on new sale or accepted override.
- **Idempotency:** `Idempotency-Key` header on all POSTs. Store `(key, request_hash, response_json, expires_at)`; replay the stored response for 24 h.
- **Jobs:** `jobs(id, kind, status, payload, result, error, created_at, started_at, finished_at)`. Worker loop or FastAPI `BackgroundTasks` for the demo; no Celery unless you already have Redis wired.
- **Transactions:** override = one transaction writing `overrides` + `sales`/`inventory` adjustment + `audit_log`.
- **Outbox:** skip for the hackathon; note it as a scale path.

### 2.5 Resilience

- Timeouts on every external call: VLM 20 s, LLM 15 s, DB 2 s.
- Retries: 2 with exponential backoff + jitter, only for idempotent calls.
- Circuit breaker for VLM/LLM; on open, fall back to cached extraction or a pre-approved fixture.
- Bulkheads: separate semaphores for vision and text calls.
- Graceful degradation: if the model registry is unavailable, serve the last pinned model version.

### 2.6 Deployment

`docker-compose.yml` services: `web`, `api`, `worker`, `mcp`, `postgres`, `redis`, `caddy`.

- Caddy terminates TLS and can add a coarse rate limit.
- Containers run as non-root, read-only root filesystem where possible.
- `api` and `mcp` share the same image, different entrypoints.
- Health: `/healthz` (liveness), `/readyz` (DB + Redis + model loaded).
- `pg_dump` before the demo; keep a seeded snapshot.

---

## 3. API design

### 3.1 Endpoints

| Method | Path | Purpose | Limit class |
|---|---|---|---|
| POST | `/v1/auth/session` | exchange API key for short JWT | auth |
| GET | `/v1/skus` | list/search SKUs | read |
| GET | `/v1/forecast/{sku_id}` | p50/p90 + drivers | read |
| POST | `/v1/forecast/{sku_id}/explain` | grounded narrative | explain |
| POST | `/v1/overrides` | simulate/accept override | write |
| GET | `/v1/reorder-plan` | budget-constrained plan | read |
| POST | `/v1/capture/bill` | multipart image → job | capture |
| POST | `/v1/capture/voice` | multipart audio → job | capture |
| GET | `/v1/jobs/{job_id}` | job status/result | read |
| GET | `/v1/health` | public health | public |

All routes under `/v1`. Breaking changes bump the version.

### 3.2 Envelope

```json
{
  "data": {},
  "meta": { "request_id": "req_...", "model_version": "lgbm-2026-11-19", "cached": true },
  "error": null
}
```

Error:

```json
{
  "error": { "code": "VALIDATION_ERROR", "message": "qty must be between 0 and 100000", "details": [] },
  "request_id": "req_..."
}
```

Never return stack traces, SQL, or upstream provider messages to the client.

### 3.3 Contracts (MCP + internal)

```python
ingest_bill_ledger(source: str, image_ref: str | None, line_items: list | None) -> BillResult
forecast_sku_demand(sku_id: UUID, as_of: date, horizon_days: int = 14) -> ForecastResult
explain_forecast_drivers(sku_id: UUID, forecast_id: UUID) -> ExplanationResult
simulate_manual_override(sku_id: UUID, qty: int, as_of: date) -> OverrideResult
```

Freeze these names and JSON schemas by hour 4. Generate Python and TypeScript types from one schema file.

---

## 4. ML plan (supervised)

### 4.1 Problem formulation

- **Target:** daily units sold per SKU.
- **Horizon:** 14 days (demo), direct multi-horizon rows (`horizon` 1–14 as a feature).
- **Model:** global LightGBM across all SKUs (pooled learning), with SKU/category/store categoricals. Per-SKU models only for high-volume A-items if time allows.
- **Objectives:**
  - `tweedie` (variance power 1.1–1.5) for the mean, good for intermittent counts.
  - `quantile` at α = 0.5 and 0.9 for intervals. Train separate models for quantiles.
- **Fallbacks:** seasonal naive and Holt-Winters/ETS per SKU. If the ML model loses on validation, serve the baseline. This is a feature, not a failure.

### 4.2 Data rules (leakage is the #1 risk)

- **Time-based splits only.** Train ≤ T−8 weeks; validation T−8…T−4; test T−4…T. No random shuffle.
- Every feature at time `t` uses data strictly before `t`.
- Rolling features are computed with `shift(1)` before `rolling()`.
- **Stockout censoring:** observed sales = min(demand, stock). Flag stockout days; exclude or correct them, or the model learns "demand is low" when it was actually unavailable.
- Festival calendar uses **distance to festival**, not just a flag; the uplift starts before the date.
- Returns/refunds netted at the day level.
- Deduplicate documents: one bill imported twice must not double sales.

### 4.3 Feature set (~30)

Lags 1/7/14/28; rolling mean/std/min/max 7/14/28; day-of-week; month; day-of-month; payday window (1–5, 25–31); festival flag + signed distance; days since last festival; price; discount/promo; lead time; shelf life; perishable flag; category; SKU id; on-hand; days since last stockout; ADI/CV² class; optional weather.

### 4.4 Training protocol

1. Load and clean sales; build a dense daily grid per SKU (zero-fill missing days; mark stockouts).
2. Build features with a single `build_features(df)` function used by train and serve. **Same code path** prevents train/serve skew.
3. Train Tweedie mean model + quantile models on the training window; early stop on validation.
4. Backtest with rolling origins (at least 3 folds): refit or walk-forward.
5. Compare against seasonal naive and ETS per SKU and aggregate.
6. Select per segment: ML if it beats baseline by ≥5% WAPE, else baseline.
7. Pin `model_version`, feature list hash, and training window in the model registry row.

### 4.5 Prediction amplification (how to push accuracy)

Do these in order; each is measurable on the backtest:

1. **Ensemble:** weighted average of LightGBM, ETS, seasonal naive. Weights from validation WAPE, capped at [0.1, 0.8].
2. **Bias correction:** per category, subtract the mean residual from validation (only if it improves WAPE).
3. **Hierarchical reconciliation:** bottom-up SKU → category, then scale SKU forecasts to match the category total (MinT if time).
4. **Conformal intervals:** take the 90th percentile of absolute residuals per horizon on validation; use for p90 and risk calculations. This gives honest coverage without distributional assumptions.
5. **Analog retrieval (optional):** find the k most similar historical weeks (festival distance, promo, price) and add their demand shape as a feature.
6. **Human feedback:** store every accepted override with reason; weekly, compare override vs model and add a per-SKU bias term. Never train on unaccepted suggestions.
7. **Retrain cadence:** nightly in production; once during the event after seeding.

### 4.6 Intermittent demand

Classify with ADI (average demand interval) and CV²:

- **Smooth** (ADI < 1.32, CV² < 0.49): LightGBM/ETS.
- **Intermittent/lumpy:** Croston/TSB or Tweedie LightGBM; do not force a normal model.
- **New SKU:** category prior + similar-SKU nearest neighbour; show "low data confidence" in the UI.

### 4.7 Accuracy gates (replace "90%")

| Segment | Definition | Gate |
|---|---|---|
| A-items | top 20% revenue, ≥ 1 sale/day | WAPE ≤ 10–12% (≈ 88–90% accuracy) |
| B-items | mid volume | WAPE ≤ 15–18% |
| C / intermittent | sparse | WAPE ≤ 25–30%, or MASE < 1 vs naive |
| Aggregate | revenue-weighted | WAPE ≤ 15% |
| Baseline lift | vs seasonal naive | ≥ 20% lower WAPE |

Report **WAPE**, **MASE**, **pinball loss** at p50/p90, and **bias**. Do not average per-SKU MAPE; low-volume SKUs explode it. State the segment when claiming "90%".

---

## 5. Explainability (XAI in the ML output)

### 5.1 Numeric explanation

- Compute SHAP values with `shap.TreeExplainer` on the p50 model.
- Return the top 3 drivers by absolute contribution, with sign and unit contribution.
- Prefer a raw-units objective (Tweedie) so SHAP contributions are additive in units. If using `log1p`, label contributions as relative and convert carefully.
- Group one-hot festival features into one "festival" driver; group lags into "recent velocity".
- Store `feature_snapshot_hash` with the explanation so it is reproducible.

### 5.2 Grounded narrative

The LLM never sees the model; it sees only a JSON payload:

```json
{
  "sku": "Festive Mustard Oil 1L",
  "p50": 48,
  "p90": 56,
  "drivers": [
    {"name": "base velocity", "units": 24},
    {"name": "Diwali proximity", "units": 16},
    {"name": "supplier lead-time buffer", "units": 8}
  ],
  "constraints": {"budget_ok": true, "shelf_life_days": 90}
}
```

Prompt contract:

- Use only the JSON. Do not compute new numbers.
- At most 3 drivers, 2 sentences, plain language.
- If a number is not in the JSON, omit it.
- No causal claims; say "associated with", not "caused by".

Post-validation:

1. Extract all numbers from the generated text.
2. Every number must appear in the payload within tolerance.
3. If validation fails, fall back to a deterministic template.
4. Log the rejected generation for review.

### 5.3 Confidence, not fake precision

- Show p50 with a p10–p90 band (conformal-calibrated).
- Replace "91% confidence" with two honest signals:
  - **Interval:** "80% chance demand is between 41 and 56 units."
  - **Data sufficiency:** high / medium / low, based on history length and stockout rate.
- If interval width > 50% of p50, show "low confidence — collect more history".

### 5.4 Counterfactual explanation

For every override, return a comparison:

```json
{
  "recommended": 48,
  "manual": 35,
  "cash_freed": 1950,
  "stockout_risk_before": 0.09,
  "stockout_risk_after": 0.23,
  "days_cover_after": 5.2
}
```

UI renders this as three deltas. This is the "amplified answer": not just a number, but the consequence of changing it.

---

## 6. Security: input sanitization and SQL safety

Full checklist with code: `SECURITY_CHECKLIST.md`. The non-negotiables:

### 6.1 SQL injection

**Rule: SQL text is a constant string; values are bound parameters. Never build SQL with f-strings, `%`, `.format()`, or concatenation.**

```python
# WRONG — injectable
cur.execute(f"SELECT * FROM sales WHERE sku_id = '{sku_id}'")

# RIGHT — ORM
stmt = select(Sale).where(Sale.sku_id == sku_id, Sale.merchant_id == merchant_id)

# RIGHT — raw SQL with binds
conn.execute(
    text("SELECT * FROM sales WHERE sku_id = :sku_id AND merchant_id = :mid"),
    {"sku_id": sku_id, "mid": merchant_id},
)
```

- Dynamic identifiers (sort column, table) are **allowlisted**, never interpolated.
- The analytics/MCP DB role is **read-only**; the app role has only the privileges it needs.
- Never expose a generic query endpoint. If you add one, it takes a named, validated query + params, not SQL text.

### 6.2 Input validation

- Pydantic v2 models: `extra="forbid"`, `str_strip_whitespace=True`, explicit bounds, enums, UUID/date types.
- Max body size (1 MB JSON), max upload (10 MB), max string lengths, max array lengths.
- Reject null bytes and control characters; normalize Unicode (NFKC).
- Validate `Content-Type` and magic bytes; never trust the filename extension.
- Pagination: `limit ≤ 100`, cursor-based or bounded offset.
- Dates: reject future dates for `as_of` and sales; bound to a sane range.

### 6.3 Tenant isolation (IDOR)

- `merchant_id` comes from the JWT/API key, never from the request body or query string.
- Every repository method takes `merchant_id` as a required argument.
- Add a test that requests another tenant's SKU and expects 404 (not 403, to avoid leaking existence).

### 6.4 File uploads

- Allowlist: `image/jpeg`, `image/png`, `image/webp`, `audio/wav`, `audio/webm`.
- Verify magic bytes; open with Pillow and `verify()`; re-encode to strip EXIF and polyglot payloads.
- Max dimensions 6000×6000; decompression-bomb guard.
- Store with a random UUID name outside the web root; serve through an authorized endpoint.
- Optional: ClamAV scan.

### 6.5 Prompt injection

OCR/voice text is **untrusted data**, never instructions.

- Fixed system prompt; document text goes in a delimited user block.
- Structured output (JSON schema) only.
- Tools are allowlisted and parameter-validated; OCR text never chooses a tool or its arguments.
- No secrets or system prompts in the model context.
- Human confirmation for any write action.
- Validate the output against the schema and arithmetic before persisting.

### 6.6 Auth, headers, secrets

- Short-lived JWT (15 min) + refresh; or API key → JWT exchange for the demo.
- RBAC: `seller`, `admin`; deny by default.
- CORS allowlist (no `*` with credentials).
- Security headers: HSTS, CSP, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`.
- Secrets in `.env`, never committed; `.geminiignore` excludes `.env`, keys, raw data.
- Redact tokens, passwords, and PII from logs.
- `pip-audit`, `npm audit`, Dependabot; pin lockfiles.

### 6.7 Error handling and logging

- Generic client errors + `request_id`; details only in server logs.
- Structured JSON logs; no request bodies with PII by default.
- Audit every write: actor, action, entity, before/after hash, request id.

---

## 7. Rate limiting and abuse protection

### 7.1 Layers

1. **Edge/proxy (Caddy/Nginx):** coarse per-IP limit, connection limits, body size.
2. **Application (FastAPI + Redis):** per-identity limits per route class.
3. **Provider budget:** per-merchant daily quotas for VLM/LLM calls.
4. **Concurrency:** semaphores for expensive calls; queue with timeout.

### 7.2 Limits

| Class | Scope | Limit | Burst |
|---|---|---|---|
| public | per IP | 60/min | 20 |
| auth | per IP + username | 10/min, 50/hour | 5 |
| read | per merchant | 120/min | 40 |
| write | per merchant | 30/min | 10 |
| explain | per merchant | 15/min | 5 |
| capture | per merchant | 6/min, 60/hour, 200/day | 3 |
| upload | per merchant | 20/hour | 5 |
| export | per merchant | 5/hour | 2 |

- Return `429` with `Retry-After` and `X-RateLimit-Limit/Remaining/Reset`.
- Key by authenticated identity when present, IP otherwise.
- Store counters in Redis (`slowapi` + `limits`), not in process memory, so multiple workers share them.
- Fail **open** for reads if Redis is down; fail **closed** for auth and capture (cost/abuse risk). Document the choice.

### 7.3 LLM/VLM cost controls

- Max input: 8k characters text, 1 image per request.
- Cache by SHA-256 of the image/audio; identical uploads return the cached extraction.
- Per-merchant daily token/vision budget; return `402`/`429` with a clear message when exhausted.
- Semaphore: max 4 concurrent vision calls, 8 text calls.
- Timeout 20 s; retry once; circuit breaker opens after 5 failures in 60 s.

### 7.4 Implementation sketch

```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

def identity(request: Request) -> str:
    return getattr(request.state, "merchant_id", None) or get_remote_address(request)

limiter = Limiter(key_func=identity, storage_uri=settings.REDIS_URL,
                  default_limits=["60/minute"], headers_enabled=True)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.post("/v1/capture/bill")
@limiter.limit("6/minute;60/hour;200/day")
async def capture_bill(request: Request, file: UploadFile = File(...)):
    ...
```

Add a load test (`k6` or `locust`) that proves 429s appear at the configured thresholds and that legitimate traffic stays under p95 targets.

---

## 8. Observability

- **Logs:** JSON, `request_id`, `merchant_id`, route, status, latency, model_version. Redact secrets/PII.
- **Metrics:** request count/latency by route, 429 rate, forecast job duration, cache hit rate, LLM tokens/cost, extraction confidence distribution, override rate.
- **Traces:** OpenTelemetry if time; otherwise request_id propagation is enough for the demo.
- **SLOs:** read p95 < 80 ms, override p95 < 50 ms, availability 99% during demo, capture success > 95%.
- **Alerts:** 5xx rate, circuit breaker open, DB pool exhaustion, daily LLM budget 80%.

---

## 9. Testing

### 9.1 Test pyramid

- **Unit:** math invariants, constraint logic, parsers. `pytest`.
- **Property-based:** `hypothesis` for qty ≥ 0, cash delta monotonic, budget never exceeded, shelf-life cap.
- **Integration:** API + Postgres via testcontainers or a disposable schema.
- **Contract:** schemas generated from one source; fail on drift.
- **ML:** leakage tests (future timestamps absent), determinism (fixed seed), backtest gate, baseline fallback.
- **E2E:** Playwright: capture → forecast → explain → override → accept.
- **Security:** SQLi payloads, XSS strings, oversized uploads, prompt injection, IDOR, rate-limit thresholds.
- **Load:** k6 on read + override endpoints.

### 9.2 CI gates (minimum)

```
ruff check . && mypy .
pytest -q
pytest tests/security -q
python eval/backtest.py --gate
pnpm lint && pnpm typecheck && pnpm test
```

A red gate blocks merge. For the hackathon, run them locally before each integration gate.

---

## 10. Revised 30-hour plan

Security and ML gates are embedded, not appended.

| Phase | Hours | Work | Exit gate |
|---|---|---|---|
| 0 Prep | T−2 days | repo, contracts, seed data, baseline, fixtures, GEMINI.md, threat model | `docker compose up` + baseline backtest runs |
| 1 Skeleton | 0–4 | DB migrations, auth, rate limiter, 4 tool stubs, OpenAPI | `curl` all tools; 429 test passes; SQLi test passes |
| 2 Forecast | 4–10 | features, Tweedie + quantile LightGBM, ETS baseline, SHAP, backtest | weighted WAPE ≤ 15%; A-item gate reported |
| 3 Capture | 10–16 | Docling/VLM, faster-whisper, schema validation, review UI | 20-bill eval F1 ≥ 0.90; math check catches planted error |
| 4 Product | 16–22 | dashboard, decision card, explanation, override sandbox, voice loop | full loop clickable; p95 override < 50 ms |
| 5 Harden | 22–27 | offline SQLite/Ollama, load test, security tests, error states, quotas | demo runs with Wi-Fi off; 429 + IDOR + upload tests pass |
| 6 Freeze | 27–30 | benchmarks, rehearsal, backup video, tag `demo-v1` | 3 clean runs; no P0 bugs |

**Owners:** P1 platform/security, P2 ML/math, P3 capture, P4 product/demo. Integration gates at 10, 16, 22, 27. Freeze `main` at 27.

**Cut lines if behind:** LiveKit → MediaRecorder; React Flow → static driver bars; local VLM → cloud + fixtures; multi-store → single store. **Never cut:** deterministic math, explanation, override, offline demo, input validation.

---

## 11. Repo structure

```
vani-ledger/
  GEMINI.md
  AGENTS.md                  # canonical cross-tool rules (imported by GEMINI.md)
  README.md
  docker-compose.yml
  .env.example
  .geminiignore
  .gemini/
    settings.json
    commands/
      test.toml
      backtest.toml
      security.toml
  apps/
    api/                     # FastAPI gateway
    web/                     # Next.js 16 PWA
  services/
    forecast-core/           # pure Python; LLM imports forbidden
      features.py
      models.py
      policy.py
      explain.py
      backtest.py
    mcp-ledger/              # FastMCP v4, imports forecast-core
    capture/                 # docling, vlm, whisper, validation
    worker/                  # jobs: capture, nightly forecast
  packages/
    contracts/               # JSON Schema → py/ts generated types
  data/
    seed/                    # synthetic kirana generator + public loaders
    fixtures/                # bills, audio, expected JSON
  eval/
    backtest.py
    extraction_eval.py
    gates.yaml
  docs/
    ARCHITECTURE.md
    SECURITY.md
    DEMO.md
    adr/
  tests/
    unit/ integration/ security/ e2e/
```

**First 12 issues:** scaffold+CI · contracts · migrations+seed · synthetic generator · baseline forecast · LightGBM+backtest · SHAP+narrative · MCP tools · override simulator+latency bench · bill pipeline+eval · rate limiting+auth · dashboard+offline.

---

## 12. Gemini CLI setup

### 12.1 Install and authenticate

```bash
npm install -g @google/gemini-cli
gemini --version
gemini                      # choose Google sign-in or set GEMINI_API_KEY
```

On this Windows machine neither Gemini CLI nor Docker is currently on PATH. Install Docker Desktop if you want the sandbox; otherwise use approval mode `default` and review every shell command.

### 12.2 Context: keep AGENTS.md canonical

Gemini CLI reads `GEMINI.md` hierarchically: `~/.gemini/GEMINI.md`, repo root, then subdirectories. It can also read `AGENTS.md` via `context.fileName`.

Recommended: keep `AGENTS.md` as the single source of truth and make `GEMINI.md` a thin entrypoint:

```markdown
# GEMINI.md
@./AGENTS.md

## Gemini-specific
- Use `plan` approval mode for design changes, `auto_edit` for implementation.
- Run the test command after every file edit.
- Never edit `data/fixtures/*.json` without updating the matching expected output.
```

`@./AGENTS.md` imports the file; do not also list `AGENTS.md` in `context.fileName` or it loads twice.

Run `/init` once to let Gemini draft context, then replace it with the curated version. Inspect with `/memory show`, refresh with `/memory refresh`, and compress long sessions with `/compress`.

### 12.3 `.gemini/settings.json`

```json
{
  "context": { "fileName": ["GEMINI.md"] },
  "tools": { "sandbox": "docker" },
  "mcpServers": {
    "postgres-ro": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres",
               "postgresql://mcp_ro:${POSTGRES_MCP_PASSWORD}@localhost:5432/vani"]
    },
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
    }
  }
}
```

- Use a **read-only** Postgres role for the MCP server; never the app role.
- Do not set `"trust": true` on the DB server.
- If `${VAR}` expansion is unreliable in your version, use a small wrapper script that reads the env var.
- Add `.gemini/settings.json` to git, but keep credentials in `.env`.

Read-only role:

```sql
CREATE ROLE mcp_ro LOGIN PASSWORD ':secret';
GRANT CONNECT ON DATABASE vani TO mcp_ro;
GRANT USAGE ON SCHEMA public TO mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mcp_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mcp_ro;
```

### 12.4 Custom commands

`.gemini/commands/test.toml`:

```toml
description = "Run backend tests"
prompt = """
Run `uv run pytest -q`. Summarize failures with file:line.
Do not modify tests to make them pass.
"""
```

`.gemini/commands/backtest.toml`:

```toml
description = "Run the forecast backtest gate"
prompt = """
Run `uv run python eval/backtest.py --gate`.
Report WAPE by segment, baseline lift, and whether the gate passed.
Do not tune hyperparameters in this command.
"""
```

### 12.5 Approval and sandbox

- `gemini --approval-mode=plan` for architecture review (read-only).
- `gemini --approval-mode=auto_edit` for implementation; still confirm shell commands.
- `gemini --sandbox` runs tools in a container (requires Docker/Podman).
- Never use YOLO mode with a DB connection or real credentials.

### 12.6 Working discipline

1. One phase per session; start with `@ENGINEERING_HANDOFF.md` and the current phase.
2. Commit before and after each phase; review diffs, don't blind-accept.
3. Run the test/backtest command after every change.
4. Keep `GEMINI.md` under ~200 lines; move detail into `docs/`.
5. Use subdirectory `GEMINI.md` for strict boundaries (`services/forecast-core/GEMINI.md`: "no LLM imports").
6. Never paste secrets or real merchant data into the prompt.

---

## 13. Demo and acceptance

**3-minute demo:** bill photo → extracted line items + confidence → 48-unit forecast with +24/+16/+8 drivers → override to 35 → ₹1,950 freed, +14% risk, 5.2 days cover → Wi-Fi off → local Ollama/SQLite answers.

**Definition of done:**

- Weighted WAPE ≤ 15%; A-item WAPE ≤ 12%; beats seasonal naive by ≥20%.
- Extraction field F1 ≥ 0.90 on 20 bills.
- Override p95 < 50 ms; unit tests on cash/risk math.
- Every forecast has ≥3 drivers + grounded narrative.
- Full demo runs offline.
- 429, IDOR, SQLi, oversized upload, and prompt-injection tests pass.
- Same inputs → same outputs (pinned model, seeded).

---

## 14. Research list

- **Forecasting:** FPP3 (STL, ETS, quantiles), M5 Accuracy findings, WAPE/MASE/pinball, Croston/TSB, conformal prediction.
- **Inventory:** newsvendor critical ratio, (R,S) policy, safety stock with lead-time variability, perishable inventory.
- **ML:** LightGBM quantile/Tweedie docs, SHAP TreeExplainer, time-series CV.
- **Security:** OWASP ASVS, OWASP API Security Top 10, SQLAlchemy bound parameters, slowapi/limits, prompt injection (OWASP LLM Top 10).
- **Protocols:** MCP spec, FastMCP v4, PydanticAI structured outputs.
- **Gemini CLI:** GEMINI.md hierarchy, settings reference, MCP servers, custom commands, sandbox.

---

## 15. Bootstrap prompt for Gemini CLI

Paste this after the repo scaffold exists:

```
Read @ENGINEERING_HANDOFF.md, @SECURITY_CHECKLIST.md, and @GEMINI.md.

We are building VANI-LEDGER for PS 01. Start with Phase 1 only:
1. Confirm the repo tree and contracts.
2. Add Alembic migrations for the schema in §2.3.
3. Add auth stub, tenant scoping, strict Pydantic models, and the rate limiter in §7.
4. Implement the four MCP tool stubs returning typed fixtures.
5. Add the security tests: SQLi, IDOR, oversized upload, 429 thresholds.

Do not implement forecasting or UI yet. Run the test command and show the output.
```

---

## 16. What not to do

- Do not let an LLM compute or "adjust" a number.
- Do not build SQL with string interpolation.
- Do not ship an endpoint without a rate limit and a strict schema.
- Do not evaluate on a random split or on training data.
- Do not claim "90% accuracy" without naming the segment and metric.
- Do not add payments, credit scoring, or GST filing.
- Do not expose the MCP server or the database to the browser.
- Do not commit `.env`, raw data, model binaries, or API keys.
