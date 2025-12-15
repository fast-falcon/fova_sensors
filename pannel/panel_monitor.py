# panel_monitor.py
"""
اسکریپت مانیتورینگ:

- هر چند ثانیه:
  - فضای sdcard را چک می‌کند و در صورت نیاز داده‌های قدیمی را پاک می‌کند (چرخشی)
  - health سیستم (CPU/RAM/Disk/Inode) را آپدیت می‌کند و در صورت بحرانی شدن، last_issue ثبت می‌کند
  - اگر tinycap یا مرورگر افتاده باشند، دوباره آن‌ها را بالا می‌آورد

این اسکریپت می‌تواند به صورت مستقل اجرا شود (service جداگانه)
یا از داخل central_role/sensor_role به صورت ترد.
"""

import os
import sqlite3
import time
import subprocess
from typing import List

from panel_paths import (
    SDCARD_ROOT,
    DB_PATH,
    MIN_FREE_SPACE_BYTES,
    DEFAULT_HTTP_PORT,
)
from panel_health import update_health_status, register_issue
from panel_net_common import su_env_run

# هر چند ثانیه مانیتور اجرا شود
CHECK_INTERVAL_SEC = 10

# آستانه‌های بحرانی (می‌توانی بعداً از config بگیری)
CPU_CRITICAL = 95.0
MEM_CRITICAL = 95.0
DISK_CRITICAL = 95.0
INODE_CRITICAL = 95.0

# اگر True باشد در صورت وضع بحرانی ریبوت می‌کند (فعلاً False برای تست)
AUTO_REBOOT_ON_CRITICAL = False

BROWSER_PROCESS_HINTS = ["com.android.browser", "com.android.chrome", "org.chromium"]
RECORDER_PROCESS_HINTS = ["tinycap"]



# IMPORTANT:
# این ماژول قبلاً برای تست، اگر tinycap پیدا نمی‌شد یک ضبط ساده‌ی جداگانه را
# (detach) استارت می‌کرد. وقتی panel_audio_local فعال باشد، این رفتار باعث
# تداخل/قفل شدن device (pcm) و تولید فایل‌های 0-byte می‌شود.
#
# به صورت پیش‌فرض این fallback را خاموش می‌کنیم.
ENABLE_FALLBACK_RECORDER = False

def _stat_free_space_bytes(path: str) -> int:
    st = os.statvfs(path)
    return int(st.f_bavail * st.f_frsize)


def _prune_audio_segments(batch_size: int = 100) -> int:
    """
    قدیمی‌ترین رکوردهای audio_segments را حذف می‌کند و فایل‌ها را پاک می‌کند.
    خروجی: تعداد رکورد حذف شده.
    """
    if not os.path.exists(DB_PATH):
        return 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, filepath FROM audio_segments ORDER BY ts ASC LIMIT ?",
        (batch_size,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return 0

    deleted = 0
    for _id, filepath in rows:
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception as e:
                print("[panel_monitor] remove audio file error:", e)
        cur.execute("DELETE FROM audio_segments WHERE id = ?", (_id,))
        deleted += 1

    conn.commit()
    conn.close()
    print(f"[panel_monitor] pruned {deleted} audio_segments")
    return deleted


def _prune_sensor_samples(batch_size: int = 500) -> int:
    """
    قدیمی‌ترین رکوردهای sensor_samples را حذف می‌کند.
    """
    if not os.path.exists(DB_PATH):
        return 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM sensor_samples ORDER BY ts ASC LIMIT ?",
        (batch_size,),
    )
    rows = cur.fetchall()
    if not rows:
        conn.close()
        return 0

    ids = [r[0] for r in rows]
    cur.executemany("DELETE FROM sensor_samples WHERE id = ?", [(i,) for i in ids])
    conn.commit()
    conn.close()
    print(f"[panel_monitor] pruned {len(ids)} sensor_samples")
    return len(ids)


def prune_storage_if_needed() -> None:
    """
    اگر فضای خالی sdcard کمتر از MIN_FREE_SPACE_BYTES باشد،
    به صورت چرخشی فایل‌های صوتی و در صورت نیاز داده‌های سنسور را حذف می‌کند
    تا حداقل ۱ گیگ خالی شود (اگر ممکن بود).
    """
    try:
        free = _stat_free_space_bytes(SDCARD_ROOT)
    except Exception as e:
        print("[panel_monitor] statvfs error:", e)
        return

    if free >= MIN_FREE_SPACE_BYTES:
        return

    print(
        f"[panel_monitor] low disk space: {free / (1024*1024):.1f} MB free, "
        "starting pruning..."
    )

    # چند دور سعی می‌کنیم تا جا خالی شود
    for _ in range(10):
        free = _stat_free_space_bytes(SDCARD_ROOT)
        if free >= MIN_FREE_SPACE_BYTES:
            break

        deleted_audio = _prune_audio_segments(batch_size=200)
        free = _stat_free_space_bytes(SDCARD_ROOT)
        if free >= MIN_FREE_SPACE_BYTES:
            break

        deleted_sensors = _prune_sensor_samples(batch_size=1000)
        free = _stat_free_space_bytes(SDCARD_ROOT)

        if deleted_audio == 0 and deleted_sensors == 0:
            # دیگر چیزی برای حذف نیست
            break

    free = _stat_free_space_bytes(SDCARD_ROOT)
    if free < MIN_FREE_SPACE_BYTES:
        msg = (
            f"disk free still low after pruning: {free / (1024*1024):.1f} MB, "
            "possible SD card almost full."
        )
        print("[panel_monitor]", msg)
        register_issue(msg)


def _run_ps_output() -> str:
    try:
        res = subprocess.run(["ps"], capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            return res.stdout
    except Exception as e:
        print("[panel_monitor] ps error:", e)
    return ""


def _is_any_process_running(hints: List[str]) -> bool:
    out = _run_ps_output()
    if not out:
        return False
    for line in out.splitlines():
        for h in hints:
            if h in line:
                return True
    return False


def _start_browser_if_needed(flask_port: int = DEFAULT_HTTP_PORT) -> None:
    if _is_any_process_running(BROWSER_PROCESS_HINTS):
        return
    url = f"http://127.0.0.1:{flask_port}/"
    print("[panel_monitor] browser not detected, starting via am:", url)
    try:
        su_env_run(
            [
                "am", "start",
                "-a", "android.intent.action.VIEW",
                "-d", url,
            ],
            detach=True,
        )
    except Exception as e:
        print("[panel_monitor] start browser error:", e)


def _start_audio_recorder_if_needed() -> None:
    """
    اگر پروسس tinycap پیدا نشود، یک tinycap ساده را استارت می‌کند.
    TODO: بعداً این تابع را به panel_audio_local یا اسکریپت واقعی ضبط وصل کن.
    """
    if not ENABLE_FALLBACK_RECORDER:
        return

    if _is_any_process_running(RECORDER_PROCESS_HINTS):
        return

    print("[panel_monitor] tinycap not detected, starting simple recorder (TODO wiring).")
    # برای تست، یک ضبط موقت ۳۰ ثانیه‌ای
    out_dir = os.path.join(SDCARD_ROOT, "audio")
    os.makedirs(out_dir, exist_ok=True)
    outfile = os.path.join(out_dir, "monitor_test.wav")
    try:
        su_env_run(
            [
                "tinycap",
                outfile,
                "-r", "44100",
                "-b", "16",
                "-c", "2",
            ],
            detach=True,
        )
    except Exception as e:
        print("[panel_monitor] start tinycap error:", e)


def check_health_thresholds_and_maybe_reboot():
    """
    health را آپدیت می‌کند و در صورت عبور از آستانه، issue ثبت می‌کند.
    اگر AUTO_REBOOT_ON_CRITICAL فعال باشد، می‌تواند reboot هم بزند.
    """
    status = update_health_status()
    critical_reasons = []

    if status.cpu_percent >= 0 and status.cpu_percent >= CPU_CRITICAL:
        critical_reasons.append(f"CPU {status.cpu_percent:.1f}% ≥ {CPU_CRITICAL}%")

    if status.mem_percent >= 0 and status.mem_percent >= MEM_CRITICAL:
        critical_reasons.append(f"MEM {status.mem_percent:.1f}% ≥ {MEM_CRITICAL}%")

    if status.disk_percent >= 0 and status.disk_percent >= DISK_CRITICAL:
        critical_reasons.append(f"DISK {status.disk_percent:.1f}% ≥ {DISK_CRITICAL}%")

    if status.inode_percent >= 0 and status.inode_percent >= INODE_CRITICAL:
        critical_reasons.append(f"INODE {status.inode_percent:.1f}% ≥ {INODE_CRITICAL}%")

    if not critical_reasons:
        return

    reason = " ; ".join(critical_reasons)
    print("[panel_monitor] CRITICAL health:", reason)
    register_issue(reason)

    if AUTO_REBOOT_ON_CRITICAL:
        # قبل از reboot می‌توانی در kv_store دلیل را بگذاری (اگر لازم شد)
        try:
            from panel_db import kv_set
            kv_set("last_restart_reason", reason)
        except Exception:
            pass
        print("[panel_monitor] AUTO_REBOOT_ON_CRITICAL enabled → rebooting...")
        try:
            su_env_run(["reboot"], detach=True)
        except Exception as e:
            print("[panel_monitor] reboot error:", e)


def monitor_loop(flask_port: int = DEFAULT_HTTP_PORT):
    """
    حلقه‌ی اصلی مانیتور. این تابع را می‌توانی در یک thread یا process جدا صدا بزنی.
    """
    print("[panel_monitor] starting monitor loop (interval", CHECK_INTERVAL_SEC, "sec)")
    while True:
        try:
            prune_storage_if_needed()
            _start_audio_recorder_if_needed()
            _start_browser_if_needed(flask_port=flask_port)
            check_health_thresholds_and_maybe_reboot()
        except Exception as e:
            print("[panel_monitor] loop error:", e)
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    print("=== panel_monitor test (single iteration) ===")
    from panel_paths import ensure_dirs

    ensure_dirs()
    # فقط یک بار کارهای اصلی را صدا می‌زنیم، نه loop بی‌نهایت، برای تست سریع.
    prune_storage_if_needed()
    _start_audio_recorder_if_needed()
    _start_browser_if_needed(flask_port=DEFAULT_HTTP_PORT)
    check_health_thresholds_and_maybe_reboot()
    print("✅ panel_monitor one-shot test OK (برای مانیتور واقعی، monitor_loop() را صدا بزن)")

