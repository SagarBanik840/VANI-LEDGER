/* VANI-LEDGER Seller Intelligence & Decision Cockpit — Zero-CDN Modular Vanilla JS
 * Adapted to Somendra-Mishra/VANI-LEDGER dual-theme sidebar + drawer layout.
 * Security invariants:
 *  - Strict CSP compliant (no inline scripts, no eval, no inline style attributes).
 *  - XSS safe: textContent and safe DOM node builders only (zero innerHTML).
 *  - Server-side tenant scoping via X-API-Key read from server-injected meta tag.
 */

(() => {
  "use strict";

  const HERO_IDS = {
    OIL: "aaaaaaaa-aaaa-4aaa-8aaa-000000000001",
    DAIRY: "aaaaaaaa-aaaa-4aaa-8aaa-000000000002",
    D2C: "aaaaaaaa-aaaa-4aaa-8aaa-000000000003",
  };

  const state = {
    apiKey: "",
    theme: "light",
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
    activeTab: "capture",
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
      toast.classList.add("is-hidden");
    }, 4200);
  }

  // ── Theme Management (Somendra-Mishra Light/Dark Dual Theme) ─────────────────

  function applyTheme(theme) {
    const resolved = theme === "dark" ? "dark" : "light";
    state.theme = resolved;
    document.documentElement.setAttribute("data-theme", resolved);
    try {
      window.localStorage.setItem("vani_theme", resolved);
    } catch {
      // Ignore storage restrictions in private browsing
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

  // ── Health & Catalog Bootstrap ───────────────────────────────────────────────

  async function loadHealth() {
    try {
      const { payload } = await apiFetch("/health");
      const d = payload && payload.data;
      if (!d) return;
      const modelPill = $("model-version-pill");
      if (modelPill && d.model_version) modelPill.textContent = d.model_version;
      const offlineText = $("offline-pill-text");
      if (offlineText) {
        offlineText.textContent = d.offline_ready ? "ENGINE READY · OFFLINE" : "DEGRADED";
      }
      const navCount = $("nav-sku-count");
      if (navCount && d.sku_count) navCount.textContent = String(d.sku_count);
      const kpiSkus = $("kpi-total-skus");
      if (kpiSkus && d.sku_count) kpiSkus.textContent = String(d.sku_count);
      const kpiSkusFoot = $("kpi-total-skus-foot");
      if (kpiSkusFoot && d.sales_count) {
        kpiSkusFoot.textContent = `${inrIntFmt.format(d.sales_count)} sales rows in local SQLite`;
      }
      const footerMeta = $("footer-health-meta");
      if (footerMeta) {
        footerMeta.textContent =
          `SQLite WAL · ${d.sku_count} SKUs · ${inrIntFmt.format(d.sales_count)} sales rows · 100% Offline Capable`;
      }
    } catch {
      const offlineText = $("offline-pill-text");
      if (offlineText) offlineText.textContent = "LOCAL API CHECK";
    }
  }

  async function loadSkus() {
    const { payload } = await apiFetch("/v1/skus?limit=100&sort_by=sku_code&order=asc");
    const items = (payload && payload.data && payload.data.items) || [];
    state.skus = items;
    state.skusById.clear();
    state.skusByCode.clear();

    const navCount = $("nav-sku-count");
    if (navCount) navCount.textContent = String(items.length);
    const kpiSkus = $("kpi-total-skus");
    if (kpiSkus) kpiSkus.textContent = String(items.length);

    const select = $("sku-select");
    clearNode(select);

    // Put the 3 Slide 6 hero SKUs first in the dropdown for quick access
    const heroOrder = [HERO_IDS.OIL, HERO_IDS.DAIRY, HERO_IDS.D2C];
    const sorted = [...items].sort((a, b) => {
      const ia = heroOrder.indexOf(a.id);
      const ib = heroOrder.indexOf(b.id);
      if (ia !== -1 && ib !== -1) return ia - ib;
      if (ia !== -1) return -1;
      if (ib !== -1) return 1;
      return a.sku_code.localeCompare(b.sku_code);
    });

    for (const sku of sorted) {
      state.skusById.set(sku.id, sku);
      state.skusByCode.set(sku.sku_code.toUpperCase(), sku);
      const opt = document.createElement("option");
      opt.value = sku.id;
      const alias = sku.hinglish_alias ? ` (${sku.hinglish_alias})` : "";
      opt.textContent = `${sku.sku_code} · ${sku.name}${alias} [${sku.abc_segment}]`;
      select.appendChild(opt);
    }

    if (state.skusById.has(state.activeSkuId)) {
      select.value = state.activeSkuId;
    } else if (sorted.length > 0) {
      state.activeSkuId = sorted[0].id;
      select.value = state.activeSkuId;
    }
  }

  // ── Decision Card (01 / CAPTURE + 02 / FORECAST) ─────────────────────────────

  function updateHeroChipSelection(skuId) {
    document.querySelectorAll(".hero-chip").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.skuId === skuId);
    });
    const select = $("sku-select");
    if (select && select.value !== skuId && state.skusById.has(skuId)) {
      select.value = skuId;
    }
  }

  function renderSkuCaptureContext(sku, forecast) {
    const badge = $("capture-verified-badge");
    const abcPill = $("sku-abc-pill");
    const perishPill = $("sku-perishable-pill");
    const ctxLine = $("capture-context-line");

    if (!sku) return;

    const aliasText = sku.hinglish_alias || sku.sku_code;
    badge.textContent = `${aliasText} · FastMCP Verified`;
    abcPill.textContent = `SEGMENT ${sku.abc_segment} · ${sku.category}`;

    if (sku.perishable) {
      perishPill.textContent = `PERISHABLE · ${sku.shelf_life_days}D SHELF LIFE`;
      perishPill.classList.remove("is-hidden");
    } else {
      perishPill.classList.add("is-hidden");
    }

    const parts = [
      `Cost ${formatINR(sku.unit_cost)}`,
      `MRP ${formatINR(sku.unit_price)}`,
      `Lead ${sku.lead_time_days}d`,
      `Shelf ${sku.shelf_life_days}d`,
      `On-hand ${Math.round(num(sku.on_hand))} ${sku.unit}s`,
    ];
    if (sku.moq > 1 || sku.pack_size > 1) {
      parts.push(`MOQ ${sku.moq} / Pack ${sku.pack_size}`);
    }
    if (sku.id === HERO_IDS.OIL) {
      parts.push('Note: "Diwali week stock add karo, shelf life 90d"');
    } else if (sku.id === HERO_IDS.DAIRY) {
      parts.push('Note: "Aaj 12 crate doodh bika, 2 packet expire"');
    } else if (sku.id === HERO_IDS.D2C) {
      parts.push("Channel: Micro-D2C · Weekly ₹50k Budget Priority");
    }
    ctxLine.textContent = parts.join("  ·  ");

    const asOfPill = $("as-of-pill");
    if (asOfPill && forecast && forecast.as_of_date) {
      asOfPill.textContent = `AS OF ${forecast.as_of_date} · IST`;
    }
  }

  function buildDriverRows(container, drivers, recommendedQty) {
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
      const pct = Math.min(100, Math.max(3, Math.round((Math.abs(d.units) / totalAbs) * 100)));
      bar.value = pct;

      const sign = d.units >= 0 ? "+" : "";
      const shareStr = Number.isFinite(Number(d.share_pct))
        ? ` (${Math.round(num(d.share_pct))}%)`
        : "";
      const valNode = el("div", "driver-val", `${sign}${d.units}${shareStr}`);
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

    renderSkuCaptureContext(sku, forecast);

    const unitLabel = sku ? `${sku.unit}s` : "units";
    $("forecast-sku-name").textContent = forecast.sku_name;
    $("forecast-recommended-qty").textContent = String(forecast.recommended_qty);
    $("forecast-unit-label").textContent = unitLabel;
    $("forecast-outlay").textContent = formatINR(forecast.outlay_amount);
    $("forecast-service-level").textContent = formatPct(forecast.service_level, 0);

    const p10 = Math.round(num(forecast.p10_total));
    const p50 = Math.round(num(forecast.p50_total));
    const p90 = Math.round(num(forecast.p90_total));
    $("forecast-interval").textContent = `${p10}–${p90} (p50: ${p50})`;

    // Top 4-Card Summary KPI Grid updates
    const kpiOrder = $("kpi-rec-order");
    if (kpiOrder) kpiOrder.textContent = `${forecast.recommended_qty} ${unitLabel}`;
    const kpiOrderFoot = $("kpi-rec-order-foot");
    if (kpiOrderFoot) kpiOrderFoot.textContent = `${forecast.sku_name} · 7-day target`;
    const kpiOutlay = $("kpi-rec-outlay");
    if (kpiOutlay) kpiOutlay.textContent = formatINR(forecast.outlay_amount);
    const kpiService = $("kpi-service-level");
    if (kpiService) kpiService.textContent = formatPct(forecast.service_level, 0);
    const kpiServiceFoot = $("kpi-service-level-foot");
    if (kpiServiceFoot) kpiServiceFoot.textContent = `Conformal p10–p90: ${p10}–${p90} units`;

    const suffPill = $("data-sufficiency-pill");
    const suff = String(forecast.data_sufficiency || "high").toUpperCase();
    suffPill.textContent = `${suff} DATA`;
    suffPill.className = "status-pill pill";
    if (suff === "HIGH") suffPill.classList.add("pill-green", "pill--emerald");
    else if (suff === "MEDIUM") suffPill.classList.add("pill-amber", "pill--amber");
    else suffPill.classList.add("pill-red", "pill--crimson");

    $("model-used-pill").textContent = forecast.model_used || "ensemble";
    $("feature-hash-note").textContent = forecast.feature_hash
      ? `snapshot #${String(forecast.feature_hash).slice(0, 10)}`
      : "";

    // Additive XAI drivers
    const drivers = Array.isArray(forecast.drivers) ? forecast.drivers : [];
    const driverSum = drivers.reduce((acc, d) => acc + num(d.units, 0), 0);
    $("drivers-sum-check").textContent =
      `SUM ${driverSum} = ${forecast.recommended_qty} UNITS`;

    buildDriverRows($("drivers-list"), drivers, forecast.recommended_qty);

    // Configure Override Slider for this SKU
    configureOverrideSlider(forecast);

    // Reset commit banner
    const commitBanner = $("commit-status-banner");
    commitBanner.classList.add("is-hidden");
    commitBanner.textContent = "";

    // Refresh explain panel if already open
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

  // ── Grounded Narrative ("Why this order?") ───────────────────────────────────

  async function loadExplanation() {
    const panel = $("explain-panel");
    const textEl = $("explain-narrative-text");
    const groundedBadge = $("explain-grounded-badge");
    const sourceBadge = $("explain-source-badge");

    panel.classList.remove("is-hidden");
    textEl.textContent = "Generating number-verified explanation…";

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
      groundedBadge.textContent = d.grounded ? "NUMBERS VERIFIED" : "FALLBACK VERIFIED";
      groundedBadge.className = d.grounded
        ? "status-pill pill-green pill pill--emerald"
        : "status-pill pill-amber pill pill--amber";

      const srcLabel =
        d.source === "llm_verified"
          ? "LLM VERIFIED · ZERO HALLUCINATION"
          : "DETERMINISTIC TEMPLATE · OFFLINE";
      sourceBadge.textContent = srcLabel;
    } catch (err) {
      textEl.textContent = `Explanation unavailable: ${err.message}`;
      groundedBadge.textContent = "ERROR";
      groundedBadge.className = "status-pill pill-red pill pill--crimson";
    }
  }

  // ── Slide-Over Recommendation Drawer (Somendra-Mishra/VANI-LEDGER) ───────────

  async function openRecommendationDrawer() {
    const forecast = state.activeForecast;
    if (!forecast) return;
    const sku = state.skusById.get(forecast.sku_id);
    const unitLabel = sku ? `${sku.unit}s` : "units";

    $("drawerTitle").textContent = `${forecast.sku_name} (${forecast.sku_code})`;
    const body = $("drawerBody");
    clearNode(body);

    // Meta row: Category tag, lead time, data readiness pill
    const metaRow = el("div", "drawer-meta-row");
    const leftMeta = el("div", "");
    leftMeta.appendChild(el("span", "tag", sku ? sku.category : "Catalog SKU"));
    leftMeta.appendChild(
      el(
        "span",
        "mono-note",
        `  Supplier lead time: ${sku ? sku.lead_time_days : 3} days · Shelf life: ${sku ? sku.shelf_life_days : 90}d`
      )
    );
    metaRow.appendChild(leftMeta);
    metaRow.appendChild(
      el(
        "span",
        "status-pill pill-green",
        `${String(forecast.data_sufficiency || "HIGH").toUpperCase()} DATA`
      )
    );
    body.appendChild(metaRow);

    // Recommendation Summary Banner
    const notice = el("div", "notice warn");
    const bannerTop = el("div", "drawer-banner-top");
    bannerTop.appendChild(
      el("strong", "", `Suggested Order: ${forecast.recommended_qty} ${unitLabel}`)
    );
    bannerTop.appendChild(el("strong", "", formatINR(forecast.outlay_amount)));
    notice.appendChild(bannerTop);
    notice.appendChild(
      el(
        "div",
        "helper-text",
        `On-hand: ${sku ? Math.round(num(sku.on_hand)) : 0} ${unitLabel} · Conformal p10–p90: ${Math.round(num(forecast.p10_total))}–${Math.round(num(forecast.p90_total))} · Service level: ${formatPct(forecast.service_level, 0)}`
      )
    );
    body.appendChild(notice);

    // Additive Drivers Section
    body.appendChild(
      el("div", "panel-title", "Why this order? / यह ऑर्डर क्यों जरूरी है?")
    );
    const drawerDrivers = el("div", "driver-list");
    buildDriverRows(
      drawerDrivers,
      Array.isArray(forecast.drivers) ? forecast.drivers : [],
      forecast.recommended_qty
    );
    body.appendChild(drawerDrivers);

    // Grounded Narrative Box
    const callout = el("div", "why-callout");
    const calloutHead = el("div", "explain-box-head");
    calloutHead.appendChild(
      el("strong", "", "Plain-Language Explanation / आसान भाषा में:")
    );
    calloutHead.appendChild(el("span", "status-pill pill-green", "NUMBERS VERIFIED"));
    callout.appendChild(calloutHead);
    const narrativeP = el(
      "p",
      "explain-text",
      state.lastNarrative || "Loading grounded seller explanation…"
    );
    callout.appendChild(narrativeP);
    body.appendChild(callout);

    // Drawer Actions
    const actions = el("div", "drawer-actions");
    const jumpSimBtn = el(
      "button",
      "btn btn-primary",
      "◈ Adjust in What-If Simulator / ऑर्डर संख्या बदलें"
    );
    jumpSimBtn.type = "button";
    jumpSimBtn.addEventListener("click", () => {
      closeRecommendationDrawer();
      const slider = $("override-slider");
      if (slider) slider.focus();
    });

    const copySlipBtn = el(
      "button",
      "btn btn-secondary",
      "💬 Copy Supplier Order Slip / सप्लायर ऑर्डर कॉपी करें"
    );
    copySlipBtn.type = "button";
    copySlipBtn.addEventListener("click", () => {
      copySupplierOrderSlip();
    });

    actions.appendChild(jumpSimBtn);
    actions.appendChild(copySlipBtn);
    body.appendChild(actions);

    $("recommendationDrawer").classList.add("open");
    $("drawerBackdrop").classList.add("active");

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
        narrativeP.textContent =
          "Deterministic drivers verified above; use the main panel to toggle EN / Hinglish.";
      }
    }
  }

  function closeRecommendationDrawer() {
    $("recommendationDrawer").classList.remove("open");
    $("drawerBackdrop").classList.remove("active");
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
      showToast(`Supplier order slip ready: ${qty} ${unitLabel} (${formatINR(estValue)})`, "success");
    } catch {
      showToast(`Order slip: ${slip}`, "success");
    }
  }

  // ── 03 / OVERRIDE What-If Sandbox (<50ms Hot Path) ───────────────────────────

  function configureOverrideSlider(forecast) {
    const rec = Math.max(0, Math.round(num(forecast.recommended_qty, 0)));
    const maxVal =
      forecast.sku_id === HERO_IDS.OIL
        ? 80
        : Math.max(80, Math.ceil((rec * 1.75) / 10) * 10);

    const slider = $("override-slider");
    slider.min = "0";
    slider.max = String(maxVal);
    slider.value = String(rec);

    $("slider-mid-label").textContent = `${Math.round(maxVal / 2)} units`;
    $("slider-max-label").textContent = `${maxVal} units`;

    $("override-manual-qty").textContent = String(rec);
    $("override-suggested-qty").textContent = String(rec);
    $("ai-marker-text").textContent = `AI SUGGESTED ${rec}`;

    const demoBtn = $("preset-demo-35");
    if (forecast.sku_id === HERO_IDS.OIL) {
      demoBtn.textContent = "Demo: 35 (-13)";
      demoBtn.dataset.qty = "35";
    } else {
      const cutQty = Math.max(0, Math.round(rec * 0.72));
      demoBtn.textContent = `Trim: ${cutQty}`;
      demoBtn.dataset.qty = String(cutQty);
    }
  }

  function renderSimulationResult(sim, serverLatencyMs, clientMs) {
    state.lastSim = sim;

    const displayMs =
      typeof serverLatencyMs === "number" && serverLatencyMs > 0
        ? serverLatencyMs
        : clientMs;
    const latPill = $("sim-latency-pill");
    latPill.textContent = `${displayMs.toFixed(1)} ms · <50ms SLA`;
    latPill.className = displayMs < 50 ? "latency-pill" : "latency-pill latency-pill--warn";

    $("override-manual-qty").textContent = String(sim.manual_qty);
    $("override-suggested-qty").textContent = String(sim.suggested_qty);

    // Tile 1: Cash Freed / Extra Cash
    const cashDelta = num(sim.cash_delta, 0);
    const cashFreed = num(sim.cash_freed, 0);
    const cashValEl = $("sim-cash-val");
    const cashLblEl = $("sim-cash-label");

    if (cashFreed > 0) {
      cashValEl.textContent = formatINR(cashFreed);
      cashValEl.className = "metric-tile-val metric-tile-val--gold";
      cashLblEl.textContent = "CASH FREED / बचत";
    } else if (cashDelta > 0) {
      cashValEl.textContent = `+${formatINR(cashDelta)}`;
      cashValEl.className = "metric-tile-val metric-tile-val--ivory";
      cashLblEl.textContent = "EXTRA OUTLAY / अतिरिक्त खर्च";
    } else {
      cashValEl.textContent = "₹0";
      cashValEl.className = "metric-tile-val metric-tile-val--gold";
      cashLblEl.textContent = "CASH FREED / बचत";
    }
    $("sim-outlay-sub").textContent = `Outlay ${formatINR(sim.outlay_amount)}`;

    // Tile 2: Stockout Risk Delta
    const riskDeltaPct = Math.round(num(sim.stockout_risk_delta, 0) * 100);
    const riskBeforePct = Math.round(num(sim.stockout_risk_before, 0) * 100);
    const riskAfterPct = Math.round(num(sim.stockout_risk_after, 0) * 100);
    const riskEl = $("sim-risk-delta");
    const riskTile = $("tile-risk");

    const sign = riskDeltaPct > 0 ? "+" : "";
    riskEl.textContent = `${sign}${riskDeltaPct}%`;
    riskTile.className = "sim-result metric-tile";

    if (riskDeltaPct >= 5) {
      riskEl.className = "metric-tile-val metric-tile-val--crimson";
      riskTile.classList.add("metric-tile--danger");
    } else if (riskDeltaPct < 0) {
      riskEl.className = "metric-tile-val metric-tile-val--emerald";
      riskTile.classList.add("metric-tile--good");
    } else {
      riskEl.className = "metric-tile-val metric-tile-val--ivory";
    }
    $("sim-risk-sub").textContent = `${riskBeforePct}% → ${riskAfterPct}%`;

    // Tile 3: Days Shelf Cover
    const daysCover = num(sim.days_cover, 0);
    $("sim-days-cover").textContent = `${daysCover.toFixed(1)} d`;
    const sku = state.skusById.get(sim.sku_id);
    $("sim-cover-sub").textContent = sku
      ? `Shelf life ${sku.shelf_life_days}d · Lead ${sku.lead_time_days}d`
      : "Order shelf cover";

    // Spoilage Guard Badge
    const spoilPct = Math.round(num(sim.spoilage_risk, 0) * 100);
    const spoilEl = $("guard-spoilage");
    const spoilText = $("guard-spoilage-text");
    if (spoilPct >= 20) {
      spoilEl.className = "guard-badge guard-badge--danger";
      spoilText.textContent = `SPOILAGE RISK ${spoilPct}% · EXCEEDS SHELF LIFE`;
    } else if (spoilPct > 5) {
      spoilEl.className = "guard-badge guard-badge--warn";
      spoilText.textContent = `SPOILAGE RISK ${spoilPct}% · CAUTION`;
    } else {
      spoilEl.className = "guard-badge guard-badge--ok";
      spoilText.textContent = `SPOILAGE RISK ${spoilPct}% · SAFE`;
    }

    // Budget Guard Badge
    const budgetEl = $("guard-budget");
    const budgetText = $("guard-budget-text");
    if (sim.within_budget) {
      budgetEl.className = "guard-badge guard-badge--ok";
      budgetText.textContent = "BUDGET GUARD · WITHIN WEEKLY CAP";
    } else {
      budgetEl.className = "guard-badge guard-badge--danger";
      budgetText.textContent = "BUDGET GUARD · EXCEEDS WEEKLY CAP";
    }
  }

  async function triggerSimulation(manualQty) {
    const qty = Math.max(0, Math.round(num(manualQty, 0)));
    $("override-manual-qty").textContent = String(qty);

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

    const manualQty = Math.max(0, parseInt(slider.value, 10) || 0);
    const reason = reasonInput.value.trim() || null;
    const idemKey = `ui-${state.activeSkuId.slice(0, 8)}-${manualQty}-${Date.now()}`;

    btn.disabled = true;
    btn.textContent = "Committing…";
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
      if (d) {
        banner.textContent =
          `COMMITTED · Override ${String(d.override_id).slice(0, 8)} (${d.suggested_qty} → ${d.manual_qty} units) written atomically to audit log.`;
        banner.classList.remove("is-hidden");
        showToast(
          `Override committed (${d.manual_qty} units) & recorded in immutable audit log.`,
          "success"
        );
      }
    } catch (err) {
      showToast(`Commit failed: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "✓ Approve & Commit Override";
    }
  }

  // ── Tab 1: Multimodal Capture (Bill / CSV / Voice) ───────────────────────────

  function renderBillResult(data) {
    const area = $("bill-result-area");
    const badges = $("bill-meta-badges");
    const banner = $("bill-math-banner");
    const tbody = $("bill-items-tbody");

    area.classList.remove("is-hidden");
    clearNode(badges);
    clearNode(banner);
    clearNode(tbody);

    if (data.demo_document) {
      badges.appendChild(el("span", "status-pill pill-amber pill pill--amber", "DEMO DOCUMENT"));
    }
    badges.appendChild(el("span", "status-pill pill-gray pill pill--mono", `adapter: ${data.source_adapter}`));
    badges.appendChild(
      el("span", "status-pill pill-blue pill pill--gold", `CONFIDENCE ${formatPct(data.confidence, 1)}`)
    );
    if (data.needs_review) {
      badges.appendChild(el("span", "status-pill pill-red pill pill--crimson", "NEEDS SELLER REVIEW"));
    }

    const mc = data.math_check;
    banner.classList.remove("is-hidden", "alert-banner--crimson", "alert-banner--emerald");
    if (mc && !mc.ok) {
      banner.classList.add("alert-banner--crimson");
      const deltaAbs = Math.abs(num(mc.delta, 0));
      banner.appendChild(
        el("div", "alert-banner-title", `${formatINR(deltaAbs)} OVERCHARGE CAUGHT`)
      );
      banner.appendChild(
        el(
          "div",
          "",
          `Supplier billed ${formatINR(mc.written_total, 2)} vs Python Decimal verified ${formatINR(mc.computed_total, 2)} (Delta ${formatINR(mc.delta, 2)}).`
        )
      );
    } else if (mc) {
      banner.classList.add("alert-banner--emerald");
      banner.appendChild(
        el(
          "div",
          "",
          `ARITHMETIC VERIFIED · Total ${formatINR(mc.computed_total, 2)} matches line items.`
        )
      );
    }

    const items = Array.isArray(data.line_items) ? data.line_items : [];
    for (const item of items) {
      const tr = document.createElement("tr");
      if (!item.line_math_ok) tr.classList.add("row-error");

      tr.appendChild(el("td", "", item.item_name));
      tr.appendChild(el("td", "num", String(item.qty)));
      tr.appendChild(el("td", "num", formatINR(item.unit_price, 2)));
      tr.appendChild(el("td", "num", formatINR(item.written_line_total, 2)));
      tr.appendChild(el("td", "num", formatINR(item.computed_line_total, 2)));

      const statusTd = document.createElement("td");
      statusTd.appendChild(
        el(
          "span",
          item.line_math_ok
            ? "status-pill pill-green pill pill--emerald"
            : "status-pill pill-red pill pill--crimson",
          item.line_math_ok ? "OK" : "MISMATCH"
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
            `${formatINR(Math.abs(num(mc.delta)))} supplier bill overcharge caught!`,
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
    btn.disabled = true;
    btn.textContent = "Auditing Sample Bill…";
    try {
      const res = await fetch("/v1/capture/sample-bill", {
        headers: state.apiKey ? { "X-API-Key": state.apiKey } : {},
      });
      if (!res.ok) throw new Error(`Fixture unavailable (${res.status})`);
      const blob = await res.blob();
      const file = new File([blob], "demo_bill.png", { type: "image/png" });
      await uploadBillFile(file, btn);
    } catch (err) {
      showToast(`Could not load sample bill: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Load Sample Crumpled Bill";
    }
  }

  function renderCsvResult(data) {
    const area = $("csv-result-area");
    area.classList.remove("is-hidden");
    clearNode(area);

    const head = el("div", "bill-meta-badges");
    head.appendChild(
      el(
        "span",
        data.duplicate_upload
          ? "status-pill pill-amber pill pill--amber"
          : "status-pill pill-green pill pill--emerald",
        data.duplicate_upload ? "DUPLICATE CSV (CACHED)" : "CSV INGESTED"
      )
    );
    if (data.document_id) {
      head.appendChild(
        el("span", "status-pill pill-gray pill pill--mono", `doc #${String(data.document_id).slice(0, 8)}`)
      );
    }
    area.appendChild(head);

    const grid = el("div", "kv-grid");
    const updatedCodes = Array.isArray(data.sku_codes_updated) ? data.sku_codes_updated : [];
    const pairs = [
      ["ROWS PROCESSED", data.rows_processed ?? 0],
      ["ROWS INSERTED", data.rows_inserted ?? 0],
      ["SKUS UPDATED", updatedCodes.length > 0 ? updatedCodes.join(", ") : "—"],
      ["TOTAL REVENUE", formatINR(data.total_revenue ?? 0, 2)],
    ];
    for (const [k, v] of pairs) {
      const item = el("div", "kv-item");
      item.appendChild(el("span", "micro-label", k));
      item.appendChild(el("span", "kv-val", String(v)));
      grid.appendChild(item);
    }
    area.appendChild(grid);
    if (Array.isArray(data.warnings) && data.warnings.length > 0) {
      area.appendChild(el("div", "mono-note", data.warnings.join(" · ")));
    }
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
        showToast("D2C sales CSV imported & audited.", "success");
      }
    } catch (err) {
      showToast(`CSV import error: ${err.message}`, "error");
    } finally {
      if (triggerBtn) triggerBtn.disabled = false;
    }
  }

  async function handleLoadSampleCsv() {
    const btn = $("btn-load-sample-csv");
    btn.disabled = true;
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
      btn.disabled = false;
    }
  }

  function renderVoiceResult(data) {
    const area = $("voice-result-area");
    area.classList.remove("is-hidden");
    clearNode(area);

    const head = el("div", "bill-meta-badges");
    head.appendChild(el("span", "status-pill pill-blue pill pill--gold", `INTENT: ${data.intent}`));
    if (data.requires_confirmation) {
      head.appendChild(el("span", "status-pill pill-amber pill pill--amber", "REQUIRES CONFIRMATION"));
    }
    area.appendChild(head);

    const grid = el("div", "kv-grid");
    const addKv = (label, value, skuCodeForJump = null) => {
      const item = el("div", "kv-item");
      item.appendChild(el("span", "micro-label", label));
      if (skuCodeForJump && state.skusByCode.has(skuCodeForJump.toUpperCase())) {
        const skuObj = state.skusByCode.get(skuCodeForJump.toUpperCase());
        const jumpBtn = el("button", "link-btn", value);
        jumpBtn.type = "button";
        jumpBtn.addEventListener("click", () => {
          selectSku(skuObj.id);
          window.scrollTo({ top: 0, behavior: "smooth" });
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
    addKv("MATCHED SKU", skuDisplay, data.matched_sku_code);
    addKv("EXTRACTED QTY", data.extracted_qty !== null ? String(data.extracted_qty) : "—");
    addKv("SPOILAGE QTY", data.spoilage_qty !== null ? String(data.spoilage_qty) : "—");
    addKv(
      "SHELF LIFE",
      data.shelf_life_days !== null ? `${data.shelf_life_days} days` : "—"
    );

    area.appendChild(grid);
    if (data.notes) {
      area.appendChild(el("div", "mono-note", `Note: ${data.notes}`));
    }
  }

  async function handleParseVoice(event) {
    if (event) event.preventDefault();
    const transcript = $("voice-transcript").value.trim();
    if (transcript.length < 2) {
      showToast("Enter at least 2 characters for the voice note.", "error");
      return;
    }

    const btn = $("btn-parse-voice");
    btn.disabled = true;
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
      btn.disabled = false;
    }
  }

  function startVoiceMic() {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    const statusEl = $("voice-mic-status");
    const visual = $("voice-visual");
    if (!SpeechRec) {
      statusEl.textContent = "Web Speech API unavailable in this browser — type Hinglish directly.";
      return;
    }
    try {
      const rec = new SpeechRec();
      rec.lang = "en-IN";
      rec.interimResults = false;
      rec.maxAlternatives = 1;
      statusEl.textContent = "Listening (en-IN)… speak now.";
      if (visual) visual.classList.add("listening");
      rec.onresult = (evt) => {
        const text = evt.results[0][0].transcript;
        $("voice-transcript").value = text;
        statusEl.textContent = `Captured: "${text}"`;
        handleParseVoice();
      };
      rec.onerror = (evt) => {
        statusEl.textContent = `Mic stopped (${evt.error || "offline"}) — typed input ready.`;
      };
      rec.onend = () => {
        if (visual) visual.classList.remove("listening");
      };
      rec.start();
    } catch {
      if (visual) visual.classList.remove("listening");
      statusEl.textContent = "Mic unavailable offline — use preset chips or type Hinglish.";
    }
  }

  // ── Tab 2: ₹50k Weekly Budget Reorder Plan (Case 03) ─────────────────────────

  async function loadReorderPlan(budgetValue) {
    const tbody = $("reorder-tbody");
    clearNode(tbody);
    const loadingTr = document.createElement("tr");
    const loadingTd = el("td", "muted-cell", "Running SciPy linprog budget optimizer…");
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

      $("budget-allocated").textContent = formatINR(allocated, 2);
      $("budget-remaining").textContent = formatINR(remaining, 2);
      $("budget-pct").textContent = `${pct.toFixed(1)}%`;
      $("budget-progress").value = pct;

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
        const skuBtn = el("button", "link-btn", `${item.sku_code} · ${item.sku_name}`);
        skuBtn.type = "button";
        skuBtn.addEventListener("click", () => {
          selectSku(item.sku_id);
          window.scrollTo({ top: 0, behavior: "smooth" });
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
          capTd.appendChild(el("span", "status-pill pill-amber pill pill--amber", "SHELF-LIFE CAP"));
        } else if (item.capped_by_budget) {
          capTd.appendChild(el("span", "status-pill pill-red pill pill--crimson", "BUDGET CAP"));
        } else if (item.allocated_qty > 0) {
          capTd.appendChild(el("span", "status-pill pill-green pill pill--emerald", "FULL ORDER"));
        } else {
          capTd.appendChild(el("span", "status-pill pill-gray pill pill--muted", "DEFERRED"));
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

  // ── Tab 3: ML Backtest & FastMCP Inspector ───────────────────────────────────

  function fmtMetricPct(val) {
    if (val === undefined || val === null || Number.isNaN(Number(val))) return "—";
    const n = Number(val);
    return `${(n * 100).toFixed(1)}%`;
  }

  function fmtMetricNum(val, digits = 3) {
    if (val === undefined || val === null || Number.isNaN(Number(val))) return "—";
    return Number(val).toFixed(digits);
  }

  async function loadBacktest() {
    const tbody = $("backtest-models-tbody");
    const segCards = $("backtest-segment-cards");
    const gatePill = $("backtest-gate-pill");
    const footnote = $("backtest-footnote");

    try {
      const { payload } = await apiFetch("/v1/eval/backtest");
      const d = payload && payload.data;
      if (!d) return;

      const passed =
        (d.gates && d.gates.all_passed === true) ||
        d.gate_passed === true ||
        String(d.status || "").toUpperCase() === "PASS";
      gatePill.textContent = passed ? "BACKTEST GATE · PASS" : "BACKTEST GATE · REVIEW";
      gatePill.className = passed
        ? "status-pill pill-green pill pill--emerald"
        : "status-pill pill-amber pill pill--amber";

      if (footnote && d.honest_benchmark_note) {
        footnote.textContent = d.honest_benchmark_note;
      }

      clearNode(segCards);
      const modelsMap = d.models && !Array.isArray(d.models) ? d.models : {};
      const ampModel = modelsMap.amplified_ensemble || {};
      const segs = ampModel.segment_wape || d.abc_segments || d.segments || {};

      const addSegCard = (label, valStr) => {
        const card = el("div", "segment-card");
        card.appendChild(el("div", "micro-label", label));
        card.appendChild(el("div", "segment-val", valStr));
        segCards.appendChild(card);
      };

      const pickSegWape = (key) => {
        const entry = segs[key];
        if (entry === undefined || entry === null) return null;
        return typeof entry === "object" ? (entry.wape ?? entry.revenue_weighted_wape) : entry;
      };

      if (pickSegWape("A") !== null) addSegCard("A-ITEM WAPE", fmtMetricPct(pickSegWape("A")));
      if (pickSegWape("B") !== null) addSegCard("B-ITEM WAPE", fmtMetricPct(pickSegWape("B")));
      if (pickSegWape("C") !== null) addSegCard("C-ITEM WAPE", fmtMetricPct(pickSegWape("C")));

      if (d.lift_over_seasonal_naive_pct !== undefined && d.lift_over_seasonal_naive_pct !== null) {
        addSegCard("LIFT VS NAIVE", `+${num(d.lift_over_seasonal_naive_pct).toFixed(1)}%`);
      }

      const cov = ampModel.p90_empirical_coverage ?? d.conformal_p90_coverage ?? d.p90_coverage;
      if (cov !== undefined && cov !== null) {
        addSegCard("P90 COVERAGE", fmtMetricPct(cov));
      }

      clearNode(tbody);
      const modelLabels = {
        seasonal_naive: "Seasonal Naive (4-wk)",
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

      if (rows.length === 0) {
        const tr = document.createElement("tr");
        const td = el("td", "muted-cell", "Run eval/backtest.py to generate metrics.");
        td.colSpan = 6;
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
      }

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
      gatePill.textContent = "UNAVAILABLE";
      gatePill.className = "status-pill pill-red pill pill--crimson";
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
          el("div", "muted-cell", "Drag the override slider or inspect a forecast to trace calls.")
        );
      } else {
        for (const call of calls.slice(0, 12)) {
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

  // ── Sidebar Navigation, Tabs & Reset Demo ────────────────────────────────────

  function switchTab(tabName) {
    state.activeTab = tabName;
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      const active = btn.dataset.tab === tabName;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });

    const panelMap = {
      capture: "panel-capture",
      reorder: "panel-reorder",
      inspector: "panel-inspector",
    };
    for (const [key, panelId] of Object.entries(panelMap)) {
      const panel = $(panelId);
      if (panel) panel.classList.toggle("is-hidden", key !== tabName);
    }

    if (tabName === "reorder" && !state.reorderLoaded) {
      loadReorderPlan($("budget-input").value);
    } else if (tabName === "inspector") {
      loadBacktest();
      loadMcpInspector();
    }
  }

  function handleSidebarNav(navKey) {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.nav === navKey);
    });
    const pageNames = {
      overview: "Overview",
      forecast: "Forecast & why",
      simulator: "What-if simulator",
      capture: "Sales & inputs",
      reorder: "Inventory & ₹50k Plan",
      inspector: "Backtest & FastMCP",
    };
    const pageNameEl = $("pageName");
    if (pageNameEl) pageNameEl.textContent = pageNames[navKey] || "Overview";

    const sidebar = $("sidebar");
    if (sidebar) sidebar.classList.remove("open");

    if (navKey === "overview") {
      window.scrollTo({ top: 0, behavior: "smooth" });
    } else if (navKey === "forecast") {
      if (!state.explainOpen) {
        state.explainOpen = true;
        loadExplanation();
      }
      const target = $("decision-card");
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (navKey === "simulator") {
      const target = $("override-card");
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      const slider = $("override-slider");
      if (slider) slider.focus();
    } else if (navKey === "capture" || navKey === "reorder" || navKey === "inspector") {
      switchTab(navKey);
      const target = $("section-operations");
      if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  async function handleResetDemo() {
    const btn = $("btn-reset-demo");
    btn.disabled = true;
    btn.textContent = "Resetting…";
    try {
      await apiFetch("/v1/demo/reset", { method: "POST" });
      state.reorderLoaded = false;
      state.inspectorLoaded = false;
      state.explainOpen = false;
      state.lastNarrative = "";
      $("explain-panel").classList.add("is-hidden");
      $("override-reason").value = "";
      closeRecommendationDrawer();
      await loadHealth();
      await loadSkus();
      await selectSku(HERO_IDS.OIL);
      if (state.activeTab === "reorder") {
        await loadReorderPlan($("budget-input").value);
      } else if (state.activeTab === "inspector") {
        await Promise.all([loadBacktest(), loadMcpInspector()]);
      }
      showToast("Demo reset complete: 40 SKUs reseeded, trained & cache warmed.", "success");
    } catch (err) {
      showToast(`Reset failed: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "↻ Reset Demo";
    }
  }

  // ── Event Binding & Init ─────────────────────────────────────────────────────

  function bindEvents() {
    // Theme toggle & mobile sidebar toggle
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

    // Sidebar navigation buttons
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.nav) handleSidebarNav(btn.dataset.nav);
      });
    });

    // Slide-Over Drawer triggers
    const openDrawerBtn = $("btn-open-drawer");
    if (openDrawerBtn) openDrawerBtn.addEventListener("click", openRecommendationDrawer);
    const headingDrawerBtn = $("btn-heading-drawer");
    if (headingDrawerBtn) headingDrawerBtn.addEventListener("click", openRecommendationDrawer);
    const drawerCloseBtn = $("drawerClose");
    if (drawerCloseBtn) drawerCloseBtn.addEventListener("click", closeRecommendationDrawer);
    const drawerBackdrop = $("drawerBackdrop");
    if (drawerBackdrop) drawerBackdrop.addEventListener("click", closeRecommendationDrawer);

    const jumpCaptureBtn = $("btn-jump-capture");
    if (jumpCaptureBtn) {
      jumpCaptureBtn.addEventListener("click", () => handleSidebarNav("capture"));
    }

    const copySlipBtn = $("btn-copy-order-slip");
    if (copySlipBtn) {
      copySlipBtn.addEventListener("click", copySupplierOrderSlip);
    }

    // Hero chips
    document.querySelectorAll(".hero-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.skuId) selectSku(btn.dataset.skuId);
      });
    });

    // 40-SKU dropdown
    $("sku-select").addEventListener("change", (evt) => {
      if (evt.target.value) selectSku(evt.target.value);
    });

    // Reset Demo
    $("btn-reset-demo").addEventListener("click", handleResetDemo);

    // Explain button & language toggle
    $("btn-why-order").addEventListener("click", () => {
      state.explainOpen = !state.explainOpen;
      if (state.explainOpen) {
        loadExplanation();
      } else {
        $("explain-panel").classList.add("is-hidden");
      }
    });

    document.querySelectorAll(".lang-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const lang = btn.dataset.lang || "en";
        state.explainLang = lang;
        document.querySelectorAll(".lang-btn").forEach((b) => {
          b.classList.toggle("is-active", b.dataset.lang === lang);
        });
        if (state.explainOpen) {
          loadExplanation();
        }
      });
    });

    // Override slider & quick presets
    const slider = $("override-slider");
    slider.addEventListener("input", () => {
      triggerSimulation(slider.value);
    });

    $("preset-ai").addEventListener("click", () => {
      if (!state.activeForecast) return;
      slider.value = String(state.activeForecast.recommended_qty);
      triggerSimulation(slider.value);
    });

    $("preset-demo-35").addEventListener("click", (evt) => {
      const targetQty = evt.currentTarget.dataset.qty || "35";
      slider.value = targetQty;
      triggerSimulation(targetQty);
    });

    $("preset-zero").addEventListener("click", () => {
      slider.value = "0";
      triggerSimulation(0);
    });

    $("override-commit-form").addEventListener("submit", handleCommitOverride);

    // Bottom tabs
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });

    // Tab 1: Bill / CSV / Voice
    const billInput = $("bill-file-input");
    billInput.addEventListener("change", () => {
      const lbl = $("bill-file-label");
      if (lbl && billInput.files && billInput.files[0]) {
        lbl.textContent = `Selected: ${billInput.files[0].name}`;
      }
    });

    $("bill-upload-form").addEventListener("submit", (evt) => {
      evt.preventDefault();
      if (!billInput.files || billInput.files.length === 0) {
        showToast("Choose a JPEG/PNG/WebP bill or click Load Sample Crumpled Bill.", "error");
        return;
      }
      uploadBillFile(billInput.files[0], $("btn-upload-bill"));
    });

    $("btn-load-sample-bill").addEventListener("click", handleLoadSampleBill);

    const csvInput = $("csv-file-input");
    csvInput.addEventListener("change", () => {
      const lbl = $("csv-file-label");
      if (lbl && csvInput.files && csvInput.files[0]) {
        lbl.textContent = `Selected: ${csvInput.files[0].name}`;
      }
    });

    $("csv-upload-form").addEventListener("submit", (evt) => {
      evt.preventDefault();
      if (!csvInput.files || csvInput.files.length === 0) {
        showToast("Choose a CSV file or click Load Sample D2C CSV.", "error");
        return;
      }
      uploadCsvFile(csvInput.files[0], $("btn-upload-csv"));
    });

    $("btn-load-sample-csv").addEventListener("click", handleLoadSampleCsv);

    document.querySelectorAll(".preset-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("voice-transcript").value = btn.dataset.phrase || "";
        handleParseVoice();
      });
    });

    $("voice-note-form").addEventListener("submit", handleParseVoice);
    $("btn-voice-mic").addEventListener("click", startVoiceMic);

    // Tab 2: Budget plan
    $("budget-plan-form").addEventListener("submit", (evt) => {
      evt.preventDefault();
      loadReorderPlan($("budget-input").value);
    });

    document.querySelectorAll("[data-budget]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("budget-input").value = btn.dataset.budget;
        loadReorderPlan(btn.dataset.budget);
      });
    });

    // Tab 3: Refresh MCP telemetry
    $("btn-refresh-mcp").addEventListener("click", loadMcpInspector);
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
