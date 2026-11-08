"""Small helpers shared by the core and the modules."""

import ipaddress
import json
import math
import urllib.request


def to_bool(value):
    """Strict boolean parsing: the string "false" must not become True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "0", "no", "off", ""):
        return False
    raise ValueError(f"expected true/false, got {value!r}")

def num(v):
    """Coerce an RPC value to a float; anything non-numeric (incl. bool, str) becomes None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None

def as_int(v):
    """Integer-valued RPC field (heights, protocol numbers, shard ids), else None."""
    f = num(v)
    return int(f) if f is not None and f.is_integer() else None

def ratio(produced, expected):
    """produced / expected as a %, or None when either isn't a usable number."""
    p, e = num(produced), num(expected)
    return p / e * 100 if p is not None and e else None

def split_addr(addr):
    """'<ipv4>:24567' -> ('<ipv4>', '24567'); '[::1]:24567' -> ('::1', '24567');
    a bare IPv6 address without brackets has no port."""
    addr = (addr or "").strip() if isinstance(addr, str) else ""
    if addr.startswith("["):
        host, _, rest = addr[1:].partition("]")
        return host, rest[1:] if rest.startswith(":") else ""
    if addr.count(":") == 1:
        host, _, port = addr.partition(":")
        return host, port
    return addr, ""

def valid_ip(host):
    """Return the normalised IP string, or None if `host` isn't an IP literal.
    Guards subprocess arguments: a value like '-f' must never reach `ping`."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None

def valid_port(port):
    try:
        p = int(port)
    except (TypeError, ValueError):
        return None
    return p if 0 < p < 65536 else None

MAX_RPC_RESPONSE_BYTES = 16 * 2**20

def http_json(url, payload=None, timeout=10):
    data = None
    headers = {"User-Agent": "near-node-dashboard/1.0"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Cap the size: a misbehaving or malicious endpoint must not be able to exhaust memory.
        # (The largest real response, mainnet `validators`, is well under 1 MB.)
        body = resp.read(MAX_RPC_RESPONSE_BYTES + 1)
        if len(body) > MAX_RPC_RESPONSE_BYTES:
            raise ValueError(f"response from {url} exceeds {MAX_RPC_RESPONSE_BYTES // 2**20} MB; ignored")
        return json.loads(body)
