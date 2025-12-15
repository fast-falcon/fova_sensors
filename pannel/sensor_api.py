# sensor_api.py
"""
Flask پنل باکس سنسور + API برای استفاده‌ی باکس مرکزی.

مسیرها:

UI (بدون احراز هویت، برای کاربر محلی):
  - GET /            → داشبورد سنسور
  - GET /audio       → لیست صداهای ضبط‌شده و امکان پخش

API برای باکس مرکزی (با Basic Auth):
  - GET /api/status
  - GET /api/audio_segments
  - GET /api/audio_file/<int:seg_id>
  - POST /api/label_update
"""

import os
import sqlite3
from functools import wraps
from typing import Optional, Dict, Any, List

from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    send_file,
    abort,
    Response,
)

from panel_paths import BASE_DIR, AUDIO_ROOT, DB_PATH
from panel_config import get_config, get_sensor_config, get_box_id
from panel_sensors_local import get_latest_env
from panel_health import get_health_status
from panel_audio_local import list_audio_segments, get_last_audio_segment
from panel_net_common import parse_basic_auth_header
from panel_db import init_db, get_sensor_history

TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)


def _get_sensor_identity() -> Dict[str, Any]:
    """
    اطلاعات پایه‌ی سنسور (box_id + sensor_name) را برمی‌گرداند.
    """
    cfg = get_config() or {}
    sensor_cfg = get_sensor_config(cfg) or {}
    box_id = get_box_id(cfg) or "UNKNOWN_SENSOR"
    sensor_name = sensor_cfg.get("sensor_name") or box_id
    return {"box_id": box_id, "sensor_name": sensor_name}


def _get_basic_auth_creds() -> Optional[Dict[str, str]]:
    cfg = get_config() or {}
    sensor_cfg = get_sensor_config(cfg) or {}
    u = sensor_cfg.get("auth_user")
    p = sensor_cfg.get("auth_pass")
    if not u or not p:
        return None
    return {"user": u, "pass": p}


def require_basic_auth(view_func):
    """
    decorator برای APIهایی که فقط باکس مرکزی می‌تواند به آن‌ها دسترسی داشته باشد.
    """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        creds = _get_basic_auth_creds()
        if not creds:
            return Response("Auth not configured", status=500)

        header = request.headers.get("Authorization")
        u, p = parse_basic_auth_header(header)
        if u != creds["user"] or p != creds["pass"]:
            return Response(
                "Unauthorized",
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="sensor_api"'},
            )
        return view_func(*args, **kwargs)

    return wrapper


def _build_env_history(sensor_id: str, env: Dict[str, Any], limit: int = 30) -> List[Dict[str, Any]]:
    history_rows = list(get_sensor_history(sensor_id, limit=limit))
    if env and env.get("ts"):
        if not history_rows or history_rows[-1][0] != env.get("ts"):
            history_rows.append((env.get("ts"), env))

    normalized: List[Dict[str, Any]] = []
    for ts_iso, row in history_rows[-limit:]:
        normalized.append(
            {
                "ts": ts_iso,
                "temp": row.get("temp"),
                "hum": row.get("hum"),
                "gas_v": row.get("gas_v"),
                "gas_dv": row.get("gas_dv"),
            }
        )

    return normalized


def _build_status_dict() -> Dict[str, Any]:
    init_db()
    ident = _get_sensor_identity()
    env_snap = get_latest_env()
    if env_snap:
        env = {
            "ts": env_snap.ts_iso,
            "temp": env_snap.temp,
            "hum": env_snap.hum,
            "gas_v": env_snap.gas_v,
            "gas_dv": env_snap.gas_dv,
            "gas_high": env_snap.gas_high,
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

    health = get_health_status()
    if health:
        health_dict = {
            "cpu_percent": health.cpu_percent,
            "mem_percent": health.mem_percent,
            "disk_percent": health.disk_percent,
            "inode_percent": health.inode_percent,
            "last_issue": health.last_issue,
            "ts": health.ts,
        }
    else:
        health_dict = None

    last_audio = get_last_audio_segment(ident["box_id"]) or None
    if last_audio:
        audio_summary = {
            "last_segment": {
                "id": last_audio["id"],
                "ts": last_audio["ts"],
                "duration_sec": last_audio["duration_sec"],
                "label": last_audio["label"],
            }
        }
    else:
        audio_summary = {}

    return {
        "sensor_id": ident["box_id"],
        "sensor_name": ident["sensor_name"],
        "env": env,
        "env_history": _build_env_history(ident["box_id"], env),
        "health": health_dict,
        "audio_summary": audio_summary,
    }


# ---------- UI ROUTES ----------

@app.route("/")
def sensor_dashboard():
    ident = _get_sensor_identity()
    status = _build_status_dict()
    return render_template("sensor/dashboard.html", ident=ident, status=status)


@app.route("/audio")
def sensor_audio_list():
    ident = _get_sensor_identity()
    segments = list_audio_segments(ident["box_id"], limit=100)
    return render_template("sensor/audio_list.html", ident=ident, segments=segments)


@app.route("/api/dashboard_state", methods=["GET"])
def sensor_dashboard_state():
    init_db()
    return jsonify(_build_status_dict())


# ---------- API ROUTES (for central) ----------

@app.route("/api/status", methods=["GET"])
@require_basic_auth
def api_status():
    return jsonify(_build_status_dict())


@app.route("/api/audio_segments", methods=["GET"])
@require_basic_auth
def api_audio_segments():
    ident = _get_sensor_identity()
    sensor_id = ident["box_id"]
    try:
        limit = int(request.args.get("limit", "50"))
    except ValueError:
        limit = 50
    label_filter = request.args.get("label")

    segments = list_audio_segments(sensor_id, limit=limit)
    if label_filter:
        segments = [s for s in segments if s.get("label") == label_filter]

    return jsonify({"sensor_id": sensor_id, "segments": segments})


@app.route("/api/audio_file/<int:seg_id>", methods=["GET"])
@require_basic_auth
def api_audio_file(seg_id: int):
    """
    بازگردانی فایل wav مربوط به segment.
    """
    if not os.path.exists(DB_PATH):
        abort(404)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT filepath FROM audio_segments WHERE id = ?",
        (seg_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        abort(404)
    filepath = row[0]
    if not filepath or not os.path.exists(filepath):
        abort(404)
    # برای اینکه مرورگر/کلاینت درست تشخیص دهد:
    return send_file(filepath, mimetype="audio/wav", as_attachment=False)


@app.route("/api/label_update", methods=["POST"])
@require_basic_auth
def api_label_update():
    """
    آپدیت label برای segmentها.

    body:
      {
        "items": [
          {"id": 123, "label": "normal"},
          {"id": 124, "label": "abnormal"},
        ]
      }
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400

    if not os.path.exists(DB_PATH):
        return jsonify({"updated": 0})

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    updated = 0
    for it in items:
        seg_id = it.get("id")
        label = it.get("label")
        if seg_id is None:
            continue
        try:
            cur.execute(
                "UPDATE audio_segments SET label = ? WHERE id = ?",
                (label, seg_id),
            )
            if cur.rowcount > 0:
                updated += 1
        except Exception as e:
            print("[sensor_api] label_update error:", e)
    conn.commit()
    conn.close()

    return jsonify({"updated": updated})


def run_flask(host: str = "0.0.0.0", port: int = 8080):
    """
    برای استفاده در sensor_role.run_sensor
    """
    init_db()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    print("=== sensor_api test ===")
    print("فقط Flask را روی پورت 8080 بالا می‌آورد. Ctrl+C برای خروج.")
    run_flask()

