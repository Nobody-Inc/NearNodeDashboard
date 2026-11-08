"""Generic collection and retention loops used by the modules."""

import time

from dashboard.core.storage import db_connect, db_insert, prune


def run_loop(app, name, collector, table, cols, stop, enabled=lambda: True):
    """Sample `collector` every collection interval (1 s in fast mode) and store the row in `table`.
    Wakes early when settings change, so a new interval applies immediately."""
    wake = app.config.subscribe()
    conn = db_connect(app.db_path)
    next_t = time.time()
    while not stop.is_set():
        try:
            if enabled():
                ts, row = collector.sample()
                if row:
                    db_insert(conn, table, cols, ts, row)
                    app.last_sample[name] = ts
        except Exception as e:  # never let the collector die
            print(f"[{name}] sample error: {e}", flush=True)
        try:
            interval = app.config.interval
        except Exception as e:
            print(f"[{name}] bad interval setting ({e}); using 60s", flush=True)
            interval = 60
        next_t += interval
        if next_t < time.time():  # fell behind (slow RPC) — don't burst to catch up
            next_t = time.time() + interval
        if wake.wait(max(0, next_t - time.time())):
            wake.clear()
            next_t = time.time()
    conn.close()


def retention_loop(app, stop):
    """Delete data older than retention_hours, every minute, using every module's statements."""
    conn = db_connect(app.db_path)
    while not stop.is_set():
        try:
            prune(conn, app.retention, time.time() - float(app.config.get("retention_hours")) * 3600)
        except Exception as e:
            print(f"[retention] cleanup error: {e}", flush=True)
        stop.wait(60)
    conn.close()
