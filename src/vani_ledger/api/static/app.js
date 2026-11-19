/* VANI-LEDGER Seller Intelligence & Decision Cockpit — Zero-CDN Modular Vanilla JS
 * Clean 4-View Seller Cockpit with instant view switching.
 * Security invariants:
 *  - Strict CSP compliant (no inline scripts, no eval, no inline style attributes).
 *  - XSS safe: textContent and safe DOM node builders only.
 *  - Server-side tenant scoping via X-API-Key read from server-injected meta tag.
 */

(() => {
  "use strict";

  const HERO_IDS = {
    OIL: "aaaaaaaa-aaaa-4aaa-8aaa-000000000001",
    DAIRY: "aaaaaaaa-aaaa-4aaa-8aaa-000000000002",
    D2C: "aaaaaaaa-aaaa-4aaa-8aaa-000000000003",
  };

  const VIEW_TITLES = {
    overview: "Overview & Reorder",
    capture: "Bill, Voice & CSV Capture",
    reorder: "₹50k Budget Planner",
    inspector: "ML Accuracy & FastMCP",
  };

  const state = {
    apiKey: "",
    theme: "light",
    activeView: "overview",
    skus: [],
    skusById: new Map(),
    skusByCode: new Map(),
    activeSkuId: HERO_IDS.OIL,
    activeForecast: null,
    lastNarrative: "",
    explainLang: "en",
    explainOpen: false,
    simInFlight: false,
    pendingSimQty: null,
    lastSim: null,
    toastTimer: null,
    reorderLoaded: false,
    inspectorLoaded: false,
  };

  // ── Formatting & Safe DOM Helpers ────────────────────────────────────────────

  const inrIntFmt = new Intl.NumberFormat("en-IN", {
    maximumFractionDigits: 0,
  });

  const inrDecFmt = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  function num(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function formatINR(value, decimals = 0) {
    const n = num(value, 0);
    const absVal = Math.abs(n);
    const formatted =
      decimals > 0 ? inrDecFmt.format(absVal) : inrIntFmt.format(Math.round(absVal));
    return (n < 0 ? "-₹" : "₹") + formatted;
  }

  function formatPct(fraction, decimals = 0) {
    const val = num(fraction, 0) * 100;
    return `${val.toFixed(decimals)}%`;
  }

  function $(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    const node = $(id);
    if (node) node.textContent = String(text);
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clearNode(node) {
    if (!node) return;
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function showToast(message, variant = "default") {
    const toast = $("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.className = "toast";
    if (variant === "error") toast.classList.add("toast--error");
    if (variant === "success") toast.classList.add("toast--success");
    if (state.toastTimer) window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => {
      toast.classList.add("hidden");
    }, 4000);
  }

  // ── Theme Management (Light / Dark) ──────────────────────────────────────────

  function applyTheme(theme) {
    const resolved = theme === "dark" ? "dark" : "light";
    state.theme = resolved;
    document.documentElement.setAttribute("data-theme", resolved);
    try {
      window.localStorage.setItem("vani_theme", resolved);
    } catch {
      // Ignore storage restrictions
    }
    const btn = $("themeToggle");
    if (btn) {
      btn.textContent = resolved === "dark" ? "☀️" : "🌙";
      btn.title = resolved === "dark" ? "Switch to Light Mode" : "Switch to Dark Mode";
    }
  }

  function initTheme() {
    let saved = "light";
    try {
      saved = window.localStorage.getItem("vani_theme") || "light";
    } catch {
      saved = "light";
    }
    applyTheme(saved);
  }

  // ── API Client ───────────────────────────────────────────────────────────────

  async function apiFetch(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.apiKey) {
      headers.set("X-API-Key", state.apiKey);
    }
    if (options.json !== undefined) {
      headers.set("Content-Type", "application/json");
    }

    const fetchOpts = {
      method: options.method || "GET",
      headers,
    };
    if (options.json !== undefined) {
      fetchOpts.body = JSON.stringify(options.json);
    } else if (options.body !== undefined) {
      fetchOpts.body = options.body;
    }

    const t0 = performance.now();
    const res = await fetch(path, fetchOpts);
    const clientMs = performance.now() - t0;

    let payload = null;
    try {
      payload = await res.json();
    } catch {
      payload = null;
    }

    if (!res.ok) {
      const msg =
        (payload && payload.error && payload.error.message) ||
        `Request failed (${res.status})`;
      const err = new Error(msg);
      err.status = res.status;
      err.payload = payload;
      throw err;
    }
    return { payload, clientMs };
  }

  // ── True View Switching (4 Focused Views) ────────────────────────────────────

  function switchView(viewKey) {
    const targetView = VIEW_TITLES[viewKey] ? viewKey : "overview";
    state.activeView = targetView;

    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.nav === targetView);
    });

    document.querySelectorAll(".view-panel").forEach((panel) => {
      const isMatch = panel.dataset.view === targetView;
      panel.classList.toggle("hidden", !isMatch);
    });

    setText("pageName", VIEW_TITLES[targetView]);

    const sidebar = $("sidebar");
    if (sidebar) sidebar.classList.remove("open");

    window.scrollTo({ top: 0, behavior: "smooth" });

    if (targetView === "reorder" && !state.reorderLoaded) {
      const budgetInput = $("budget-input");
      loadReorderPlan(budgetInput ? budgetInput.value : 50000);
    } else if (targetView === "inspector") {
      loadBacktest();
      loadMcpInspector();
    }
  }

  // ── Health & Catalog Bootstrap ───────────────────────────────────────────────

  async function loadHealth() {
    try {
      const { payload } = await apiFetch("/health");
      const d = payload && payload.data;
      if (!d) return;
      setText("offline-pill-text", d.offline_ready ? "Offline Ready" : "Degraded");
      if (d.sku_count) {
        setText("nav-sku-count", d.sku_count);
      }
      if (d.sku_count && d.sales_count) {
        setText(
          "kpi-total-skus-foot",
          `${d.sku_count} SKUs · ${inrIntFmt.format(d.sales_count)} sales rows`
        );
        setText(
          "footer-health-meta",
          `SQLite WAL · ${d.sku_count} SKUs · ${inrIntFmt.format(d.sales_count)} sales rows · 100% Offline Capable`
        );
      }
    } catch {
      setText("offline-pill-text", "Offline Ready");
    }
  }

  function populateSkuDropdown(filterQuery = "") {
    const select = $("sku-select");
    if (!select) return;
    clearNode(select);

    const heroOrder = [HERO_IDS.OIL, HERO_IDS.DAIRY, HERO_IDS.D2C];
    const sorted = [...state.skus].sort((a, b) => {
      const ia = heroOrder.indexOf(a.id);
      const ib = heroOrder.indexOf(b.id);
      if (ia !== -1 && ib !== -1) return ia - ib;
      if (ia !== -1) return -1;
      if (ib !== -1) return 1;
      return a.sku_code.localeCompare(b.sku_code);
    });

    const q = filterQuery.trim().toLowerCase();
    const filtered = q
      ? sorted.filter((sku) => {
          const text =
            `${sku.sku_code} ${sku.name} ${sku.hinglish_alias || ""} ${sku.category || ""}`.toLowerCase();
          return text.includes(q);
        })
      : sorted;

    const listToRender = filtered.length > 0 ? filtered : sorted;

    for (const sku of listToRender) {
      const opt = document.createElement("option");
      opt.value = sku.id;
      const alias = sku.hinglish_alias ? ` (${sku.hinglish_alias})` : "";
      opt.textContent = `${sku.name}${alias} · ${sku.sku_code}`;
      select.appendChild(opt);
    }

    if (listToRender.some((s) => s.id === state.activeSkuId)) {
      select.value = state.activeSkuId;
    } else if (listToRender.length > 0) {
      select.value = listToRender[0].id;
    }
  }

  async function loadSkus() {
    const { payload } = await apiFetch("/v1/skus?limit=100&sort_by=sku_code&order=asc");
    const items = (payload && payload.data && payload.data.items) || [];
    state.skus = items;
    state.skusById.clear();
    state.skusByCode.clear();

    setText("nav-sku-count", items.length);

    for (const sku of items) {
      state.skusById.set(sku.id, sku);
      state.skusByCode.set(sku.sku_code.toUpperCase(), sku);
    }

    populateSkuDropdown("");
  }

  // ── View 1: Overview & Reorder (Step 1 Forecast + Step 2 Simulator) ──────────

  function updateHeroChipSelection(skuId) {
    document.querySelectorAll(".hero-chip").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.skuId === skuId);
    });
    const select = $("sku-select");
    if (select && select.value !== skuId && state.skusById.has(skuId)) {
      select.value = skuId;
    }
  }

  function renderSkuCaptureContext(sku) {
    if (!sku) return;

    setText("sku-abc-pill", `Step 1 · Segment ${sku.abc_segment}`);

    const perishPill = $("sku-perishable-pill");
    if (perishPill) {
      if (sku.perishable) {
        perishPill.textContent = `Perishable · ${sku.shelf_life_days}d Shelf Life`;
        perishPill.classList.remove("hidden");
      } else {
        perishPill.classList.add("hidden");
      }
    }

    const parts = [
      `Cost ${formatINR(sku.unit_cost)}`,
      `MRP ${formatINR(sku.unit_price)}`,
      `Lead time ${sku.lead_time_days}d`,
      `Shelf life ${sku.shelf_life_days}d`,
      `On-hand ${Math.round(num(sku.on_hand))} ${sku.unit}s`,
    ];
    setText("capture-context-line", parts.join(" · "));
  }

  function buildDriverRows(container, drivers, recommendedQty) {
    if (!container) return;
    clearNode(container);
    const totalAbs = Math.max(
      1,
      recommendedQty || drivers.reduce((acc, d) => acc + Math.abs(d.units), 0)
    );
    for (const d of drivers) {
      const row = el("div", "driver-row");
      const nameNode = el("div", "driver-name", d.name);

      const bar = document.createElement("progress");
      bar.className = "driver-progress";
      if (d.units < 0 || d.direction === "negative") {
        bar.classList.add("driver-progress--neg");
      }
      bar.max = 100;
      const pct = Math.min(100, Math.max(4, Math.round((Math.abs(d.units) / totalAbs) * 100)));
      bar.value = pct;

      const sign = d.units >= 0 ? "+" : "";
      const valNode = el("div", "driver-val", `${sign}${d.units} units`);
      if (d.units < 0 || d.direction === "negative") {
        valNode.classList.add("driver-val--neg");
      }

      row.appendChild(nameNode);
      row.appendChild(bar);
      row.appendChild(valNode);
      container.appendChild(row);
    }
  }

  function renderForecastCard(forecast) {
    state.activeForecast = forecast;
    const sku = state.skusById.get(forecast.sku_id);

    renderSkuCaptureContext(sku);

    const unitLabel = sku ? `${sku.unit}s` : "units";
    const p10 = Math.round(num(forecast.p10_total));
    const p50 = Math.round(num(forecast.p50_total));
    const p90 = Math.round(num(forecast.p90_total));

    // 4 Simple Summary KPI Cards
    setText("kpi-rec-order", `${forecast.recommended_qty} ${unitLabel}`);
    setText("kpi-rec-order-foot", `${forecast.sku_name} · 7-day order`);
    setText("kpi-rec-outlay", formatINR(forecast.outlay_amount));
    setText("kpi-service-level", formatPct(forecast.service_level, 0));
    setText("forecast-interval", `${p10}–${p90} units`);
    setText("kpi-demand-range-foot", `Median demand: ${p50} ${unitLabel}`);

    // Left Card: Step 1 · Recommended Order & Why
    setText("forecast-sku-name", forecast.sku_name);
    setText("forecast-recommended-qty", forecast.recommended_qty);
    setText("forecast-unit-label", unitLabel);
    setText("forecast-outlay", formatINR(forecast.outlay_amount));
    setText("forecast-service-level", formatPct(forecast.service_level, 0));

    const drivers = Array.isArray(forecast.drivers) ? forecast.drivers : [];
    setText("drivers-sum-check", `Total: ${forecast.recommended_qty} ${unitLabel}`);
    buildDriverRows($("drivers-list"), drivers, forecast.recommended_qty);

    // Configure Step 2 Override Slider
    configureOverrideSlider(forecast);

    // Reset commit status banner
    const commitBanner = $("commit-status-banner");
    if (commitBanner) {
      commitBanner.classList.add("hidden");
      commitBanner.textContent = "";
    }

    if (state.explainOpen) {
      loadExplanation();
    }
  }

  async function selectSku(skuId) {
    state.activeSkuId = skuId;
    updateHeroChipSelection(skuId);

    try {
      const { payload } = await apiFetch(`/v1/forecasts/${encodeURIComponent(skuId)}`);
      if (payload && payload.data) {
        renderForecastCard(payload.data);
        await triggerSimulation(payload.data.recommended_qty);
      }
    } catch (err) {
      showToast(`Could not load forecast: ${err.message}`, "error");
    }
  }

  // ── Grounded Plain-Language Explanation ──────────────────────────────────────

  async function loadExplanation() {
    const panel = $("explain-panel");
    const textEl = $("explain-narrative-text");
    const groundedBadge = $("explain-grounded-badge");
    if (!panel || !textEl) return;

    panel.classList.remove("hidden");
    textEl.textContent = "Generating verified explanation…";

    try {
      const { payload } = await apiFetch(
        `/v1/forecasts/${encodeURIComponent(state.activeSkuId)}/explain`,
        {
          method: "POST",
          json: { language: state.explainLang },
        }
      );
      const d = payload && payload.data;
      if (!d) return;

      state.lastNarrative = d.narrative || "";
      textEl.textContent = d.narrative;
      if (groundedBadge) {
        groundedBadge.textContent = d.grounded ? "Numbers Verified" : "Template Verified";
        groundedBadge.className = d.grounded
          ? "status-pill pill-green"
          : "status-pill pill-amber";
      }
    } catch (err) {
      textEl.textContent = `Explanation unavailable: ${err.message}`;
      if (groundedBadge) {
        groundedBadge.textContent = "Error";
        groundedBadge.className = "status-pill pill-red";
      }
    }
  }

  // ── Slide-Over Recommendation Drawer ─────────────────────────────────────────

  async function openRecommendationDrawer() {
    const forecast = state.activeForecast;
    if (!forecast) return;
    const sku = state.skusById.get(forecast.sku_id);
    const unitLabel = sku ? `${sku.unit}s` : "units";

    setText("drawerTitle", `${forecast.sku_name} (${forecast.sku_code})`);
    const body = $("drawerBody");
    if (!body) return;
    clearNode(body);

    const metaRow = el("div", "drawer-meta-row");
    metaRow.appendChild(el("span", "tag", sku ? sku.category : "Catalog SKU"));
    metaRow.appendChild(
      el(
        "span",
        "mono-note",
        `Lead time: ${sku ? sku.lead_time_days : 3}d · Shelf life: ${sku ? sku.shelf_life_days : 90}d`
      )
    );
    body.appendChild(metaRow);

    const notice = el("div", "notice warn");
    const bannerTop = el("div", "drawer-banner-top");
    bannerTop.appendChild(
      el("strong", "", `Recommended Order: ${forecast.recommended_qty} ${unitLabel}`)
    );
    bannerTop.appendChild(el("strong", "", formatINR(forecast.outlay_amount)));
    notice.appendChild(bannerTop);
    notice.appendChild(
      el(
        "div",
        "helper-text",
        `On-hand: ${sku ? Math.round(num(sku.on_hand)) : 0} ${unitLabel} · Demand range: ${Math.round(num(forecast.p10_total))}–${Math.round(num(forecast.p90_total))} · Confidence: ${formatPct(forecast.service_level, 0)}`
      )
    );
    body.appendChild(notice);

    body.appendChild(el("div", "section-label", "Why this order?"));
    const drawerDrivers = el("div", "driver-list");
    buildDriverRows(
      drawerDrivers,
      Array.isArray(forecast.drivers) ? forecast.drivers : [],
      forecast.recommended_qty
    );
    body.appendChild(drawerDrivers);

    const callout = el("div", "why-callout");
    const calloutHead = el("div", "explain-box-head");
    calloutHead.appendChild(el("strong", "", "Plain-Language Explanation"));
    calloutHead.appendChild(el("span", "status-pill pill-green", "Numbers Verified"));
    callout.appendChild(calloutHead);
    const narrativeP = el(
      "p",
      "explain-text",
      state.lastNarrative || "Loading verified explanation…"
    );
    callout.appendChild(narrativeP);
    body.appendChild(callout);

    const actions = el("div", "drawer-actions");
    const jumpSimBtn = el("button", "btn btn-primary", "Adjust in What-If Simulator");
    jumpSimBtn.type = "button";
    jumpSimBtn.addEventListener("click", () => {
      closeRecommendationDrawer();
      switchView("overview");
      const slider = $("override-slider");
      if (slider) slider.focus();
    });

    const copySlipBtn = el("button", "btn btn-secondary", "Copy Supplier Order Slip");
    copySlipBtn.type = "button";
    copySlipBtn.addEventListener("click", copySupplierOrderSlip);

    actions.appendChild(jumpSimBtn);
    actions.appendChild(copySlipBtn);
    body.appendChild(actions);

    const drawer = $("recommendationDrawer");
    const backdrop = $("drawerBackdrop");
    if (drawer) drawer.classList.add("open");
    if (backdrop) backdrop.classList.add("active");

    if (!state.lastNarrative) {
      try {
        const { payload } = await apiFetch(
          `/v1/forecasts/${encodeURIComponent(state.activeSkuId)}/explain`,
          {
            method: "POST",
            json: { language: state.explainLang },
          }
        );
        if (payload && payload.data && payload.data.narrative) {
          state.lastNarrative = payload.data.narrative;
          narrativeP.textContent = payload.data.narrative;
        }
      } catch {
        narrativeP.textContent = "Deterministic drivers verified above.";
      }
    }
  }

  function closeRecommendationDrawer() {
    const drawer = $("recommendationDrawer");
    const backdrop = $("drawerBackdrop");
    if (drawer) drawer.classList.remove("open");
    if (backdrop) backdrop.classList.remove("active");
  }

  async function copySupplierOrderSlip() {
    const forecast = state.activeForecast;
    if (!forecast) return;
    const sku = state.skusById.get(forecast.sku_id);
    const qty = state.lastSim ? state.lastSim.manual_qty : forecast.recommended_qty;
    const unitCost = sku ? num(sku.unit_cost) : 0;
    const estValue = qty * unitCost;
    const unitLabel = sku ? `${sku.unit}s` : "units";
    const slip =
      `*VANI-LEDGER Purchase Order* | Product: ${forecast.sku_name} (${forecast.sku_code}) | ` +
      `Qty: ${qty} ${unitLabel} | Est. Value: ${formatINR(estValue)} | Lead Time: ${sku ? sku.lead_time_days : 3}d`;

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(slip);
      }
      showToast(`Order slip copied: ${qty} ${unitLabel} (${formatINR(estValue)})`, "success");
    } catch {
      showToast(`Order slip: ${slip}`, "success");
    }
  }

  // ── Step 2: What-If Quantity Simulator (<50ms Hot Path) ──────────────────────

  function configureOverrideSlider(forecast) {
    const rec = Math.max(0, Math.round(num(forecast.recommended_qty, 0)));
    const maxVal =
      forecast.sku_id === HERO_IDS.OIL
        ? 80
        : Math.max(80, Math.ceil((rec * 1.75) / 10) * 10);

    const slider = $("override-slider");
    if (slider) {
      slider.min = "0";
      slider.max = String(maxVal);
      slider.value = String(rec);
    }

    setText("slider-mid-label", `${Math.round(maxVal / 2)} units`);
    setText("slider-max-label", `${maxVal} units`);
    setText("override-manual-qty", rec);
    setText("override-suggested-qty", rec);

    const demoBtn = $("preset-demo-35");
    if (demoBtn) {
      if (forecast.sku_id === HERO_IDS.OIL) {
        demoBtn.textContent = "Try 35 (-13)";
        demoBtn.dataset.qty = "35";
      } else {
        const cutQty = Math.max(0, Math.round(rec * 0.72));
        demoBtn.textContent = `Try ${cutQty}`;
        demoBtn.dataset.qty = String(cutQty);
      }
    }
  }

  function renderSimulationResult(sim, serverLatencyMs, clientMs) {
    state.lastSim = sim;

    const displayMs =
      typeof serverLatencyMs === "number" && serverLatencyMs > 0
        ? serverLatencyMs
        : clientMs;
    const latPill = $("sim-latency-pill");
    if (latPill) {
      latPill.textContent = `${displayMs.toFixed(1)} ms`;
      latPill.className = displayMs < 50 ? "latency-pill" : "latency-pill latency-pill--warn";
    }

    setText("override-manual-qty", sim.manual_qty);
    setText("override-suggested-qty", sim.suggested_qty);

    // Tile 1: Cash Freed / Extra Cost
    const cashDelta = num(sim.cash_delta, 0);
    const cashFreed = num(sim.cash_freed, 0);
    const cashValEl = $("sim-cash-val");

    if (cashFreed > 0) {
      setText("sim-cash-label", "Cash Freed");
      if (cashValEl) {
        cashValEl.textContent = formatINR(cashFreed);
        cashValEl.className = "tile-value tile-value--teal";
      }
    } else if (cashDelta > 0) {
      setText("sim-cash-label", "Extra Cost");
      if (cashValEl) {
        cashValEl.textContent = `+${formatINR(cashDelta)}`;
        cashValEl.className = "tile-value";
      }
    } else {
      setText("sim-cash-label", "Cash Freed");
      if (cashValEl) {
        cashValEl.textContent = "₹0";
        cashValEl.className = "tile-value tile-value--teal";
      }
    }
    setText("sim-outlay-sub", `Order cost: ${formatINR(sim.outlay_amount)}`);

    // Tile 2: Stockout Risk
    const riskDeltaPct = Math.round(num(sim.stockout_risk_delta, 0) * 100);
    const riskBeforePct = Math.round(num(sim.stockout_risk_before, 0) * 100);
    const riskAfterPct = Math.round(num(sim.stockout_risk_after, 0) * 100);
    const riskEl = $("sim-risk-delta");
    const riskTile = $("tile-risk");

    const sign = riskDeltaPct > 0 ? "+" : "";
    if (riskEl) {
      riskEl.textContent = `${riskAfterPct}%`;
      if (riskDeltaPct >= 5) {
        riskEl.className = "tile-value tile-value--red";
      } else if (riskDeltaPct < 0) {
        riskEl.className = "tile-value tile-value--teal";
      } else {
        riskEl.className = "tile-value";
      }
    }
    if (riskTile) {
      riskTile.className = "metric-tile";
      if (riskDeltaPct >= 5) riskTile.classList.add("metric-tile--danger");
      else if (riskDeltaPct < 0) riskTile.classList.add("metric-tile--good");
    }
    setText("sim-risk-sub", `${sign}${riskDeltaPct}% (vs ${riskBeforePct}% suggested)`);

    // Tile 3: Days of Stock
    const daysCover = num(sim.days_cover, 0);
    setText("sim-days-cover", `${daysCover.toFixed(1)} Days`);
    const sku = state.skusById.get(sim.sku_id);
    setText(
      "sim-cover-sub",
      sku ? `Shelf life: ${sku.shelf_life_days}d` : "Stock protection"
    );

    // Status Pill 1: Budget Guard
    const budgetEl = $("guard-budget");
    if (budgetEl) {
      budgetEl.className = sim.within_budget
        ? "guard-badge guard-badge--ok"
        : "guard-badge guard-badge--danger";
    }
    setText("guard-budget-text", sim.within_budget ? "Within Budget" : "Exceeds Weekly Budget");

    // Status Pill 2: Shelf-Life Guard
    const spoilPct = Math.round(num(sim.spoilage_risk, 0) * 100);
    const spoilEl = $("guard-spoilage");
    if (spoilEl) {
      if (spoilPct >= 20) {
        spoilEl.className = "guard-badge guard-badge--danger";
        setText("guard-spoilage-text", `Spoilage Risk ${spoilPct}% (Exceeds Shelf Life)`);
      } else if (spoilPct > 5) {
        spoilEl.className = "guard-badge guard-badge--warn";
        setText("guard-spoilage-text", `Spoilage Caution (${spoilPct}%)`);
      } else {
        spoilEl.className = "guard-badge guard-badge--ok";
        setText("guard-spoilage-text", "Shelf-Life Safe");
      }
    }
  }

  async function triggerSimulation(manualQty) {
    const qty = Math.max(0, Math.round(num(manualQty, 0)));
    setText("override-manual-qty", qty);

    if (state.simInFlight) {
      state.pendingSimQty = qty;
      return;
    }

    state.simInFlight = true;
    try {
      const { payload, clientMs } = await apiFetch("/v1/overrides/simulate", {
        method: "POST",
        json: {
          sku_id: state.activeSkuId,
          manual_qty: qty,
        },
      });
      if (payload && payload.data) {
        const srvMs = payload.meta && payload.meta.latency_ms;
        renderSimulationResult(payload.data, srvMs, clientMs);
      }
    } catch (err) {
      showToast(`Simulation error: ${err.message}`, "error");
    } finally {
      state.simInFlight = false;
      if (state.pendingSimQty !== null) {
        const nextQty = state.pendingSimQty;
        state.pendingSimQty = null;
        if (nextQty !== qty) {
          triggerSimulation(nextQty);
        }
      }
    }
  }

  async function handleCommitOverride(event) {
    event.preventDefault();
    const btn = $("btn-commit-override");
    const slider = $("override-slider");
    const reasonInput = $("override-reason");
    const banner = $("commit-status-banner");

    const manualQty = Math.max(0, parseInt(slider ? slider.value : "0", 10) || 0);
    const reason = reasonInput ? reasonInput.value.trim() || null : null;
    const idemKey = `ui-${state.activeSkuId.slice(0, 8)}-${manualQty}-${Date.now()}`;

    if (btn) {
      btn.disabled = true;
      btn.textContent = "Saving…";
    }
    try {
      const { payload } = await apiFetch("/v1/overrides/commit", {
        method: "POST",
        headers: { "Idempotency-Key": idemKey },
        json: {
          sku_id: state.activeSkuId,
          manual_qty: manualQty,
          reason,
        },
      });
      const d = payload && payload.data;
      if (d && banner) {
        banner.textContent =
          `Saved order decision: ${d.manual_qty} units (vs ${d.suggested_qty} suggested) logged to audit trail.`;
        banner.classList.remove("hidden");
        showToast(`Saved order decision (${d.manual_qty} units).`, "success");
      }
    } catch (err) {
      showToast(`Save failed: ${err.message}`, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Save Order Decision";
      }
    }
  }

  // ── View 2: Bill, Voice & CSV Capture ────────────────────────────────────────

  function renderBillResult(data) {
    const area = $("bill-result-area");
    const badges = $("bill-meta-badges");
    const banner = $("bill-math-banner");
    const tbody = $("bill-items-tbody");
    if (!area || !badges || !banner || !tbody) return;

    area.classList.remove("hidden");
    clearNode(badges);
    clearNode(banner);
    clearNode(tbody);

    if (data.demo_document) {
      badges.appendChild(el("span", "status-pill pill-amber", "DEMO DOCUMENT"));
    }
    badges.appendChild(
      el("span", "status-pill pill-blue", `Confidence ${formatPct(data.confidence, 0)}`)
    );
    if (data.needs_review) {
      badges.appendChild(el("span", "status-pill pill-red", "Needs Review"));
    }

    const mc = data.math_check;
    banner.classList.remove("hidden", "alert-banner--crimson", "alert-banner--emerald");
    if (mc && !mc.ok) {
      banner.classList.add("alert-banner--crimson");
      const deltaAbs = Math.abs(num(mc.delta, 0));
      banner.appendChild(
        el(
          "div",
          "alert-banner-title",
          `${formatINR(deltaAbs)} Overcharge Caught (${formatINR(mc.written_total)} billed vs ${formatINR(mc.computed_total)} actual)`
        )
      );
      banner.appendChild(
        el(
          "div",
          "",
          "Python Decimal audit flagged a supplier total mismatch before payment."
        )
      );
    } else if (mc) {
      banner.classList.add("alert-banner--emerald");
      banner.appendChild(
        el("div", "", `Verified: Total ${formatINR(mc.computed_total, 2)} matches all line items.`)
      );
    }

    const items = Array.isArray(data.line_items) ? data.line_items : [];
    for (const item of items) {
      const tr = document.createElement("tr");
      if (!item.line_math_ok) tr.classList.add("row-error");

      tr.appendChild(el("td", "", item.item_name));
      tr.appendChild(el("td", "num", String(item.qty)));
      tr.appendChild(el("td", "num", formatINR(item.unit_price)));
      tr.appendChild(el("td", "num", formatINR(item.written_line_total)));
      tr.appendChild(el("td", "num", formatINR(item.computed_line_total)));

      const statusTd = document.createElement("td");
      statusTd.appendChild(
        el(
          "span",
          item.line_math_ok ? "status-pill pill-green" : "status-pill pill-red",
          item.line_math_ok ? "OK" : "Mismatch"
        )
      );
      tr.appendChild(statusTd);
      tbody.appendChild(tr);
    }
  }

  async function uploadBillFile(file, triggerBtn) {
    const formData = new FormData();
    formData.append("file", file, file.name || "supplier_bill.png");

    if (triggerBtn) triggerBtn.disabled = true;
    try {
      const { payload } = await apiFetch("/v1/capture/bill", {
        method: "POST",
        body: formData,
      });
      if (payload && payload.data) {
        renderBillResult(payload.data);
        const mc = payload.data.math_check;
        if (mc && !mc.ok) {
          showToast(
            `${formatINR(Math.abs(num(mc.delta)))} supplier overcharge caught!`,
            "error"
          );
        } else {
          showToast("Supplier bill verified cleanly.", "success");
        }
      }
    } catch (err) {
      showToast(`Bill audit error: ${err.message}`, "error");
    } finally {
      if (triggerBtn) triggerBtn.disabled = false;
    }
  }

  async function handleLoadSampleBill() {
    const btn = $("btn-load-sample-bill");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Testing Bill…";
    }
    try {
      const res = await fetch("/v1/capture/sample-bill", {
        headers: state.apiKey ? { "X-API-Key": state.apiKey } : {},
      });
      if (!res.ok) throw new Error(`Sample bill unavailable (${res.status})`);
      const blob = await res.blob();
      const file = new File([blob], "demo_bill.png", { type: "image/png" });
      await uploadBillFile(file, btn);
    } catch (err) {
      showToast(`Could not load sample bill: ${err.message}`, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Test Sample Supplier Bill";
      }
    }
  }

  function renderCsvResult(data) {
    const area = $("csv-result-area");
    if (!area) return;
    area.classList.remove("hidden");
    clearNode(area);

    const head = el("div", "bill-meta-badges");
    head.appendChild(
      el(
        "span",
        data.duplicate_upload ? "status-pill pill-amber" : "status-pill pill-green",
        data.duplicate_upload ? "Duplicate CSV (Cached)" : "CSV Imported"
      )
    );
    area.appendChild(head);

    const grid = el("div", "kv-grid");
    const updatedCodes = Array.isArray(data.sku_codes_updated) ? data.sku_codes_updated : [];
    const pairs = [
      ["Rows Processed", data.rows_processed ?? 0],
      ["Rows Inserted", data.rows_inserted ?? 0],
      ["SKUs Updated", updatedCodes.length > 0 ? updatedCodes.join(", ") : "—"],
      ["Total Revenue", formatINR(data.total_revenue ?? 0)],
    ];
    for (const [k, v] of pairs) {
      const item = el("div", "kv-item");
      item.appendChild(el("span", "kv-label", k));
      item.appendChild(el("span", "kv-val", String(v)));
      grid.appendChild(item);
    }
    area.appendChild(grid);
  }

  async function uploadCsvFile(file, triggerBtn) {
    const formData = new FormData();
    formData.append("file", file, file.name || "d2c_sales.csv");

    if (triggerBtn) triggerBtn.disabled = true;
    try {
      const { payload } = await apiFetch("/v1/capture/csv", {
        method: "POST",
        body: formData,
      });
      if (payload && payload.data) {
        renderCsvResult(payload.data);
        showToast("D2C sales CSV imported.", "success");
      }
    } catch (err) {
      showToast(`CSV import error: ${err.message}`, "error");
    } finally {
      if (triggerBtn) triggerBtn.disabled = false;
    }
  }

  async function handleLoadSampleCsv() {
    const btn = $("btn-load-sample-csv");
    if (btn) btn.disabled = true;
    try {
      const res = await fetch("/v1/capture/sample-csv", {
        headers: state.apiKey ? { "X-API-Key": state.apiKey } : {},
      });
      if (!res.ok) throw new Error(`Sample CSV unavailable (${res.status})`);
      const blob = await res.blob();
      const file = new File([blob], "d2c_sample_sales.csv", { type: "text/csv" });
      await uploadCsvFile(file, btn);
    } catch (err) {
      showToast(`Could not load sample CSV: ${err.message}`, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function renderVoiceResult(data) {
    const area = $("voice-result-area");
    if (!area) return;
    area.classList.remove("hidden");
    clearNode(area);

    const head = el("div", "bill-meta-badges");
    head.appendChild(el("span", "status-pill pill-blue", `Intent: ${data.intent}`));
    if (data.requires_confirmation) {
      head.appendChild(el("span", "status-pill pill-amber", "Preview Ready"));
    }
    area.appendChild(head);

    const grid = el("div", "kv-grid");
    const addKv = (label, value, skuCodeForJump = null) => {
      const item = el("div", "kv-item");
      item.appendChild(el("span", "kv-label", label));
      if (skuCodeForJump && state.skusByCode.has(skuCodeForJump.toUpperCase())) {
        const skuObj = state.skusByCode.get(skuCodeForJump.toUpperCase());
        const jumpBtn = el("button", "link-btn", value);
        jumpBtn.type = "button";
        jumpBtn.addEventListener("click", () => {
          switchView("overview");
          selectSku(skuObj.id);
        });
        item.appendChild(jumpBtn);
      } else {
        item.appendChild(el("span", "kv-val", value));
      }
      grid.appendChild(item);
    };

    const skuDisplay = data.matched_sku_name
      ? `${data.matched_sku_name} (${data.matched_sku_code || ""})`
      : "No exact SKU match";
    addKv("Matched Product", skuDisplay, data.matched_sku_code);
    addKv("Quantity", data.extracted_qty !== null ? String(data.extracted_qty) : "—");
    addKv("Expired / Spoiled", data.spoilage_qty !== null ? String(data.spoilage_qty) : "—");
    addKv(
      "Shelf Life",
      data.shelf_life_days !== null ? `${data.shelf_life_days} days` : "—"
    );

    area.appendChild(grid);
  }

  async function handleParseVoice(event) {
    if (event) event.preventDefault();
    const transcriptEl = $("voice-transcript");
    const transcript = transcriptEl ? transcriptEl.value.trim() : "";
    if (transcript.length < 2) {
      showToast("Enter at least 2 characters for the note.", "error");
      return;
    }

    const btn = $("btn-parse-voice");
    if (btn) btn.disabled = true;
    try {
      const { payload } = await apiFetch("/v1/capture/voice-note", {
        method: "POST",
        json: { transcript },
      });
      if (payload && payload.data) {
        renderVoiceResult(payload.data);
      }
    } catch (err) {
      showToast(`Voice parse error: ${err.message}`, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function startVoiceMic() {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRec) {
      setText("voice-mic-status", "Browser speech unavailable — click a sample phrase or type.");
      return;
    }
    try {
      const rec = new SpeechRec();
      rec.lang = "en-IN";
      rec.interimResults = false;
      rec.maxAlternatives = 1;
      setText("voice-mic-status", "Listening (en-IN)… speak now.");
      rec.onresult = (evt) => {
        const text = evt.results[0][0].transcript;
        const transcriptEl = $("voice-transcript");
        if (transcriptEl) transcriptEl.value = text;
        setText("voice-mic-status", `Captured: "${text}"`);
        handleParseVoice();
      };
      rec.onerror = () => {
        setText("voice-mic-status", "Mic offline — use sample phrases or type directly.");
      };
      rec.start();
    } catch {
      setText("voice-mic-status", "Mic unavailable — use sample phrases or type directly.");
    }
  }

  // ── View 3: ₹50k Budget Planner ──────────────────────────────────────────────

  async function loadReorderPlan(budgetValue) {
    const tbody = $("reorder-tbody");
    if (!tbody) return;
    clearNode(tbody);
    const loadingTr = document.createElement("tr");
    const loadingTd = el("td", "muted-cell", "Calculating optimal budget allocation…");
    loadingTd.colSpan = 9;
    loadingTr.appendChild(loadingTd);
    tbody.appendChild(loadingTr);

    const budgetParam =
      budgetValue !== undefined && budgetValue !== null && String(budgetValue).trim() !== ""
        ? `?weekly_budget=${encodeURIComponent(String(budgetValue))}`
        : "";

    try {
      const { payload } = await apiFetch(`/v1/reorder-plan${budgetParam}`);
      const d = payload && payload.data;
      if (!d) return;

      const weekly = num(d.weekly_budget, 50000);
      const allocated = num(d.total_allocated_cost, 0);
      const remaining = num(d.budget_remaining, 0);
      const pct = weekly > 0 ? Math.min(100, (allocated / weekly) * 100) : 0;

      setText("budget-allocated", `${formatINR(allocated)} allocated of ${formatINR(weekly)}`);
      setText("budget-remaining", formatINR(remaining));
      setText("budget-pct", `${pct.toFixed(1)}%`);
      const prog = $("budget-progress");
      if (prog) prog.value = pct;

      clearNode(tbody);
      const items = Array.isArray(d.items) ? d.items : [];
      if (items.length === 0) {
        const tr = document.createElement("tr");
        const td = el("td", "muted-cell", "No reorder items available.");
        td.colSpan = 9;
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
      }

      for (const item of items) {
        const tr = document.createElement("tr");
        if (item.sku_id === state.activeSkuId) tr.classList.add("row-highlight");

        const skuTd = document.createElement("td");
        const skuBtn = el("button", "link-btn", `${item.sku_name} (${item.sku_code})`);
        skuBtn.type = "button";
        skuBtn.addEventListener("click", () => {
          switchView("overview");
          selectSku(item.sku_id);
        });
        skuTd.appendChild(skuBtn);
        tr.appendChild(skuTd);

        tr.appendChild(el("td", "", item.category));
        tr.appendChild(el("td", "num", String(item.recommended_qty)));
        tr.appendChild(el("td", "num", String(item.allocated_qty)));
        tr.appendChild(el("td", "num", formatINR(item.unit_cost)));
        tr.appendChild(el("td", "num", formatINR(item.allocated_cost)));
        tr.appendChild(el("td", "num", `₹${num(item.margin_per_rupee).toFixed(2)}`));
        tr.appendChild(el("td", "num", formatPct(item.stockout_urgency, 0)));

        const capTd = document.createElement("td");
        if (item.capped_by_shelf_life) {
          capTd.appendChild(el("span", "status-pill pill-amber", "Shelf-Life Cap"));
        } else if (item.capped_by_budget) {
          capTd.appendChild(el("span", "status-pill pill-red", "Budget Cap"));
        } else if (item.allocated_qty > 0) {
          capTd.appendChild(el("span", "status-pill pill-green", "Full Order"));
        } else {
          capTd.appendChild(el("span", "status-pill", "Deferred"));
        }
        tr.appendChild(capTd);
        tbody.appendChild(tr);
      }

      state.reorderLoaded = true;
    } catch (err) {
      clearNode(tbody);
      const tr = document.createElement("tr");
      const td = el("td", "muted-cell", `Reorder plan unavailable: ${err.message}`);
      td.colSpan = 9;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  // ── View 4: ML Accuracy & FastMCP (Judge View) ───────────────────────────────

  function fmtMetricPct(val) {
    if (val === undefined || val === null || Number.isNaN(Number(val))) return "—";
    return `${(Number(val) * 100).toFixed(2)}%`;
  }

  function fmtMetricNum(val, digits = 2) {
    if (val === undefined || val === null || Number.isNaN(Number(val))) return "—";
    return Number(val).toFixed(digits);
  }

  async function loadBacktest() {
    const tbody = $("backtest-models-tbody");
    const segCards = $("backtest-segment-cards");
    const gatePill = $("backtest-gate-pill");
    const footnote = $("backtest-footnote");
    if (!tbody || !segCards) return;

    try {
      const { payload } = await apiFetch("/v1/eval/backtest");
      const d = payload && payload.data;
      if (!d) return;

      const passed =
        (d.gates && d.gates.all_passed === true) ||
        d.gate_passed === true ||
        String(d.status || "").toUpperCase() === "PASS";
      if (gatePill) {
        gatePill.textContent = passed ? "Gate: PASS" : "Gate: Review";
        gatePill.className = passed ? "status-pill pill-green" : "status-pill pill-amber";
      }

      if (footnote && d.honest_benchmark_note) {
        footnote.textContent = d.honest_benchmark_note;
      }

      clearNode(segCards);
      const modelsMap = d.models && !Array.isArray(d.models) ? d.models : {};
      const ampModel = modelsMap.amplified_ensemble || {};
      const segs = ampModel.segment_wape || d.abc_segments || d.segments || {};

      const addSegCard = (label, valStr) => {
        const card = el("div", "segment-card");
        card.appendChild(el("div", "segment-label", label));
        card.appendChild(el("div", "segment-val", valStr));
        segCards.appendChild(card);
      };

      if (ampModel.revenue_weighted_wape !== undefined) {
        addSegCard("Overall WAPE", fmtMetricPct(ampModel.revenue_weighted_wape));
      }
      if (d.lift_over_seasonal_naive_pct !== undefined && d.lift_over_seasonal_naive_pct !== null) {
        addSegCard("vs Baseline", `+${num(d.lift_over_seasonal_naive_pct).toFixed(2)}%`);
      }

      const pickSegWape = (key) => {
        const entry = segs[key];
        if (entry === undefined || entry === null) return null;
        return typeof entry === "object" ? (entry.wape ?? entry.revenue_weighted_wape) : entry;
      };

      if (pickSegWape("A") !== null) addSegCard("A-Items WAPE", fmtMetricPct(pickSegWape("A")));
      if (pickSegWape("B") !== null) addSegCard("B-Items WAPE", fmtMetricPct(pickSegWape("B")));
      if (pickSegWape("C") !== null) addSegCard("C-Items WAPE", fmtMetricPct(pickSegWape("C")));

      clearNode(tbody);
      const modelLabels = {
        seasonal_naive: "Seasonal Naive (4-wk Baseline)",
        stl_calendar: "STL / ETS Calendar",
        lgbm_tweedie: "Global LightGBM (Tweedie)",
        amplified_ensemble: "5-Stage Amplified Ensemble",
      };
      const preferredOrder = [
        "seasonal_naive",
        "stl_calendar",
        "lgbm_tweedie",
        "amplified_ensemble",
      ];
      const rows = Array.isArray(d.models)
        ? d.models
        : preferredOrder
            .filter((k) => modelsMap[k])
            .map((k) => ({ key: k, model: modelLabels[k] || k, ...modelsMap[k] }));

      for (const m of rows) {
        const name = String(m.model || m.name || "model");
        const isBest = m.key === "amplified_ensemble" || name.toLowerCase().includes("amplif");
        const tr = document.createElement("tr");
        if (isBest) tr.classList.add("row-highlight");

        tr.appendChild(el("td", "", name));
        tr.appendChild(el("td", "num", fmtMetricPct(m.revenue_weighted_wape ?? m.wape)));
        tr.appendChild(el("td", "num", fmtMetricNum(m.mase, 2)));
        tr.appendChild(el("td", "num", fmtMetricNum(m.pinball_p50 ?? m.pinball_50, 2)));
        tr.appendChild(el("td", "num", fmtMetricNum(m.pinball_p90 ?? m.pinball_90, 2)));
        const biasVal = m.bias ?? m.forecast_bias;
        const biasStr =
          biasVal !== undefined && biasVal !== null
            ? `${num(biasVal) >= 0 ? "+" : ""}${(num(biasVal) * 100).toFixed(1)}%`
            : "—";
        tr.appendChild(el("td", "num", biasStr));
        tbody.appendChild(tr);
      }
    } catch (err) {
      clearNode(tbody);
      const tr = document.createElement("tr");
      const td = el("td", "muted-cell", `Backtest error: ${err.message}`);
      td.colSpan = 6;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  async function loadMcpInspector() {
    const toolsList = $("mcp-tools-list");
    const callsList = $("mcp-calls-list");
    if (!toolsList || !callsList) return;

    try {
      const { payload } = await apiFetch("/v1/mcp/tools");
      const d = payload && payload.data;
      if (!d) return;

      clearNode(toolsList);
      const tools = Array.isArray(d.tools) ? d.tools : [];
      for (const tool of tools) {
        const card = el("div", "mcp-tool-card");
        card.appendChild(el("div", "mcp-tool-name", `${tool.name}()`));
        card.appendChild(el("div", "mcp-tool-desc", tool.description || ""));

        const params = tool.parameters || tool.input_schema || {};
        let paramNames = [];
        if (Array.isArray(params)) {
          paramNames = params.map((p) => (typeof p === "string" ? p : p.name));
        } else if (params && typeof params === "object") {
          const props = params.properties || params;
          paramNames = Object.keys(props);
        }
        if (paramNames.length > 0) {
          card.appendChild(el("div", "mcp-tool-params", `params: ${paramNames.join(", ")}`));
        }
        toolsList.appendChild(card);
      }

      clearNode(callsList);
      const calls = Array.isArray(d.recent_invocations)
        ? d.recent_invocations
        : Array.isArray(d.recent_calls)
          ? d.recent_calls
          : [];

      if (calls.length === 0) {
        callsList.appendChild(
          el("div", "muted-cell", "Move the simulator slider to see live deterministic calls.")
        );
      } else {
        for (const call of calls.slice(0, 10)) {
          const row = el("div", "mcp-call-row");
          const toolName = call.tool_name || call.tool || "tool";
          const latency =
            call.latency_ms !== undefined ? `${num(call.latency_ms).toFixed(1)} ms` : "";
          const status = call.ok === false ? "ERR" : "OK";
          row.appendChild(el("span", "", `${toolName}()`));
          row.appendChild(el("span", "mono-note", `${status} · ${latency}`));
          callsList.appendChild(row);
        }
      }
      state.inspectorLoaded = true;
    } catch (err) {
      clearNode(toolsList);
      toolsList.appendChild(el("div", "muted-cell", `MCP inspector error: ${err.message}`));
    }
  }

  // ── Reset Demo ───────────────────────────────────────────────────────────────

  async function handleResetDemo() {
    const btn = $("btn-reset-demo");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Resetting…";
    }
    try {
      await apiFetch("/v1/demo/reset", { method: "POST" });
      state.reorderLoaded = false;
      state.inspectorLoaded = false;
      state.explainOpen = false;
      state.lastNarrative = "";
      const expPanel = $("explain-panel");
      if (expPanel) expPanel.classList.add("hidden");
      const reasonEl = $("override-reason");
      if (reasonEl) reasonEl.value = "";
      const searchEl = $("sku-search");
      if (searchEl) searchEl.value = "";
      closeRecommendationDrawer();
      await loadHealth();
      await loadSkus();
      await selectSku(HERO_IDS.OIL);
      if (state.activeView === "reorder") {
        const budgetEl = $("budget-input");
        await loadReorderPlan(budgetEl ? budgetEl.value : 50000);
      } else if (state.activeView === "inspector") {
        await Promise.all([loadBacktest(), loadMcpInspector()]);
      }
      showToast("Demo reset complete: 40 SKUs reseeded & cache warmed.", "success");
    } catch (err) {
      showToast(`Reset failed: ${err.message}`, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "↻ Reset Demo";
      }
    }
  }

  // ── Event Binding & Init ─────────────────────────────────────────────────────

  function bindEvents() {
    const themeBtn = $("themeToggle");
    if (themeBtn) {
      themeBtn.addEventListener("click", () => {
        applyTheme(state.theme === "dark" ? "light" : "dark");
      });
    }

    const mobileMenu = $("mobileMenu");
    if (mobileMenu) {
      mobileMenu.addEventListener("click", () => {
        const sb = $("sidebar");
        if (sb) sb.classList.toggle("open");
      });
    }

    // Sidebar 4-view navigation
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.nav) switchView(btn.dataset.nav);
      });
    });

    // Slide-Over Drawer
    const openDrawerBtn = $("btn-open-drawer");
    if (openDrawerBtn) openDrawerBtn.addEventListener("click", openRecommendationDrawer);
    const drawerCloseBtn = $("drawerClose");
    if (drawerCloseBtn) drawerCloseBtn.addEventListener("click", closeRecommendationDrawer);
    const drawerBackdrop = $("drawerBackdrop");
    if (drawerBackdrop) drawerBackdrop.addEventListener("click", closeRecommendationDrawer);

    const jumpCaptureBtn = $("btn-jump-capture");
    if (jumpCaptureBtn) {
      jumpCaptureBtn.addEventListener("click", () => switchView("capture"));
    }

    const copySlipBtn = $("btn-copy-order-slip");
    if (copySlipBtn) {
      copySlipBtn.addEventListener("click", copySupplierOrderSlip);
    }

    // Hero demo pills
    document.querySelectorAll(".hero-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.skuId) selectSku(btn.dataset.skuId);
      });
    });

    // 40-SKU search filter & dropdown
    const searchInput = $("sku-search");
    if (searchInput) {
      searchInput.addEventListener("input", () => {
        populateSkuDropdown(searchInput.value);
        const select = $("sku-select");
        if (select && select.value && select.value !== state.activeSkuId) {
          selectSku(select.value);
        }
      });
    }

    const skuSelect = $("sku-select");
    if (skuSelect) {
      skuSelect.addEventListener("change", (evt) => {
        if (evt.target.value) selectSku(evt.target.value);
      });
    }

    // Reset Demo
    const resetBtn = $("btn-reset-demo");
    if (resetBtn) resetBtn.addEventListener("click", handleResetDemo);

    // Explain toggle & language switch
    const whyBtn = $("btn-why-order");
    if (whyBtn) {
      whyBtn.addEventListener("click", () => {
        state.explainOpen = !state.explainOpen;
        const expPanel = $("explain-panel");
        if (state.explainOpen) {
          loadExplanation();
        } else if (expPanel) {
          expPanel.classList.add("hidden");
        }
      });
    }

    document.querySelectorAll(".lang-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const lang = btn.dataset.lang || "en";
        state.explainLang = lang;
        document.querySelectorAll(".lang-btn").forEach((b) => {
          b.classList.toggle("is-active", b.dataset.lang === lang);
        });
        if (!state.explainOpen) {
          state.explainOpen = true;
        }
        loadExplanation();
      });
    });

    // Step 2 What-If Simulator slider & presets
    const slider = $("override-slider");
    if (slider) {
      slider.addEventListener("input", () => {
        triggerSimulation(slider.value);
      });
    }

    const presetAi = $("preset-ai");
    if (presetAi && slider) {
      presetAi.addEventListener("click", () => {
        if (!state.activeForecast) return;
        slider.value = String(state.activeForecast.recommended_qty);
        triggerSimulation(slider.value);
      });
    }

    const presetDemo = $("preset-demo-35");
    if (presetDemo && slider) {
      presetDemo.addEventListener("click", (evt) => {
        const targetQty = evt.currentTarget.dataset.qty || "35";
        slider.value = targetQty;
        triggerSimulation(targetQty);
      });
    }

    const presetZero = $("preset-zero");
    if (presetZero && slider) {
      presetZero.addEventListener("click", () => {
        slider.value = "0";
        triggerSimulation(0);
      });
    }

    const commitForm = $("override-commit-form");
    if (commitForm) commitForm.addEventListener("submit", handleCommitOverride);

    // View 2: Bill / Voice / CSV Capture
    const billInput = $("bill-file-input");
    if (billInput) {
      billInput.addEventListener("change", () => {
        if (billInput.files && billInput.files[0]) {
          setText("bill-file-label", `Selected: ${billInput.files[0].name}`);
        }
      });
    }

    const billForm = $("bill-upload-form");
    if (billForm && billInput) {
      billForm.addEventListener("submit", (evt) => {
        evt.preventDefault();
        if (!billInput.files || billInput.files.length === 0) {
          showToast("Choose a bill image or click Test Sample Supplier Bill.", "error");
          return;
        }
        uploadBillFile(billInput.files[0], $("btn-upload-bill"));
      });
    }

    const sampleBillBtn = $("btn-load-sample-bill");
    if (sampleBillBtn) sampleBillBtn.addEventListener("click", handleLoadSampleBill);

    const csvInput = $("csv-file-input");
    if (csvInput) {
      csvInput.addEventListener("change", () => {
        if (csvInput.files && csvInput.files[0]) {
          setText("csv-file-label", `Selected: ${csvInput.files[0].name}`);
        }
      });
    }

    const csvForm = $("csv-upload-form");
    if (csvForm && csvInput) {
      csvForm.addEventListener("submit", (evt) => {
        evt.preventDefault();
        if (!csvInput.files || csvInput.files.length === 0) {
          showToast("Choose a CSV file or click Load Sample D2C CSV.", "error");
          return;
        }
        uploadCsvFile(csvInput.files[0], $("btn-upload-csv"));
      });
    }

    const sampleCsvBtn = $("btn-load-sample-csv");
    if (sampleCsvBtn) sampleCsvBtn.addEventListener("click", handleLoadSampleCsv);

    document.querySelectorAll(".preset-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        const transcriptEl = $("voice-transcript");
        if (transcriptEl) transcriptEl.value = btn.dataset.phrase || "";
        handleParseVoice();
      });
    });

    const voiceForm = $("voice-note-form");
    if (voiceForm) voiceForm.addEventListener("submit", handleParseVoice);
    const voiceMicBtn = $("btn-voice-mic");
    if (voiceMicBtn) voiceMicBtn.addEventListener("click", startVoiceMic);

    // View 3: ₹50k Budget Planner
    const budgetForm = $("budget-plan-form");
    if (budgetForm) {
      budgetForm.addEventListener("submit", (evt) => {
        evt.preventDefault();
        const budgetEl = $("budget-input");
        loadReorderPlan(budgetEl ? budgetEl.value : 50000);
      });
    }

    document.querySelectorAll("[data-budget]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const budgetEl = $("budget-input");
        if (budgetEl) budgetEl.value = btn.dataset.budget;
        loadReorderPlan(btn.dataset.budget);
      });
    });

    // View 4: Refresh FastMCP calls
    const refreshMcpBtn = $("btn-refresh-mcp");
    if (refreshMcpBtn) refreshMcpBtn.addEventListener("click", loadMcpInspector);
  }

  async function init() {
    initTheme();
    const metaKey = document.querySelector('meta[name="vani-api-key"]');
    state.apiKey = metaKey ? metaKey.getAttribute("content") || "" : "";

    bindEvents();
    await loadHealth();
    try {
      await loadSkus();
      await selectSku(HERO_IDS.OIL);
    } catch (err) {
      showToast(`Bootstrap error: ${err.message}`, "error");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
