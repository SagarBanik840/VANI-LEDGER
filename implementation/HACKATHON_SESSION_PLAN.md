# HACKATHON SESSION PLAN — 7 Hours to a Working Demo

**Window:** 17:00–00:00, Thursday 19 November 2026
**Constraint:** AI-assisted build only; one human + Gemini CLI
**Goal by midnight:** a running, explainable forecast → override → capture loop on seeded data, with a backup video and a submitted repo.

This is deliberately **not** the full architecture in `ENGINEERING_HANDOFF.md`. It is the smallest honest slice that demonstrates the thesis:

> LLMs perceive and explain; Python computes; the seller decides.

---

## 1. Tonight's stack (and what changes later)

| Layer | Tonight | Post-event target | Why |
|---|---|---|---|
| Database | **SQLite** via SQLAlchemy | Postgres 17 + pgvector | No Docker installed; zero setup |
| Cache / limits | **In-memory** slowapi | Redis | One process tonight |
| UI | **FastAPI + Jinja2 + vanilla JS/CSS** | Next.js 16 PWA | No build step; 20-minute pages |
| Forecast | **STL/calendar decomposition** + optional LightGBM | LightGBM quantile + ensemble | Guaranteed additive drivers |
| Capture | **Fixture adapter** (+ Tesseract if present) | Docling + VLM | Contract is real; adapter swaps later |
| Voice | **Cut** | LiveKit + Whisper + Kokoro | Highest risk, lowest demo value tonight |
| Auth | Static API key header | JWT + RBAC | Enough to test 401/403 |
| Deploy | `localhost:8000` | Docker Compose + Caddy | Demo runs locally, so "offline" is real |

**Do not install Docker, Postgres, Redis, Next.js, LiveKit, or a local VLM tonight.** Every one is a potential hour lost. The contracts and math are real; the adapters are replaceable.

**Repo location:** `C:\Users\Sagar Banik\Documents\vani-ledger` (outside the submission folder).

**Tonight's tree (small on purpose):**

```
vani-ledger/
  app/
    main.py          FastAPI routes
    db.py            SQLAlchemy engine/session
    models.py        Sku, Sale, Forecast, Override, Document
    schemas.py       Pydantic request/response
    forecast.py      decomposition + drivers + backtest
    overrides.py     simulator + risk math
    capture.py       upload validation + fixture adapter
    templates/index.html
    static/app.css
    static/app.js
  configs/demo.yaml  demo SKU calibration
  data/fixtures/     bill image + expected JSON
  scripts/seed.py
  scripts/reset_demo.py
  tests/
  DEMO.md
  README.md
```

---

## 2. Scope: what ships, what waits

### P0 — must exist at 22:30

- [ ] Seeded SQLite DB with 40 SKUs × 2 years of daily sales.
- [ ] Forecast for the demo SKU: **48 units** with drivers **+24 base / +16 festival / +8 lead buffer**.
- [ ] Backtest script that prints a real WAPE on held-out weeks.
- [ ] Override simulator: **35 units → ₹1,950 freed, ~23% stockout risk (+14 pts), ~5.2 days cover**.
- [ ] One page: decision card, driver bars, slider, three metric tiles.
- [ ] "Why this order?" explanation built from the same numbers.
- [ ] Bill upload → 4 line items + confidence + ₹300 math discrepancy.
- [ ] Reset script so the demo can be re-run cleanly.
- [ ] 3-minute backup video.

### P1 — only if P0 is done by 21:35

- [ ] Rate-limit + validation tests (429, SQLi, IDOR, mass assignment).
- [ ] Real Tesseract extraction on the demo bill.
- [ ] p90 band / confidence label.
- [ ] SKU selector with 3 demo SKUs.

### P2 — explicitly cut tonight

- Voice, LiveKit, Kokoro, local VLM, React Flow, Next.js, PWA service worker, Postgres, Redis, JWT, multi-tenant auth, LightGBM tuning.

---

## 3. Timeline

Times are hard. The exit test is the only definition of done for a block.

| Time | Block | Deliverable | Exit test |
|---|---|---|---|
| 17:00–17:15 | **Pre-flight** | repo, uv env, FastAPI hello, Gemini context | `curl localhost:8000/health` → `{"ok":true}` |
| 17:15–17:45 | **Schema + seed** | SQLite tables, 40 SKUs, 730 days, demo calibration | `sqlite3` count > 20k sales rows |
| 17:45–18:40 | **Forecast engine** | decomposition, drivers, p90, backtest | script prints `p50=48`, `[24,16,8]`, WAPE |
| 18:40–19:25 | **Override + API** | `/api/forecast`, `/api/override`, math tests | `35 → 1950 / ~23% / ~5.2` |
| 19:25–20:15 | **Decision card UI** | dark/gold page, SKU header, bars, 48 units | browser shows 48 + three bars |
| 20:15–20:55 | **Override UI** | slider 0–80, live tiles, accept button | drag to 35 → tiles update |
| 20:55–21:35 | **Capture** | upload → 4 line items + math check | upload demo bill → table + ₹300 flag |
| 21:35–22:05 | **Explain + polish** | grounded "why", loading/error/empty states | explanation uses only payload numbers |
| 22:05–22:30 | **Harden** | API key, rate limit, strict schemas, tests | 429 + SQLi test pass |
| 22:30–23:10 | **Rehearsal** | 3 clean runs + backup video | video saved, timer < 3:30 |
| 23:10–23:45 | **Freeze** | tag `demo-v1`, README, DEMO.md | reset + run from clean DB works |
| 23:45–00:00 | **Submit** | repo link, video, form | confirmation screenshot |

**Rule:** if a block is 10 minutes late, apply its cut line immediately. Do not steal time from rehearsal.

---

## 4. Block-by-block prompts

Use `gemini --approval-mode=auto_edit` in the repo root. Commit after every block.

### Block 1 — Pre-flight (15 min)

```bash
mkdir vani-ledger && cd vani-ledger
git init
uv python pin 3.12
uv init --bare
uv add fastapi uvicorn sqlalchemy pydantic pydantic-settings jinja2 python-multipart slowapi pytest httpx
```

Prompt:

```
Read @AGENTS.md and @HACKATHON_SESSION_PLAN.md.

Create the FastAPI skeleton in `app/`:
- `main.py` with `/health` returning {"ok": true}.
- `db.py` with SQLite at ./vani.db and a session dependency.
- Jinja2 templates + static mount.
- `tests/test_health.py`.
Do not add Postgres, Redis, Next.js, Docker, auth, or forecasting yet.
Run `uv run pytest -q` and `uv run uvicorn app.main:app --port 8000`, then curl /health.
```

Exit: health returns 200; commit `chore: skeleton`.

### Block 2 — Schema + seed (30 min)

```
Add SQLAlchemy models and a deterministic seed script:

Tables: sku, sale, calendar, forecast, override, document.
Seed 40 kirana SKUs, 730 daily rows each (29,200 sales), with:
- weekday seasonality,
- festival multipliers from a small Indian calendar (Diwali, Holi, Eid, Navratri, Pongal),
- 3% stockout days (flag them, suppress sales),
- supplier lead times 1–5 days, shelf life 7–180 days.

Demo SKU: "Festive Mustard Oil 1L", unit_cost=150, price=220,
lead_time=2, shelf_life=90, on_hand=29, horizon=7.
Calibrate its history so the decomposition yields p50≈48 with
base≈24, festival≈16, buffer≈8. Put all calibration in configs/demo.yaml.

Add `scripts/reset_demo.py` that drops and reseeds.
Print row counts and the demo SKU's 14-day history mean.
```

Exit: `uv run python scripts/seed.py` then `sqlite3 vani.db "select count(*) from sale;"` > 20,000.

**Calibration note:** the buffer is `lead_time_days × recent_daily_rate`. If the numbers drift, adjust the festival multiplier and on_hand in `configs/demo.yaml`, not in the API.

### Block 3 — Forecast engine (55 min)

```
Implement `app/forecast.py`:

1. `load_series(sku_id)` → daily units, zero-filled, stockout-aware.
2. `decompose(series, as_of, horizon=7)`:
   - base = median daily sales (last 28 days, excluding stockouts) × horizon
   - festival = base × uplift, where uplift comes from calendar distance
     (peak 10 days before Diwali, decays after)
   - buffer = recent_daily_rate × lead_time_days
   - trend = clamp(linear slope over 28 days, ±10% of base)
   - p50 = round(base + festival + buffer + trend)
   - residual_sd from the last 8 weeks; p90 = p50 + 1.28 × residual_sd × sqrt(horizon)
3. `drivers()` returns [{name, units}] for base, festival, buffer, trend.
4. `backtest()`: rolling 8-week holdout, report WAPE and MASE vs seasonal naive.

Optional: if lightgbm is installed, train a small model and compare.
Use it ONLY if it beats the decomposition by ≥5% WAPE; otherwise keep
the decomposition and print "baseline wins".

Do not round the drivers independently of p50; they must sum to p50.
```

Exit: `uv run python -m app.forecast --demo` prints `p50=48`, drivers `24/16/8/0`, and a WAPE number.

**Cut line:** if LightGBM install/training exceeds 15 minutes, delete it and keep the decomposition. The story is "baseline first"; a black box that loses to the baseline is a worse demo.

### Block 4 — Override + API (45 min)

```
Implement `app/overrides.py` and the JSON API:

POST /api/override {sku_id, qty}
- cash_delta = (qty - p50) * unit_cost
- daily_rate = p50 / horizon
- days_cover = qty / daily_rate
- stockout_risk = P(D > on_hand + qty), normal(mean=p50, sd=residual_sd*sqrt(horizon))
- spoilage_risk for perishables = P(D_shelf_life < on_hand + qty)
- budget_ok = qty * unit_cost <= weekly_budget

GET /api/forecast/{sku_id}
GET /api/skus
POST /api/override/accept → writes override row

Use strict Pydantic models (extra="forbid"), bound SQLAlchemy queries,
and an in-memory rate limit: 60/min read, 10/min override.

Add tests:
- qty=35 → cash_delta ≈ 1950
- stockout risk ≈ 0.23 and delta ≈ +0.14
- days_cover ≈ 5.2
- qty > 100000 → 422
- negative qty → 422
Benchmark the simulator: p95 < 50 ms over 500 calls.
```

Exit: `curl` returns the three demo numbers; `pytest` passes.

**Cut line:** if the normal CDF is fiddly, use the empirical residual quantiles (simpler and more honest). Never hardcode the risk.

### Block 5 — Decision card UI (50 min)

```
Build a single page at `/` with Jinja2 + vanilla JS + CSS:

Theme: near-black (#0B0B0C), ivory (#F2EEE3), gold (#C9A227),
muted (#8A8578). Georgia headings, Segoe UI body.

Layout:
- Top bar: "VANI—LEDGER" left, "AARAMBHATHON 2026 / PS 01" right.
- Left: Seller Decision Card — SKU name, 48 units (Georgia 64),
  "7-day reorder", confidence chip, three driver bars (+24/+16/+8),
  p90 line.
- Right: override panel — slider 0–80, current qty, three tiles
  (cash freed, stockout risk, days cover), Accept button.
- Bottom: capture panel placeholder.

Fetch /api/forecast on load. No React, no build step, no CDN.
All CSS in app/static/app.css, JS in app/static/app.js.
```

Exit: page shows 48, bars render, no console errors.

**Cut line:** if the layout takes too long, drop the top bar and capture panel; the decision card + slider are the demo.

### Block 6 — Override UI (40 min)

```
Wire the slider:
- debounce 120 ms, POST /api/override.
- update the three tiles with the returned numbers.
- show "₹1,950 freed", "23% stockout risk (+14 pts)", "5.2 days cover".
- Accept button POSTs /api/override/accept, disables, shows "Override recorded".
- Add a reset link that reloads the seed state.

Add loading skeleton, error toast, and a "no data" state.
```

Exit: drag 48 → 35; tiles update; accept writes a row (`select count(*) from override`).

### Block 7 — Capture (40 min)

```
Add POST /api/capture/bill (multipart):
- validate: ≤10 MB, JPEG/PNG/WEBP magic bytes, ≤36 MP, re-encode with Pillow.
- try pytesseract if installed; otherwise load data/fixtures/bill_demo.json.
- return {line_items[4], confidence, math_check{ok:false, delta:300}, source:"demo fixture"}.
- persist a document row.
- UI: upload button, table of items, red math-check banner.

The fixture must contain a deliberate ₹300 discrepancy:
4,200 written vs 3,900 computed.
Label fixture-backed results "DEMO DOCUMENT" in the UI.
```

Exit: upload demo bill → 4 rows + red "₹300 discrepancy" banner.

**Cut line:** skip Tesseract entirely; the fixture adapter is the demo. Do not spend time on OCR accuracy tonight.

### Block 8 — Explain + polish (30 min)

```
Add POST /api/explain:
- payload = {p50, horizon, drivers, constraints}.
- deterministic template:
  "Base velocity contributes 24 units. Diwali proximity adds 16.
   A 2-day supplier lead time adds 8. The plan stays within budget."
- optional: if GEMINI_API_KEY is set, ask Gemini to rewrite the template.
  Validate that every number in the output exists in the payload;
  otherwise return the template.
- UI: "Why this order?" expandable panel.

Polish: loading/empty/error states, keyboard focus, mobile width,
and a footer "Deterministic math · seller in control".
```

Exit: explanation contains only 24/16/8/48 and no invented numbers.

### Block 9 — Harden (25 min)

```
Add:
- X-API-Key dependency on /api/* (key from .env, default "demo-key").
- slowapi in-memory limits: 60/min read, 10/min override, 5/min capture.
- Pydantic extra="forbid" on every request model.
- Security tests: 401 without key, 429 after limit, SQLi string in sku_id,
  mass assignment merchant_id, oversized upload.
- `scripts/reset_demo.py` that reseeds and clears overrides.
```

Exit: `uv run pytest -q` green; 429 test passes.

### Block 10 — Rehearsal (40 min)

No new features. Run the demo three times with a timer. Fix only blockers.

Record a backup with Windows Game Bar (`Win+G`) or OBS:
- 17:00–17:10 open + forecast
- 17:10–17:40 drivers + explain
- 17:40–18:20 override
- 18:20–18:50 capture
- 18:50–19:00 offline reload + close

Save `demo/backup.mp4`.

### Block 11 — Freeze + submit (35 min)

- Tag `demo-v1`.
- `README.md`: one-liner, stack, run commands, demo script.
- `DEMO.md`: exact clicks and numbers.
- `PROMPTS.md`: the prompts used (if the event requires an AI log).
- Reset DB; run from clean; confirm.
- Submit repo link + video.

---

## 5. The 3-minute demo script

| Time | Action | Say |
|---|---|---|
| 0:00 | Open dashboard | "This is VANI-LEDGER. One seller, 40 SKUs, two years of messy history." |
| 0:20 | Show Festive Mustard Oil | "Seven-day reorder: 48 units. Not a black box — three drivers." |
| 0:40 | Point at bars | "24 from base velocity, 16 from Diwali proximity, 8 from a two-day lead time." |
| 1:00 | Click "Why this order?" | "Plain language, grounded in the same numbers. The LLM never does arithmetic." |
| 1:20 | Drag slider to 35 | "I disagree. Show me the consequence before I commit." |
| 1:35 | Point at tiles | "₹1,950 freed, stockout risk rises 14 points to 23%, 5.2 days of cover." |
| 1:55 | Accept override | "The seller is the final authority. The override is recorded and audited." |
| 2:10 | Upload bill | "A crumpled supplier bill becomes structured line items." |
| 2:30 | Point at ₹300 flag | "Deterministic math catches a ₹300 overcharge the eye misses." |
| 2:45 | Wi-Fi off, reload | "Everything runs locally. No cloud dependency on stage." |
| 2:55 | Close | "Perception separated from calculation. Explainable, overridable, offline." |

**Numbers to memorize:** 48, 24/16/8, 35, ₹1,950, +14 pts, 23%, 5.2 days, ₹300.

---

## 6. Contingencies

| If… | Then… |
|---|---|
| Gemini CLI auth/install fails | Use `plan`/`auto_edit` with any available model; do not burn >15 min. Fall back to writing the 6 files by hand. |
| Seed data won't calibrate to 48 | Set `configs/demo.yaml` to the nearest SKU/scenario; update the demo script, not the math. |
| Forecast block overruns | Ship the decomposition without LightGBM; label it "STL + calendar baseline". |
| UI block overruns | Use a single server-rendered HTML page; drop capture panel. |
| Capture overruns | Return the fixture directly; label "DEMO DOCUMENT". |
| Rate limit test fails | Disable the limiter for the demo, keep the code; note it as P1. |
| Anything breaks after 22:30 | Revert to the last tag; use the backup video. |
| Laptop/venue Wi-Fi dies | Everything is local; if the app won't start, play the video. |

**Never cut:** the override math, the explanation, the reset script, the backup video.

---

## 7. Freeze checklist

- [ ] `git tag demo-v1` exists.
- [ ] `scripts/reset_demo.py` runs from a clean checkout.
- [ ] `uv run pytest -q` green.
- [ ] Dashboard loads at `localhost:8000`.
- [ ] 48 → 35 → ₹1,950 / +14 pts / 5.2 days.
- [ ] Explanation contains only payload numbers.
- [ ] Bill upload returns 4 items + ₹300 flag.
- [ ] Wi-Fi off → reload works.
- [ ] `demo/backup.mp4` plays.
- [ ] Repo link and video link submitted.

---

## 8. After midnight (do not do tonight)

1. Swap SQLite → Postgres 17 + pgvector; add Alembic.
2. Swap in-memory limits → Redis + slowapi; add JWT/RBAC.
3. Replace the fixture adapter with Docling + VLM; add the extraction eval set.
4. Add LightGBM quantile + SHAP; compare against the decomposition; keep the winner.
5. Add voice (LiveKit + faster-whisper + Kokoro).
6. Port the UI to Next.js 16; add React Flow and the PWA shell.
7. Run the real backtest and publish WAPE by segment. Only then discuss "90%".
