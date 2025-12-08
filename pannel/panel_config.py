# panel_config.py
"""
مدیریت فایل کانفیگ panel_config.json
"""

import json
import os
import threading
import uuid
import secrets
from typing import Any, Dict, Optional

from panel_paths import CONFIG_PATH

_CONFIG_LOCK = threading.RLock()
_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def _gen_box_id() -> str:
    """
    تولید شناسه‌ی غیرقابل حدس برای هر باکس.
    """
    return "bx_" + uuid.uuid4().hex + "_" + secrets.token_urlsafe(8)


def _normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    مطمئن می‌شود فیلدهای پایه مثل box_id وجود دارند.
    """
    if "box_id" not in cfg or not cfg["box_id"]:
        cfg["box_id"] = _gen_box_id()
    if "role" not in cfg:
        cfg["role"] = None
    if "central" not in cfg:
        cfg["central"] = None
    if "sensor" not in cfg:
        cfg["sensor"] = None
    return cfg


def load_config() -> Optional[Dict[str, Any]]:
    """
    کانفیگ را از دیسک می‌خواند. اگر وجود نداشته باشد، None برمی‌گرداند.
    """
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is not None:
            return _CONFIG_CACHE

        if not os.path.exists(CONFIG_PATH):
            return None
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print("[panel_config] load_config error:", e)
            return None

        cfg = _normalize_config(cfg)
        _CONFIG_CACHE = cfg
        return cfg


def save_config(cfg: Optional[Dict[str, Any]] = None) -> None:
    """
    cfg را در CONFIG_PATH ذخیره می‌کند. اگر None باشد، از کش استفاده می‌کند.
    """
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        if cfg is None:
            cfg = _CONFIG_CACHE
        if cfg is None:
            return
        cfg = _normalize_config(cfg)
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("[panel_config] save_config error:", e)
            return
        _CONFIG_CACHE = cfg


def set_config(cfg: Dict[str, Any]) -> None:
    """
    کانفیگ را در کش تنظیم می‌کند (و می‌توان بعداً با save_config ذخیره کرد).
    """
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        _CONFIG_CACHE = _normalize_config(cfg)


def get_config() -> Optional[Dict[str, Any]]:
    """
    کش را برمی‌گرداند؛ اگر خالی بود، از روی دیسک بارگذاری می‌کند.
    """
    cfg = load_config()
    return cfg


def get_role(cfg: Optional[Dict[str, Any]]) -> Optional[str]:
    if not cfg:
        return None
    role = cfg.get("role")
    if role in ("central", "sensor"):
        return role
    return None


def get_box_id(cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if cfg is None:
        cfg = get_config()
    if not cfg:
        return None
    return cfg.get("box_id")


def get_central_config(cfg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cfg:
        return None
    return cfg.get("central") or None


def get_sensor_config(cfg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cfg:
        return None
    return cfg.get("sensor") or None


if __name__ == "__main__":
    print("=== panel_config test ===")
    from panel_paths import CONFIG_PATH
    print("CONFIG_PATH:", CONFIG_PATH)

    cfg = load_config()
    if cfg is None:
        print("هیچ کانفیگی نبود، ساخت کانفیگ تست...")
        cfg = {
            "role": "sensor",
            "central": None,
            "sensor": {"sensor_name": "TEST_SENSOR"},
        }
        set_config(cfg)
        save_config()
        cfg = load_config()

    print("Loaded config:", cfg)
    print("role:", get_role(cfg))
    print("box_id:", get_box_id(cfg))
    print("✅ panel_config OK")

