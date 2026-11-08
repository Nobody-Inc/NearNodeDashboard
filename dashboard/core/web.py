"""The HTTP API and static file server. Core routes: /api/config (GET/POST), /api/latest,
/api/metrics, /api/alerts. Modules add their own routes (Module.routes)."""

import gzip
import json
import math
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from dashboard.core.httpserver import REQUEST_TIMEOUT_SEC, content_security_policy, host_of
from dashboard.core.storage import CACHE
from dashboard.core.util import valid_ip
from dashboard.core.version import BASE_DIR

STATIC_DIR = os.path.join(BASE_DIR, "static")
MAX_RANGE_SEC = 366 * 86400
MAX_POINTS = 2000


COMPRESS_MIN_BYTES = 1024
COMPRESSIBLE = ("application/json", "application/javascript", "text/")


class Body(bytes):
    """A response body that remembers its gzip form, so a cached response (or static file) is
    compressed once however many clients fetch it."""

    @property
    def gz(self):
        if "_gz" not in self.__dict__:
            self.__dict__["_gz"] = gzip.compress(self, compresslevel=6, mtime=0)
        return self.__dict__["_gz"]


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
        return CACHE.get_or_compute(key, lambda: Body(json.dumps(compute()).encode()))


def make_handler(app, hosts):
    module_routes = {}
    for m in app.modules.values():
        module_routes.update(m.routes())
    static_cache, static_lock = {}, threading.Lock()   # path -> (mtime_ns, size, etag, Body, CSP or None)

    class Handler(BaseHTTPRequestHandler):
        # Drop a connection that sends nothing for this long, so a slow or stalled client can't
        # hold a server thread forever.
        timeout = REQUEST_TIMEOUT_SEC
        server_version = "NearNodeDashboard"   # don't advertise the Python version
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", cache="no-store", etag=None, csp=None):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode()
            compressible = len(body) >= COMPRESS_MIN_BYTES and ctype.startswith(COMPRESSIBLE)
            encoding = None
            if compressible and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
                body, encoding = (body if isinstance(body, Body) else Body(body)).gz, "gzip"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
            if compressible:
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", cache)
            if etag:
                self.send_header("ETag", etag)
            if csp:
                self.send_header("Content-Security-Policy", csp)
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
            # Browsers may keep static files but must revalidate (no-cache): an unchanged file is a
            # bodiless 304, and an upgrade (new mtime/size, so a new ETag) shows up straight away.
            st = os.stat(full)
            with static_lock:
                hit = static_cache.get(full)
            if not hit or hit[:2] != (st.st_mtime_ns, st.st_size):
                with open(full, "rb") as f:
                    data = f.read()
                csp = content_security_policy(data) if ctype.startswith("text/html") else None
                hit = (st.st_mtime_ns, st.st_size, f'"{st.st_mtime_ns:x}-{st.st_size:x}"', Body(data), csp)
                with static_lock:
                    static_cache[full] = hit
            etag = hit[2]
            if etag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return None
            return self._send(200, hit[3], ctype, cache="no-cache", etag=etag, csp=hit[4])

    return Handler
