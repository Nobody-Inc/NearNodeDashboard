#!/usr/bin/env python3
"""Near Node Dashboard exporter: an optional second service that republishes what the dashboard
collects, so a central system can scrape it.

It polls the dashboard's local API on a schedule, builds one snapshot, and serves it read-only:
  /metrics            Prometheus text format (scrape with Prometheus, VictoriaMetrics, Grafana Agent...)
  /api/v1/snapshot    the same data as JSON
  /                   status page: last updated and time to next update
  /healthz            200 when the last poll of the dashboard succeeded (no token needed)

Run:  python3 exporter.py [--dashboard http://127.0.0.1:8787] [--host 0.0.0.0] [--port 9787]
                          [--interval auto|SECONDS] [--token-file PATH] [--per-peer]
"""

import argparse
import hmac
import html
import json
import math
import os
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from server import APP_VERSION, MAX_RPC_RESPONSE_BYTES, REQUEST_TIMEOUT_SEC, make_server

VERSION = APP_VERSION
MIN_INTERVAL, MAX_INTERVAL = 15, 3600


# --------------------------------------------------------------------------- reading the dashboard

def fetch(base, path, timeout=15):
    req = urllib.request.Request(base + path, headers={"User-Agent": "near-node-exporter/" + VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(MAX_RPC_RESPONSE_BYTES + 1)
    if len(body) > MAX_RPC_RESPONSE_BYTES:
        raise ValueError(f"{path}: response too large")
    return json.loads(body)


def last_value(series, key):
    """Most recent non-null value of a column in a /api/metrics series."""
    for v in reversed((series or {}).get(key) or []):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            return v
    return None


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def build_snapshot(base):
    """One consistent snapshot from the dashboard's API (all reads hit its response cache)."""
    cfg = fetch(base, "/api/config")
    latest = fetch(base, "/api/latest")
    metrics = fetch(base, "/api/metrics?range=3600&points=60")   # last values + 1-hour sums
    health = fetch(base, "/api/health")
    peers = fetch(base, "/api/peers?range=3600")
    logs = fetch(base, "/api/logs?range=3600")
    shard = fetch(base, "/api/shard?range=3600")
    sysm, near, ping = metrics.get("system", {}), metrics.get("near", {}), metrics.get("ping", {})
    S = lambda k: last_value(sysm, k)  # noqa: E731
    S_sum = lambda k: sum(v for v in (sysm.get(k) or []) if num(v) is not None)  # noqa: E731
    N = lambda k: last_value(near, k)  # noqa: E731

    account = latest.get("validator_account_id") or None
    hostname = latest.get("server_hostname") or None
    pct = lambda v: None if v is None else v / 100  # noqa: E731
    active_peers = [p for p in peers.get("peers", []) if p.get("connected")]
    checks = (latest.get("host_checks") or {}).get("items") or []
    return {
        "node": account or hostname or "unknown",
        "validator_account_id": account,
        "hostname": hostname,
        "chain_id": latest.get("chain_id"),
        "dashboard_version": latest.get("app_version"),
        "collection_interval_seconds": 1 if cfg.get("fast_mode") else cfg.get("interval_seconds"),
        "data_collected_at": num(latest.get("time")),
        "system": {
            "cpu_ratio": pct(S("cpu_pct")), "load1": S("load1"),
            "memory_used_bytes": S("mem_used"), "memory_total_bytes": S("mem_total"), "memory_ratio": pct(S("mem_pct")),
            "swap_ratio": pct(S("swap_pct")),
            "disk_used_bytes": S("disk_used"), "disk_total_bytes": S("disk_total"), "disk_ratio": pct(S("disk_pct")),
            "disk_read_bytes_per_second": S("disk_read_bps"), "disk_write_bytes_per_second": S("disk_write_bps"),
            "network_receive_bytes_per_second": S("net_recv_bps"), "network_transmit_bytes_per_second": S("net_sent_bps"),
            "neard_cpu_ratio": pct(S("proc_cpu_pct")), "neard_resident_bytes": S("proc_rss"),
            "uptime_seconds": S("uptime_sec"), "boot_time_seconds": S("boot_time"),
            # USE extras
            "cpu_user_ratio": pct(S("cpu_user")), "cpu_system_ratio": pct(S("cpu_system")),
            "cpu_iowait_ratio": pct(S("cpu_iowait")), "cpu_steal_ratio": pct(S("cpu_steal")), "cpu_cores": S("cpu_cores"),
            "load_per_core": S("load_per_core"), "load5": S("load5"), "load15": S("load15"),
            "procs_running": S("procs_running"), "procs_blocked": S("procs_blocked"),
            "context_switches_per_second": S("ctx_switches_ps"), "interrupts_per_second": S("interrupts_ps"),
            "memory_available_bytes": S("mem_available"), "swap_used_bytes": S("swap_used"),
            "swap_in_bytes_per_second": S("swap_in_bps"), "swap_out_bytes_per_second": S("swap_out_bps"),
            "oom_kills_1h": S_sum("oom_kills"),
            "disk_read_iops": S("disk_read_iops"), "disk_write_iops": S("disk_write_iops"),
            "disk_await_seconds": None if S("disk_await_ms") is None else S("disk_await_ms") / 1000,
            "disk_busy_ratio": pct(S("disk_util_pct")), "disk_queue_depth": S("disk_queue"),
            "filesystem_max_ratio": pct(S("fs_max_pct")), "inode_max_ratio": pct(S("inode_max_pct")),
            "network_link_ratio": pct(S("net_util_pct")), "network_errors_per_second": S("net_errors_ps"),
            "network_drops_per_second": S("net_drops_ps"), "tcp_retransmit_ratio": pct(S("tcp_retrans_pct")),
            "tcp_established": S("tcp_established"), "tcp_time_wait": S("tcp_time_wait"),
            "tcp_close_wait": S("tcp_close_wait"), "tcp_connections": S("tcp_total"),
            "dns_lookup_seconds": None if S("dns_ms") is None else S("dns_ms") / 1000, "dns_ok": S("dns_ok"),
            "processes": S("proc_count"), "zombie_processes": S("proc_zombies"),
            "file_handles_used": S("fd_used"), "file_handles_ratio": pct(S("fd_pct")),
            "clock_offset_seconds": None if S("clock_offset_ms") is None else S("clock_offset_ms") / 1000,
            "ntp_synced": S("ntp_synced"), "temperature_max_celsius": S("temp_max_c"),
            "failed_logins_1h": S_sum("failed_logins"), "log_errors_1h": S_sum("sys_log_errors"),
            "log_warnings_1h": S_sum("sys_log_warnings"),
        },
        "filesystems": [{"mount": f.get("mount"), "fstype": f.get("fstype"), "used_bytes": f.get("used"),
                         "size_bytes": f.get("total"), "used_ratio": pct(f.get("pct")),
                         "inode_ratio": pct(f.get("inode_pct"))} for f in health.get("filesystems", [])],
        "alerts": [{k: a.get(k) for k in ("id", "severity", "title", "detail", "since")} for a in health.get("alerts", [])],
        "checks": {k: {kk: vv for kk, vv in v.items() if kk in ("status", "detail", "offset_ms", "synced", "ms", "security",
                                                                 "total", "reboot_required", "unexpected", "certs", "drives")}
                   for k, v in (health.get("checks") or {}).items()},
        "node_status": {
            "rpc_up": 0 if latest.get("error") else 1, "rpc_error": latest.get("error"),
            "synced": N("synced"), "syncing": 1 if latest.get("syncing") else 0,
            "block_height": num(latest.get("block_height")) or N("block_height"),
            "blocks_per_second": N("blocks_per_sec"), "block_lag_seconds": N("block_lag_sec"),
            "peers": N("peers"), "peers_max": N("max_peers"),
            "p2p_receive_bytes_per_second": N("near_recv_bps"), "p2p_transmit_bytes_per_second": N("near_sent_bps"),
            "rpc_latency_seconds": None if N("rpc_latency_ms") is None else N("rpc_latency_ms") / 1000,
            "version": latest.get("version"), "build": latest.get("build"),
            "protocol_version": num(latest.get("protocol_version")),
            "latest_protocol_version": num(latest.get("latest_protocol_version")),
            "node_uptime_seconds": num(latest.get("node_uptime_sec")),
        },
        "validator": {
            "status": latest.get("validator_status"),
            "active": 1 if (latest.get("validator_status") or "").startswith("Active") else 0,
            "in_next_epoch": None if latest.get("in_next_epoch") is None else int(bool(latest.get("in_next_epoch"))),
            "kickout_reason": latest.get("kickout_reason"),
            "rank": num(latest.get("rank")), "validators": N("num_validators"),
            "stake_near": N("stake"), "seat_price_near": N("seat_price"),
            "epoch_height": N("epoch_height"), "epoch_progress_ratio": pct(N("epoch_progress_pct")),
            "epoch_blocks_remaining": num(latest.get("epoch_blocks_remaining")),
            "uptime_ratio": pct(num(latest.get("uptime_pct"))),
            **{f"{k}_{x}": N(f"{k}_{x}") for k in ("blocks", "chunks", "endorsements") for x in ("produced", "expected")},
            "validators_data_stale": int(bool(latest.get("validators_stale"))),
            "shards": shard.get("shards") if shard.get("is_validator") else None,
        },
        "peers": {
            "active": peers.get("count"), "disconnected_recently": peers.get("disconnected"),
            "joins_1h": (peers.get("churn") or {}).get("joins"), "leaves_1h": (peers.get("churn") or {}).get("leaves"),
            "ping_median_seconds": None if last_value(ping, "median_ms") is None else last_value(ping, "median_ms") / 1000,
            "ping_p90_seconds": None if last_value(ping, "p90_ms") is None else last_value(ping, "p90_ms") / 1000,
            "ping_loss_ratio": pct(last_value(ping, "loss_pct")),
            "list": [{"name": p.get("account_id") or p.get("ip"), "account_id": p.get("account_id"), "ip": p.get("ip"),
                      "port": p.get("port"), "ping_avg_seconds": None if not (p.get("ping") or {}).get("avg") else p["ping"]["avg"] / 1000,
                      "ping_loss_ratio": pct((p.get("ping") or {}).get("loss_pct"))} for p in active_peers],
        },
        "logs_1h": {"warnings": (logs.get("counts") or {}).get("WARN"), "errors": (logs.get("counts") or {}).get("ERROR"),
                    "source_ok": (logs.get("status") or {}).get("ok")},
        "host_checks": [{"title": c.get("title"), "status": c.get("status"), "detail": c.get("detail")} for c in checks],
    }


# --------------------------------------------------------------------------- Prometheus format

def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus(snap, meta, per_peer=False):
    out, seen = [], set()
    base = {"node": snap.get("node") or "unknown", "chain": snap.get("chain_id") or "unknown"}

    def g(name, value, help_, labels=None):
        if value is None:
            return
        full = "near_node_" + name
        if full not in seen:
            seen.add(full)
            out.append(f"# HELP {full} {help_}")
            out.append(f"# TYPE {full} gauge")
        lab = {**base, **(labels or {})}
        out.append(f'{full}{{{",".join(f"{k}=\"{_esc(v)}\"" for k, v in lab.items())}}} {float(value):.10g}')

    s, n, v, p, lg = snap["system"], snap["node_status"], snap["validator"], snap["peers"], snap["logs_1h"]
    # exporter / freshness
    g("exporter_up", meta["up"], "1 if the last poll of the dashboard succeeded")
    g("exporter_last_update_timestamp_seconds", meta["last_success"], "Unix time of the last successful poll")
    g("exporter_next_update_timestamp_seconds", meta["next_update"], "Unix time of the next scheduled poll")
    g("exporter_update_interval_seconds", meta["interval"], "Seconds between polls of the dashboard")
    g("data_collected_timestamp_seconds", snap.get("data_collected_at"), "Unix time the dashboard last sampled the node")
    dv = snap.get("dashboard_version") or {}
    g("dashboard_info", 1, "Near Node Dashboard version", {"version": dv.get("version") or "",
                                                            "commit": dv.get("commit") or "", "date": dv.get("date") or ""})
    g("info", 1, "Node version information", {"version": n.get("version") or "", "build": n.get("build") or "",
                                               "account": snap.get("validator_account_id") or "",
                                               "hostname": snap.get("hostname") or ""})
    # system
    for key, help_ in [("cpu_ratio", "System CPU usage (0-1)"), ("load1", "1-minute load average"),
                       ("memory_used_bytes", "Memory used"), ("memory_total_bytes", "Total memory"),
                       ("memory_ratio", "Memory usage (0-1)"), ("swap_ratio", "Swap usage (0-1)"),
                       ("disk_used_bytes", "Disk used on the tracked mount"), ("disk_total_bytes", "Disk size of the tracked mount"),
                       ("disk_ratio", "Disk usage (0-1)"), ("disk_read_bytes_per_second", "Disk read throughput"),
                       ("disk_write_bytes_per_second", "Disk write throughput"),
                       ("network_receive_bytes_per_second", "Host network receive rate"),
                       ("network_transmit_bytes_per_second", "Host network transmit rate"),
                       ("neard_cpu_ratio", "neard CPU usage as a share of the machine (0-1)"),
                       ("neard_resident_bytes", "neard resident memory"), ("uptime_seconds", "System uptime"),
                       ("boot_time_seconds", "Unix time the system booted")]:
        g("system_" + key, s.get(key), help_)
    # node
    for key, help_ in [("rpc_up", "1 if the node's RPC answered"), ("synced", "Share of recent samples synced (0-1)"),
                       ("syncing", "1 if the node reports it is syncing"), ("block_height", "Latest block height"),
                       ("blocks_per_second", "Observed block rate"), ("block_lag_seconds", "Age of the node's latest block"),
                       ("peers", "Active peers"), ("peers_max", "Maximum peers"),
                       ("p2p_receive_bytes_per_second", "P2P receive rate"), ("p2p_transmit_bytes_per_second", "P2P transmit rate"),
                       ("rpc_latency_seconds", "Round trip of the /status call"), ("protocol_version", "Network protocol version"),
                       ("latest_protocol_version", "Newest protocol the binary supports"), ("node_uptime_seconds", "neard uptime")]:
        g(key if key.startswith(("rpc_", "block", "peers", "p2p", "protocol", "latest_", "node_")) else "node_" + key,
          n.get(key), help_)
    # validator
    for key, help_ in [("active", "1 if the validator is in the current set"), ("in_next_epoch", "1 if in the next epoch's set"),
                       ("rank", "Stake rank in the current set"), ("validators", "Validators in the current set"),
                       ("stake_near", "Validator stake in NEAR"), ("seat_price_near", "Lowest stake in the current set, in NEAR"),
                       ("epoch_height", "Current epoch height"), ("epoch_progress_ratio", "Epoch progress (0-1)"),
                       ("epoch_blocks_remaining", "Blocks left in the epoch"), ("uptime_ratio", "Average production ratio this epoch (0-1)"),
                       ("validators_data_stale", "1 if validator data hasn't refreshed recently")]:
        g("validator_" + key, v.get(key), help_)
    for kind in ("blocks", "chunks", "endorsements"):
        g(f"validator_{kind}_produced", v.get(f"{kind}_produced"), f"{kind.capitalize()} produced this epoch")
        g(f"validator_{kind}_expected", v.get(f"{kind}_expected"), f"{kind.capitalize()} expected this epoch")
    # peers
    for key, help_ in [("active", "Active peers"), ("disconnected_recently", "Peers that disconnected in the last hour"),
                       ("joins_1h", "Peer joins in the last hour"), ("leaves_1h", "Peer leaves in the last hour"),
                       ("ping_median_seconds", "Median peer round-trip time"), ("ping_p90_seconds", "90th percentile peer round-trip time"),
                       ("ping_loss_ratio", "Share of peer pings with no reply (0-1)")]:
        g("peers_" + key, p.get(key), help_)
    if per_peer:
        for peer in p.get("list", []):
            labels = {"peer": peer.get("name") or "", "ip": peer.get("ip") or ""}
            g("peer_ping_seconds", peer.get("ping_avg_seconds"), "Average round-trip time to a peer (last hour)", labels)
            g("peer_ping_loss_ratio", peer.get("ping_loss_ratio"), "Share of pings to a peer with no reply (0-1)", labels)
    # extended system metrics (USE)
    extra_help = {
        "cpu_user_ratio": "CPU time in user space (0-1)", "cpu_system_ratio": "CPU time in the kernel (0-1)",
        "cpu_iowait_ratio": "CPU time waiting on I/O (0-1)", "cpu_steal_ratio": "CPU time taken by the hypervisor (0-1)",
        "cpu_cores": "Logical CPU cores", "load_per_core": "1-minute load average per core", "load5": "5-minute load average",
        "load15": "15-minute load average", "procs_running": "Runnable processes (run queue)",
        "procs_blocked": "Processes blocked on I/O", "context_switches_per_second": "Context switches per second",
        "interrupts_per_second": "Interrupts per second", "memory_available_bytes": "Memory available for new work",
        "swap_used_bytes": "Swap in use", "swap_in_bytes_per_second": "Swap-in rate", "swap_out_bytes_per_second": "Swap-out rate",
        "oom_kills_1h": "Processes killed by the OOM killer in the last hour", "disk_read_iops": "Disk reads per second",
        "disk_write_iops": "Disk writes per second", "disk_await_seconds": "Average disk request latency",
        "disk_busy_ratio": "Busiest disk's busy time (0-1)", "disk_queue_depth": "Average disk requests in flight",
        "filesystem_max_ratio": "Fullest filesystem's space use (0-1)", "inode_max_ratio": "Highest inode use across filesystems (0-1)",
        "network_link_ratio": "Busiest interface's use of its link speed (0-1)", "network_errors_per_second": "Packet errors per second",
        "network_drops_per_second": "Packets dropped per second", "tcp_retransmit_ratio": "Share of TCP segments retransmitted (0-1)",
        "tcp_established": "TCP sockets in ESTABLISHED", "tcp_time_wait": "TCP sockets in TIME_WAIT",
        "tcp_close_wait": "TCP sockets in CLOSE_WAIT", "tcp_connections": "TCP sockets (excluding listeners)",
        "dns_lookup_seconds": "DNS resolution time", "dns_ok": "1 if the last DNS lookup succeeded",
        "processes": "Processes", "zombie_processes": "Zombie processes", "file_handles_used": "Open file handles (system)",
        "file_handles_ratio": "Open file handles vs the system limit (0-1)", "clock_offset_seconds": "System clock offset from NTP",
        "ntp_synced": "1 if the clock is NTP-synchronised", "temperature_max_celsius": "Hottest hardware sensor",
        "failed_logins_1h": "Failed SSH logins in the last hour", "log_errors_1h": "System journal errors in the last hour",
        "log_warnings_1h": "System journal warnings in the last hour"}
    for key, help_ in extra_help.items():
        g("system_" + key, s.get(key), help_)
    for f in snap.get("filesystems", []):
        lab = {"mount": f.get("mount") or "", "fstype": f.get("fstype") or ""}
        g("filesystem_used_bytes", f.get("used_bytes"), "Space used per filesystem", lab)
        g("filesystem_size_bytes", f.get("size_bytes"), "Size per filesystem", lab)
        g("filesystem_used_ratio", f.get("used_ratio"), "Space use per filesystem (0-1)", lab)
        g("filesystem_inode_ratio", f.get("inode_ratio"), "Inode use per filesystem (0-1)", lab)
    ck = snap.get("checks", {})
    for cert in (ck.get("tls") or {}).get("certs") or []:
        g("tls_cert_days_left", cert.get("days_left"), "Days until a TLS certificate expires", {"host": cert.get("host") or ""})
    pt = ck.get("patches") or {}
    g("security_updates_pending", pt.get("security"), "Security updates waiting to be installed")
    g("updates_pending", pt.get("total"), "Package updates waiting to be installed")
    if pt.get("reboot_required") is not None:
        g("reboot_required", int(bool(pt.get("reboot_required"))), "1 if a reboot is needed to finish updates")
    drives = (ck.get("smart") or {}).get("drives") or []
    for d in drives:
        if d.get("passed") is not None:
            g("smart_healthy", int(bool(d["passed"])), "1 if a drive's SMART self-assessment passed", {"device": d.get("device") or ""})
    if (ck.get("last_boot") or {}).get("unexpected") is not None:
        g("last_reboot_unexpected", int(bool(ck["last_boot"]["unexpected"])), "1 if the previous boot had no clean shutdown")
    # alerts (symptom-based, evaluated by the dashboard)
    alerts = snap.get("alerts", [])
    for sev in ("critical", "warning", "info"):
        g("alerts_active", sum(1 for a in alerts if a.get("severity") == sev), "Active alerts by severity", {"severity": sev})
    for a in alerts:
        g("alert", 1, "An active alert (1 while it lasts)", {"alert": a.get("id") or "", "severity": a.get("severity") or "",
                                                             "title": a.get("title") or ""})
    # logs, host checks
    g("log_warnings_1h", lg.get("warnings"), "neard WARN lines in the last hour")
    g("log_errors_1h", lg.get("errors"), "neard ERROR lines in the last hour")
    for c in snap.get("host_checks", []):
        if c.get("status") in ("pass", "warn", "fail"):
            g("host_check_ok", 1 if c["status"] == "pass" else 0, "1 if a startup host check passed",
              {"check": c.get("title") or ""})
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- poller

class Exporter:
    def __init__(self, dashboard, interval, per_peer):
        self.dashboard = dashboard.rstrip("/")
        self.interval_setting = interval  # "auto" or seconds
        self.per_peer = per_peer
        self.lock = threading.Lock()
        self.snapshot = None
        self.meta = {"up": 0, "last_success": None, "last_attempt": None, "next_update": None,
                     "interval": MIN_INTERVAL, "error": "not polled yet"}
        self.body_json, self.body_prom = b"{}", b""

    def interval_for(self, snap):
        if self.interval_setting != "auto":
            return self.interval_setting
        coll = (snap or {}).get("collection_interval_seconds") or 60
        return int(min(MAX_INTERVAL, max(MIN_INTERVAL, coll)))

    def poll_once(self):
        now = time.time()
        try:
            snap = build_snapshot(self.dashboard)
            interval = self.interval_for(snap)
            meta = {"up": 1, "last_success": now, "last_attempt": now, "next_update": now + interval,
                    "interval": interval, "error": None}
        except Exception as e:
            snap = self.snapshot
            interval = self.interval_for(snap)
            meta = {**self.meta, "up": 0, "last_attempt": now, "next_update": now + min(interval, 30),
                    "interval": interval, "error": f"can't read the dashboard at {self.dashboard}: {e}"}
        body = {"exporter": {"version": VERSION, "up": meta["up"], "error": meta["error"],
                             "last_updated": meta["last_success"], "last_attempt": meta["last_attempt"],
                             "next_update": meta["next_update"], "interval_seconds": meta["interval"]},
                **(snap or {})}
        with self.lock:
            self.snapshot, self.meta = snap, meta
            self.body_json = json.dumps(body).encode()
            self.body_prom = prometheus(snap, meta, self.per_peer).encode() if snap else prometheus(
                {"system": {}, "node_status": {}, "validator": {}, "peers": {}, "logs_1h": {}, "host_checks": []},
                meta).encode()
        return meta

    def run(self, stop):
        """Poll on schedule (main() has already taken the first snapshot)."""
        while not stop.wait(max(1, (self.meta["next_update"] or 0) - time.time())):
            meta = self.poll_once()
            if meta["error"]:
                print(f"[exporter] {meta['error']}", flush=True)


# --------------------------------------------------------------------------- HTTP

STATUS_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{node} · Near Node exporter</title>
<style>
:root {{ color-scheme: light dark; --bg:#f9f9f7; --card:#fcfcfb; --text:#0b0b0b; --muted:#52514e; --border:rgba(11,11,11,.1); --good:#0ca30c; --bad:#d03b3b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0d0d0d; --card:#1a1a19; --text:#f0efec; --muted:#c3c2b7; --border:rgba(255,255,255,.1); }} }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
.wrap {{ max-width:760px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:20px; margin:0 0 4px; }} .sub {{ color:var(--muted); margin-bottom:20px; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; margin-bottom:12px; }}
.row {{ display:flex; justify-content:space-between; gap:12px; padding:4px 0; }} .row span:first-child {{ color:var(--muted); }}
.big {{ font-size:22px; font-weight:650; }} code {{ font-size:12px; }} a {{ color:inherit; }}
.dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }}
pre {{ background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:10px; overflow:auto; font-size:12px; }}
</style></head><body><div class="wrap">
<h1>Near Node exporter · {node}</h1>
<div class="sub">{chain} · read-only snapshot of the Near Node Dashboard for central scraping</div>
<div class="card">
  <div class="row"><span>Status</span><span><span class="dot" style="background:var(--{status_color})"></span>{status}</span></div>
  <div class="row"><span>Last updated</span><span id="last" data-t="{last_success}">{last_text}</span></div>
  <div class="row"><span>Next update in</span><span class="big" id="next" data-t="{next_update}">—</span></div>
  <div class="row"><span>Update interval</span><span>{interval}s</span></div>
  <div class="row"><span>Node data collected</span><span id="coll" data-t="{collected}">—</span></div>
  {error_row}
</div>
<div class="card">
  <div class="row"><span>Block height</span><span>{height}</span></div>
  <div class="row"><span>Validator</span><span>{vstatus}</span></div>
  <div class="row"><span>Peers</span><span>{peers}</span></div>
</div>
<div class="card">
  <b>Endpoints</b>
  <div class="row"><span><a href="metrics{tq}">/metrics</a></span><span>Prometheus text format</span></div>
  <div class="row"><span><a href="api/v1/snapshot{tq}">/api/v1/snapshot</a></span><span>JSON</span></div>
  <div class="row"><span>/healthz</span><span>200 when the last poll succeeded (no token)</span></div>
  <p style="color:var(--muted);margin:12px 0 6px">Example Prometheus scrape job:</p>
<pre>- job_name: near-node
  scrape_interval: {interval}s
  {auth}static_configs:
    - targets: ["{host}"]</pre>
</div></div>
<script>
function ago(t) {{ const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? Math.round(s) + "s ago" : s < 3600 ? Math.round(s / 60) + "m ago" : (s / 3600).toFixed(1) + "h ago"; }}
function tick() {{
  const last = document.getElementById("last"), next = document.getElementById("next"), coll = document.getElementById("coll");
  if (+last.dataset.t) last.textContent = new Date(last.dataset.t * 1000).toLocaleString() + " (" + ago(+last.dataset.t) + ")";
  if (+coll.dataset.t) coll.textContent = new Date(coll.dataset.t * 1000).toLocaleTimeString() + " (" + ago(+coll.dataset.t) + ")";
  const left = Math.round(+next.dataset.t - Date.now() / 1000);
  next.textContent = !+next.dataset.t ? "—" : left > 0 ? left + "s" : "updating…";
  if (+next.dataset.t && left < -2) location.reload();   // fetch the new snapshot shortly after it's due
}}
tick(); setInterval(tick, 1000);
</script></body></html>"""


def make_handler(exp, token):
    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_SEC
        server_version = "NearNodeExporter"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, q):
            if not token:
                return True
            auth = self.headers.get("Authorization") or ""
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else (q.get("token") or [""])[0]
            return hmac.compare_digest(given.encode(), token.encode())

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/healthz":
                ok = exp.meta["up"] == 1
                return self._send(200 if ok else 503, "ok\n" if ok else (exp.meta["error"] or "down") + "\n",
                                  "text/plain; charset=utf-8")
            if not self._authorized(q):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer realm="near-node-exporter"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            with exp.lock:
                body_prom, body_json, snap, meta = exp.body_prom, exp.body_json, exp.snapshot or {}, dict(exp.meta)
            if u.path == "/metrics":
                return self._send(200, body_prom, "text/plain; version=0.0.4; charset=utf-8")
            if u.path == "/api/v1/snapshot":
                return self._send(200, body_json, "application/json")
            if u.path == "/":
                e = lambda v: html.escape(str(v))  # noqa: E731
                n, v, p = snap.get("node_status", {}), snap.get("validator", {}), snap.get("peers", {})
                tq = "?token=" + e((q.get("token") or [""])[0]) if (q.get("token") or [""])[0] else ""
                page = STATUS_PAGE.format(
                    node=e(snap.get("node", "unknown")), chain=e(snap.get("chain_id") or "unknown chain"),
                    status="Up" if meta["up"] else "Can't read the dashboard", status_color="good" if meta["up"] else "bad",
                    last_success=meta["last_success"] or 0, last_text="never" if not meta["last_success"] else "",
                    next_update=meta["next_update"] or 0, interval=e(meta["interval"]),
                    collected=snap.get("data_collected_at") or 0,
                    error_row=f'<div class="row"><span>Error</span><span>{e(meta["error"])}</span></div>' if meta["error"] else "",
                    height=e(f"{int(n['block_height']):,}" if n.get("block_height") else "—"),
                    vstatus=e(v.get("status") or "—"),
                    peers=e(f"{int(p['active'])}" if isinstance(p.get("active"), (int, float)) else "—"),
                    tq=tq, auth="authorization:\n    credentials: \"&lt;token&gt;\"\n  " if token else "",
                    host=e(self.headers.get("Host") or f"<this-server>:{self.server.server_address[1]}"))
                return self._send(200, page, "text/html; charset=utf-8")
            return self._send(404, "not found\n", "text/plain; charset=utf-8")

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dashboard", default="http://127.0.0.1:8787", help="the dashboard's address (default %(default)s)")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=9787)
    ap.add_argument("--interval", default="auto",
                    help=f"seconds between polls, or 'auto' = the dashboard's collection interval "
                         f"(clamped to {MIN_INTERVAL}-{MAX_INTERVAL}s)")
    ap.add_argument("--token-file", help="file holding the access token; requests then need "
                                         "'Authorization: Bearer <token>' (or ?token=)")
    ap.add_argument("--per-peer", action="store_true", help="also export per-peer ping metrics (one series per peer)")
    args = ap.parse_args()

    interval = args.interval
    if interval != "auto":
        interval = int(interval)
        if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
            ap.error(f"--interval must be 'auto' or {MIN_INTERVAL}-{MAX_INTERVAL}")
    token = None
    if args.token_file:
        with open(args.token_file) as f:
            token = f.read().strip()
        if len(token) < 16:
            ap.error("the token in --token-file must be at least 16 characters")

    exp = Exporter(args.dashboard, interval, args.per_peer)
    stop = threading.Event()
    exp.poll_once()  # first snapshot before serving
    threading.Thread(target=exp.run, args=(stop,), daemon=True).start()
    server = make_server(args.host, args.port, make_handler(exp, token))
    print(f"Near Node exporter listening on {args.host}:{args.port}, reading {args.dashboard} "
          f"(interval {interval}, token {'required' if token else 'NOT set: anyone who can reach the port can read it'})",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
