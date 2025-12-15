# panel_health.py
"""
جمع‌آوری وضعیت سلامت سیستم (CPU/RAM/Disk/Inode) و ثبت آخرین مشکل.

- update_health_status(): اندازه‌گیری جدید و ذخیره در کش
- register_issue(reason): ثبت متن آخرین مشکل
- get_health_status(): برگرداندن HealthStatus آخرین وضعیت
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from panel_paths import SDCARD_ROOT
from panel_db import kv_get, kv_set

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None


@dataclass
class HealthStatus:
    cpu_percent: float
    mem_percent: float
    disk_percent: float
    inode_percent: float
    last_issue: Optional[str]
    ts: str  # ISO string


_LAST_STATUS: Optional[HealthStatus] = None
_KV_KEY = "health:latest"


def _measure_cpu_percent() -> float:
    if psutil is not None:
        try:
            return float(psutil.cpu_percent(interval=0.5))
        except Exception:
            pass
    # fallback: بدون psutil → /proc/stat دو نمونه
    try:
        def _read_cpu_times():
            with open("/proc/stat", "r") as f:
                first = f.readline()
            parts = first.strip().split()
            if len(parts) < 5 or parts[0] != "cpu":
                raise RuntimeError("unexpected /proc/stat format")
            values = [float(x) for x in parts[1:]]
            idle = values[3]
            iowait = values[4] if len(values) > 4 else 0.0
            total = sum(values)
            return idle + iowait, total

        idle1, total1 = _read_cpu_times()
        time.sleep(0.5)
        idle2, total2 = _read_cpu_times()
        diff_idle = idle2 - idle1
        diff_total = total2 - total1
        if diff_total <= 0:
            return -1.0
        usage = (1.0 - (diff_idle / diff_total)) * 100.0
        # clamp to [0,100]
        usage = max(0.0, min(usage, 100.0))
        return float(usage)
    except Exception as e:
        print("[panel_health] cpu fallback error:", e)
    return -1.0


def _measure_mem_percent() -> float:
    if psutil is not None:
        try:
            return float(psutil.virtual_memory().percent)
        except Exception:
            pass

    # fallback: /proc/meminfo
    try:
        mem_total = None
        mem_available = None
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = float(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available = float(line.split()[1])
        if mem_total and mem_available:
            used = mem_total - mem_available
            return used * 100.0 / mem_total
    except Exception:
        pass
    return -1.0


def _measure_disk_and_inode_percent(path: str) -> (float, float):
    """
    استفاده از os.statvfs برای محاسبه‌ی حجم و inode.
    """
    try:
        st = os.statvfs(path)
        total = float(st.f_blocks) * st.f_frsize
        free = float(st.f_bavail) * st.f_frsize
        used = total - free
        disk_percent = (used * 100.0 / total) if total > 0 else -1.0

        total_inodes = float(st.f_files)
        free_inodes = float(st.f_favail if st.f_favail > 0 else st.f_ffree)
        used_inodes = total_inodes - free_inodes
        inode_percent = (used_inodes * 100.0 / total_inodes) if total_inodes > 0 else -1.0
        return disk_percent, inode_percent
    except Exception:
        return -1.0, -1.0


def update_health_status() -> HealthStatus:
    """
    اندازه‌گیری جدید انجام می‌دهد و نتیجه را در کش + kv_store ذخیره می‌کند.
    """
    global _LAST_STATUS

    cpu = _measure_cpu_percent()
    mem = _measure_mem_percent()
    disk, inode = _measure_disk_and_inode_percent(SDCARD_ROOT)

    now_iso = datetime.now(timezone.utc).isoformat()

    last_issue = _LAST_STATUS.last_issue if _LAST_STATUS else None
    status = HealthStatus(
        cpu_percent=cpu,
        mem_percent=mem,
        disk_percent=disk,
        inode_percent=inode,
        last_issue=last_issue,
        ts=now_iso,
    )
    _LAST_STATUS = status

    data = {
        "cpu_percent": cpu,
        "mem_percent": mem,
        "disk_percent": disk,
        "inode_percent": inode,
        "last_issue": last_issue,
        "ts": now_iso,
    }
    kv_set(_KV_KEY, json.dumps(data))
    return status


def register_issue(reason: str) -> None:
    """
    آخرین مشکل را ثبت می‌کند (در حافظه و kv_store).
    """
    global _LAST_STATUS
    if _LAST_STATUS is None:
        # اگر هنوز اندازه‌گیری نشده، یکبار اندازه‌گیری کن
        update_health_status()
    if _LAST_STATUS is None:
        return

    _LAST_STATUS.last_issue = reason
    _LAST_STATUS.ts = datetime.now(timezone.utc).isoformat()

    data = {
        "cpu_percent": _LAST_STATUS.cpu_percent,
        "mem_percent": _LAST_STATUS.mem_percent,
        "disk_percent": _LAST_STATUS.disk_percent,
        "inode_percent": _LAST_STATUS.inode_percent,
        "last_issue": _LAST_STATUS.last_issue,
        "ts": _LAST_STATUS.ts,
    }
    kv_set(_KV_KEY, json.dumps(data))


def get_health_status() -> Optional[HealthStatus]:
    """
    آخرین HealthStatus را برمی‌گرداند. اگر در حافظه نباشد، از kv_store لود می‌کند.
    """
    global _LAST_STATUS
    if _LAST_STATUS is not None:
        return _LAST_STATUS

    raw = kv_get(_KV_KEY, None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None

    status = HealthStatus(
        cpu_percent=float(data.get("cpu_percent", -1.0)),
        mem_percent=float(data.get("mem_percent", -1.0)),
        disk_percent=float(data.get("disk_percent", -1.0)),
        inode_percent=float(data.get("inode_percent", -1.0)),
        last_issue=data.get("last_issue"),
        ts=str(data.get("ts")),
    )
    _LAST_STATUS = status
    return status


if __name__ == "__main__":
    print("=== panel_health test ===")
    from panel_paths import ensure_dirs

    ensure_dirs()
    s = update_health_status()
    print("HealthStatus:", s)
    register_issue("test_issue_example")
    s2 = get_health_status()
    print("HealthStatus after issue:", s2)
    print("✅ panel_health basic test OK")

