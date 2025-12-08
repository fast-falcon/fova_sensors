# panel_main.py
"""
entrypoint مشترک بین باکس مرکزی و باکس سنسور.

- اگر کانفیگ وجود داشته باشد:
    role = central  → run_central()
    role = sensor   → run_sensor()
- اگر کانفیگ نباشد:
    → ویزارد اولیه (panel_wizard.run_wizard)
"""

from panel_config import load_config, get_role
from central_role import run_central
from sensor_role import run_sensor
from panel_wizard import run_wizard


def main():
    cfg = load_config()
    role = get_role(cfg)

    if cfg is None or role not in ("central", "sensor"):
        print("[panel_main] no valid config found → starting wizard...")
        run_wizard()
        return

    if role == "central":
        print("[panel_main] starting as CENTRAL")
        run_central()
    else:
        print("[panel_main] starting as SENSOR")
        run_sensor()


if __name__ == "__main__":
    print("=== panel_main test ===")
    print("با توجه به وضعیت panel_config.json، یا ویزارد را اجرا می‌کند یا سرویس را.")
    main()

