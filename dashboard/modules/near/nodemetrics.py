"""neard's own Prometheus metrics (/metrics): what /status and the validators RPC don't show.

Collected every metrics_interval_seconds (60 s by default, not affected by 1s mode): chunk
endorsement health (accepted/rejected, state witness network round trip and validation time,
validation failures), finality lag, tier-1/tier-2 peer connections, quality of the blocks this
node produces, block and chunk processing times, RocksDB compaction backlog and size, and
protocol upgrade voting. Counters and histograms are turned into per-interval rates, averages and
90th percentiles from the difference between two scrapes.

The endpoint is large (~3 MB on mainnet, not compressible by neard), so only the lines needed are
parsed, and a dashboard reading a remote node should keep the interval long."""

import math
import re
import time
import urllib.request

from dashboard.core.util import MAX_RPC_RESPONSE_BYTES

NEARD_COLS = [
    # 1. chunk endorsements (received while about to produce a block) and state witnesses
    "endorse_accepted_ps", "endorse_rejected_ps", "endorse_reject_pct",
    "witness_rtt_ms", "witness_rtt_p90_ms", "witness_validation_ms", "witness_validation_failed",
    # 2. finality
    "finality_lag_blocks",
    # 3. peer connections by tier (T1 = direct validator connections)
    "peers_t1", "peers_t2",
    # 4. blocks this node produced
    "blocks_produced_n", "endorsed_stake_pct", "excluded_chunks", "missing_endorsements",
    # 5. processing times
    "block_processing_ms", "block_processing_p90_ms", "produce_chunk_ms", "produce_distribute_chunk_ms",
    # 6. RocksDB
    "db_pending_compaction_bytes", "db_running_compactions", "db_sst_bytes",
    # 7. protocol upgrade
    "node_protocol_version", "protocol_version_next", "protocol_upgrade_height",
    "is_validator", "is_block_producer",
]

PREFIXES = (
    "near_chunk_endorsements_accepted", "near_chunk_endorsements_rejected",
    "near_chunk_state_witness_network_roundtrip_time_", "near_chunk_state_witness_validation_time_",
    "near_chunk_witness_validation_failed_total",
    "near_block_height_head", "near_final_block_height",
    "near_peer_connections{",
    "near_block_produced_total", "near_block_producer_endorsed_stake_ratio_",
    "near_block_producer_excluded_chunks_count", "near_block_producer_missing_endorsement_count_",
    "near_block_processing_time_", "near_produce_chunk_time_", "near_produce_and_distribute_chunk_time_",
    "near_rocksdb_estimate_pending_compaction_bytes", "near_rocksdb_num_running_compactions",
    "near_rocksdb_live_sst_files_size",
    "near_protocol_version_votes", "near_protocol_version_next", "near_node_protocol_version",
    "near_protocol_upgrade_block_height", "near_node_protocol_upgrade_voting_start",
    "near_is_validator", "near_is_block_producer",
)

LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse(text, prefixes=PREFIXES):
    """{metric name: [(labels, value)]} for the lines starting with one of `prefixes`."""
    out = {}
    for line in text.splitlines():
        if not line.startswith(prefixes):
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        out.setdefault(m.group(1), []).append((dict(LABEL_RE.findall(m.group(2) or "")), value))
    return out


def quantile(buckets, q):
    """Prometheus-style histogram_quantile over {upper bound: cumulative count} (deltas)."""
    bounds = sorted(buckets)
    if not bounds or buckets[bounds[-1]] <= 0:
        return None
    target, prev_bound, prev_count = q * buckets[bounds[-1]], 0.0, 0.0
    for b in bounds:
        c = buckets[b]
        if c >= target:
            if math.isinf(b):
                return prev_bound
            span = c - prev_count
            return prev_bound + (b - prev_bound) * ((target - prev_count) / span if span > 0 else 1)
        prev_bound, prev_count = b, c
    return None


class Scrape:
    """One parsed /metrics response with helpers for totals and histograms."""

    def __init__(self, metrics):
        self.m = metrics

    def total(self, name, **match):
        rows = [v for labels, v in self.m.get(name, []) if all(labels.get(k) == val for k, val in match.items())]
        return sum(rows) if rows else None

    def by_label(self, name, label):
        out = {}
        for labels, v in self.m.get(name, []):
            out[labels.get(label, "")] = out.get(labels.get(label, ""), 0) + v
        return out

    def buckets(self, name):
        out = {}
        for labels, v in self.m.get(name + "_bucket", []):
            le = labels.get("le")
            if le is not None:
                b = math.inf if le == "+Inf" else float(le)
                out[b] = out.get(b, 0) + v
        return out


class NodeMetrics:
    def __init__(self, config):
        self.config = config
        self.prev = None          # (time, Scrape) of the previous successful scrape
        self.latest = {"error": "not collected yet"}

    def url(self):
        return (self.config.get("metrics_url") or (self.config.get("rpc_url") or "").rstrip("/") + "/metrics")

    def fetch(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "near-node-dashboard"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(MAX_RPC_RESPONSE_BYTES + 1)
        if len(body) > MAX_RPC_RESPONSE_BYTES:
            raise ValueError("/metrics response too large")
        return body.decode("utf-8", "replace")

    def sample(self):
        url, now = self.url(), time.time()
        try:
            cur = Scrape(parse(self.fetch(url)))
        except Exception as e:
            self.latest = {"time": now, "url": url, "error": f"{type(e).__name__}: {e}"[:300]}
            return now, {}
        if not cur.m:
            self.latest = {"time": now, "url": url, "error": "no neard metrics found at this URL"}
            return now, {}
        row = self._gauges(cur)
        if self.prev:
            row.update(self._rates(self.prev[1], cur, now - self.prev[0]))
        self.prev = (now, cur)
        self.latest = {
            "time": now, "url": url, "error": None,
            "votes": cur.by_label("near_protocol_version_votes", "protocol_version"),
            "voting_start": cur.by_label("near_node_protocol_upgrade_voting_start", "protocol_version"),
            "rejected_by_reason": cur.by_label("near_chunk_endorsements_rejected", "reason"),
            "validation_failed_by_error": cur.by_label("near_chunk_witness_validation_failed_total", "error"),
            "excluded_by_reason": cur.by_label("near_block_producer_excluded_chunks_count", "reason"),
            **{k: row.get(k) for k in ("node_protocol_version", "protocol_version_next", "protocol_upgrade_height",
                                       "is_validator", "is_block_producer", "finality_lag_blocks", "peers_t1", "peers_t2")},
        }
        return now, row

    @staticmethod
    def _gauges(s):
        row = {}
        head, final = s.total("near_block_height_head"), s.total("near_final_block_height")
        if head is not None and final is not None:
            row["finality_lag_blocks"] = max(0, head - final)
        tiers = s.by_label("near_peer_connections", "tier")
        if tiers:
            row["peers_t1"], row["peers_t2"] = tiers.get("T1", 0), tiers.get("T2", 0)
        row["db_pending_compaction_bytes"] = s.total("near_rocksdb_estimate_pending_compaction_bytes")
        row["db_running_compactions"] = s.total("near_rocksdb_num_running_compactions")
        row["db_sst_bytes"] = s.total("near_rocksdb_live_sst_files_size")
        row["node_protocol_version"] = s.total("near_node_protocol_version")
        row["protocol_version_next"] = s.total("near_protocol_version_next")
        up = s.total("near_protocol_upgrade_block_height")
        row["protocol_upgrade_height"] = up if up else None     # 0 = no upgrade scheduled
        row["is_validator"] = s.total("near_is_validator")
        row["is_block_producer"] = s.total("near_is_block_producer")
        return {k: v for k, v in row.items() if v is not None}

    @staticmethod
    def _rates(p, c, dt):
        """Per-interval values from the change between two scrapes (None after a neard restart)."""
        def delta(name, **match):
            a, b = p.total(name, **match), c.total(name, **match)
            return b - a if a is not None and b is not None and b >= a else None

        def avg_ms(hist):
            ds, dc = delta(hist + "_sum"), delta(hist + "_count")
            return ds / dc * 1000 if ds is not None and dc else None

        def p90_ms(hist):
            pb, cb = p.buckets(hist), c.buckets(hist)
            d = {b: cb[b] - pb.get(b, 0) for b in cb}
            if not d or any(v < 0 for v in d.values()):
                return None
            q = quantile(d, 0.9)
            return q * 1000 if q is not None else None

        row = {}
        acc, rej = delta("near_chunk_endorsements_accepted"), delta("near_chunk_endorsements_rejected")
        if acc is not None and dt > 0:
            row["endorse_accepted_ps"] = acc / dt
        if rej is not None and dt > 0:
            row["endorse_rejected_ps"] = rej / dt
        if acc is not None and rej is not None and acc + rej > 0:
            row["endorse_reject_pct"] = rej / (acc + rej) * 100
        row["witness_rtt_ms"] = avg_ms("near_chunk_state_witness_network_roundtrip_time")
        row["witness_rtt_p90_ms"] = p90_ms("near_chunk_state_witness_network_roundtrip_time")
        row["witness_validation_ms"] = avg_ms("near_chunk_state_witness_validation_time")
        failed = delta("near_chunk_witness_validation_failed_total")
        # A failure counter only appears once the first failure happens: absent before = 0.
        if failed is None and c.total("near_chunk_witness_validation_failed_total") is not None \
                and p.total("near_chunk_witness_validation_failed_total") is None:
            failed = c.total("near_chunk_witness_validation_failed_total")
        row["witness_validation_failed"] = failed if failed is not None else 0
        row["blocks_produced_n"] = delta("near_block_produced_total")
        ds, dc = delta("near_block_producer_endorsed_stake_ratio_sum"), delta("near_block_producer_endorsed_stake_ratio_count")
        if ds is not None and dc:
            row["endorsed_stake_pct"] = ds / dc * 100
        row["excluded_chunks"] = delta("near_block_producer_excluded_chunks_count")   # counter, by reason/shard
        ms, mc = delta("near_block_producer_missing_endorsement_count_sum"), delta("near_block_producer_missing_endorsement_count_count")
        if ms is not None and mc:
            row["missing_endorsements"] = ms / mc
        row["block_processing_ms"] = avg_ms("near_block_processing_time")
        row["block_processing_p90_ms"] = p90_ms("near_block_processing_time")
        row["produce_chunk_ms"] = avg_ms("near_produce_chunk_time")
        row["produce_distribute_chunk_ms"] = avg_ms("near_produce_and_distribute_chunk_time")
        return {k: v for k, v in row.items() if v is not None}
