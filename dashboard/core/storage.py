"""SQLite storage: connections, the data-generation counter, the response cache, a connection pool,
and generic time-series tables whose columns and bucket aggregation are declared by the modules."""

import contextlib
import os
import queue
import sqlite3
import threading
import time
from collections import OrderedDict


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

def db_insert(conn, table, cols, ts, row):
    names = ["ts"] + cols
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
        [ts] + [row.get(c) for c in cols],
    )
    conn.commit()
    DATA_GEN.bump()


# How each column is combined when samples are averaged into time buckets ("MAX", "MIN" or
# "SUM"; anything not listed is averaged). Filled from the enabled modules at startup.
AGGREGATIONS = {}


def db_init(path, series, statements):
    """Create the database: `series` maps time-series table -> REAL columns (ts is the primary key;
    missing columns are added, so new versions extend existing databases); `statements` are the
    modules' other CREATE TABLE / CREATE INDEX statements."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = db_connect(path)
    for stmt in statements:
        conn.execute(stmt)
    for table, cols in series.items():
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (ts REAL PRIMARY KEY)")
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for c in cols:
            if c not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {c} REAL")
    conn.commit()
    conn.close()


def db_query(conn, table, cols, since, bucket):
    aggs = [f"{AGGREGATIONS.get(c, 'AVG')}({c})" for c in cols]
    sql = (f"SELECT CAST(ts / ? AS INTEGER) * ? AS t, {','.join(aggs)} FROM {table} "
           f"WHERE ts >= ? GROUP BY t ORDER BY t")
    rows = conn.execute(sql, (bucket, bucket, since)).fetchall()
    out = {"t": [r[0] for r in rows]}
    for i, c in enumerate(cols):
        # Non-numeric values (e.g. text stored from a misbehaving RPC) become gaps instead of errors.
        out[c] = [round(r[i + 1], 4) if isinstance(r[i + 1], (int, float)) else None for r in rows]
    return out


def prune(conn, statements, cutoff):
    """Run the modules' retention statements (each takes the cutoff timestamp as its one parameter)."""
    for stmt in statements:
        conn.execute(stmt, (cutoff,))
    conn.commit()
    DATA_GEN.bump()
