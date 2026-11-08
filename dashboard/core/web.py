"""The HTTP API and static file server. Core routes: /api/config (GET/POST), /api/latest,
/api/metrics, /api/alerts. Modules add their own routes (Module.routes)."""

import json
import math
import os
import socket
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from dashboard.core.httpserver import REQUEST_TIMEOUT_SEC, host_of
from dashboard.core.storage import CACHE
from dashboard.core.util import valid_ip
from dashboard.core.version import BASE_DIR

STATIC_DIR = os.path.join(BASE_DIR, "static")
MAX_RANGE_SEC = 366 * 86400
MAX_POINTS = 2000


def qnum(q, name, default, lo, hi, cast=float):
    raw = q.get(name, [str(default)])[0]
    try:
        value = cast(float(raw))
    except (TypeError, ValueError):
        raise ValueError(f"query parameter {name!r} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"query parameter {name!r} must be a finite number")
    return min(max(value, lo), hi)


class Request:
    """What a module's route handler gets."""

    def __init__(self, app, q):
        self.app, self.q = app, q

    @property
    def rng(self):
        """The ?range= query parameter in seconds (validated when a route uses it)."""
        return qnum(self.q, "range", 3600, 60, MAX_RANGE_SEC)

    def param(self, name, default=""):
        return self.q.get(name, [default])[0]

    def number(self, name, default, lo, hi, cast=float):
        return qnum(self.q, name, default, lo, hi, cast)

    def with_conn(self, fn):
        with self.app.pool.conn() as conn:
            return fn(conn)

    def cached(self, key, compute):
        """JSON bytes from the response cache while no new data has been stored; on a miss, computed
        and serialised once for every client asking for the same view."""
        return CACHE.get_or_compute(key, lambda: json.dumps(compute()).encode())


def make_handler(app, hosts):
    module_routes = {}
    for m in app.modules.values():
        module_routes.update(m.routes())

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

        def _latest(self):
            body = {}
            for m in app.modules.values():
                body.update(m.latest())
            body.update(server_hostname=socket.gethostname(), app_version=app.version,
                        read_only=bool(app.config.get("read_only")), modules=list(app.modules),
                        alerts=app.alerts, sampled_at=max(app.last_sample.values(), default=None))
            return body

        def _metrics(self, req):
            points = req.number("points", 720, 10, MAX_POINTS, int)
            rng = req.rng

            def compute():
                interval = app.config.interval
                # bucket >= range/points, so every series has at most ~points entries however
                # large `range` is.
                bucket = max(interval, math.ceil(rng / points))
                since = time.time() - rng

                def query(conn):
                    body = {"now": time.time(), "range": rng, "bucket": bucket, "interval": interval}
                    for m in app.modules.values():
                        body.update(m.metrics(conn, since, bucket))
                    return body
                return req.with_conn(query)
            return req.cached(("metrics", rng, points), compute)

        def _get(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/api/config":
                return self._send(200, app.config.snapshot())
            if u.path == "/api/latest":
                return self._send(200, self._latest())
            if u.path == "/api/alerts":
                return self._send(200, {"time": time.time(), "alerts": app.alerts})
            if u.path == "/api/metrics":
                return self._send(200, self._metrics(Request(app, q)))
            if u.path in module_routes:
                return self._send(200, module_routes[u.path](Request(app, q)))
            if u.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
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
