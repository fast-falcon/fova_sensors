#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import re
import sys
import os

IW_BIN = "iw"          # اگر باینریت iw-arm است، بکنش "iw-arm" یا symlink بساز به /opt/bin/iw
INTERFACE = "wlan0"
WPA_CONF = "/data/misc/wifi/wpa_supplicant.conf"
SU_ENV = "su_env"      # همین اسکریپتی که در /opt/bin ساختی


# ---------- اجرای دستورها با su_env ----------

def su_env_run(cmd, input_data=None, detach=False):
    """
    اجرای دستور به صورت روت از طریق su_env.
    cmd: لیست مثل ["svc", "wifi", "disable"]
    اگر detach=True باشد، پروسه در session جدا اجرا می‌شود و با قطع SSH نمی‌میرد.
    """
    full_cmd = [SU_ENV] + cmd
    kwargs = {
        "stdin": subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        "stdout": subprocess.PIPE if not detach else subprocess.DEVNULL,
        "stderr": subprocess.PIPE if not detach else subprocess.DEVNULL,
    }

    if detach:
        # پروسه را در یک session جدا اجرا کن تا SIGHUP نکشدش
        def preexec():
            os.setsid()
        kwargs["preexec_fn"] = preexec

    try:
        proc = subprocess.Popen(full_cmd, **kwargs)
    except FileNotFoundError:
        print("✖ su_env پیدا نشد. مطمئن شو /opt/bin/su_env وجود دارد و در PATH است.")
        sys.exit(1)

    if detach:
        # در حالت جدا، دیگه منتظر خروجی نمی‌مونیم
        return 0, b""

    out, err = proc.communicate(input_data)
    rc = proc.returncode
    if rc != 0 and err:
        sys.stderr.write(err.decode(errors="ignore"))
    return rc, out


def ensure_original_backup():
    """
    از wpa_supplicant.conf یک بکاپ .orig می‌گیرد (فقط بار اول که وجود ندارد).
    """
    backup_path = WPA_CONF + ".orig"
    script = f'''
if [ ! -f "{backup_path}" ] && [ -f "{WPA_CONF}" ]; then
    cp "{WPA_CONF}" "{backup_path}"
fi
'''
    su_env_run(["sh"], input_data=script.encode("utf-8"))


# ---------- تبدیل سیگنال ----------

def dbm_to_level(dbm, max_level=10):
    try:
        dbm = float(dbm)
    except Exception:
        return None
    if dbm <= -90:
        return 1
    if dbm >= -30:
        return max_level
    ratio = (dbm + 90.0) / 60.0
    level = 1 + int(ratio * (max_level - 1))
    return level


def dbm_to_quality(dbm):
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
    if freq is None:
        return None
    try:
        f = int(freq)
    except Exception:
        return None
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if f == 2484:
        return 14
    if 5000 <= f <= 5900:
        return (f - 5000) // 5
    return None


# ---------- اسکن وای‌فای با iw ----------

def scan_wifi(iw_bin=IW_BIN, interface=INTERFACE):
    try:
        proc = subprocess.Popen(
            [iw_bin, "dev", interface, "scan"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
    except FileNotFoundError:
        print("✖ باینری iw پیدا نشد. بذارش تو PATH، مثلا /opt/bin/iw")
        sys.exit(1)

    out, _ = proc.communicate()
    text = out.decode(errors="ignore")
    lines = text.splitlines()

    networks = []
    cur = None

    for line in lines:
        stripped = line.strip()

        # شروع BSS
        m_bss = re.match(r"^BSS ([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\(", stripped)
        if m_bss:
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
                "security": set(),
            }
            continue

        if cur is None:
            continue

        m = re.match(r"^freq:\s*(\d+)", stripped)
        if m:
            f = int(m.group(1))
            cur["freq"] = f
            cur["channel"] = freq_to_channel(f)
            continue

        m = re.match(r"^signal:\s*(-?\d+\.?\d*) dBm", stripped)
        if m:
            dbm = float(m.group(1))
            cur["dbm"] = dbm
            cur["level"] = dbm_to_level(dbm)
            cur["quality"] = dbm_to_quality(dbm)
            continue

        m = re.match(r"^SSID:\s*(.*)", stripped)
        if m:
            ssid = m.group(1)
            cur["SSID"] = ssid
            continue

        if stripped.startswith("capability:"):
            if "Privacy" in stripped:
                cur["privacy"] = True
            continue

        if stripped.startswith("RSN:"):
            cur["privacy"] = True
            cur["security"].add("WPA2")
            continue

        if stripped.startswith("WPA:"):
            cur["privacy"] = True
            cur["security"].add("WPA")
            continue

    if cur:
        networks.append(cur)

    # نوع امنیت
    for n in networks:
        if not n["security"]:
            if n["privacy"]:
                n["security"] = {"WEP"}
            else:
                n["security"] = {"OPEN"}
        n["security"] = "/".join(sorted(n["security"]))

    return networks


# ---------- کار با wpa_supplicant.conf ----------

def read_wpa_conf(path=WPA_CONF):
    rc, out = su_env_run(["cat", path])
    if rc != 0:
        return ""
    return out.decode(errors="ignore")


def write_wpa_conf(new_content, path=WPA_CONF):
    cmd = ["sh", "-c", f"cat > {path}"]
    rc, _ = su_env_run(cmd, input_data=new_content.encode("utf-8"))
    if rc != 0:
        print("✖ خطا در نوشتن wpa_supplicant.conf")
        sys.exit(1)
    # chown ممکنه بسته به رام dot یا colon بخواد، هر دو رو امتحان می‌کنیم
    su_env_run(["chown", "system.wifi", path])
    su_env_run(["chown", "system:wifi", path])
    su_env_run(["chmod", "660", path])


def update_wpa_conf_single_network(ssid, password, security, path=WPA_CONF):
    """
    فایل wpa_supplicant.conf را طوری می‌نویسد که
    فقط یک network (همین SSID) داخلش باشد.
    هدر اصلی (ctrl_interface, device_name, ...) حفظ می‌شود.
    """
    content = read_wpa_conf(path)
    if not content.strip():
        header = "ctrl_interface=wlan0\nupdate_config=1\n\n"
    else:
        idx = content.find("network={")
        if idx != -1:
            header = content[:idx].rstrip() + "\n\n"
        else:
            header = content.rstrip() + "\n\n"

    block_lines = []
    block_lines.append("network={")
    block_lines.append(f'    ssid="{ssid}"')

    if security == "OPEN":
        block_lines.append("    key_mgmt=NONE")
    elif security == "WEP":
        block_lines.append("    key_mgmt=NONE")
        block_lines.append(f'    wep_key0="{password}"')
        block_lines.append("    wep_tx_keyidx=0")
    else:
        block_lines.append("    key_mgmt=WPA-PSK")
        block_lines.append(f'    psk="{password}"')

    block_lines.append("    priority=1")
    block_lines.append("}")
    block = "\n".join(block_lines) + "\n"

    new_content = header + block + "\n"
    write_wpa_conf(new_content, path)


def schedule_wifi_restart_with_fallback():
    """
    اسکریپت روت جدا:
      - wifi را disable/enable می‌کند
      - تا حدود ۳۰ ثانیه منتظر IP می‌ماند
      - اگر IP نگرفت، بکاپ .orig را برمی‌گرداند و دوباره wifi را ریستارت می‌کند
      - همه‌چیز را در لاگ می‌نویسد
    """
    script_path = "/data/local/tmp/wifi_reconnect.sh"
    log_path = "/data/local/tmp/wifi_reconnect.log"
    backup_path = WPA_CONF + ".orig"

    script = f"""#!/system/bin/sh
CONF="{WPA_CONF}"
BACKUP="{backup_path}"
LOG="{log_path}"

PATH=/sbin:/vendor/bin:/system/sbin:/system/bin:/system/xbin
export PATH

mkdir -p /data/local/tmp 2>/dev/null

echo "---- wifi_reconnect start ----" >> "$LOG"
date >> "$LOG" 2>/dev/null || true

echo "Disabling wifi" >> "$LOG"
svc wifi disable >> "$LOG" 2>&1
sleep 3
echo "Enabling wifi" >> "$LOG"
svc wifi enable >> "$LOG" 2>&1

i=0
ok=0
while [ $i -lt 10 ]; do
    ip=$(getprop dhcp.wlan0.ipaddress)
    if [ "x$ip" = "x" ]; then
        ip=$(ip addr show wlan0 2>/dev/null | awk '/inet /{{print $2}}')
    fi
    if [ "x$ip" = "x" ]; then
        ip=$(ifconfig wlan0 2>/dev/null | awk '/inet /{{print $2}}' | head -n1)
    fi
    echo "Check $i, ip=$ip" >> "$LOG"
    if [ "x$ip" != "x" ]; then
        ok=1
        break
    fi
    sleep 3
    i=$((i+1))
done

if [ $ok -eq 0 ] && [ -f "$BACKUP" ]; then
    echo "No IP, restoring backup" >> "$LOG"
    cp "$BACKUP" "$CONF"
    chown system.wifi "$CONF" 2>>"$LOG" || chown system:wifi "$CONF" 2>>"$LOG" || true
    chmod 660 "$CONF" 2>>"$LOG"
    echo "Restarting wifi after restore" >> "$LOG"
    svc wifi disable >> "$LOG" 2>&1
    sleep 3
    svc wifi enable >> "$LOG" 2>&1
fi

echo "wifi_reconnect done" >> "$LOG"
date >> "$LOG" 2>/dev/null || true
"""

    # اسکریپت را روی دیسک بنویس
    su_env_run(["sh", "-c", f"cat > {script_path}"], input_data=script.encode("utf-8"))
    su_env_run(["chmod", "755", script_path])

    # اجرای detached
    su_env_run(["sh", "-c", script_path], detach=True)


# ---------- UI متنی ----------

def choose_network(networks):
    if not networks:
        print("هیچ شبکه‌ای پیدا نشد.")
        sys.exit(0)

    networks = sorted(
        networks,
        key=lambda n: (n["quality"] if n["quality"] is not None else 0),
        reverse=True
    )

    print("لیست شبکه‌ها:\n")
    print("شماره\tSSID\t\tBSSID\t\tSignal(dBm)\tLevel\tQuality%\tSecurity")
    for idx, n in enumerate(networks, start=1):
        ssid = n["SSID"] if n["SSID"] else "<hidden>"
        b = n["BSSID"] or ""
        dbm = n["dbm"] if n["dbm"] is not None else ""
        lvl = n["level"] if n["level"] is not None else ""
        q = n["quality"] if n["quality"] is not None else ""
        sec = n.get("security", "")
        print(f"{idx}\t{ssid}\t{b}\t{dbm}\t{lvl}\t{q}\t{sec}")

    choice = input("\nشماره شبکه‌ای که می‌خوای وصل شی (یا q برای خروج): ").strip()
    if choice.lower() == 'q':
        sys.exit(0)
    try:
        num = int(choice)
    except ValueError:
        print("ورودی نامعتبر.")
        sys.exit(1)
    if num < 1 or num > len(networks):
        print("شماره خارج از محدوده.")
        sys.exit(1)
    return networks[num - 1]


def main():
    print("در حال اسکن وای‌فای ...")
    nets = scan_wifi()
    target = choose_network(nets)

    ssid = target["SSID"] if target["SSID"] else ""
    security = target.get("security", "OPEN")

    print(f"\nانتخاب شد: SSID = «{ssid or '<hidden>'}»  | Security = {security}")

    if not ssid:
        print("این شبکه SSID مخفی دارد و در این نسخه پشتیبانی نمی‌شود.")
        sys.exit(1)

    if security == "OPEN":
        print("این شبکه ظاهراً بدون رمز (OPEN) است.")
        use_open = input("می‌خوای به صورت OPEN وصل شی؟ (y/n): ").strip().lower()
        if use_open != 'y':
            print("لغو شد.")
            sys.exit(0)
        password = ""
    else:
        password = input("پسورد وای‌فای را وارد کن (خالی = انصراف): ").strip()
        if not password:
            print("لغو شد.")
            sys.exit(0)

    print("\nدر حال آماده‌سازی بکاپ اولیه (در صورت نیاز) ...")
    ensure_original_backup()

    print("در حال به‌روزرسانی wpa_supplicant.conf (فقط همین شبکه) ...")
    update_wpa_conf_single_network(ssid, password, security)

    print("\n⚠ چون با SSH روی همین وای‌فای وصلی، با خاموش‌شدن Wi-Fi احتمالاً ارتباطت قطع می‌شه.")
    print("   یک اسکریپت روت جداگانه روی دستگاه اجرا می‌شود که وای‌فای را ریستارت کند و در صورت عدم اتصال، بکاپ را برگرداند.")

    schedule_wifi_restart_with_fallback()

    print("\n✅ کار پایتون تمام شد. چند ثانیه صبر کن تا اسکریپت روت کارش را انجام دهد.")
    print("   برای دیدن لاگ:")
    print("   su_env cat /data/local/tmp/wifi_reconnect.log")


if __name__ == "__main__":
    main()

