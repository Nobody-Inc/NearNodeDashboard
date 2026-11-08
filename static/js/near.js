// Near Node Dashboard: the "near" module's part of the page (node, validator and peer charts,
// peers table, host checks, service log, same-shard validators). Registered with the core in js/core.js.
"use strict";

// Each bucket's value is the share of samples in it that were synced (0–100%).
function syncRuns(ts, ys, bucket) {
  let drops = 0, longest = 0, run = 0, total = 0, synced = 0;
  for (let i = 0; i < ys.length; i++) {
    if (!isNum(ys[i])) continue;
    total++; synced += ys[i];
    if (ys[i] < 99.95) { if (run === 0) drops++; run += bucket * (1 - ys[i] / 100); longest = Math.max(longest, run); }
    else run = 0;
  }
  return { drops, longest, pct: total ? synced / total : null };
}
function syncState() {
  const L = latest || {}, lag = lastOf(lastData?.near?.block_lag_sec);
  if (L.error) return ["--critical", "✕ RPC unreachable"];
  if (L.syncing) return ["--warning", "⟳ Syncing"];
  if (isNum(lag) && lag > SYNC_LAG_LIMIT) return ["--serious", "! Behind " + fmtDur(lag)];
  if (L.block_height) return ["--good", "✓ Synced"];
  return null;
}
const SYNC_LAG_LIMIT = 30; // keep in step with SYNC_LAG_THRESHOLD_SEC in dashboard/modules/near/collector.py
card({ id: "sync", group: "node", title: "Sync status",
  sub: `Share of samples where the node was synced (RPC up, not syncing, lag ≤ ${SYNC_LAG_LIMIT}s)`, src: "near",
  fmt: v => fmtPct(v), axisFmt: v => v + "%", yMax: 100, stepped: true,
  series: [{ key: "synced", label: "Synced", color: "--s1", scale: 100 }],
  headline: () => { const s = syncState(); return s ? `<span style="display:inline-flex;align-items:center;gap:6px;font-size:15px"><span class="dot" style="background:var(${s[0]})"></span>${esc(s[1])}</span>` : "—"; },
  trend: (st, def, src) => {
    const r = syncRuns(src.t, (src.synced || []).map(v => isNum(v) ? v * 100 : v), lastData.bucket);
    if (r.pct === null) return "";
    return `<span>synced <b>${fmtPct(r.pct, 2)}</b> of range</span>` +
      `<span>${r.drops} ${r.drops === 1 ? "drop" : "drops"}</span>` +
      (r.drops ? `<span>longest ~${fmtDur(r.longest)}</span>` : "");
  } });
card({ id: "logs", group: "node", title: "Service log warnings & errors", sub: "neard WARN / ERROR lines per time bucket",
  src: "logs", fmt: v => isNum(v) ? fmtNum(v, 0) : "—", stepped: true,
  series: [{ key: "warn", label: "⚠ Warnings", color: "--warning" }, { key: "error", label: "✕ Errors", color: "--critical" }],
  headline: () => logs ? `${fmtNum(logs.counts.WARN + logs.counts.ERROR)} <span style="font-size:12px;font-weight:400;color:var(--text-2)">lines</span>` : "—",
  trend: () => {
    if (!logs) return "";
    const s = logs.status;
    if (!s.enabled) return `<span>log collection is off</span>`;
    if (s.ok === false) return `<span>✕ can't read log: ${esc(s.error)}</span>`;
    const top = [...logs.groups].sort((a, b) => b.count - a.count)[0];
    return `<span>${fmtNum(logs.counts.WARN)} warnings · ${fmtNum(logs.counts.ERROR)} errors in range</span>` +
      (top ? `<span>most frequent: ${esc(((top.target ? top.target + ": " : "") + top.message).slice(0, 70))}… (${fmtNum(top.count)})</span>` : "");
  } });
card({ id: "bps", group: "node", title: "Block rate", sub: "Blocks per second observed by the node", src: "near",
  fmt: v => isNum(v) ? v.toFixed(2) + " /s" : "—", axisFmt: v => v.toFixed(1),
  series: [{ key: "blocks_per_sec", label: "Blocks/s", color: "--s1" }] });
card({ id: "lag", group: "node", title: "Block lag", sub: "Time since the node's latest block", src: "near",
  fmt: fmtDur, series: [{ key: "block_lag_sec", label: "Lag", color: "--s1" }] });
card({ id: "peers", group: "node", title: "Active peers", src: "near", fmt: v => fmtNum(v, 0),
  series: [{ key: "peers", label: "Peers", color: "--s1" }],
  subFn: src => { const m = lastOf(src.max_peers); return isNum(m) ? `max ${m}` : ""; } });
card({ id: "p2p", group: "node", title: "NEAR P2P traffic", sub: "As reported by /network_info", src: "near", fmt: fmtRate, axisFmt: fmtBytesAxis,
  series: [{ key: "near_recv_bps", label: "Received", color: "--s1" }, { key: "near_sent_bps", label: "Sent", color: "--s2" }],
  headline: (st) => fmtRate((st[0]?.last || 0) + (st[1]?.last || 0)),
  trend: (st) => `<span>recv avg ${fmtRate(st[0]?.avg)}</span><span>sent avg ${fmtRate(st[1]?.avg)}</span>` });
card({ id: "version", group: "node", title: "neard version & protocol",
  sub: "Network protocol vs the newest protocol this neard binary supports", src: "near",
  fmt: v => isNum(v) ? String(Math.round(v)) : "—", axisFmt: v => Number.isInteger(v) ? String(v) : "",
  beginAtZero: false, stepped: true, stepSize: 1, grace: 1,
  series: [{ key: "protocol_version", label: "Network protocol", color: "--s1" },
           { key: "latest_protocol_version", label: "Binary supports", color: "--s2" }],
  headline: () => latest?.version ? `<span style="font-size:15px">neard ${esc(latest.version)}</span>` : "—",
  subFn: () => {
    const L = latest || {};
    if (!L.protocol_version) return "";
    if (L.latest_protocol_version > L.protocol_version)
      return `Binary supports protocol ${L.latest_protocol_version}; network is on ${L.protocol_version} (ready for the upgrade)`;
    if (L.latest_protocol_version < L.protocol_version)
      return `Binary only supports protocol ${L.latest_protocol_version} but the network is on ${L.protocol_version}: upgrade neard`;
    return `Protocol ${L.protocol_version} · binary is up to date with the network`;
  },
  trend: () => {
    const vs = lastData?.versions || [];
    if (!vs.length) return "";
    const label = v => `${esc(v.version)}${v.build && v.build !== v.version ? ` (${esc(v.build)})` : ""} · p${esc(v.protocol_version)}`;
    const changes = vs.filter(v => !v.before_range);
    const startRow = vs.find(v => v.before_range);
    if (!changes.length || (changes.length === 1 && !startRow)) {
      const v = vs[vs.length - 1];
      return `<span>${label(v)} since ${new Date(v.ts * 1000).toLocaleString()}</span><span>no changes in range</span>`;
    }
    const items = changes.slice(-4).map(v => `<span>→ ${label(v)} at ${fmtTime(v.ts * 1000, lastData.range)}</span>`);
    return (startRow ? `<span>started on ${label(startRow)}</span>` : "") + items.join("") +
      (changes.length > 4 ? `<span>+${changes.length - 4} earlier changes</span>` : "");
  } });
card({ id: "churn", group: "node", title: "Peer churn", sub: "Peers joining and leaving per time bucket", src: "near",
  fmt: v => isNum(v) ? fmtNum(v, 0) : "—", stepped: true,
  series: [{ key: "peer_joins", label: "Joined", color: "--s1" }, { key: "peer_leaves", label: "Left", color: "--s2" }],
  headline: () => peers?.churn ? `${fmtNum(peers.churn.joins + peers.churn.leaves)} <span style="font-size:12px;font-weight:400;color:var(--text-2)">events</span>` : "—",
  trend: () => {
    const c = peers?.churn; if (!c) return "";
    const top = c.top_reconnects.map(t => `${esc(t.name || String(t.peer_id).slice(0, 14))} (${esc(t.joins)})`).join(", ");
    return `<span>${esc(c.joins)} joined · ${esc(c.leaves)} left in range</span>` +
      (top ? `<span>most reconnects: ${top}</span>` : `<span>no repeat reconnects</span>`);
  } });
card({ id: "peerping", group: "node", title: "Peer ping", sub: "Round-trip time across all active peers", src: "ping",
  fmt: v => isNum(v) ? v.toFixed(0) + " ms" : "—",
  series: [{ key: "median_ms", label: "Median", color: "--s1" }, { key: "p90_ms", label: "90th percentile", color: "--s2" }],
  trend: (st, def, src) => {
    const loss = stats(src.t, src.loss_pct), lastPeers = lastOf(src.peers), lastReplied = lastOf(src.replied);
    return (st[0] ? `<span>median avg ${def.fmt(st[0].avg)}</span>` : "") +
      (loss ? `<span>loss avg ${fmtPct(loss.avg)} · max ${fmtPct(loss.max)}</span>` : "") +
      (isNum(lastPeers) ? `<span>${esc(lastReplied)}/${esc(lastPeers)} replied last round</span>` : "");
  } });
card({ id: "latency", group: "node", title: "RPC latency", sub: "Round trip for /status", src: "near",
  fmt: v => isNum(v) ? v.toFixed(0) + " ms" : "—", series: [{ key: "rpc_latency_ms", label: "Latency", color: "--s1" }] });

card({ id: "uptime", group: "validator", title: "Production this epoch", sub: "Produced ÷ expected", src: "near",
  fmt: v => fmtPct(v, 2), axisFmt: v => v + "%", beginAtZero: false, yMax: 100,
  series: [{ key: "blocks_pct", label: "Blocks", color: "--s1" }, { key: "chunks_pct", label: "Chunks", color: "--s2" },
           { key: "endorsements_pct", label: "Endorsements", color: "--s3" }],
  headline: (st) => { const v = st.filter(Boolean).map(s => s.last); return v.length ? fmtPct(v.reduce((a, b) => a + b) / v.length, 2) : "—"; },
  trend: (st, def, src) => {
    const p = k => `${fmtNum(lastOf(src[k + "_produced"]))}/${fmtNum(lastOf(src[k + "_expected"]))}`;
    return `<span>blocks ${p("blocks")}</span><span>chunks ${p("chunks")}</span><span>endorsements ${p("endorsements")}</span>`;
  } });
card({ id: "stake", group: "validator", title: "Stake vs seat price", src: "near", fmt: fmtNear, axisFmt: v => fmtNum(v), yMin: 0,
  series: [{ key: "stake", label: "Your stake", color: "--s1" }, { key: "seat_price", label: "Seat price", color: "--s2" }],
  sub: "Seat price ≈ lowest stake in the active set",
  headline: (st) => st[0] && st[1] ? `${(st[0].last / st[1].last).toFixed(2)}×` : "—",
  trend: (st) => {
    if (!st[0]) return `<span>Set a validator account to compare stake.</span>`;
    const d = st[0].last - st[0].first;
    return `<span>stake change ${d >= 0 ? "+" : "−"}${fmtNear(Math.abs(d))}</span>` + (st[1] ? `<span>margin ${fmtNear(st[0].last - st[1].last)}</span>` : "");
  } });
card({ id: "epoch", group: "validator", title: "Epoch progress", src: "near", fmt: v => fmtPct(v), axisFmt: v => v + "%", yMax: 100,
  series: [{ key: "epoch_progress_pct", label: "Progress", color: "--s1" }],
  subFn: src => { const e = lastOf(src.epoch_height); return isNum(e) ? `epoch ${e}` : ""; },
  trend: () => latest?.epoch_blocks_remaining != null ? `<span>${fmtNum(latest.epoch_blocks_remaining)} blocks remaining</span>` : "" });


// ---------------------------------------------------------------- peers table
let peers = null, peerSort = { key: "stake", dir: -1 };
try { const s = JSON.parse(localStorage.getItem("nvs-peer-sort")); if (s?.key) peerSort = s; } catch {}

function peerSortValue(p, key) {
  if (key === "blocks" || key === "chunks" || key === "endorsements") return p[key]?.pct ?? null;
  if (key.startsWith("ping_")) {
    const f = { ping_last: "last", ping_avg: "avg", ping_min: "min", ping_jitter: "jitter", ping_loss: "loss_pct" }[key];
    return p.ping?.[f] ?? null;
  }
  if (key === "session") {
    // Connected peers rank by session length; disconnected ones after them, most recent first.
    const now = peers.now;
    return p.connected ? (p.connected_since ? now - p.connected_since : 0) + 1e9 : -(now - p.last_seen);
  }
  if (key === "ip") return p.ip ? p.ip.split(".").map(n => n.padStart(3, "0")).join(".") : null;
  return p[key] ?? null;
}
function pctCell(s) {
  if (!s || !s.expected) return `<td class="num"><span class="muted">${s ? "0 expected" : "—"}</span></td>`;
  const icon = s.pct < 80 ? `<span style="color:var(--critical)" title="Below 80%">✕</span> `
             : s.pct < 95 ? `<span style="color:var(--warning)" title="Below 95%">⚠</span> ` : "";
  return `<td class="num"><span class="${icon ? "pct-warn" : ""}">${icon}${fmtPct(s.pct, 2)}</span><br>` +
         `<span class="muted">${fmtCount(s.produced)} / ${fmtCount(s.expected)}</span></td>`;
}
const fmtMs = v => isNum(v) ? (v < 10 ? v.toFixed(1) : v.toFixed(0)) + " ms" : "—";
// The x-axis spans the whole selected range (same for every row), and the line breaks wherever
// a bucket has no pings, e.g. while the peer was disconnected, instead of joining across the gap.
function sparkline(trend, t0, t1, bucket) {
  if (!trend || !trend.length) return `<span class="muted">—</span>`;
  const W = 96, H = 24, P = 2;
  const vals = trend.map(t => t[1]).filter(isNum);
  if (!vals.length) return `<span class="muted">no replies</span>`;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const x = t => P + Math.max(0, Math.min(1, (t + bucket / 2 - t0) / ((t1 - t0) || 1))) * (W - 2 * P);
  const y = v => vals.length === 1 || hi === lo ? H / 2 : H - P - (v - lo) / span * (H - 2 * P);
  let d = "", pen = false, marks = "", prevT = null;
  for (const [t, v, lost] of trend) {
    if (prevT !== null && t - prevT > bucket * 1.5) pen = false;  // missing buckets -> gap
    if (isNum(v)) { d += `${pen ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`; pen = true; }
    else pen = false;
    if (lost) marks += `<circle cx="${x(t).toFixed(1)}" cy="${H - P}" r="2" fill="var(--critical)"/>`;
    prevT = t;
  }
  // An isolated point has no neighbour to draw a line to, so give it a dot to keep it visible.
  for (const seg of d.split("M").filter(s => s && !s.includes("L"))) {
    const [cx, cy] = seg.split(",");
    marks += `<circle cx="${cx}" cy="${cy}" r="1.5" fill="var(--s1)"/>`;
  }
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Ping trend ${fmtMs(lo)} to ${fmtMs(hi)}">` +
    `<path d="${d}" fill="none" stroke="var(--s1)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>${marks}` +
    `<title>${fmtMs(lo)} – ${fmtMs(hi)}${marks ? " · red dots = lost pings" : ""}</title></svg>`;
}
function pingCells(pg) {
  if (!pg) return `<td class="num muted" colspan="5">not pinged yet</td><td></td>`;
  const lossIcon = pg.loss_pct >= 100 ? `<span style="color:var(--critical)">✕</span> ` : pg.loss_pct > 5 ? `<span style="color:var(--warning)">⚠</span> ` : "";
  const last = isNum(pg.last) ? fmtMs(pg.last) : `<span style="color:var(--critical)">✕</span> timeout`;
  return `<td class="num">${last}</td>
    <td class="num">${fmtMs(pg.avg)}</td>
    <td class="num">${fmtMs(pg.min)}<br><span class="muted">${fmtMs(pg.max)}</span></td>
    <td class="num">${fmtMs(pg.jitter)}</td>
    <td class="num"><span class="${lossIcon ? "pct-warn" : ""}">${lossIcon}${fmtPct(pg.loss_pct, 1)}</span><br><span class="muted">${esc(pg.received)}/${esc(pg.sent)}</span></td>
    <td>${sparkline(pg.trend, peers.now - peers.range, peers.now, peers.ping?.bucket || 60)}</td>`;
}
function sessionCell(p) {
  const now = peers.now;
  const rc = p.reconnects > 1 ? `<br><span class="muted">${p.reconnects} reconnects</span>`
           : p.reconnects === 1 && p.first_seen && p.first_seen < now - peers.range ? `<br><span class="muted">reconnected</span>` : "";
  if (p.connected) {
    return `<td>${p.connected_since ? fmtDur(now - p.connected_since) : "—"}${rc}</td>`;
  }
  return `<td><span title="Last seen ${new Date(p.last_seen * 1000).toLocaleString()}">left ${fmtDur(now - p.last_seen)} ago</span>${rc}</td>`;
}
function renderPeers() {
  const tbody = document.querySelector("#peerTable tbody");
  if (!peers) return;
  const q = document.getElementById("peerSearch").value.trim().toLowerCase();
  const onlyV = document.getElementById("peerValidatorsOnly").checked;
  const showGone = document.getElementById("peerShowGone").checked;
  let rows = peers.peers.filter(p => (!onlyV || p.account_id) && (showGone || p.connected) &&
    (!q || [p.account_id, p.ip, p.peer_id].some(v => v && v.toLowerCase().includes(q))));
  const { key, dir } = peerSort;
  rows.sort((a, b) => {
    const x = peerSortValue(a, key), y = peerSortValue(b, key);
    if (x === null && y === null) return 0;
    if (x === null) return 1; if (y === null) return -1;   // empty values always last
    return (x < y ? -1 : x > y ? 1 : 0) * dir;
  });
  document.querySelectorAll("#peerTable th[data-sort]").forEach(th => {
    th.querySelector(".arrow")?.remove();
    th.setAttribute("aria-sort", th.dataset.sort === key ? (dir > 0 ? "ascending" : "descending") : "none");
    if (th.dataset.sort === key) th.insertAdjacentHTML("beforeend", `<span class="arrow">${dir > 0 ? "▲" : "▼"}</span>`);
  });
  const named = peers.peers.filter(p => p.account_id && p.connected).length;
  document.getElementById("peerCount").textContent =
    `${rows.length} shown · ${peers.count} active peers · ${named} validators · ${peers.disconnected} disconnected` +
    (peers.time ? ` · updated ${new Date(peers.time * 1000).toLocaleTimeString()}` : "");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="13" class="muted">${config && config.collect_peers === false ? "Peer collection is turned off (Settings → Data collection)."
      : peers.count ? "No peers match the filter." : "No peer data yet (needs /network_info)."}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(p => {
    const tag = (!p.connected ? `<span class="tag">disconnected</span>` : "") +
      (p.validator === "next" ? `<span class="tag">next epoch</span>` : p.slashed ? `<span class="tag">slashed</span>` : "");
    const name = p.account_id ? `${esc(p.account_id)}${tag}`
      : `<span class="muted">unknown</span>${tag}`;
    return `<tr class="${p.connected ? "" : "gone"}">
      <td>${name}<br><span class="muted mono" title="${esc(p.peer_id)}">${esc((p.peer_id || "").slice(0, 20))}…</span></td>
      <td><span class="mono">${esc(p.ip || "—")}</span>${p.port ? `<span class="muted mono">:${esc(p.port)}</span>` : ""}</td>
      ${sessionCell(p)}
      <td class="num">${p.stake != null ? fmtNear(p.stake) : `<span class="muted">—</span>`}</td>
      ${pctCell(p.blocks)}${pctCell(p.chunks)}${pctCell(p.endorsements)}
      ${pingCells(p.ping)}
    </tr>`;
  }).join("");
  const pi = peers.ping || {}, lr = pi.last_round;
  document.getElementById("pingInfo").textContent = !pi.enabled ? "Peer pinging is off (Settings)."
    : `Method: ${pi.method === "tcp" ? "TCP connect to the peer's port" : "ICMP"}, one round every ${pi.interval}s` +
      (lr ? `, last round ${new Date(lr.time * 1000).toLocaleTimeString()}: ${lr.replied}/${lr.peers} replied.` : ", first round pending.") +
      (pi.error ? ` Error: ${pi.error}` : "");
}
document.querySelector("#peerTable thead").addEventListener("click", e => {
  const th = e.target.closest("th[data-sort]"); if (!th) return;
  const key = th.dataset.sort;
  peerSort = { key, dir: peerSort.key === key ? -peerSort.dir : (key === "account_id" || key === "ip" ? 1 : -1) };
  try { localStorage.setItem("nvs-peer-sort", JSON.stringify(peerSort)); } catch {}
  renderPeers();
});
document.getElementById("peerSearch").addEventListener("input", renderPeers);
document.getElementById("peerValidatorsOnly").addEventListener("change", renderPeers);
document.getElementById("peerShowGone").addEventListener("change", renderPeers);


// ---------------------------------------------------------------- host checks (run once at service start)
const CHECK_BADGE = {
  pass: ["--good", "✓", "Pass"], warn: ["--warning", "⚠", "Warning"],
  fail: ["--critical", "✕", "Fail"], na: ["--muted", "–", "N/A"],
};
function renderChecks(hc) {
  const st = document.getElementById("checkStatus"), tbody = document.querySelector("#checkTable tbody");
  if (!hc || !hc.time) { st.textContent = "running…"; return; }
  const c = hc.counts || {};
  st.textContent = `checked at service start, ${new Date(hc.time * 1000).toLocaleString()} on ${hc.hostname}` +
    ` · ${c.pass || 0} passed` + (c.warn ? ` · ${c.warn} warnings` : "") + (c.fail ? ` · ${c.fail} failed` : "") +
    (c.na ? ` · ${c.na} not applicable` : "") + " · restart the dashboard to re-check";
  let group = null;
  tbody.innerHTML = hc.items.map(i => {
    const [color, icon, label] = CHECK_BADGE[i.status] || CHECK_BADGE.na;
    const head = i.group !== group ? `<tr class="grp"><td colspan="5">${esc(i.group)}</td></tr>` : "";
    group = i.group;
    return head + `<tr>
      <td><span class="mono">${esc(i.title)}</span></td>
      <td><span class="lvl"><i style="color:var(${color})">${icon}</i>${label}</span></td>
      <td><span class="mono">${esc(i.actual ?? "—")}</span></td>
      <td><span class="mono">${esc(i.expected ?? "—")}</span></td>
      <td class="detail">${esc(i.detail || "")}</td>
    </tr>`;
  }).join("");
}


// ---------------------------------------------------------------- service log
let logs = null, logLevel = "all";
const lvlBadge = l => l === "ERROR"
  ? `<span class="lvl"><i style="color:var(--critical)">✕</i>ERROR</span>`
  : `<span class="lvl"><i style="color:var(--warning)">⚠</i>WARN</span>`;
let logsSeq = 0;
async function loadLogs() {
  const seq = ++logsSeq;
  const q = encodeURIComponent(document.getElementById("logSearch").value.trim());
  const body = await getJSON(`/api/logs?range=${rangeSeconds()}&level=${logLevel}&q=${q}`);
  if (seq === logsSeq) logs = body;  // ignore responses that arrive after a newer request
}
function renderLogs() {
  if (!logs) return;
  const s = logs.status, st = document.getElementById("logStatus");
  const src = s.source === "journald" ? `journald unit ${s.target || "neard"}` : `file ${s.target || "(not set)"}`;
  st.innerHTML = !s.enabled ? "collection off (Settings → Data collection)"
    : s.ok === false ? `<span style="color:var(--critical)">✕</span> ${esc(s.error)}`
    : `${esc(src)}` + (s.last_read ? ` · checked ${new Date(s.last_read * 1000).toLocaleTimeString()}` : " · waiting for first read") +
      (s.warning ? ` · <span style="color:var(--warning)">⚠</span> ${esc(s.warning)}` : "");
  const grouped = document.getElementById("logGroup").checked;
  const rows = grouped ? logs.groups : logs.recent;
  const thead = document.querySelector("#logTable thead"), tbody = document.querySelector("#logTable tbody");
  thead.innerHTML = grouped
    ? `<tr><th>Level</th><th class="num">Count</th><th>Last seen</th><th>Target</th><th>Message (latest example)</th></tr>`
    : `<tr><th>Time</th><th>Level</th><th>Target</th><th>Message</th></tr>`;
  document.getElementById("logCount").textContent =
    `${fmtNum(logs.counts.WARN)} warnings · ${fmtNum(logs.counts.ERROR)} errors in range` +
    (rows.length >= 200 ? " · showing latest 200" : "");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">${s.ok === false || !s.enabled ? "No log data." :
      "No warnings or errors in this range" + (logLevel !== "all" || document.getElementById("logSearch").value ? " matching the filter." : ".")}</td></tr>`;
    return;
  }
  const msgCell = r => `<td class="msgcell"><div class="msg" title="Click to expand">${esc(r.message)}</div></td>`;
  tbody.innerHTML = rows.map(r => grouped
    ? `<tr><td>${lvlBadge(r.level)}</td><td class="num">${fmtNum(r.count)}</td>
         <td>${fmtDur(Date.now() / 1000 - r.last)} ago${r.count > 1 ? `<br><span class="muted">first ${fmtDur(Date.now() / 1000 - r.first)} ago</span>` : ""}</td>
         <td><span class="mono">${esc(r.target)}</span></td>${msgCell(r)}</tr>`
    : `<tr><td><span title="${new Date(r.ts * 1000).toLocaleString()}">${new Date(r.ts * 1000).toLocaleTimeString()}</span></td>
         <td>${lvlBadge(r.level)}</td><td><span class="mono">${esc(r.target)}</span></td>${msgCell(r)}</tr>`).join("");
}
document.querySelector("#logTable").addEventListener("click", e => {
  const m = e.target.closest(".msg"); if (m) m.classList.toggle("open");
});
document.getElementById("logLevel").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  logLevel = b.dataset.l;
  document.querySelectorAll("#logLevel button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  await loadLogs(); renderLogs();
});
let logSearchTimer = null;
document.getElementById("logSearch").addEventListener("input", () => {
  clearTimeout(logSearchTimer);
  logSearchTimer = setTimeout(async () => { await loadLogs(); renderLogs(); }, 300);
});
document.getElementById("logGroup").addEventListener("change", renderLogs);


// ---------------------------------------------------------------- same-shard validators
let shard = null, shardSort = { key: "chunks", dir: -1 };
try { const s = JSON.parse(localStorage.getItem("nvs-shard-sort")); if (s?.key) shardSort = s; } catch {}
try { document.getElementById("shardEndorsers").checked = localStorage.getItem("nvs-shard-endorsers") === "1"; } catch {}

function shardSortValue(r, key) {
  if (key === "blocks" || key === "chunks" || key === "endorsements") return r[key]?.pct ?? null;
  if (key === "role") return r.producer ? 0 : 1;
  if (key === "ping") return r.peer?.ping_avg ?? null;
  return r[key] ?? null;
}
function renderShard() {
  const empty = document.getElementById("shardEmpty"), box = document.getElementById("shardBox");
  if (!shard) return;
  if (!shard.is_validator) {
    box.hidden = true;
    empty.textContent = `Not shown: ${shard.reason}. This table appears when the node is an active validator; set the validator account in Settings.`;
    document.getElementById("shardTitle").textContent = "Validators on your shard";
    return;
  }
  box.hidden = false; empty.textContent = "";
  const multi = shard.shards.length > 1;
  document.getElementById("shardTitle").textContent =
    `Validators on your shard${multi ? "s" : ""} (${shard.shards.join(", ")})`;
  const { key, dir } = shardSort;
  const rows = [...shard.rows].sort((a, b) => {
    if (multi && key !== "shard" && a.shard !== b.shard) return a.shard - b.shard;  // keep shards grouped
    const x = shardSortValue(a, key), y = shardSortValue(b, key);
    if (x === null && y === null) return 0;
    if (x === null) return 1; if (y === null) return -1;
    return (x < y ? -1 : x > y ? 1 : 0) * dir;
  });
  document.querySelectorAll("#shardTable th[data-sort]").forEach(th => {
    th.querySelector(".arrow")?.remove();
    th.setAttribute("aria-sort", th.dataset.sort === key ? (dir > 0 ? "ascending" : "descending") : "none");
    if (th.dataset.sort === key) th.insertAdjacentHTML("beforeend", `<span class="arrow">${dir > 0 ? "▲" : "▼"}</span>`);
  });
  const producers = shard.rows.filter(r => r.producer).length;
  document.getElementById("shardCount").textContent =
    `${shard.rows.length} validators · ${producers} chunk producers · epoch ${shard.epoch_height}`;
  document.querySelector("#shardTable tbody").innerHTML = rows.map(r => {
    const role = r.producer ? "Chunk producer" : "Endorser";
    const tags = (r.you ? `<span class="tag">you</span>` : "") + (r.slashed ? `<span class="tag">slashed</span>` : "");
    const ping = r.peer
      ? `${fmtMs(r.peer.ping_avg)}<br><span class="muted">${isNum(r.peer.loss_pct) ? fmtPct(r.peer.loss_pct, 0) + " loss" : "not pinged yet"}</span>`
      : `<span class="muted">not a peer</span>`;
    return `<tr${r.you ? ` style="background:var(--wash)"` : ""}>
      <td class="num">${esc(r.shard)}</td>
      <td>${r.you ? `<b>${esc(r.account_id)}</b>` : esc(r.account_id)}${tags}</td>
      <td>${role}</td>
      <td class="num">${fmtNear(r.stake)}<br><span class="muted">rank #${esc(r.rank)}</span></td>
      ${pctCell(r.blocks)}${pctCell(r.chunks)}${pctCell(r.endorsements)}
      <td class="num">${ping}</td>
    </tr>`;
  }).join("");
}
document.querySelector("#shardTable thead").addEventListener("click", e => {
  const th = e.target.closest("th[data-sort]"); if (!th) return;
  const key = th.dataset.sort;
  shardSort = { key, dir: shardSort.key === key ? -shardSort.dir : (key === "account_id" || key === "shard" || key === "role" ? 1 : -1) };
  try { localStorage.setItem("nvs-shard-sort", JSON.stringify(shardSort)); } catch {}
  renderShard();
});
document.getElementById("shardEndorsers").addEventListener("change", e => {
  try { localStorage.setItem("nvs-shard-endorsers", e.target.checked ? "1" : "0"); } catch {}
  invalidate(["shard"]);
});

// ---------------------------------------------------------------- RED summary for the neard service
function renderRed() {
  // RED for the neard service
  const n = lastData?.near || {}, LN = k => lastOf(n[k]);
  const pcts = ["blocks_pct", "chunks_pct", "endorsements_pct"].map(LN).filter(isNum);
  const missed = pcts.length ? 100 - Math.min(...pcts) : null;
  const okAvg = (n.ok || []).filter(isNum); const rpcErr = okAvg.length ? (1 - okAvg.reduce((a, b) => a + b, 0) / okAvg.length) * 100 : null;
  document.getElementById("redTiles").innerHTML = [
    tile("Rate", isNum(LN("blocks_per_sec")) ? LN("blocks_per_sec").toFixed(2) + " blocks/s" : "—", `${fmtNum(LN("peers"))} peers`),
    tile("Errors", isNum(missed) ? fmtPct(missed, 2) + " missed" : "—", `RPC errors ${isNum(rpcErr) ? fmtPct(rpcErr, 1) : "—"} in range`),
    tile("Duration", isNum(LN("block_lag_sec")) ? "lag " + fmtDur(LN("block_lag_sec")) : "—", `RPC ${isNum(LN("rpc_latency_ms")) ? LN("rpc_latency_ms").toFixed(0) + " ms" : "—"}`),
  ].join("");
}

// ---------------------------------------------------------------- registration
registerModule({
  name: "near",
  sections: { node: "collect_node", validator: "collect_validator", shard: "collect_validator", peers: "collect_peers" },
  tileOrder: 0,
  header(L) {
    const n = lastData?.near || {};
    const health = document.getElementById("health");
    const state = syncState();
    if (state) {
      const r = lastData ? syncRuns(n.t, (n.synced || []).map(v => isNum(v) ? v * 100 : v), lastData.bucket) : {};
      health.innerHTML = statusPill(state[0], state[1]) +
        (isNum(r.pct) ? `<span style="font-weight:400;color:var(--text-2)">· ${fmtPct(r.pct, 1)} synced</span>` : "");
      health.title = "Current sync state · share of samples synced in the selected range";
    }
  },
  headerSub(L) {
    const sub = [];
    if (L.chain_id) sub.push(esc(L.chain_id));
    if (L.version) sub.push("neard " + esc(L.version));
    if (L.protocol_version) sub.push("protocol " + esc(L.protocol_version) + (L.latest_protocol_version && L.latest_protocol_version !== L.protocol_version ? ` (latest ${esc(L.latest_protocol_version)})` : ""));
    if (L.node_uptime_sec) sub.push("node up " + fmtDur(L.node_uptime_sec));
    return sub;
  },
  banner(L) {
    const errs = [L.error, L.network_info_error && "network_info: " + L.network_info_error, L.validators_error && "validators: " + L.validators_error,
      L.host_checks?.counts?.fail && `Host checks: ${L.host_checks.counts.fail} failed at service start (see NEAR node → Host checks).`,
      L.validators_stale && `Validator data is stale: last successful fetch ${fmtDur(Date.now() / 1000 - L.validators_fetched_at)} ago. Validator figures may be out of date.`,
      L.rpc_nodes_seen > 1 && `This RPC URL is load-balanced: ${L.rpc_nodes_seen} different nodes answered in recent samples. ` +
        `Peers, churn, peer ping and sync stats mix data from different nodes. Point the RPC URL at a single node (your own) for accurate data.`].filter(Boolean);
    return errs.length ? [...errs.map(esc), `<span style="color:var(--muted)">RPC: ${esc(L.rpc_url || "")}</span>`] : [];
  },
  tiles(L, data) {
    const n = data.near || {};
    const lag = lastOf(n.block_lag_sec);
    let vStatus = esc(L.validator_status || "—"), vDetail = esc(L.validator_account_id || "no account configured");
    if (L.kickout_reason) vDetail += " · kicked last epoch: " + esc(Object.keys(L.kickout_reason)[0]);
    else if (L.validator_status === "Active") vDetail += (L.in_next_epoch ? " · in next epoch" : " · NOT in next epoch");
    const uptime = isNum(L.uptime_pct) ? fmtPct(L.uptime_pct, 2) : "—";
    const stake = lastOf(n.stake), seat = lastOf(n.seat_price);
    const ep = lastOf(n.epoch_progress_pct);
    return [
    tile("Validator", vStatus, vDetail),
    tile("Uptime this epoch", uptime, L.rank ? `rank #${esc(L.rank)} of ${esc(lastOf(n.num_validators))}` : ""),
    tile("Stake", fmtNear(stake), isNum(seat) ? `seat price ${fmtNear(seat)}` : ""),
    tile("Epoch " + (lastOf(n.epoch_height) ?? ""), fmtPct(ep), L.epoch_blocks_remaining != null ? `${fmtNum(L.epoch_blocks_remaining)} blocks left · ~${fmtDur(L.epoch_blocks_remaining / (lastOf(n.blocks_per_sec) || 1.1))}` : "", isNum(ep) ? ep : null),
    tile("Block height", isNum(L.block_height) ? L.block_height.toLocaleString() : fmtNum(lastOf(n.block_height)), isNum(lag) ? `lag ${fmtDur(lag)}` : ""),
    tile("Peers", fmtNum(lastOf(n.peers)), isNum(lastOf(n.max_peers)) ? `of ${lastOf(n.max_peers)} max` : ""),
    ];
  },
  afterCore() {
    renderChecks(latest.host_checks);
    renderRed();
  },
  polls: {
    peers: { every: 30, wanted: () => config?.collect_peers !== false && (sectionOpen("peers") || sectionOpen("node")),
             fetch: () => getJSON(`/api/peers?range=${rangeSeconds()}`), apply(r) { peers = r; renderPeers(); } },
    shard: { every: 60, wanted: () => config?.collect_validator !== false && sectionOpen("shard"),
             fetch: () => getJSON(`/api/shard?range=${rangeSeconds()}&endorsers=${document.getElementById("shardEndorsers").checked ? 1 : 0}`),
             apply(r) { shard = r; renderShard(); } },
    // Not while someone is reading an expanded message (the table would re-render under them).
    logs: { every: 15, wanted: () => config?.collect_logs !== false && sectionOpen("node") && !document.querySelector("#logTable .msg.open"),
            fetch: () => loadLogs(), apply() { if (!document.querySelector("#logTable .msg.open")) renderLogs(); } },
  },
  onSectionOpen(key) {
    if (key === "peers" && peers) renderPeers();
    if (key === "shard" && shard) renderShard();
  },
  onTheme() { renderPeers(); renderShard(); },
});
