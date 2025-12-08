# panel_audio_local.py
"""
مدیریت ضبط صدا (tinycap) روی باکس سنسور / مرکزی.

وظایف:
  - تقسیم صدا به قطعات (segment) مثلاً ۳۰ ثانیه‌ای
  - ذخیره فایل‌ها در مسیر AUDIO_ROOT با ساختار پوشه‌ی روزانه
  - ثبت متادیتا در دیتابیس (audio_segments)
  - ارائه‌ی متد get_last_audio_segment برای خلاصه‌سازی به سمت مرکزی/سرور
"""

import os
import subprocess
import threading
import time
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from panel_paths import AUDIO_ROOT, SDCARD_ROOT, DB_PATH, ensure_dirs
from panel_db import store_audio_segment


_SEGMENT_SECONDS_DEFAULT = 30
_AUDIO_THREAD: Optional[threading.Thread] = None
_STOP_FLAG = False


def _which_su_env() -> Optional[str]:
    try:
        res = subprocess.run(["which", "su_env"], capture_output=True, text=True, timeout=1)
        if res.returncode == 0:
            p = res.stdout.strip()
            return p or None
    except Exception:
        pass
    return None


def _start_tinycap_segment(filepath: str, duration_sec: int):
    """
    یک tinycap برای مدت duration_sec روی filepath استارت می‌کند.
    تلاش می‌کند از su_env استفاده کند اگر موجود باشد.
    """
    ensure_dirs()
    su_env_path = _which_su_env()

    base_cmd = ["tinycap", filepath, "-r", "44100", "-b", "16", "-c", "2"]
    if su_env_path:
        cmd = [su_env_path] + base_cmd
    else:
        cmd = base_cmd

    print("[panel_audio_local] starting tinycap:", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd)
    except FileNotFoundError:
        print("[panel_audio_local] tinycap not found on this device.")
        return
    except Exception as e:
        print("[panel_audio_local] error starting tinycap:", e)
        return

    # منتظر می‌مانیم و بعد پروسه را قطع می‌کنیم
    try:
        time.sleep(duration_sec)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _audio_loop(sensor_id: str, segment_seconds: int):
    global _STOP_FLAG
    ensure_dirs()
    os.makedirs(AUDIO_ROOT, exist_ok=True)
    print(f"[panel_audio_local] audio loop started for {sensor_id}, segment={segment_seconds}s")

    while not _STOP_FLAG:
        start_ts = datetime.now(timezone.utc)
        day_dir = os.path.join(AUDIO_ROOT, start_ts.strftime("%Y%m%d"))
        os.makedirs(day_dir, exist_ok=True)

        fname = f"{sensor_id}_{start_ts.strftime('%H%M%S')}.wav"
        fpath = os.path.join(day_dir, fname)

        _start_tinycap_segment(fpath, segment_seconds)

        ts_iso = start_ts.isoformat()
        duration_sec = float(segment_seconds)
        try:
            store_audio_segment(sensor_id, ts_iso, fpath, duration_sec, label=None)
        except Exception as e:
            print("[panel_audio_local] store_audio_segment error:", e)


def start_audio_capture(sensor_id: str, segment_seconds: int = _SEGMENT_SECONDS_DEFAULT):
    """
    شروع اجرای حلقه‌ی ضبط صدا در یک ترد daemon.
    """
    global _AUDIO_THREAD, _STOP_FLAG
    if _AUDIO_THREAD is not None:
        return
    _STOP_FLAG = False
    t = threading.Thread(target=_audio_loop, args=(sensor_id, segment_seconds), daemon=True)
    _AUDIO_THREAD = t
    t.start()


def stop_audio_capture():
    global _STOP_FLAG
    _STOP_FLAG = True


def get_last_audio_segment(sensor_id: str) -> Optional[Dict[str, Any]]:
    """
    از جدول audio_segments آخرین segment مربوط به sensor_id را برمی‌گرداند.
    خروجی:
      {
        "id": int,
        "sensor_id": str,
        "ts": str,
        "filepath": str,
        "duration_sec": float,
        "label": Optional[str],
      }
    """
    if not os.path.exists(DB_PATH):
        return None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, sensor_id, ts, filepath, duration_sec, label "
        "FROM audio_segments WHERE sensor_id = ? ORDER BY ts DESC LIMIT 1",
        (sensor_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "sensor_id": row[1],
        "ts": row[2],
        "filepath": row[3],
        "duration_sec": row[4],
        "label": row[5],
    }


def list_audio_segments(sensor_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    لیست تعدادی از segmentهای اخیر.
    """
    if not os.path.exists(DB_PATH):
        return []

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, sensor_id, ts, filepath, duration_sec, label "
        "FROM audio_segments WHERE sensor_id = ? ORDER BY ts DESC LIMIT ?",
        (sensor_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    result: List[Dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "id": row[0],
                "sensor_id": row[1],
                "ts": row[2],
                "filepath": row[3],
                "duration_sec": row[4],
                "label": row[5],
            }
        )
    return result


if __name__ == "__main__":
    print("=== panel_audio_local test ===")
    ensure_dirs()
    sid = "TEST_SENSOR_AUDIO"
    print("starting one short segment (5 seconds)...")
    start_audio_capture(sid, segment_seconds=5)
    # کمی صبر می‌کنیم تا حداقل یک segment ثبت شود
    time.sleep(7)
    stop_audio_capture()
    last = get_last_audio_segment(sid)
    print("last audio segment:", last)
    print("✅ panel_audio_local basic test (بدون tinycap واقعی فقط ساختار) OK")

