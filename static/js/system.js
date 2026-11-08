// Near Node Dashboard: the "system" module's part of the page (host charts, USE summaries,
// health & security checks). Registered with the core in js/core.js.
"use strict";

// ---------------------------------------------------------------- chart definitions
card({ id: "cpu", group: "cpu", title: "CPU usage", src: "system", fmt: v => fmtPct(v), axisFmt: v => v + "%", yMax: 100,
  series: [{ key: "cpu_pct", label: "System", color: "--s1" }, { key: "proc_cpu_pct", label: "neard", color: "--s2" }],
  subFn: src => { const l = lastOf(src.load1); return isNum(l) ? `load avg (1m) ${l.toFixed(2)}` : ""; },
  trend: (st, def) => trendHtml(st[0], def) });
card({ id: "mem", group: "mem", title: "Memory used vs available", src: "system", fmt: v => fmtBytes(v), axisFmt: fmtBytesAxis,
  series: [{ key: "mem_used", label: "Used", color: "--s1" }, { key: "mem_available", label: "Available", color: "--s3" },
           { key: "proc_rss", label: "neard RSS", color: "--s2" }],
  headline: (st, src) => fmtPct(lastOf(src.mem_pct)),
  subFn: src => { const t = lastOf(src.mem_total), sw = lastOf(src.swap_pct); return isNum(t) ? `of ${fmtBytes(t)} · swap ${fmtPct(sw)}` : ""; },
  trend: (st, def) => trendHtml(st[0], def) });
card({ id: "disk", group: "disk", title: "Disk usage", src: "system", fmt: v => fmtBytes(v), axisFmt: fmtBytesAxis, beginAtZero: false,
  series: [{ key: "disk_used", label: "Used", color: "--s1" }],
  headline: (st, src) => fmtPct(lastOf(src.disk_pct)),
  subFn: src => { const t = lastOf(src.disk_total); return isNum(t) ? `${config?.disk_path || ""} · ${fmtBytes(t)} total` : ""; },
  trend: (st, def, src) => {
    const s = st[0]; if (!s) return "";
    const total = lastOf(src.disk_total);
    let extra = "";
    if (s.slope > 0 && isNum(total)) extra = `full in ~${fmtDur((total - s.last) / s.slope * 3600)}`;
    return trendHtml(s, def, { extra });
  } });
card({ id: "diskio", group: "disk", title: "Disk I/O", src: "system", fmt: fmtRate, axisFmt: fmtBytesAxis,
  series: [{ key: "disk_read_bps", label: "Read", color: "--s1" }, { key: "disk_write_bps", label: "Write", color: "--s2" }],
  headline: (st) => fmtRate((st[0]?.last || 0) + (st[1]?.last || 0)),
  trend: (st, def) => `<span>read avg ${fmtRate(st[0]?.avg)}</span><span>write avg ${fmtRate(st[1]?.avg)}</span><span>peak ${fmtRate(Math.max(st[0]?.max || 0, st[1]?.max || 0))}</span>` });
card({ id: "uptime-sys", group: "health", title: "System uptime", sub: "Time since the machine last booted",
  src: "system", fmt: fmtUptime, axisFmt: v => fmtDur(v),
  series: [{ key: "uptime_sec", label: "Uptime", color: "--s1" }],
  subFn: src => { const b = lastOf(src.boot_time); return isNum(b) ? `booted ${new Date(b * 1000).toLocaleString()}` : ""; },
  trend: (st, def, src) => {
    const n = rebootsIn(src.boot_time);
    return `<span>${n === 0 ? "no reboots" : `<b>${n} reboot${n === 1 ? "" : "s"}</b>`} in range</span>` +
      (st[0] ? `<span>longest ${fmtUptime(st[0].max)}</span>` : "");
  } });
card({ id: "net", group: "net", title: "Network (host)", src: "system", fmt: fmtRate, axisFmt: fmtBytesAxis,
  series: [{ key: "net_recv_bps", label: "In", color: "--s1" }, { key: "net_sent_bps", label: "Out", color: "--s2" }],
  headline: (st) => fmtRate((st[0]?.last || 0) + (st[1]?.last || 0)),
  trend: (st) => `<span>in avg ${fmtRate(st[0]?.avg)}</span><span>out avg ${fmtRate(st[1]?.avg)}</span><span>peak ${fmtRate(Math.max(st[0]?.max || 0, st[1]?.max || 0))}</span>` });

// ---- extended system metrics (USE)
const fmtRateN = (unit) => v => isNum(v) ? (v >= 1000 ? fmtNum(v, 0) : v.toFixed(v < 10 ? 2 : 0)) + unit : "—";
const fmtMs1 = v => isNum(v) ? (v < 10 ? v.toFixed(2) : v.toFixed(0)) + " ms" : "—";
const intAxis = v => Number.isInteger(v) ? fmtNum(v, 0) : "";   // count charts: no 0.5 / 1.5 ticks
const sumOf = arr => (arr || []).filter(isNum).reduce((a, b) => a + b, 0);
card({ id: "cpu-util", group: "cpu", title: "CPU utilization", sub: "Where CPU time goes (iowait and steal are Linux only)",
  src: "system", fmt: v => fmtPct(v), axisFmt: v => v + "%", yMax: 100,
  series: [{ key: "cpu_user", label: "User", color: "--s1" }, { key: "cpu_system", label: "System", color: "--s2" },
           { key: "cpu_iowait", label: "iowait", color: "--s3" }, { key: "cpu_steal", label: "Steal", color: "--s4" }] });
card({ id: "load", group: "cpu", title: "Load per core", sub: "1-minute load ÷ cores; above 1.0 means work is queuing",
  src: "system", fmt: v => isNum(v) ? v.toFixed(2) : "—", series: [{ key: "load_per_core", label: "Load / core", color: "--s1" }],
  subFn: src => { const c = lastOf(src.cpu_cores), l5 = lastOf(src.load5), l15 = lastOf(src.load15);
    return isNum(c) ? `${c} cores · 5m ${isNum(l5) ? (l5 / c).toFixed(2) : "—"} · 15m ${isNum(l15) ? (l15 / c).toFixed(2) : "—"} per core` : ""; } });
card({ id: "runq", group: "cpu", title: "Run queue", sub: "Processes running or waiting for CPU, and blocked on I/O",
  src: "system", fmt: v => isNum(v) ? v.toFixed(0) : "—",
  series: [{ key: "procs_running", label: "Running", color: "--s1" }, { key: "procs_blocked", label: "Blocked", color: "--s2" }] });
card({ id: "ctx", group: "cpu", title: "Context switches & interrupts", src: "system", fmt: fmtRateN(" /s"),
  axisFmt: v => fmtNum(v), series: [{ key: "ctx_switches_ps", label: "Context switches", color: "--s1" },
                                    { key: "interrupts_ps", label: "Interrupts", color: "--s2" }] });
card({ id: "swap", group: "mem", title: "Swap used", src: "system", fmt: v => fmtBytes(v), axisFmt: fmtBytesAxis,
  series: [{ key: "swap_used", label: "Swap used", color: "--s1" }],
  subFn: src => { const p = lastOf(src.swap_pct); return isNum(p) ? `${fmtPct(p)} of swap` : ""; } });
card({ id: "swapio", group: "mem", title: "Swap in / out", sub: "Sustained swap-in means memory pressure", src: "system",
  fmt: fmtRate, axisFmt: fmtBytesAxis,
  series: [{ key: "swap_in_bps", label: "In", color: "--s1" }, { key: "swap_out_bps", label: "Out", color: "--s2" }] });
card({ id: "oom", group: "mem", title: "OOM kills", sub: "Processes killed by the kernel for lack of memory", src: "system",
  fmt: v => isNum(v) ? fmtNum(v, 0) : "—", axisFmt: intAxis, stepped: true, series: [{ key: "oom_kills", label: "OOM kills", color: "--critical" }],
  headline: (st, src) => `${fmtNum(sumOf(src.oom_kills))} <span style="font-size:12px;font-weight:400;color:var(--text-2)">in range</span>` });
card({ id: "fsmax", group: "disk", title: "Fullest filesystem", sub: "Highest space and inode use across filesystems",
  src: "system", fmt: v => fmtPct(v), axisFmt: v => v + "%", yMax: 100,
  series: [{ key: "fs_max_pct", label: "Space", color: "--s1" }, { key: "inode_max_pct", label: "Inodes", color: "--s2" }] });
card({ id: "iops", group: "disk", title: "IOPS", src: "system", fmt: fmtRateN(" /s"), axisFmt: v => fmtNum(v),
  series: [{ key: "disk_read_iops", label: "Read", color: "--s1" }, { key: "disk_write_iops", label: "Write", color: "--s2" }] });
card({ id: "await", group: "disk", title: "I/O latency (await)", sub: "Average time per disk request, queueing included",
  src: "system", fmt: fmtMs1, series: [{ key: "disk_await_ms", label: "await", color: "--s1" }] });
card({ id: "busy", group: "disk", title: "Disk busy", sub: "Busiest disk's share of time with I/O in flight (Linux)",
  src: "system", fmt: v => fmtPct(v), axisFmt: v => v + "%", yMax: 100, series: [{ key: "disk_util_pct", label: "Busy", color: "--s1" }] });
card({ id: "queue", group: "disk", title: "Queue depth", sub: "Average requests in flight across disks (Linux)",
  src: "system", fmt: v => isNum(v) ? v.toFixed(2) : "—", series: [{ key: "disk_queue", label: "Queue", color: "--s1" }] });
card({ id: "netutil", group: "net", title: "Link utilization", sub: "Busiest interface vs its link speed",
  src: "system", fmt: v => fmtPct(v), axisFmt: v => v + "%", series: [{ key: "net_util_pct", label: "Utilization", color: "--s1" }] });
card({ id: "neterr", group: "net", title: "Packet errors & drops", src: "system", fmt: fmtRateN(" /s"),
  series: [{ key: "net_errors_ps", label: "Errors", color: "--critical" }, { key: "net_drops_ps", label: "Drops", color: "--s2" }] });
card({ id: "retrans", group: "net", title: "TCP retransmits", sub: "Share of TCP segments sent again (Linux)", src: "system",
  fmt: v => fmtPct(v, 2), axisFmt: v => v + "%", series: [{ key: "tcp_retrans_pct", label: "Retransmitted", color: "--s1" }] });
card({ id: "tcp", group: "net", title: "TCP connections", sub: "Sockets by state (Linux)", src: "system",
  fmt: v => isNum(v) ? fmtNum(v, 0) : "—",
  series: [{ key: "tcp_established", label: "ESTABLISHED", color: "--s1" }, { key: "tcp_time_wait", label: "TIME_WAIT", color: "--s2" },
           { key: "tcp_close_wait", label: "CLOSE_WAIT", color: "--s3" }] });
card({ id: "dns", group: "net", title: "DNS resolution time", sub: "Lookup through the system resolver (cached answers are fast)",
  src: "system", fmt: fmtMs1, series: [{ key: "dns_ms", label: "Lookup", color: "--s1" }],
  subFn: src => { const ok = lastOf(src.dns_ok); return ok === 0 ? "✕ last lookup failed" : ""; } });
card({ id: "procs", group: "health", title: "Processes", src: "system", fmt: v => isNum(v) ? fmtNum(v, 0) : "—",
  series: [{ key: "proc_count", label: "Processes", color: "--s1" }],
  trend: (st, def, src) => `<span>zombies ${fmtNum(lastOf(src.proc_zombies))}</span>` + (st[0] ? `<span>avg ${def.fmt(st[0].avg)}</span><span>max ${def.fmt(st[0].max)}</span>` : "") });
card({ id: "fds", group: "health", title: "Open file handles", src: "system", fmt: v => isNum(v) ? fmtNum(v, 0) : "—",
  series: [{ key: "fd_used", label: "In use", color: "--s1" }],
  subFn: src => { const m = lastOf(src.fd_max), p = lastOf(src.fd_pct); return isNum(m) ? `${fmtPct(p)} of the ${fmtNum(m)} limit` : "system limit: unlimited"; } });
card({ id: "clock", group: "health", title: "Clock offset", sub: "Difference from NTP time; validators need an accurate clock",
  src: "system", fmt: v => isNum(v) ? (v >= 0 ? "+" : "") + fmtMs1(v) : "—", beginAtZero: false,
  series: [{ key: "clock_offset_ms", label: "Offset", color: "--s1" }],
  trend: (st, def) => trendHtml(st[0], { ...def, fmt: v => isNum(v) ? fmtMs1(v) : "—" }),
  subFn: src => { const s = lastOf(src.ntp_synced); return s === 1 ? "✓ NTP synchronised" : s === 0 ? "✕ not NTP synchronised" : ""; } });
card({ id: "temp", group: "health", title: "Hottest temperature", src: "system", fmt: v => isNum(v) ? v.toFixed(0) + " °C" : "—",
  beginAtZero: false, series: [{ key: "temp_max_c", label: "Hottest sensor", color: "--s2" }] });
card({ id: "logins", group: "security", title: "Failed logins", sub: "Failed SSH logins (from the journal, every 5 min)",
  src: "system", fmt: v => isNum(v) ? fmtNum(v, 0) : "—", axisFmt: intAxis, stepped: true, series: [{ key: "failed_logins", label: "Failed logins", color: "--s2" }],
  headline: (st, src) => `${fmtNum(sumOf(src.failed_logins))} <span style="font-size:12px;font-weight:400;color:var(--text-2)">in range</span>` });
card({ id: "syslog", group: "security", title: "System log errors & warnings", sub: "All services, from the journal (every 5 min)",
  src: "system", fmt: v => isNum(v) ? fmtNum(v, 0) : "—", axisFmt: intAxis, stepped: true,
  series: [{ key: "sys_log_errors", label: "Errors", color: "--critical" }, { key: "sys_log_warnings", label: "Warnings", color: "--warning" }],
  headline: (st, src) => `${fmtNum(sumOf(src.sys_log_errors))} <span style="font-size:12px;font-weight:400;color:var(--text-2)">errors in range</span>` });


// ---------------------------------------------------------------- USE summaries and health tables

let healthData = null;

function useLine(id, u, sat, err) {
  const el = document.getElementById(id); if (!el) return;
  el.innerHTML = `<b title="Utilization">U</b>${u}<span class="sep">·</span><b title="Saturation">S</b>${sat}<span class="sep">·</span><b title="Errors">E</b>${err}`;
}

function renderHealth() {
  const s = lastData?.system || {}, h = healthData || {}, ck = h.checks || {};
  const L = k => lastOf(s[k]);
  // USE summaries per resource
  useLine("use-cpu", `${fmtPct(L("cpu_pct"))} busy` + (isNum(L("cpu_steal")) ? `, steal ${fmtPct(L("cpu_steal"))}` : ""),
    `load ${isNum(L("load_per_core")) ? L("load_per_core").toFixed(2) : "—"}/core` + (isNum(L("procs_running")) ? `, run queue ${L("procs_running")}` : ""),
    "—");
  useLine("use-mem", `${fmtPct(L("mem_pct"))} used, ${fmtBytes(L("mem_available"))} available`,
    `swap ${fmtPct(L("swap_pct"))}, swap-in ${fmtRate(L("swap_in_bps"))}`,
    `${fmtNum(sumOf(s.oom_kills))} OOM kills in range`);
  const smart = ck.smart;
  useLine("use-disk", `fullest fs ${fmtPct(L("fs_max_pct"), 0)}` + (isNum(L("disk_util_pct")) ? `, busiest disk ${fmtPct(L("disk_util_pct"), 0)}` : ""),
    `await ${fmtMs1(L("disk_await_ms"))}` + (isNum(L("disk_queue")) ? `, queue ${L("disk_queue").toFixed(2)}` : ""),
    smart ? `SMART ${STATUS[smart.status]?.[2] || "N/A"}` : "SMART —");
  useLine("use-net", isNum(L("net_util_pct")) ? `${fmtPct(L("net_util_pct"))} of link` : `${fmtRate((L("net_recv_bps") || 0) + (L("net_sent_bps") || 0))}`,
    isNum(L("tcp_retrans_pct")) ? `retransmits ${fmtPct(L("tcp_retrans_pct"), 2)}` : "retransmits —",
    `${isNum(L("net_errors_ps")) ? ((L("net_errors_ps") || 0) + (L("net_drops_ps") || 0)).toFixed(1) : "—"} errors+drops/s`);
  // filesystems
  const fs = h.filesystems || [];
  document.querySelector("#fsTable tbody").innerHTML = fs.length ? fs.map(f => `<tr>
      <td><span class="mono">${esc(f.mount)}</span><br><span class="muted mono">${esc(f.device)}</span></td>
      <td>${esc(f.fstype)}</td><td class="num">${fmtBytes(f.used)}</td><td class="num">${fmtBytes(f.total)}</td>
      <td>${meter(f.pct)}</td><td>${meter(f.inode_pct)}</td></tr>`).join("")
    : `<tr><td colspan="6" class="muted">No filesystem data yet.</td></tr>`;
  // health checks table
  const rows = [], row = (name, st, value, detail) => rows.push(`<tr><td>${esc(name)}</td><td>${badge(st)}</td><td>${value}</td><td class="detail">${esc(detail || "")}</td></tr>`);
  if (ck.last_boot) row("Last reboot", ck.last_boot.status, isNum(L("uptime_sec")) ? `up ${fmtUptime(L("uptime_sec"))}` : "—", ck.last_boot.detail);
  if (ck.clock) row("Clock (NTP)", ck.clock.status, isNum(ck.clock.offset_ms) ? `${ck.clock.offset_ms >= 0 ? "+" : ""}${fmtMs1(ck.clock.offset_ms)}` : "—", ck.clock.detail);
  if (ck.dns) row("DNS", ck.dns.status, fmtMs1(ck.dns.ms), ck.dns.detail);
  if (ck.smart) {
    if ((ck.smart.drives || []).length) for (const d of ck.smart.drives)
      row(`SMART ${d.device}`, d.passed === true ? "pass" : d.passed === false ? "fail" : "na",
          (isNum(d.temperature) ? `${d.temperature} °C` : "") + (isNum(d.percentage_used) ? ` · ${d.percentage_used}% worn` : "") || "—",
          `${d.model || ""} ${d.detail || ""}`);
    else row("SMART", ck.smart.status, "—", ck.smart.detail);
  }
  const temps = [...(h.temperatures || [])].sort((a, b) => b.current - a.current).slice(0, 4);
  for (const t of temps) row(`Temperature: ${t.label}`, isNum(t.critical) && t.current >= t.critical ? "fail" : (isNum(t.high) && t.current >= t.high) || t.current > 85 ? "warn" : "pass",
    `${t.current.toFixed(0)} °C`, `${t.chip}` + (isNum(t.high) ? ` · high ${t.high} °C` : "") + (isNum(t.critical) ? ` · critical ${t.critical} °C` : ""));
  if (!temps.length) row("Temperatures", "na", "—", "No temperature sensors readable (Linux hwmon).");
  for (const f of (h.fans || []).slice(0, 4)) row(`Fan: ${f.label}`, f.rpm > 0 ? "pass" : "warn", `${fmtNum(f.rpm)} RPM`, f.chip);
  if (ck.ipmi) row("Power supplies & fans (IPMI)", ck.ipmi.status, "—", ck.ipmi.detail);
  if (ck.patches) row("Security updates", ck.patches.status, isNum(ck.patches.security) ? String(ck.patches.security) : "—", ck.patches.detail);
  for (const c of (ck.tls?.certs || [])) row(`TLS ${c.host}`, c.status, isNum(c.days_left) ? `${c.days_left.toFixed(0)} days` : "—", c.detail);
  if (ck.tls && !(ck.tls.certs || []).length) row("TLS certificates", "na", "—", ck.tls.detail);
  if (ck.logs) row("Journal (logins, log rates)", ck.logs.status, "—", ck.logs.detail);
  document.querySelector("#healthTable tbody").innerHTML = rows.join("") || `<tr><td colspan="4" class="muted">${config?.collect_health === false ? "Health checks are turned off (Settings → Data collection)." : "Running checks…"}</td></tr>`;
}

// ---------------------------------------------------------------- registration
registerModule({
  name: "system",
  sections: { system: "collect_system", health: "collect_health" },
  chartSections: { cpu: "system", mem: "system", disk: "system", net: "system", security: "health" },
  tileOrder: 1,
  tiles(L, data) {
    const s = data.system || {};
    return [
    tile("CPU", fmtPct(lastOf(s.cpu_pct)), isNum(lastOf(s.proc_cpu_pct)) ? `neard ${fmtPct(lastOf(s.proc_cpu_pct))}` : "", lastOf(s.cpu_pct)),
    tile("Memory", fmtPct(lastOf(s.mem_pct)), `${fmtBytes(lastOf(s.mem_used))} of ${fmtBytes(lastOf(s.mem_total))}`, lastOf(s.mem_pct)),
    tile("Disk", fmtPct(lastOf(s.disk_pct)), `${fmtBytes(lastOf(s.disk_used))} of ${fmtBytes(lastOf(s.disk_total))}`, lastOf(s.disk_pct)),
    tile("System uptime", fmtUptime(lastOf(s.uptime_sec)),
      isNum(lastOf(s.boot_time)) ? `since ${new Date(lastOf(s.boot_time) * 1000).toLocaleDateString([], { month: "short", day: "numeric" })}` +
        (rebootsIn(s.boot_time) ? ` · ${rebootsIn(s.boot_time)} reboot(s) in range` : "") : ""),
    ];
  },
  afterCore: renderHealth,
  polls: {
    // Also refreshes the alerts panel between header refreshes.
    health: { every: 30, wanted: () => true, fetch: () => getJSON("/api/health"),
              apply(h) { healthData = h; renderHealth(); renderAlerts(h.alerts); } },
  },
});
