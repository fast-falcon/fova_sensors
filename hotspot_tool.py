#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import sys
import os

SU_ENV = "su_env"  # مثل wifi_tool.py

# ----------- اجرای دستورها با su_env -----------

def su_env_run(cmd, input_data=None, detach=False):
    """
    اجرای دستور به صورت روت از طریق su_env.
    cmd: لیست مثل ["ndc", "softap", "startap"]
    اگر detach=True باشد، پروسه در session جدا اجرا می‌شود
    و با قطع SSH نمی‌میرد.
    """
    full_cmd = [SU_ENV] + cmd

    kwargs = {
        "stdin": subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE if not detach else subprocess.DEVNULL,
        "stderr": subprocess.PIPE if not detach else subprocess.DEVNULL,
    }

    if detach:
        # پروسه را در یک session جدا اجرا کن
        def preexec():
            os.setsid()
        kwargs["preexec_fn"] = preexec

    try:
        proc = subprocess.Popen(full_cmd, **kwargs)
    except FileNotFoundError:
        print("✖ su_env پیدا نشد. مطمئن شو /opt/bin/su_env وجود دارد و در PATH است.")
        sys.exit(1)

    if detach:
        # در حالت جدا، منتظر خروجی نمی‌مونیم
        return 0, b""

    out, err = proc.communicate(input_data)
    rc = proc.returncode
    if rc != 0 and err:
        sys.stderr.write(err.decode(errors="ignore"))
    return rc, out


# ----------- ساخت و اجرای اسکریپت روت هات‌اسپات -----------

def schedule_hotspot_start(ssid, password):
    """
    یک اسکریپت روت می‌سازد و به صورت detached اجرا می‌کند
    که هات‌اسپات را با ndc بالا می‌آورد.
    """

    script_path = "/data/local/tmp/hotspot_start.sh"
    log_path = "/data/local/tmp/hotspot.log"

    # برای قرار دادن در دابل‌کوت، " را escape می‌کنیم
    ssid_shell = ssid.replace('"', '\\"')
    pass_shell = password.replace('"', '\\"')

    script = f"""#!/system/bin/sh
LOG="{log_path}"
SSID="{ssid_shell}"
PASS="{pass_shell}"

PATH=/sbin:/vendor/bin:/system/sbin:/system/bin:/system/xbin
export PATH

mkdir -p /data/local/tmp 2>/dev/null

echo "==== Hotspot script start ====" >> "$LOG"
date >> "$LOG" 2>/dev/null || true

echo "[*] Cleaning previous tether/softap state..." >> "$LOG"

ndc tether interface remove wlan0 >> "$LOG" 2>&1
ndc tether stop >> "$LOG" 2>&1
ndc ipfwd disable >> "$LOG" 2>&1
ndc softap stopap >> "$LOG" 2>&1
ndc softap fwreload wlan0 STA >> "$LOG" 2>&1
ndc interface setcfg wlan0 0.0.0.0 0 multicast broadcast down >> "$LOG" 2>&1

echo "[*] Stopping wpa_supplicant services..." >> "$LOG"
setprop ctl.stop wpa_supplicant >> "$LOG" 2>&1
setprop ctl.stop p2p_supplicant >> "$LOG" 2>&1
sleep 1

echo "[*] Switching firmware to AP mode..." >> "$LOG"
ndc softap fwreload wlan0 AP >> "$LOG" 2>&1

echo "[*] Setting SSID/Password..." >> "$LOG"
ndc softap set wlan0 "$SSID" wpa2-psk "$PASS" >> "$LOG" 2>&1

echo "[*] Starting hostapd (softap)..." >> "$LOG"
ndc softap startap >> "$LOG" 2>&1
sleep 1

echo "[*] Giving IP to wlan0..." >> "$LOG"
ndc interface setcfg wlan0 192.168.43.1 24 up >> "$LOG" 2>&1
ip addr show wlan0 >> "$LOG" 2>&1

echo "[*] Enabling tethering & DHCP..." >> "$LOG"
ndc tether interface add wlan0 >> "$LOG" 2>&1
ndc ipfwd enable >> "$LOG" 2>&1

ndc tether start \\
  192.168.42.2 192.168.42.254 \\
  192.168.43.2 192.168.43.254 \\
  192.168.44.2 192.168.44.254 \\
  192.168.45.2 192.168.45.254 \\
  192.168.46.2 192.168.46.254 \\
  192.168.47.2 192.168.47.254 \\
  192.168.48.2 192.168.48.254 >> "$LOG" 2>&1

ndc tether dns set 8.8.8.8 8.8.4.4 >> "$LOG" 2>&1

echo "[+] Hotspot should now be UP (SSID=$SSID)" >> "$LOG"
date >> "$LOG" 2>/dev/null || true
echo "==== Hotspot script done ====" >> "$LOG"
"""

    # اسکریپت را روی دیسک بنویس
    su_env_run(
        ["sh", "-c", f"cat > {script_path}"],
        input_data=script.encode("utf-8")
    )
    su_env_run(["chmod", "755", script_path])

    # اجرای detached (پس‌زمینه)
    su_env_run(["sh", "-c", script_path], detach=True)


# ----------- main: گرفتن SSID/پسورد و شلیک اسکریپت -----------

def main():
    print("=== Android Hotspot Tool (ndc + su_env) ===\n")
    print("این اسکریپت ازت SSID (اسم وای‌فای / username) و پسورد هات‌اسپات را می‌گیرد،")
    print("بعد یک اسکریپت روت جدا می‌سازد و آن را در پس‌زمینه اجرا می‌کند،")
    print("طوری که اگر SSH قطع شود هم هات‌اسپات بالا می‌آید.\n")

    ssid = input("SSID (اسم وای‌فای / username): ").strip()
    password = input("Password (حداقل ۸ کاراکتر): ").strip()

    if not ssid:
        print("✖ SSID خالی است.")
        sys.exit(1)

    if len(password) < 8:
        print("✖ پسورد باید حداقل ۸ کاراکتر باشد.")
        sys.exit(1)

    print("\n⚠ توجه: اگر الان با وای‌فای به دستگاه SSH زدی، با این کار احتمالاً ارتباط قطع می‌شود،")
    print("   ولی اسکریپت روت در پس‌زمینه ادامه می‌دهد و هات‌اسپات را بالا می‌آورد.\n")

    schedule_hotspot_start(ssid, password)

    print("✅ اسکریپت روت برای شروع هات‌اسپات شلیک شد (detached).")
    print("   برای دیدن لاگ:")
    print("   su_env cat /data/local/tmp/hotspot.log\n")
    print("   از یک دستگاه دیگر، شبکهٔ وای‌فای با SSID که دادی را جستجو کن.")


if __name__ == "__main__":
    main()

