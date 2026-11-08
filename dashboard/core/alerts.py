"""Symptom-based alerts. The engine asks every module for its active alerts every 30 s and tracks
how long each has been active. Module rules should use sustained windows (ctx.window), not single
samples, so a brief spike doesn't trigger them."""

import time

from dashboard.core.storage import DATA_GEN, db_connect

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def alert(id_, severity, title, detail):
    return {"id": id_, "severity": severity, "title": title, "detail": detail}


class AlertContext:
    """What a module's alert rules get: the settings, a DB connection and window helpers."""

    def __init__(self, app, conn):
        self.app, self.conn = app, conn
        self.config = app.config.snapshot()

    def window(self, table, cols, seconds, fn="AVG"):
        row = self.conn.execute(f"SELECT {','.join(f'{fn}({c})' for c in cols)} FROM {table} WHERE ts >= ?",
                                (time.time() - seconds,)).fetchone()
        return {c: (v if isinstance(v, (int, float)) else None) for c, v in zip(cols, row or [None] * len(cols))}

    def latest(self, table, col):
        r = self.conn.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
        return r[0] if r and isinstance(r[0], (int, float)) else None


class AlertEngine:
    INTERVAL = 30

    def __init__(self, app):
        self.app = app
        self.since = {}

    def evaluate(self, conn):
        ctx, out = AlertContext(self.app, conn), []
        for m in self.app.modules.values():
            try:
                out += m.alerts(ctx)
            except Exception as e:
                print(f"[alerts] {m.name} rules failed: {e}", flush=True)
        out.sort(key=lambda a: (SEVERITY_ORDER.get(a["severity"], 9), a["title"]))
        return out

    def run(self, stop):
        conn = db_connect(self.app.db_path)
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
