#!/usr/bin/env python3
"""Near Node Dashboard — collects system + NEAR node metrics and serves a local dashboard.

Run:  python3 server.py [--host 0.0.0.0] [--port 8787] [--config config.json]
Then open http://<this-machine's-ip>:8787 (or http://127.0.0.1:8787 locally)
"""

import argparse
import contextlib
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import queue
from collections import OrderedDict

import psutil

import health

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "1.0.0"
REPO_URL = "https://github.com/Nobody-Inc/NearNodeDashboard"
STATIC_DIR = os.path.join(BASE_DIR, "static")

DEFAULT_CONFIG = {
    # Normal sampling interval in seconds (default: every minute).
    "interval_seconds": 60,
    # High-frequency mode: sample every second, overriding interval_seconds.
    "fast_mode": False,
    # How long to keep samples.
    "retention_hours": 24,
    # NEAR RPC endpoint used for /status, /network_info and JSON-RPC calls.
    # For a validator, point this at your own node (e.g. http://127.0.0.1:3030).
    "rpc_url": "http://127.0.0.1:3030",
    # Optional separate endpoint for the (large) `validators` JSON-RPC call. Blank = rpc_url.
    "validators_rpc_url": "",
    # How often to call `validators` (~400 KB on mainnet: it returns every validator, so keep it modest).
    "validators_interval_seconds": 120,
    # Validator pool to track. Blank = use validator_account_id reported by the node's /status.
    "validator_account_id": "",
    # Filesystem path whose disk usage is tracked (point it at your NEAR data dir's mount).
    "disk_path": "/",
    # Process name used to find the NEAR client for per-process CPU / memory.
    "process_name": "neard",
    # Per-section data collection switches. Turning one off stops gathering (and the RPC calls
    # only that section needs); existing data stays until retention removes it.
    "collect_system": True,
    "collect_node": True,
    "collect_validator": True,  # also feeds the "validators on your shard" table
    "collect_peers": True,      # peer tracking, churn and pings
    "collect_logs": True,       # WARN/ERROR lines from the neard service log
    "collect_health": True,     # slower health/security checks: DNS, clock, logins, log rates, SMART, patches, TLS
    # Hostname whose DNS resolution time is measured (every minute).
    "dns_check_host": "near.org",
    # Extra HTTPS endpoints whose certificate expiry is checked, comma-separated host[:port].
    # HTTPS RPC URLs are always checked.
    "tls_check_hosts": "",
    # Use `sudo -n` for SMART (smartctl) and IPMI (ipmitool) checks. Only enable it with passwordless
    # sudo for those commands; otherwise every attempt is logged as a failed sudo login.
    "health_checks_sudo": False,
    # Lock the settings: when true, nothing can be changed from the web page (Settings dialog,
    # 1s-mode toggle). Can only be set by editing this file on the server; takes effect on restart.
    "read_only": False,
    # Where to read the neard log: "journald" (systemd unit) or "file" (a log file path; plain
    # neard output or Docker json-file logs).
    "log_source": "journald",
    "log_unit": "neard",
    "log_file": "",
    "log_poll_seconds": 10,
    # Peer pinging: every active peer is pinged once per round.
    "ping_enabled": True,
    # "icmp" uses the system `ping` command; "tcp" times a TCP connect to the peer's NEAR port
    # (use it if ICMP is blocked).
    "ping_method": "icmp",
    # Seconds between ping rounds. Independent of fast_mode so peers are not pinged every second.
    "ping_interval_seconds": 60,
    "ping_timeout_ms": 2000,
    # neard's P2P port (network.addr in ~/.near/config.json); checked for inbound firewall access.
    "p2p_port": 24567,
    # Retry firewall-rule reads with `sudo -n`. Off by default: for a user without sudo rights each
    # attempt is logged by PAM as a failed authentication. Only enable it if the service user has
    # passwordless sudo for ufw/firewall-cmd/nft/iptables.
    "firewall_check_sudo": False,
    "db_path": "data/stats.db",
}

PING_COLS = ["peers", "replied", "median_ms", "p90_ms", "loss_pct"]

SYSTEM_COLS = [
    "cpu_pct", "load1", "mem_pct", "mem_used", "mem_total", "swap_pct",
    "disk_pct", "disk_used", "disk_total", "disk_read_bps", "disk_write_bps",
    "net_sent_bps", "net_recv_bps", "proc_cpu_pct", "proc_rss",
    "uptime_sec", "boot_time",
    # extended USE metrics (health.ExtendedStats) and slow checks (health.HealthMonitor)
    "cpu_user", "cpu_system", "cpu_iowait", "cpu_steal", "cpu_cores", "load5", "load15", "load_per_core",
    "procs_running", "procs_blocked", "ctx_switches_ps", "interrupts_ps",
    "mem_available", "swap_used", "swap_in_bps", "swap_out_bps", "oom_kills",
    "disk_read_iops", "disk_write_iops", "disk_await_ms", "disk_util_pct", "disk_queue", "fs_max_pct", "inode_max_pct",
    "net_util_pct", "net_errors_ps", "net_drops_ps", "tcp_retrans_ps", "tcp_retrans_pct",
    "tcp_established", "tcp_time_wait", "tcp_close_wait", "tcp_total", "dns_ms", "dns_ok",
    "proc_count", "proc_zombies", "fd_used", "fd_max", "fd_pct", "clock_offset_ms", "ntp_synced", "temp_max_c",
    "failed_logins", "sys_log_errors", "sys_log_warnings",
]

NEAR_COLS = [
    "ok", "synced", "rpc_latency_ms", "block_height", "blocks_per_sec", "block_lag_sec", "syncing",
    "peers", "max_peers", "peer_joins", "peer_leaves", "near_sent_bps", "near_recv_bps",
    "protocol_version", "latest_protocol_version", "epoch_start_height",
    "epoch_height", "epoch_progress_pct", "num_validators", "seat_price", "stake",
    "blocks_produced", "blocks_expected", "chunks_produced", "chunks_expected",
    "endorsements_produced", "endorsements_expected",
    "blocks_pct", "chunks_pct", "endorsements_pct",
]

# Which section each NEAR column belongs to (for the per-section collection switches).
NODE_COLS = {"ok", "synced", "rpc_latency_ms", "block_height", "blocks_per_sec", "block_lag_sec", "syncing",
             "peers", "max_peers", "near_sent_bps", "near_recv_bps", "protocol_version", "latest_protocol_version",
             "epoch_start_height"}
PEER_COLS = {"peer_joins", "peer_leaves"}
VALIDATOR_COLS = set(NEAR_COLS) - NODE_COLS - PEER_COLS

# Aggregation used when down-sampling into time buckets.
MAX_AGG = {"block_height", "epoch_height", "disk_total", "mem_total", "max_peers",
           "blocks_produced", "blocks_expected", "chunks_produced", "chunks_expected",
           "endorsements_produced", "endorsements_expected", "epoch_progress_pct",
           "protocol_version", "latest_protocol_version", "epoch_start_height", "uptime_sec", "boot_time",
           "cpu_cores", "fd_max", "fs_max_pct", "inode_max_pct", "temp_max_c", "proc_zombies"}
MIN_AGG = {"ok", "dns_ok", "ntp_synced"}
# event counts: a bucket's value is the total within it
SUM_AGG = {"peer_joins", "peer_leaves", "oom_kills", "failed_logins", "sys_log_errors", "sys_log_warnings"}

# A node counts as "synced" when RPC answers, it isn't syncing, and its head block is this fresh.
SYNC_LAG_THRESHOLD_SEC = 30


# --------------------------------------------------------------------------- config

class Config:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.changed = threading.Event()
        self.data = dict(DEFAULT_CONFIG)
        if os.path.exists(path):
            with open(path) as f:
                loaded = json.load(f)
            # Validate each setting from the file; a bad value falls back to its default
            # instead of crashing a collector thread later.
            for key, value in loaded.items():
                if key == "db_path" and isinstance(value, str) and value.strip():
                    self.data["db_path"] = value
                    continue
                try:
                    self.data.update(validate_config_patch({key: value}, from_file=True))
                except (ValueError, TypeError) as e:
                    if key == "read_only":
                        # Fail closed: a typo like "ture" must not silently unlock the settings.
                        self.data["read_only"] = True
                        print(f"[config] invalid read_only value {value!r} in {path}: {e}; treating it as "
                              f"true (settings locked). Use true or false.", flush=True)
                        continue
                    print(f"[config] ignoring invalid {key!r} in {path}: {e}; using default "
                          f"{DEFAULT_CONFIG.get(key)!r}", flush=True)
        else:
            self.save()

    def get(self, key):
        with self.lock:
            return self.data[key]

    def snapshot(self):
        with self.lock:
            return dict(self.data)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def update(self, patch):
        """Apply a settings change from the web page. Refused entirely in read-only mode."""
        with self.lock:
            if self.data.get("read_only"):
                raise PermissionError(READ_ONLY_MESSAGE)
        clean = validate_config_patch(patch)
        with self.lock:
            if self.data.get("read_only"):  # re-check under the same lock as the write
                raise PermissionError(READ_ONLY_MESSAGE)
            self.data.update(clean)
            self.save()
        DATA_GEN.bump()
        self.changed.set()
        return self.snapshot()

    @property
    def interval(self):
        with self.lock:
            return 1 if self.data["fast_mode"] else max(1, int(self.data["interval_seconds"]))


def to_bool(value):
    """Strict boolean parsing: the string "false" must not become True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off", ""):
        return False
    raise ValueError(f"expected true/false, got {value!r}")


def num(v):
    """Coerce an RPC value to a float; anything non-numeric (incl. bool, str) becomes None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


READ_ONLY_MESSAGE = ("Settings are read-only: \"read_only\" is enabled in config.json on the server. "
                     "To change settings, set it to false there and restart the dashboard.")


def validate_config_patch(patch, from_file=False):
    """Validate settings. `from_file` is True when loading config.json: only then may
    read_only be set, so the web page can never unlock itself."""
    if not isinstance(patch, dict):
        raise ValueError("settings must be a JSON object")
    out = {}
    for key, value in patch.items():
        if key not in DEFAULT_CONFIG or key == "db_path":
            continue
        if key == "read_only":
            if not from_file:
                raise ValueError("read_only can only be changed by editing config.json on the server")
            out[key] = to_bool(value)
            continue
        if key in ("interval_seconds", "validators_interval_seconds"):
            value = int(value)
            if not 1 <= value <= 86400:
                raise ValueError(f"{key} must be between 1 and 86400")
        elif key == "ping_interval_seconds":
            value = int(value)
            if not 5 <= value <= 86400:
                raise ValueError("ping_interval_seconds must be between 5 and 86400")
        elif key == "ping_timeout_ms":
            value = int(value)
            if not 100 <= value <= 10000:
                raise ValueError("ping_timeout_ms must be between 100 and 10000")
        elif key == "ping_method":
            value = str(value).strip().lower()
            if value not in ("icmp", "tcp"):
                raise ValueError("ping_method must be 'icmp' or 'tcp'")
        elif key in ("ping_enabled", "firewall_check_sudo", "health_checks_sudo") or key.startswith("collect_"):
            value = to_bool(value)
        elif key == "dns_check_host":
            value = str(value).strip().lower()
            if not re.match(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", value):
                raise ValueError("dns_check_host must be a hostname like near.org")
        elif key == "tls_check_hosts":
            items = [i.strip().lower() for i in str(value).split(",") if i.strip()]
            for i in items:
                if not re.match(r"^[a-z0-9.-]+(:\d{1,5})?$", i):
                    raise ValueError(f"tls_check_hosts: {i!r} isn't host or host:port")
            if len(items) > 20:
                raise ValueError("tls_check_hosts: at most 20 hosts")
            value = ",".join(items)
        elif key == "log_source":
            value = str(value).strip().lower()
            if value not in ("journald", "file"):
                raise ValueError("log_source must be 'journald' or 'file'")
        elif key == "p2p_port":
            value = int(value)
            if not 1 <= value <= 65535:
                raise ValueError("p2p_port must be between 1 and 65535")
        elif key == "log_poll_seconds":
            value = int(value)
            if not 1 <= value <= 3600:
                raise ValueError("log_poll_seconds must be between 1 and 3600")
        elif key == "retention_hours":
            value = float(value)
            if not 0.05 <= value <= 24 * 365:
                raise ValueError("retention_hours must be between 0.05 and 8760")
        elif key == "fast_mode":
            value = to_bool(value)
        elif key in ("rpc_url", "validators_rpc_url"):
            value = str(value).strip().rstrip("/")
            if value and not value.startswith(("http://", "https://")):
                raise ValueError(f"{key} must start with http:// or https://")
            if key == "rpc_url" and not value:
                raise ValueError("rpc_url is required")
        elif key == "disk_path":
            value = str(value).strip() or "/"
            if not os.path.exists(value):
                raise ValueError(f"disk_path {value!r} does not exist")
        else:
            value = str(value).strip()
        out[key] = value
    return out


# --------------------------------------------------------------------------- storage

def db_connect(path, shared=False):
    # shared=True: a pooled connection used by one request thread at a time (see DBPool).
    conn = sqlite3.connect(path, timeout=10, check_same_thread=not shared)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class DataGeneration:
    """Counter bumped whenever stored data changes. Cached API responses are keyed on it, so a
    response is reused only until new data arrives (see ResponseCache)."""

    def __init__(self):
        self._n = 0
        self._lock = threading.Lock()

    def bump(self):
        with self._lock:
            self._n += 1

    @property
    def value(self):
        return self._n


DATA_GEN = DataGeneration()


class ResponseCache:
    """Small LRU cache of computed API responses. An entry is reused while the data generation is
    unchanged and it is younger than `max_age` (so time-based fields like "now" stay fresh)."""

    def __init__(self, max_entries=48, max_age=30):
        self._d = OrderedDict()
        self._lock = threading.Lock()
        self.max_entries, self.max_age = max_entries, max_age

    def get_or_compute(self, key, compute):
        gen, now = DATA_GEN.value, time.time()
        with self._lock:
            hit = self._d.get(key)
            if hit and hit[0] == gen and now - hit[1] < self.max_age:
                self._d.move_to_end(key)
                return hit[2]
        value = compute()  # outside the lock; concurrent misses may both compute, which is harmless
        with self._lock:
            self._d[key] = (gen, now, value)
            self._d.move_to_end(key)
            while len(self._d) > self.max_entries:
                self._d.popitem(last=False)
        return value


CACHE = ResponseCache()


class DBPool:
    """Reuses SQLite connections across HTTP requests instead of opening (and re-running the
    PRAGMAs on) a new one per request. Each connection is used by one request at a time."""

    def __init__(self, path, size=8):
        self.path, self.size = path, size
        self._q = queue.LifoQueue()

    @contextlib.contextmanager
    def conn(self):
        try:
            c = self._q.get_nowait()
        except queue.Empty:
            c = db_connect(self.path, shared=True)
        try:
            yield c
        except Exception:
            c.close()
            raise
        else:
            if self._q.qsize() < self.size:
                self._q.put(c)
            else:
                c.close()


def version_info():
    """App version plus the git commit it was installed from. install.sh writes VERSION.json
    (the install folder has no .git); a copy run straight from a git clone asks git."""
    info = {"version": APP_VERSION, "commit": None, "date": None, "modified": False, "repo": REPO_URL}
    try:
        with open(os.path.join(BASE_DIR, "VERSION.json")) as f:
            data = json.load(f)
        info.update({k: data.get(k) for k in ("commit", "date", "modified") if k in data})
        return info
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(["git", "-C", BASE_DIR, "log", "-1", "--format=%h %cs"], capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            info["commit"], info["date"] = out.stdout.split()[:2]
            dirty = subprocess.run(["git", "-C", BASE_DIR, "status", "--porcelain", "--untracked-files=no"],
                                   capture_output=True, text=True, timeout=5)
            info["modified"] = bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return info


VERSION_INFO = None   # filled at startup by main()


def db_init(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = db_connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS peer_pings (ts REAL, ip TEXT, peer_id TEXT, rtt_ms REAL)")
    conn.execute("CREATE INDEX IF NOT EXISTS peer_pings_ip_ts ON peer_pings (ip, ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS peer_pings_ts ON peer_pings (ts)")
    # One row per peer ever seen; `connected` is the state as of the latest /network_info.
    conn.execute("""CREATE TABLE IF NOT EXISTS peers_seen (
        peer_id TEXT PRIMARY KEY, ip TEXT, port TEXT, account_id TEXT,
        first_seen REAL, last_seen REAL, connected INTEGER, connected_since REAL)""")
    conn.execute("CREATE TABLE IF NOT EXISTS peer_events (ts REAL, peer_id TEXT, ip TEXT, account_id TEXT, event TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS peer_events_ts ON peer_events (ts)")
    # WARN/ERROR lines from the neard log. `sig` is the message with numbers/hashes/IPs
    # normalised away, used to group repeats.
    conn.execute("CREATE TABLE IF NOT EXISTS node_logs (ts REAL, level TEXT, target TEXT, message TEXT, sig TEXT)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS node_logs_dedupe ON node_logs (ts, level, message)")
    conn.execute("CREATE INDEX IF NOT EXISTS node_logs_ts ON node_logs (ts)")
    # Lets the grouped view find each group's latest example without scanning the table.
    conn.execute("CREATE INDEX IF NOT EXISTS node_logs_sig ON node_logs (sig, level, ts)")
    # One row per change of neard version/build or protocol numbers (never pruned; it's tiny).
    conn.execute("""CREATE TABLE IF NOT EXISTS version_history (
        ts REAL, version TEXT, build TEXT, protocol_version INTEGER, latest_protocol_version INTEGER)""")
    for table, cols in (("system", SYSTEM_COLS), ("near", NEAR_COLS), ("ping_rounds", PING_COLS)):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (ts REAL PRIMARY KEY)")
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for c in cols:
            if c not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {c} REAL")
    conn.commit()
    conn.close()


def db_insert(conn, table, cols, ts, row):
    names = ["ts"] + cols
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
        [ts] + [row.get(c) for c in cols],
    )
    conn.commit()
    DATA_GEN.bump()


def version_changes(conn, since):
    """Version history rows inside the range, preceded by the row in effect when the range starts."""
    cols = ("ts", "version", "build", "protocol_version", "latest_protocol_version")
    before = conn.execute("SELECT * FROM version_history WHERE ts < ? ORDER BY ts DESC LIMIT 1", (since,)).fetchall()
    inside = conn.execute("SELECT * FROM version_history WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    return [dict(zip(cols, r)) | {"before_range": i < len(before)} for i, r in enumerate(before + inside)]


def epoch_marks(conn, since):
    """When each new epoch started within the range: the first sample that saw a new epoch start
    height (from /status, collected with the NEAR node section), falling back to the validator data's
    epoch number for older history. The earliest epoch in the range started before it (or before
    collection began), so it isn't a boundary."""
    rows = []
    for col in ("epoch_start_height", "epoch_height"):
        rows = conn.execute(f"SELECT {col}, MIN(ts), MAX(epoch_height) FROM near WHERE ts >= ? AND {col} IS NOT NULL "
                            f"GROUP BY {col} ORDER BY MIN(ts)", (since,)).fetchall()
        if len(rows) > 1:
            break
    return [{"ts": ts, "epoch": int(ep) if isinstance(ep, (int, float)) else None,
             "start_height": int(key) if col == "epoch_start_height" and isinstance(key, (int, float)) else None}
            for key, ts, ep in rows[1:]]


def db_query(conn, table, cols, since, bucket):
    aggs = []
    for c in cols:
        fn = "MAX" if c in MAX_AGG else "MIN" if c in MIN_AGG else "SUM" if c in SUM_AGG else "AVG"
        aggs.append(f"{fn}({c})")
    sql = (f"SELECT CAST(ts / ? AS INTEGER) * ? AS t, {','.join(aggs)} FROM {table} "
           f"WHERE ts >= ? GROUP BY t ORDER BY t")
    rows = conn.execute(sql, (bucket, bucket, since)).fetchall()
    out = {"t": [r[0] for r in rows]}
    for i, c in enumerate(cols):
        # Non-numeric values (e.g. text stored from a misbehaving RPC) become gaps instead of errors.
        out[c] = [round(r[i + 1], 4) if isinstance(r[i + 1], (int, float)) else None for r in rows]
    return out


# --------------------------------------------------------------------------- collectors

class SystemCollector:
    def __init__(self, config):
        self.config = config
        self.prev_disk = None
        self.prev_net = None
        self.prev_t = None
        self.proc = None
        self.first = True
        self.ext = health.ExtendedStats()
        self.monitor = None   # health.HealthMonitor, set by main()

    def _find_proc(self):
        name = self.config.get("process_name")
        if not name:
            return None
        if self.proc is not None:
            try:
                if self.proc.is_running() and self.proc.name() == name:
                    return self.proc
            except psutil.Error:
                pass
            self.proc = None
        for p in psutil.process_iter(["name"]):
            if p.info.get("name") == name:
                self.proc = p
                p.cpu_percent(None)  # prime; first reading is meaningless
                return None
        return None

    def sample(self):
        now = time.time()
        row = {}
        if self.first:
            # cpu_percent(None) measures since the previous call; with no previous call it
            # returns a meaningless 0.0, so measure the first sample over one second.
            row["cpu_pct"] = psutil.cpu_percent(interval=1.0)
            self.first = False
            now = time.time()
        else:
            row["cpu_pct"] = psutil.cpu_percent(interval=None)
        boot = psutil.boot_time()
        row["boot_time"] = round(boot)  # rounded: some systems report sub-second jitter
        row["uptime_sec"] = max(0.0, now - boot)
        try:
            row["load1"] = os.getloadavg()[0]
        except OSError:
            pass
        vm = psutil.virtual_memory()
        row.update(mem_pct=vm.percent, mem_used=vm.total - vm.available, mem_total=vm.total)
        row["swap_pct"] = psutil.swap_memory().percent
        try:
            du = psutil.disk_usage(self.config.get("disk_path"))
            row.update(disk_pct=du.percent, disk_used=du.used, disk_total=du.total)
        except OSError:
            pass

        disk = psutil.disk_io_counters()
        net_bytes = None
        nics = psutil.net_io_counters(pernic=True)
        if nics:
            sent = sum(v.bytes_sent for k, v in nics.items() if not k.startswith("lo"))
            recv = sum(v.bytes_recv for k, v in nics.items() if not k.startswith("lo"))
            net_bytes = (sent, recv)
        if self.prev_t is not None:
            dt = max(now - self.prev_t, 1e-6)
            if disk and self.prev_disk:
                row["disk_read_bps"] = max(0, disk.read_bytes - self.prev_disk.read_bytes) / dt
                row["disk_write_bps"] = max(0, disk.write_bytes - self.prev_disk.write_bytes) / dt
            if net_bytes and self.prev_net:
                row["net_sent_bps"] = max(0, net_bytes[0] - self.prev_net[0]) / dt
                row["net_recv_bps"] = max(0, net_bytes[1] - self.prev_net[1]) / dt
        self.prev_disk, self.prev_net, self.prev_t = disk, net_bytes, now

        proc = self._find_proc()
        if proc is not None:
            try:
                with proc.oneshot():
                    # psutil reports per-core %; normalise to % of the whole machine.
                    row["proc_cpu_pct"] = proc.cpu_percent(None) / (psutil.cpu_count() or 1)
                    row["proc_rss"] = proc.memory_info().rss
            except psutil.Error:
                self.proc = None
        try:
            row.update({k: v for k, v in self.ext.sample(now).items() if v is not None})
        except Exception as e:  # extended metrics are best-effort; never lose the basic sample
            print(f"[system] extended stats error: {e}", flush=True)
        if self.monitor is not None and self.config.get("collect_health"):
            row.update({k: v for k, v in self.monitor.gauges.items() if v is not None})
            row.update(self.monitor.pop_counts())
        return now, row


MAX_RPC_RESPONSE_BYTES = 16 * 2**20
NETWORK_INFO_MIN_SEC = 10   # /network_info is fetched at most this often (matters in 1s mode)


def http_json(url, payload=None, timeout=10):
    data = None
    headers = {"User-Agent": "near-node-dashboard/1.0"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Cap the size: a misbehaving or malicious endpoint must not be able to exhaust memory.
        # (The largest real response, mainnet `validators`, is well under 1 MB.)
        body = resp.read(MAX_RPC_RESPONSE_BYTES + 1)
        if len(body) > MAX_RPC_RESPONSE_BYTES:
            raise ValueError(f"response from {url} exceeds {MAX_RPC_RESPONSE_BYTES // 2**20} MB; ignored")
        return json.loads(body)


def rpc(url, method, params):
    body = http_json(url, {"jsonrpc": "2.0", "id": "stats", "method": method, "params": params}, timeout=20)
    if "error" in body:
        raise RuntimeError(json.dumps(body["error"])[:300])
    return body["result"]


def parse_near_time(s):
    # e.g. 2026-11-08T03:41:36.507474318Z — trim nanoseconds for fromisoformat.
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s + "+00:00").timestamp()


def yocto_to_near(v):
    try:
        return int(v) / 1e24
    except (TypeError, ValueError):
        return None


def as_int(v):
    """Integer-valued RPC field (heights, protocol numbers, shard ids), else None."""
    f = num(v)
    return int(f) if f is not None and f.is_integer() else None


def ratio(produced, expected):
    """produced / expected as a %, or None when either isn't a usable number."""
    p, e = num(produced), num(expected)
    return p / e * 100 if p is not None and e else None


def split_addr(addr):
    """'<ipv4>:24567' -> ('<ipv4>', '24567'); '[::1]:24567' -> ('::1', '24567');
    a bare IPv6 address without brackets has no port."""
    addr = (addr or "").strip() if isinstance(addr, str) else ""
    if addr.startswith("["):
        host, _, rest = addr[1:].partition("]")
        return host, rest[1:] if rest.startswith(":") else ""
    if addr.count(":") == 1:
        host, _, port = addr.partition(":")
        return host, port
    return addr, ""


def valid_ip(host):
    """Return the normalised IP string, or None if `host` isn't an IP literal.
    Guards subprocess arguments: a value like '-f' must never reach `ping`."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def valid_port(port):
    try:
        p = int(port)
    except (TypeError, ValueError):
        return None
    return p if 0 < p < 65536 else None


def ping_key(ip, port, method):
    """Ping results are keyed by host for ICMP (same host = same answer) and host:port for TCP."""
    return f"{ip}:{port}" if method == "tcp" else ip


# --------------------------------------------------------------------------- service log

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# neard (tracing) format: "2024-05-13T10:00:00.123456Z  WARN target::path: message"
LOG_RE = re.compile(r"^(?:(?P<ts>\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?Z?)\s+)?"
                    r"(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR)\s+(?P<rest>.*)$")
# Optional span prefixes like "sync{id=1}:" before the module target.
TARGET_RE = re.compile(r"^(?:[\w-]+\{[^}]*\}:\s*)*(?P<target>[A-Za-z_][\w]*(?:::\w+)*):\s+(?P<msg>.*)$", re.S)
SIG_SUBS = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?:ed25519:|secp256k1:)?[1-9A-HJ-NP-Za-km-z]{32,}\b"), "<hash>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|µs|us|ns|%)?\b"), "<n>"),
]
LOG_MAX_MSG = 2000
LOG_BACKFILL_BYTES = 4 * 1024 * 1024   # how much of an existing log file to read on first start
LOG_MAX_READ = 16 * 1024 * 1024        # per poll


def log_signature(target, msg):
    s = f"{target}: {msg}"[:400]
    for rx, rep in SIG_SUBS:
        s = rx.sub(rep, s)
    return s


TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?Z?)\s")


def _parse_log_ts(s):
    try:
        return parse_near_time(s) if s.endswith("Z") else datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def line_ts(line):
    """Timestamp at the start of a log line (any level), or None."""
    m = TS_RE.match(ANSI_RE.sub("", line).lstrip())
    return _parse_log_ts(m.group(1)) if m else None


def parse_log_line(line, fallback_ts):
    """Return {ts, level, target, message, sig} for WARN/ERROR/panic lines, else None."""
    line = ANSI_RE.sub("", line).strip()
    if not line:
        return None
    m = LOG_RE.match(line)
    if m and m.group("level") in ("WARN", "WARNING", "ERROR"):
        level = "ERROR" if m.group("level") == "ERROR" else "WARN"
        rest = m.group("rest")
        t = TARGET_RE.match(rest)
        target, msg = (t.group("target"), t.group("msg")) if t else ("", rest)
        ts = (_parse_log_ts(m.group("ts")) if m.group("ts") else None) or fallback_ts
    elif " panicked at " in line:
        level, target, msg, ts = "ERROR", "panic", line, fallback_ts
    else:
        return None
    msg = msg[:LOG_MAX_MSG]
    return {"ts": ts, "level": level, "target": target, "message": msg, "sig": log_signature(target, msg)}


class LogCollector:
    """Follows the neard service log (journald unit or file) and stores WARN/ERROR lines."""

    def __init__(self, config):
        self.config = config
        self.cursor = None          # journald position
        self.file_id = None         # (dev, inode) of the followed file
        self.offset = 0
        self.source_key = None      # resets position when source/unit/file changes
        self.coverage_start = None  # earliest time this run's reads cover (for zero-filling the chart)
        self._follower = None       # long-running `journalctl -f` (see _start_follower)
        self.status = {"ok": None, "error": None, "warning": None, "last_read": None}

    def _since(self, conn):
        last = conn.execute("SELECT MAX(ts) FROM node_logs").fetchone()[0]
        floor = time.time() - float(self.config.get("retention_hours")) * 3600
        return max(last or 0, floor)

    JOURNAL_PERMISSION_WARNING = ("No permission to read the system journal. Add this user to the "
                                  "'systemd-journal' group and restart the dashboard.")

    def _stop_follower(self):
        f, self._follower = self._follower, None
        if f is not None:
            try:
                f["proc"].kill()
                f["proc"].wait(timeout=5)
            except Exception:
                pass

    def _start_follower(self, conn, unit):
        """Start one long-running `journalctl -f` instead of a new journalctl every poll. Reader
        threads move its stdout lines into a queue and keep its stderr for error messages."""
        cmd = ["journalctl", "-u", unit, "-o", "json", "--no-pager", "--output-fields=MESSAGE", "-f"]
        if self.cursor:
            cmd += ["--after-cursor", self.cursor]
        else:
            since = self._since(conn)
            if self.coverage_start is None:
                self.coverage_start = since
            cmd += ["--since", f"@{int(since)}"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1)
        except FileNotFoundError:
            raise RuntimeError("journalctl not found: this system has no systemd journal. "
                               "Use log source 'file' instead.")
        f = {"proc": proc, "lines": queue.Queue(), "stderr": []}

        def pump(stream, sink):
            for line in stream:
                sink(line)

        threading.Thread(target=pump, args=(proc.stdout, f["lines"].put), daemon=True).start()
        threading.Thread(target=pump, args=(proc.stderr, lambda l: f["stderr"].append(l) if len(f["stderr"]) < 50 else None),
                         daemon=True).start()
        self._follower = f

    def _read_journald(self, conn, unit):
        if self._follower is None:
            self._start_follower(conn, unit)
        f = self._follower
        lines, raw_lines = [], []
        while True:
            try:
                raw_lines.append(f["lines"].get_nowait())
            except queue.Empty:
                break
        for raw in raw_lines:
            try:
                j = json.loads(raw)
            except ValueError:
                continue
            self.cursor = j.get("__CURSOR", self.cursor)
            msg = j.get("MESSAGE")
            if isinstance(msg, list):  # journald encodes non-UTF-8/escape-laden messages as bytes
                msg = bytes(msg).decode("utf-8", "replace")
            if msg:
                ts = int(j.get("__REALTIME_TIMESTAMP", 0)) / 1e6 or time.time()
                lines.append((msg, ts))
        err = "".join(f["stderr"])
        if f["proc"].poll() is not None and f["lines"].empty():
            # journalctl exited: report why, and restart from the last cursor on the next poll.
            self._follower = None
            if f["proc"].returncode != 0:
                raise RuntimeError((err.strip() or f"journalctl exited with code {f['proc'].returncode}")[:300])
        warning = (self.JOURNAL_PERMISSION_WARNING
                   if "not seeing messages from other users" in err or "No journal files" in err else None)
        return lines, warning

    def _read_file(self, path):
        if not path:
            raise RuntimeError("No log file configured (Settings → Log file)")
        try:
            st = os.stat(path)
        except OSError as e:
            raise RuntimeError(f"Cannot read log file: {e}")
        skip_partial = False
        if self.file_id != (st.st_dev, st.st_ino) or st.st_size < self.offset:
            first = self.file_id is None
            self.file_id = (st.st_dev, st.st_ino)
            # First start: backfill the tail of the file. Rotation/truncation: start from the top.
            self.offset = max(0, st.st_size - LOG_BACKFILL_BYTES) if first else 0
            skip_partial = self.offset > 0
        now = time.time()
        with open(path, "rb") as f:
            f.seek(self.offset)
            data = f.read(LOG_MAX_READ)
        end = data.rfind(b"\n")
        if end < 0:
            return [], None
        self.offset += end + 1
        lines = data[:end].decode("utf-8", "replace").splitlines()
        if skip_partial:
            lines = lines[1:]
        out = []
        for line in lines:
            if line.startswith('{"log":'):  # Docker json-file driver
                try:
                    line = json.loads(line).get("log", "")
                except ValueError:
                    pass
            out.append((line, now))
        if self.coverage_start is None:
            # This run's view of the file starts at its first timestamped line.
            self.coverage_start = next((t for t in (line_ts(l) for l, _ in out) if t), now)
        return out, None

    def poll(self, conn):
        cfg = self.config.snapshot()
        key = (cfg["log_source"], cfg["log_unit"], cfg["log_file"])
        if key != self.source_key:
            self._stop_follower()
            self.source_key, self.cursor, self.file_id, self.offset = key, None, None, 0
            self.coverage_start = None
        if cfg["log_source"] != "journald":
            self._stop_follower()
        if cfg["log_source"] == "journald":
            lines, warning = self._read_journald(conn, cfg["log_unit"] or "neard")
        else:
            lines, warning = self._read_file(cfg["log_file"])
        # Lines without their own timestamp (e.g. panics) take the timestamp of the nearest
        # preceding line, so re-reading the same file after a restart produces the same rows
        # and the dedupe index drops them, instead of storing them again with "now".
        rows, last_ts = [], None
        for line, fallback in lines:
            last_ts = line_ts(line) or last_ts
            r = parse_log_line(line, last_ts or fallback)
            if r:
                rows.append(r)
        conn.executemany("INSERT OR IGNORE INTO node_logs (ts, level, target, message, sig) VALUES (?,?,?,?,?)",
                         [(r["ts"], r["level"], r["target"], r["message"], r["sig"]) for r in rows])
        conn.commit()
        if rows:
            DATA_GEN.bump()
        self.status = {"ok": True, "error": None, "warning": warning, "last_read": time.time(),
                       "lines_read": len(lines), "matched": len(rows)}

    def run(self, db_path, stop):
        conn = db_connect(db_path)
        last = 0.0
        while not stop.is_set():
            if not self.config.get("collect_logs"):
                self._stop_follower()
            elif time.time() - last >= int(self.config.get("log_poll_seconds")):
                last = time.time()
                try:
                    self.poll(conn)
                except Exception as e:
                    self.status = {"ok": False, "error": str(e), "warning": None, "last_read": time.time()}
            stop.wait(1)
        self._stop_follower()
        conn.close()

    def query(self, conn, rng, level="all", q="", limit=200):
        since = time.time() - rng
        where, args = ["ts >= ?"], [since]
        if level in ("WARN", "ERROR"):
            where.append("level = ?"); args.append(level)
        if q:
            where.append("(message LIKE ? OR target LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
        w = " AND ".join(where)
        recent = [dict(zip(("ts", "level", "target", "message"), r)) for r in conn.execute(
            f"SELECT ts, level, target, message FROM node_logs WHERE {w} ORDER BY ts DESC LIMIT ?", (*args, limit))]
        groups = [dict(zip(("level", "sig", "count", "first", "last", "target", "message"), r)) for r in conn.execute(
            f"""SELECT level, sig, COUNT(*), MIN(ts), MAX(ts), MAX(target),
                       (SELECT message FROM node_logs n2 WHERE n2.sig = n1.sig AND n2.level = n1.level
                        ORDER BY ts DESC LIMIT 1)
                FROM node_logs n1 WHERE {w} GROUP BY level, sig ORDER BY MAX(ts) DESC LIMIT ?""", (*args, limit))]
        counts = dict(conn.execute("SELECT level, COUNT(*) FROM node_logs WHERE ts >= ? GROUP BY level", (since,)))
        cfg = self.config.snapshot()
        return {"status": {**self.status, "enabled": cfg["collect_logs"], "source": cfg["log_source"],
                           "target": cfg["log_unit"] if cfg["log_source"] == "journald" else cfg["log_file"]},
                "counts": {"WARN": counts.get("WARN", 0), "ERROR": counts.get("ERROR", 0)},
                "recent": recent, "groups": groups}

    def series(self, conn, since, bucket):
        """Warn/error counts per bucket; empty buckets are 0 while the log source is readable."""
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS t, SUM(level = 'WARN'), SUM(level = 'ERROR') "
            "FROM node_logs WHERE ts >= ? GROUP BY t", (bucket, bucket, since))}
        out = {"t": [], "warn": [], "error": [], "bucket": bucket}
        if self.status.get("ok"):
            # Zero-fill only the span the log has actually been read for (this run's backfill,
            # or earlier stored lines); before that there is no data, so leave a gap.
            earliest = conn.execute("SELECT MIN(ts) FROM node_logs").fetchone()[0]
            covered = min(x for x in (self.coverage_start, earliest, time.time()) if x is not None)
            start = int(max(since, covered) // bucket) * bucket
            ts = range(start, int(time.time()) + 1, bucket)
        else:
            ts = sorted(rows)
        for t in ts:
            w, e = rows.get(t, (0, 0))
            out["t"].append(t); out["warn"].append(w); out["error"].append(e)
        return out


# --------------------------------------------------------------------------- host checks (run once at start)

MAX_EXPECTED_PATH_BDP = 8388608
# (sysctl key, recommended value(s), indexes where a *larger* value is also fine)
SYSCTL_RECOMMENDED = [
    ("net.core.rmem_max", [MAX_EXPECTED_PATH_BDP], {0}),
    ("net.core.wmem_max", [MAX_EXPECTED_PATH_BDP], {0}),
    ("net.ipv4.tcp_rmem", [4096, 87380, MAX_EXPECTED_PATH_BDP], {2}),
    ("net.ipv4.tcp_wmem", [4096, 16384, MAX_EXPECTED_PATH_BDP], {2}),
    ("net.ipv4.tcp_slow_start_after_idle", [0], set()),
]


def _ints(text):
    try:
        return [int(x) for x in str(text).split()]
    except ValueError:
        return None


def _sysctl_ok(actual, expected, may_exceed):
    return (actual is not None and len(actual) == len(expected) and
            all(a >= e if i in may_exceed else a == e for i, (a, e) in enumerate(zip(actual, expected))))


# Directories systemd-sysctl reads at boot, highest priority first. A file name in an earlier
# directory masks the same name in later ones.
SYSCTL_DIRS = ("etc/sysctl.d", "run/sysctl.d", "usr/local/lib/sysctl.d", "usr/lib/sysctl.d", "lib/sysctl.d")


def _sysctl_boot_files(root="/"):
    """Config files in the order they're applied at boot, following systemd-sysctl: unique file
    names across SYSCTL_DIRS (earlier directory wins), applied in name order. /etc/sysctl.conf is
    usually the symlink /etc/sysctl.d/99-sysctl.conf, which therefore sorts *before* e.g. local.conf;
    if it isn't linked in, it's applied last (as `sysctl --system` does)."""
    chosen = {}
    for d in SYSCTL_DIRS:
        full = os.path.join(root, d)
        if os.path.isdir(full):
            for name in os.listdir(full):
                if name.endswith(".conf") and name not in chosen:
                    chosen[name] = os.path.join(full, name)
    files = [chosen[n] for n in sorted(chosen)]
    conf = os.path.join(root, "etc/sysctl.conf")
    if os.path.isfile(conf) and os.path.realpath(conf) not in {os.path.realpath(f) for f in files}:
        files.append(conf)
    return files


def _persisted_sysctls(root="/"):
    """key -> (value, file) for the definition that wins at boot (the last one applied)."""
    files = _sysctl_boot_files(root)
    found = {}
    for path in files:
        try:
            with open(path) as f:
                for line in f:
                    line = line.split("#", 1)[0].split(";", 1)[0].strip()
                    if "=" in line:
                        k, _, v = line.partition("=")
                        found[k.strip().lstrip("-").replace("/", ".")] = (" ".join(v.split()), path)
        except OSError:
            continue
    return found


def check_sysctls(proc="/proc/sys", root="/"):
    items = []
    if not os.path.isdir(os.path.join(proc, "net")):
        return [{"id": "sysctl", "group": "Network tuning", "title": "Kernel network settings", "status": "na",
                 "detail": f"Not applicable on {sys.platform}: these are Linux kernel settings. "
                           "Run the dashboard on the Linux host running neard to check them."}]
    persisted = _persisted_sysctls(root)
    for key, expected, may_exceed in SYSCTL_RECOMMENDED:
        want = " ".join(map(str, expected))
        try:
            with open(os.path.join(proc, *key.split("."))) as f:
                raw = " ".join(f.read().split())
        except OSError as e:
            items.append({"id": key, "group": "Network tuning", "title": key, "status": "warn",
                          "expected": want, "actual": None, "detail": f"Couldn't read the current value: {e}"})
            continue
        live_ok = _sysctl_ok(_ints(raw), expected, may_exceed)
        p_val, p_file = persisted.get(key, (None, None))
        saved_ok = _sysctl_ok(_ints(p_val) if p_val else None, expected, may_exceed)
        if live_ok and saved_ok:
            status, detail = "pass", f"Set, and saved in {p_file} so it survives a reboot."
        elif live_ok:
            status = "warn"
            detail = (f"Set now, but at boot {p_file} sets it to {p_val!r} (it's the last file applied "
                      f"that sets this key)."
                      if p_val else "Set now, but not saved in any sysctl.d file or /etc/sysctl.conf, "
                                    "so it will reset on reboot.")
        else:
            status = "fail"
            detail = (f"Current value doesn't match. {p_file} has {p_val!r}" if p_val else
                      "Current value doesn't match, and it isn't saved in a sysctl config file") + \
                     f". Apply with: sudo sysctl -w {key}=\"{want}\""
        if live_ok and any(a > e for a, e in zip(_ints(raw) or [], expected)):
            detail += " (higher than recommended, which is fine)"
        items.append({"id": key, "group": "Network tuning", "title": key, "status": status,
                      "expected": want, "actual": raw, "saved": p_val, "saved_in": p_file, "detail": detail})
    return items


def _run(cmd, timeout=5, use_sudo=False):
    """Run a command. Returns (returncode, output), or None if the tool isn't installed.

    Only if `use_sudo` is set (the firewall_check_sudo setting) does a permission error get retried
    with `sudo -n`. That never prompts, but for a user without sudo rights every attempt is still
    logged by PAM as a failed authentication ("auth could not identify password"), so it's off by
    default: the service user the installer creates has no sudo rights."""
    import shutil
    if not shutil.which(cmd[0]):
        return None
    attempts = [cmd]
    if use_sudo and os.geteuid() != 0 and shutil.which("sudo"):
        attempts.append(["sudo", "-n"] + cmd)
    for attempt in attempts:
        try:
            out = subprocess.run(attempt, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (out.stdout + out.stderr).strip()
        denied = out.returncode != 0 and re.search(r"permission|denied|root|privilege|not permitted|password", text, re.I)
        if not denied:
            return out.returncode, text
    return 1, ("needs root to read firewall rules (the check uses inbound peer connections instead; "
               "to read rules, give the service user passwordless sudo for this command and turn on "
               "firewall_check_sudo)")


def _ufw_rule_matches(to_field, port):
    """Does a ufw rule's "To" field (e.g. '24567/tcp', '24000:25000/tcp', '22,24567/tcp', '24567')
    cover `port` over TCP?"""
    spec, _, proto = to_field.partition("/")
    if proto and proto not in ("tcp",):
        return False
    for part in spec.split(","):
        lo, _, hi = part.partition(":")
        try:
            if int(lo) <= port <= int(hi or lo):
                return True
        except ValueError:
            continue
    return False


def _ufw_verdict(text, port):
    """Parse `ufw status verbose`: explicit rules for the port first, then the default incoming policy."""
    for line in text.splitlines():
        cols = line.split()
        if len(cols) >= 2 and _ufw_rule_matches(cols[0], port) and "(out)" not in line.lower():
            action = cols[1].upper()
            if action in ("ALLOW", "LIMIT"):
                return "allowed", f"ufw rule: {' '.join(cols)}"
            if action in ("DENY", "REJECT"):
                return "blocked", f"ufw rule: {' '.join(cols)}"
    m = re.search(r"Default:\s*(\w+)\s*\(incoming\)", text)
    if m and m.group(1).lower() == "allow":
        return "allowed", "ufw default incoming policy is allow"
    return "blocked", (f"ufw is active, denies incoming by default, and has no ALLOW rule covering "
                       f"{port}/tcp. Fix: sudo ufw allow {port}/tcp")


def _firewall_rules(port, use_sudo=False):
    """Best-effort look at local firewall rules. Returns (verdict, tool, detail) where verdict is
    'allowed', 'blocked', 'open' (no active filtering), or 'unknown'."""
    p = str(port)
    findings = []

    r = _run(["ufw", "status", "verbose"], use_sudo=use_sudo)
    if r:
        code, text = r
        if code == 0 and "Status: inactive" in text:
            findings.append(("open", "ufw", "ufw is installed but inactive"))
        elif code == 0 and "Status: active" in text:
            verdict, detail = _ufw_verdict(text, port)
            return verdict, "ufw", detail
        else:
            findings.append(("unknown", "ufw", text.splitlines()[0] if text else "couldn't read ufw status"))

    r = _run(["firewall-cmd", "--state"], use_sudo=use_sudo)
    if r and r[0] == 0 and "running" in r[1]:
        ports = _run(["firewall-cmd", "--list-ports"], use_sudo=use_sudo)
        if ports and ports[0] == 0:
            return (("allowed", "firewalld", f"firewalld allows {p}/tcp") if f"{p}/tcp" in ports[1].split() else
                    ("blocked", "firewalld", f"firewalld is running and {p}/tcp isn't in its open ports. "
                                             f"Fix: sudo firewall-cmd --permanent --add-port={p}/tcp && sudo firewall-cmd --reload"))
        findings.append(("unknown", "firewalld", "firewalld is running but its ports couldn't be read"))

    r = _run(["nft", "list", "ruleset"], use_sudo=use_sudo)
    if r and r[0] == 0 and r[1]:
        text = r[1]
        if re.search(rf"dport\s+(\{{[^}}]*\b{p}\b[^}}]*\}}|{p}\b).*accept", text):
            return ("allowed", "nftables", f"nftables has an accept rule for port {p}")
        if re.search(r"hook\s+input[^;]*;\s*policy\s+drop", text):
            return ("blocked", "nftables", f"nftables input policy is drop with no accept rule for port {p}")
        findings.append(("open" if "hook input" not in text else "unknown", "nftables",
                         "no input filtering found" if "hook input" not in text else
                         f"input rules exist but none mention port {p}"))
    elif r:
        findings.append(("unknown", "nftables", r[1].splitlines()[0] if r[1] else "couldn't read nftables"))

    r = _run(["iptables", "-S", "INPUT"], use_sudo=use_sudo)
    if r and r[0] == 0:
        text = r[1]
        if re.search(rf"--dport {p}\b.*-j ACCEPT", text) or re.search(rf"--dports [\d,:]*\b{p}\b.*-j ACCEPT", text):
            return ("allowed", "iptables", f"iptables has an ACCEPT rule for port {p}")
        policy_drop = re.search(r"^-P INPUT (DROP|REJECT)", text, re.M)
        # Only a catch-all rule (no source, port or other match) drops everything; a rule that
        # bans one IP or closes another port doesn't affect the P2P port.
        catch_all = re.search(r"^-A INPUT -j (DROP|REJECT)\b", text, re.M)
        if policy_drop or catch_all:
            return ("blocked", "iptables", f"iptables drops/rejects input by default and has no ACCEPT rule "
                                           f"for port {p}. Fix: sudo iptables -I INPUT -p tcp --dport {p} -j ACCEPT")
        findings.append(("open", "iptables", "iptables INPUT policy is ACCEPT with no catch-all drop rule"))
    elif r:
        findings.append(("unknown", "iptables", r[1].splitlines()[0] if r[1] else "couldn't read iptables"))

    if not findings:
        return ("unknown", None, "no supported firewall tool found (ufw, firewalld, nftables, iptables)")
    if all(f[0] == "open" for f in findings):
        return ("open", ", ".join(f[1] for f in findings), "; ".join(f[2] for f in findings))
    return ("unknown", ", ".join(f[1] for f in findings), "; ".join(f[2] for f in findings))


def _remote_node_host(rpc_url):
    """The node's host if rpc_url points at another machine, else None."""
    host = (urlparse(rpc_url).hostname or "").lower()
    if not host or host in ("localhost",) or host in {socket.gethostname().lower(), socket.getfqdn().lower()}:
        return None
    ip = valid_ip(host)
    if ip and (ipaddress.ip_address(ip).is_loopback or ipaddress.ip_address(ip).is_unspecified):
        return None
    try:  # one of this machine's own addresses?
        if ip and any(a.address == ip for addrs in psutil.net_if_addrs().values() for a in addrs):
            return None
    except Exception:
        pass
    return host


def check_p2p_reachable(host, port):
    """Connect to the node's P2P port from this machine: a genuine inbound test through every
    firewall in the path when the dashboard runs elsewhere."""
    title = f"P2P port {port}/tcp reachable from this host"
    item = {"id": "p2p_reachable", "group": "Firewall", "title": title, "expected": "connects"}
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=5) as s:
            peer_ip = s.getpeername()[0]
        ms = (time.perf_counter() - t0) * 1000
        if not ipaddress.ip_address(peer_ip.split("%")[0]).is_global:
            # Same LAN/VPC: a cloud firewall that only admits internal traffic would also pass this.
            item.update(status="warn", actual=f"connected in {ms:.0f} ms (private network)",
                        detail=f"Connected to {host}:{port} ({peer_ip}) from {socket.gethostname()}, but over a "
                               f"private network address, so this doesn't prove the port is reachable from the "
                               f"internet. Check the cloud/provider firewall allows {port}/tcp from anywhere.")
        else:
            item.update(status="pass", actual=f"connected in {ms:.0f} ms",
                        detail=f"Opened a TCP connection to {host}:{port} from {socket.gethostname()} over the "
                               f"internet, so the node accepts inbound P2P connections from outside.")
    except ConnectionRefusedError:
        item.update(status="fail", actual="refused",
                    detail=f"{host}:{port} refused the connection: nothing is listening on that port, or a "
                           f"firewall is rejecting it. Check neard's network.addr and the firewall.")
    except socket.timeout:
        item.update(status="fail", actual="timed out",
                    detail=f"No answer from {host}:{port} within 5 s: a firewall (host or cloud) is most "
                           f"likely dropping inbound traffic to {port}/tcp.")
    except OSError as e:
        item.update(status="warn", actual="error", detail=f"Couldn't test {host}:{port}: {e}")
    return item


def neard_running_locally(port, process_name):
    """True/False if we can tell whether neard runs on this machine, None if we can't."""
    try:
        conns = psutil.net_connections(kind="tcp")
        if any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port for c in conns):
            return True
    except (psutil.AccessDenied, PermissionError):
        pass  # macOS without root: fall back to the process list
    except Exception:
        pass
    try:
        if process_name and any(p.info.get("name") == process_name for p in psutil.process_iter(["name"])):
            return True
    except Exception:
        return None
    return False


def check_p2p_firewall(port, peer_listen_addrs=(), use_sudo=False):
    """`peer_listen_addrs`: (ip, port) pairs peers advertise as their listening address
    (from /network_info), used to tell our outbound connections apart from inbound ones."""
    title = f"Inbound P2P port {port}/tcp"
    item = {"id": "firewall", "group": "Firewall", "title": title, "expected": "inbound allowed"}
    listening, inbound, sock_err = None, [], None
    outbound_targets = set(peer_listen_addrs)
    try:
        conns = psutil.net_connections(kind="tcp")
        listening = any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port for c in conns)

        def remote(c):
            ip = valid_ip(c.raddr.ip.split("%")[0]) if c.raddr else None  # strip IPv6 zone ids
            return ip if ip and not ipaddress.ip_address(ip).is_loopback else None

        def is_inbound(c):
            # Local port == P2P port normally means the peer connected to us. But if neard reuses
            # its listening port as the source port for outbound connections, those look the same;
            # an outbound connection goes *to* the peer's advertised listening address (and usually
            # its P2P port), so exclude those rather than mistake them for inbound traffic.
            ip = remote(c)
            if not ip or c.status != psutil.CONN_ESTABLISHED or not c.laddr or c.laddr.port != port:
                return False
            return (ip, c.raddr.port) not in outbound_targets and c.raddr.port != port

        inbound = sorted({remote(c) for c in conns if is_inbound(c)})
    except (psutil.AccessDenied, PermissionError):
        sock_err = "listing sockets needs root on this OS"
    except Exception as e:
        sock_err = str(e)

    verdict, tool, rule_detail = _firewall_rules(port, use_sudo) if sys.platform.startswith("linux") else \
        ("unknown", None, f"firewall rule checks are Linux-only ({sys.platform})")
    item["listening"], item["inbound_peers"], item["firewall"] = listening, len(inbound), tool
    notes = []
    if listening is False:
        notes.append(f"Nothing is listening on port {port}: is neard running, and is p2p_port right?")
    if inbound:
        status = "pass"
        notes.insert(0, f"{len(inbound)} remote peer(s) are connected inbound to port {port}, so inbound "
                        f"traffic is reaching neard.")
        item["actual"] = f"{len(inbound)} inbound peers"
    elif verdict == "allowed":
        status, item["actual"] = "pass", "allowed by rule"
        notes.insert(0, rule_detail + ". No inbound peer connections were seen yet, so a cloud/provider "
                                      "firewall can't be ruled out.")
    elif verdict == "blocked":
        status, item["actual"] = "fail", "blocked"
        notes.insert(0, rule_detail)
    elif verdict == "open":
        status, item["actual"] = "warn", "no local filtering"
        notes.insert(0, f"{rule_detail}. But no inbound peer connections were seen, so check your "
                        f"cloud/provider firewall (security group) allows {port}/tcp.")
    else:
        status, item["actual"] = ("na" if not sys.platform.startswith("linux") else "warn"), "unconfirmed"
        notes.insert(0, f"Couldn't confirm: {rule_detail}" + (f"; {sock_err}" if sock_err else "") +
                     ", and no inbound peer connections were seen.")
    item["status"], item["detail"] = status, " ".join(notes)
    return item


def run_host_checks(config, near=None, wait=120, stop=None):
    """Runs once per service start. If neard should be on this machine but isn't up yet (e.g. the
    dashboard started first at boot), wait up to `wait` seconds for it before checking."""
    port, pname = int(config.get("p2p_port")), config.get("process_name")
    remote = _remote_node_host(config.get("rpc_url"))
    deadline = time.time() + wait
    local = neard_running_locally(port, pname)
    while not local and not remote and time.time() < deadline and not (stop and stop.is_set()):
        time.sleep(5)
        local = neard_running_locally(port, pname)
    if near is not None:  # give the NEAR collector a moment to fetch the peer list
        for _ in range(10):
            if near.active_peers:
                break
            time.sleep(1)
    started = time.time()
    try:
        peers = []
        for p in (near.active_peers if near is not None else []):
            host, pport = split_addr(p.get("addr"))
            ip, pp = valid_ip(host), valid_port(pport)
            if ip and pp:
                peers.append((ip, pp))
        items = check_sysctls() + [check_p2p_firewall(port, peers, bool(config.get("firewall_check_sudo")))]
        if local is False and remote:
            # neard isn't running here, so this machine's kernel/firewall settings don't affect it.
            # Keep the values for reference but don't count them; test the node over the network.
            for i in items:
                if i["status"] != "na":
                    i["status"] = "na"
                i["detail"] = (f"Not counted: neard isn't running on this machine ({socket.gethostname()}), so "
                               f"its settings don't affect the node at {remote}. Run the dashboard on the node "
                               f"to check these. " + ("Local result: " + i.get("detail", "") if i.get("detail") else ""))
            items.append(check_p2p_reachable(remote, port))
        elif local is None and remote:
            # Can't tell whether neard runs here: show both, and say so.
            items.append(check_p2p_reachable(remote, port))
            items[-1]["detail"] += (f" (Couldn't tell whether neard runs on this machine; {remote} comes from the "
                                    f"RPC URL, which may be a public RPC rather than your node.)")
    except Exception as e:  # a check must never take the server down
        items = [{"id": "error", "group": "Host checks", "title": "Host checks", "status": "warn",
                  "detail": f"Checks failed to run: {e}"}]
    return {"time": started, "hostname": socket.gethostname(), "platform": sys.platform, "items": items,
            "counts": {s: sum(1 for i in items if i["status"] == s) for s in ("pass", "warn", "fail", "na")}}


class Pinger:
    """Pings every active peer once per round and stores per-peer RTTs plus a round summary."""

    def __init__(self, config, near):
        self.config = config
        self.near = near
        self.last = {}          # ip -> (ts, rtt_ms or None)
        self.last_round = None  # summary of the latest round
        self.error = None

    @staticmethod
    def ping_icmp(ip, timeout_ms):
        ip = valid_ip(ip)
        if ip is None:  # never pass anything but an IP literal to the ping command
            return None
        v6 = ":" in ip
        if sys.platform == "darwin":
            # macOS: -W is in milliseconds; ping6 has no -W, so rely on the subprocess timeout.
            cmd = ["ping6", "-n", "-c", "1", ip] if v6 else ["ping", "-n", "-c", "1", "-W", str(timeout_ms), ip]
        else:
            # Linux iputils: -W is in whole seconds.
            cmd = ["ping", "-6" if v6 else "-4", "-n", "-c", "1", "-W", str(max(1, math.ceil(timeout_ms / 1000))), ip]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_ms / 1000 + 1)
        except (subprocess.TimeoutExpired, OSError):
            return None
        m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out.stdout)
        return float(m.group(1)) if m else None

    @staticmethod
    def ping_tcp(ip, port, timeout_ms):
        ip = valid_ip(ip)
        if ip is None:
            return None
        t0 = time.perf_counter()
        try:
            with socket.create_connection((ip, valid_port(port) or 24567), timeout=timeout_ms / 1000):
                pass
        except ConnectionRefusedError:
            pass  # the host answered with a reset, which still measures the round trip
        except (OSError, ValueError):
            return None
        return (time.perf_counter() - t0) * 1000

    def targets(self, method):
        """Unique ping targets as (key, ip, port, peer_id). Peers whose address isn't a valid
        IP literal are skipped."""
        seen = {}
        for p in self.near.active_peers:
            host, port = split_addr(p.get("addr"))
            ip = valid_ip(host)
            if not ip:
                continue
            port = valid_port(port) or 24567
            key = ping_key(ip, port, method)
            if key not in seen:
                seen[key] = (key, ip, port, p.get("id"))
        return list(seen.values())

    def round(self, conn):
        cfg = self.config.snapshot()
        method, timeout = cfg["ping_method"], int(cfg["ping_timeout_ms"])
        targets = self.targets(method)
        if not targets:
            return
        ts = time.time()

        def one(t):
            _, ip, port, _ = t
            return self.ping_tcp(ip, port, timeout) if method == "tcp" else self.ping_icmp(ip, timeout)

        with ThreadPoolExecutor(max_workers=32) as ex:
            rtts = list(ex.map(one, targets))
        # The `ip` column holds the ping key (host for ICMP, host:port for TCP).
        conn.executemany("INSERT INTO peer_pings (ts, ip, peer_id, rtt_ms) VALUES (?,?,?,?)",
                         [(ts, key, pid, rtt) for (key, _, _, pid), rtt in zip(targets, rtts)])
        for (key, _, _, _), rtt in zip(targets, rtts):
            self.last[key] = (ts, rtt)
        ok = sorted(r for r in rtts if r is not None)
        row = {"peers": len(targets), "replied": len(ok), "loss_pct": (1 - len(ok) / len(targets)) * 100}
        if ok:
            row["median_ms"] = ok[len(ok) // 2]
            row["p90_ms"] = ok[min(len(ok) - 1, int(len(ok) * 0.9))]
        db_insert(conn, "ping_rounds", PING_COLS, ts, row)
        self.last_round = {"time": ts, "method": method, **row}

    def run(self, db_path, stop):
        conn = db_connect(db_path)
        last_run = 0.0
        while not stop.is_set():
            due = time.time() - last_run >= int(self.config.get("ping_interval_seconds"))
            on = self.config.get("collect_peers") and self.config.get("ping_enabled")
            if on and due and self.near.active_peers:
                last_run = time.time()
                try:
                    self.round(conn)
                    self.error = None
                except Exception as e:
                    self.error = str(e)
                    print(f"[ping] round error: {e}", flush=True)
            stop.wait(1)  # re-checks the (possibly changed) interval each second
        conn.close()

    def stats(self, conn, rng, spark_points=30):
        """Per-IP ping statistics over the last `rng` seconds plus a down-sampled RTT trend."""
        since = time.time() - rng
        out = {}
        for ip, sent, recv, avg, mn, mx, sq in conn.execute(
                "SELECT ip, COUNT(*), COUNT(rtt_ms), AVG(rtt_ms), MIN(rtt_ms), MAX(rtt_ms), AVG(rtt_ms*rtt_ms) "
                "FROM peer_pings WHERE ts >= ? GROUP BY ip", (since,)):
            # `ip` here is the ping key (see ping_key).
            jitter = math.sqrt(max(0.0, sq - avg * avg)) if avg is not None and recv > 1 else None
            last_ts, last_rtt = self.last.get(ip, (None, None))
            out[ip] = {"sent": sent, "received": recv, "loss_pct": (1 - recv / sent) * 100 if sent else None,
                       "avg": avg, "min": mn, "max": mx, "jitter": jitter,
                       "last": last_rtt, "last_ts": last_ts, "trend": []}
        bucket = max(int(self.config.get("ping_interval_seconds")), int(rng / spark_points) or 1)
        for ip, b, avg, lost in conn.execute(
                "SELECT ip, CAST(ts / ? AS INTEGER) AS b, AVG(rtt_ms), COUNT(*) - COUNT(rtt_ms) "
                "FROM peer_pings WHERE ts >= ? GROUP BY ip, b ORDER BY ip, b", (bucket, since)):
            if ip in out:
                out[ip]["trend"].append([b * bucket, None if avg is None else round(avg, 2), lost])
        return out, bucket

    def lookup(self, stats, addr_ip, addr_port):
        """Find a peer's stats in the dict returned by stats(), for the current ping method."""
        ip = valid_ip(addr_ip)
        if not ip:
            return None
        return stats.get(ping_key(ip, valid_port(addr_port) or 24567, self.config.get("ping_method")))


class NearCollector:
    def __init__(self, config):
        self.config = config
        self.prev_height = None
        self.prev_t = None
        self.validators = None
        self.validators_t = 0
        self.validators_busy = False
        self.validators_error = None
        self.active_peers = []
        self.known_producers = []
        self.peers_t = None
        self.recent_node_keys = []
        # Start in baseline mode: peers that changed while the server was down must not be
        # counted as churn at startup.
        self._peers_rebaseline = True
        self._peer_state = None           # peer_id -> [connected, ip, port, account] (see _track_peers)
        self._ni, self._ni_t, self._ni_url = None, 0.0, None   # last /network_info (see _sample)
        self._last_seen_flush = 0.0
        self._prev_peer_sample = None
        self.db_path = None  # set by main(); the collector thread opens its own connection
        self._conn = None
        self.epoch_length = None
        self.epoch_length_url = None
        self.epoch_retry_at = 0.0
        self.validators_fetched_at = None
        self._val_gen = 0           # bumped whenever validator data is discarded
        self._last_url = None
        self._last_vurl = None
        self.latest = {"error": "No data collected yet"}

    def _reset_validators(self):
        self._val_gen += 1          # an in-flight fetch from before this point is discarded
        self.validators, self.validators_error, self.validators_fetched_at = None, None, None
        self.validators_t = 0

    def _fetch_validators(self, url, gen):
        try:
            result = rpc(url, "validators", [None])
            # Drop the result if validator collection was turned off or the URL changed meanwhile.
            if gen == self._val_gen and self.config.get("collect_validator"):
                self.validators, self.validators_error = result, None
                self.validators_fetched_at = time.time()
                DATA_GEN.bump()
        except Exception as e:
            if gen == self._val_gen:
                self.validators_error = str(e)
        finally:
            self.validators_busy = False

    def _epoch_length(self, url):
        if self.epoch_length is None or self.epoch_length_url != url:
            if time.time() < self.epoch_retry_at:
                return 43200  # recent failure: don't retry on every sample
            try:
                cfg = rpc(url, "EXPERIMENTAL_protocol_config", {"finality": "final"})
                length = as_int(cfg.get("epoch_length"))
                if not length:
                    raise ValueError("no epoch_length in protocol config")
                self.epoch_length, self.epoch_length_url = length, url
            except Exception:
                self.epoch_retry_at = time.time() + 300
                return 43200  # mainnet default
        return self.epoch_length

    def sample(self):
        cfg = self.config.snapshot()
        now, row = self._sample(cfg)
        # Only store the columns of sections whose collection is turned on, as numbers
        # (anything non-numeric from the RPC is dropped rather than stored as text).
        allowed = ((NODE_COLS if cfg["collect_node"] else set()) |
                   (PEER_COLS if cfg["collect_peers"] else set()) |
                   (VALIDATOR_COLS if cfg["collect_validator"] else set()))
        return now, {k: num(v) for k, v in row.items() if k in allowed}

    def _sample(self, cfg):
        on_node, on_val, on_peers = cfg["collect_node"], cfg["collect_validator"], cfg["collect_peers"]
        url = cfg["rpc_url"]
        vurl = cfg["validators_rpc_url"] or url
        now = time.time()
        row = {"ok": 0, "synced": 0}
        latest = {"time": now, "rpc_url": url}
        if url != self._last_url:
            # Different node: its peers, heights and node keys aren't comparable with the old one's.
            if self._last_url is not None:
                self._peers_rebaseline = True
            self.recent_node_keys, self.prev_height, self.prev_t = [], None, None
            self._last_url = url
        if vurl != self._last_vurl:
            self._reset_validators()
            self._last_vurl = vurl
        if not on_peers:
            # Paused: forget the live peer list; re-baseline on resume so the gap isn't counted as churn.
            self.active_peers, self._peers_rebaseline = [], True
        if not on_val and (self.validators is not None or self.validators_busy):
            self._reset_validators()
        try:
            t0 = time.perf_counter()
            status = http_json(f"{url}/status")
            row["rpc_latency_ms"] = (time.perf_counter() - t0) * 1000
        except Exception as e:
            latest["error"] = f"/status failed: {e}"
            self.latest = latest
            return now, row

        # A load-balanced RPC answers from different nodes; peer lists then alternate between nodes
        # and show up as false churn. Detect it by the node key changing between samples.
        self.recent_node_keys = (self.recent_node_keys + [status.get("node_public_key")])[-20:]
        latest["rpc_nodes_seen"] = len({k for k in self.recent_node_keys if k})

        if not isinstance(status, dict):
            latest["error"] = "/status returned an unexpected response"
            self.latest = latest
            return now, row
        sync = status.get("sync_info") if isinstance(status.get("sync_info"), dict) else {}
        version = status.get("version") if isinstance(status.get("version"), dict) else {}
        height = as_int(sync.get("latest_block_height"))
        row["ok"] = 1
        row["block_height"] = height
        row["syncing"] = 1 if sync.get("syncing") else 0
        try:
            row["block_lag_sec"] = max(0.0, now - parse_near_time(sync["latest_block_time"]))
        except Exception:
            pass
        lag = row.get("block_lag_sec")
        row["synced"] = int(not row["syncing"] and lag is not None and lag <= SYNC_LAG_THRESHOLD_SEC)
        if self.prev_height is not None and height is not None and now > self.prev_t:
            row["blocks_per_sec"] = max(0, height - self.prev_height) / (now - self.prev_t)
        self.prev_height, self.prev_t = height, now

        text = lambda v: v if isinstance(v, str) else None  # noqa: E731
        latest.update(
            version=text(version.get("version")),
            build=text(version.get("build")),
            chain_id=text(status.get("chain_id")),
            protocol_version=as_int(status.get("protocol_version")),
            latest_protocol_version=as_int(status.get("latest_protocol_version")),
            node_uptime_sec=num(status.get("uptime_sec")),
            node_public_key=text(status.get("node_public_key")),
            block_height=height,
            latest_block_time=text(sync.get("latest_block_time")),
            syncing=bool(sync.get("syncing")),
            epoch_start_height=as_int(sync.get("epoch_start_height")),
            node_validator_account_id=text(status.get("validator_account_id")),
        )
        row["protocol_version"] = latest["protocol_version"]
        row["epoch_start_height"] = latest["epoch_start_height"]   # changes when a new epoch starts
        row["latest_protocol_version"] = latest["latest_protocol_version"]
        if on_node:
            try:
                self._track_version(now, latest)
            except Exception as e:
                print(f"[near] version tracking error: {e}", flush=True)

        if on_node or on_peers:
            # /network_info is large (~50 KB with the producer list) and peer counts don't need
            # second-by-second detail, so in 1s mode fetch it at most every NETWORK_INFO_MIN_SEC and
            # reuse the last values in between (keeps the charts continuous).
            fresh = now - self._ni_t >= NETWORK_INFO_MIN_SEC or self._ni is None or url != self._ni_url
            try:
                if fresh:
                    self._ni, self._ni_t, self._ni_url = http_json(f"{url}/network_info"), now, url
                ni = self._ni
                row.update(peers=ni.get("num_active_peers"), max_peers=ni.get("peer_max_count"),
                           near_sent_bps=ni.get("sent_bytes_per_sec"),
                           near_recv_bps=ni.get("received_bytes_per_sec"))
                dicts = lambda xs: [x for x in xs if isinstance(x, dict)] if isinstance(xs, list) else []  # noqa: E731
                if fresh:
                    self.known_producers = dicts(ni.get("known_producers"))
                latest["known_producers"] = len(self.known_producers)
                if on_peers and fresh:
                    self.active_peers = dicts(ni.get("active_peers"))
                    self.peers_t = now
                    try:
                        row["peer_joins"], row["peer_leaves"] = self._track_peers(now)
                    except Exception as e:
                        print(f"[near] peer tracking error: {e}", flush=True)
            except Exception as e:
                self._ni = None
                latest["network_info_error"] = str(e)

        start = latest["epoch_start_height"]
        epoch_len = self._epoch_length(url) if on_val else None
        if start and height and epoch_len:
            row["epoch_progress_pct"] = min(100.0, max(0.0, (height - start) / epoch_len * 100))
            latest["epoch_length"] = epoch_len
            latest["epoch_blocks_remaining"] = max(0, start + epoch_len - height)

        account = cfg["validator_account_id"] or latest["node_validator_account_id"] or ""
        latest["validator_account_id"] = account
        v_interval = int(cfg["validators_interval_seconds"])
        due = now - self.validators_t >= v_interval
        if on_val and due and not self.validators_busy:
            # The validators response is large/slow; fetch it off-thread so 1s sampling isn't blocked.
            self.validators_busy = True
            self.validators_t = now
            threading.Thread(target=self._fetch_validators, args=(vurl, self._val_gen), daemon=True).start()
        if self.validators_error:
            latest["validators_error"] = self.validators_error
        if self.validators_fetched_at:
            latest["validators_fetched_at"] = self.validators_fetched_at
            # Stale = several fetch intervals without a successful refresh.
            latest["validators_stale"] = now - self.validators_fetched_at > max(3 * v_interval, 180)
        if isinstance(self.validators, dict):
            row.update(self._validator_stats(self.validators, account, latest))
        self.latest = latest
        return now, row

    def _db(self):
        if self._conn is None:
            self._conn = db_connect(self.db_path)
        return self._conn

    PEER_LAST_SEEN_FLUSH_SEC = 60

    def _track_peers(self, now):
        """Diff the active peer list against the known peers; record join/leave events.
        Returns (joins, leaves).

        The known set is kept in memory (loaded from peers_seen once), so a sample only writes the
        peers that joined, left or changed address/account; last_seen for connected peers is
        refreshed at most once a minute instead of 40-odd upserts on every sample."""
        conn = self._db()
        if self._peer_state is None:
            self._peer_state = {r[0]: [bool(r[1]), r[2], r[3], r[4]] for r in conn.execute(
                "SELECT peer_id, connected, ip, port, account_id FROM peers_seen")}
        known = self._peer_state
        by_peer_id = {k["peer_id"]: k.get("account_id") for k in self.known_producers if k.get("peer_id")}
        # First run ever, or resuming after peer collection was paused / a new RPC node: record the
        # current set without counting it as churn.
        baseline = not known or self._peers_rebaseline
        self._peers_rebaseline = False
        current = {}
        for p in self.active_peers:
            pid = p.get("id")
            if pid:
                ip, port = split_addr(p.get("addr"))
                current[pid] = (ip, port, p.get("account_id") or by_peer_id.get(pid))
        joins = [pid for pid in current if not (pid in known and known[pid][0])]
        leaves = [pid for pid, k in known.items() if k[0] and pid not in current]
        # Peers whose row must be written: new/reconnected, or address/account changed.
        dirty = [pid for pid in current if pid in joins or known[pid][1:3] != list(current[pid][:2])
                 or (current[pid][2] and known[pid][3] != current[pid][2])]
        flush_last_seen = baseline or now - self._last_seen_flush >= self.PEER_LAST_SEEN_FLUSH_SEC
        writes = bool(dirty or leaves or flush_last_seen or baseline)

        if dirty:
            conn.executemany(
                """INSERT INTO peers_seen (peer_id, ip, port, account_id, first_seen, last_seen, connected, connected_since)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                   ON CONFLICT(peer_id) DO UPDATE SET
                     ip = excluded.ip, port = excluded.port,
                     account_id = COALESCE(excluded.account_id, peers_seen.account_id),
                     last_seen = excluded.last_seen,
                     connected_since = CASE WHEN peers_seen.connected = 1 THEN peers_seen.connected_since
                                            ELSE excluded.connected_since END,
                     connected = 1""",
                [(pid, *current[pid], now, now, now) for pid in dirty])
        if leaves:
            # last seen = the previous sample, when the peer was still connected
            left_at = self._prev_peer_sample or now
            conn.executemany("UPDATE peers_seen SET connected = 0, last_seen = ? WHERE peer_id = ?",
                             [(left_at, pid) for pid in leaves])
        if flush_last_seen:
            conn.executemany("UPDATE peers_seen SET last_seen = ? WHERE peer_id = ?",
                             [(now, pid) for pid in current if pid not in dirty])
            self._last_seen_flush = now
        if baseline:
            # We weren't watching before now (startup, paused collection, new RPC node), so a
            # peer's earlier connected_since can't be trusted; its session is counted from now.
            conn.executemany("UPDATE peers_seen SET connected_since = ? WHERE peer_id = ?",
                             [(now, pid) for pid in current])
        elif joins or leaves:
            conn.executemany(
                "INSERT INTO peer_events (ts, peer_id, ip, account_id, event) VALUES (?, ?, ?, ?, ?)",
                [(now, pid, current[pid][0], current[pid][2], "join") for pid in joins] +
                [(now, pid, known[pid][1], known[pid][3], "leave") for pid in leaves])
        if writes:
            conn.commit()

        for pid in leaves:
            known[pid][0] = False
        for pid, (ip, port, acct) in current.items():
            prev_acct = known[pid][3] if pid in known else None
            known[pid] = [True, ip, port, acct or prev_acct]
        if len(known) > 20000:  # bound memory: forget long-gone peers (their rows stay in the DB)
            for pid in [p for p, k in known.items() if not k[0]][:len(known) - 20000]:
                del known[pid]
        self._prev_peer_sample = now
        return (0, 0) if baseline else (len(joins), len(leaves))

    def _peer_row(self, pid, ip, port, account, current, nxt):
        row = {"peer_id": pid, "account_id": account, "ip": ip, "port": port,
               "validator": "current" if account in current else "next" if account in nxt else None}
        v = current.get(account)
        if v:
            row["stake"] = yocto_to_near(v.get("stake"))
            row["slashed"] = bool(v.get("is_slashed"))
            for kind in ("blocks", "chunks", "endorsements"):
                produced, expected = num(v.get(f"num_produced_{kind}")), num(v.get(f"num_expected_{kind}"))
                row[kind] = {"produced": produced, "expected": expected, "pct": ratio(produced, expected)}
        return row

    def _track_version(self, now, latest):
        """Append to version_history whenever neard version/build or protocol numbers change."""
        cur = (latest.get("version"), latest.get("build"),
               latest.get("protocol_version"), latest.get("latest_protocol_version"))
        if cur == getattr(self, "_last_version", None):
            return
        conn = self._db()
        last = conn.execute("SELECT version, build, protocol_version, latest_protocol_version "
                            "FROM version_history ORDER BY ts DESC LIMIT 1").fetchone()
        if last is None or tuple(last) != cur:
            conn.execute("INSERT INTO version_history VALUES (?, ?, ?, ?, ?)", (now, *cur))
            conn.commit()
        self._last_version = cur

    def shard_table(self, account, ping_lookup, include_endorsers=False):
        """Validators assigned to the same shard(s) as `account`, with per-shard stats.
        `ping_lookup(ip, port)` returns a peer's ping stats or None."""
        v = self.validators if isinstance(self.validators, dict) else {}
        current = [c for c in v.get("current_validators") or [] if isinstance(c, dict) and c.get("account_id")]
        me = next((c for c in current if c["account_id"] == account), None) if account else None
        if not me:
            reason = ("Validator data collection is turned off" if not self.config.get("collect_validator") else
                      "No validator account configured" if not account else
                      "Validator data not loaded yet" if not current else
                      f"{account} is not in the current validator set")
            return {"is_validator": False, "account": account, "reason": reason}

        # Map validator account -> connected peer address via known_producers, for the ping column.
        peer_addr = {p.get("id"): split_addr(p.get("addr")) for p in self.active_peers}
        acct_addr = {k["account_id"]: peer_addr[k.get("peer_id")] for k in self.known_producers
                     if k.get("account_id") and k.get("peer_id") in peer_addr}

        def shard_ids(c, field):
            ids = c.get(field)
            return [as_int(s) for s in ids] if isinstance(ids, list) else []

        def per_shard(c, kind, shard):
            ids = shard_ids(c, "shards" if kind == "chunks" else "shards_endorsed")
            if shard not in ids:
                return None
            i = ids.index(shard)
            prods, exps = c.get(f"num_produced_{kind}_per_shard"), c.get(f"num_expected_{kind}_per_shard")
            prod = num(prods[i]) if isinstance(prods, list) and i < len(prods) else None
            exp = num(exps[i]) if isinstance(exps, list) and i < len(exps) else None
            return {"produced": prod, "expected": exp, "pct": ratio(prod, exp)}

        # "Your shard" = the shard(s) you produce chunks for; chunk-validator-only nodes
        # (no chunk production) fall back to the shards they endorse.
        produced_shards = {s for s in shard_ids(me, "shards") if s is not None}
        my_shards = sorted(produced_shards or {s for s in shard_ids(me, "shards_endorsed") if s is not None})
        stakes = sorted((s for s in (yocto_to_near(c.get("stake")) for c in current) if s is not None), reverse=True)
        rows = []
        for shard in my_shards:
            for c in current:
                producer = shard in shard_ids(c, "shards")
                endorser = shard in shard_ids(c, "shards_endorsed")
                # Nearly every validator endorses every shard, so endorsers are opt-in.
                if not (producer or c["account_id"] == account or (include_endorsers and endorser)):
                    continue
                stake = yocto_to_near(c.get("stake"))
                be, bp = num(c.get("num_expected_blocks")), num(c.get("num_produced_blocks"))
                ip, port = acct_addr.get(c["account_id"], (None, None))
                ping = ping_lookup(ip, port) if ip else None
                rows.append({
                    "shard": shard, "account_id": str(c["account_id"]), "you": c["account_id"] == account,
                    "producer": producer, "endorser": endorser,
                    "stake": stake, "rank": 1 + sum(1 for s in stakes if stake is not None and s > stake),
                    "slashed": bool(c.get("is_slashed")),
                    "blocks": {"produced": bp, "expected": be, "pct": ratio(bp, be)},
                    "chunks": per_shard(c, "chunks", shard),
                    "endorsements": per_shard(c, "endorsements", shard),
                    "peer": {"ip": ip, "ping_avg": ping and ping.get("avg"),
                             "ping_last": ping and ping.get("last"), "loss_pct": ping and ping.get("loss_pct")}
                            if ip else None,
                })
        return {"is_validator": True, "account": account, "epoch_height": as_int(v.get("epoch_height")),
                "shards": my_shards, "produced_shards": sorted(produced_shards),
                "validators_time": self.validators_fetched_at, "rows": rows}

    def peers_table(self, conn, rng):
        """Active peers plus peers that disconnected within the range, with validator stats,
        connection session info and churn summary."""
        now = time.time()
        since = now - rng
        by_peer_id = {k["peer_id"]: k.get("account_id") for k in self.known_producers if k.get("peer_id")}
        v = self.validators if isinstance(self.validators, dict) else {}
        accounts = lambda xs: [x for x in xs or [] if isinstance(x, dict) and x.get("account_id")]  # noqa: E731
        current = {c["account_id"]: c for c in accounts(v.get("current_validators"))}
        nxt = {c["account_id"] for c in accounts(v.get("next_validators"))}
        seen = {r[0]: r[1:] for r in conn.execute(
            "SELECT peer_id, first_seen, last_seen, connected, connected_since, ip, port, account_id FROM peers_seen")}
        joins_by_peer = dict(conn.execute(
            "SELECT peer_id, COUNT(*) FROM peer_events WHERE event = 'join' AND ts >= ? GROUP BY peer_id", (since,)))

        rows, active_ids = [], set()
        for p in self.active_peers:
            pid = p.get("id")
            active_ids.add(pid)
            ip, port = split_addr(p.get("addr"))
            row = self._peer_row(pid, ip, port, p.get("account_id") or by_peer_id.get(pid), current, nxt)
            s = seen.get(pid)
            row.update(connected=True, connected_since=s[3] if s else None, first_seen=s[0] if s else None,
                       reconnects=joins_by_peer.get(pid, 0))
            rows.append(row)
        for pid, (first, last, connected, _, ip, port, account) in seen.items():
            if pid in active_ids or connected or last < since:
                continue
            row = self._peer_row(pid, ip, port, account, current, nxt)
            row.update(connected=False, last_seen=last, first_seen=first, reconnects=joins_by_peer.get(pid, 0))
            rows.append(row)

        totals = dict(conn.execute(
            "SELECT event, COUNT(*) FROM peer_events WHERE ts >= ? GROUP BY event", (since,)))
        top = [{"peer_id": pid, "name": acct or ip, "joins": n} for pid, acct, ip, n in conn.execute(
            """SELECT peer_id, MAX(account_id), MAX(ip), COUNT(*) AS n FROM peer_events
               WHERE event = 'join' AND ts >= ? GROUP BY peer_id HAVING n > 1 ORDER BY n DESC LIMIT 5""", (since,))]
        return {"time": self.peers_t, "validators_time": self.validators_fetched_at,
                "count": len(active_ids), "disconnected": len(rows) - len(active_ids), "peers": rows,
                "churn": {"joins": totals.get("join", 0), "leaves": totals.get("leave", 0), "top_reconnects": top}}

    @staticmethod
    def _validator_stats(v, account, latest):
        row = {}
        accounts = lambda xs: [x for x in xs or [] if isinstance(x, dict) and x.get("account_id")]  # noqa: E731
        current = accounts(v.get("current_validators"))
        row["epoch_height"] = as_int(v.get("epoch_height"))
        row["num_validators"] = len(current)
        stakes = sorted((s for s in (yocto_to_near(c.get("stake")) for c in current) if s is not None), reverse=True)
        if stakes:
            row["seat_price"] = stakes[-1]
        latest["total_stake"] = sum(stakes)
        if not account:
            latest["validator_status"] = "No validator account configured"
            return row

        me = next((c for c in current if c["account_id"] == account), None)
        nxt = next((c for c in accounts(v.get("next_validators")) if c["account_id"] == account), None)
        prop = next((c for c in accounts(v.get("current_proposals")) if c["account_id"] == account), None)
        kick = next((c for c in accounts(v.get("prev_epoch_kickout")) if c["account_id"] == account), None)
        latest["in_next_epoch"] = nxt is not None
        latest["has_proposal"] = prop is not None
        reason = kick.get("reason") if kick else None
        latest["kickout_reason"] = reason if isinstance(reason, (dict, str)) else None
        if nxt:
            latest["next_stake"] = yocto_to_near(nxt.get("stake"))
        if me:
            row["stake"] = yocto_to_near(me.get("stake"))
            latest["validator_status"] = "Active (slashed)" if me.get("is_slashed") else "Active"
            if row["stake"] is not None:
                latest["rank"] = 1 + sum(1 for s in stakes if s > row["stake"])
            pcts = []
            for kind in ("blocks", "chunks", "endorsements"):
                p, e = num(me.get(f"num_produced_{kind}")), num(me.get(f"num_expected_{kind}"))
                row[f"{kind}_produced"], row[f"{kind}_expected"] = p, e
                pct = ratio(p, e)
                if pct is not None:
                    row[f"{kind}_pct"] = pct
                    pcts.append(pct)
            latest["uptime_pct"] = sum(pcts) / len(pcts) if pcts else None
        else:
            latest["validator_status"] = (
                "Joining next epoch" if nxt else "Proposal pending" if prop else "Not in validator set")
        return row


def run_loop(name, config, collector, table, cols, db_path, stop, prune=False, enabled=lambda: True):
    conn = db_connect(db_path)
    last_prune = 0
    next_t = time.time()
    while not stop.is_set():
        try:
            if enabled():
                ts, row = collector.sample()
                if row:
                    db_insert(conn, table, cols, ts, row)
        except Exception as e:  # never let the collector die
            print(f"[{name}] sample error: {e}", flush=True)
        # Retention cleanup runs independently, so a failing sample can't stop old data being deleted.
        if prune and time.time() - last_prune > 60:
            last_prune = time.time()
            try:
                cutoff = time.time() - float(config.get("retention_hours")) * 3600
                for old in ("system", "near", "ping_rounds", "peer_pings", "peer_events", "node_logs"):
                    conn.execute(f"DELETE FROM {old} WHERE ts < ?", (cutoff,))
                conn.execute("DELETE FROM peers_seen WHERE connected = 0 AND last_seen < ?", (cutoff,))
                conn.commit()
                DATA_GEN.bump()
            except Exception as e:
                print(f"[{name}] retention cleanup error: {e}", flush=True)
        try:
            interval = config.interval
        except Exception as e:
            print(f"[{name}] bad interval setting ({e}); using 60s", flush=True)
            interval = 60
        next_t += interval
        if next_t < time.time():  # fell behind (slow RPC) — don't burst to catch up
            next_t = time.time() + interval
        # Wake early if config changes so a new interval applies immediately.
        if config.changed.wait(max(0, next_t - time.time())):
            config.changed.clear()
            next_t = time.time()
    conn.close()


# --------------------------------------------------------------------------- alerts

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


class AlertEngine:
    """Symptom-based alerts, evaluated every 30 s. Rules look at sustained windows (e.g. a 5-minute
    average) rather than single samples, so a brief spike doesn't page anyone, and they focus on what
    an operator would notice: the node falling behind, missed blocks, errors, latency, running out of
    space, failing hardware, expiring certificates."""

    INTERVAL = 30

    def __init__(self, app):
        self.app = app
        self.since = {}

    @staticmethod
    def _window(conn, table, cols, seconds, fn="AVG"):
        row = conn.execute(f"SELECT {','.join(f'{fn}({c})' for c in cols)} FROM {table} WHERE ts >= ?",
                           (time.time() - seconds,)).fetchone()
        return {c: (v if isinstance(v, (int, float)) else None) for c, v in zip(cols, row or [None] * len(cols))}

    @staticmethod
    def _latest(conn, table, col):
        r = conn.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
        return r[0] if r and isinstance(r[0], (int, float)) else None

    def evaluate(self, conn):
        app, out = self.app, []
        cfg, L = app.config.snapshot(), app.near.latest

        def add(id_, severity, title, detail):
            out.append({"id": id_, "severity": severity, "title": title, "detail": detail})

        # ---- NEAR node (RED: errors and duration as the node's users see them)
        if cfg["collect_node"]:
            w2 = self._window(conn, "near", ["ok", "block_lag_sec"], 120)
            w5 = self._window(conn, "near", ["rpc_latency_ms", "peers"], 300)
            if L.get("error") and w2["ok"] is not None and w2["ok"] < 0.5:
                add("rpc_down", "critical", "Node RPC is unreachable", f"{L['error']} (for over a minute)")
            elif L.get("syncing"):
                add("syncing", "critical", "Node is syncing", "The node isn't at the chain head, so it can't validate.")
            elif w2["block_lag_sec"] is not None and w2["block_lag_sec"] > 30:
                add("behind", "critical" if w2["block_lag_sec"] > 60 else "warning", "Node is falling behind",
                    f"Latest block is {w2['block_lag_sec']:.0f} s old on average over 2 minutes.")
            if w5["rpc_latency_ms"] is not None and w5["rpc_latency_ms"] > 1000:
                add("rpc_slow", "warning", "Node RPC is slow", f"/status takes {w5['rpc_latency_ms']:.0f} ms (5-min average).")
            if w5["peers"] is not None and w5["peers"] < 5:
                add("few_peers", "critical" if w5["peers"] < 2 else "warning", "Very few peers",
                    f"{w5['peers']:.0f} active peers (5-min average): the node may be isolated.")
        # ---- validator duties
        if cfg["collect_validator"] and L.get("validator_status"):
            near = {k: self._latest(conn, "near", k) for k in ("blocks_pct", "chunks_pct", "endorsements_pct")}
            low = {k.split("_")[0]: v for k, v in near.items() if v is not None and v < 95}
            if low:
                worst = min(low.values())
                add("missed", "critical" if worst < 80 else "warning", "Missing blocks/chunks/endorsements",
                    "This epoch: " + ", ".join(f"{k} {v:.1f}%" for k, v in low.items()) + " produced (below 95%).")
            if (L.get("validator_status") or "").startswith("Active") and L.get("in_next_epoch") is False:
                add("not_next", "critical", "Not in the next epoch's validator set",
                    "The validator is active now but will drop out next epoch.")
            if L.get("kickout_reason"):
                add("kicked", "warning", "Kicked out last epoch", json.dumps(L["kickout_reason"])[:200])
            if L.get("validators_stale"):
                add("val_stale", "warning", "Validator data is stale", "The validators RPC call hasn't succeeded recently.")
        if cfg["collect_logs"]:
            n = conn.execute("SELECT COUNT(*) FROM node_logs WHERE level = 'ERROR' AND ts >= ?",
                             (time.time() - 900,)).fetchone()[0]
            if n > 10:
                add("neard_errors", "warning", "neard is logging errors", f"{n} ERROR lines in the last 15 minutes.")
        # ---- host resources (USE): saturation and errors that users would notice
        if cfg["collect_system"]:
            w = self._window(conn, "system", ["disk_await_ms", "disk_read_iops", "disk_write_iops", "swap_in_bps",
                                               "mem_available", "mem_total", "tcp_retrans_pct", "net_errors_ps",
                                               "net_drops_ps", "cpu_steal", "cpu_iowait", "fd_pct"], 300)
            load10 = self._window(conn, "system", ["load_per_core"], 600)["load_per_core"]
            for fs in (app.system.ext.details.get("filesystems") or []):
                for kind, pct in (("space", fs.get("pct")), ("inodes", fs.get("inode_pct"))):
                    if pct is not None and pct >= 85:
                        add(f"fs_{kind}_{fs['mount']}", "critical" if pct >= 90 else "warning",
                            f"Filesystem {fs['mount']} is {pct:.0f}% full ({kind})",
                            "Free up space or grow the volume before it fills." if kind == "space" else
                            "Running out of inodes (too many files).")
            iops = (w["disk_read_iops"] or 0) + (w["disk_write_iops"] or 0)
            if w["disk_await_ms"] is not None and w["disk_await_ms"] > 50 and iops > 5:
                add("disk_latency", "warning", "Disk I/O latency is high",
                    f"Average request takes {w['disk_await_ms']:.0f} ms over 5 minutes ({iops:.0f} IOPS).")
            oom = self._window(conn, "system", ["oom_kills"], 3600, "SUM")["oom_kills"]
            if oom:
                add("oom", "critical", "Out-of-memory kills", f"The kernel killed {oom:.0f} process(es) in the last hour.")
            if w["swap_in_bps"] is not None and w["swap_in_bps"] > 1_000_000:
                add("swapping", "warning", "System is swapping", f"Swap-in {w['swap_in_bps'] / 1e6:.1f} MB/s (5-min average).")
            if w["mem_available"] is not None and w["mem_total"] and w["mem_available"] / w["mem_total"] < 0.05:
                add("mem_low", "warning", "Memory almost exhausted", "Less than 5% of memory available (5-min average).")
            if w["tcp_retrans_pct"] is not None and w["tcp_retrans_pct"] > 5:
                add("retrans", "warning", "High TCP retransmits", f"{w['tcp_retrans_pct']:.1f}% of segments retransmitted (5-min average).")
            if (w["net_errors_ps"] or 0) + (w["net_drops_ps"] or 0) > 10:
                add("net_errors", "warning", "Network errors and drops",
                    f"{(w['net_errors_ps'] or 0) + (w['net_drops_ps'] or 0):.0f} packet errors/drops per second (5-min average).")
            if w["cpu_steal"] is not None and w["cpu_steal"] > 10:
                add("steal", "warning", "CPU steal is high", f"{w['cpu_steal']:.0f}% of CPU time taken by the hypervisor (noisy neighbour).")
            if w["cpu_iowait"] is not None and w["cpu_iowait"] > 30:
                add("iowait", "warning", "CPU waiting on disk", f"{w['cpu_iowait']:.0f}% iowait (5-min average).")
            if load10 is not None and load10 > 2:
                add("load", "warning", "CPU is saturated", f"Load is {load10:.1f}× the core count (10-min average).")
            if w["fd_pct"] is not None and w["fd_pct"] > 80:
                add("fds", "warning", "File descriptors running out", f"{w['fd_pct']:.0f}% of the system limit in use.")
            zombies = self._latest(conn, "system", "proc_zombies")
            if zombies and zombies > 50:
                add("zombies", "warning", "Many zombie processes", f"{zombies:.0f} zombie processes.")
            temp = self._latest(conn, "system", "temp_max_c")
            if temp is not None and temp > 85:
                add("temp", "critical" if temp > 95 else "warning", "Hardware is running hot", f"Hottest sensor at {temp:.0f} °C.")
        # ---- slow health / security checks
        if cfg["collect_health"] and app.monitor is not None:
            r = app.monitor.snapshot()
            c = r.get("clock") or {}
            if c.get("synced") is False:
                add("ntp", "warning", "Clock isn't NTP-synchronised", c.get("detail", ""))
            if c.get("offset_ms") is not None and abs(c["offset_ms"]) > 100:
                add("clock_drift", "critical" if abs(c["offset_ms"]) > 500 else "warning", "Clock drift",
                    f"System clock is off by {c['offset_ms']:+.0f} ms.")
            if (r.get("dns") or {}).get("status") == "warn":
                add("dns", "warning", "DNS resolution is failing or slow", r["dns"].get("detail", ""))
            if (r.get("smart") or {}).get("status") == "fail":
                add("smart", "critical", "A drive is failing (SMART)", r["smart"].get("detail", ""))
            if (r.get("ipmi") or {}).get("status") == "fail":
                add("ipmi", "critical", "Power supply or fan failure", r["ipmi"].get("detail", ""))
            for cert in (r.get("tls") or {}).get("certs", []):
                if cert.get("status") in ("fail", "warn") and cert.get("days_left", 0) is not None:
                    add(f"tls_{cert['host']}", "critical" if cert["status"] == "fail" else "warning",
                        f"TLS certificate: {cert['host']}", cert.get("detail", ""))
            pt = r.get("patches") or {}
            if pt.get("security"):
                add("patches", "info", "Security updates pending", pt.get("detail", ""))
            elif pt.get("reboot_required"):
                add("reboot", "info", "Reboot required", "A reboot is needed to finish installing updates.")
            if (r.get("last_boot") or {}).get("unexpected"):
                add("unexpected_reboot", "warning", "Last reboot was unexpected", r["last_boot"].get("detail", ""))
            fails = self._window(conn, "system", ["failed_logins"], 3600, "SUM")["failed_logins"]
            if fails and fails > 100:
                add("logins", "warning", "Many failed logins", f"{fails:.0f} failed SSH logins in the last hour.")
        for item in (app.host_checks or {}).get("items", []):
            if item.get("status") == "fail":
                add(f"hostcheck_{item.get('id')}", "warning", f"Host check failed: {item.get('title')}", item.get("detail", ""))
        out.sort(key=lambda a: (SEVERITY_ORDER.get(a["severity"], 9), a["title"]))
        return out

    def run(self, db_path, stop):
        conn = db_connect(db_path)
        while not stop.is_set():
            try:
                alerts, now = self.evaluate(conn), time.time()
                ids = {a["id"] for a in alerts}
                self.since = {k: v for k, v in self.since.items() if k in ids}
                for a in alerts:
                    a["since"] = self.since.setdefault(a["id"], now)
                if [(a["id"], a["severity"]) for a in alerts] != [(a["id"], a["severity"]) for a in self.app.alerts]:
                    DATA_GEN.bump()
                self.app.alerts = alerts
            except Exception as e:
                print(f"[alerts] evaluation error: {e}", flush=True)
            stop.wait(self.INTERVAL)
        conn.close()


# --------------------------------------------------------------------------- http

class App:
    def __init__(self, config, near_collector, pinger, logs, system=None, monitor=None):
        self.config = config
        self.near = near_collector
        self.pinger = pinger
        self.logs = logs
        self.system = system
        self.monitor = monitor
        self.alerts = []
        self.host_checks = {"time": None, "running": True, "items": []}  # filled once at startup
        self.db_path = os.path.join(BASE_DIR, config.get("db_path"))
        self.pool = DBPool(self.db_path)


MAX_RANGE_SEC = 366 * 86400
MAX_POINTS = 2000


def host_of(value):
    """Host part of a Host header / netloc: 'localhost:8787' -> 'localhost', '[::1]:8787' -> '::1'."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:].partition("]")[0]
    return value.rpartition(":")[0] if value.count(":") == 1 else value


def allowed_hosts(bind_host, extra=()):
    hosts = {"localhost", "127.0.0.1", "::1", host_of(bind_host)}
    for name in (socket.gethostname(), socket.getfqdn()):
        hosts.update({name.lower(), name.lower().split(".")[0]})
    hosts.update(h.lower() for h in extra)
    hosts.discard("")
    return hosts


def make_handler(app, hosts):
    class Handler(BaseHTTPRequestHandler):
        # Drop a connection that sends nothing for this long, so a slow or stalled client can't
        # hold a server thread forever.
        timeout = REQUEST_TIMEOUT_SEC
        server_version = "NearNodeDashboard"   # don't advertise the Python version
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self):
            """Blocks DNS rebinding: the Host header must be this machine (by name) or an IP literal.
            A rebinding attack always arrives with the attacker's domain name as the Host."""
            host = host_of(self.headers.get("Host"))
            return host in hosts or valid_ip(host) is not None

        def _same_origin(self):
            """For state-changing requests: reject cross-site browser requests (CSRF)."""
            site = self.headers.get("Sec-Fetch-Site")
            if site and site not in ("same-origin", "none"):
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() != (self.headers.get("Host") or "").strip().lower():
                return False
            return True

        @staticmethod
        def _with_conn(fn):
            with app.pool.conn() as conn:
                return fn(conn)

        def _send_cached(self, key, compute):
            """Serve a JSON response from the cache while no new data has been stored; on a miss,
            compute and serialise it once for every client asking for the same view."""
            body = CACHE.get_or_compute(key, lambda: json.dumps(compute()).encode())
            return self._send(200, body)

        def _ping_stats(self, rng):
            # Shared by /api/peers and /api/shard; ping data only changes once per ping round.
            return CACHE.get_or_compute(("pingstats", rng), lambda: self._with_conn(
                lambda c: app.pinger.stats(c, rng)))

        @staticmethod
        def _qnum(q, name, default, lo, hi, cast=float):
            raw = q.get(name, [str(default)])[0]
            try:
                value = cast(float(raw))
            except (TypeError, ValueError):
                raise ValueError(f"query parameter {name!r} must be a number")
            if not math.isfinite(value):
                raise ValueError(f"query parameter {name!r} must be a finite number")
            return min(max(value, lo), hi)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "Host not allowed"})
            try:
                return self._get()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                print(f"[http] {self.path}: {e!r}", flush=True)
                return self._send(500, {"error": "internal error"})

        def _get(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/config":
                return self._send(200, app.config.snapshot())
            if u.path == "/api/latest":
                return self._send(200, {**app.near.latest, "host_checks": app.host_checks,
                                        "server_hostname": socket.gethostname(), "app_version": VERSION_INFO,
                                        "read_only": bool(app.config.get("read_only"))})
            if u.path == "/api/health":
                det = app.system.ext.details if app.system else {}
                return self._send(200, {
                    "time": time.time(), "alerts": app.alerts,
                    "checks": app.monitor.snapshot() if app.monitor else {},
                    "filesystems": det.get("filesystems", []), "disks": det.get("disks", []),
                    "nics": det.get("nics", []), "tcp_states": det.get("tcp_states", {}),
                    "temperatures": det.get("temperatures", []), "fans": det.get("fans", []),
                    "cores": psutil.cpu_count(), "platform": sys.platform,
                })
            # Only API routes take ?range=; static files ignore query strings.
            rng = self._qnum(q, "range", 3600, 60, MAX_RANGE_SEC) if u.path.startswith("/api/") else None
            if u.path == "/api/logs":
                level, text = q.get("level", ["all"])[0].upper(), q.get("q", [""])[0].strip()[:200]
                return self._send_cached(("logs", rng, level, text), lambda: self._with_conn(
                    lambda c: app.logs.query(c, rng, level, text)))
            if u.path == "/api/shard":
                endorsers = q.get("endorsers", ["0"])[0] == "1"

                def shard():
                    ping, _ = self._ping_stats(rng)
                    account = app.near.latest.get("validator_account_id") or app.config.get("validator_account_id")
                    return app.near.shard_table(account, lambda ip, port: app.pinger.lookup(ping, ip, port), endorsers)
                return self._send_cached(("shard", rng, endorsers), shard)
            if u.path == "/api/peers":
                def peers():
                    table = self._with_conn(lambda c: app.near.peers_table(c, rng))
                    ping, ping_bucket = self._ping_stats(rng)
                    for row in table["peers"]:
                        row["ping"] = app.pinger.lookup(ping, row["ip"], row["port"])
                    cfg = app.config.snapshot()
                    table["ping"] = {"enabled": cfg["ping_enabled"], "method": cfg["ping_method"],
                                     "interval": cfg["ping_interval_seconds"], "error": app.pinger.error,
                                     "last_round": app.pinger.last_round, "bucket": ping_bucket}
                    table["now"] = time.time()
                    table["range"] = rng
                    return table
                return self._send_cached(("peers", rng), peers)
            if u.path == "/api/metrics":
                points = self._qnum(q, "points", 720, 10, MAX_POINTS, int)

                def metrics():
                    interval = app.config.interval
                    # bucket >= range/points, so every series (incl. the zero-filled log series) has
                    # at most ~points entries however large `range` is.
                    bucket = max(interval, math.ceil(rng / points))
                    ping_bucket = max(bucket, int(app.config.get("ping_interval_seconds")))
                    since = time.time() - rng

                    def query(conn):
                        return {
                            "now": time.time(), "range": rng, "bucket": bucket, "interval": interval,
                            "system": db_query(conn, "system", SYSTEM_COLS, since, bucket),
                            "near": db_query(conn, "near", NEAR_COLS, since, bucket),
                            "versions": version_changes(conn, since),
                            "epochs": epoch_marks(conn, since),
                            "logs": app.logs.series(conn, since, max(bucket, 60)),
                            "ping": {**db_query(conn, "ping_rounds", PING_COLS, since, ping_bucket),
                                     "bucket": ping_bucket},
                        }
                    return self._with_conn(query)
                return self._send_cached(("metrics", rng, points), metrics)
            return self._static(u.path)

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "Host not allowed"})
            if urlparse(self.path).path != "/api/config":
                return self._send(404, {"error": "not found"})
            # CSRF protection. Requiring a JSON content type forces browsers to send a CORS
            # preflight for cross-site requests (which this server never approves), and the
            # Origin / Sec-Fetch-Site check rejects cross-site requests outright.
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._send(415, {"error": "Content-Type must be application/json"})
            if not self._same_origin():
                return self._send(403, {"error": "cross-site request rejected"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 <= length <= 64 * 1024:
                    raise ValueError("request body too large")
                patch = json.loads(self.rfile.read(length) or b"{}")
                return self._send(200, app.config.update(patch))
            except PermissionError as e:   # read-only mode
                return self._send(403, {"error": str(e), "read_only": True})
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.realpath(os.path.join(STATIC_DIR, path.lstrip("/")))
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                     ".css": "text/css"}.get(os.path.splitext(full)[1], "application/octet-stream")
            with open(full, "rb") as f:
                return self._send(200, f.read(), ctype)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (default 0.0.0.0 = all IPv4 addresses; '::' = all IPv4 and IPv6; "
                         "127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra hostname the dashboard may be reached by (e.g. a DNS name or SSH alias); "
                         "localhost, this machine's hostname and IP addresses are always allowed")
    args = ap.parse_args()

    global VERSION_INFO
    VERSION_INFO = version_info()
    config = Config(args.config)
    db_path = os.path.join(BASE_DIR, config.get("db_path"))
    db_init(db_path)

    stop = threading.Event()
    near = NearCollector(config)
    near.db_path = db_path
    # The two collectors share one `changed` event; give each its own view of it.
    sys_cfg, near_cfg = config, _ConfigView(config)
    system = SystemCollector(config)
    monitor = health.HealthMonitor(config, on_change=DATA_GEN.bump)
    system.monitor = monitor
    threading.Thread(target=monitor.run, args=(stop,), daemon=True).start()
    threading.Thread(target=run_loop, daemon=True,
                     args=("system", sys_cfg, system, "system", SYSTEM_COLS, db_path, stop, True,
                           lambda: config.get("collect_system"))).start()
    threading.Thread(target=run_loop, daemon=True,
                     args=("near", near_cfg, near, "near", NEAR_COLS, db_path, stop, False,
                           lambda: any(config.get(k) for k in ("collect_node", "collect_validator", "collect_peers")))).start()

    pinger = Pinger(config, near)
    threading.Thread(target=pinger.run, args=(db_path, stop), daemon=True).start()

    logs = LogCollector(config)
    threading.Thread(target=logs.run, args=(db_path, stop), daemon=True).start()

    app = App(config, near, pinger, logs, system, monitor)
    threading.Thread(target=AlertEngine(app).run, args=(db_path, stop), daemon=True).start()

    def host_checks():  # once per service start, off the main thread (firewall tools can be slow)
        app.host_checks = run_host_checks(config, near, stop=stop)
        bad = [i["title"] for i in app.host_checks["items"] if i["status"] in ("fail", "warn")]
        if bad:
            print(f"[checks] needs attention: {', '.join(bad)}", flush=True)

    threading.Thread(target=host_checks, daemon=True).start()

    server = make_server(args.host, args.port, make_handler(app, allowed_hosts(args.host, args.allow_host)))
    print(f"Near Node Dashboard {APP_VERSION}"
          + (f" ({VERSION_INFO['commit']}, {VERSION_INFO['date']})" if VERSION_INFO.get("commit") else "")
          + f" listening on {args.host}:{args.port}  "
          f"(interval {config.interval}s, retention {config.get('retention_hours')}h, rpc {config.get('rpc_url')})",
          flush=True)
    for url in listen_urls(args.host, args.port):
        print(f"  {url}", flush=True)
    if config.get("read_only"):
        print("  Read-only mode: settings can't be changed from the web page (read_only in config.json).",
              flush=True)
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        print("  Note: the dashboard has no login. Anyone who can reach this port can view it and change "
              "its settings; restrict the port with a firewall.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


REQUEST_TIMEOUT_SEC = 30
MAX_CONNECTIONS = 64            # simultaneous connections, all clients
MAX_CONNECTIONS_PER_CLIENT = 16


class BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that caps simultaneous connections (overall and per client IP) and
    request read time, so one client can't exhaust threads with many slow connections."""
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self._conn_lock = threading.Lock()
        self._active = 0
        self._per_client = {}
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        ip = client_address[0]
        with self._conn_lock:
            if self._active >= MAX_CONNECTIONS or self._per_client.get(ip, 0) >= MAX_CONNECTIONS_PER_CLIENT:
                refused = True
            else:
                refused = False
                self._active += 1
                self._per_client[ip] = self._per_client.get(ip, 0) + 1
        if refused:
            self.shutdown_request(request)  # over the limit: close right away
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(ip)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def _release(self, ip):
        with self._conn_lock:
            self._active -= 1
            n = self._per_client.get(ip, 1) - 1
            if n > 0:
                self._per_client[ip] = n
            else:
                self._per_client.pop(ip, None)


class DualStackServer(BoundedServer):
    """Listens on IPv6 *and* IPv4 when bound to '::'."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def make_server(host, port, handler):
    if host == "::" or (valid_ip(host) and ":" in host):
        try:
            return DualStackServer((host, port), handler)
        except OSError as e:
            if host != "::":
                raise
            print(f"[http] IPv6 unavailable ({e}); listening on all IPv4 addresses instead", flush=True)
            host = "0.0.0.0"
    return BoundedServer((host, port), handler)


def listen_urls(host, port):
    """URLs the dashboard can be opened at, for the startup message."""
    if host not in ("0.0.0.0", "::"):
        return [f"http://{'[' + host + ']' if ':' in host else host}:{port}"]
    urls = [f"http://127.0.0.1:{port}"]
    try:
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                ip = valid_ip(a.address.split("%")[0])
                if not ip or ipaddress.ip_address(ip).is_loopback or ipaddress.ip_address(ip).is_link_local:
                    continue
                if ":" in ip and host != "::":
                    continue
                urls.append(f"http://{'[' + ip + ']' if ':' in ip else ip}:{port}")
    except Exception:
        pass
    return urls


class _ConfigView:
    """Proxy to Config with its own change event, so both loops wake on a config update."""

    def __init__(self, base):
        self._base = base
        self.changed = threading.Event()
        original = base.update

        def update(patch):
            result = original(patch)
            self.changed.set()
            return result

        base.update = update

    def __getattr__(self, name):
        return getattr(self._base, name)


if __name__ == "__main__":
    main()
