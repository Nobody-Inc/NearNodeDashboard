// Near Node Dashboard: page core (formatting, charts, header, alerts, polling, settings).
// The modules' parts of the page are in js/system.js and js/near.js; js/start.js starts it.
"use strict";

// ---------------------------------------------------------------- formatting
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const isNum = v => typeof v === "number" && isFinite(v);
function fmtBytes(v, d = 1) {
  if (!isNum(v)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0, x = Math.abs(v);
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return (v < 0 ? "-" : "") + x.toFixed(i === 0 ? 0 : d) + " " + u[i];
}
// Axis labels for byte values: one unit for the whole axis, with just enough decimals that
// neighbouring gridlines never show the same label (e.g. 2.04 / 2.06 / 2.08 TB, not 2 / 2 / 2 TB).
function fmtBytesAxis(v, ticks) {
  if (!isNum(v)) return "—";
  const vals = (ticks || []).map(t => t.value).filter(isNum);
  const top = Math.max(1, ...vals.map(Math.abs), Math.abs(v));
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0, div = 1;
  while (top / div >= 1024 && i < u.length - 1) { div *= 1024; i++; }
  const step = vals.length > 1 ? Math.abs(vals[1] - vals[0]) / div : 1;
  const d = !step || step >= 1 ? 0 : Math.min(3, Math.ceil(-Math.log10(step) - 1e-9));
  return (v / div).toFixed(d) + " " + u[i];
}
const fmtRate = v => isNum(v) ? fmtBytes(v) + "/s" : "—";
const fmtPct = (v, d = 1) => isNum(v) ? v.toFixed(d) + "%" : "—";
function fmtNum(v, d = 0) {
  if (!isNum(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
  return v.toLocaleString(undefined, { maximumFractionDigits: d });
}
const fmtCount = v => isNum(v) ? v.toLocaleString() : "—";
const fmtNear = v => isNum(v) ? fmtNum(v, 0) + " Ⓝ" : "—";
function fmtDur(s) {
  if (!isNum(s)) return "—";
  if (s < 60) return s.toFixed(s < 10 ? 1 : 0) + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return (s / 3600).toFixed(1) + "h";
  return (s / 86400).toFixed(1) + "d";
}
function fmtUptime(s) {
  if (!isNum(s)) return "—";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
// Reboots = changes in boot time across the series (tolerate small clock adjustments).
function rebootsIn(boots) {
  const b = (boots || []).filter(isNum);
  let n = 0;
  for (let i = 1; i < b.length; i++) if (Math.abs(b[i] - b[i - 1]) > 120) n++;
  return n;
}
function fmtTime(ms, range) {
  const d = new Date(ms);
  const opts = range > 86400 ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
             : range > 1800 ? { hour: "2-digit", minute: "2-digit" }
             : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return d.toLocaleString([], opts);
}
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------------------------------------------------------------- trends
function stats(xs, ys) {
  const pts = [];
  for (let i = 0; i < ys.length; i++) if (isNum(ys[i])) pts.push([xs[i], ys[i]]);
  if (!pts.length) return null;
  let min = Infinity, max = -Infinity, sum = 0;
  for (const [, y] of pts) { min = Math.min(min, y); max = Math.max(max, y); sum += y; }
  const n = pts.length, avg = sum / n;
  let slope = null; // units per hour, least squares
  // Only trust a trend once the data spans enough time (≥5 min and ≥10% of the selected range).
  const span = pts[n - 1][0] - pts[0][0];
  if (n >= 5 && span >= Math.max(300, rangeSeconds() * 0.1)) {
    const mx = pts.reduce((a, p) => a + p[0], 0) / n;
    let num = 0, den = 0;
    for (const [x, y] of pts) { num += (x - mx) * (y - avg); den += (x - mx) ** 2; }
    if (den > 0) slope = num / den * 3600;
  }
  return { min, max, avg, slope, last: pts[n - 1][1], first: pts[0][1], n };
}

// ---------------------------------------------------------------- charts
const crosshair = {
  id: "crosshair",
  afterDraw(chart) {
    const a = chart.tooltip?.getActiveElements?.();
    if (!a || !a.length) return;
    const x = a[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
    ctx.save(); ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
  }
};

// Dashed vertical line, labelled "epoch N", wherever a new epoch started in the range.
let epochMarks = [];
const epochLines = {
  id: "epochLines",
  afterDatasetsDraw(chart) {
    if (!epochMarks.length) return;
    const { ctx, chartArea: a, scales: { x } } = chart;
    ctx.save();
    ctx.strokeStyle = css("--muted"); ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
    ctx.fillStyle = css("--text-2"); ctx.font = '10px system-ui, -apple-system, "Segoe UI", sans-serif';
    for (const m of epochMarks) {
      const px = x.getPixelForValue(m.ts * 1000);
      if (!isFinite(px) || px < a.left || px > a.right) continue;
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
      const label = m.epoch ? `epoch ${m.epoch}` : "new epoch", w = ctx.measureText(label).width;
      ctx.fillText(label, px + 4 + w > a.right ? px - 4 - w : px + 4, a.top + 10);   // keep the label inside
    }
    ctx.restore();
  }
};

const CHARTS = [];          // charts that have been built
const PENDING_CARDS = [];   // declared by the module scripts; built once their module is known to run
/**
 * def: { id, group, title, sub?, src, series:[{key,label,color,scale?}], fmt, axisFmt?, yMin?, yMax?,
 *        headline?(latest values) , trend?: (stats, def) => html, extra?: (ctx) => html }
 */
function card(def) {   // declare a chart; buildCards() creates it if its module runs
  PENDING_CARDS.push(def);
  return def;
}

// Create the charts of running modules. A module that isn't running never gets its charts built.
function buildCards() {
  let built = false;
  for (let i = 0; i < PENDING_CARDS.length;) {
    const def = PENDING_CARDS[i];
    if (moduleOn(sectionModule(chartSection(def.group))?.name)) {
      buildCard(def);
      PENDING_CARDS.splice(i, 1);
      built = true;
    } else i++;
  }
  if (built) themeCharts();
}

function buildCard(def) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="card-h">
      <div><div class="card-t">${esc(def.title)}</div><div class="card-s" data-sub>${esc(def.sub || "")}</div></div>
      <div class="card-now" data-now>—</div>
    </div>
    ${def.series.length > 1 ? `<div class="legend" data-legend></div>` : ""}
    <div class="trend" data-trend></div>
    <div class="chart-box"><canvas aria-label="${esc(def.title)} chart" role="img"></canvas><div class="empty" data-empty>Waiting for data…</div></div>`;
  document.getElementById("g-" + def.group).appendChild(el);
  def.el = el;
  def.chart = new Chart(el.querySelector("canvas"), {
    type: "line",
    data: { datasets: def.series.map(s => ({ label: s.label, data: [], parsing: false, spanGaps: false,
      borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, pointHitRadius: 8,
      tension: def.stepped ? 0 : 0.15, stepped: def.stepped || false,
      fill: def.series.length === 1 ? "origin" : false })) },
    options: {
      animation: false, maintainAspectRatio: false, normalized: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => items.length ? new Date(items[0].parsed.x).toLocaleString() : "",
            label: it => ` ${it.dataset.label}: ${def.fmt(it.parsed.y)}`,
          },
        },
      },
      scales: {
        x: { type: "linear", grid: { display: false }, border: {}, ticks: { maxTicksLimit: 6, maxRotation: 0 } },
        y: { min: def.yMin, max: def.yMax, beginAtZero: def.beginAtZero !== false, grace: def.grace ?? "5%",
             border: { display: false },
             ticks: { maxTicksLimit: 5, stepSize: def.stepSize, callback: (v, i, ticks) => (def.axisFmt || def.fmt)(v, ticks) } },
      },
    },
    plugins: [crosshair, epochLines],
  });
  CHARTS.push(def);
}

function themeCharts() {
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.font.size = 11;
  for (const def of CHARTS) {
    const o = def.chart.options;
    o.scales.x.ticks.color = o.scales.y.ticks.color = css("--muted");
    o.scales.y.grid = { color: css("--grid"), drawTicks: false };
    o.scales.x.border.color = css("--axis");
    o.plugins.tooltip.backgroundColor = css("--surface");
    o.plugins.tooltip.titleColor = css("--text");
    o.plugins.tooltip.bodyColor = css("--text-2");
    o.plugins.tooltip.borderColor = css("--border");
    o.plugins.tooltip.borderWidth = 1;
    def.chart.data.datasets.forEach((ds, i) => {
      const c = css(def.series[i].color);
      ds.borderColor = c; ds.pointBackgroundColor = c; ds.pointBorderColor = css("--surface");
      ds.backgroundColor = c + "1f";
    });
    def.chart.update("none");
  }
}

// Insert null points where samples are missing so lines break across outages.
function toPoints(ts, ys, bucket, scale = 1) {
  const out = [];
  for (let i = 0; i < ts.length; i++) {
    if (i > 0 && ts[i] - ts[i - 1] > bucket * 2.5) out.push({ x: (ts[i - 1] + bucket) * 1000, y: null });
    out.push({ x: ts[i] * 1000, y: isNum(ys[i]) ? ys[i] * scale : null });
  }
  return out;
}

function trendHtml(s, def, opts = {}) {
  if (!s) return "";
  const f = def.fmt;
  let parts = [`avg ${f(s.avg)}`, `min ${f(s.min)}`, `max ${f(s.max)}`];
  if (s.slope !== null && opts.slope !== false) {
    const rel = Math.abs(s.slope) / Math.max(Math.abs(s.avg), 1e-9);
    const flat = rel < 0.005;
    const arrow = flat ? "→" : s.slope > 0 ? "↑" : "↓";
    parts.push(`<span class="${flat ? "" : s.slope > 0 ? "up" : "down"}">${arrow} ${flat ? "flat" : (s.slope > 0 ? "+" : "−") + f(Math.abs(s.slope)) + "/h"}</span>`);
  }
  if (opts.extra) parts.push(opts.extra);
  return parts.map(p => `<span>${p}</span>`).join("");
}

function renderCharts(data) {
  const range = data.range;
  epochMarks = data.epochs || [];
  for (const def of CHARTS) {
    if (!sectionOpen(chartSection(def.group))) continue;   // redrawn when the section is opened
    const src = data[def.src];
    if (!src) continue;                                    // its module isn't running
    const ts = src.t;
    let any = false;
    def.chart.data.datasets.forEach((ds, i) => {
      const s = def.series[i];
      ds.data = toPoints(ts, src[s.key], src.bucket || data.bucket, s.scale || 1);
      if (ds.data.some(p => p.y !== null)) any = true;
    });
    def.chart.options.scales.x.min = (data.now - range) * 1000;
    def.chart.options.scales.x.max = data.now * 1000;
    def.chart.options.scales.x.ticks.callback = v => fmtTime(v, range);
    def.chart.update("none");
    def.el.querySelector("[data-empty]").style.display = any ? "none" : "flex";

    const st = def.series.map(s => stats(ts, (src[s.key] || []).map(v => isNum(v) ? v * (s.scale || 1) : v)));
    const now = def.headline ? def.headline(st, src) : (st[0] ? def.fmt(st[0].last) : "—");
    def.el.querySelector("[data-now]").innerHTML = now;
    const leg = def.el.querySelector("[data-legend]");
    if (leg) leg.innerHTML = def.series.map((s, i) =>
      `<span><i style="background:var(${s.color})"></i>${esc(s.label)} <b>${st[i] ? def.fmt(st[i].last) : "—"}</b></span>`).join("");
    def.el.querySelector("[data-trend]").innerHTML = def.trend ? def.trend(st, def, src) : trendHtml(st[0], def);
    if (def.subFn) def.el.querySelector("[data-sub]").textContent = def.subFn(src) || def.sub || "";
  }
}

const lastOf = (arr) => { if (!arr) return null; for (let i = arr.length - 1; i >= 0; i--) if (isNum(arr[i])) return arr[i]; return null; };

// ---------------------------------------------------------------- tiles / header

let config = null, latest = null, lastData = null;

function tile(k, v, d = "", barPct = null) {
  return `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${v}</div>` +
    (d ? `<div class="d" title="${d.replace(/<[^>]*>/g, "")}">${d}</div>` : "") +
    (barPct !== null ? `<div class="bar"><i style="width:${Math.max(0, Math.min(100, barPct))}%"></i></div>` : "") + `</div>`;
}
function statusPill(color, label) {
  return `<span class="dot" style="background:var(${color})"></span>${esc(label)}`;
}

// Node name: the validator account if there is one (configured or reported by the node),
// otherwise the server's hostname. Shown in the heading and first in the browser tab title.
function renderNodeName(L) {
  const name = L.validator_account_id || L.server_hostname || "";
  const el = document.getElementById("nodeName");
  el.textContent = name;
  el.hidden = !name;
  el.title = L.validator_account_id && L.server_hostname ? `server: ${L.server_hostname}` : "";
  const title = name ? `${name} · Near Node Dashboard` : "Near Node Dashboard";
  if (document.title !== title) document.title = title;
}

// Footer: app version and the commit this install came from (links to it on GitHub).
function renderVersion(v) {
  const el = document.getElementById("appVersion");
  if (!v || !v.version || el.dataset.shown === JSON.stringify(v)) return;
  el.dataset.shown = JSON.stringify(v);
  const commit = v.commit && /^[0-9a-f]{4,40}$/.test(v.commit)
    ? ` · <a href="${esc(v.repo)}/commit/${esc(v.commit)}" target="_blank" rel="noopener">${esc(v.commit)}</a>` +
      (v.date ? ` (${esc(v.date)})` : "") + (v.modified ? " + local changes" : "")
    : "";
  const past = isNum(v.distance) && v.distance > 0
    ? `<span title="${esc(v.distance)} commit(s) after the v${esc(v.version)} release">+${esc(v.distance)}</span>` : "";
  el.innerHTML = `v${esc(v.version)}${past}${commit}`;
  document.getElementById("appVersionSep").hidden = false;
}


function renderHeader() {
  const L = latest || {}, mods = activeModules();
  renderNodeName(L);
  renderVersion(L.app_version);
  if (config && typeof L.read_only === "boolean" && L.read_only !== !!config.read_only) {
    config.read_only = L.read_only;   // server restarted with read_only changed
    applyReadOnly();
  }
  for (const m of mods) m.header?.(L);

  const sub = mods.flatMap(m => m.headerSub?.(L) || []);
  if (config) sub.push(`collecting every ${config.fast_mode ? 1 : config.interval_seconds}s · keep ${config.retention_hours}h`);
  sub.push('<span id="updatedInfo"></span>');
  document.getElementById("sub").innerHTML = sub.map(x => `<span>${x}</span>`).join("");
  updateCountdown();

  const banner = document.getElementById("banner"), lines = mods.flatMap(m => m.banner?.(L) || []);
  banner.innerHTML = lines.join("<br>");
  banner.classList.toggle("show", lines.length > 0);

  document.getElementById("tiles").innerHTML = [...mods].sort((a, b) => (a.tileOrder || 0) - (b.tileOrder || 0))
    .flatMap(m => m.tiles?.(L, lastData || {}) || []).join("");
}

// ---------------------------------------------------------------- page modules
// Each module script (js/system.js, js/near.js) describes its part of the page with
// registerModule({...}); the core composes them:
//   name            the server module it belongs to ("system", "near")
//   sections        {data-sec key of a <section>: collect_* setting that switches it}
//   chartSections   {chart group (g-<group>): the section it lives in}, for groups not named after a section
//   tileOrder       position of its header tiles (lower first)
//   tiles(L, data)  header tiles (HTML)       headerSub(L)  items for the line under the title (HTML)
//   banner(L)       warning lines (HTML)       header(L)     anything else in the header
//   afterCore()     after each /api/latest + /api/metrics refresh
//   polls           {name: {every: seconds, wanted(), fetch(), apply(result)}}: its own endpoints
//   onSectionOpen(key), onTheme()
// Sections, header items and settings fields marked data-module="<name>" are hidden while that
// module isn't running on the server (config.json "modules").
const PAGE_MODULES = [];
function registerModule(m) { PAGE_MODULES.push(m); }
function moduleOn(name) {   // before the server has said, assume every module runs
  const on = latest?.modules || config?.modules;
  return !name || !on || on.includes(name);
}
const activeModules = () => PAGE_MODULES.filter(m => moduleOn(m.name));
const sectionModule = key => PAGE_MODULES.find(m => key in (m.sections || {}));
function chartSection(group) {
  for (const m of PAGE_MODULES) if (m.chartSections?.[group]) return m.chartSections[group];
  return group;
}
// When the shown data was sampled: the NEAR sample time, or the latest sample of any module.
const sampleTime = () => latest?.time ?? latest?.sampled_at;

// ---------------------------------------------------------------- alerts and shared status helpers

const SEV = { critical: ["--critical", "✕"], warning: ["--warning", "⚠"], info: ["--s1", "ℹ"] };
const STATUS = { pass: ["--good", "✓", "OK"], warn: ["--warning", "⚠", "Warning"], fail: ["--critical", "✕", "Failing"], na: ["--muted", "–", "N/A"] };
const badge = st => { const [c, i, l] = STATUS[st] || STATUS.na; return `<span class="lvl"><i style="color:var(${c})">${i}</i>${l}</span>`; };
const meter = pct => !isNum(pct) ? `<span class="muted">—</span>` :
  `<span class="meter ${pct >= 90 ? "crit" : pct >= 80 ? "warn" : ""}"><i style="width:${Math.min(100, pct)}%"></i></span>${fmtPct(pct, 0)}`;

function renderAlerts(alerts) {
  const list = document.getElementById("alertList");
  if (!alerts) return;
  if (!alerts.length) { list.innerHTML = `<div class="alerts-ok"><span style="color:var(--good)">✓</span> No active alerts</div>`; return; }
  const now = Date.now() / 1000;
  list.innerHTML = alerts.map(a => {
    const [c, icon] = SEV[a.severity] || SEV.info;
    return `<div class="alert-row ${esc(a.severity)}"><span style="color:var(${c})">${icon}</span>
      <div><span class="t">${esc(a.title)}</span> <span class="d">${esc(a.detail)}</span></div>
      <span class="since" title="${esc(new Date(a.since * 1000).toLocaleString())}">for ${fmtDur(Math.max(0, now - a.since))}</span></div>`;
  }).join("");
}

// ---------------------------------------------------------------- data loop
let range = 3600, timer = null;
try { const r = localStorage.getItem("nvs-range"); if (r) range = r === "all" ? "all" : +r; } catch {}

function rangeSeconds() { return range === "all" ? (config?.retention_hours || 24) * 3600 : range; }

async function getJSON(url) {
  const r = await fetch(url);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `${url} returned HTTP ${r.status}`);
  return body;
}
// Polling. Each kind of data is fetched only as often as it can change, only for sections that
// are open and collecting, and not at all while the tab is hidden. A cheap 1-second tick decides
// what's due; it makes no request unless something is.
// core = charts + header (/api/metrics, /api/latest); each module adds its own polls
// ({name: {every (seconds), wanted(), fetch(), apply(result)}}), e.g. peers, logs, health.
const lastFetch = { core: 0 };
const modulePolls = () => Object.assign({}, ...activeModules().map(m => m.polls || {}));
const collectionInterval = () => config?.fast_mode ? 1 : (config?.interval_seconds || 60);
// When the charts and header are next fetched. In normal mode that's just after the server's next
// sample is due, so new data shows up within a couple of seconds without extra requests; when chart
// buckets are longer than the collection interval (e.g. 1s mode on the 24h view), it's the bucket cadence.
function nextCoreAt() {
  if (!lastFetch.core) return 0;   // invalidated (range change, tab shown again, settings saved): fetch now
  const every = coreEvery(), iv = collectionInterval(), t = sampleTime();
  if (every <= iv && isNum(t)) {
    const next = t + iv + 2;                  // just after the next sample
    if (next > lastFetch.core) return next;   // that sample is still to come
    return lastFetch.core + Math.min(every, 15);   // sample is late: check again soon
  }
  return lastFetch.core + every;
}
// Header: when the shown data was collected, and the countdown to the next update.
function updateCountdown() {
  const el = document.getElementById("updatedInfo");
  if (!el) return;
  const now = Date.now() / 1000, t = sampleTime();
  if (!isNum(t) || !lastFetch.core) { el.textContent = ""; return; }
  const left = Math.round(nextCoreAt() - now);
  el.textContent = `last updated ${new Date(t * 1000).toLocaleTimeString()} (${fmtDur(Math.max(0, now - t))} ago)` +
    (document.hidden ? "" : ` · next update ${left > 0 ? "in " + (left < 60 ? left + "s" : fmtDur(left)) : "now"}`);
}

function coreEvery() {
  // New samples arrive once per collection interval, and a chart point covers one bucket
  // (e.g. 144 s on the 24h view), so polling faster than the longer of the two changes nothing.
  const iv = config?.fast_mode ? 1 : (config?.interval_seconds || 60);
  return Math.min(60, Math.max(1, iv, lastData?.bucket || 0));
}
const sectionOpen = key => !collapsed.includes(key);
function invalidate(groups) {   // make these groups due now, then poll
  for (const g of groups) lastFetch[g] = 0;
  refresh();
}
const invalidateAll = () => invalidate(["core", ...Object.keys(modulePolls())]);

let refreshing = false, refreshQueued = false;
async function refresh() {
  if (refreshing) { refreshQueued = true; return; }
  if (document.hidden) return;
  const now = Date.now() / 1000, polls = modulePolls();
  const due = g => g === "core" ? now >= nextCoreAt() - 0.25
    : polls[g].wanted() && now - (lastFetch[g] || 0) >= polls[g].every - 0.25;
  const jobs = {};
  if (due("core")) {
    jobs.metrics = getJSON(`/api/metrics?range=${rangeSeconds()}&points=600`);
    jobs.latest = getJSON("/api/latest");
  }
  for (const g in polls) if (due(g)) jobs["poll:" + g] = polls[g].fetch();
  const names = Object.keys(jobs);
  if (!names.length) return;
  refreshing = true;
  try {
    const results = Object.fromEntries(names.map((n, i) => [n, null]));
    (await Promise.all(Object.values(jobs))).forEach((r, i) => { results[names[i]] = r; });
    const t = Date.now() / 1000;
    if (jobs.metrics) { lastData = results.metrics; latest = results.latest; lastFetch.core = t; }
    for (const g in polls) if (jobs["poll:" + g]) lastFetch[g] = t;
    if (jobs.metrics) {
      applyModules();   // the server may have restarted with a different module list
      renderCharts(lastData);
      renderHeader();
      renderAlerts(latest.alerts);
      for (const m of activeModules()) m.afterCore?.();
    }
    for (const g in polls) if (jobs["poll:" + g]) polls[g].apply(results["poll:" + g]);
  } catch (e) {
    const b = document.getElementById("banner");
    b.textContent = "Cannot reach the stats server: " + e.message; b.classList.add("show");
  } finally {
    refreshing = false;
    if (refreshQueued) { refreshQueued = false; refresh(); }
  }
}

function schedule() {
  if (!timer) timer = setInterval(() => { updateCountdown(); refresh(); }, 1000);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) invalidate(["core"]);   // catch up straight away when the tab is shown again
});

async function loadConfig() {
  config = await getJSON("/api/config");
  document.getElementById("fast").checked = !!config.fast_mode;
  applyModules();
  applyReadOnly();
}

async function saveConfig(patch) {
  const r = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) });
  const body = await r.json();
  if (r.status === 403 && body.read_only) {   // read-only mode was turned on since the page loaded
    await loadConfig().catch(() => {});
  }
  if (!r.ok) throw new Error(body.error || "Save failed");
  config = body;
  document.getElementById("fast").checked = !!config.fast_mode;
  applyModules();
  applyReadOnly();
  schedule();
  setTimeout(invalidateAll, 1500);
}


// ---------------------------------------------------------------- read-only mode
// read_only can only be set in config.json on the server; the server refuses every settings
// change while it's on. The page mirrors that: warning bar, view-only dialog, locked 1s toggle.
function applyReadOnly() {
  const ro = !!config?.read_only;
  document.getElementById("readOnlyNotice").hidden = !ro;
  document.getElementById("settingsReadOnly").hidden = !ro;
  const fast = document.getElementById("fast");
  fast.disabled = ro;
  fast.closest(".toggle").classList.toggle("disabled", ro);
  fast.closest(".toggle").title = ro ? "Read-only mode: can't be changed from this page"
                                     : "Collect every second instead of the configured interval";
  const btn = document.getElementById("openSettings");
  btn.innerHTML = ro ? 'Settings<span class="lock" aria-label="read-only">🔒</span>' : "Settings";
  btn.title = ro ? "Settings are read-only (view only)" : "";
  for (const el of form.elements) {
    if (el.name) el.disabled = ro;
  }
  document.getElementById("saveSettings").hidden = ro;
  document.getElementById("cancelSettings").textContent = ro ? "Close" : "Cancel";
}

// ---------------------------------------------------------------- UI wiring
function markRange() {
  document.querySelectorAll("#ranges button").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.r === String(range))));
}
document.getElementById("ranges").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  range = b.dataset.r === "all" ? "all" : +b.dataset.r;
  try { localStorage.setItem("nvs-range", String(range)); } catch {}
  markRange(); invalidateAll();
});
document.getElementById("fast").addEventListener("change", async e => {
  try { await saveConfig({ fast_mode: e.target.checked }); }
  catch (err) { alert(err.message); e.target.checked = !e.target.checked; }
});

const dlg = document.getElementById("settings"), form = document.getElementById("settingsForm");
document.getElementById("openSettings").addEventListener("click", async () => {
  if (!config) {
    try { await loadConfig(); }
    catch (e) { alert("Can't load settings from the server: " + e.message); return; }
  }
  for (const el of form.elements) {
    if (!el.name || !(el.name in config)) continue;
    if (el.type === "checkbox") el.checked = !!config[el.name]; else el.value = config[el.name];
  }
  document.getElementById("settingsErr").textContent = "";
  applyReadOnly();
  dlg.showModal();
  // In read-only mode every field is disabled, so the browser would focus (and scroll to) the
  // Close button at the bottom; show the read-only warning at the top instead.
  dlg.scrollTop = 0;
  if (config?.read_only) document.getElementById("settingsReadOnly").focus({ preventScroll: true });
});
document.getElementById("saveSettings").addEventListener("click", async e => {
  e.preventDefault();
  if (!form.reportValidity()) return;
  const patch = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    patch[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
  }
  try { await saveConfig(patch); dlg.close(); }
  catch (err) { document.getElementById("settingsErr").textContent = err.message; }
});

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", themeCharts);


// ---------------------------------------------------------------- collapsible sections
let collapsed = [];
try { collapsed = JSON.parse(localStorage.getItem("nvs-collapsed")) || []; } catch {}
function applySections() {
  document.querySelectorAll(".sec").forEach(sec => {
    const key = sec.dataset.sec, isCollapsed = collapsed.includes(key);
    sec.classList.toggle("collapsed", isCollapsed);
    sec.querySelector(".sec-head").setAttribute("aria-expanded", String(!isCollapsed));
    sec.hidden = !moduleOn(sectionModule(key)?.name);
    const off = sec.querySelector(".sec-off");
    const enabled = !config || config[sectionModule(key)?.sections[key]] !== false;
    off.hidden = enabled;
    off.title = enabled ? "" : "Not collecting new data. Turn it on in Settings → Data collection. Existing data is still shown.";
  });
}
document.querySelectorAll(".sec-head").forEach(btn => btn.addEventListener("click", () => {
  const key = btn.closest(".sec").dataset.sec;
  collapsed = collapsed.includes(key) ? collapsed.filter(k => k !== key) : [...collapsed, key];
  try { localStorage.setItem("nvs-collapsed", JSON.stringify(collapsed)); } catch {}
  applySections();
  if (sectionOpen(key)) {
    if (lastData) renderCharts(lastData);   // charts in collapsed sections are skipped while closed
    for (const m of activeModules()) m.onSectionOpen?.(key);
    refresh();
  }
}));

// Hide the sections, header items and settings of modules the server isn't running.
function applyModules() {
  document.querySelectorAll("[data-module]").forEach(el => { el.hidden = !moduleOn(el.dataset.module); });
  if (config || latest) buildCards();   // once the server has said which modules run
  applySections();
}

function currentTheme() { return document.documentElement.dataset.theme || "system"; }
function markTheme() {
  document.querySelectorAll("#theme button").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.theme === currentTheme())));
}
document.getElementById("theme").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  const t = b.dataset.theme;
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  try { t === "system" ? localStorage.removeItem("nvs-theme") : localStorage.setItem("nvs-theme", t); } catch {}
  markTheme();
  themeCharts();      // chart colours are read from CSS variables, so re-apply them
  for (const m of activeModules()) m.onTheme?.();
});
markTheme();
