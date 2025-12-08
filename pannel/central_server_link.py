# central_server_link.py
"""
uplink باکس مرکزی به سرور اصلی:

- اتصال TCP به سرور (GATEWAY_HOST/GATEWAY_PORT روی سرور Django)
- handshake اولیه:
    hello = {type:"hello", central_id, cluster_name, ssh_pub_key}
    ← welcome = {type:"welcome", server_public_key, reverse_ssh_port}
- encryption: RSA+AES-GCM (panel_crypto.encrypt_with_public_key)
- صف outbox (panel_db) برای ذخیره‌ی payload ها در حالت offline
- گرفتن config از سرور از طریق HTTP:
    /monitoring/api/config/<central_id> → central_send_interval + sensors push_interval
"""

import json
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from panel_config import get_config, get_central_config, get_box_id
from panel_paths import DEFAULT_SERVER_SOCKET_PORT, DEFAULT_SERVER_HTTP_PORT
from panel_db import enqueue_outbox, get_next_outbox, mark_outbox_sent, kv_get, kv_set
from panel_crypto import encrypt_with_public_key, encode_ciphertext
from panel_sensors_local import get_latest_env, EnvSnapshot
from panel_audio_local import get_last_audio_segment
from panel_health import get_health_status, update_health_status
from panel_ssh import get_ssh_public_key_text
from central_sensors_link import get_sensors_for_server_payload, set_sensor_push_interval

# کلیدهای kv_store
KV_SERVER_PUBKEY = "server:pubkey_pem"
KV_SEND_INTERVAL = "central:send_interval"

DEFAULT_SEND_INTERVAL = 10.0  # ثانیه
CONFIG_REFRESH_INTERVAL = 60.0  # هر چند ثانیه از سرور config بخوانیم

_UPLINK_THREAD: Optional[threading.Thread] = None
_STOP_FLAG = False


def _http_get_json(url: str, timeout: float = 5.0) -> Dict[str, Any]:
    req = Request(url, method="GET")
    req.add_header("Accept", "application/json")
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def _connect_and_handshake(server_host: str, server_port: int, central_id: str, cluster_name: str):
    """
    سوکت را به سرور وصل می‌کند و handshake اولیه را انجام می‌دهد.
    خروجی: (sock, fileobj, server_public_key_str, reverse_ssh_port)
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10.0)
    s.connect((server_host, server_port))
    f = s.makefile("rwb")

    ssh_pub = get_ssh_public_key_text()
    hello = {
        "type": "hello",
        "central_id": central_id,
        "cluster_name": cluster_name,
        "ssh_pub_key": ssh_pub,
    }
    f.write(json.dumps(hello).encode("utf-8") + b"\n")
    f.flush()

    line = f.readline()
    if not line:
        raise RuntimeError("no welcome from server")
    welcome = json.loads(line.decode("utf-8"))
    if welcome.get("type") != "welcome":
        raise RuntimeError("invalid welcome from server")

    server_pub = welcome.get("server_public_key")
    if not server_pub:
        raise RuntimeError("server_public_key missing in welcome")
    reverse_ssh_port = welcome.get("reverse_ssh_port")

    kv_set(KV_SERVER_PUBKEY, server_pub)

    print(
        f"[central_server_link] handshake OK: reverse_ssh_port={reverse_ssh_port}"
    )

    return s, f, server_pub, reverse_ssh_port


def _fetch_config_from_server(central_id: str, server_host: str, server_http_port: int) -> Optional[float]:
    """
    config را از سرور می‌گیرد:
      /monitoring/api/config/<central_id> → {
        "central_send_interval": float,
        "sensors": { "<sensor_id>": { "push_interval": float } }
      }

    خروجی: send_interval جدید (یا None)
    """
    url = f"http://{server_host}:{server_http_port}/monitoring/api/config/{central_id}/"
    try:
        data = _http_get_json(url, timeout=5.0)
    except Exception as e:
        print("[central_server_link] error fetching config from server:", e)
        return None

    send_interval = data.get("central_send_interval")
    if send_interval is not None:
        try:
            send_interval = float(send_interval)
            kv_set(KV_SEND_INTERVAL, str(send_interval))
            print("[central_server_link] central send_interval from server:", send_interval)
        except Exception:
            send_interval = None

    sensors_cfg = data.get("sensors") or {}
    for sid, cfg in sensors_cfg.items():
        pi = cfg.get("push_interval")
        if pi is not None:
            try:
                pi_f = float(pi)
                set_sensor_push_interval(sid, pi_f)
                print(f"[central_server_link] push_interval for sensor {sid} set to {pi_f}")
            except Exception:
                pass

    return send_interval


def _load_send_interval_from_kv() -> float:
    v = kv_get(KV_SEND_INTERVAL, None)
    if not v:
        return DEFAULT_SEND_INTERVAL
    try:
        return float(v)
    except Exception:
        return DEFAULT_SEND_INTERVAL


def _build_payload_for_server(central_id: str, cluster_name: str) -> Dict[str, Any]:
    """
    payload تجمیعی مرکزی + سنسورها برای ارسال به سرور اصلی.
    """
    # env مرکزی
    snap: Optional[EnvSnapshot] = get_latest_env()
    if snap:
        env = {
            "temp": snap.temp,
            "hum": snap.hum,
            "gas_v": snap.gas_v,
            "gas_dv": snap.gas_dv,
            "gas_high": snap.gas_high,
        }
        env_ts = snap.ts_iso
    else:
        env = {
            "temp": None,
            "hum": None,
            "gas_v": None,
            "gas_dv": None,
            "gas_high": False,
        }
        env_ts = datetime.now(timezone.utc).isoformat()

    # audio مرکزی
    audio = get_last_audio_segment(central_id) or {}
    if audio:
        audio_summary = {
            "last_segment": {
                "id": audio.get("id"),
                "ts": audio.get("ts"),
                "duration_sec": audio.get("duration_sec"),
                "label": audio.get("label"),
            }
        }
    else:
        audio_summary = {}

    # health مرکزی
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

    sensors = get_sensors_for_server_payload()

    now_iso = datetime.now(timezone.utc).isoformat()

    payload = {
        "type": "samples",
        "central_id": central_id,
        "cluster_name": cluster_name,
        "ts": now_iso,
        "central": {
            "env_ts": env_ts,
            "env": env,
            "audio_summary": audio_summary,
            "health": health_dict,
            # ssh_pub_key مرکزی را هم می‌توانیم اینجا بگذاریم
            "ssh_pub_key": get_ssh_public_key_text(),
        },
        "sensors": sensors,
    }
    return payload


def _send_payload(sock_file, server_public_key: str, payload: Dict[str, Any]) -> bool:
    """
    payload را رمز می‌کند و روی سوکت می‌فرستد و منتظر ack می‌شود.
    """
    try:
        plaintext = json.dumps(payload).encode("utf-8")
        cipher = encrypt_with_public_key(server_public_key.encode("utf-8"), plaintext)
        cipher_b64 = encode_ciphertext(cipher)
        frame = {"type": "data", "ciphertext": cipher_b64}
        sock_file.write(json.dumps(frame).encode("utf-8") + b"\n")
        sock_file.flush()

        # ack
        ack_line = sock_file.readline()
        if not ack_line:
            print("[central_server_link] no ack from server")
            return False
        try:
            ack = json.loads(ack_line.decode("utf-8"))
        except Exception:
            print("[central_server_link] invalid ack JSON")
            return False
        if not ack.get("ok"):
            print("[central_server_link] server ack not ok:", ack)
            return False
        return True
    except Exception as e:
        print("[central_server_link] send_payload error:", e)
        return False


def _uplink_loop():
    global _STOP_FLAG

    cfg = get_config() or {}
    if cfg.get("role") != "central":
        print("[central_server_link] role is not 'central' → exiting uplink loop")
        return
    central_cfg = get_central_config(cfg) or {}
    central_id = get_box_id(cfg) or "UNKNOWN_CENTRAL"
    cluster_name = central_cfg.get("cluster_name") or central_id

    server_host = central_cfg.get("server_host") or "127.0.0.1"
    server_socket_port = int(central_cfg.get("server_socket_port") or DEFAULT_SERVER_SOCKET_PORT)
    server_http_port = int(central_cfg.get("server_http_port") or DEFAULT_SERVER_HTTP_PORT)

    send_interval = _load_send_interval_from_kv()
    last_config_fetch = 0.0

    sock = None
    f = None
    server_pub = kv_get(KV_SERVER_PUBKEY, None)

    print(
        f"[central_server_link] uplink loop started -> server {server_host}:{server_socket_port}, "
        f"central_id={central_id}, send_interval={send_interval}"
    )

    while not _STOP_FLAG:
        try:
            # اگر سوکت نداریم، تلاش برای اتصال
            if sock is None or f is None or server_pub is None:
                try:
                    print("[central_server_link] connecting to server...")
                    sock, f, server_pub_str, rev_port = _connect_and_handshake(
                        server_host, server_socket_port, central_id, cluster_name
                    )
                    server_pub = server_pub_str
                    print("[central_server_link] connected to server.")
                    # TODO: اینجا می‌توانیم start_reverse_ssh(server_host, rev_port) را هم صدا بزنیم
                except Exception as e:
                    print("[central_server_link] connect/handshake failed:", e)
                    sock = None
                    f = None
                    server_pub = None
                    time.sleep(5.0)
                    continue

            # هر CONFIG_REFRESH_INTERVAL ثانیه، config را از سرور بگیر
            now = time.time()
            if now - last_config_fetch > CONFIG_REFRESH_INTERVAL:
                new_si = _fetch_config_from_server(central_id, server_host, server_http_port)
                if new_si is not None:
                    send_interval = new_si
                last_config_fetch = now

            # اول: ارسال outboxهای قدیمی (در صورت وجود)
            for _ in range(10):  # حداکثر ۱۰ رکورد در هر iteration
                row = get_next_outbox()
                if not row:
                    break
                oid, payload = row
                ok = _send_payload(f, server_pub, payload)
                if not ok:
                    raise RuntimeError("send_payload failed for outbox item")
                mark_outbox_sent(oid)

            # حالا payload جدید برای این لحظه
            payload = _build_payload_for_server(central_id, cluster_name)
            oid = enqueue_outbox(payload)
            ok = _send_payload(f, server_pub, payload)
            if ok:
                mark_outbox_sent(oid)

            time.sleep(send_interval)

        except Exception as e:
            print("[central_server_link] uplink loop error:", e)
            # در صورت هر خطا، سوکت را می‌بندیم و در دور بعد reconnect می‌کنیم
            try:
                if f:
                    f.close()
            except Exception:
                pass
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
            sock = None
            f = None
            server_pub = None
            time.sleep(5.0)

    # اگر STOP_FLAG فعال شد، سوکت را ببند
    try:
        if f:
            f.close()
    except Exception:
        pass
    try:
        if sock:
            sock.close()
    except Exception:
        pass
    print("[central_server_link] uplink loop stopped.")


def start_uplink_to_server():
    """
    ترد uplink را راه می‌اندازد.
    """
    global _UPLINK_THREAD, _STOP_FLAG
    if _UPLINK_THREAD is not None:
        return
    _STOP_FLAG = False
    t = threading.Thread(target=_uplink_loop, daemon=True)
    _UPLINK_THREAD = t
    t.start()


def stop_uplink_to_server():
    global _STOP_FLAG
    _STOP_FLAG = True


if __name__ == "__main__":
    print("=== central_server_link test (dry run) ===")
    cfg = get_config()
    print("config:", cfg)
    if not cfg or cfg.get("role") != "central":
        print("⚠️ نقش این باکس central نیست یا کانفیگ موجود نیست؛ uplink واقعی ممکن نیست.")
    else:
        print("شروع ترد uplink برای چند ثانیه...")
        start_uplink_to_server()
        time.sleep(5)
        stop_uplink_to_server()
        print("uplink stop called.")
    print("✅ central_server_link basic test (از نظر ساختار) OK")

