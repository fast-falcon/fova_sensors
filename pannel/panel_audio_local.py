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
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from panel_db import store_audio_segment
from panel_paths import AUDIO_ROOT, DB_PATH, ensure_dirs


# ساختار ضبط صدا طبق base_record.py که به صورت دستی تست شده است.
import base_record as _br


_SEGMENT_SECONDS_DEFAULT = 30
_AUDIO_THREAD: Optional[threading.Thread] = None
_STOP_FLAG = False

# ---------- تنظیمات کارت و فایل ----------
_CARD = _br.CARD
_DEVICE = _br.DEVICE

_RAW_FILENAME = _br.RAW_FILENAME
_FINAL_FILENAME = _br.FINAL_FILENAME

_NATIVE_RATE = _br.NATIVE_RATE
_NATIVE_CHANNELS = _br.NATIVE_CHANNELS
_SAMPLE_WIDTH = _br.SAMPLE_WIDTH

_TARGET_RATE = _br.TARGET_RATE
_TARGET_CHANNELS = _br.TARGET_CHANNELS

_HIGHPASS_CUTOFF_HZ = _br.HIGHPASS_CUTOFF_HZ
_SILENCE_RMS_THRESHOLD = _br.SILENCE_RMS_THRESHOLD
_SILENCE_WINDOW_MS = _br.SILENCE_WINDOW_MS


def _configure_mixer():
    """
    تنظیم خودکار میکسر کارت 0 برای tinycap.
    """

    try:
        _br.configure_mixer()
        print("[panel_audio_local] mixer configured (base_record)")
    except Exception as e:
        print("[panel_audio_local] mix config error:", e)


def _start_tinycap_segment(filepath: str, duration_sec: int) -> bool:
    """
    اجرای ضبط tinycap به مدت duration_sec ثانیه، و سپس terminate/kill.

    چرا این مدل؟
      - روی بعضی buildهای tinycap، آپشن -T یا درست کار نمی‌کند یا در حالت device-busy
        باعث گیرکردن می‌شود.
      - اگر مانیتور/پروسس دیگری tinycap را بالا آورده باشد، tinycap ممکن است بلاک شود
        و فایل 0-byte بسازد.
    """

    ensure_dirs()

    # بدون -T: خودمان بعد از duration_sec پروسه را می‌بندیم (مثل base_record.py)
    cmd = [
        "su_env",
        "tinycap",
        filepath,
        "-D",
        _CARD,
        "-d",
        _DEVICE,
        "-r",
        str(_NATIVE_RATE),
        "-b",
        "16",
        "-c",
        str(_NATIVE_CHANNELS),
    ]

    print("[panel_audio_local] starting tinycap:", " ".join(cmd), f"(duration={duration_sec}s)")

    # خروجی tinycap را برای دیباگ نگه می‌داریم (آخرین چند خط)
    tail_lines: List[str] = []

    def _tail_reader(p):
        try:
            if p.stdout is None:
                return
            for line in p.stdout:
                line = line.rstrip("\n")
                if line:
                    tail_lines.append(line)
                    # جلوگیری از رشد بی‌نهایت
                    if len(tail_lines) > 80:
                        del tail_lines[:40]
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # setsid -> بتوانیم کل گروه را kill کنیم
        )
    except FileNotFoundError:
        print("[panel_audio_local] tinycap not found on this device.")
        return False
    except Exception as e:
        print("[panel_audio_local] error starting tinycap:", e)
        return False

    t = threading.Thread(target=_tail_reader, args=(proc,), daemon=True)
    t.start()

    # صبر تا پایان segment (یا STOP_FLAG/exit زودتر)
    deadline = time.time() + max(1, int(duration_sec))
    while time.time() < deadline and not _STOP_FLAG:
        if proc.poll() is not None:
            break
        time.sleep(0.2)

    # اگر هنوز زنده است، با SIGTERM کل گروه را می‌بندیم
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=3)
            except Exception:
                pass

    rc = proc.returncode
    size_bytes = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    print(f"[panel_audio_local] tinycap finished (target={duration_sec}s, returncode={rc}, size={size_bytes} bytes)")

    # اگر فایل خیلی کوچک/خالی است، خروجی tinycap را چاپ کن (برای علت‌یابی)
    if size_bytes <= 44:
        if tail_lines:
            print("[panel_audio_local] tinycap output tail:\n  - " + "\n  - ".join(tail_lines[-20:]))
        else:
            print("[panel_audio_local] tinycap produced empty file and no stdout captured.")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
        return False

    return True

def _process_audio(raw_path: str, final_path: str) -> bool:
    if not os.path.exists(raw_path):
        print(f"[panel_audio_local] raw file missing, skipping: {raw_path}")
        return False

    raw_size = os.path.getsize(raw_path)
    if raw_size <= 44:
        print(f"[panel_audio_local] raw too small ({raw_size} bytes), skipping processing")
        try:
            os.remove(raw_path)
        except FileNotFoundError:
            pass
        return False

    print(f"[panel_audio_local] raw size before header fix: {raw_size} bytes")
    _br.fix_wav_header(raw_path, _NATIVE_CHANNELS, _NATIVE_RATE, _SAMPLE_WIDTH)
    _br.make_small_wav(
        raw_path,
        final_path,
        target_rate=_TARGET_RATE,
        target_channels=_TARGET_CHANNELS,
    )
    try:
        os.remove(raw_path)
        print(f"[panel_audio_local] removed raw file {raw_path}")
    except FileNotFoundError:
        pass
    return os.path.exists(final_path)


def _audio_loop(sensor_id: str, segment_seconds: int):
    global _STOP_FLAG, _AUDIO_THREAD
    ensure_dirs()
    os.makedirs(AUDIO_ROOT, exist_ok=True)
    print(
        f"[panel_audio_local] audio loop started for {sensor_id}, segment={segment_seconds}s (STOP_FLAG aware)"
    )

    try:
        _configure_mixer()
    except Exception as e:
        print("[panel_audio_local] mixer config error:", e)

    while not _STOP_FLAG:
        start_ts = datetime.now(timezone.utc)
        day_dir = os.path.join(AUDIO_ROOT, start_ts.strftime("%Y%m%d"))
        os.makedirs(day_dir, exist_ok=True)

        base_name = f"{sensor_id}_{start_ts.strftime('%H%M%S')}"
        raw_path = os.path.join(day_dir, f"{base_name}_{_RAW_FILENAME}")
        final_path = os.path.join(day_dir, f"{base_name}_{_FINAL_FILENAME}")

        if not _start_tinycap_segment(raw_path, segment_seconds):
            print("[panel_audio_local] tinycap did not start; sleeping a bit before retry")
            time.sleep(2.0)
            continue

        processed_ok = _process_audio(raw_path, final_path)
        if not processed_ok:
            print("[panel_audio_local] post-processing failed; skipping metadata save")
            if not _STOP_FLAG:
                time.sleep(1.0)
            continue

        ts_iso = start_ts.isoformat()
        duration_sec = float(segment_seconds)
        try:
            store_audio_segment(sensor_id, ts_iso, final_path, duration_sec, label=None)
            print(
                f"[panel_audio_local] segment saved → {final_path} (duration={duration_sec}s, ts={ts_iso})"
            )
        except Exception as e:
            print("[panel_audio_local] store_audio_segment error:", e)

        if _STOP_FLAG:
            print("[panel_audio_local] STOP_FLAG set; processed last segment before exit")
            break

    print("[panel_audio_local] audio loop stopping; STOP_FLAG set")
    _AUDIO_THREAD = None


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

