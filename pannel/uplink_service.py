# uplink_service.py
"""
ارسال داده از باکس سنسور به باکس مرکزی + گرفتن تنظیمات از مرکزی.

منطق کلی:
  - در هر چرخه (push_interval ثانیه):
      - خواندن آخرین env (از panel_sensors_local)
      - خواندن خلاصه‌ی صدا (از panel_audio_local)
      - خواندن وضعیت سلامت (از panel_health)
      - ساخت payload
      - رمزنگاری با کلید عمومی مرکزی (RSA+AES-GCM)
      - ارسال به /api/sensor_push روی باکس مرکزی با Basic Auth
      - در صورت موفقیت، اگر مرکزی مقدار push_interval جدید بدهد، اعمال می‌کنیم.

  - کلید عمومی مرکزی از /api/public_key گرفته می‌شود و در kv_store کش می‌شود.
  - user/pass برای Basic Auth:
      اگر در config نبود، اینجا random تولید می‌شود، در config ذخیره می‌شود
      و در اولین payload نیز برای مرکزی ارسال می‌شود تا بداند.
"""

import base64
import json
import time
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from panel_config import get_config, get_sensor_config, save_config, get_box_id
from panel_paths import DEFAULT_HTTP_PORT
from panel_db import kv_get, kv_set
from panel_crypto import encrypt_with_public_key, encode_ciphertext
from panel_sensors_local import get_latest_env, EnvSnapshot
from panel_audio_local import get_last_audio_segment
from panel_health import get_health_status, update_health_status

DEFAULT_PUSH_INTERVAL = 10.0  # ثانیه
KV_CENTRAL_PUBKEY = "central:pubkey_pem"
KV_PUSH_INTERVAL = "sensor:push_interval"

_UPLINK_THREAD: Optional[threading.Thread] = None
_STOP_FLAG = False


def _http_get_json(url: str, timeout: float = 5.0) -> Dict[str, Any]:
    req = Request(url, method="GET")
    req.add_header("Accept", "application/json")
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def _http_post_json(
    url: str,
    obj: Dict[str, Any],
    auth_user: Optional[str],
    auth_pass: Optional[str],
    timeout: float = 5.0,
) -> Dict[str, Any]:
    body = json.dumps(obj).encode("utf-8")
    req = Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if auth_user and auth_pass:
        token = base64.b64encode(f"{auth_user}:{auth_pass}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", "Basic " + token)
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if not data:
        return {}
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return {}


def _ensure_auth_in_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    اگر auth_user/pass برای سنسور ست نشده باشد، اینجا تولید و ذخیره می‌کنیم.
    """
    import secrets

    sensor_cfg = cfg.get("sensor") or {}
    if not sensor_cfg.get("auth_user") or not sensor_cfg.get("auth_pass"):
        user = "sx_" + secrets.token_urlsafe(6)
        pw = secrets.token_urlsafe(16)
        sensor_cfg["auth_user"] = user
        sensor_cfg["auth_pass"] = pw
        cfg["sensor"] = sensor_cfg
        save_config(cfg)
        print("[uplink_service] generated BasicAuth for sensor:", user)
    return cfg


def _get_central_public_key(host: str, port: int) -> Optional[bytes]:
    """
    تلاش می‌کند کلید عمومی مرکزی را از kv_store بخواند
    و اگر نبود، از /api/public_key روی مرکزی می‌گیرد.
    """
    cached = kv_get(KV_CENTRAL_PUBKEY, None)
    if cached:
        return cached.encode("utf-8")

    url = f"http://{host}:{port}/api/public_key"
    try:
        data = _http_get_json(url)
    except Exception as e:
        print("[uplink_service] error getting central public key:", e)
        return None

    pem = data.get("public_key")
    if not pem:
        print("[uplink_service] central /api/public_key no public_key field")
        return None

    kv_set(KV_CENTRAL_PUBKEY, pem)
    return pem.encode("utf-8")


def _update_push_interval_from_central(
    host: str,
    port: int,
    sensor_id: str,
    auth_user: str,
    auth_pass: str,
) -> float:
    """
    از /api/sensor_config/<sensor_id> روی مرکزی push_interval را می‌گیرد.
    در صورت خطا، از kv_store یا مقدار پیش‌فرض استفاده می‌کند.
    """
    url = f"http://{host}:{port}/api/sensor_config/{sensor_id}"
    try:
        data = _http_get_json(url)
        interval = float(data.get("push_interval", DEFAULT_PUSH_INTERVAL))
        kv_set(KV_PUSH_INTERVAL, str(interval))
        return interval
    except Exception as e:
        print("[uplink_service] error getting push_interval:", e)
        cached = kv_get(KV_PUSH_INTERVAL, None)
        if cached:
            try:
                return float(cached)
            except Exception:
                pass
        return DEFAULT_PUSH_INTERVAL


def _build_payload(sensor_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    payload را از آخرین env/health/audio می‌سازد.
    """
    cfg = get_config() or {}
    box_id = get_box_id(cfg) or "UNKNOWN_SENSOR"
    sensor_name = sensor_cfg.get("sensor_name") or box_id

    # env
    snap: Optional[EnvSnapshot] = get_latest_env()
    if snap is None:
        now_iso = datetime.now(timezone.utc).isoformat()
        env = {
            "temp": None,
            "hum": None,
            "gas_v": None,
            "gas_dv": None,
            "gas_high": False,
        }
        ts_env = now_iso
    else:
        env = {
            "temp": snap.temp,
            "hum": snap.hum,
            "gas_v": snap.gas_v,
            "gas_dv": snap.gas_dv,
            "gas_high": snap.gas_high,
        }
        ts_env = snap.ts_iso

    # audio summary
    audio = get_last_audio_segment(box_id) or {}
    audio_summary: Dict[str, Any] = {}
    if audio:
        audio_summary = {
            "last_segment": {
                "id": audio.get("id"),
                "ts": audio.get("ts"),
                "duration_sec": audio.get("duration_sec"),
                "label": audio.get("label"),
            }
        }

    # health
    health = get_health_status()
    if health is None:
        health = update_health_status()
    health_dict = {
        "cpu_percent": health.cpu_percent,
        "mem_percent": health.mem_percent,
        "disk_percent": health.disk_percent,
        "inode_percent": health.inode_percent,
        "last_issue": health.last_issue,
        "ts": health.ts,
    }

    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "type": "sensor_samples",
        "sensor_id": box_id,
        "sensor_name": sensor_name,
        "ts": now_iso,
        "env_ts": ts_env,
        "env": env,
        "audio_summary": audio_summary,
        "health": health_dict,
        # برای اولین بار، مرکزی می‌تواند از این‌ها برای ثبت credentialها استفاده کند
        "auth_user": sensor_cfg.get("auth_user"),
        "auth_pass": sensor_cfg.get("auth_pass"),
    }
    return payload


def _uplink_loop():
    global _STOP_FLAG, _UPLINK_THREAD

    cfg = get_config() or {}
    if cfg.get("role") != "sensor":
        print("[uplink_service] role is not 'sensor' → exiting uplink loop")
        return
    sensor_cfg = get_sensor_config(cfg) or {}

    central_host = sensor_cfg.get("central_host") or "127.0.0.1"
    central_http_port = int(sensor_cfg.get("central_http_port") or DEFAULT_HTTP_PORT)

    cfg = _ensure_auth_in_config(cfg)
    sensor_cfg = cfg.get("sensor") or {}
    auth_user = sensor_cfg.get("auth_user")
    auth_pass = sensor_cfg.get("auth_pass")

    box_id = get_box_id(cfg) or "UNKNOWN_SENSOR"

    # اولین بار: تلاش برای دریافت push_interval از مرکزی
    push_interval = _update_push_interval_from_central(
        central_host, central_http_port, box_id, auth_user, auth_pass
    )

    print(
        f"[uplink_service] uplink loop started → central http://{central_host}:{central_http_port}, "
        f"sensor_id={box_id}, push_interval={push_interval}s"
    )

    while not _STOP_FLAG:
        try:
            pem = _get_central_public_key(central_host, central_http_port)
            if not pem:
                print("[uplink_service] no central public key, skip this round")
            else:
                payload = _build_payload(sensor_cfg)
                plaintext = json.dumps(payload).encode("utf-8")
                cipher = encrypt_with_public_key(pem, plaintext)
                cipher_b64 = encode_ciphertext(cipher)

                url = f"http://{central_host}:{central_http_port}/api/sensor_push"
                frame = {
                    "sensor_id": box_id,
                    "ciphertext": cipher_b64,
                }
                try:
                    resp = _http_post_json(url, frame, auth_user, auth_pass, timeout=10.0)
                    # اگر مرکزی مقدار جدید push_interval یا پیام خاصی برگرداند:
                    new_pi = resp.get("push_interval")
                    if new_pi is not None:
                        try:
                            push_interval = float(new_pi)
                            kv_set(KV_PUSH_INTERVAL, str(push_interval))
                            print("[uplink_service] push_interval updated to", push_interval)
                        except Exception:
                            pass
                except (URLError, HTTPError) as e:
                    print("[uplink_service] HTTP error:", e)
                except Exception as e:
                    print("[uplink_service] error posting to central:", e)
        except Exception as e:
            print("[uplink_service] loop exception:", e)

        time.sleep(push_interval)

    _UPLINK_THREAD = None


def start_uplink():
    """
    شروع ترد uplink.
    """
    global _UPLINK_THREAD, _STOP_FLAG
    if _UPLINK_THREAD is not None:
        return
    _STOP_FLAG = False
    t = threading.Thread(target=_uplink_loop, daemon=True)
    _UPLINK_THREAD = t
    t.start()


def stop_uplink():
    global _STOP_FLAG
    _STOP_FLAG = True


if __name__ == "__main__":
    print("=== uplink_service test (dry run) ===")
    # این تست فرض می‌کند فایل کانفیگ سنسور وجود دارد.
    cfg = get_config()
    print("config:", cfg)
    if not cfg or cfg.get("role") != "sensor":
        print("⚠️ نقش این باکس sensor نیست یا کانفیگ وجود ندارد؛ تست واقعی uplink ممکن نیست.")
    else:
        print("شروع ترد uplink برای چند ثانیه (بدون قطع):")
        start_uplink()
        # چند ثانیه صبر می‌کنیم
        time.sleep(5)
        stop_uplink()
        print("uplink stop called.")
    print("✅ uplink_service basic test (از نظر ساختار) OK")

