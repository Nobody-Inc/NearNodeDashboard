"""Extended host metrics and slow-changing health/security checks for the Near Node Dashboard.

Organised around the USE method (Utilization, Saturation, Errors) for each resource:

  CPU      U: user/system/iowait/steal %     S: load per core, run queue     E: (none on Linux)
  Memory   U: used vs available              S: swap use, swap-in/out rate   E: OOM kills
  Disk     U: space/inodes per fs, busy %    S: await latency, queue depth   E: SMART health
  Network  U: bandwidth vs link capacity     S: TCP retransmits, conn states E: packet errors/drops

`ExtendedStats.sample()` runs with every system sample (cheap: psutil + /proc reads).
`HealthMonitor` runs slower checks on their own schedules (DNS, clock, logins, log rates, SMART,
patches, TLS, IPMI, last boot) so 1s mode doesn't hammer them.

Everything is best-effort: a value that isn't available on this OS, or needs root, is None.
"""

import json
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psutil

LINUX = sys.platform.startswith("linux")
PSEUDO_FS = {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2", "devpts",
             "mqueue", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "autofs", "fusectl", "configfs",
             "efivarfs", "nsfs", "ramfs", "binfmt_misc", "hugetlbfs", "rpc_pipefs", "fuse.lxcfs", "devfs"}
SKIP_DISKS = ("loop", "ram", "zram", "dm-", "md", "sr", "fd", "nbd")
TCP_STATE_NAMES = {"01": "established", "02": "syn_sent", "03": "syn_recv", "04": "fin_wait1", "05": "fin_wait2",
                   "06": "time_wait", "07": "close", "08": "close_wait", "09": "last_ack", "0A": "listen", "0B": "closing"}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------- /proc readers (Linux)

def proc_stat(root="/proc"):
    """Context switches, interrupts and run-queue counts from /proc/stat."""
    text = _read(os.path.join(root, "stat"))
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("ctxt", "intr", "procs_running", "procs_blocked"):
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def vmstat(root="/proc"):
    out = {}
    for line in (_read(os.path.join(root, "vmstat")) or "").splitlines():
        k, _, v = line.partition(" ")
        if v.strip().isdigit():
            out[k] = int(v)
    return out


def physical_disks(sys_root="/sys"):
    try:
        return {d for d in os.listdir(os.path.join(sys_root, "block")) if not d.startswith(SKIP_DISKS)}
    except OSError:
        return set()


def diskstats(root="/proc", sys_root="/sys"):
    """Per physical disk counters from /proc/diskstats (partitions and virtual devices excluded,
    so nothing is counted twice)."""
    disks = physical_disks(sys_root)
    out = {}
    for line in (_read(os.path.join(root, "diskstats")) or "").splitlines():
        f = line.split()
        if len(f) < 14 or f[2] not in disks:
            continue
        try:
            out[f[2]] = {"reads": int(f[3]), "read_sectors": int(f[5]), "read_ms": int(f[6]),
                         "writes": int(f[7]), "write_sectors": int(f[9]), "write_ms": int(f[10]),
                         "io_ms": int(f[12]), "weighted_ms": int(f[13])}
        except ValueError:
            continue
    return out


def net_snmp(root="/proc"):
    """TCP counters (RetransSegs, OutSegs, InErrs...) from /proc/net/snmp."""
    lines = (_read(os.path.join(root, "net", "snmp")) or "").splitlines()
    out = {}
    for head, vals in zip(lines, lines[1:]):
        if head.startswith("Tcp:") and vals.startswith("Tcp:") and head != vals:
            for k, v in zip(head.split()[1:], vals.split()[1:]):
                try:
                    out[k] = int(v)
                except ValueError:
                    pass
            break
    return out


def tcp_states(root="/proc"):
    """Count TCP sockets by state from /proc/net/tcp and tcp6 (no root needed)."""
    counts = {}
    for name in ("tcp", "tcp6"):
        for line in (_read(os.path.join(root, "net", name)) or "").splitlines()[1:]:
            f = line.split()
            if len(f) > 3:
                st = TCP_STATE_NAMES.get(f[3].upper(), "other")
                counts[st] = counts.get(st, 0) + 1
    return counts


def file_nr(root="/proc"):
    """(allocated, unused, max) file handles from /proc/sys/fs/file-nr."""
    parts = (_read(os.path.join(root, "sys", "fs", "file-nr")) or "").split()
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return None


# --------------------------------------------------------------------------- per-sample stats

class ExtendedStats:
    """Cheap per-sample USE metrics. Keeps the previous counters to turn them into rates."""

    def __init__(self, proc_root="/proc", sys_root="/sys"):
        self.proc, self.sys = proc_root, sys_root
        self.prev = None          # (time, counters)
        self.proc_cache = (0.0, None, None)
        self.details = {}
        psutil.cpu_times_percent(interval=None)  # prime: next call measures since now

    def _counters(self):
        c = {"swap": psutil.swap_memory(), "nics": psutil.net_io_counters(pernic=True) or {}}
        if LINUX:
            c["stat"] = proc_stat(self.proc)
            c["vm"] = vmstat(self.proc)
            c["disks"] = diskstats(self.proc, self.sys)
            c["snmp"] = net_snmp(self.proc)
        else:
            cs = psutil.cpu_stats()
            c["stat"] = {"ctxt": cs.ctx_switches, "intr": cs.interrupts}
            c["vm"] = {}
            try:
                c["disks"] = {k: {"reads": v.read_count, "writes": v.write_count, "read_ms": v.read_time,
                                  "write_ms": v.write_time, "read_sectors": v.read_bytes / 512,
                                  "write_sectors": v.write_bytes / 512}
                              for k, v in (psutil.disk_io_counters(perdisk=True) or {}).items()
                              if not k.startswith(SKIP_DISKS)}
            except Exception:
                c["disks"] = {}
            c["snmp"] = {}
        return c

    def sample(self, now):
        row, det = {}, {}
        cores = psutil.cpu_count() or 1
        # ---- CPU: utilization breakdown, load relative to cores
        t = psutil.cpu_times_percent(interval=None)
        row["cpu_user"] = t.user + getattr(t, "nice", 0)
        row["cpu_system"] = t.system + getattr(t, "irq", 0) + getattr(t, "softirq", 0)
        row["cpu_iowait"] = getattr(t, "iowait", None)
        row["cpu_steal"] = getattr(t, "steal", None)
        row["cpu_cores"] = cores
        try:
            l1, l5, l15 = os.getloadavg()
            row.update(load5=l5, load15=l15, load_per_core=l1 / cores)
        except OSError:
            pass
        # ---- memory
        vm = psutil.virtual_memory()
        row["mem_available"] = vm.available
        c = self._counters()
        row["swap_used"] = c["swap"].used
        if "procs_running" in c["stat"]:
            row["procs_running"] = c["stat"]["procs_running"]
            row["procs_blocked"] = c["stat"].get("procs_blocked")

        # ---- filesystems: space and inodes, per real filesystem
        filesystems, seen = [], set()
        try:
            parts = psutil.disk_partitions(all=False)
        except Exception:
            parts = []
        for p in parts:
            if p.fstype in PSEUDO_FS or p.device in seen or p.mountpoint.startswith(("/snap/", "/System/Volumes/")) \
                    and p.mountpoint != "/System/Volumes/Data":
                continue
            seen.add(p.device)
            try:
                du = psutil.disk_usage(p.mountpoint)
                st = os.statvfs(p.mountpoint)
            except (OSError, PermissionError):
                continue
            inodes = st.f_files
            inode_pct = (inodes - st.f_ffree) / inodes * 100 if inodes else None
            filesystems.append({"mount": p.mountpoint, "device": p.device, "fstype": p.fstype, "used": du.used,
                                "total": du.total, "pct": du.percent, "inode_pct": inode_pct,
                                "inodes_used": inodes - st.f_ffree if inodes else None, "inodes_total": inodes or None})
        det["filesystems"] = filesystems
        row["fs_max_pct"] = max((f["pct"] for f in filesystems), default=None)
        row["inode_max_pct"] = max((f["inode_pct"] for f in filesystems if f["inode_pct"] is not None), default=None)

        # ---- system health: file descriptors, processes/zombies (every 10 s), temperatures
        fnr = file_nr(self.proc) if LINUX else None
        if fnr:
            used = fnr[0] - fnr[1]
            row["fd_used"] = used
            if 0 < fnr[2] < 2**62:   # modern kernels report LONG_MAX, i.e. effectively unlimited
                row.update(fd_max=fnr[2], fd_pct=used / fnr[2] * 100)
        if now - self.proc_cache[0] >= 10:
            count = zombies = 0
            for pr in psutil.process_iter(["status"]):
                count += 1
                if pr.info.get("status") == psutil.STATUS_ZOMBIE:
                    zombies += 1
            self.proc_cache = (now, count, zombies)
        row["proc_count"], row["proc_zombies"] = self.proc_cache[1], self.proc_cache[2]
        temps, fans = [], []
        try:
            for chip, entries in (psutil.sensors_temperatures() or {}).items():
                for e in entries:
                    if _num(e.current) is not None and -50 < e.current < 150:
                        temps.append({"chip": chip, "label": e.label or chip, "current": e.current,
                                      "high": _num(e.high), "critical": _num(e.critical)})
        except (AttributeError, OSError):
            pass
        try:
            for chip, entries in (psutil.sensors_fans() or {}).items():
                fans.extend({"chip": chip, "label": e.label or chip, "rpm": e.current} for e in entries)
        except (AttributeError, OSError):
            pass
        det["temperatures"], det["fans"] = temps, fans
        row["temp_max_c"] = max((x["current"] for x in temps), default=None)

        # ---- TCP connection states (Linux)
        if LINUX:
            states = tcp_states(self.proc)
            det["tcp_states"] = states
            row["tcp_established"] = states.get("established", 0)
            row["tcp_time_wait"] = states.get("time_wait", 0)
            row["tcp_close_wait"] = states.get("close_wait", 0)
            row["tcp_total"] = sum(v for k, v in states.items() if k != "listen")

        # ---- rates from counters
        nic_stats = {}
        try:
            nic_stats = psutil.net_if_stats() or {}
        except Exception:
            pass
        if self.prev:
            dt = max(now - self.prev[0], 1e-6)
            p = self.prev[1]
            d = lambda a, b: max(0, a - b)  # noqa: E731  (counters can reset)
            s0, s1 = p["stat"], c["stat"]
            if "ctxt" in s1 and "ctxt" in s0:
                row["ctx_switches_ps"] = d(s1["ctxt"], s0["ctxt"]) / dt
            if "intr" in s1 and "intr" in s0:
                row["interrupts_ps"] = d(s1["intr"], s0["intr"]) / dt
            row["swap_in_bps"] = d(c["swap"].sin, p["swap"].sin) / dt
            row["swap_out_bps"] = d(c["swap"].sout, p["swap"].sout) / dt
            if "oom_kill" in c["vm"] and "oom_kill" in p["vm"]:
                row["oom_kills"] = d(c["vm"]["oom_kill"], p["vm"]["oom_kill"])
            # disks: IOPS, latency (await), busy %, queue depth
            disks, tot_ops, tot_ms, util_max, queue = [], 0, 0, None, None
            for name, cur in c["disks"].items():
                old = p["disks"].get(name)
                if not old:
                    continue
                r, w = d(cur["reads"], old["reads"]), d(cur["writes"], old["writes"])
                ms = d(cur["read_ms"], old["read_ms"]) + d(cur["write_ms"], old["write_ms"])
                info = {"name": name, "read_iops": r / dt, "write_iops": w / dt,
                        "read_bps": d(cur["read_sectors"], old["read_sectors"]) * 512 / dt,
                        "write_bps": d(cur["write_sectors"], old["write_sectors"]) * 512 / dt,
                        "await_ms": ms / (r + w) if r + w else 0.0}
                tot_ops += r + w
                tot_ms += ms
                if "io_ms" in cur:
                    info["util_pct"] = min(100.0, d(cur["io_ms"], old["io_ms"]) / (dt * 1000) * 100)
                    info["queue"] = d(cur["weighted_ms"], old["weighted_ms"]) / (dt * 1000)
                    util_max = max(util_max or 0, info["util_pct"])
                    queue = (queue or 0) + info["queue"]
                disks.append(info)
            det["disks"] = disks
            if disks:
                row["disk_read_iops"] = sum(x["read_iops"] for x in disks)
                row["disk_write_iops"] = sum(x["write_iops"] for x in disks)
                row["disk_await_ms"] = tot_ms / tot_ops if tot_ops else 0.0
                row["disk_util_pct"], row["disk_queue"] = util_max, queue
            # network: utilization vs link speed, errors, drops
            nics, util_max, errs, drops = [], None, 0, 0
            for name, cur in c["nics"].items():
                old = p["nics"].get(name)
                if not old or name.startswith(("lo", "docker", "veth", "br-", "virbr")):
                    continue
                rx = d(cur.bytes_recv, old.bytes_recv) / dt
                tx = d(cur.bytes_sent, old.bytes_sent) / dt
                e = d(cur.errin + cur.errout, old.errin + old.errout)
                dr = d(cur.dropin + cur.dropout, old.dropin + old.dropout)
                st = nic_stats.get(name)
                speed = st.speed if st and st.speed and st.speed > 0 else None   # Mbit/s
                util = max(rx, tx) * 8 / (speed * 1e6) * 100 if speed else None
                if util is not None:
                    util_max = max(util_max or 0, util)
                errs += e
                drops += dr
                if st is None or st.isup:
                    nics.append({"name": name, "rx_bps": rx, "tx_bps": tx, "speed_mbps": speed,
                                 "util_pct": util, "errors": e, "drops": dr})
            det["nics"] = nics
            row["net_util_pct"] = util_max
            row["net_errors_ps"] = errs / dt
            row["net_drops_ps"] = drops / dt
            # TCP retransmits
            if "RetransSegs" in c["snmp"] and "RetransSegs" in p["snmp"]:
                rs = d(c["snmp"]["RetransSegs"], p["snmp"]["RetransSegs"])
                out = d(c["snmp"].get("OutSegs", 0), p["snmp"].get("OutSegs", 0))
                row["tcp_retrans_ps"] = rs / dt
                row["tcp_retrans_pct"] = rs / out * 100 if out else 0.0
        self.prev = (now, c)
        self.details = det
        return row


# --------------------------------------------------------------------------- slow checks

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
            "detail": detail + f" (as of the last package list refresh)"}


def tls_expiry(host, port=443, timeout=10):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host) as t:
                cert = t.getpeercert()
        expires = ssl.cert_time_to_seconds(cert["notAfter"])
        days = (expires - time.time()) / 86400
        status = "fail" if days < 3 else "warn" if days < 14 else "pass"
        return {"host": f"{host}:{port}", "status": status, "expires": expires, "days_left": days,
                "detail": f"expires in {days:.0f} days"}
    except ssl.SSLCertVerificationError as e:
        return {"host": f"{host}:{port}", "status": "fail", "detail": f"certificate invalid: {e.verify_message}"}
    except Exception as e:
        return {"host": f"{host}:{port}", "status": "warn", "detail": f"couldn't check: {e}"}


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


class HealthMonitor:
    """Runs the slow checks on their own schedules and keeps the latest results.
    Gauges (DNS, clock) are carried into every system sample; counts (failed logins, system log
    errors/warnings) are handed to the next sample once, so bucket sums are correct."""

    SCHEDULE = {"dns": 60, "clock": 300, "logs": 300, "ipmi": 300, "smart": 21600, "patches": 21600, "tls": 21600}

    def __init__(self, config, on_change=None):
        self.config = config
        self.on_change = on_change or (lambda: None)
        self.results = {}
        self.lock = threading.Lock()
        self.last_run = {}
        self.pending = {}
        self.logs_since = time.time()
        self.gauges = {}

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

    def tls_hosts(self):
        from urllib.parse import urlparse
        hosts = []
        for key in ("rpc_url", "validators_rpc_url"):
            u = urlparse(self.config.get(key) or "")
            if u.scheme == "https" and u.hostname:
                hosts.append((u.hostname, u.port or 443))
        for item in (self.config.get("tls_check_hosts") or "").split(","):
            item = item.strip()
            if item:
                host, _, port = item.rpartition(":") if re.search(r":\d+$", item) else (item, "", "443")
                hosts.append((host, int(port)))
        return list(dict.fromkeys(hosts))

    def run_check(self, name):
        sudo = bool(self.config.get("health_checks_sudo"))
        if name == "dns":
            host = self.config.get("dns_check_host") or "near.org"
            ms, ok, err = resolve_time(host)
            self.gauges.update(dns_ms=ms, dns_ok=1 if ok else 0)
            self._set("dns", {"status": "pass" if ok and ms < 500 else "warn", "host": host, "ms": ms,
                              "detail": f"{host} resolved in {ms:.0f} ms" if ok else f"{host} failed to resolve: {err}"})
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
        elif name == "tls":
            certs = [tls_expiry(h, p) for h, p in self.tls_hosts()]
            worst = "fail" if any(c["status"] == "fail" for c in certs) else \
                "warn" if any(c["status"] == "warn" for c in certs) else "pass" if certs else "na"
            self._set("tls", {"status": worst, "certs": certs,
                              "detail": f"{len(certs)} certificate(s) checked" if certs else
                              "No HTTPS endpoints to check (add some in Settings → TLS hosts)."})
        elif name == "ipmi":
            self._set("ipmi", ipmi_status(sudo))

    def run(self, stop):
        try:
            self._set("last_boot", last_boot_status())
        except Exception as e:
            self._set("last_boot", {"status": "na", "detail": f"check failed: {e}"})
        while not stop.is_set():
            if self.config.get("collect_health"):
                now = time.time()
                for name, every in self.SCHEDULE.items():
                    if now - self.last_run.get(name, 0) >= every:
                        self.last_run[name] = now
                        try:
                            self.run_check(name)
                        except Exception as e:
                            self._set(name, {"status": "na", "detail": f"check failed: {e}"})
            stop.wait(5)
