# panel_wizard.py
"""
ویزارد راه‌انداز اولیه برای باکس (مرکزی یا سنسور).

فقط وقتی اجرا می‌شود که هیچ panel_config.json یی وجود نداشته باشد.
گام‌ها:
  1) انتخاب نقش (central / sensor)
  2) بر اساس نقش، فرم‌های تنظیمات
  3) تایید نهایی، ذخیره config، ساخت کلیدها، تلاش برای reboot
"""

import os
from typing import Any, Dict

from flask import Flask, request, redirect, url_for, render_template

from panel_paths import BASE_DIR, DEFAULT_HTTP_PORT
from panel_config import set_config, save_config, load_config, get_role
from panel_crypto import ensure_box_keypair
from panel_ssh import ensure_ssh_key
from panel_net_common import su_env_run

TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)

# وضعیت موقت ویزارد در حافظه
_WIZARD_STATE: Dict[str, Any] = {}


def start_hotspot_if_possible(ssid: str, password: str):
    """
    TODO:
      اینجا می‌تونی به hotspot_tool.schedule_hotspot_start(ssid, password) وصل شوی.
      فعلاً فقط برای جلوگیری از import خطی، print می‌کند.
    """
    try:
        # مثال اگر hotspot_tool کنار این فایل‌هاست:
        # from hotspot_tool import schedule_hotspot_start
        # schedule_hotspot_start(ssid, password)
        print(f"[panel_wizard] (TODO) start hotspot SSID={ssid} PASS={password}")
    except Exception as e:
        print("[panel_wizard] hotspot start failed:", e)


def open_local_browser(url: str):
    """
    با am مرورگر دیفالت اندروید را روی URL مورد نظر باز می‌کند.
    """
    try:
        su_env_run(
            [
                "am", "start",
                "-a", "android.intent.action.VIEW",
                "-d", url,
            ],
            detach=True,
        )
    except Exception as e:
        print("[panel_wizard] open_local_browser error:", e)


@app.route("/wizard/role", methods=["GET", "POST"])
def wizard_role():
    # اگر به هر دلیل config در حین ویزارد ساخته شده، ریدایرکت به نقش واقعی
    cfg = load_config()
    role = get_role(cfg)
    if cfg and role in ("central", "sensor"):
        from panel_main import main as panel_main_entry
        # در حالت واقعی بهتره process دوباره start بشه؛ اینجا فقط پیام می‌دیم.
        return render_template("wizard/role_select.html", already_configured=True)

    if request.method == "POST":
        role = request.form.get("role")
        if role == "central":
            _WIZARD_STATE.clear()
            _WIZARD_STATE["role"] = "central"
            return redirect(url_for("wizard_central_step1"))
        elif role == "sensor":
            _WIZARD_STATE.clear()
            _WIZARD_STATE["role"] = "sensor"
            return redirect(url_for("wizard_sensor_step1"))
    return render_template("wizard/role_select.html", already_configured=False)


@app.route("/wizard/central/step1", methods=["GET", "POST"])
def wizard_central_step1():
    if _WIZARD_STATE.get("role") != "central":
        return redirect(url_for("wizard_role"))

    context = {
        "cluster_name": _WIZARD_STATE.get("cluster_name", ""),
        "hotspot_ssid": _WIZARD_STATE.get("hotspot_ssid", "box_config"),
        "hotspot_password": _WIZARD_STATE.get("hotspot_password", "12345678"),
    }

    errors = []
    if request.method == "POST":
        cluster_name = (request.form.get("cluster_name") or "").strip()
        hotspot_ssid = (request.form.get("hotspot_ssid") or "").strip() or "box_config"
        hotspot_password = (request.form.get("hotspot_password") or "").strip()

        if not cluster_name:
            errors.append("نام مجموعه (Cluster) الزامی است.")
        if not hotspot_password or len(hotspot_password) < 8:
            errors.append("رمز هات‌اسپات حداقل باید ۸ کاراکتر باشد.")

        if not errors:
            _WIZARD_STATE["cluster_name"] = cluster_name
            _WIZARD_STATE["hotspot_ssid"] = hotspot_ssid
            _WIZARD_STATE["hotspot_password"] = hotspot_password

            # در همین مرحله می‌توانیم هات‌اسپات box_config را بالا بیاوریم
            start_hotspot_if_possible(hotspot_ssid, hotspot_password)

            return redirect(url_for("wizard_central_step2"))

    return render_template("wizard/central_step1.html", errors=errors, **context)


@app.route("/wizard/central/step2", methods=["GET", "POST"])
def wizard_central_step2():
    if _WIZARD_STATE.get("role") != "central":
        return redirect(url_for("wizard_role"))

    context = {
        "server_host": _WIZARD_STATE.get("server_host", ""),
        "server_socket_port": _WIZARD_STATE.get("server_socket_port", 9000),
        "server_http_port": _WIZARD_STATE.get("server_http_port", 8000),
    }

    errors = []
    if request.method == "POST":
        server_host = (request.form.get("server_host") or "").strip()
        try:
            server_socket_port = int(request.form.get("server_socket_port") or "9000")
        except ValueError:
            server_socket_port = 9000
        try:
            server_http_port = int(request.form.get("server_http_port") or "8000")
        except ValueError:
            server_http_port = 8000

        if not server_host:
            errors.append("آدرس سرور اصلی الزامی است.")

        if not errors:
            _WIZARD_STATE["server_host"] = server_host
            _WIZARD_STATE["server_socket_port"] = server_socket_port
            _WIZARD_STATE["server_http_port"] = server_http_port

            return redirect(url_for("wizard_central_confirm"))

    return render_template("wizard/central_step2.html", errors=errors, **context)


@app.route("/wizard/central/confirm", methods=["GET", "POST"])
def wizard_central_confirm():
    if _WIZARD_STATE.get("role") != "central":
        return redirect(url_for("wizard_role"))

    if request.method == "POST":
        cfg = {
            "role": "central",
            "central": {
                "cluster_name": _WIZARD_STATE["cluster_name"],
                "hotspot_ssid": _WIZARD_STATE["hotspot_ssid"],
                "hotspot_password": _WIZARD_STATE["hotspot_password"],
                "server_host": _WIZARD_STATE["server_host"],
                "server_socket_port": _WIZARD_STATE["server_socket_port"],
                "server_http_port": _WIZARD_STATE["server_http_port"],
            },
            "sensor": None,
        }
        set_config(cfg)
        save_config()

        # آماده‌سازی کلیدها
        ensure_box_keypair()
        ensure_ssh_key()

        # تلاش برای ریستارت
        try:
            su_env_run(["reboot"], detach=True)
        except Exception as e:
            print("[panel_wizard] reboot failed:", e)

        return render_template("wizard/done_reboot.html", role="central")

    return render_template("wizard/central_confirm.html", state=_WIZARD_STATE)


@app.route("/wizard/sensor/step1", methods=["GET", "POST"])
def wizard_sensor_step1():
    if _WIZARD_STATE.get("role") != "sensor":
        return redirect(url_for("wizard_role"))

    context = {
        "sensor_name": _WIZARD_STATE.get("sensor_name", ""),
        "wifi_ssid": _WIZARD_STATE.get("wifi_ssid", "box_config"),
        "wifi_password": _WIZARD_STATE.get("wifi_password", ""),
        "central_host": _WIZARD_STATE.get("central_host", "172.10.1.1"),
        "central_http_port": _WIZARD_STATE.get("central_http_port", DEFAULT_HTTP_PORT),
        "online_enabled": _WIZARD_STATE.get("online_enabled", True),
    }

    errors = []
    if request.method == "POST":
        sensor_name = (request.form.get("sensor_name") or "").strip()
        wifi_ssid = (request.form.get("wifi_ssid") or "").strip()
        wifi_password = (request.form.get("wifi_password") or "").strip()
        central_host = (request.form.get("central_host") or "").strip() or "172.10.1.1"
        try:
            central_http_port = int(
                request.form.get("central_http_port") or str(DEFAULT_HTTP_PORT)
            )
        except ValueError:
            central_http_port = DEFAULT_HTTP_PORT
        online_enabled = request.form.get("online_enabled") == "on"

        if not sensor_name:
            errors.append("نام سنسور الزامی است.")
        if not wifi_ssid:
            errors.append("نام وای‌فای مرکزی الزامی است.")
        if not wifi_password:
            errors.append("رمز وای‌فای مرکزی الزامی است.")

        if not errors:
            _WIZARD_STATE.update(
                sensor_name=sensor_name,
                wifi_ssid=wifi_ssid,
                wifi_password=wifi_password,
                central_host=central_host,
                central_http_port=central_http_port,
                online_enabled=online_enabled,
            )
            return redirect(url_for("wizard_sensor_confirm"))

    return render_template("wizard/sensor_step1.html", errors=errors, **context)


@app.route("/wizard/sensor/confirm", methods=["GET", "POST"])
def wizard_sensor_confirm():
    if _WIZARD_STATE.get("role") != "sensor":
        return redirect(url_for("wizard_role"))

    if request.method == "POST":
        # auth_user/pass را فعلاً خالی می‌گذاریم؛ sensor_api خودش بعداً gen_token می‌کند
        cfg = {
            "role": "sensor",
            "central": None,
            "sensor": {
                "sensor_name": _WIZARD_STATE["sensor_name"],
                "wifi_ssid": _WIZARD_STATE["wifi_ssid"],
                "wifi_password": _WIZARD_STATE["wifi_password"],
                "central_host": _WIZARD_STATE["central_host"],
                "central_http_port": _WIZARD_STATE["central_http_port"],
                "auth_user": "",
                "auth_pass": "",
                "online_enabled": _WIZARD_STATE.get("online_enabled", True),
            },
        }
        set_config(cfg)
        save_config()

        ensure_box_keypair()
        ensure_ssh_key()

        try:
            su_env_run(["reboot"], detach=True)
        except Exception as e:
            print("[panel_wizard] reboot failed:", e)

        return render_template("wizard/done_reboot.html", role="sensor")

    return render_template("wizard/sensor_confirm.html", state=_WIZARD_STATE)


def run_wizard():
    """
    توسط panel_main وقتی config وجود ندارد فراخوانی می‌شود.
    """
    # سعی می‌کنیم روی خود باکس مرورگر را باز کنیم
    url = f"http://127.0.0.1:{DEFAULT_HTTP_PORT}/wizard/role"
    open_local_browser(url)

    print("[panel_wizard] starting wizard Flask app on port", DEFAULT_HTTP_PORT)
    app.run(host="0.0.0.0", port=DEFAULT_HTTP_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    print("=== panel_wizard test ===")
    print("در حال اجرای ویزارد روی پورت", DEFAULT_HTTP_PORT)
    print("برای خروج Ctrl+C بزن.")
    run_wizard()

