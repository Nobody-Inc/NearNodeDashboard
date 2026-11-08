"""Network path tests: is the default gateway reachable, and do the configured DNS servers
answer? Gateways are pinged; each DNS server gets a real DNS query sent straight to it (not
through the system resolver, which would hide a dead server behind a working one). When a test
fails, mtr traces the path to that target so you can see where packets stop."""

import ipaddress
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

LINUX = sys.platform.startswith("linux")
MAX_DNS_SERVERS = 4
PING_COUNT = 3
MTR_CYCLES = 10
MTR_MIN_INTERVAL = 600     # seconds between traces to the same target
TRACE_KEEP = 6 * 3600      # how long a trace stays visible after it ran


def _ip(value):
    """A normalised IP address string (IPv6 may carry a %zone), or None if `value` isn't one.
    Only validated addresses are ever passed to ping or mtr."""
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _run(cmd, timeout):
    """(returncode, stdout+stderr) or None if the tool isn't installed."""
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as e:
        return 1, str(e)


# --------------------------------------------------------------------------- discovery

def default_gateways(root="/"):
    """Default gateway addresses, e.g. [("192.168.1.1", "eth0")], IPv4 first."""
    out = []
    if LINUX:
        best = {}
        for line in _read(os.path.join(root, "proc/net/route")).splitlines()[1:]:
            f = line.split()
            # Iface Destination Gateway Flags RefCnt Use Metric Mask ...; RTF_GATEWAY = 0x2
            if len(f) >= 8 and f[1] == "00000000" and f[7] == "00000000" and int(f[3], 16) & 0x2:
                gw = socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
                metric = int(f[6])
                if gw not in best or metric < best[gw][1]:
                    best[gw] = (f[0], metric)
        out += [(gw, iface) for gw, (iface, _) in sorted(best.items(), key=lambda kv: kv[1][1])]
        for line in _read(os.path.join(root, "proc/net/ipv6_route")).splitlines():
            f = line.split()
            # dest dest_plen src src_plen nexthop metric refcnt use flags iface
            if len(f) >= 10 and f[0] == "0" * 32 and f[1] == "00" and f[4] != "0" * 32:
                addr = ipaddress.IPv6Address(bytes.fromhex(f[4]))
                gw = f"{addr}%{f[9]}" if addr.is_link_local else str(addr)
                if gw not in [g for g, _ in out]:
                    out.append((gw, f[9]))
    elif sys.platform == "darwin":
        for args in (["route", "-n", "get", "default"], ["route", "-n", "get", "-inet6", "default"]):
            r = _run(args, 5)
            if r and r[0] == 0:
                gw = re.search(r"gateway:\s*(\S+)", r[1])
                iface = re.search(r"interface:\s*(\S+)", r[1])
                if gw:
                    out.append((gw.group(1), iface.group(1) if iface else ""))
    return [(ip, iface) for ip, iface in ((_ip(g), i) for g, i in out) if ip]


def dns_servers(root="/"):
    """The DNS servers this machine uses. With systemd-resolved, /etc/resolv.conf only lists the
    local stub (127.0.0.53), so the upstream servers are read too."""
    found = []
    for path in ("etc/resolv.conf", "run/systemd/resolve/resolv.conf"):
        for line in _read(os.path.join(root, path)).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                ip = _ip(parts[1])
                if ip and ip not in found:
                    found.append(ip)
    return found[:MAX_DNS_SERVERS + 1]


# --------------------------------------------------------------------------- tests

def ping(ip, count=PING_COUNT):
    """{"rtt_ms", "loss_pct", "error"} from the system ping command."""
    v6 = ":" in ip
    if sys.platform == "darwin":
        cmd = ["ping6", "-n", "-c", str(count), ip] if v6 else ["ping", "-n", "-c", str(count), "-W", "1000", ip]
    else:
        cmd = ["ping", "-6" if v6 else "-4", "-n", "-c", str(count), "-W", "1", ip]
    r = _run(cmd, timeout=count * 2 + 5)
    if r is None:
        return {"rtt_ms": None, "loss_pct": None, "error": "ping is not installed"}
    out = r[1]
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= [\d.]+/([\d.]+)/", out)   # min/avg/max[/mdev]
    if not loss:
        return {"rtt_ms": None, "loss_pct": None, "error": (out.strip().splitlines() or ["ping failed"])[-1][:200]}
    return {"rtt_ms": float(rtt.group(1)) if rtt else None, "loss_pct": float(loss.group(1)), "error": None}


RCODES = {1: "FORMERR", 2: "SERVFAIL", 4: "NOTIMP", 5: "REFUSED"}


def dns_query(server, name, timeout=2.0):
    """Send one DNS query for `name` (A record) directly to `server`. NXDOMAIN still counts as
    the server answering. Returns (ms or None, ok, detail)."""
    qid = random.randrange(65536)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.strip(".").split(".")) + b"\0"
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    host = server.split("%")[0]
    try:
        addr = socket.getaddrinfo(server, 53, family, socket.SOCK_DGRAM)[0][4]
    except OSError:
        addr = (host, 53)
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.perf_counter()
        try:
            s.sendto(query, addr)
            deadline = t0 + timeout
            while True:
                s.settimeout(max(0.01, deadline - time.perf_counter()))
                data, src = s.recvfrom(4096)
                if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid and src[0].split("%")[0] == host:
                    break
        except socket.timeout:
            return None, False, f"no answer within {timeout:.0f}s"
        except OSError as e:
            return None, False, str(e)
    ms = (time.perf_counter() - t0) * 1000
    rcode = struct.unpack(">H", data[2:4])[0] & 0xF
    if rcode in (0, 3):
        return ms, True, f"answered in {ms:.0f} ms" + (" (NXDOMAIN)" if rcode == 3 else "")
    return ms, False, f"answered with {RCODES.get(rcode, f'error code {rcode}')}"


def mtr(ip):
    """A text mtr report to `ip`, or a note on why it couldn't run."""
    if not shutil.which("mtr"):
        return False, "mtr is not installed (Debian/Ubuntu: sudo apt install mtr-tiny)."
    cmd = ["mtr", "--report", "--report-wide", "--no-dns", "--report-cycles", str(MTR_CYCLES)]
    cmd += ["-6" if ":" in ip else "-4", ip]
    r = _run(cmd, timeout=MTR_CYCLES * 3 + 30)
    out = (r[1] if r else "").strip()
    if r and r[0] == 0 and out:
        return True, out[:20000]
    return False, (out or "mtr failed")[:2000]


# --------------------------------------------------------------------------- the check

class NetworkPath:
    """Runs the gateway and DNS server tests and keeps the latest mtr trace per target."""

    def __init__(self):
        self.traces = {}         # target ip -> trace dict
        self.last_mtr = {}       # target ip -> time of the last mtr run

    def run(self, dns_name, run_mtr=True):
        gateways, servers = default_gateways(), dns_servers()
        targets = [{"kind": "gateway", "ip": ip, "iface": iface} for ip, iface in gateways]
        targets += [{"kind": "dns", "ip": ip} for ip in servers]
        if not targets:
            return {"status": "na", "targets": [], "traces": [],
                    "detail": "No default gateway or DNS servers found"}, {}

        def test(t):
            if t["kind"] == "gateway":
                p = ping(t["ip"])
                t.update(rtt_ms=p["rtt_ms"], loss_pct=p["loss_pct"])
                if p["error"]:
                    t.update(ok=None, status="na", detail=p["error"])
                else:
                    t["ok"] = p["loss_pct"] < 100
                    t["status"] = "fail" if p["loss_pct"] >= 100 else "warn" if p["loss_pct"] >= 20 else "pass"
                    t["detail"] = ("no replies" if p["loss_pct"] >= 100 else
                                   f"{p['rtt_ms']:.1f} ms average, {p['loss_pct']:.0f}% loss over {PING_COUNT} pings")
            else:
                ms, ok, detail = dns_query(t["ip"], dns_name)
                local = ipaddress.ip_address(t["ip"].split("%")[0]).is_loopback
                t.update(rtt_ms=ms, ok=ok, status="pass" if ok else "fail",
                         detail=("local resolver: " if local else "") + f"{dns_name} {detail}")
            return t

        with ThreadPoolExecutor(max_workers=min(8, len(targets))) as ex:
            targets = list(ex.map(test, targets))

        # Trace the path to each failing target (not loopback; at most every MTR_MIN_INTERVAL).
        now = time.time()
        failing = [t for t in targets if t["status"] in ("fail", "warn") and t["ok"] is not None
                   and not ipaddress.ip_address(t["ip"].split("%")[0]).is_loopback
                   and now - self.last_mtr.get(t["ip"], 0) >= MTR_MIN_INTERVAL]
        if run_mtr and failing:
            for t in failing:
                self.last_mtr[t["ip"]] = now
            with ThreadPoolExecutor(max_workers=min(4, len(failing))) as ex:
                for t, (ok, out) in zip(failing, ex.map(lambda t: mtr(t["ip"]), failing)):
                    self.traces[t["ip"]] = {"ip": t["ip"], "kind": t["kind"], "at": now, "ran": ok,
                                            "reason": t["detail"], "output": out}
        self.traces = {k: v for k, v in self.traces.items() if now - v["at"] < TRACE_KEEP}

        gw = [t for t in targets if t["kind"] == "gateway" and t["loss_pct"] is not None]
        dns = [t for t in targets if t["kind"] == "dns"]
        dns_failed = sum(1 for t in dns if not t["ok"])
        if any(t["status"] == "fail" for t in gw) or (dns and dns_failed == len(dns)):
            status = "fail"
        elif any(t["status"] in ("fail", "warn") for t in targets):
            status = "warn"
        else:
            status = "pass"
        parts = []
        if gw:
            parts.append(f"gateway {gw[0]['ip']}: " + ("unreachable" if gw[0]["loss_pct"] >= 100 else
                                                      f"{gw[0]['rtt_ms']:.1f} ms, {gw[0]['loss_pct']:.0f}% loss"))
        if dns:
            parts.append(f"{len(dns) - dns_failed}/{len(dns)} DNS servers answering")
        gauges = {
            "gateway_rtt_ms": gw[0]["rtt_ms"] if gw else None,
            "gateway_loss_pct": gw[0]["loss_pct"] if gw else None,
            "dns_server_ms": max((t["rtt_ms"] for t in dns if t["ok"] and t["rtt_ms"] is not None), default=None),
            "dns_server_fail_pct": dns_failed / len(dns) * 100 if dns else None,
        }
        return {"status": status, "targets": targets, "detail": "; ".join(parts),
                "traces": sorted(self.traces.values(), key=lambda x: -x["at"])}, gauges
