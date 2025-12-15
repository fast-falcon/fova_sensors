# central_api.py
"""
Flask پنل باکس مرکزی + API برای سنسورها.

UI:
  - GET /        → داشبورد مرکزی
  - GET /audio   → لیست صداهای باکس مرکزی و سنسورها

API برای سنسورها:
  - GET /api/public_key
  - POST /api/sensor_push   (Basic Auth)
  - GET /api/sensor_config/<sensor_id>   (push_interval)

API دیباگ:
  - GET /api/sensors_state
"""

import os
import base64
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from flask import (
    Flask,
    jsonify,
    request,
    render_template,
    Response,
)

from panel_paths import BASE_DIR, CRYPTO_PRIV_KEY, DEFAULT_HTTP_PORT, SENSOR_OFFLINE_SECONDS
from panel_config import get_config, get_central_config, get_box_id
from panel_crypto import ensure_box_keypair, load_box_public_key, decrypt_with_private_key
from panel_sensors_local import get_latest_env
from panel_audio_local import get_last_audio_segment, list_audio_segments
from panel_health import get_health_status
from panel_db import init_db, DB_PATH, get_latest_sensor_sample
from panel_net_common import parse_basic_auth_header
from central_sensors_link import (
    update_sensor_state_from_payload,
    get_sensors_states,
    get_sensor_push_interval,
    has_auth,
    validate_auth,
    set_auth,
    compute_state_label,
)

TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)


def _get_central_identity() -> Dict[str, Any]:
    cfg = get_config() or {}
    central_cfg = get_central_config(cfg) or {}
    box_id = get_box_id(cfg) or "UNKNOWN_CENTRAL"
    cluster_name = central_cfg.get("cluster_name") or box_id
    return {"box_id": box_id, "cluster_name": cluster_name}


def _has_env_data(env: Dict[str, Any]) -> bool:
    if not env:
        return False
    for k in ("temp", "hum", "gas_v", "gas_dv"):
        if env.get(k) is not None:
            return True
    return False


def _merge_env_with_db(env: Optional[Dict[str, Any]], sensor_id: str) -> Dict[str, Any]:
    """
    اگر env فعلی داده‌ای نداشت، آخرین مقدار DB را تزریق می‌کند تا داشبورد خالی نماند.
    """
    env = env or {}
    if _has_env_data(env):
        return env

    fallback = get_latest_sensor_sample(sensor_id)
    if not fallback:
        return env

    ts_iso, data = fallback
    merged = {
        "ts": env.get("ts") or ts_iso,
        "temp": env.get("temp") if env.get("temp") is not None else data.get("temp"),
        "hum": env.get("hum") if env.get("hum") is not None else data.get("hum"),
        "gas_v": env.get("gas_v") if env.get("gas_v") is not None else data.get("gas_v"),
        "gas_dv": env.get("gas_dv") if env.get("gas_dv") is not None else data.get("gas_dv"),
        "gas_high": env.get("gas_high") if env.get("gas_high") is not None else data.get("gas_high", False),
    }
    return merged


def _build_central_status() -> Dict[str, Any]:
    ident = _get_central_identity()
    # env
    snap = get_latest_env()
    if snap:
        env = {
            "ts": snap.ts_iso,
            "temp": snap.temp,
            "hum": snap.hum,
            "gas_v": snap.gas_v,
            "gas_dv": snap.gas_dv,
            "gas_high": snap.gas_high,
        }
    else:
        env = {
            "ts": None,
            "temp": None,
            "hum": None,
            "gas_v": None,
            "gas_dv": None,
            "gas_high": False,
        }

    env = _merge_env_with_db(env, ident["box_id"])

    # health
    h = get_health_status()
    if h:
        cpu = h.cpu_percent if h.cpu_percent >= 0 else None
        mem = h.mem_percent if h.mem_percent >= 0 else None
        disk = h.disk_percent if h.disk_percent >= 0 else None
        inode = h.inode_percent if h.inode_percent >= 0 else None
        health = {
            "cpu_percent": cpu,
            "mem_percent": mem,
            "disk_percent": disk,
            "inode_percent": inode,
            "last_issue": h.last_issue,
            "ts": h.ts,
        }
    else:
        health = None

    # audio
    audio = get_last_audio_segment(ident["box_id"]) or None
    if audio:
        audio_summary = {
            "last_segment": {
                "id": audio["id"],
                "ts": audio["ts"],
                "duration_sec": audio["duration_sec"],
                "label": audio["label"],
            }
        }
    else:
        audio_summary = {}

    return {
        "ident": ident,
        "env": env,
        "health": health,
        "audio_summary": audio_summary,
    }


# ---------- UI routes ----------

@app.route("/")
def central_dashboard():
    init_db()
    central_status = _build_central_status()
    sensors = get_sensors_states()

    # برای هر سنسور، یک state ساده (online/offline) می‌سازیم
    sensor_cards = []
    for st in sensors:
        state_label = compute_state_label(st)
        env = _merge_env_with_db(st.env, st.sensor_id)
        sensor_cards.append(
            {
                "sensor_id": st.sensor_id,
                "sensor_name": st.sensor_name,
                "ip": st.ip,
                "last_seen": st.last_seen,
                "env": env,
                "audio_summary": st.audio_summary,
                "health": st.health,
                "state": state_label,
            }
        )

    return render_template(
        "central/dashboard.html",
        central=central_status,
        sensors=sensor_cards,
    )


@app.route("/audio")
def central_audio():
    init_db()
    ident = _get_central_identity()
    central_segments = list_audio_segments(ident["box_id"], limit=100)
    sensors = get_sensors_states()
    return render_template(
        "central/audio.html",
        ident=ident,
        central_segments=central_segments,
        sensors=sensors,
    )


# ---------- API for sensors ----------

@app.route("/api/public_key", methods=["GET"])
def api_public_key():
    """
    کلید عمومی RSA باکس مرکزی را برمی‌گرداند تا سنسورها با آن AES+RSA encrypt کنند.
    """
    ensure_box_keypair()
    pem = load_box_public_key()
    return jsonify({"public_key": pem.decode("utf-8")})


@app.route("/api/sensor_push", methods=["POST"])
def api_sensor_push():
    """
    دریافت دیتا از سنسور:

    body:
      {
        "sensor_id": "...",
        "ciphertext": "<base64( encrypt_with_public_key(...) )>"
      }

    Basic Auth:
      Authorization: Basic base64(user:pass)

    مرحله‌ها:
      - چک auth (اگر اولین بار است، ثبت می‌کنیم)
      - decrypt با کلید خصوصی مرکزی
      - payload را به central_sensors_link می‌دهیم
      - push_interval فعلی سنسور را در پاسخ برمی‌گردانیم
    """
    ident = _get_central_identity()
    data = request.get_json(silent=True) or {}
    sensor_id = data.get("sensor_id")
    cipher_b64 = data.get("ciphertext")
    if not sensor_id or not cipher_b64:
        return jsonify({"error": "invalid payload"}), 400

    # Basic Auth
    user, pw = parse_basic_auth_header(request.headers.get("Authorization"))
    if not user or not pw:
        return Response(
            "Unauthorized",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="central_sensor_push"'},
        )

    # اگر اولین بار است، auth را ثبت می‌کنیم
    if not has_auth(sensor_id):
        set_auth(sensor_id, user, pw)
    else:
        if not validate_auth(sensor_id, user, pw):
            return Response(
                "Unauthorized",
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="central_sensor_push"'},
            )

    # decrypt
    try:
        cipher_bytes = base64.b64decode(cipher_b64.encode("ascii"))
    except Exception as e:
        print("[central_api] invalid base64 ciphertext:", e)
        return jsonify({"error": "bad ciphertext"}), 400

    try:
        plain = decrypt_with_private_key(CRYPTO_PRIV_KEY, cipher_bytes)
        payload = json.loads(plain.decode("utf-8"))
    except Exception as e:
        print("[central_api] decrypt or JSON error:", e)
        return jsonify({"error": "decrypt_failed"}), 400

    if payload.get("type") != "sensor_samples":
        return jsonify({"error": "invalid payload type"}), 400

    remote_ip = request.headers.get("X-Forwarded-For") or request.remote_addr or None
    st = update_sensor_state_from_payload(payload, remote_ip)
    pi = get_sensor_push_interval(st.sensor_id)

    return jsonify({"ok": True, "push_interval": pi})


@app.route("/api/sensor_config/<sensor_id>", methods=["GET"])
def api_sensor_config(sensor_id: str):
    """
    برگرداندن push_interval برای سنسور؛ سنسورها در sensor_to_central از این استفاده می‌کنند.
    """
    pi = get_sensor_push_interval(sensor_id)
    return jsonify({"sensor_id": sensor_id, "push_interval": pi})


# ---------- Debug API ----------

@app.route("/api/sensors_state", methods=["GET"])
def api_sensors_state():
    sensors = get_sensors_states()
    out: List[Dict[str, Any]] = []
    for st in sensors:
        out.append(
            {
                "sensor_id": st.sensor_id,
                "sensor_name": st.sensor_name,
                "ip": st.ip,
                "last_seen": st.last_seen,
                "state": compute_state_label(st),
                "env": st.env,
                "audio_summary": st.audio_summary,
                "health": st.health,
            }
        )
    return jsonify({"sensors": out})


def run_flask(host: str = "0.0.0.0", port: int = DEFAULT_HTTP_PORT):
    init_db()
    ensure_box_keypair()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    print("=== central_api test ===")
    print("Flask پنل مرکزی روی پورت", DEFAULT_HTTP_PORT, "بالا می‌آید. Ctrl+C برای خروج.")
    run_flask()

