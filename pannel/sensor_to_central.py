# sensor_to_central.py
"""
رابط سازگاری برای شروع/توقف uplink به باکس مرکزی.
منطق اصلی در uplink_service.py قرار دارد تا قابل غیرفعال‌سازی یا توسعه‌ی مجزا باشد.
"""

from uplink_service import start_uplink, stop_uplink


if __name__ == "__main__":
    print("=== sensor_to_central (wrapper) test ===")
    start_uplink()
    print("uplink thread started (wrapper). stopping soon...")
    import time

    time.sleep(2)
    stop_uplink()
    print("✅ sensor_to_central wrapper OK")
