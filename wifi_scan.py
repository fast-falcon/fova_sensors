#!/usr/bin/env python3
import subprocess
import re
import json
import sys

# مسیر باینری iw رو اینجا تنظیم کن
IW_BIN = "iw"   # مثلا: "/data/opt/home/pouria/iw-arm"
INTERFACE = "wlan0"


def dbm_to_level(dbm, max_level=10):
    """
    تبدیل dBm به سطح 1..max_level
    بازه تقریبی: -90 (ضعیف) تا -30 (خیلی قوی)
    """
    try:
        dbm = float(dbm)
    except Exception:
        return None

    if dbm <= -90:
        return 1
    if dbm >= -30:
        return max_level

    # نگاشت خطی -90..-30 -> 1..max_level
    ratio = (dbm + 90.0) / 60.0  # 0..1
    level = 1 + int(ratio * (max_level - 1))
    return level


def dbm_to_quality(dbm):
    """
    تبدیل dBm به درصد کیفیت (0..100)
    بازه تقریبی: -90 (0%) تا -30 (100%)
    """
    try:
        dbm = float(dbm)
    except Exception:
        return None

    if dbm <= -90:
        return 0
    if dbm >= -30:
        return 100

    ratio = (dbm + 90.0) / 60.0
    return int(ratio * 100)


def freq_to_channel(freq):
    """
    تبدیل فرکانس به شماره کانال تقریبی
    2.4GHz و 5GHz
    """
    if freq is None:
        return None
    try:
        f = int(freq)
    except Exception:
        return None

    # 2.4 GHz band
    if 2412 <= f <= 2472:
        return (f - 2407) // 5  # 2412 -> 1, 2437 -> 6, ...
    if f == 2484:
        return 14

    # 5 GHz band (تقریبی)
    if 5000 <= f <= 5900:
        return (f - 5000) // 5  # 5180 -> 36, 5200 -> 40, ...

    return None


def scan_wifi(iw_bin=IW_BIN, interface=INTERFACE):
    try:
        proc = subprocess.Popen(
            [iw_bin, "dev", interface, "scan"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
    except FileNotFoundError:
        print("✖ برنامه‌ی iw پیدا نشد. IW_BIN را درست تنظیم کن.")
        sys.exit(1)

    out, _ = proc.communicate()
    text = out.decode(errors="ignore")
    lines = text.splitlines()

    networks = []
    cur = None

    for line in lines:
        line = line.rstrip("\n")
        stripped = line.strip()

        # شروع BSS / شبکه جدید
        # فقط اگر بعد از BSS یک MAC به فرم XX:XX:XX:XX:XX:XX باشد
        m_bss = re.match(r"^BSS ([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\(", stripped)
        if m_bss:
            # قبلی رو ذخیره کن
            if cur:
                networks.append(cur)

            bssid = m_bss.group(1)
            cur = {
                "BSSID": bssid,
                "SSID": None,
                "freq": None,
                "channel": None,
                "dbm": None,
                "level": None,
                "quality": None,
                "privacy": False,
                "security": set(),  # مثلا {"WPA2", "WPA"}
            }
            continue

        if cur is None:
            continue

        # freq: 2437
        m = re.match(r"^freq:\s*(\d+)", stripped)
        if m:
            f = int(m.group(1))
            cur["freq"] = f
            cur["channel"] = freq_to_channel(f)
            continue

        # signal: -68.00 dBm
        m = re.match(r"^signal:\s*(-?\d+\.?\d*) dBm", stripped)
        if m:
            dbm = float(m.group(1))
            cur["dbm"] = dbm
            cur["level"] = dbm_to_level(dbm)
            cur["quality"] = dbm_to_quality(dbm)
            continue

        # SSID: ...
        m = re.match(r"^SSID:\s*(.*)", stripped)
        if m:
            ssid = m.group(1)
            cur["SSID"] = ssid
            continue

        # capability: ESS Privacy ...
        if stripped.startswith("capability:"):
            if "Privacy" in stripped:
                cur["privacy"] = True
            continue

        # RSN: → معمولاً WPA2
        if stripped.startswith("RSN:"):
            cur["privacy"] = True
            cur["security"].add("WPA2")
            continue

        # WPA: → معمولاً WPA (قدیمی‌تر)
        if stripped.startswith("WPA:"):
            cur["privacy"] = True
            cur["security"].add("WPA")
            continue

    # آخرین شبکه
    if cur:
        networks.append(cur)

    # تعیین نوع امنیت نهایی
    for n in networks:
        if not n["security"]:
            if n["privacy"]:
                # privacy هست ولی WPA/RSN نه → احتمالاً WEP
                n["security"] = {"WEP"}
            else:
                n["security"] = {"OPEN"}
        # تبدیل set به string مرتب
        n["security"] = "/".join(sorted(n["security"]))

    return networks


def print_table(networks):
    print("SSID\t\tBSSID\t\tFreq\tCh\tSignal(dBm)\tLevel(1-10)\tQuality%\tSecurity")
    for n in networks:
        ssid = n["SSID"] if n["SSID"] else "<hidden>"
        b = n["BSSID"] or ""
        f = n["freq"] or ""
        ch = n["channel"] or ""
        dbm = n["dbm"] if n["dbm"] is not None else ""
        lvl = n["level"] if n["level"] is not None else ""
        q = n["quality"] if n["quality"] is not None else ""
        sec = n.get("security", "")
        print(f"{ssid}\t{b}\t{f}\t{ch}\t{dbm}\t{lvl}\t{q}\t{sec}")


def main():
    nets = scan_wifi()
    print_table(nets)
    print("\nJSON output:")
    print(json.dumps(nets, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

