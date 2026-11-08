"""Checks run once when the service starts: the kernel network settings recommended for
validators (sysctl), and inbound access to neard's P2P port (local firewall rules, inbound peer
connections, or a TCP connection to the node when the dashboard runs elsewhere)."""

import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

import psutil

from dashboard.core.util import split_addr, valid_ip, valid_port


MAX_EXPECTED_PATH_BDP = 8388608

# (sysctl key, recommended value(s), indexes where a *larger* value is also fine)
SYSCTL_RECOMMENDED = [
    ("net.core.rmem_max", [MAX_EXPECTED_PATH_BDP], {0}),
    ("net.core.wmem_max", [MAX_EXPECTED_PATH_BDP], {0}),
    ("net.ipv4.tcp_rmem", [4096, 87380, MAX_EXPECTED_PATH_BDP], {2}),
    ("net.ipv4.tcp_wmem", [4096, 16384, MAX_EXPECTED_PATH_BDP], {2}),
    ("net.ipv4.tcp_slow_start_after_idle", [0], set()),
]

def _ints(text):
    try:
        return [int(x) for x in str(text).split()]
    except ValueError:
        return None

def _sysctl_ok(actual, expected, may_exceed):
    return (actual is not None and len(actual) == len(expected) and
            all(a >= e if i in may_exceed else a == e for i, (a, e) in enumerate(zip(actual, expected))))

# Directories systemd-sysctl reads at boot, highest priority first. A file name in an earlier
# directory masks the same name in later ones.
SYSCTL_DIRS = ("etc/sysctl.d", "run/sysctl.d", "usr/local/lib/sysctl.d", "usr/lib/sysctl.d", "lib/sysctl.d")

def _sysctl_boot_files(root="/"):
    """Config files in the order they're applied at boot, following systemd-sysctl: unique file
    names across SYSCTL_DIRS (earlier directory wins), applied in name order. /etc/sysctl.conf is
    usually the symlink /etc/sysctl.d/99-sysctl.conf, which therefore sorts *before* e.g. local.conf;
    if it isn't linked in, it's applied last (as `sysctl --system` does)."""
    chosen = {}
    for d in SYSCTL_DIRS:
        full = os.path.join(root, d)
        if os.path.isdir(full):
            for name in os.listdir(full):
                if name.endswith(".conf") and name not in chosen:
                    chosen[name] = os.path.join(full, name)
    files = [chosen[n] for n in sorted(chosen)]
    conf = os.path.join(root, "etc/sysctl.conf")
    if os.path.isfile(conf) and os.path.realpath(conf) not in {os.path.realpath(f) for f in files}:
        files.append(conf)
    return files

def _persisted_sysctls(root="/"):
    """key -> (value, file) for the definition that wins at boot (the last one applied)."""
    files = _sysctl_boot_files(root)
    found = {}
    for path in files:
        try:
            with open(path) as f:
                for line in f:
                    line = line.split("#", 1)[0].split(";", 1)[0].strip()
                    if "=" in line:
                        k, _, v = line.partition("=")
                        found[k.strip().lstrip("-").replace("/", ".")] = (" ".join(v.split()), path)
        except OSError:
            continue
    return found

def check_sysctls(proc="/proc/sys", root="/"):
    items = []
    if not os.path.isdir(os.path.join(proc, "net")):
        return [{"id": "sysctl", "group": "Network tuning", "title": "Kernel network settings", "status": "na",
                 "detail": f"Not applicable on {sys.platform}: these are Linux kernel settings. "
                           "Run the dashboard on the Linux host running neard to check them."}]
    persisted = _persisted_sysctls(root)
    for key, expected, may_exceed in SYSCTL_RECOMMENDED:
        want = " ".join(map(str, expected))
        try:
            with open(os.path.join(proc, *key.split("."))) as f:
                raw = " ".join(f.read().split())
        except OSError as e:
            items.append({"id": key, "group": "Network tuning", "title": key, "status": "warn",
                          "expected": want, "actual": None, "detail": f"Couldn't read the current value: {e}"})
            continue
        live_ok = _sysctl_ok(_ints(raw), expected, may_exceed)
        p_val, p_file = persisted.get(key, (None, None))
        saved_ok = _sysctl_ok(_ints(p_val) if p_val else None, expected, may_exceed)
        if live_ok and saved_ok:
            status, detail = "pass", f"Set, and saved in {p_file} so it survives a reboot."
        elif live_ok:
            status = "warn"
            detail = (f"Set now, but at boot {p_file} sets it to {p_val!r} (it's the last file applied "
                      f"that sets this key)."
                      if p_val else "Set now, but not saved in any sysctl.d file or /etc/sysctl.conf, "
                                    "so it will reset on reboot.")
        else:
            status = "fail"
            detail = (f"Current value doesn't match. {p_file} has {p_val!r}" if p_val else
                      "Current value doesn't match, and it isn't saved in a sysctl config file") + \
                     f". Apply with: sudo sysctl -w {key}=\"{want}\""
        if live_ok and any(a > e for a, e in zip(_ints(raw) or [], expected)):
            detail += " (higher than recommended, which is fine)"
        items.append({"id": key, "group": "Network tuning", "title": key, "status": status,
                      "expected": want, "actual": raw, "saved": p_val, "saved_in": p_file, "detail": detail})
    return items

def _run(cmd, timeout=5, use_sudo=False):
    """Run a command. Returns (returncode, output), or None if the tool isn't installed.

    Only if `use_sudo` is set (the firewall_check_sudo setting) does a permission error get retried
    with `sudo -n`. That never prompts, but for a user without sudo rights every attempt is still
    logged by PAM as a failed authentication ("auth could not identify password"), so it's off by
    default: the service user the installer creates has no sudo rights."""
    import shutil
    if not shutil.which(cmd[0]):
        return None
    attempts = [cmd]
    if use_sudo and os.geteuid() != 0 and shutil.which("sudo"):
        attempts.append(["sudo", "-n"] + cmd)
    for attempt in attempts:
        try:
            out = subprocess.run(attempt, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (out.stdout + out.stderr).strip()
        denied = out.returncode != 0 and re.search(r"permission|denied|root|privilege|not permitted|password", text, re.I)
        if not denied:
            return out.returncode, text
    return 1, ("needs root to read firewall rules (the check uses inbound peer connections instead; "
               "to read rules, give the service user passwordless sudo for this command and turn on "
               "firewall_check_sudo)")

def _ufw_rule_matches(to_field, port):
    """Does a ufw rule's "To" field (e.g. '24567/tcp', '24000:25000/tcp', '22,24567/tcp', '24567')
    cover `port` over TCP?"""
    spec, _, proto = to_field.partition("/")
    if proto and proto not in ("tcp",):
        return False
    for part in spec.split(","):
        lo, _, hi = part.partition(":")
        try:
            if int(lo) <= port <= int(hi or lo):
                return True
        except ValueError:
            continue
    return False

def _ufw_verdict(text, port):
    """Parse `ufw status verbose`: explicit rules for the port first, then the default incoming policy."""
    for line in text.splitlines():
        cols = line.split()
        if len(cols) >= 2 and _ufw_rule_matches(cols[0], port) and "(out)" not in line.lower():
            action = cols[1].upper()
            if action in ("ALLOW", "LIMIT"):
                return "allowed", f"ufw rule: {' '.join(cols)}"
            if action in ("DENY", "REJECT"):
                return "blocked", f"ufw rule: {' '.join(cols)}"
    m = re.search(r"Default:\s*(\w+)\s*\(incoming\)", text)
    if m and m.group(1).lower() == "allow":
        return "allowed", "ufw default incoming policy is allow"
    return "blocked", (f"ufw is active, denies incoming by default, and has no ALLOW rule covering "
                       f"{port}/tcp. Fix: sudo ufw allow {port}/tcp")

def _firewall_rules(port, use_sudo=False):
    """Best-effort look at local firewall rules. Returns (verdict, tool, detail) where verdict is
    'allowed', 'blocked', 'open' (no active filtering), or 'unknown'."""
    p = str(port)
    findings = []

    r = _run(["ufw", "status", "verbose"], use_sudo=use_sudo)
    if r:
        code, text = r
        if code == 0 and "Status: inactive" in text:
            findings.append(("open", "ufw", "ufw is installed but inactive"))
        elif code == 0 and "Status: active" in text:
            verdict, detail = _ufw_verdict(text, port)
            return verdict, "ufw", detail
        else:
            findings.append(("unknown", "ufw", text.splitlines()[0] if text else "couldn't read ufw status"))

    r = _run(["firewall-cmd", "--state"], use_sudo=use_sudo)
    if r and r[0] == 0 and "running" in r[1]:
        ports = _run(["firewall-cmd", "--list-ports"], use_sudo=use_sudo)
        if ports and ports[0] == 0:
            return (("allowed", "firewalld", f"firewalld allows {p}/tcp") if f"{p}/tcp" in ports[1].split() else
                    ("blocked", "firewalld", f"firewalld is running and {p}/tcp isn't in its open ports. "
                                             f"Fix: sudo firewall-cmd --permanent --add-port={p}/tcp && sudo firewall-cmd --reload"))
        findings.append(("unknown", "firewalld", "firewalld is running but its ports couldn't be read"))

    r = _run(["nft", "list", "ruleset"], use_sudo=use_sudo)
    if r and r[0] == 0 and r[1]:
        text = r[1]
        if re.search(rf"dport\s+(\{{[^}}]*\b{p}\b[^}}]*\}}|{p}\b).*accept", text):
            return ("allowed", "nftables", f"nftables has an accept rule for port {p}")
        if re.search(r"hook\s+input[^;]*;\s*policy\s+drop", text):
            return ("blocked", "nftables", f"nftables input policy is drop with no accept rule for port {p}")
        findings.append(("open" if "hook input" not in text else "unknown", "nftables",
                         "no input filtering found" if "hook input" not in text else
                         f"input rules exist but none mention port {p}"))
    elif r:
        findings.append(("unknown", "nftables", r[1].splitlines()[0] if r[1] else "couldn't read nftables"))

    r = _run(["iptables", "-S", "INPUT"], use_sudo=use_sudo)
    if r and r[0] == 0:
        text = r[1]
        if re.search(rf"--dport {p}\b.*-j ACCEPT", text) or re.search(rf"--dports [\d,:]*\b{p}\b.*-j ACCEPT", text):
            return ("allowed", "iptables", f"iptables has an ACCEPT rule for port {p}")
        policy_drop = re.search(r"^-P INPUT (DROP|REJECT)", text, re.M)
        # Only a catch-all rule (no source, port or other match) drops everything; a rule that
        # bans one IP or closes another port doesn't affect the P2P port.
        catch_all = re.search(r"^-A INPUT -j (DROP|REJECT)\b", text, re.M)
        if policy_drop or catch_all:
            return ("blocked", "iptables", f"iptables drops/rejects input by default and has no ACCEPT rule "
                                           f"for port {p}. Fix: sudo iptables -I INPUT -p tcp --dport {p} -j ACCEPT")
        findings.append(("open", "iptables", "iptables INPUT policy is ACCEPT with no catch-all drop rule"))
    elif r:
        findings.append(("unknown", "iptables", r[1].splitlines()[0] if r[1] else "couldn't read iptables"))

    if not findings:
        return ("unknown", None, "no supported firewall tool found (ufw, firewalld, nftables, iptables)")
    if all(f[0] == "open" for f in findings):
        return ("open", ", ".join(f[1] for f in findings), "; ".join(f[2] for f in findings))
    return ("unknown", ", ".join(f[1] for f in findings), "; ".join(f[2] for f in findings))

def _remote_node_host(rpc_url):
    """The node's host if rpc_url points at another machine, else None."""
    host = (urlparse(rpc_url).hostname or "").lower()
    if not host or host in ("localhost",) or host in {socket.gethostname().lower(), socket.getfqdn().lower()}:
        return None
    ip = valid_ip(host)
    if ip and (ipaddress.ip_address(ip).is_loopback or ipaddress.ip_address(ip).is_unspecified):
        return None
    try:  # one of this machine's own addresses?
        if ip and any(a.address == ip for addrs in psutil.net_if_addrs().values() for a in addrs):
            return None
    except Exception:
        pass
    return host

def check_p2p_reachable(host, port):
    """Connect to the node's P2P port from this machine: a genuine inbound test through every
    firewall in the path when the dashboard runs elsewhere."""
    title = f"P2P port {port}/tcp reachable from this host"
    item = {"id": "p2p_reachable", "group": "Firewall", "title": title, "expected": "connects"}
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=5) as s:
            peer_ip = s.getpeername()[0]
        ms = (time.perf_counter() - t0) * 1000
        if not ipaddress.ip_address(peer_ip.split("%")[0]).is_global:
            # Same LAN/VPC: a cloud firewall that only admits internal traffic would also pass this.
            item.update(status="warn", actual=f"connected in {ms:.0f} ms (private network)",
                        detail=f"Connected to {host}:{port} ({peer_ip}) from {socket.gethostname()}, but over a "
                               f"private network address, so this doesn't prove the port is reachable from the "
                               f"internet. Check the cloud/provider firewall allows {port}/tcp from anywhere.")
        else:
            item.update(status="pass", actual=f"connected in {ms:.0f} ms",
                        detail=f"Opened a TCP connection to {host}:{port} from {socket.gethostname()} over the "
                               f"internet, so the node accepts inbound P2P connections from outside.")
    except ConnectionRefusedError:
        item.update(status="fail", actual="refused",
                    detail=f"{host}:{port} refused the connection: nothing is listening on that port, or a "
                           f"firewall is rejecting it. Check neard's network.addr and the firewall.")
    except socket.timeout:
        item.update(status="fail", actual="timed out",
                    detail=f"No answer from {host}:{port} within 5 s: a firewall (host or cloud) is most "
                           f"likely dropping inbound traffic to {port}/tcp.")
    except OSError as e:
        item.update(status="warn", actual="error", detail=f"Couldn't test {host}:{port}: {e}")
    return item

def neard_running_locally(port, process_name):
    """True/False if we can tell whether neard runs on this machine, None if we can't."""
    try:
        conns = psutil.net_connections(kind="tcp")
        if any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port for c in conns):
            return True
    except (psutil.AccessDenied, PermissionError):
        pass  # macOS without root: fall back to the process list
    except Exception:
        pass
    try:
        if process_name and any(p.info.get("name") == process_name for p in psutil.process_iter(["name"])):
            return True
    except Exception:
        return None
    return False

def check_p2p_firewall(port, peer_listen_addrs=(), use_sudo=False):
    """`peer_listen_addrs`: (ip, port) pairs peers advertise as their listening address
    (from /network_info), used to tell our outbound connections apart from inbound ones."""
    title = f"Inbound P2P port {port}/tcp"
    item = {"id": "firewall", "group": "Firewall", "title": title, "expected": "inbound allowed"}
    listening, inbound, sock_err = None, [], None
    outbound_targets = set(peer_listen_addrs)
    try:
        conns = psutil.net_connections(kind="tcp")
        listening = any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port for c in conns)

        def remote(c):
            ip = valid_ip(c.raddr.ip.split("%")[0]) if c.raddr else None  # strip IPv6 zone ids
            return ip if ip and not ipaddress.ip_address(ip).is_loopback else None

        def is_inbound(c):
            # Local port == P2P port normally means the peer connected to us. But if neard reuses
            # its listening port as the source port for outbound connections, those look the same;
            # an outbound connection goes *to* the peer's advertised listening address (and usually
            # its P2P port), so exclude those rather than mistake them for inbound traffic.
            ip = remote(c)
            if not ip or c.status != psutil.CONN_ESTABLISHED or not c.laddr or c.laddr.port != port:
                return False
            return (ip, c.raddr.port) not in outbound_targets and c.raddr.port != port

        inbound = sorted({remote(c) for c in conns if is_inbound(c)})
    except (psutil.AccessDenied, PermissionError):
        sock_err = "listing sockets needs root on this OS"
    except Exception as e:
        sock_err = str(e)

    verdict, tool, rule_detail = _firewall_rules(port, use_sudo) if sys.platform.startswith("linux") else \
        ("unknown", None, f"firewall rule checks are Linux-only ({sys.platform})")
    item["listening"], item["inbound_peers"], item["firewall"] = listening, len(inbound), tool
    notes = []
    if listening is False:
        notes.append(f"Nothing is listening on port {port}: is neard running, and is p2p_port right?")
    if inbound:
        status = "pass"
        notes.insert(0, f"{len(inbound)} remote peer(s) are connected inbound to port {port}, so inbound "
                        f"traffic is reaching neard.")
        item["actual"] = f"{len(inbound)} inbound peers"
    elif verdict == "allowed":
        status, item["actual"] = "pass", "allowed by rule"
        notes.insert(0, rule_detail + ". No inbound peer connections were seen yet, so a cloud/provider "
                                      "firewall can't be ruled out.")
    elif verdict == "blocked":
        status, item["actual"] = "fail", "blocked"
        notes.insert(0, rule_detail)
    elif verdict == "open":
        status, item["actual"] = "warn", "no local filtering"
        notes.insert(0, f"{rule_detail}. But no inbound peer connections were seen, so check your "
                        f"cloud/provider firewall (security group) allows {port}/tcp.")
    else:
        status, item["actual"] = ("na" if not sys.platform.startswith("linux") else "warn"), "unconfirmed"
        notes.insert(0, f"Couldn't confirm: {rule_detail}" + (f"; {sock_err}" if sock_err else "") +
                     ", and no inbound peer connections were seen.")
    item["status"], item["detail"] = status, " ".join(notes)
    return item

def run_host_checks(config, near=None, wait=120, stop=None):
    """Runs once per service start. If neard should be on this machine but isn't up yet (e.g. the
    dashboard started first at boot), wait up to `wait` seconds for it before checking."""
    port, pname = int(config.get("p2p_port")), config.get("process_name")
    remote = _remote_node_host(config.get("rpc_url"))
    deadline = time.time() + wait
    local = neard_running_locally(port, pname)
    while not local and not remote and time.time() < deadline and not (stop and stop.is_set()):
        time.sleep(5)
        local = neard_running_locally(port, pname)
    if near is not None:  # give the NEAR collector a moment to fetch the peer list
        for _ in range(10):
            if near.active_peers:
                break
            time.sleep(1)
    started = time.time()
    try:
        peers = []
        for p in (near.active_peers if near is not None else []):
            host, pport = split_addr(p.get("addr"))
            ip, pp = valid_ip(host), valid_port(pport)
            if ip and pp:
                peers.append((ip, pp))
        items = check_sysctls() + [check_p2p_firewall(port, peers, bool(config.get("firewall_check_sudo")))]
        if local is False and remote:
            # neard isn't running here, so this machine's kernel/firewall settings don't affect it.
            # Keep the values for reference but don't count them; test the node over the network.
            for i in items:
                if i["status"] != "na":
                    i["status"] = "na"
                i["detail"] = (f"Not counted: neard isn't running on this machine ({socket.gethostname()}), so "
                               f"its settings don't affect the node at {remote}. Run the dashboard on the node "
                               f"to check these. " + ("Local result: " + i.get("detail", "") if i.get("detail") else ""))
            items.append(check_p2p_reachable(remote, port))
        elif local is None and remote:
            # Can't tell whether neard runs here: show both, and say so.
            items.append(check_p2p_reachable(remote, port))
            items[-1]["detail"] += (f" (Couldn't tell whether neard runs on this machine; {remote} comes from the "
                                    f"RPC URL, which may be a public RPC rather than your node.)")
    except Exception as e:  # a check must never take the server down
        items = [{"id": "error", "group": "Host checks", "title": "Host checks", "status": "warn",
                  "detail": f"Checks failed to run: {e}"}]
    return {"time": started, "hostname": socket.gethostname(), "platform": sys.platform, "items": items,
            "counts": {s: sum(1 for i in items if i["status"] == s) for s in ("pass", "warn", "fail", "na")}}
