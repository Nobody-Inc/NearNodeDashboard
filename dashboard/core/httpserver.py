"""HTTP server plumbing: connection limits, IPv4/IPv6 listening, Host-header allowlist helpers."""

import ipaddress
import socket
import threading
from http.server import ThreadingHTTPServer

import psutil

from dashboard.core.util import valid_ip


def host_of(value):
    """Host part of a Host header / netloc: 'localhost:8787' -> 'localhost', '[::1]:8787' -> '::1'."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:].partition("]")[0]
    return value.rpartition(":")[0] if value.count(":") == 1 else value

def allowed_hosts(bind_host, extra=()):
    hosts = {"localhost", "127.0.0.1", "::1", host_of(bind_host)}
    for name in (socket.gethostname(), socket.getfqdn()):
        hosts.update({name.lower(), name.lower().split(".")[0]})
    hosts.update(h.lower() for h in extra)
    hosts.discard("")
    return hosts

REQUEST_TIMEOUT_SEC = 30

MAX_CONNECTIONS = 64            # simultaneous connections, all clients

MAX_CONNECTIONS_PER_CLIENT = 16

class BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that caps simultaneous connections (overall and per client IP) and
    request read time, so one client can't exhaust threads with many slow connections."""
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self._conn_lock = threading.Lock()
        self._active = 0
        self._per_client = {}
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        ip = client_address[0]
        with self._conn_lock:
            if self._active >= MAX_CONNECTIONS or self._per_client.get(ip, 0) >= MAX_CONNECTIONS_PER_CLIENT:
                refused = True
            else:
                refused = False
                self._active += 1
                self._per_client[ip] = self._per_client.get(ip, 0) + 1
        if refused:
            self.shutdown_request(request)  # over the limit: close right away
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(ip)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def _release(self, ip):
        with self._conn_lock:
            self._active -= 1
            n = self._per_client.get(ip, 1) - 1
            if n > 0:
                self._per_client[ip] = n
            else:
                self._per_client.pop(ip, None)

class DualStackServer(BoundedServer):
    """Listens on IPv6 *and* IPv4 when bound to '::'."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()

def make_server(host, port, handler):
    if host == "::" or (valid_ip(host) and ":" in host):
        try:
            return DualStackServer((host, port), handler)
        except OSError as e:
            if host != "::":
                raise
            print(f"[http] IPv6 unavailable ({e}); listening on all IPv4 addresses instead", flush=True)
            host = "0.0.0.0"
    return BoundedServer((host, port), handler)

def listen_urls(host, port):
    """URLs the dashboard can be opened at, for the startup message."""
    if host not in ("0.0.0.0", "::"):
        return [f"http://{'[' + host + ']' if ':' in host else host}:{port}"]
    urls = [f"http://127.0.0.1:{port}"]
    try:
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                ip = valid_ip(a.address.split("%")[0])
                if not ip or ipaddress.ip_address(ip).is_loopback or ipaddress.ip_address(ip).is_link_local:
                    continue
                if ":" in ip and host != "::":
                    continue
                urls.append(f"http://{'[' + ip + ']' if ':' in ip else ip}:{port}")
    except Exception:
        pass
    return urls
