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
    # مقدار None یعنی داده موجود نیست / هنوز سنسور وصل نشده.
    return EnvSnapshot(
        ts_iso=now_iso,
        temp=None,
        hum=None,
        gas_v=None,
        gas_dv=None,
        gas_high=False,
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

