# panel_audio_local.py
"""
مدیریت ضبط صدا (tinycap) روی باکس سنسور / مرکزی.

وظایف:
  - تقسیم صدا به قطعات (segment) مثلاً ۳۰ ثانیه‌ای
  - ذخیره فایل‌ها در مسیر AUDIO_ROOT با ساختار پوشه‌ی روزانه
  - ثبت متادیتا در دیتابیس (audio_segments)
  - ارائه‌ی متد get_last_audio_segment برای خلاصه‌سازی به سمت مرکزی/سرور
"""

import audioop
import os
import signal
import sqlite3
import subprocess
import threading
import time
import wave
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from panel_db import store_audio_segment
from panel_paths import AUDIO_ROOT, DB_PATH, ensure_dirs


_SEGMENT_SECONDS_DEFAULT = 30
_AUDIO_THREAD: Optional[threading.Thread] = None
_STOP_FLAG = False

# ---------- تنظیمات کارت و فایل ----------
_CARD = "0"
_DEVICE = "0"

_RAW_FILENAME = "usb2_raw.wav"
_FINAL_FILENAME = "usb2.wav"

_NATIVE_RATE = 48000
_NATIVE_CHANNELS = 2
_SAMPLE_WIDTH = 2

_TARGET_RATE = 16000
_TARGET_CHANNELS = 1

_HIGHPASS_CUTOFF_HZ = 100.0
_SILENCE_RMS_THRESHOLD = 200
_SILENCE_WINDOW_MS = 10


def _which_su_env() -> Optional[str]:
    try:
        res = subprocess.run(["which", "su_env"], capture_output=True, text=True, timeout=1)
        if res.returncode == 0:
            p = res.stdout.strip()
            return p or None
    except Exception:
        pass
    return None


def _configure_mixer():
    """
    تنظیم خودکار میکسر کارت 0 برای tinycap.
    """

    def run_mix(args: List[str]):
        cmd = ["su_env", "tinymix", "-D", _CARD] + args
        print("[panel_audio_local]", " ".join(cmd))
        try:
            subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            print("[panel_audio_local] mix error:", e)

    run_mix(["0", "63"])
    run_mix(["2", "1"])
    run_mix(["29", "2"])
    print("[panel_audio_local] mixer configured for capture on card 0")


def _fix_wav_header(path: str, channels: int, rate: int, sample_width: int):
    if not os.path.exists(path):
        print(f"[panel_audio_local] raw file missing, skip header fix: {path}")
        return

    with open(path, "rb") as f:
        data = f.read()

    if len(data) <= 44:
        print(f"[panel_audio_local] raw too small ({len(data)} bytes), skip header fix")
        return

    raw_pcm = data[44:]
    tmp_path = path + ".fixed"

    print(
        f"[panel_audio_local] fixing WAV header for {path} (len(raw)={len(raw_pcm)} bytes)"
    )
    with wave.open(tmp_path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(rate)
        w.writeframes(raw_pcm)

    os.replace(tmp_path, path)
    print(f"[panel_audio_local] header fixed → {path}")


def _highpass_filter(pcm: bytes, sampwidth: int, rate: int, cutoff_hz: float) -> bytes:
    if sampwidth != 2:
        return pcm

    import math
    import array

    length = len(pcm)
    num_samples = length // 2
    x = array.array("h")
    x.frombytes(pcm)

    dt = 1.0 / float(rate)
    rc = 1.0 / (2.0 * math.pi * float(cutoff_hz))
    alpha = dt / (rc + dt)

    low_prev = 0.0
    y = array.array("h")

    for n in range(num_samples):
        xn = float(x[n])
        low = low_prev + alpha * (xn - low_prev)
        high = xn - low
        if high > 32767:
            high = 32767
        elif high < -32768:
            high = -32768
        y.append(int(high))
        low_prev = low

    return y.tobytes()


def _gate_true_silence(
    pcm: bytes,
    sampwidth: int,
    rate: int,
    rms_threshold: int,
    window_ms: int,
) -> bytes:
    if sampwidth != 2:
        return pcm

    frame_bytes = sampwidth
    window_samples = int(rate * window_ms / 1000.0)
    if window_samples <= 0:
        return pcm

    window_bytes = window_samples * frame_bytes
    length = len(pcm)
    out = bytearray(pcm)

    for start in range(0, length, window_bytes):
        end = min(start + window_bytes, length)
        chunk = pcm[start:end]
        if not chunk:
            break
        rms = audioop.rms(chunk, sampwidth)
        if rms < rms_threshold:
            out[start:end] = b"\x00" * (end - start)

    return bytes(out)


def _make_small_wav(path_in: str, path_out: str) -> None:
    if not os.path.exists(path_in):
        print(f"[panel_audio_local] File {path_in} does not exist, skipping small wav")
        return

    size = os.path.getsize(path_in)
    if size <= 44:
        print(f"[panel_audio_local] File {path_in} is empty ({size} bytes), skipping small wav")
        return

    with wave.open(path_in, "rb") as r:
        nch = r.getnchannels()
        sw = r.getsampwidth()
        fr = r.getframerate()
        nframes = r.getnframes()
        frames = r.readframes(nframes)

    print(
        f"[panel_audio_local] compressing {path_in}: {nch}ch,{fr}Hz -> {_TARGET_CHANNELS}ch,{_TARGET_RATE}Hz"
    )

    if nch == 2 and _TARGET_CHANNELS == 1:
        frames_mono = audioop.tomono(frames, sw, 0.5, 0.5)
        nch_in = 1
    else:
        frames_mono = frames
        nch_in = nch

    converted, _ = audioop.ratecv(
        frames_mono,
        sw,
        nch_in,
        fr,
        _TARGET_RATE,
        None,
    )

    hp = _highpass_filter(
        converted,
        sampwidth=sw,
        rate=_TARGET_RATE,
        cutoff_hz=_HIGHPASS_CUTOFF_HZ,
    )

    gated = _gate_true_silence(
        hp,
        sampwidth=sw,
        rate=_TARGET_RATE,
        rms_threshold=_SILENCE_RMS_THRESHOLD,
        window_ms=_SILENCE_WINDOW_MS,
    )

    with wave.open(path_out, "wb") as w:
        w.setnchannels(_TARGET_CHANNELS)
        w.setsampwidth(sw)
        w.setframerate(_TARGET_RATE)
        w.writeframes(gated)

    print(
        f"[panel_audio_local] Final wav written to {path_out} (16k/mono + high-pass + soft gate)"
    )


def _start_tinycap_segment(filepath: str, duration_sec: int) -> bool:
    """
    اجرای ضبط tinycap با قطع/وصل خودکار (بدون نیاز به ورودی کاربر).

    الهام‌گرفته از الگوی record.py: پروسس را با timeout کنترل می‌کنیم و در صورت گیرکردن
    به‌صورت تهاجمی kill می‌زنیم تا حلقه بتواند دوباره شروع کند.
    """

    ensure_dirs()
    su_env_path = _which_su_env()

    base_cmd = [
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

    # اگر tinycap خودش duration را رعایت نکرد، ما timeout می‌گذاریم
    timeout_sec = max(1.0, float(duration_sec)) + 1.0
    cmd = [su_env_path] + base_cmd if su_env_path else base_cmd

    print("[panel_audio_local] starting tinycap:", " ".join(cmd), f"(timeout={timeout_sec}s)")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("[panel_audio_local] tinycap not found on this device.")
        return False
    except Exception as e:
        print("[panel_audio_local] error starting tinycap:", e)
        return False

    try:
        # خروجی tinycap را می‌خوانیم ولی مهم‌تر اینکه timeout داشته باشیم
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            print("[panel_audio_local] tinycap timeout → terminating…")
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                print("[panel_audio_local] tinycap still alive → killing…")
                proc.kill()
        # اگر stop flag در میانه set شد، باز هم پروسس را می‌بندیم
        if _STOP_FLAG and proc.poll() is None:
            proc.terminate()
    finally:
        try:
            # خواندن باقیمانده stdout برای لاگ
            if proc.stdout:
                for line in proc.stdout.readlines():
                    if line:
                        print(line, end="")
        except Exception:
            pass

    rc = proc.returncode
    print(
        f"[panel_audio_local] tinycap segment finished (target={duration_sec}s, returncode={rc})"
    )
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

    _fix_wav_header(raw_path, _NATIVE_CHANNELS, _NATIVE_RATE, _SAMPLE_WIDTH)
    _make_small_wav(raw_path, final_path)
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

        if _STOP_FLAG:
            print("[panel_audio_local] STOP_FLAG set after capture, skipping processing")
            break

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

