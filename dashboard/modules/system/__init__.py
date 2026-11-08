"""System module: host statistics (USE method) and slower health/security checks."""

import sys
import threading
import time

import psutil

from dashboard.core.alerts import alert
from dashboard.core.collect import run_loop
from dashboard.core.config import Setting, boolean, existing_path, hostname, text
from dashboard.core.module import Module
from dashboard.core.storage import db_query
from dashboard.modules.system.checks import HealthMonitor, detect_virtualization
from dashboard.modules.system.stats import SystemCollector

SYSTEM_COLS = [
    "cpu_pct", "load1", "mem_pct", "mem_used", "mem_total", "swap_pct",
    "disk_pct", "disk_used", "disk_total", "disk_read_bps", "disk_write_bps",
    "net_sent_bps", "net_recv_bps", "proc_cpu_pct", "proc_rss",
    "uptime_sec", "boot_time",
    # extended USE metrics (stats.ExtendedStats) and slow checks (checks.HealthMonitor)
    "cpu_user", "cpu_system", "cpu_iowait", "cpu_steal", "cpu_cores", "load5", "load15", "load_per_core",
    "procs_running", "procs_blocked", "ctx_switches_ps", "interrupts_ps",
    "mem_available", "swap_used", "swap_in_bps", "swap_out_bps", "oom_kills",
    "disk_read_iops", "disk_write_iops", "disk_await_ms", "disk_util_pct", "disk_queue", "fs_max_pct", "inode_max_pct",
    "net_util_pct", "net_errors_ps", "net_drops_ps", "tcp_retrans_ps", "tcp_retrans_pct",
    "tcp_established", "tcp_time_wait", "tcp_close_wait", "tcp_total", "dns_ms", "dns_ok",
    "proc_count", "proc_zombies", "fd_used", "fd_max", "fd_pct", "clock_offset_ms", "ntp_synced", "temp_max_c",
    "failed_logins", "sys_log_errors", "sys_log_warnings",
]


class SystemModule(Module):
    name = "system"
    title = "System"
    settings = {
        # Filesystem path whose usage is tracked (point it at your data directory's mount).
        "disk_path": Setting("/", existing_path),
        # Process whose CPU and memory are tracked separately (e.g. the NEAR client).
        "process_name": Setting("neard", text),
        "collect_system": Setting(True, boolean),
        # Slower health/security checks: DNS, clock, logins, log rates, SMART, patches, IPMI.
        "collect_health": Setting(True, boolean),
        # Hostname whose DNS resolution time is measured (every minute).
        "dns_check_host": Setting("near.org", hostname),
        # Use `sudo -n` for SMART (smartctl) and IPMI (ipmitool). Only with passwordless sudo for
        # those commands; otherwise every attempt is logged as a failed sudo login.
        "health_checks_sudo": Setting(False, boolean),
    }
    series = {"system": SYSTEM_COLS}
    aggregations = {**{c: "MAX" for c in ("disk_total", "mem_total", "uptime_sec", "boot_time", "cpu_cores", "fd_max",
                                          "fs_max_pct", "inode_max_pct", "temp_max_c", "proc_zombies")},
                    "dns_ok": "MIN", "ntp_synced": "MIN",
                    # event counts: a bucket's value is the total within it
                    **{c: "SUM" for c in ("oom_kills", "failed_logins", "sys_log_errors", "sys_log_warnings")}}
    retention = ["DELETE FROM system WHERE ts < ?"]

    def __init__(self, app):
        super().__init__(app)
        self.collector = SystemCollector(self.config)
        try:
            self.virtualization = detect_virtualization()
        except Exception as e:   # never block startup on detection
            self.virtualization = {"virtual": None, "kind": None, "type": "unknown", "detail": f"detection failed: {e}"}
        self.monitor = HealthMonitor(self.config, virtualization=self.virtualization)
        # A VM or container can't see the host's sensors: skip temperatures and fans.
        self.collector.ext.hardware_sensors = not self.virtualization.get("virtual")
        self.collector.monitor = self.monitor

    def start(self, stop):
        threading.Thread(target=self.monitor.run, args=(stop,), daemon=True).start()
        threading.Thread(target=run_loop, daemon=True,
                         args=(self.app, "system", self.collector, "system", SYSTEM_COLS, stop,
                               lambda: self.config.get("collect_system"))).start()

    def metrics(self, conn, since, bucket):
        return {"system": db_query(conn, "system", SYSTEM_COLS, since, bucket)}

    def routes(self):
        return {"/api/health": self.health}

    def health(self, req):
        det = self.collector.ext.details
        return {
            "time": time.time(), "alerts": self.app.alerts,
            "checks": self.monitor.snapshot(),
            "filesystems": det.get("filesystems", []), "disks": det.get("disks", []),
            "nics": det.get("nics", []), "tcp_states": det.get("tcp_states", {}),
            "temperatures": det.get("temperatures", []), "fans": det.get("fans", []),
            "cores": psutil.cpu_count(), "platform": sys.platform, "virtualization": self.virtualization,
        }

    def alerts(self, ctx):
        """Host symptoms: running out of space/memory, latency, packet loss, failing hardware..."""
        out, cfg = [], ctx.config

        def add(id_, severity, title, detail):
            out.append(alert(id_, severity, title, detail))

        # ---- host resources (USE): saturation and errors that users would notice
        if cfg["collect_system"]:
            w = ctx.window("system", ["disk_await_ms", "disk_read_iops", "disk_write_iops", "swap_in_bps",
                                               "mem_available", "mem_total", "tcp_retrans_pct", "net_errors_ps",
                                               "net_drops_ps", "cpu_steal", "cpu_iowait", "fd_pct"], 300)
            load10 = ctx.window("system", ["load_per_core"], 600)["load_per_core"]
            for fs in (self.collector.ext.details.get("filesystems") or []):
                for kind, pct in (("space", fs.get("pct")), ("inodes", fs.get("inode_pct"))):
                    if pct is not None and pct >= 85:
                        add(f"fs_{kind}_{fs['mount']}", "critical" if pct >= 90 else "warning",
                            f"Filesystem {fs['mount']} is {pct:.0f}% full ({kind})",
                            "Free up space or grow the volume before it fills." if kind == "space" else
                            "Running out of inodes (too many files).")
            iops = (w["disk_read_iops"] or 0) + (w["disk_write_iops"] or 0)
            if w["disk_await_ms"] is not None and w["disk_await_ms"] > 50 and iops > 5:
                add("disk_latency", "warning", "Disk I/O latency is high",
                    f"Average request takes {w['disk_await_ms']:.0f} ms over 5 minutes ({iops:.0f} IOPS).")
            oom = ctx.window("system", ["oom_kills"], 3600, "SUM")["oom_kills"]
            if oom:
                add("oom", "critical", "Out-of-memory kills", f"The kernel killed {oom:.0f} process(es) in the last hour.")
            if w["swap_in_bps"] is not None and w["swap_in_bps"] > 1_000_000:
                add("swapping", "warning", "System is swapping", f"Swap-in {w['swap_in_bps'] / 1e6:.1f} MB/s (5-min average).")
            if w["mem_available"] is not None and w["mem_total"] and w["mem_available"] / w["mem_total"] < 0.05:
                add("mem_low", "warning", "Memory almost exhausted", "Less than 5% of memory available (5-min average).")
            if w["tcp_retrans_pct"] is not None and w["tcp_retrans_pct"] > 5:
                add("retrans", "warning", "High TCP retransmits", f"{w['tcp_retrans_pct']:.1f}% of segments retransmitted (5-min average).")
            if (w["net_errors_ps"] or 0) + (w["net_drops_ps"] or 0) > 10:
                add("net_errors", "warning", "Network errors and drops",
                    f"{(w['net_errors_ps'] or 0) + (w['net_drops_ps'] or 0):.0f} packet errors/drops per second (5-min average).")
            if w["cpu_steal"] is not None and w["cpu_steal"] > 10:
                add("steal", "warning", "CPU steal is high", f"{w['cpu_steal']:.0f}% of CPU time taken by the hypervisor (noisy neighbour).")
            if w["cpu_iowait"] is not None and w["cpu_iowait"] > 30:
                add("iowait", "warning", "CPU waiting on disk", f"{w['cpu_iowait']:.0f}% iowait (5-min average).")
            if load10 is not None and load10 > 2:
                add("load", "warning", "CPU is saturated", f"Load is {load10:.1f}× the core count (10-min average).")
            if w["fd_pct"] is not None and w["fd_pct"] > 80:
                add("fds", "warning", "File descriptors running out", f"{w['fd_pct']:.0f}% of the system limit in use.")
            zombies = ctx.latest("system", "proc_zombies")
            if zombies and zombies > 50:
                add("zombies", "warning", "Many zombie processes", f"{zombies:.0f} zombie processes.")
            temp = ctx.latest("system", "temp_max_c")
            if temp is not None and temp > 85:
                add("temp", "critical" if temp > 95 else "warning", "Hardware is running hot", f"Hottest sensor at {temp:.0f} °C.")
        # ---- slow health / security checks
        if cfg["collect_health"] and self.monitor is not None:
            r = self.monitor.snapshot()
            c = r.get("clock") or {}
            if c.get("synced") is False:
                add("ntp", "warning", "Clock isn't NTP-synchronised", c.get("detail", ""))
            if c.get("offset_ms") is not None and abs(c["offset_ms"]) > 100:
                add("clock_drift", "critical" if abs(c["offset_ms"]) > 500 else "warning", "Clock drift",
                    f"System clock is off by {c['offset_ms']:+.0f} ms.")
            if (r.get("dns") or {}).get("status") == "warn":
                add("dns", "warning", "DNS resolution is failing or slow", r["dns"].get("detail", ""))
            if (r.get("smart") or {}).get("status") == "fail":
                add("smart", "critical", "A drive is failing (SMART)", r["smart"].get("detail", ""))
            if (r.get("ipmi") or {}).get("status") == "fail":
                add("ipmi", "critical", "Power supply or fan failure", r["ipmi"].get("detail", ""))
            pt = r.get("patches") or {}
            if pt.get("security"):
                add("patches", "info", "Security updates pending", pt.get("detail", ""))
            elif pt.get("reboot_required"):
                add("reboot", "info", "Reboot required", "A reboot is needed to finish installing updates.")
            if (r.get("last_boot") or {}).get("unexpected"):
                add("unexpected_reboot", "warning", "Last reboot was unexpected", r["last_boot"].get("detail", ""))
            fails = ctx.window("system", ["failed_logins"], 3600, "SUM")["failed_logins"]
            if fails and fails > 100:
                add("logins", "warning", "Many failed logins", f"{fails:.0f} failed SSH logins in the last hour.")
        return out


MODULE = SystemModule
