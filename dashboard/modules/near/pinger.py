"""Pings every active peer once per round (ICMP via the system ping, or a TCP connect to the
peer's NEAR port) and keeps per-peer round-trip statistics."""

import math
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from dashboard.core.storage import db_connect, db_insert
from dashboard.core.util import split_addr, valid_ip, valid_port


PING_COLS = ["peers", "replied", "median_ms", "p90_ms", "loss_pct"]

def ping_key(ip, port, method):
    """Ping results are keyed by host for ICMP (same host = same answer) and host:port for TCP."""
    return f"{ip}:{port}" if method == "tcp" else ip

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
