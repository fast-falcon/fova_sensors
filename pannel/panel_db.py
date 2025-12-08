# panel_db.py
"""
مدیریت دیتابیس SQLite مشترک (روی sdcard):

- kv_store: key/value برای تنظیمات ساده
- outbox: صف دیتای در انتظار ارسال (مثلاً مرکزی→سرور)
- sensor_samples: لاگ لوکال اندازه‌گیری‌ها
- audio_segments: متادیتای قطعات صوتی ذخیره‌شده
"""

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from panel_paths import DB_PATH, ensure_dirs

_DB_LOCK = threading.RLock()
_DB_CONN: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _DB_CONN
    with _DB_LOCK:
        if _DB_CONN is None:
            ensure_dirs()
            _DB_CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
            _DB_CONN.row_factory = sqlite3.Row
        return _DB_CONN


def init_db():
    """
    ساخت جدول‌ها (اگر وجود ندارند).
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sensor_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            data_json TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audio_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            filepath TEXT NOT NULL,
            duration_sec REAL,
            label TEXT
        )
        """
    )

    conn.commit()


def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None:
        return default
    return row["value"]


def kv_set(key: str, value: str) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO kv_store(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def enqueue_outbox(payload: Dict[str, Any]) -> int:
    """
    payload (دیکشنری) را به صورت JSON در outbox ذخیره می‌کند و id را برمی‌گرداند.
    """
    conn = _get_conn()
    cur = conn.cursor()
    ts = datetime.utcnow().isoformat() + "Z"
    cur.execute(
        "INSERT INTO outbox(payload, created_at, sent_at) VALUES (?, ?, NULL)",
        (json.dumps(payload), ts),
    )
    conn.commit()
    return cur.lastrowid


def get_next_outbox() -> Optional[Tuple[int, Dict[str, Any]]]:
    """
    قدیمی‌ترین رکوردی که هنوز sent_at NULL است را برمی‌گرداند.
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, payload FROM outbox WHERE sent_at IS NULL ORDER BY id ASC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        payload = {}
    return row["id"], payload


def mark_outbox_sent(outbox_id: int) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    ts = datetime.utcnow().isoformat() + "Z"
    cur.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (ts, outbox_id))
    conn.commit()


def store_sensor_sample(sensor_id: str, ts_iso: str, env: Dict[str, Any]) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sensor_samples(sensor_id, ts, data_json) VALUES (?, ?, ?)",
        (sensor_id, ts_iso, json.dumps(env)),
    )
    conn.commit()


def store_audio_segment(
    sensor_id: str,
    ts_iso: str,
    filepath: str,
    duration_sec: float,
    label: Optional[str] = None,
) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO audio_segments(sensor_id, ts, filepath, duration_sec, label) "
        "VALUES (?, ?, ?, ?, ?)",
        (sensor_id, ts_iso, filepath, duration_sec, label),
    )
    conn.commit()


if __name__ == "__main__":
    print("=== panel_db test ===")
    print("DB_PATH:", DB_PATH)
    init_db()
    print("DB init OK")

    # تست kv
    kv_set("test_key", "123")
    v = kv_get("test_key")
    print("kv_get('test_key') =", v)
    assert v == "123"

    # تست outbox
    oid = enqueue_outbox({"hello": "world"})
    print("enqueue_outbox id:", oid)
    row = get_next_outbox()
    print("get_next_outbox:", row)
    if row:
        mark_outbox_sent(row[0])
        print("marked sent")

    # تست لاگ سنسور و صوت
    store_sensor_sample("sensor-1", "2025-01-01T00:00:00Z", {"temp": 25.5})
    store_audio_segment(
        "sensor-1", "2025-01-01T00:00:10Z", "/sdcard/panel/audio/test.wav", 30.0, "normal"
    )
    print("✅ panel_db basic tests OK")

