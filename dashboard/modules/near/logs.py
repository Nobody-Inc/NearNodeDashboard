"""Follows the neard service log (journald unit or a log file) and stores WARN/ERROR/panic lines."""

import json
import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime

from dashboard.core.storage import DATA_GEN, db_connect
from dashboard.modules.near.collector import parse_near_time


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
