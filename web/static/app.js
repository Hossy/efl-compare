"use strict";

// ── State ────────────────────────────────────────────────────────────────
const state = {
  data: null,          // full payload from /api/plans
  plans: [],           // convenience alias
  compareTier: null,   // number
  sort: { key: "rate", dir: 1 },   // dir 1 = asc
  search: "",
  hideCredit: false,
  onlyFav: false,
  favs: loadFavs(),
  collapsed: loadCollapsed(),
};

// ── Persistence helpers ──────────────────────────────────────────────────
function loadFavs() {
  try { return new Set(JSON.parse(localStorage.getItem("efl_favs") || "[]")); }
  catch { return new Set(); }
}
function saveFavs() {
  localStorage.setItem("efl_favs", JSON.stringify([...state.favs]));
}
function loadCollapsed() {
  try { return new Set(JSON.parse(localStorage.getItem("efl_collapsed") || "[]")); }
  catch { return new Set(); }
}
function saveCollapsed() {
  localStorage.setItem("efl_collapsed", JSON.stringify([...state.collapsed]));
}

// ── Theme ────────────────────────────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem("efl_theme");
  const theme = saved || "dark";
  document.documentElement.setAttribute("data-theme", theme);
  updateThemeBtn(theme);
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("efl_theme", next);
    updateThemeBtn(next);
  });
}
function updateThemeBtn(theme) {
  document.getElementById("theme-toggle").textContent =
    theme === "dark" ? "☀ Light" : "☾ Dark";
}

// ── Formatting ───────────────────────────────────────────────────────────
const fmtCents = (c) => (c == null ? "—" : c.toFixed(2) + "¢");
const fmtMoney = (d) => "$" + d.toFixed(2);

function rateAt(plan, tier) {
  const r = plan.rates_cents_per_kwh || {};
  const v = r[String(tier)];
  return typeof v === "number" ? v : null;
}
// Estimated monthly bill ($) = effective ¢/kWh × kWh ÷ 100.
function monthlyAt(plan, tier) {
  const r = rateAt(plan, tier);
  return r == null ? null : (r * tier) / 100;
}

// ── Data load ────────────────────────────────────────────────────────────
async function load() {
  let payload;
  try {
    const res = await fetch("/api/plans", { cache: "no-store" });
    payload = await res.json();
  } catch (err) {
    showBanner("warn", "Could not reach the server API: " + err);
    return;
  }
  state.data = payload;
  state.plans = payload.plans || [];

  const src = payload._source || {};
  if (!src.ok) {
    showBanner("warn", src.message || "No plan data available.");
    document.getElementById("subtitle").textContent = "No data loaded";
    renderEmpty(src.message || "No plan data available yet.");
    document.getElementById("count").textContent = "";
    return;
  }
  hideBanner();

  // Compare tier default from payload; ensure it's a real tier.
  const tiers = payload.usage_tiers || [];
  state.compareTier = tiers.includes(payload.compare_tier)
    ? payload.compare_tier
    : (tiers.length ? tiers[Math.floor(tiers.length / 2)] : null);

  buildTierSelect(tiers);
  updateSubtitle();
  render();
}

function buildTierSelect(tiers) {
  const sel = document.getElementById("tier-select");
  sel.innerHTML = "";
  tiers.forEach((t) => {
    const opt = document.createElement("option");
    opt.value = String(t);
    opt.textContent = t.toLocaleString() + " kWh";
    if (t === state.compareTier) opt.selected = true;
    sel.appendChild(opt);
  });
}

function updateSubtitle() {
  const d = state.data;
  const tdu = d.tdu || {};
  const parts = [];
  if (d.zip) parts.push("ZIP " + d.zip);
  if (d.generated) parts.push("generated " + d.generated.replace("T", " "));
  if (tdu.per_kwh_cents != null)
    parts.push(`Oncor TDU ${tdu.per_kwh_cents.toFixed(3)}¢/kWh + ${fmtMoney(tdu.fixed_mo_dollars || 0)}/mo`);
  document.getElementById("subtitle").textContent = parts.join("  ·  ");
}

// ── Filtering / grouping ─────────────────────────────────────────────────
function visiblePlans() {
  const q = state.search.trim().toLowerCase();
  return state.plans.filter((p) => {
    if (state.hideCredit && p.has_bill_credit) return false;
    if (state.onlyFav && !state.favs.has(p.pid)) return false;
    if (q) {
      const hay = (p.provider + " " + p.plan).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function groupByTerm(plans) {
  const groups = new Map();
  for (const p of plans) {
    const key = p.term_months || 0;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(p);
  }
  // Longest term first (matches the parent tool).
  return [...groups.entries()].sort((a, b) => b[0] - a[0]);
}

function sortPlans(plans) {
  const { key, dir } = state.sort;
  const t = state.compareTier;
  const val = (p) => {
    switch (key) {
      case "provider": return (p.provider + p.plan).toLowerCase();
      case "term": return p.term_months || 0;
      case "rnw": return p.renewable_pct || 0;
      case "permo": return monthlyAt(p, t) ?? Infinity;
      case "rate":
      default: return rateAt(p, t) ?? Infinity;
    }
  };
  return [...plans].sort((a, b) => {
    const va = val(a), vb = val(b);
    if (va < vb) return -1 * dir;
    if (va > vb) return 1 * dir;
    return 0;
  });
}

// Best (cheapest at compare tier) among non-current plans in a group.
function bestPid(plans) {
  let best = null, bestRate = Infinity;
  for (const p of plans) {
    if (p.current) continue;
    const r = rateAt(p, state.compareTier);
    if (r != null && r < bestRate) { bestRate = r; best = p.pid; }
  }
  return best;
}

// ── Render ───────────────────────────────────────────────────────────────
function render() {
  const content = document.getElementById("content");
  const filtered = visiblePlans();
  document.getElementById("count").textContent =
    `${filtered.length} of ${state.plans.length} plans`;

  if (!filtered.length) {
    renderEmpty("No plans match the current filters.");
    return;
  }

  content.innerHTML = "";
  const groups = groupByTerm(filtered);
  const t = state.compareTier;

  for (const [term, plansRaw] of groups) {
    const plans = sortPlans(plansRaw);
    const best = bestPid(plansRaw);
    const collapsed = state.collapsed.has(String(term));

    const groupEl = document.createElement("section");
    groupEl.className = "group";

    const head = document.createElement("div");
    head.className = "group-head";
    const cheapest = plans.reduce((m, p) => {
      const r = rateAt(p, t); return (r != null && r < m) ? r : m;
    }, Infinity);
    head.innerHTML =
      `<span class="caret">${collapsed ? "▶" : "▼"}</span>` +
      `<h2>${term ? term + "-month" : "Unknown term"}</h2>` +
      `<span class="meta">${plans.length} plan${plans.length === 1 ? "" : "s"}` +
      (cheapest < Infinity ? ` · from ${fmtCents(cheapest)}/kWh` : "") + `</span>`;
    head.addEventListener("click", () => {
      if (state.collapsed.has(String(term))) state.collapsed.delete(String(term));
      else state.collapsed.add(String(term));
      saveCollapsed();
      render();
    });
    groupEl.appendChild(head);

    if (!collapsed) {
      groupEl.appendChild(buildTable(plans, best, t));
    }
    content.appendChild(groupEl);
  }
}

function buildTable(plans, best, tier) {
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");

  const cols = [
    { key: "fav", label: "", sortable: false },
    { key: "provider", label: "Plan", sortable: true },
    { key: "rate", label: `¢/kWh @ ${tier.toLocaleString()}`, sortable: true, num: true },
    { key: "permo", label: "Est. $/mo", sortable: true, num: true },
    { key: "term", label: "Term", sortable: true, num: true },
    { key: "rnw", label: "Renew.", sortable: true, num: true },
    { key: "etf", label: "Cancel fee", sortable: false },
    { key: "flags", label: "Flags", sortable: false },
  ];

  const thead = document.createElement("thead");
  const tr = document.createElement("tr");
  for (const c of cols) {
    const th = document.createElement("th");
    if (c.num) th.className = "num";
    let label = c.label;
    if (c.sortable && state.sort.key === c.key) {
      label += ` <span class="arrow">${state.sort.dir === 1 ? "▲" : "▼"}</span>`;
    }
    th.innerHTML = label;
    if (c.sortable) {
      th.addEventListener("click", () => {
        if (state.sort.key === c.key) state.sort.dir *= -1;
        else state.sort = { key: c.key, dir: 1 };
        render();
      });
    } else {
      th.style.cursor = "default";
    }
    tr.appendChild(th);
  }
  thead.appendChild(tr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const p of plans) {
    tbody.appendChild(buildRow(p, best, tier));
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function buildRow(p, best, tier) {
  const tr = document.createElement("tr");
  if (p.current) tr.className = "is-current";
  else if (p.pid === best) tr.className = "is-best";

  // Fav / marker cell
  const favTd = document.createElement("td");
  if (p.current) {
    favTd.innerHTML = `<span class="pin" title="Your current plan">📍</span>`;
  } else {
    const btn = document.createElement("button");
    btn.className = "fav" + (state.favs.has(p.pid) ? " on" : "");
    btn.type = "button";
    btn.title = "Toggle favorite";
    btn.innerHTML = state.favs.has(p.pid) ? "❤" : "♡";
    btn.addEventListener("click", () => {
      if (state.favs.has(p.pid)) state.favs.delete(p.pid);
      else state.favs.add(p.pid);
      saveFavs();
      render();
    });
    favTd.appendChild(btn);
    if (p.pid === best) {
      const star = document.createElement("span");
      star.className = "star"; star.title = "Cheapest in group"; star.textContent = " ★";
      favTd.appendChild(star);
    }
  }
  tr.appendChild(favTd);

  // Plan / provider
  const nameTd = document.createElement("td");
  const nameHtml = p.facts_url
    ? `<a href="${escapeAttr(p.facts_url)}" target="_blank" rel="noopener">${escapeHtml(p.plan)}</a>`
    : escapeHtml(p.plan);
  nameTd.innerHTML =
    `<div class="plan-name">${nameHtml}</div>` +
    `<div class="provider">${escapeHtml(p.provider)}</div>`;
  tr.appendChild(nameTd);

  // Rate
  const rate = rateAt(p, tier);
  const rateTd = document.createElement("td");
  rateTd.className = "num";
  rateTd.innerHTML = `<span class="rate-main">${fmtCents(rate)}</span>`;
  tr.appendChild(rateTd);

  // $/mo
  const mo = monthlyAt(p, tier);
  const moTd = document.createElement("td");
  moTd.className = "num";
  moTd.innerHTML = mo == null ? "—" : `<span class="permo">${fmtMoney(mo)}</span>`;
  tr.appendChild(moTd);

  // Term
  const termTd = document.createElement("td");
  termTd.className = "num";
  termTd.textContent = p.term_months ? p.term_months + " mo" : "—";
  tr.appendChild(termTd);

  // Renewable
  const rnwTd = document.createElement("td");
  rnwTd.className = "num";
  rnwTd.innerHTML = p.renewable_pct != null
    ? `<span class="${p.renewable_pct >= 100 ? "rnw" : ""}">${p.renewable_pct}%</span>` : "—";
  tr.appendChild(rnwTd);

  // Cancel fee
  const etfTd = document.createElement("td");
  etfTd.textContent = p.cancellation_fee || "—";
  tr.appendChild(etfTd);

  // Flags
  const flagsTd = document.createElement("td");
  flagsTd.appendChild(buildBadges(p));
  tr.appendChild(flagsTd);

  return tr;
}

function buildBadges(p) {
  const wrap = document.createElement("span");
  wrap.className = "badges";
  const add = (cls, text, title) => {
    const b = document.createElement("span");
    b.className = "badge " + cls;
    b.textContent = text;
    if (title) b.title = title;
    wrap.appendChild(b);
  };
  const src = (p.src || "").toUpperCase();
  if (src === "EFL") add("src-efl", "EFL", "Rates from the legal EFL document");
  else if (src === "LLM") add("src-llm", "LLM", "Rates extracted by local AI from EFL text");
  else if (src === "API") add("src-api", "API", "Rates estimated from PUCT CSV price data");

  if (p.has_bill_credit) add("b-credit", "¢",
    p.fees_credits_text || "Bill-credit plan — advertised rate valid near the credit threshold");
  if (p.one_time_fee_dollars > 0) add("b-fee", "⚠ $" + p.one_time_fee_dollars,
    "One-time setup fee (not included in displayed rates)");
  if (p.manual) add("b-manual", "M", p.special_terms || "Manually-supplied EFL — not verified against powertochoose.org");
  if (p.current) add("b-current", "CURRENT", "Your existing plan");
  return wrap;
}

// ── Small UI helpers ─────────────────────────────────────────────────────
function renderEmpty(msg) {
  document.getElementById("content").innerHTML =
    `<div class="empty">${escapeHtml(msg)}</div>`;
}
function showBanner(kind, msg) {
  const el = document.getElementById("banner");
  el.className = "banner " + (kind || "");
  el.textContent = msg;
  el.hidden = false;
}
function hideBanner() { document.getElementById("banner").hidden = true; }

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;");
}

// ── Wire up controls ─────────────────────────────────────────────────────
function initControls() {
  document.getElementById("tier-select").addEventListener("change", (e) => {
    state.compareTier = Number(e.target.value);
    render();
  });
  document.getElementById("search").addEventListener("input", (e) => {
    state.search = e.target.value;
    render();
  });
  document.getElementById("hide-credit").addEventListener("change", (e) => {
    state.hideCredit = e.target.checked;
    render();
  });
  document.getElementById("only-fav").addEventListener("change", (e) => {
    state.onlyFav = e.target.checked;
    render();
  });
}

// Reflect data source in the footer once loaded.
function updateSourceNote() {
  const src = (state.data && state.data._source) || {};
  const note = document.getElementById("source-note");
  if (!src.ok) { note.textContent = ""; return; }
  const base = "Data: " + (src.path || "");
  note.textContent = base + (src.file_mtime ? "  ·  updated " + src.file_mtime.replace("T", " ") : "");
}

// ── Boot ─────────────────────────────────────────────────────────────────
initTheme();
initControls();
load().then(updateSourceNote);
