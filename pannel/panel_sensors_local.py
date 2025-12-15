# panel_sensors_local.py
"""
خواندن سنسورهای محیطی روی خود باکس سنسور.

این ماژول یک لایه‌ی انتزاعی ساده می‌دهد:
  - EnvSnapshot: دما/رطوبت/گاز و ...
  - start_sensor_reader(sensor_id, interval_sec): اجرای یک لوپ که به صورت دوره‌ای سنسورها را می‌خواند
  - get_latest_env(): آخرین اندازه‌گیری (یا None)

نکته:
  اینجا من منطق سخت‌افزاری را ساده نگه داشته‌ام؛
  تو می‌توانی این ماژول را به اسکریپت واقعی‌ات (مثل sensor_lesten.py) وصل کنی.
"""

import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from panel_db import store_sensor_sample
from panel_paths import ensure_dirs


@dataclass
class EnvSnapshot:
    ts_iso: str
    temp: Optional[float]
    hum: Optional[float]
    gas_v: Optional[float]
    gas_dv: Optional[float]
    gas_high: bool


_LATEST_ENV: Optional[EnvSnapshot] = None
_READER_THREAD: Optional[threading.Thread] = None
_STOP_FLAG = False
_SENSOR_LESTEN_ENV_KEY = "SENSOR_LESTEN_PATH"
_SENSOR_LESTEN_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "sensor_lesten.py"),
    os.path.join(os.path.dirname(__file__), "..", "sensor_lesten.py"),
    "sensor_lesten.py",
]


def _parse_sensor_lesten_line(line: str) -> Optional[Dict[str, float]]:
    """پارسر ساده خروجی sensor_lesten.py برای استخراج دما/رطوبت/گاز."""

    temp_match = re.search(r"A1D1:\s*([-+]?\d+(?:\.\d+)?)°C\s*([-+]?\d+(?:\.\d+)?)%RH", line)
    gas_match = re.search(r"A1F1:\s*V=([-+]?\d+(?:\.\d+)?)\s*Δ=([+\-]?\d+(?:\.\d+)?)", line)
    if not temp_match and not gas_match:
        return None

    parsed: Dict[str, float] = {}
    if temp_match:
        parsed["temp"] = float(temp_match.group(1))
        parsed["hum"] = float(temp_match.group(2))
    if gas_match:
        parsed["gas_v"] = float(gas_match.group(1))
        parsed["gas_dv"] = float(gas_match.group(2))
    return parsed if parsed else None


def _find_sensor_lesten_path() -> Optional[str]:
    """اول از متغیر محیطی، بعد از مسیرهای معمول فایل sensor_lesten.py را پیدا می‌کند."""

    env_path = os.environ.get(_SENSOR_LESTEN_ENV_KEY)
    if env_path and os.path.exists(env_path):
        return env_path
    for candidate in _SENSOR_LESTEN_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None


def _read_sensor_lesten_subprocess() -> Dict[str, Optional[float]]:
    """sensor_lesten.py را اجرا می‌کند و اولین خط قابل پارس را برمی‌گرداند."""

    sensor_path = _find_sensor_lesten_path()
    if not sensor_path:
        raise FileNotFoundError(
            "sensor_lesten.py not found; set SENSOR_LESTEN_PATH or place it next to panel files"
        )

    proc = subprocess.Popen(
        [sys.executable, sensor_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    deadline = time.time() + 5.0
    parsed: Dict[str, Optional[float]] = {"temp": None, "hum": None, "gas_v": None, "gas_dv": None}
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            values = _parse_sensor_lesten_line(line)
            if values:
                parsed.update({k: values.get(k, parsed.get(k)) for k in parsed.keys()})
                if parsed["temp"] is not None or parsed["gas_v"] is not None:
                    break
            if time.time() > deadline:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()

    if parsed["temp"] is None and parsed["gas_v"] is None:
        stderr = ""
        if proc.stderr:
            stderr = proc.stderr.read().strip()
        raise RuntimeError(f"sensor_lesten produced no data. stderr: {stderr}")
    return parsed


def _read_env_hardware() -> EnvSnapshot:
    """
    این تابع قرار است واقعا از سنسورها بخواند.
    فعلا به صورت placeholder است تا تو بتوانی آن را با منطق خودت (مثلاً sensor_lesten) پر کنی.

    TODO:
      اگر خواستی، می‌توانی اینجا sensor_lesten را به صورت subprocess اجرا کنی
      و آخرین ورودی stdout آن را پارس کنی، یا یک API ساده داخلش اضافه کنی
      که یک snapshot JSON برگرداند.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    readings = _read_sensor_lesten_subprocess()
    gas_dv = readings.get("gas_dv")
    gas_high = bool(gas_dv is not None and gas_dv > 0.0)
    return EnvSnapshot(
        ts_iso=now_iso,
        temp=readings.get("temp"),
        hum=readings.get("hum"),
        gas_v=readings.get("gas_v"),
        gas_dv=gas_dv,
        gas_high=gas_high,
    )


def get_latest_env() -> Optional[EnvSnapshot]:
    """
    آخرین EnvSnapshot خوانده‌شده توسط ترد reader را برمی‌گرداند.
    """
    return _LATEST_ENV


def _sensor_reader_loop(sensor_id: str, interval_sec: float):
    global _LATEST_ENV, _STOP_FLAG
    ensure_dirs()
    while not _STOP_FLAG:
        try:
            snap = _read_env_hardware()
            _LATEST_ENV = snap
            data: Dict[str, Any] = {
                "temp": snap.temp,
                "hum": snap.hum,
                "gas_v": snap.gas_v,
                "gas_dv": snap.gas_dv,
                "gas_high": snap.gas_high,
            }
            store_sensor_sample(sensor_id, snap.ts_iso, data)
        except Exception as e:
            # در صورت خطا، فقط لاگ چاپ می‌کنیم و ادامه می‌دهیم
            print("[panel_sensors_local] error:", e)
        time.sleep(interval_sec)


def start_sensor_reader(sensor_id: str, interval_sec: float = 5.0):
    """
    یک ترد daemon راه می‌اندازد که به صورت دوره‌ای سنسورها را می‌خواند.
    """
    global _READER_THREAD, _STOP_FLAG
    if _READER_THREAD is not None:
        return
    _STOP_FLAG = False
    t = threading.Thread(target=_sensor_reader_loop, args=(sensor_id, interval_sec), daemon=True)
    _READER_THREAD = t
    t.start()


def stop_sensor_reader():
    """
    برای تست/خاموش کردن ترد reader.
    """
    global _STOP_FLAG
    _STOP_FLAG = True


if __name__ == "__main__":
    print("=== panel_sensors_local test ===")
    ensure_dirs()
    # برای تست، یک sensor_id فرضی استفاده می‌کنیم
    sid = "TEST_SENSOR_BOX"
    start_sensor_reader(sid, interval_sec=2.0)
    print("sensor_reader started for", sid)
    for i in range(3):
        time.sleep(2.1)
        snap = get_latest_env()
        print("latest env:", snap)
    stop_sensor_reader()
    print("✅ panel_sensors_local basic test OK")

