/* NOI dashboard frontend.
 *
 * This file draws; it does not calculate. Every figure comes from
 * POST /api/noi/render, which turns a view (period, basis, filters, property)
 * and a list of widget specs into render-ready data. The AI assistant returns
 * prose plus widget specs; adding one to the dashboard just appends a spec, so
 * the chart is drawn from the dataset by the same server code as the rest.
 *
 * The layout (widgets, order, sizes, titles, pinned AI notes) is saved in this
 * browser's localStorage. Answer text is model output: always escaped, always
 * labelled.
 */
"use strict";

const STORE_KEY = "noi-dashboard-v1";
const state = {
  meta: null, view: { from: null, to: null, compare: "budget", sectors: [], regions: [], property_id: null },
  layouts: { portfolio: [], property: [] }, title: "", editing: false, chatOpen: true,
  results: [], viewInfo: null, turns: [], provider: null, flash: null,
};
const trendStore = new Map();
let trendSeq = 0;

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const newId = () => "w" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthLabel = (m, year = true) => MONTH_NAMES[Number(m.slice(5)) - 1] + (year ? " " + m.slice(2, 4) : "");

/* ------------------------------------------------------------------ formatting */

function money(value, compact = true) {
  if (value === null || value === undefined) return "–";
  const sign = value < 0 ? "−" : "", abs = Math.abs(value);
  if (!compact) return sign + "£" + Math.round(abs).toLocaleString("en-GB");
  if (abs >= 1e6) return `${sign}£${(abs / 1e6).toFixed(2)}m`;
  if (abs >= 1e3) return `${sign}£${Math.round(abs / 1e3).toLocaleString("en-GB")}k`;
  return `${sign}£${Math.round(abs)}`;
}
function fmt(value, format, compact = true) {
  if (value === null || value === undefined) return "–";
  if (format === "pct") return (value * 100).toFixed(1) + "%";
  if (format === "psf") return (value < 0 ? "−£" : "£") + Math.abs(value).toFixed(2);
  return money(value, compact);
}
function deltaText(row, format) {
  if (row.delta === null || row.delta === undefined) return "";
  if (row.delta === 0) return "no change";
  const up = row.delta > 0, abs = Math.abs(row.delta);
  const size = format === "pct" ? (abs * 100).toFixed(1) + " pts" : fmt(abs, format);
  const pct = row.delta_pct === null || row.delta_pct === undefined ? ""
    : ` (${up ? "+" : "−"}${Math.abs(row.delta_pct * 100).toFixed(1)}%)`;
  return `${up ? "▲" : "▼"} ${size}${pct}`;
}
const tone = (favourable) => (favourable === true ? "good" : favourable === false ? "bad" : "flat");
const deltaHtml = (row, format) => (row.delta === null || row.delta === undefined ? ""
  : `<span class="delta ${tone(row.favourable)}">${deltaText(row, format)}</span>`);
const wayText = (favourable) => (favourable === true ? "favourable" : favourable === false ? "unfavourable" : "");

function textHtml(text) {
  // Plain text to paragraphs and "- " lists. Escaped: this may be model output.
  const out = [];
  for (const block of String(text || "").split(/\n\s*\n/)) {
    const lines = block.split("\n").map((l) => l.trim()).filter(Boolean);
    let para = [], items = [];
    const flush = () => {
      if (para.length) out.push(`<p>${para.map(esc).join("<br>")}</p>`);
      if (items.length) out.push(`<ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`);
      para = []; items = [];
    };
    for (const line of lines) {
      if (/^[-•*]\s+/.test(line)) { if (para.length) flush(); items.push(line.replace(/^[-•*]\s+/, "")); }
      else { if (items.length) flush(); para.push(line); }
    }
    flush();
  }
  return out.join("");
}

function modeBadge(mode, label) {
  if (mode === "real_ai") return `<span class="badge real">Real AI${label ? " · " + esc(label) : ""}</span>`;
  if (mode === "simulated") return `<span class="badge sim">Simulated${label ? " · " + esc(label) : ""}</span>`;
  return `<span class="badge off">Not connected</span>`;
}

function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast no-print" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 5000);
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error?.message || data.detail?.[0]?.msg || `Request failed (${response.status})`);
  return data;
}

/* ------------------------------------------------------------------ saved layout */

function defaultLayouts() {
  const w = (spec) => ({ id: newId(), size: "half", ...spec });
  return {
    portfolio: [
      w({ type: "kpi", size: "full" }),
      w({ type: "trend", metric: "noi" }),
      w({ type: "bridge", dimension: "property" }),
      w({ type: "rank", dimension: "property", metric: "noi_variance", order: "asc" }),
      w({ type: "rank", dimension: "opex_line", order: "desc" }),
      w({ type: "table", size: "full" }),
    ],
    property: [
      w({ type: "kpi", size: "full" }),
      w({ type: "trend", metric: "noi" }),
      w({ type: "bridge", dimension: "line" }),
      w({ type: "statement", size: "full" }),
      w({ type: "trend", metric: "occupancy" }),
      w({ type: "rank", dimension: "opex_line", order: "desc" }),
    ],
  };
}
function save() {
  try {
    const { from, to, compare, sectors, regions } = state.view;
    localStorage.setItem(STORE_KEY, JSON.stringify({ layouts: state.layouts, title: state.title,
      chatOpen: state.chatOpen, provider: state.provider, view: { from, to, compare, sectors, regions } }));
  } catch { /* storage unavailable: the dashboard still works, it just will not remember */ }
}
function load() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { saved = null; }
  const defaults = defaultLayouts();
  const ok = (list) => Array.isArray(list) && list.every((w) => w && typeof w.type === "string" && w.id);
  state.layouts.portfolio = ok(saved?.layouts?.portfolio) ? saved.layouts.portfolio : defaults.portfolio;
  state.layouts.property = ok(saved?.layouts?.property) ? saved.layouts.property : defaults.property;
  state.title = typeof saved?.title === "string" ? saved.title : "";
  state.chatOpen = saved?.chatOpen !== false;
  state.provider = saved?.provider || null;
  const months = state.meta.months, v = saved?.view || {};
  state.view.from = months.includes(v.from) ? v.from : months[0];
  state.view.to = months.includes(v.to) ? v.to : months[months.length - 1];
  if (state.view.from > state.view.to) state.view.from = months[0];
  state.view.compare = v.compare === "prior" ? "prior" : "budget";
  state.view.sectors = (v.sectors || []).filter((s) => state.meta.sectors.includes(s));
  state.view.regions = (v.regions || []).filter((r) => state.meta.regions.includes(r));
}
const layoutKey = () => (state.view.property_id ? "property" : "portfolio");
const layout = () => state.layouts[layoutKey()];
const property = (id) => state.meta.properties.find((p) => p.id === id);

/* ------------------------------------------------------------------ filters */

function renderFilters() {
  const { months, sectors, regions, properties } = state.meta, v = state.view;
  const options = (selected) => months.map((m) =>
    `<option value="${m}"${m === selected ? " selected" : ""}>${monthLabel(m)}</option>`).join("");
  $("f-from").innerHTML = options(v.from);
  $("f-to").innerHTML = options(v.to);
  const last = months.length - 1, ytd = months.findIndex((m) => m.endsWith("-01"));
  const presets = [["Full year", 0, last], ["Year to date", ytd < 0 ? 0 : ytd, last],
                   ["Last 3 months", last - 2, last], ["Latest month", last, last]];
  $("f-presets").innerHTML = presets.map(([label, a, b]) =>
    `<button type="button" data-from="${months[a]}" data-to="${months[b]}" aria-pressed="${
      v.from === months[a] && v.to === months[b]}">${label}</button>`).join("");
  $("f-compare").innerHTML = [["budget", "Budget"], ["prior", "Prior year"]].map(([key, label]) =>
    `<button type="button" data-compare="${key}" aria-pressed="${v.compare === key}">${label}</button>`).join("");
  const chips = (names, picked, attr) => names.map((n) =>
    `<button type="button" class="chip" data-${attr}="${esc(n)}" aria-pressed="${picked.includes(n)}">${esc(n)}</button>`).join("");
  $("f-sectors").innerHTML = chips(sectors, v.sectors, "sector");
  $("f-regions").innerHTML = chips(regions, v.regions, "region");
  $("f-sector-wrap").hidden = $("f-region-wrap").hidden = Boolean(v.property_id);
  $("f-property").innerHTML = `<option value="">Whole portfolio</option>` + sectors.map((s) =>
    `<optgroup label="${esc(s)}">${properties.filter((p) => p.sector === s).map((p) =>
      `<option value="${p.id}"${p.id === v.property_id ? " selected" : ""}>${esc(p.name)}</option>`).join("")}</optgroup>`).join("");
}

function setView(change, { keepProperty = false } = {}) {
  Object.assign(state.view, change);
  if (state.view.from > state.view.to) {
    if ("from" in change) state.view.to = state.view.from; else state.view.from = state.view.to;
  }
  if (!keepProperty && ("sectors" in change || "regions" in change)) state.view.property_id = null;
  syncHash();
  save();
  renderFilters();
  return refresh();
}
function openProperty(id) {
  window.scrollTo({ top: 0 });
  return setView({ property_id: id || null }, { keepProperty: true });
}
function syncHash() {
  const want = state.view.property_id ? `#property-${state.view.property_id}` : "";
  if (location.hash !== want) history.replaceState(null, "", want || location.pathname);
}

/* ------------------------------------------------------------------ rendering: widget bodies */

function spark(values, cls = "spark") {
  const pts = values.filter((v) => v !== null && v !== undefined);
  if (pts.length < 2) return "";
  const lo = Math.min(...pts), hi = Math.max(...pts), W = 72, H = 24, pad = 3;
  const x = (i) => pad + (i * (W - 2 * pad)) / (pts.length - 1);
  const y = (v) => (hi === lo ? H / 2 : H - pad - ((v - lo) / (hi - lo)) * (H - 2 * pad));
  const d = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  return `<svg class="${cls}" viewBox="0 0 ${W} ${H}" aria-hidden="true"><path d="${d}" fill="none" stroke="var(--series)" stroke-width="1.5" stroke-linejoin="round"/><circle cx="${x(pts.length - 1).toFixed(1)}" cy="${y(pts[pts.length - 1]).toFixed(1)}" r="2.2" fill="var(--series)"/></svg>`;
}

function renderKpi(data) {
  return `<div class="tiles">${data.tiles.map((t) => `
    <div class="tile">
      <div class="tile-label">${esc(t.label)}</div>
      <div class="tile-value">${fmt(t.value, t.format)}</div>
      <div class="tile-foot">
        <div>${deltaHtml(t, t.format)}<div class="tile-compare">${esc(data.compare_label)} ${fmt(t.compare, t.format)}</div></div>
        ${spark(t.spark)}
      </div>
    </div>`).join("")}</div>`;
}

function renderTrend(data) {
  const key = "t" + (++trendSeq);
  trendStore.set(key, data);
  return `<div class="legend"><span><i></i>Actual</span><span><i class="ref"></i>${esc(data.compare_label)}</span></div>
    <div class="chart" data-trend="${key}" role="img" aria-label="${esc(data.title)}: line chart of actual against ${esc(data.compare_label.toLowerCase())}"></div>`;
}

function niceTicks(lo, hi, count = 4) {
  if (lo === hi) { lo -= Math.abs(lo) * 0.05 || 1; hi += Math.abs(hi) * 0.05 || 1; }
  const raw = (hi - lo) / count, pow = 10 ** Math.floor(Math.log10(raw)), unit = raw / pow;
  const step = (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 2.5 ? 2.5 : unit <= 5 ? 5 : 10) * pow;
  const start = Math.floor(lo / step) * step, ticks = [];
  for (let v = start; v < hi + step * 0.999; v += step) ticks.push(Number(v.toPrecision(12)));
  return ticks;
}

function drawTrend(el) {
  const data = trendStore.get(el.dataset.trend);
  if (!data) return;
  const compact = Boolean(el.closest(".suggest"));
  const W = Math.max(260, Math.round(el.clientWidth || 520)), H = compact ? 150 : 230;
  const m = { l: 54, r: 18, t: 16, b: 24 };
  const [actual, ref] = data.series, n = data.months.length;
  const all = [...actual.values, ...ref.values].filter((v) => v !== null);
  const ticks = niceTicks(Math.min(...all), Math.max(...all), compact ? 3 : 4);
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const x = (i) => m.l + (i * (W - m.l - m.r)) / (n - 1);
  const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);
  const path = (values) => values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const tickFmt = (v) => (data.format === "pct" ? (v * 100).toFixed(hi - lo < 0.05 ? 1 : 0) + "%" : fmt(v, data.format));
  const every = (W - m.l - m.r) / (n - 1) < 40 ? 2 : 1;
  const [p0, p1] = data.period, half = (W - m.l - m.r) / (n - 1) / 2;
  let svg = `<svg viewBox="0 0 ${W} ${H}">`;
  for (const t of ticks) {
    svg += `<line class="gridline" x1="${m.l}" x2="${W - m.r}" y1="${y(t).toFixed(1)}" y2="${y(t).toFixed(1)}"/>` +
           `<text x="${m.l - 8}" y="${(y(t) + 4).toFixed(1)}" text-anchor="end">${tickFmt(t)}</text>`;
  }
  data.months.forEach((mo, i) => {
    if (i % every === 0) svg += `<text x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle">${monthLabel(mo, i === 0 || mo.endsWith("-01"))}</text>`;
  });
  svg += `<path class="line-ref" d="${path(ref.values)}"/><path class="line-actual" d="${path(actual.values)}"/>`;
  if (p0 > 0) svg += `<rect class="dim" x="${m.l - 2}" y="${m.t - 6}" width="${(x(p0) - half - m.l + 2).toFixed(1)}" height="${H - m.t - m.b + 8}"/>`;
  if (p1 < n - 1) svg += `<rect class="dim" x="${(x(p1) + half).toFixed(1)}" y="${m.t - 6}" width="${(W - m.r - x(p1) - half + 4).toFixed(1)}" height="${H - m.t - m.b + 8}"/>`;
  const end = actual.values[p1];
  svg += `<circle cx="${x(p1).toFixed(1)}" cy="${y(end).toFixed(1)}" r="4" fill="var(--series)" stroke="#fff" stroke-width="2"/>` +
         `<text class="val" x="${x(p1).toFixed(1)}" y="${(y(end) - 9).toFixed(1)}" text-anchor="${p1 > n - 3 ? "end" : "middle"}">${fmt(end, data.format)}</text>`;
  svg += `<g class="hover" visibility="hidden"><line class="cross" y1="${m.t}" y2="${H - m.b}"/>` +
         `<circle class="h-ref" r="3.5" fill="var(--reference)" stroke="#fff" stroke-width="1.5"/>` +
         `<circle class="h-act" r="4" fill="var(--series)" stroke="#fff" stroke-width="2"/></g>` +
         `<rect class="hit" x="${m.l - half}" y="${m.t}" width="${W - m.l - m.r + 2 * half}" height="${H - m.t - m.b}" fill="transparent"/></svg>`;
  el.innerHTML = svg;
  const node = el.querySelector("svg"), hover = node.querySelector(".hover");
  node.querySelector(".hit").addEventListener("mousemove", (event) => {
    const box = node.getBoundingClientRect(), px = ((event.clientX - box.left) / box.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * (n - 1))));
    hover.setAttribute("visibility", "visible");
    hover.querySelector(".cross").setAttribute("x1", x(i)); hover.querySelector(".cross").setAttribute("x2", x(i));
    hover.querySelector(".h-act").setAttribute("cx", x(i)); hover.querySelector(".h-act").setAttribute("cy", y(actual.values[i]));
    hover.querySelector(".h-ref").setAttribute("cx", x(i)); hover.querySelector(".h-ref").setAttribute("cy", y(ref.values[i]));
    const diff = actual.values[i] - ref.values[i];
    const diffText = data.format === "pct" ? `${diff >= 0 ? "+" : "−"}${Math.abs(diff * 100).toFixed(1)} pts`
      : `${diff >= 0 ? "+" : "−"}${fmt(Math.abs(diff), data.format)}`;
    showTip(`<b>${monthLabel(data.months[i])}</b>Actual ${fmt(actual.values[i], data.format)}<br>${esc(ref.name)} ${fmt(ref.values[i], data.format)}<br>Difference ${diffText}`, event);
  });
  node.querySelector(".hit").addEventListener("mouseleave", () => { hover.setAttribute("visibility", "hidden"); hideTip(); });
}
function drawTrends(root = document) { root.querySelectorAll("[data-trend]").forEach(drawTrend); }

function labelCell(row) {
  const detail = row.detail ? `<small>${esc(row.detail)}</small>` : "";
  return row.property_id
    ? `<div class="lbl"><button type="button" class="linkish" data-open="${esc(row.property_id)}">${esc(row.label)}</button>${detail}</div>`
    : `<div class="lbl">${esc(row.label)}${detail}</div>`;
}

function renderRank(data) {
  if (!data.rows.length) return `<p class="muted">Nothing to show for these filters.</p>`;
  const values = data.rows.flatMap((r) => [r.value, r.compare]).filter((v) => v !== null && v !== undefined);
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values), span = hi - lo || 1;
  const pos = (v) => ((v - lo) / span) * 100;
  const hasMark = data.rows.some((r) => r.compare !== null && r.compare !== undefined);
  const rows = data.rows.map((r) => {
    const a = pos(Math.min(0, r.value)), b = pos(Math.max(0, r.value));
    const cls = data.diverging ? tone(r.favourable) : "";
    const tip = data.diverging
      ? `<b>${esc(r.label)}</b>NOI ${fmt(r.value, data.format)} ${wayText(r.favourable)} to ${esc(data.compare_label.toLowerCase())}`
      : `<b>${esc(r.label)}</b>Actual ${fmt(r.value, data.format, false)}<br>${esc(data.compare_label)} ${fmt(r.compare, data.format, false)}${r.delta ? `<br>${deltaText(r, data.format)} ${wayText(r.favourable)}` : ""}`;
    const value = data.diverging ? `${r.value > 0 ? "+" : ""}${fmt(r.value, data.format)}` : fmt(r.value, data.format);
    const side = data.diverging && r.delta_pct !== null && r.delta_pct !== undefined
      ? `<span class="delta ${tone(r.favourable)}">${r.delta_pct > 0 ? "+" : "−"}${Math.abs(r.delta_pct * 100).toFixed(1)}%</span>`
      : deltaHtml(r, data.format);
    return `${labelCell(r)}
      <div class="track" data-tip="${esc(tip)}">
        <div class="bar ${cls} ${r.value < 0 ? "neg" : ""}" style="left:${a.toFixed(2)}%;width:${(b - a).toFixed(2)}%"></div>
        ${lo < 0 ? `<div class="zero" style="left:${pos(0).toFixed(2)}%"></div>` : ""}
        ${r.compare !== null && r.compare !== undefined ? `<div class="mark" style="left:${pos(r.compare).toFixed(2)}%"></div>` : ""}
      </div>
      <div class="num">${value}</div><div class="dcol">${side}</div>`;
  }).join("");
  const legend = data.diverging
    ? `<div class="legend"><span><i class="box" style="background:var(--good)"></i>Ahead of ${esc(data.compare_label.toLowerCase())}</span><span><i class="box" style="background:var(--bad)"></i>Behind</span></div>`
    : hasMark ? `<div class="legend"><span><i class="box" style="background:var(--series)"></i>Actual</span><span><i class="tick"></i>${esc(data.compare_label)}</span></div>` : "";
  const more = data.rows.length < data.total_rows ? `<p class="chart-note">Showing ${data.rows.length} of ${data.total_rows}.</p>` : "";
  return `${legend}<div class="bars">${rows}</div>${more}`;
}

function renderBridge(data) {
  let running = data.start.value;
  const points = [running];
  const steps = data.steps.map((s) => { const from = running; running += s.delta; points.push(running); return { ...s, from, to: running }; });
  points.push(data.end.value);
  const min = Math.min(...points), max = Math.max(...points), pad = (max - min || Math.abs(max) * 0.02 || 1) * 0.12;
  const lo = min - pad, span = max + pad - lo;
  const pos = (v) => ((v - lo) / span) * 100;
  const pin = (p, cls) => `<div class="lbl"><b>${esc(p.label)}</b></div>
    <div class="track" data-tip="${esc(`<b>${esc(p.label)} NOI</b>${money(p.value, false)}`)}"><div class="pin" style="left:${pos(p.value).toFixed(2)}%"></div></div>
    <div class="num">${money(p.value)}</div><div class="dcol ${cls}"></div>`;
  const rows = steps.map((s) => {
    const a = pos(Math.min(s.from, s.to)), b = pos(Math.max(s.from, s.to));
    const tip = `<b>${esc(s.label)}</b>${s.delta > 0 ? "Added" : "Took"} ${money(Math.abs(s.delta), false)} ${s.delta > 0 ? "to" : "off"} NOI against ${esc(data.start.label.toLowerCase())}`;
    return `${labelCell(s)}
      <div class="track" data-tip="${esc(tip)}"><div class="bar float ${s.favourable ? "good" : "bad"}" style="left:${a.toFixed(2)}%;width:${Math.max(b - a, 0.6).toFixed(2)}%"></div></div>
      <div class="num"><span class="delta ${s.favourable ? "good" : "bad"}">${s.delta > 0 ? "+" : "−"}${money(Math.abs(s.delta))}</span></div><div class="dcol"></div>`;
  }).join("");
  const total = data.end.value - data.start.value;
  return `<div class="bars">${pin(data.start, "")}${rows}${pin(data.end, "")}</div>
    <p class="chart-note">Each bar is one item's effect on NOI; green adds, red takes away. Net: <span class="delta ${total >= 0 ? "good" : "bad"}">${total >= 0 ? "+" : "−"}${money(Math.abs(total))}</span>.
    The scale is zoomed to the gap between ${esc(data.start.label.toLowerCase())} and actual, so compare bars with each other, not with total NOI.</p>`;
}

function renderStatement(data) {
  const rows = data.rows.map((r) => {
    const cls = { subtotal: "sub", total: "total", ratio: "ratio" }[r.kind] || "";
    const pct = !r.delta || r.delta_pct === null || r.delta_pct === undefined ? "" : `${r.delta_pct > 0 ? "+" : "−"}${Math.abs(r.delta_pct * 100).toFixed(1)}%`;
    return `<tr class="${cls}"><td>${esc(r.label)}</td><td class="num">${fmt(r.value, r.format, false)}</td>
      <td class="num">${fmt(r.compare, r.format, false)}</td>
      <td class="num">${r.delta ? `<span class="delta ${tone(r.favourable)}">${r.delta > 0 ? "+" : "−"}${r.format === "pct" ? (Math.abs(r.delta) * 100).toFixed(1) + " pts" : fmt(Math.abs(r.delta), r.format, false)}</span>` : "–"}</td>
      <td class="num">${pct}</td><td>${r.delta ? `<span class="delta ${tone(r.favourable)}">${r.favourable ? "Favourable" : "Unfavourable"}</span>` : ""}</td></tr>`;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr><th>Line</th><th class="num">Actual</th><th class="num">${esc(data.compare_label)}</th>
    <th class="num">Variance</th><th class="num">%</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderTable(data) {
  const cells = (r) => `<td class="num">${fmt(r.occupancy, "pct")}</td><td class="num">${money(r.revenue)}</td>
    <td class="num">${money(r.opex)}</td><td class="num">${money(r.noi)}</td>
    <td class="num">${r.noi_delta ? `<span class="delta ${tone(r.favourable)}">${r.noi_delta > 0 ? "+" : "−"}${money(Math.abs(r.noi_delta))} (${r.noi_delta_pct > 0 ? "+" : "−"}${Math.abs(r.noi_delta_pct * 100).toFixed(1)}%)</span>` : "–"}</td>
    <td class="num">${fmt(r.margin, "pct")}</td><td class="num">${fmt(r.noi_psf, "psf")}</td>`;
  const rows = data.rows.map((r) => `<tr class="clickable" data-open="${esc(r.property_id)}">
    <td><button type="button" class="linkish" data-open="${esc(r.property_id)}">${esc(r.name)}</button><div class="small muted">${esc(r.sector)} · ${esc(r.city)}</div></td>
    ${cells(r)}<td>${spark(r.spark)}</td></tr>`).join("");
  const total = data.rows.length > 1 ? `<tr class="total"><td>Total</td>${cells(data.total)}<td></td></tr>` : "";
  return `<div class="table-wrap"><table><thead><tr><th>Property</th><th class="num">Occupancy</th><th class="num">Revenue</th>
    <th class="num">Expenses</th><th class="num">NOI</th><th class="num">vs ${esc(data.compare_label.toLowerCase())}</th>
    <th class="num">Margin</th><th class="num">NOI / sq ft</th><th>NOI by month</th></tr></thead><tbody>${rows}${total}</tbody></table></div>`;
}

function provenanceHtml(ai) {
  if (!ai) return "";
  const when = ai.at ? new Date(ai.at).toLocaleDateString("en-GB", { dateStyle: "medium" }) : "";
  return `<div class="provenance">${modeBadge(ai.mode, ai.model)}<span>${ai.mode === "simulated" ? "Scripted commentary" : "AI-generated commentary"}${when ? ", " + esc(when) : ""}. Not independently checked against the figures.</span></div>`;
}
function renderNote(widget) {
  const body = state.editing
    ? `<textarea data-cfg="text" aria-label="Note text">${esc(widget.text || "")}</textarea>`
    : textHtml(widget.text) || `<p class="muted">Empty note. Switch on Customise to write in it.</p>`;
  return `${widget.ai?.question ? `<p class="small muted">In answer to: ${esc(widget.ai.question)}</p>` : ""}
    <div class="note-text">${body}</div>${provenanceHtml(widget.ai)}`;
}

function renderBody(spec, data) {
  switch (spec.type) {
    case "kpi": return renderKpi(data);
    case "trend": return renderTrend(data);
    case "rank": return renderRank(data);
    case "bridge": return renderBridge(data);
    case "statement": return renderStatement(data);
    case "table": return renderTable(data);
    default: return "";
  }
}

/* ------------------------------------------------------------------ rendering: board */

const TYPE_LABEL = { kpi: "Headline figures", trend: "Monthly trend", rank: "Ranking", bridge: "Variance bridge",
                     statement: "NOI statement", table: "Property table", note: "Note" };

function scopeValue(w) {
  const s = w.scope || {};
  return s.property_id ? `p:${s.property_id}` : s.sector ? `s:${s.sector}` : s.region ? `r:${s.region}` : "";
}
function configHtml(w) {
  const cat = state.meta.catalogue;
  const select = (name, label, options, value) => `<label>${label}<select data-cfg="${name}">${options.map((o) =>
    `<option value="${esc(o.key)}"${o.key === value ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select></label>`;
  let html = `<label>Title<input type="text" data-cfg="title" value="${esc(w.title || "")}" placeholder="Automatic" maxlength="90"></label>`;
  if (w.type === "trend") html += select("metric", "Plot", cat.trend_metrics, w.metric || "noi");
  if (w.type === "rank") {
    html += select("dimension", "By", cat.rank_dimensions, w.dimension || "property");
    if (!["opex_line", "revenue_line"].includes(w.dimension)) html += select("metric", "Measure", cat.rank_metrics, w.metric || "noi");
    html += select("order", "Order", [{ key: "desc", label: "Highest first" }, { key: "asc", label: "Lowest first" }], w.order || "desc");
    html += `<label>Show top<input type="number" data-cfg="limit" min="0" max="50" value="${w.limit || 0}" title="0 shows all"></label>`;
  }
  if (w.type === "bridge") html += select("dimension", "By", cat.bridge_dimensions, w.dimension || "line");
  if (w.type !== "note") {
    const sv = scopeValue(w), opt = (value, label) => `<option value="${esc(value)}"${value === sv ? " selected" : ""}>${esc(label)}</option>`;
    html += `<label>Covers<select data-cfg="scope">${opt("", "Whatever the filters show")}
      <optgroup label="Always this sector">${state.meta.sectors.map((s) => opt(`s:${s}`, s)).join("")}</optgroup>
      <optgroup label="Always this region">${state.meta.regions.map((r) => opt(`r:${r}`, r)).join("")}</optgroup>
      <optgroup label="Always this property">${state.meta.properties.map((p) => opt(`p:${p.id}`, p.name)).join("")}</optgroup></select></label>`;
  }
  return `<div class="wcfg">${html}</div>`;
}

function widgetHtml(w, i, count) {
  const result = state.results[i] || {};
  const data = result.data, isNote = w.type === "note";
  const title = w.title || (isNote ? (w.ai ? "Commentary" : "Note") : data?.title) || TYPE_LABEL[w.type] || "Widget";
  const sub = isNote || !data ? "" : `${esc(data.scope_label)}${w.scope && Object.keys(w.scope).length ? " (fixed)" : ""} · actual vs ${esc(data.compare_label.toLowerCase())}`;
  const tools = `<span class="wtools">
    <button type="button" data-act="up" ${i === 0 ? "disabled" : ""} aria-label="Move up">↑</button>
    <button type="button" data-act="down" ${i === count - 1 ? "disabled" : ""} aria-label="Move down">↓</button>
    <button type="button" data-act="size">${w.size === "full" ? "Make half width" : "Make full width"}</button>
    <button type="button" data-act="remove" class="danger">Remove</button></span>`;
  const body = isNote ? renderNote(w) : data ? renderBody(result.spec || w, data)
    : `<p class="widget-error">${esc(result.error || "This widget could not be drawn.")}</p>`;
  return `<section class="widget ${w.size === "full" ? "full" : ""} ${w.type === "kpi" ? "kpi-widget" : ""}${state.flash === w.id ? " flash" : ""}" id="${esc(w.id)}" data-i="${i}">
    <div class="widget-head"><div class="min0"><h3>${esc(title)}</h3>${sub ? `<div class="widget-sub">${sub}</div>` : ""}</div>${tools}</div>
    ${configHtml(w)}<div class="wbody">${body}</div></section>`;
}

function renderBoard() {
  const { meta, view, viewInfo } = state, prop = view.property_id ? property(view.property_id) : null;
  document.body.classList.toggle("editing", state.editing);
  $("brand-sub").textContent = `${meta.portfolio} · ${meta.fictional ? "fictional data only" : meta.currency}`;
  const title = $("report-title");
  if (document.activeElement !== title) title.textContent = state.title || "Net operating income report";
  title.contentEditable = state.editing ? "true" : "false";
  const period = view.from === view.to ? monthLabel(view.from) : `${monthLabel(view.from)} to ${monthLabel(view.to)}`;
  const scopeBits = prop ? [`${prop.name}`, `${prop.sector}, ${prop.city}`, `${prop.sqft.toLocaleString("en-GB")} sq ft`]
    : [meta.portfolio, [...view.sectors, ...view.regions].join(", ") || `all ${meta.properties.length} properties`];
  $("report-sub").textContent = `${scopeBits.join(" · ")} · ${period} · actual against ${viewInfo.compare_label.toLowerCase()}`;
  $("report-stamp").innerHTML = `Prepared ${new Date().toLocaleDateString("en-GB", { dateStyle: "long" })}<br>${meta.fictional ? "Fictional data · " : ""}${esc(meta.currency)}`;
  $("crumbs").innerHTML = prop ? (() => {
    const all = meta.properties, at = all.findIndex((p) => p.id === prop.id);
    const prev = all[(at + all.length - 1) % all.length], next = all[(at + 1) % all.length];
    return `<button type="button" data-open="">Portfolio</button> / ${esc(prop.name)} &nbsp;·&nbsp;
      <button type="button" data-open="${prev.id}">← ${esc(prev.name)}</button> &nbsp;
      <button type="button" data-open="${next.id}">${esc(next.name)} →</button>`;
  })() : "";
  $("property-note").innerHTML = prop?.note ? `<div class="banner note"><strong>Note on file</strong>${esc(prop.note)}</div>` : "";
  $("edit-bar").hidden = !state.editing;
  if (state.editing) {
    $("edit-bar").innerHTML = `<strong>Customising the ${prop ? "property" : "portfolio"} layout.</strong>
      <label>Add <select id="add-type">${Object.entries(TYPE_LABEL).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
      <button type="button" class="btn tiny" id="add-widget">Add</button>
      <button type="button" class="btn secondary tiny" id="reset-layout">Reset this layout</button>
      <span class="small muted">Click the report title to rename it. Changes are saved in this browser.</span>`;
  }
  trendStore.clear();
  const widgets = layout();
  $("widgets").innerHTML = widgets.length ? widgets.map((w, i) => widgetHtml(w, i, widgets.length)).join("")
    : `<div class="widget full"><p class="muted">This layout is empty. Switch on Customise to add widgets, or ask a question and add a chart from the answer.</p></div>`;
  $("report-foot").textContent = `Source: ${meta.portfolio} NOI dataset, ${monthLabel(meta.months[0])} to ${monthLabel(meta.months[meta.months.length - 1])}. ` +
    `NOI is revenue less operating expenses, before financing, tax and capital expenditure. Prior year is the ${meta.prior_basis || "same period a year earlier"}.` +
    (meta.fictional ? " All properties and figures are fictional." : "");
  drawTrends($("widgets"));
  drawChatTrends();
  if (state.flash) {
    const el = document.getElementById(state.flash);
    state.flash = null;
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

let refreshSeq = 0;
async function refresh() {
  const seq = ++refreshSeq;
  const widgets = layout().map((w) => (w.type === "note" ? { type: "note", text: w.text || " " }
    : { type: w.type, metric: w.metric, dimension: w.dimension, order: w.order, limit: w.limit, scope: w.scope }));
  try {
    const result = await api("/api/noi/render", { view: state.view, widgets });
    if (seq !== refreshSeq) return;
    state.results = result.widgets;
    state.viewInfo = result.view;
    renderBoard();
  } catch (error) { toast(error.message, true); }
}

/* ------------------------------------------------------------------ editing */

function addWidget(widget, { toLayout = layoutKey() } = {}) {
  const w = { id: newId(), size: widget.type === "table" || widget.type === "statement" || widget.type === "kpi" ? "full" : "half", ...widget };
  state.layouts[toLayout].push(w);
  state.flash = w.id;
  save();
  return refresh();
}

function onWidgetClick(event) {
  const open = event.target.closest("[data-open]");
  if (open && !event.target.closest(".wcfg")) { openProperty(open.dataset.open); return; }
  const act = event.target.closest("[data-act]");
  if (!act) return;
  const list = layout(), i = Number(act.closest(".widget").dataset.i), w = list[i];
  if (act.dataset.act === "up" && i > 0) [list[i - 1], list[i]] = [list[i], list[i - 1]];
  if (act.dataset.act === "down" && i < list.length - 1) [list[i + 1], list[i]] = [list[i], list[i + 1]];
  if (act.dataset.act === "size") w.size = w.size === "full" ? "half" : "full";
  if (act.dataset.act === "remove") list.splice(i, 1);
  save();
  refresh();
}
function onWidgetChange(event) {
  const field = event.target.dataset.cfg;
  if (!field) return;
  const w = layout()[Number(event.target.closest(".widget").dataset.i)], value = event.target.value;
  if (field === "scope") {
    const [kind, id] = [value.slice(0, 1), value.slice(2)];
    w.scope = kind === "p" ? { property_id: id } : kind === "s" ? { sector: id } : kind === "r" ? { region: id } : undefined;
  } else if (field === "limit") w.limit = Math.max(0, Math.min(50, Math.round(Number(value) || 0)));
  else if (field === "title") w.title = value.trim() || undefined;
  else w[field] = value;
  save();
  if (field === "text") return;  // keep focus in the textarea; it is re-rendered when editing ends
  refresh();
}

/* ------------------------------------------------------------------ chat */

const STARTERS = ["How are we doing against budget?", "Which properties need attention, and why?",
                  "What is driving operating expenses?", "Where is occupancy weakest?"];

function renderProviders() {
  const providers = state.meta.providers;
  const usable = providers.filter((p) => p.available);
  if (!usable.some((p) => p.id === state.provider)) {
    state.provider = (usable.find((p) => p.execution_mode === "real_ai") || usable[0])?.id || null;
  }
  $("chat-provider").innerHTML = providers.map((p) =>
    `<option value="${esc(p.id)}"${p.id === state.provider ? " selected" : ""}${p.available ? "" : " disabled"}>${esc(p.label)}${p.available ? "" : " (not available)"}</option>`).join("");
  const current = providers.find((p) => p.id === state.provider);
  const missing = providers.find((p) => p.execution_mode === "real_ai" && !p.available);
  $("chat-provider-note").innerHTML = current
    ? `${modeBadge(current.execution_mode, current.model)} ${current.execution_mode === "simulated"
        ? "Scripted replies that recognise a few question types. No AI model is called." + (missing ? ` For real answers: ${esc(missing.unavailable_reason || "")}` : "")
        : "Your question and the portfolio figures are sent to the Anthropic API from the server."}`
    : "No assistant is available.";
}

function suggestionHtml(s, t, i) {
  if (s.kind === "add_chart") {
    return `<div class="suggest"><h4>${esc(s.data.title)}</h4>
      <div class="small muted">${esc(s.data.scope_label)} · actual vs ${esc(s.data.compare_label.toLowerCase())}</div>
      ${renderBody(s.widget, s.data)}
      <button type="button" class="btn tiny" data-turn="${t}" data-s="${i}">${s.added ? "Added ✓ · add again" : "Add to dashboard"}</button></div>`;
  }
  return `<button type="button" class="btn secondary tiny" data-turn="${t}" data-s="${i}">${esc(s.label)}</button>`;
}

function renderChat() {
  const log = $("chat-log");
  if (!state.turns.length) {
    log.innerHTML = `<p class="muted">Ask a question about the portfolio. Answers can be pinned to the dashboard, and any chart an answer suggests is drawn from the data, not from the answer.</p>
      <div class="starter">${STARTERS.map((q) => `<button type="button" data-starter="${esc(q)}">${esc(q)}</button>`).join("")}</div>`;
    return;
  }
  log.innerHTML = state.turns.map((turn, t) => {
    let answer;
    if (turn.pending) answer = `<p class="thinking">Working on it…</p>`;
    else if (turn.error) answer = `<p class="chat-error">${esc(turn.error)}</p>`;
    else {
      const charts = turn.suggestions.map((s, i) => (s.kind === "add_chart" ? suggestionHtml(s, t, i) : "")).join("");
      const links = turn.suggestions.map((s, i) => (s.kind !== "add_chart" ? suggestionHtml(s, t, i) : "")).join("");
      answer = `<div class="note-text">${textHtml(turn.answer)}</div>
        <div class="provenance">${modeBadge(turn.provenance.execution_mode, turn.provenance.model)}</div>
        ${turn.notes.filter((n) => !/^Simulated answer/.test(n)).map((n) => `<p class="small muted">${esc(n)}</p>`).join("")}
        <div class="a-actions"><button type="button" class="btn secondary tiny" data-turn="${t}" data-pin="1">${turn.pinned ? "Pinned ✓ · pin again" : "Pin answer to dashboard"}</button>${links}</div>${charts}`;
    }
    return `<div class="q">${esc(turn.question)}</div><div class="a">${answer}</div>`;
  }).join("");
  drawChatTrends();
  log.scrollTop = log.scrollHeight;
}
function drawChatTrends() { drawTrends($("chat-log")); }

async function ask(question) {
  question = question.trim();
  if (!question || state.turns.some((t) => t.pending)) return;
  if (!state.provider) { toast("No assistant is available.", true); return; }
  const history = state.turns.filter((t) => t.answer).map((t) => ({ question: t.question, answer: t.answer }));
  const turn = { question, pending: true };
  state.turns.push(turn);
  $("chat-send").disabled = true;
  renderChat();
  try {
    const result = await api("/api/noi/ask", { question, provider: state.provider, view: state.view, history });
    Object.assign(turn, { pending: false, answer: result.answer, suggestions: result.suggestions, notes: result.notes,
                          provenance: result.provenance, answered_at: result.answered_at });
  } catch (error) {
    Object.assign(turn, { pending: false, error: error.message });
  }
  $("chat-send").disabled = false;
  // Chart previews hold keys into trendStore, which the board clears on redraw; redraw both together.
  renderBoard();
  renderChat();
}

function onChatClick(event) {
  const starter = event.target.closest("[data-starter]");
  if (starter) { ask(starter.dataset.starter); return; }
  const open = event.target.closest("[data-open]");
  if (open) { openProperty(open.dataset.open); return; }
  const button = event.target.closest("[data-turn]");
  if (!button) return;
  const turn = state.turns[Number(button.dataset.turn)];
  const ai = { mode: turn.provenance.execution_mode, model: turn.provenance.model, at: turn.answered_at, question: turn.question };
  if (button.dataset.pin) {
    turn.pinned = true;
    addWidget({ type: "note", text: turn.answer, ai }).then(renderChat);
    toast("Answer pinned to the dashboard.");
    return;
  }
  const s = turn.suggestions[Number(button.dataset.s)];
  if (s.kind === "add_chart") {
    s.added = true;
    addWidget({ ...s.widget, ai }).then(renderChat);
    toast("Chart added to the dashboard.");
  } else if (s.kind === "open_property") openProperty(s.property_id).then(renderChat);
  else if (s.kind === "set_view") setView({ ...s.view, property_id: null }, { keepProperty: true }).then(renderChat);
}

/* ------------------------------------------------------------------ tooltip */

function showTip(html, event) {
  const tip = $("tip");
  tip.innerHTML = html;
  tip.hidden = false;
  const pad = 14, box = tip.getBoundingClientRect();
  tip.style.left = Math.min(window.innerWidth - box.width - 8, event.clientX + pad) + "px";
  tip.style.top = Math.max(8, Math.min(window.innerHeight - box.height - 8, event.clientY + pad)) + "px";
}
function hideTip() { $("tip").hidden = true; }

/* ------------------------------------------------------------------ start */

function setChatOpen(open) {
  state.chatOpen = open;
  $("chat").hidden = !open;
  $("shell").classList.toggle("with-chat", open);
  $("btn-chat").setAttribute("aria-pressed", String(open));
  save();
  drawTrends();
}

function wire() {
  $("f-from").addEventListener("change", (e) => setView({ from: e.target.value }));
  $("f-to").addEventListener("change", (e) => setView({ to: e.target.value }));
  $("f-presets").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView({ from: b.dataset.from, to: b.dataset.to }); });
  $("f-compare").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView({ compare: b.dataset.compare }); });
  const toggle = (list, name) => (list.includes(name) ? list.filter((n) => n !== name) : [...list, name]);
  $("f-sectors").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView({ sectors: toggle(state.view.sectors, b.dataset.sector) }); });
  $("f-regions").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView({ regions: toggle(state.view.regions, b.dataset.region) }); });
  $("f-property").addEventListener("change", (e) => openProperty(e.target.value));
  $("crumbs").addEventListener("click", (e) => { const b = e.target.closest("[data-open]"); if (b) openProperty(b.dataset.open); });

  $("btn-edit").addEventListener("click", () => {
    state.editing = !state.editing;
    $("btn-edit").setAttribute("aria-pressed", String(state.editing));
    $("btn-edit").textContent = state.editing ? "Done customising" : "Customise";
    renderBoard();
    renderChat();
  });
  $("btn-print").addEventListener("click", () => {
    if (state.editing) $("btn-edit").click();
    window.print();
  });
  $("btn-chat").addEventListener("click", () => setChatOpen(!state.chatOpen));
  $("report-title").addEventListener("blur", () => { state.title = $("report-title").textContent.trim().slice(0, 120); save(); });
  $("report-title").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.target.blur(); } });
  $("edit-bar").addEventListener("click", (e) => {
    if (e.target.id === "add-widget") {
      const type = $("add-type").value;
      addWidget(type === "note" ? { type, text: "" } : type === "trend" ? { type, metric: "noi" }
        : type === "rank" ? { type, dimension: state.view.property_id ? "opex_line" : "property", metric: "noi", order: "desc" }
        : type === "bridge" ? { type, dimension: state.view.property_id ? "line" : "property" } : { type });
    }
    if (e.target.id === "reset-layout") { state.layouts[layoutKey()] = defaultLayouts()[layoutKey()]; save(); refresh(); }
  });
  $("widgets").addEventListener("click", onWidgetClick);
  $("widgets").addEventListener("change", onWidgetChange);

  $("chat-provider").addEventListener("change", (e) => { state.provider = e.target.value; save(); renderProviders(); });
  $("chat-clear").addEventListener("click", () => { state.turns = state.turns.filter((t) => t.pending); renderChat(); });
  $("chat-log").addEventListener("click", onChatClick);
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); const input = $("chat-input"); ask(input.value); input.value = ""; });
  $("chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chat-form").requestSubmit(); } });

  document.addEventListener("mouseover", (e) => { const el = e.target.closest?.("[data-tip]"); if (el) showTip(el.dataset.tip, e); });
  document.addEventListener("mousemove", (e) => { const el = e.target.closest?.("[data-tip]"); if (el) showTip(el.dataset.tip, e); });
  document.addEventListener("mouseout", (e) => { if (e.target.closest?.("[data-tip]")) hideTip(); });
  let resizeTimer;
  new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => drawTrends(), 120); }).observe($("widgets"));
  window.addEventListener("hashchange", () => {
    const id = (location.hash.match(/^#property-(.+)$/) || [])[1] || null;
    if ((id || null) !== state.view.property_id && (!id || property(id))) openProperty(id);
  });
}

async function start() {
  try {
    state.meta = await api("/api/noi/meta");
  } catch (error) { toast(error.message, true); return; }
  load();
  const hashId = (location.hash.match(/^#property-(.+)$/) || [])[1];
  if (hashId && property(hashId)) state.view.property_id = hashId;
  wire();
  renderFilters();
  renderProviders();
  setChatOpen(state.chatOpen);
  renderChat();
  await refresh();
}
start();
