"""NEAR application module: node and validator stats, peers and pings, the neard service log,
same-shard validators, epoch markers and the startup host checks."""

import json
import threading
import time

from dashboard.core.alerts import alert
from dashboard.core.collect import run_loop
from dashboard.core.config import Setting, boolean, choice, integer, text, url
from dashboard.core.module import Module
from dashboard.core.storage import CACHE, db_query
from dashboard.modules.near.collector import NEAR_COLS, NearCollector, epoch_marks, version_changes
from dashboard.modules.near.hostchecks import run_host_checks
from dashboard.modules.near.logs import LogCollector
from dashboard.modules.near.pinger import PING_COLS, Pinger


class NearModule(Module):
    name = "near"
    title = "NEAR"
    settings = {
        # NEAR RPC endpoint for /status, /network_info and JSON-RPC. For a validator, your own node.
        "rpc_url": Setting("http://127.0.0.1:3030", url(required=True)),
        # Optional separate endpoint for the (large) `validators` call. Blank = rpc_url.
        "validators_rpc_url": Setting("", url(required=False)),
        # How often to call `validators` (~400 KB on mainnet: it returns every validator).
        "validators_interval_seconds": Setting(120, integer(1, 86400)),
        # Validator pool to track. Blank = the validator_account_id the node reports.
        "validator_account_id": Setting("", text),
        "collect_node": Setting(True, boolean),
        "collect_validator": Setting(True, boolean),  # also feeds the "validators on your shard" table
        "collect_peers": Setting(True, boolean),      # peer tracking, churn and pings
        "collect_logs": Setting(True, boolean),       # WARN/ERROR lines from the neard service log
        # Where to read the neard log: "journald" (systemd unit) or "file" (plain or Docker json logs).
        "log_source": Setting("journald", choice("journald", "file")),
        "log_unit": Setting("neard", text),
        "log_file": Setting("", text),
        "log_poll_seconds": Setting(10, integer(1, 3600)),
        # Peer pinging: every active peer once per round; "tcp" times a connect to the NEAR port.
        "ping_enabled": Setting(True, boolean),
        "ping_method": Setting("icmp", choice("icmp", "tcp")),
        "ping_interval_seconds": Setting(60, integer(5, 86400)),   # not affected by fast_mode
        "ping_timeout_ms": Setting(2000, integer(100, 10000)),
        # neard's P2P port (network.addr in ~/.near/config.json); checked for inbound access.
        "p2p_port": Setting(24567, integer(1, 65535)),
        # Read firewall rules with `sudo -n` (off: each attempt is logged as a failed sudo login).
        "firewall_check_sudo": Setting(False, boolean),
    }
    series = {"near": NEAR_COLS, "ping_rounds": PING_COLS}
    aggregations = {**{c: "MAX" for c in ("block_height", "epoch_height", "max_peers", "blocks_produced",
                                          "blocks_expected", "chunks_produced", "chunks_expected",
                                          "endorsements_produced", "endorsements_expected", "epoch_progress_pct",
                                          "protocol_version", "latest_protocol_version", "epoch_start_height")},
                    "ok": "MIN", "peer_joins": "SUM", "peer_leaves": "SUM"}
    schema = [
        "CREATE TABLE IF NOT EXISTS peer_pings (ts REAL, ip TEXT, peer_id TEXT, rtt_ms REAL)",
        "CREATE INDEX IF NOT EXISTS peer_pings_ip_ts ON peer_pings (ip, ts)",
        "CREATE INDEX IF NOT EXISTS peer_pings_ts ON peer_pings (ts)",
        # One row per peer ever seen; `connected` is the state as of the latest /network_info.
        """CREATE TABLE IF NOT EXISTS peers_seen (
        peer_id TEXT PRIMARY KEY, ip TEXT, port TEXT, account_id TEXT,
        first_seen REAL, last_seen REAL, connected INTEGER, connected_since REAL)""",
        "CREATE TABLE IF NOT EXISTS peer_events (ts REAL, peer_id TEXT, ip TEXT, account_id TEXT, event TEXT)",
        "CREATE INDEX IF NOT EXISTS peer_events_ts ON peer_events (ts)",
        # WARN/ERROR lines from the neard log; `sig` normalises numbers/hashes/IPs to group repeats.
        "CREATE TABLE IF NOT EXISTS node_logs (ts REAL, level TEXT, target TEXT, message TEXT, sig TEXT)",
        "CREATE UNIQUE INDEX IF NOT EXISTS node_logs_dedupe ON node_logs (ts, level, message)",
        "CREATE INDEX IF NOT EXISTS node_logs_ts ON node_logs (ts)",
        "CREATE INDEX IF NOT EXISTS node_logs_sig ON node_logs (sig, level, ts)",
        # One row per change of neard version/build or protocol numbers (never pruned; it's tiny).
        """CREATE TABLE IF NOT EXISTS version_history (
        ts REAL, version TEXT, build TEXT, protocol_version INTEGER, latest_protocol_version INTEGER)""",
    ]
    retention = [f"DELETE FROM {t} WHERE ts < ?" for t in ("near", "ping_rounds", "peer_pings", "peer_events", "node_logs")] \
        + ["DELETE FROM peers_seen WHERE connected = 0 AND last_seen < ?"]

    def __init__(self, app):
        super().__init__(app)
        self.near = NearCollector(self.config)
        self.near.db_path = app.db_path
        self.pinger = Pinger(self.config, self.near)
        self.logs = LogCollector(self.config)
        self.host_checks = {"time": None, "running": True, "items": []}  # filled once at startup

    def start(self, stop):
        cfg = self.config
        threading.Thread(target=run_loop, daemon=True,
                         args=(self.app, "near", self.near, "near", NEAR_COLS, stop,
                               lambda: any(cfg.get(k) for k in ("collect_node", "collect_validator", "collect_peers")))).start()
        threading.Thread(target=self.pinger.run, args=(self.app.db_path, stop), daemon=True).start()
        threading.Thread(target=self.logs.run, args=(self.app.db_path, stop), daemon=True).start()

        def host_checks():  # once per service start, off the main thread (firewall tools can be slow)
            self.host_checks = run_host_checks(cfg, self.near, stop=stop)
            bad = [i["title"] for i in self.host_checks["items"] if i["status"] in ("fail", "warn")]
            if bad:
                print(f"[checks] needs attention: {', '.join(bad)}", flush=True)
        threading.Thread(target=host_checks, daemon=True).start()

    def describe(self):
        return f"rpc {self.config.get('rpc_url')}"

    def latest(self):
        return {**self.near.latest, "host_checks": self.host_checks}

    def metrics(self, conn, since, bucket):
        ping_bucket = max(bucket, int(self.config.get("ping_interval_seconds")))
        return {
            "near": db_query(conn, "near", NEAR_COLS, since, bucket),
            "versions": version_changes(conn, since),
            "epochs": epoch_marks(conn, since),
            "logs": self.logs.series(conn, since, max(bucket, 60)),
            "ping": {**db_query(conn, "ping_rounds", PING_COLS, since, ping_bucket), "bucket": ping_bucket},
        }

    # ---------------------------------------------------------------- routes
    def routes(self):
        return {"/api/logs": self.logs_route, "/api/shard": self.shard_route, "/api/peers": self.peers_route}

    def _ping_stats(self, req):
        # Shared by /api/peers and /api/shard; ping data only changes once per ping round.
        return CACHE.get_or_compute(("pingstats", req.rng), lambda: req.with_conn(lambda c: self.pinger.stats(c, req.rng)))

    def logs_route(self, req):
        rng, level, text_ = req.rng, req.param("level", "all").upper(), req.param("q").strip()[:200]
        return req.cached(("logs", rng, level, text_), lambda: req.with_conn(lambda c: self.logs.query(c, rng, level, text_)))

    def shard_route(self, req):
        endorsers = req.param("endorsers", "0") == "1"

        def shard():
            ping, _ = self._ping_stats(req)
            account = self.near.latest.get("validator_account_id") or self.config.get("validator_account_id")
            return self.near.shard_table(account, lambda ip, port: self.pinger.lookup(ping, ip, port), endorsers)
        return req.cached(("shard", req.rng, endorsers), shard)

    def peers_route(self, req):
        rng = req.rng

        def peers():
            table = req.with_conn(lambda c: self.near.peers_table(c, rng))
            ping, ping_bucket = self._ping_stats(req)
            for row in table["peers"]:
                row["ping"] = self.pinger.lookup(ping, row["ip"], row["port"])
            cfg = self.config.snapshot()
            table["ping"] = {"enabled": cfg["ping_enabled"], "method": cfg["ping_method"],
                             "interval": cfg["ping_interval_seconds"], "error": self.pinger.error,
                             "last_round": self.pinger.last_round, "bucket": ping_bucket}
            table["now"] = time.time()
            table["range"] = rng
            return table
        return req.cached(("peers", rng), peers)

    # ---------------------------------------------------------------- alerts
    def alerts(self, ctx):
        """The node's symptoms (RED): unreachable, behind, slow; missed duties; errors; host checks."""
        out, cfg, L = [], ctx.config, self.near.latest

        def add(id_, severity, title, detail):
            out.append(alert(id_, severity, title, detail))

        # ---- NEAR node (RED: errors and duration as the node's users see them)
        if cfg["collect_node"]:
            w2 = ctx.window("near", ["ok", "block_lag_sec"], 120)
            w5 = ctx.window("near", ["rpc_latency_ms", "peers"], 300)
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
            near = {k: ctx.latest("near", k) for k in ("blocks_pct", "chunks_pct", "endorsements_pct")}
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
            n = ctx.conn.execute("SELECT COUNT(*) FROM node_logs WHERE level = 'ERROR' AND ts >= ?",
                             (time.time() - 900,)).fetchone()[0]
            if n > 10:
                add("neard_errors", "warning", "neard is logging errors", f"{n} ERROR lines in the last 15 minutes.")
        for item in (self.host_checks or {}).get("items", []):
            if item.get("status") == "fail":
                add(f"hostcheck_{item.get('id')}", "warning", f"Host check failed: {item.get('title')}", item.get("detail", ""))
        return out


MODULE = NearModule
