"""Slower host health and security checks, each on its own schedule: DNS, clock/NTP, gateway and
DNS server reachability (netpath), failed logins and system log rates (journal), SMART, pending
security updates, IPMI power supplies and fans, and whether the last reboot was clean."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from dashboard.modules.system.netpath import NetworkPath


def run_cmd(cmd, timeout=20, use_sudo=False):
    """(returncode, stdout, stderr), or None if the tool isn't installed. sudo -n is only tried when
    explicitly enabled (it never prompts, but failures are logged as failed sudo logins)."""
    if not shutil.which(cmd[0]):
        return None
    attempts = [cmd] + ([["sudo", "-n"] + cmd] if use_sudo and os.geteuid() != 0 and shutil.which("sudo") else [])
    out = None
    for a in attempts:
        try:
            r = subprocess.run(a, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            out = (1, "", str(e))
            continue
        out = (r.returncode, r.stdout, r.stderr)
        if not re.search(r"permission denied|must be root|not permitted|access denied|insufficient privilege",
                         r.stdout + r.stderr, re.I):
            break
    return out

def journal_count(args, since, until, pattern=None, cap=500_000):
    """Count journal lines (optionally matching `pattern`) between two Unix times, streaming."""
    if not shutil.which("journalctl"):
        return None
    cmd = ["journalctl", "-q", "--no-pager", "-o", "cat", "--since", f"@{int(since)}", "--until", f"@{int(until)}"] + args
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             encoding="utf-8", errors="replace")
    except OSError:
        return None
    n, rx = 0, re.compile(pattern) if pattern else None
    try:
        for line in p.stdout:
            if rx is None or rx.search(line):
                n += 1
                if n >= cap:
                    break
    finally:
        p.kill()
        p.wait()
    return n

def resolve_time(host, timeout=5):
    """Time a DNS lookup (through the system resolver, so cached answers count as fast)."""
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(socket.getaddrinfo, host, None)
        try:
            fut.result(timeout=timeout)
            return (time.perf_counter() - t0) * 1000, True, None
        except Exception as e:
            return None, False, str(e) or "lookup timed out"

def parse_duration_ms(text):
    """'+1.234ms' / '-87us' / '2.1s' -> milliseconds."""
    m = re.match(r"\s*([+-]?[\d.]+)\s*(ns|us|µs|ms|s|min)?", text or "")
    if not m:
        return None
    v = float(m.group(1))
    return v * {"ns": 1e-6, "us": 1e-3, "µs": 1e-3, "ms": 1, "s": 1000, "min": 60000}.get(m.group(2) or "s", 1000)

def clock_status():
    synced, offset, source = None, None, None
    r = run_cmd(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=5)
    if r and r[0] == 0 and r[1].strip() in ("yes", "no"):
        synced = r[1].strip() == "yes"
    r = run_cmd(["chronyc", "-c", "tracking"], timeout=5)
    if r and r[0] == 0 and r[1].count(",") > 5:
        try:
            offset, source = float(r[1].split(",")[4]) * 1000, "chrony"
            if synced is None:
                synced = True
        except ValueError:
            pass
    if offset is None:
        r = run_cmd(["timedatectl", "timesync-status"], timeout=5)
        if r and r[0] == 0:
            m = re.search(r"Offset:\s*(\S+)", r[1])
            if m:
                offset, source = parse_duration_ms(m.group(1)), "systemd-timesyncd"
    if offset is None and synced is None:
        return {"status": "na", "detail": "No NTP status available (timedatectl/chrony not found)."}
    bad = synced is False or (offset is not None and abs(offset) > 100)
    detail = ("NTP synchronised" if synced else "NOT synchronised to NTP" if synced is False else "sync state unknown") + \
             (f", offset {offset:+.2f} ms ({source})" if offset is not None else "")
    return {"status": "warn" if bad else "pass", "synced": synced, "offset_ms": offset, "detail": detail}

def smart_status(use_sudo=False):
    if not shutil.which("smartctl"):
        return {"status": "na", "detail": "smartctl not installed (sudo apt install smartmontools).", "drives": []}
    scan = run_cmd(["smartctl", "--scan", "-j"], timeout=20, use_sudo=use_sudo)
    try:
        devices = json.loads(scan[1]).get("devices", []) if scan else []
    except ValueError:
        devices = []
    drives = []
    for dev in devices:
        name, dtype = dev.get("name"), dev.get("type")
        if not name or not re.match(r"^/dev/[\w/.-]+$", name):
            continue
        r = run_cmd(["smartctl", "-H", "-A", "-j"] + (["-d", dtype] if dtype and re.match(r"^[\w,+-]+$", dtype) else [])
                    + [name], timeout=30, use_sudo=use_sudo)
        try:
            j = json.loads(r[1]) if r else {}
        except ValueError:
            j = {}
        passed = (j.get("smart_status") or {}).get("passed")
        msg = " ".join((j.get("smartctl") or {}).get("messages", [{}])[0].get("string", "").split()) if (j.get("smartctl") or {}).get("messages") else ""
        drives.append({"device": name, "model": j.get("model_name"), "passed": passed,
                       "temperature": (j.get("temperature") or {}).get("current"),
                       "percentage_used": (j.get("nvme_smart_health_information_log") or {}).get("percentage_used"),
                       "detail": msg or ("passed" if passed else "FAILED" if passed is False else "no health data")})
    if not drives:
        return {"status": "na", "drives": [], "detail": "No drives readable. SMART needs root: run as root or enable "
                                                         "health_checks_sudo with passwordless sudo for smartctl."}
    failed = [d for d in drives if d["passed"] is False]
    unknown = [d for d in drives if d["passed"] is None]
    return {"status": "fail" if failed else "warn" if unknown and len(unknown) == len(drives) else "pass",
            "drives": drives, "detail": f"{len(drives) - len(failed) - len(unknown)} passed, {len(failed)} failed"
                                        + (f", {len(unknown)} unreadable (needs root)" if unknown else "")}

def patch_status():
    reboot = os.path.exists("/var/run/reboot-required")
    total = security = None
    tool = None
    if os.path.exists("/usr/lib/update-notifier/apt-check"):
        r = run_cmd(["/usr/lib/update-notifier/apt-check"], timeout=60)
        m = re.search(r"(\d+);(\d+)", (r[2] + r[1]) if r else "")
        if m:
            total, security, tool = int(m.group(1)), int(m.group(2)), "apt"
    if total is None and shutil.which("apt-get"):
        r = run_cmd(["apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=120)
        if r and r[0] == 0:
            inst = [l for l in r[1].splitlines() if l.startswith("Inst ")]
            total, tool = len(inst), "apt"
            security = sum(1 for l in inst if re.search(r"-security|Debian-Security", l))
    if total is None and shutil.which("dnf"):
        r = run_cmd(["dnf", "-q", "--cacheonly", "updateinfo", "list", "--security"], timeout=120)
        if r and r[0] == 0:
            security, tool = len([l for l in r[1].splitlines() if l.strip()]), "dnf"
            r2 = run_cmd(["dnf", "-q", "--cacheonly", "check-update"], timeout=120)
            total = len([l for l in (r2[1] if r2 else "").splitlines() if l.strip() and not l.startswith(" ")]) if r2 else None
    if total is None and security is None:
        return {"status": "na", "reboot_required": reboot, "detail": "No supported package manager found (apt, dnf)."}
    status = "warn" if security or reboot else "pass"
    detail = f"{security or 0} security update(s), {total if total is not None else '?'} total"
    if reboot:
        detail += "; a reboot is required"
    return {"status": status, "security": security, "total": total, "reboot_required": reboot, "tool": tool,
            "detail": detail + " (as of the last package list refresh)"}

def ipmi_status(use_sudo=False):
    """Power supply and fan status via ipmitool (servers with a BMC; usually needs root)."""
    if not shutil.which("ipmitool"):
        return {"status": "na", "detail": "ipmitool not installed (only servers with a BMC report PSU status).",
                "sensors": []}
    sensors = []
    for kind in ("Power Supply", "Fan"):
        r = run_cmd(["ipmitool", "sdr", "type", kind], timeout=30, use_sudo=use_sudo)
        if not r or r[0] != 0:
            continue
        for line in r[1].splitlines():
            cols = [c.strip() for c in line.split("|")]
            if len(cols) >= 5:
                sensors.append({"type": kind, "name": cols[0], "state": cols[2], "reading": cols[4]})
    if not sensors:
        return {"status": "na", "sensors": [], "detail": "No IPMI data (needs root, or enable health_checks_sudo)."}
    bad = [s for s in sensors if s["state"].lower() not in ("ok", "ns")]
    return {"status": "fail" if bad else "pass", "sensors": sensors,
            "detail": f"{len(sensors) - len(bad)} OK" + (f", {len(bad)} failing: " + ", ".join(s["name"] for s in bad) if bad else "")}

CLEAN_SHUTDOWN = re.compile(r"Reached target (System Power Off|System Reboot|Shutdown|System Shutdown|Power-Off|Reboot)"
                            r"|systemd-shutdown|Journal stopped|System is (powering down|rebooting)")

def last_boot_status():
    """Did the previous boot end with a clean shutdown? (Needs a persistent journal.)"""
    if not shutil.which("journalctl"):
        return {"status": "na", "detail": "journalctl not available."}
    r = run_cmd(["journalctl", "-b", "-1", "-n", "400", "-q", "--no-pager", "-o", "cat"], timeout=30)
    if not r or r[0] != 0 or not r[1].strip():
        return {"status": "na", "detail": "No journal from the previous boot (enable a persistent journal to detect "
                                          "unexpected reboots: sudo mkdir -p /var/log/journal)."}
    if CLEAN_SHUTDOWN.search(r[1]):
        return {"status": "pass", "unexpected": False, "detail": "The last reboot followed a clean shutdown."}
    return {"status": "warn", "unexpected": True,
            "detail": "The previous boot has no clean-shutdown record: the last reboot looks unexpected "
                      "(crash, power loss or hard reset)."}

# DMI vendor/product strings of common hypervisors and cloud VMs (lowercase substrings).
VM_DMI_MARKERS = ("kvm", "qemu", "vmware", "virtualbox", "xen", "bochs", "bhyve", "parallels",
                  "virtual machine", "google compute engine", "droplet", "openstack", "hvm domu")


def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def detect_virtualization(root="/"):
    """Whether this machine is a virtual machine (or container). Returns
    {"virtual": bool or None, "kind": "vm" | "container" | None, "type": str, "detail": str};
    "virtual" is None when it couldn't be determined (hardware checks then still run)."""
    def result(virtual, kind, type_, how):
        if virtual:
            detail = f"{'Container' if kind == 'container' else 'Virtual machine'} ({type_}), detected via {how}"
        elif virtual is False:
            detail = f"Physical server (detected via {how})"
        else:
            detail = "Couldn't tell whether this is a virtual machine"
        return {"virtual": virtual, "kind": kind, "type": type_, "detail": detail}

    if sys.platform == "darwin":
        r = run_cmd(["sysctl", "-n", "kern.hv_vmm_present"], timeout=5)
        if r and r[0] == 0 and r[1].strip() in ("0", "1"):
            vm = r[1].strip() == "1"
            return result(vm, "vm" if vm else None, "macOS VM" if vm else "none", "kern.hv_vmm_present")
        return result(None, None, "unknown", "")
    # systemd-detect-virt knows every common hypervisor and container runtime.
    if shutil.which("systemd-detect-virt"):
        r = run_cmd(["systemd-detect-virt"], timeout=5)
        what = r[1].strip() if r else ""
        if r and r[0] == 0 and what and what != "none":
            ct = run_cmd(["systemd-detect-virt", "--container", "--quiet"], timeout=5)
            return result(True, "container" if ct and ct[0] == 0 else "vm", what, "systemd-detect-virt")
        if what == "none":
            return result(False, None, "none", "systemd-detect-virt")
    # Fallbacks: the CPU's hypervisor flag (x86), then the DMI vendor/product names.
    for line in _read_text(os.path.join(root, "proc/cpuinfo")).splitlines():
        if line.startswith("flags") and " hypervisor" in f" {line.split(':', 1)[-1]} ":
            return result(True, "vm", "hypervisor", "the CPU hypervisor flag")
    dmi = " ".join(_read_text(os.path.join(root, "sys/class/dmi/id", f)) for f in ("sys_vendor", "product_name")).lower()
    hit = next((m for m in VM_DMI_MARKERS if m in dmi), None)
    if hit:
        return result(True, "vm", hit, "DMI product information")
    if dmi.strip():
        return result(False, None, "none", "DMI product information")
    return result(None, None, "unknown", "")


# Checks that read physical hardware; meaningless (or always failing) inside a VM or container.
HARDWARE_CHECKS = ("smart", "ipmi")


class HealthMonitor:
    """Runs the slow checks on their own schedules and keeps the latest results.
    Gauges (DNS, clock) are carried into every system sample; counts (failed logins, system log
    errors/warnings) are handed to the next sample once, so bucket sums are correct."""

    SCHEDULE = {"dns": 60, "network": 60, "clock": 300, "logs": 300, "ipmi": 300, "smart": 21600, "patches": 21600}

    def __init__(self, config, on_change=None, virtualization=None):
        self.config = config
        self.on_change = on_change or (lambda: None)
        self.results = {}
        # On a VM or container the hardware checks (SMART, IPMI) are skipped.
        self.virtualization = virtualization or {"virtual": None, "kind": None, "type": "unknown", "detail": ""}
        self.virtual = bool(self.virtualization.get("virtual"))
        self.lock = threading.Lock()
        self.last_run = {}
        self.pending = {}
        self.logs_since = time.time()
        self.gauges = {}
        self.netpath = NetworkPath()

    def _set(self, name, value):
        value["checked_at"] = time.time()
        with self.lock:
            self.results[name] = value
        self.on_change()

    def pop_counts(self):
        with self.lock:
            out, self.pending = self.pending, {}
        return out

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.results.items()}

    def run_check(self, name):
        sudo = bool(self.config.get("health_checks_sudo"))
        if name == "dns":
            host = self.config.get("dns_check_host") or "near.org"
            ms, ok, err = resolve_time(host)
            self.gauges.update(dns_ms=ms, dns_ok=1 if ok else 0)
            self._set("dns", {"status": "pass" if ok and ms < 500 else "warn", "host": host, "ms": ms,
                              "detail": f"{host} resolved in {ms:.0f} ms" if ok else f"{host} failed to resolve: {err}"})
        elif name == "network":
            result, gauges = self.netpath.run(self.config.get("dns_check_host") or "near.org",
                                              run_mtr=bool(self.config.get("mtr_on_failure")))
            self.gauges.update(gauges)
            self._set("network", result)
        elif name == "clock":
            res = clock_status()
            self.gauges.update(clock_offset_ms=res.get("offset_ms"),
                               ntp_synced=None if res.get("synced") is None else int(res["synced"]))
            self._set("clock", res)
        elif name == "logs":
            now = time.time()
            since, self.logs_since = self.logs_since, now
            failed = journal_count(["_COMM=sshd", "_COMM=sshd-session"], since, now,
                                   r"Failed password|Invalid user|authentication failure|Failed publickey")
            errs = journal_count(["-p", "err"], since, now)
            warns = journal_count(["-p", "warning..warning"], since, now)
            with self.lock:
                for k, v in (("failed_logins", failed), ("sys_log_errors", errs), ("sys_log_warnings", warns)):
                    if v is not None:
                        self.pending[k] = self.pending.get(k, 0) + v
            self._set("logs", {"status": "na" if failed is None and errs is None else "pass",
                               "failed_logins": failed, "errors": errs, "warnings": warns,
                               "detail": "journal not readable" if errs is None else
                               f"last {int((now - since) / 60) or 1} min: {failed or 0} failed logins, "
                               f"{errs} errors, {warns} warnings"})
        elif name == "smart":
            self._set("smart", smart_status(sudo))
        elif name == "patches":
            self._set("patches", patch_status())
        elif name == "ipmi":
            self._set("ipmi", ipmi_status(sudo))

    def run(self, stop):
        v = self.virtualization
        self._set("virtualization", {
            "status": "pass" if v.get("virtual") is False else "na",   # informational: VM, container or unknown
            "virtual": v.get("virtual"), "kind": v.get("kind"), "type": v.get("type"),
            "detail": v.get("detail", "") + (". Hardware checks (SMART, temperatures, fans, IPMI) are skipped: "
                                               "the host's hardware isn't visible from here." if v.get("virtual") else "")})
        try:
            self._set("last_boot", last_boot_status())
        except Exception as e:
            self._set("last_boot", {"status": "na", "detail": f"check failed: {e}"})
        while not stop.is_set():
            if self.config.get("collect_health"):
                now = time.time()
                for name, every in self.SCHEDULE.items():
                    if self.virtual and name in HARDWARE_CHECKS:
                        continue
                    if now - self.last_run.get(name, 0) >= every:
                        self.last_run[name] = now
                        try:
                            self.run_check(name)
                        except Exception as e:
                            self._set(name, {"status": "na", "detail": f"check failed: {e}"})
            stop.wait(5)
