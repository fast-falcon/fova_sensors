#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import signal
import time
import subprocess

LOG_PATH = "/data/local/tmp/hotspot.log"


def run(cmd, ignore_error=False):
    """
    اجرای یک دستور سیستم.
    cmd لیست آرگومان‌هاست، مثلا ["ndc", "softap", "startap"]
    """
    cmd = ["su_env"] + cmd
    try:
        subprocess.run(cmd, check=not ignore_error)
    except subprocess.CalledProcessError as e:
        msg = f"[ERROR] command failed: {' '.join(cmd)} (exit {e.returncode})"
        print(msg)
        if not ignore_error:
            raise


def ensure_root():
    """
    چک کن اسکریپت با روت اجرا شده.
    """
    if hasattr(os, "geteuid"):
        if os.geteuid() != 0:
            print("[-] این اسکریپت باید به‌صورت root اجرا شود (sudo / su).")
            sys.exit(1)
    else:
        # روی بعضی بیلدها geteuid ممکنه نباشه، ریسک می‌کنیم
        pass


def daemonize():
    """
    تبدیل اسکریپت به daemon:
    - detach از ترمینال / SSH
    - نادیده گرفتن SIGHUP
    - redirect stdout/stderr به فایل لاگ
    """

    # fork اول
    pid = os.fork()
    if pid > 0:
        # parent خارج شود
        sys.exit(0)

    # ساخت سشن جدید
    os.setsid()

    # سیگنال قطع ترمینال رو نادیده بگیر
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    # fork دوم (روش کلاسیک daemon)
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # بستن stdin
    try:
        sys.stdin.close()
    except Exception:
        pass

    # redirect stdout/stderr به فایل لاگ
    f = open(LOG_PATH, "a+", buffering=1)
    os.dup2(f.fileno(), sys.stdout.fileno())
    os.dup2(f.fileno(), sys.stderr.fileno())

    print("\n--- hotspot_daemon started in background ---")
    print("PID:", os.getpid())
    print("Log file:", LOG_PATH)
    print("-------------------------------------------\n")


def setup_hotspot(ssid, password):
    """
    همون دنباله‌ی ndc / setprop که دستی تست کردیم
    """

    print("[*] Cleaning previous tethering / softap state...")

    # تمیز کردن state قبلی
    run(["ndc", "tether", "interface", "remove", "wlan0"], ignore_error=True)
    run(["ndc", "tether", "stop"], ignore_error=True)
    run(["ndc", "ipfwd", "disable"], ignore_error=True)
    run(["ndc", "softap", "stopap"], ignore_error=True)
    run(["ndc", "softap", "fwreload", "wlan0", "STA"], ignore_error=True)
    run(
        ["ndc", "interface", "setcfg", "wlan0",
         "0.0.0.0", "0", "multicast", "broadcast", "down"],
        ignore_error=True,
    )

    print("[*] Stopping wpa_supplicant services...")
    run(["setprop", "ctl.stop", "wpa_supplicant"], ignore_error=True)
    run(["setprop", "ctl.stop", "p2p_supplicant"], ignore_error=True)

    time.sleep(1.0)

    print("[*] Starting SoftAP (hostapd)...")

    # سوئیچ فریم‌ور به AP
    run(["ndc", "softap", "fwreload", "wlan0", "AP"])

    # تنظیم SSID و پسورد
    run(["ndc", "softap", "set", "wlan0", ssid, "wpa2-psk", password])

    # استارت hostapd
    run(["ndc", "softap", "startap"])

    # IP دادن به wlan0
    run(["ndc", "interface", "setcfg", "wlan0", "192.168.43.1", "24", "up"])

    print("[*] Enabling tethering and DHCP (dnsmasq)...")

    # اضافه کردن wlan0 به تترینگ
    run(["ndc", "tether", "interface", "add", "wlan0"])

    # روشن کردن ip forwarding
    run(["ndc", "ipfwd", "enable"])

    # همان رنج‌هایی که تو لاگ خودت بود
    run([
        "ndc", "tether", "start",
        "192.168.42.2", "192.168.42.254",
        "192.168.43.2", "192.168.43.254",
        "192.168.44.2", "192.168.44.254",
        "192.168.45.2", "192.168.45.254",
        "192.168.46.2", "192.168.46.254",
        "192.168.47.2", "192.168.47.254",
        "192.168.48.2", "192.168.48.254",
    ])

    # DNS برای dnsmasq
    run(["ndc", "tether", "dns", "set", "8.8.8.8", "8.8.4.4"])

    print("[+] Hotspot should now be UP.")
    print(f"[+] SSID: {ssid}")
    print("[i] Check from another device and connect to this Wi-Fi.")


def main():
    ensure_root()

    print("=== Android Hotspot Daemon (ndc) ===")
    print("این اسکریپت ازت SSID (مثل username) و password هات‌اسپات رو می‌گیره.")
    print("بعد از گرفتن ورودی، خودش رو از SSH جدا می‌کنه و در پس‌زمینه ادامه می‌ده.\n")

    ssid = input("SSID (اسم وای‌فای / username): ").strip()
    passwd = input("Password (حداقل ۸ کاراکتر): ").strip()

    if not ssid:
        print("[-] SSID خالی است.")
        sys.exit(1)

    if len(passwd) < 8:
        print("[-] پسورد باید حداقل ۸ کاراکتر باشد.")
        sys.exit(1)

    print("\n[*] حالا اسکریپت جدا می‌شود و در پس‌زمینه ادامه می‌دهد.")
    print("[*] بعداً می‌توانی لاگ را این‌طور ببینی:")
    print(f"    cat {LOG_PATH}\n")

    # جدا شدن از SSH/ترمینال
    daemonize()

    # از اینجا به بعد، در پس‌زمینه و بدون وابستگی به SSH ادامه می‌دهد
    try:
        setup_hotspot(ssid, passwd)
    except Exception as e:
        print(f"[FATAL] Exception: {e!r}")
        sys.exit(1)


if __name__ == "__main__":
    main()

