# panel_paths.py
"""
مسیرها و ثابت‌های مشترک برای کل سیستم.
"""

import os

# ریشه‌ی کدها (همین جایی که فایل‌ها هستند)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ریشه‌ی ذخیره‌سازی روی sdcard
# اگر sdcard نباشد، روی BASE_DIR fallback می‌کنیم (برای تست روی PC)
_SDCARD_DEFAULT = "/sdcard/panel"
SDCARD_ROOT = _SDCARD_DEFAULT if os.path.isdir("/sdcard") else os.path.join(BASE_DIR, "sdcard_sim")

# مسیر فایل کانفیگ
CONFIG_PATH = os.path.join(BASE_DIR, "panel_config.json")

# مسیر دیتابیس اصلی
DB_PATH = os.path.join(SDCARD_ROOT, "panel_data.sqlite3")

# مسیر پوشه‌های صدا و سنسور
AUDIO_ROOT = os.path.join(SDCARD_ROOT, "audio")
SENSOR_LOG_ROOT = os.path.join(SDCARD_ROOT, "sensors")

# مسیر کلیدهای رمزنگاری (RSA)
CRYPTO_PRIV_KEY = os.path.join(SDCARD_ROOT, "crypto_private.pem")
CRYPTO_PUB_KEY = os.path.join(SDCARD_ROOT, "crypto_public.pem")

# پورت‌های پیش‌فرض
DEFAULT_HTTP_PORT = 8080
DEFAULT_SERVER_SOCKET_PORT = 9000
DEFAULT_SERVER_HTTP_PORT = 8000

# مدت زمانی که اگر سنسور ازش داده نیامد، offline فرض شود (ثانیه)
SENSOR_OFFLINE_SECONDS = 60.0

# حداقل فضای خالی روی sdcard بر حسب بایت (۱ گیگ)
MIN_FREE_SPACE_BYTES = 1 * 1024 * 1024 * 1024


def ensure_dirs():
    """
    پوشه‌های لازم را می‌سازد (اگر نباشند).
    """
    for d in [SDCARD_ROOT, AUDIO_ROOT, SENSOR_LOG_ROOT]:
        os.makedirs(d, exist_ok=True)


if __name__ == "__main__":
    print("=== panel_paths test ===")
    print("BASE_DIR:", BASE_DIR)
    print("SDCARD_ROOT:", SDCARD_ROOT)
    print("CONFIG_PATH:", CONFIG_PATH)
    print("DB_PATH:", DB_PATH)
    print("CRYPTO_PRIV_KEY:", CRYPTO_PRIV_KEY)
    ensure_dirs()
    print("✅ ensure_dirs() اجرا شد.")

