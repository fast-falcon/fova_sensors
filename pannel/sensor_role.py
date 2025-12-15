# sensor_role.py
"""
نقش کامل باکس سنسور:

- راه‌اندازی دیتابیس و پوشه‌ها
- ساخت/تایید کلیدهای RSA (panel_crypto) و SSH (panel_ssh)
- شروع خواندن سنسورها (panel_sensors_local)
- شروع ضبط صدا (panel_audio_local)
- شروع uplink به باکس مرکزی (sensor_to_central)
- شروع مانیتورینگ (panel_monitor)
- بالا آوردن Flask پنل سنسور (sensor_api)

این فایل فقط glue است.
"""

import threading

from panel_paths import DEFAULT_HTTP_PORT, ensure_dirs
from panel_db import init_db
from panel_config import get_box_id, get_config, get_sensor_config, get_audio_segment_seconds
from panel_crypto import ensure_box_keypair
from panel_ssh import ensure_ssh_key

from panel_sensors_local import start_sensor_reader
from panel_audio_local import start_audio_capture
from panel_monitor import monitor_loop
from uplink_service import start_uplink

import sensor_api


def run_sensor():
    """
    entrypoint اصلی نقش سنسور.
    """
    ensure_dirs()
    init_db()

    cfg = get_config()
    if not cfg or cfg.get("role") != "sensor":
        print("[sensor_role] config not found or role != sensor. abort.")
        return

    # کلیدهای امنیتی
    ensure_box_keypair()
    ensure_ssh_key()

    box_id = get_box_id(cfg) or "UNKNOWN_SENSOR"
    sensor_cfg = get_sensor_config(cfg) or {}
    sensor_name = sensor_cfg.get("sensor_name") or box_id
    online_enabled = sensor_cfg.get("online_enabled", True)

    print(f"[sensor_role] starting as SENSOR: {sensor_name} ({box_id})")

    # شروع ترد مانیتورینگ
    t_monitor = threading.Thread(
        target=monitor_loop,
        kwargs={"flask_port": DEFAULT_HTTP_PORT},
        daemon=True,
    )
    t_monitor.start()
    print("[sensor_role] monitor_loop started")

    # شروع خواندن سنسورها
    # interval را فعلاً ثابت می‌گذاریم (مثلا ۵ ثانیه). می‌توانی بعداً از config یا server تنظیم کنی.
    start_sensor_reader(sensor_id=box_id, interval_sec=5.0)
    print("[sensor_role] sensor_reader started")

    # شروع ضبط صدا (segmentهای ۳۰ ثانیه‌ای)
    segment_seconds = get_audio_segment_seconds(cfg)
    start_audio_capture(sensor_id=box_id, segment_seconds=segment_seconds)
    print(
        f"[sensor_role] audio_capture started (segment={segment_seconds}s)"
    )

    # شروع uplink به مرکزی
    if online_enabled:
        start_uplink()
        print("[sensor_role] uplink to central started (online mode)")
    else:
        print("[sensor_role] online mode disabled → uplink thread not started")

    # در نهایت، Flask پنل را بالا می‌آوریم
    print(f"[sensor_role] starting Flask panel on port {DEFAULT_HTTP_PORT}")
    sensor_api.run_flask(host="0.0.0.0", port=DEFAULT_HTTP_PORT)


if __name__ == "__main__":
    print("=== sensor_role test ===")
    print("این تست فرض می‌کند panel_config.json با role='sensor' تنظیم شده باشد.")
    print("اگر نیست، ویزارد یا دستی آن را تنظیم کن.")
    run_sensor()

