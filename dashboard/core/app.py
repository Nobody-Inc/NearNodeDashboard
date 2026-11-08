"""Assembles the core and the enabled modules, and runs the server."""

import argparse
import importlib
import os
import threading

from dashboard.core.alerts import AlertEngine
from dashboard.core.collect import retention_loop
from dashboard.core.config import Config, Setting, boolean, integer, nonempty_text, number
from dashboard.core.httpserver import allowed_hosts, listen_urls, make_server
from dashboard.core.storage import AGGREGATIONS, DBPool, db_init
from dashboard.core.version import BASE_DIR, version_info
from dashboard.core.web import make_handler

# Modules that can be enabled with the "modules" setting, in page order.
AVAILABLE_MODULES = ("system", "near")


def load_module_class(name):
    return importlib.import_module(f"dashboard.modules.{name}").MODULE


def module_list(v):
    names = [str(n).strip().lower() for n in (v if isinstance(v, list) else str(v).split(","))]
    names = [n for n in names if n]
    unknown = [n for n in names if n not in AVAILABLE_MODULES]
    if unknown:
        raise ValueError(f"unknown module(s) {unknown}; available: {list(AVAILABLE_MODULES)}")
    return list(dict.fromkeys(names))


CORE_SETTINGS = {
    # Normal sampling interval in seconds (default: every minute).
    "interval_seconds": Setting(60, integer(1, 86400)),
    # High-frequency mode: sample every second, overriding interval_seconds.
    "fast_mode": Setting(False, boolean),
    # How long to keep samples.
    "retention_hours": Setting(24, number(0.05, 24 * 365)),
    # Lock the settings: when true, nothing can be changed from the web page. Can only be set in
    # this file (takes effect on restart); an invalid value fails closed (locked).
    "read_only": Setting(False, boolean, page="reject", invalid=True),
    # Which modules run (file only; takes effect on restart). E.g. ["system"] for a plain server monitor.
    "modules": Setting(list(AVAILABLE_MODULES), module_list, page="ignore"),
    "db_path": Setting("data/stats.db", nonempty_text, page="ignore"),
}


class App:
    """Holds the shared pieces (settings, storage, alerts) and the enabled modules."""

    def __init__(self, config_path):
        classes = {n: load_module_class(n) for n in AVAILABLE_MODULES}
        registry = dict(CORE_SETTINGS)
        for cls in classes.values():   # every module's settings are known, enabled or not
            registry.update(cls.settings)
        self.config = Config(config_path, registry)
        self.version = version_info()
        self.db_path = os.path.join(BASE_DIR, self.config.get("db_path"))
        self.pool = DBPool(self.db_path)
        self.alerts = []
        self.last_sample = {}
        self.modules = {n: classes[n](self) for n in self.config.get("modules")}
        self.series, self.schema, self.retention = {}, [], []
        for m in self.modules.values():
            self.series.update(m.series)
            self.schema += m.schema
            self.retention += m.retention
            AGGREGATIONS.update(m.aggregations)

    def start(self, stop):
        db_init(self.db_path, self.series, self.schema)
        for m in self.modules.values():
            m.start(stop)
        threading.Thread(target=retention_loop, args=(self, stop), daemon=True).start()
        threading.Thread(target=AlertEngine(self).run, args=(stop,), daemon=True).start()


def main(doc=None):
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address (default 0.0.0.0 = all IPv4 addresses; '::' = all IPv4 and IPv6; "
                         "127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra hostname the dashboard may be reached by (e.g. a DNS name or SSH alias); "
                         "localhost, this machine's hostname and IP addresses are always allowed")
    args = ap.parse_args()

    app = App(args.config)
    stop = threading.Event()
    app.start(stop)

    server = make_server(args.host, args.port, make_handler(app, allowed_hosts(args.host, args.allow_host)))
    v, config = app.version, app.config
    details = ", ".join(x for x in [f"interval {config.interval}s", f"retention {config.get('retention_hours')}h",
                                     f"modules {', '.join(app.modules) or 'none'}"]
                        + [m.describe() for m in app.modules.values()] if x)
    print(f"Near Node Dashboard {v['version']}"
          + (f"+{v['distance']}" if v.get("distance") else "")
          + (f" ({v['commit']}, {v['date']})" if v.get("commit") else "")
          + f" listening on {args.host}:{args.port}  ({details})", flush=True)
    for u in listen_urls(args.host, args.port):
        print(f"  {u}", flush=True)
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
