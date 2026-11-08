"""NEAR node collection: /status, /network_info and the validators RPC; peer tracking (joins,
leaves, sessions), version history, and the peers and same-shard validator tables."""

import json
import threading
import time
from datetime import datetime

from dashboard.core.storage import DATA_GEN, db_connect
from dashboard.core.util import as_int, http_json, num, ratio, split_addr


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

NETWORK_INFO_MIN_SEC = 10   # /network_info is fetched at most this often (matters in 1s mode)

# A node counts as "synced" when RPC answers, it isn't syncing, and its head block is this fresh.
SYNC_LAG_THRESHOLD_SEC = 30

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
