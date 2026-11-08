"""Per-sample host metrics: the basic system collector plus the extended USE metrics
(CPU breakdown, run queue, swap rates, OOM kills, per-disk latency/IOPS/busy/queue, filesystems and
inodes, link utilization, packet errors, TCP retransmits and states, processes, file handles, sensors).
Linux readers take a root path so they can be tested against fake /proc and /sys trees."""

import math
import os
import sys
import time

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


class SystemCollector:
    def __init__(self, config):
        self.config = config
        self.prev_disk = None
        self.prev_net = None
        self.prev_t = None
        self.proc = None
        self.first = True
        self.ext = ExtendedStats()
        self.monitor = None   # checks.HealthMonitor, set by the system module

    def _find_proc(self):
        name = self.config.get("process_name")
        if not name:
            return None
        if self.proc is not None:
            try:
                if self.proc.is_running() and self.proc.name() == name:
                    return self.proc
            except psutil.Error:
                pass
            self.proc = None
        for p in psutil.process_iter(["name"]):
            if p.info.get("name") == name:
                self.proc = p
                p.cpu_percent(None)  # prime; first reading is meaningless
                return None
        return None

    def sample(self):
        now = time.time()
        row = {}
        if self.first:
            # cpu_percent(None) measures since the previous call; with no previous call it
            # returns a meaningless 0.0, so measure the first sample over one second.
            row["cpu_pct"] = psutil.cpu_percent(interval=1.0)
            self.first = False
            now = time.time()
        else:
            row["cpu_pct"] = psutil.cpu_percent(interval=None)
        boot = psutil.boot_time()
        row["boot_time"] = round(boot)  # rounded: some systems report sub-second jitter
        row["uptime_sec"] = max(0.0, now - boot)
        try:
            row["load1"] = os.getloadavg()[0]
        except OSError:
            pass
        vm = psutil.virtual_memory()
        row.update(mem_pct=vm.percent, mem_used=vm.total - vm.available, mem_total=vm.total)
        row["swap_pct"] = psutil.swap_memory().percent
        try:
            du = psutil.disk_usage(self.config.get("disk_path"))
            row.update(disk_pct=du.percent, disk_used=du.used, disk_total=du.total)
        except OSError:
            pass

        disk = psutil.disk_io_counters()
        net_bytes = None
        nics = psutil.net_io_counters(pernic=True)
        if nics:
            sent = sum(v.bytes_sent for k, v in nics.items() if not k.startswith("lo"))
            recv = sum(v.bytes_recv for k, v in nics.items() if not k.startswith("lo"))
            net_bytes = (sent, recv)
        if self.prev_t is not None:
            dt = max(now - self.prev_t, 1e-6)
            if disk and self.prev_disk:
                row["disk_read_bps"] = max(0, disk.read_bytes - self.prev_disk.read_bytes) / dt
                row["disk_write_bps"] = max(0, disk.write_bytes - self.prev_disk.write_bytes) / dt
            if net_bytes and self.prev_net:
                row["net_sent_bps"] = max(0, net_bytes[0] - self.prev_net[0]) / dt
                row["net_recv_bps"] = max(0, net_bytes[1] - self.prev_net[1]) / dt
        self.prev_disk, self.prev_net, self.prev_t = disk, net_bytes, now

        proc = self._find_proc()
        if proc is not None:
            try:
                with proc.oneshot():
                    # psutil reports per-core %; normalise to % of the whole machine.
                    row["proc_cpu_pct"] = proc.cpu_percent(None) / (psutil.cpu_count() or 1)
                    row["proc_rss"] = proc.memory_info().rss
            except psutil.Error:
                self.proc = None
        try:
            row.update({k: v for k, v in self.ext.sample(now).items() if v is not None})
        except Exception as e:  # extended metrics are best-effort; never lose the basic sample
            print(f"[system] extended stats error: {e}", flush=True)
        if self.monitor is not None and self.config.get("collect_health"):
            row.update({k: v for k, v in self.monitor.gauges.items() if v is not None})
            row.update(self.monitor.pop_counts())
        return now, row
