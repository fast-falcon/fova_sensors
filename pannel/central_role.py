# central_role.py
"""
نقش کامل باکس مرکزی:

- آماده‌سازی پوشه‌ها و دیتابیس
- ساخت/تایید کلیدهای RSA (panel_crypto) و SSH (panel_ssh)
- شروع خواندن سنسور محیطی خود باکس (panel_sensors_local)
- شروع ضبط صدا (panel_audio_local)
- شروع uplink به سرور اصلی (central_server_link)
- شروع مانیتورینگ (panel_monitor)
- بالا آوردن Flask پنل مرکزی (central_api)

این فایل entrypoint نقش مرکزی است و توسط panel_main فراخوانی می‌شود.
"""

import threading

from panel_paths import ensure_dirs, DEFAULT_HTTP_PORT
from panel_db import init_db
from panel_config import get_config, get_audio_segment_seconds
from panel_crypto import ensure_box_keypair
from panel_ssh import ensure_ssh_key
from panel_config import get_box_id

from panel_sensors_local import start_sensor_reader
from panel_audio_local import start_audio_capture
from panel_monitor import monitor_loop
from central_server_link import start_uplink_to_server

import central_api


def run_central():
    """
    entrypoint نقش باکس مرکزی.
    """
    ensure_dirs()
    init_db()

    cfg = get_config()
    if not cfg or cfg.get("role") != "central":
        print("[central_role] config not found or role != central. abort.")
        return

    # کلیدهای امنیتی
    ensure_box_keypair()
    ensure_ssh_key()

    central_id = get_box_id(cfg) or "UNKNOWN_CENTRAL"

    print(f"[central_role] starting as CENTRAL: {central_id}")

    # ترد مانیتورینگ
    t_monitor = threading.Thread(
        target=monitor_loop,
        kwargs={"flask_port": DEFAULT_HTTP_PORT},
        daemon=True,
    )
    t_monitor.start()
    print("[central_role] monitor_loop started")

    # سنسور محیطی خود باکس (همان central_id)
    start_sensor_reader(sensor_id=central_id, interval_sec=5.0)
    print("[central_role] sensor_reader for central started")

    # ضبط صدا روی خود باکس
    segment_seconds = get_audio_segment_seconds(cfg)
    start_audio_capture(sensor_id=central_id, segment_seconds=segment_seconds)
    print(
        f"[central_role] audio_capture for central started (segment={segment_seconds}s)"
    )

    # uplink به سرور اصلی
    central_cfg = cfg.get("central") or {}
    if central_cfg.get("online_enabled", True):
        start_uplink_to_server()
        print("[central_role] uplink to main server started")
    else:
        print("[central_role] online mode disabled → uplink to server skipped")

    # در نهایت پنل مرکزی را بالا می‌آوریم
    print(f"[central_role] starting central Flask panel on port {DEFAULT_HTTP_PORT}")
    central_api.run_flask(host="0.0.0.0", port=DEFAULT_HTTP_PORT)


if __name__ == "__main__":
    print("=== central_role test ===")
    print("این تست فرض می‌کند panel_config.json با role='central' تنظیم شده باشد.")
    run_central()

