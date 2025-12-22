# central_sensors_link.py
"""
مدیریت وضعیت سنسورهای زیرمجموعه‌ی باکس مرکزی.

این ماژول:
  - وضعیت هر سنسور را در حافظه نگه می‌دارد (env, audio_summary, health, ip, last_seen, auth)
  - push_interval هر سنسور را در kv_store ذخیره می‌کند
  - برای central_server_link داده‌ی تجمیع‌شده آماده می‌کند
"""

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from panel_db import kv_get, kv_set, store_sensor_sample
from panel_paths import SENSOR_OFFLINE_SECONDS

DEFAULT_SENSOR_PUSH_INTERVAL = 2.0  # ثانیه
_KV_PUSH_PREFIX = "sensor_push_interval:"
_KV_AUTH_PREFIX = "central:sensor_auth:"


@dataclass
class SensorState:
    sensor_id: str
    sensor_name: str
    ip: Optional[str]
    last_seen: str             # ISO
    env: Dict[str, Any]
    audio_summary: Dict[str, Any]
    health: Dict[str, Any]
    auth_user: Optional[str]
    auth_pass: Optional[str]


_SENSORS: Dict[str, SensorState] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_sensor_state_from_payload(payload: Dict[str, Any], remote_ip: Optional[str]) -> SensorState:
    """
    از payload decrypt شده‌ی sensor_to_central، وضعیت سنسور را آپدیت می‌کند.

    payload مثال:
      {
        "type": "sensor_samples",
        "sensor_id": ...,
        "sensor_name": ...,
        "ts": ...,
        "env_ts": ...,
        "env": {...},
        "audio_summary": {...},
        "health": {...},
        "auth_user": "...",
        "auth_pass": "..."
      }
    """
    sensor_id = payload.get("sensor_id") or "UNKNOWN"
    sensor_name = payload.get("sensor_name") or sensor_id
    env = payload.get("env") or {}
    audio_summary = payload.get("audio_summary") or {}
    health = payload.get("health") or {}
    auth_user = payload.get("auth_user") or None
    auth_pass = payload.get("auth_pass") or None
    env_ts = payload.get("env_ts") or payload.get("ts") or _now_iso()

    now_iso = _now_iso()

    # در DB لوکال لاگ سنسور را هم ذخیره می‌کنیم
    try:
        store_sensor_sample(sensor_id, env_ts, env)
    except Exception as e:
        print("[central_sensors_link] store_sensor_sample error:", e)

    st = SensorState(
        sensor_id=sensor_id,
        sensor_name=sensor_name,
        ip=remote_ip,
        last_seen=now_iso,
        env=env,
        audio_summary=audio_summary,
        health=health,
        auth_user=auth_user,
        auth_pass=auth_pass,
    )
    _SENSORS[sensor_id] = st

    # auth را در kv_store هم ذخیره می‌کنیم
    if auth_user and auth_pass:
        kv_set(_KV_AUTH_PREFIX + sensor_id, f"{auth_user}:{auth_pass}")

    return st


def get_sensor_state(sensor_id: str) -> Optional[SensorState]:
    return _SENSORS.get(sensor_id)


def get_sensors_states() -> List[SensorState]:
    return list(_SENSORS.values())


def has_auth(sensor_id: str) -> bool:
    v = kv_get(_KV_AUTH_PREFIX + sensor_id, None)
    return v is not None


def validate_auth(sensor_id: str, user: str, password: str) -> bool:
    v = kv_get(_KV_AUTH_PREFIX + sensor_id, None)
    if not v:
        return False
    try:
        u, p = v.split(":", 1)
    except ValueError:
        return False
    return (u == user) and (p == password)


def set_auth(sensor_id: str, user: str, password: str) -> None:
    kv_set(_KV_AUTH_PREFIX + sensor_id, f"{user}:{password}")


def get_sensor_push_interval(sensor_id: str) -> float:
    """
    push_interval هر سنسور را از kv_store می‌گیرد؛ اگر نبود، مقدار پیش‌فرض.
    """
    v = kv_get(_KV_PUSH_PREFIX + sensor_id, None)
    if not v:
        return DEFAULT_SENSOR_PUSH_INTERVAL
    try:
        return float(v)
    except Exception:
        return DEFAULT_SENSOR_PUSH_INTERVAL


def set_sensor_push_interval(sensor_id: str, interval: float) -> None:
    kv_set(_KV_PUSH_PREFIX + sensor_id, str(interval))


def get_sensors_for_server_payload() -> List[Dict[str, Any]]:
    """
    برای ارسال به سرور اصلی، خلاصه‌ای از وضعیت سنسورها را برمی‌گرداند.
    """
    out: List[Dict[str, Any]] = []
    for st in _SENSORS.values():
        out.append(
            {
                "sensor_id": st.sensor_id,
                "sensor_name": st.sensor_name,
                "ip": st.ip,
                "last_seen": st.last_seen,
                "env": st.env,
                "audio_summary": st.audio_summary,
                "health": st.health,
                # TODO: می‌توان ssh_pub_key سنسور را هم اینجا اضافه کرد
            }
        )
    return out


def compute_state_label(st: SensorState) -> str:
    """
    یک state ساده برمی‌گرداند: "offline" یا "online".
    (می‌توان بعداً آن را به normal/warning/alert بر اساس health/env/پاسخ سرور گسترش داد.)
    """
    try:
        last = datetime.fromisoformat(st.last_seen)
        diff = datetime.now(timezone.utc) - last
        if diff.total_seconds() > SENSOR_OFFLINE_SECONDS:
            return "offline"
    except Exception:
        return "unknown"
    return "online"


if __name__ == "__main__":
    print("=== central_sensors_link test ===")
    test_payload = {
        "type": "sensor_samples",
        "sensor_id": "S1",
        "sensor_name": "Sensor one",
        "ts": _now_iso(),
        "env_ts": _now_iso(),
        "env": {"temp": 25.0, "hum": 40.0, "gas_v": 0.1, "gas_dv": 0.01, "gas_high": False},
        "audio_summary": {"last_segment": {"id": 1, "ts": _now_iso(), "duration_sec": 30.0, "label": None}},
        "health": {"cpu_percent": 10.0, "mem_percent": 20.0, "disk_percent": 30.0, "inode_percent": 40.0, "last_issue": None, "ts": _now_iso()},
        "auth_user": "u1",
        "auth_pass": "p1",
    }
    st = update_sensor_state_from_payload(test_payload, "192.168.1.10")
    print("SensorState:", st)
    print("compute_state_label:", compute_state_label(st))
    print("get_sensors_for_server_payload:", get_sensors_for_server_payload())
    set_sensor_push_interval("S1", 5.5)
    print("push_interval S1:", get_sensor_push_interval("S1"))
    set_auth("S1", "u1", "p1")
    print("validate_auth:", validate_auth("S1", "u1", "p1"))
    print("✅ central_sensors_link basic test OK")
