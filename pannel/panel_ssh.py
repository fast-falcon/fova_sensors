# panel_ssh.py
"""
مدیریت کلید SSH و راه‌اندازی reverse SSH برای باکس‌ها.

- ensure_ssh_key(): اگر کلید SSH وجود نداشته باشد، تولید می‌کند.
- get_ssh_public_key_text(): محتوای id_ed25519.pub را برمی‌گرداند.
- start_reverse_ssh(remote_host, remote_port, local_port=22): یک اتصال reverse ssh به سرور باز می‌کند.
"""

import os
import subprocess
from typing import Optional

from panel_paths import SDCARD_ROOT
from panel_net_common import su_env_run

SSH_DIR = os.path.join(SDCARD_ROOT, "ssh")
SSH_PRIV_KEY_PATH = os.path.join(SSH_DIR, "id_ed25519")
SSH_PUB_KEY_PATH = os.path.join(SSH_DIR, "id_ed25519.pub")


def ensure_ssh_key() -> None:
    """
    اگر کلیدهای SSH روی باکس وجود نداشته باشند، آن‌ها را تولید می‌کند.

    تلاش می‌کند از ssh-keygen استفاده کند:
      ssh-keygen -t ed25519 -N "" -f <SSH_PRIV_KEY_PATH>
    اگر در دستگاه موجود نباشد، یک کلید تقلبی (فقط برای تست) می‌سازد.
    """
    if os.path.exists(SSH_PRIV_KEY_PATH) and os.path.exists(SSH_PUB_KEY_PATH):
        return

    os.makedirs(SSH_DIR, exist_ok=True)

    # تلاش برای استفاده از ssh-keygen واقعی
    try:
        res = su_env_run(
            ["which", "ssh-keygen"],
            detach=False,
            timeout=2,
        )
        have_ssh_keygen = bool(res and res.returncode == 0 and res.stdout.strip())
    except Exception:
        have_ssh_keygen = False

    if have_ssh_keygen:
        print("[panel_ssh] generating SSH key via ssh-keygen...")
        # -q برای بی‌صدا، -N "" بدون پسورد
        su_env_run(
            ["ssh-keygen", "-t", "ed25519", "-q", "-N", "", "-f", SSH_PRIV_KEY_PATH],
            detach=False,
            timeout=30,
        )
    else:
        print("[panel_ssh] ssh-keygen not found, creating fake ssh keys (ONLY FOR TEST).")
        with open(SSH_PRIV_KEY_PATH, "w", encoding="utf-8") as f:
            f.write("FAKE_SSH_PRIVATE_KEY")
        with open(SSH_PUB_KEY_PATH, "w", encoding="utf-8") as f:
            f.write("ssh-ed25519 FAKE_SSH_PUBLIC_KEY panel_fake\n")


def get_ssh_public_key_text() -> str:
    """
    محتوای فایل public key را برمی‌گرداند (برای ارسال به مرکزی/سرور).
    اگر وجود نداشت، رشته‌ی خالی برمی‌گرداند.
    """
    try:
        with open(SSH_PUB_KEY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def start_reverse_ssh(
    remote_host: str,
    remote_port: int,
    local_port: int = 22,
    remote_user: str = "root",
) -> None:
    """
    یک reverse ssh باز می‌کند:
      ssh -o StrictHostKeyChecking=no -N -R <remote_port>:localhost:<local_port> <user>@<remote_host>

    این تابع non-blocking است (detach=True) و صرفاً پروسس را استارت می‌کند.
    فرض بر این است که private key ssh قبلاً توسط ensure_ssh_key ساخته شده
    و ssh client روی باکس نصب است.
    """
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-N",
        "-R", f"{remote_port}:localhost:{local_port}",
        f"{remote_user}@{remote_host}",
    ]
    print("[panel_ssh] starting reverse ssh:", " ".join(cmd))
    su_env_run(cmd, detach=True)


if __name__ == "__main__":
    from panel_paths import ensure_dirs

    print("=== panel_ssh test ===")
    ensure_dirs()
    ensure_ssh_key()
    pub = get_ssh_public_key_text()
    print("SSH_PUB_KEY_PATH:", SSH_PUB_KEY_PATH)
    print("Public key snippet:", (pub[:60] + "...") if len(pub) > 60 else pub)
    # تست reverse ssh را واقعا نمی‌زنیم، فقط چاپ می‌کنیم:
    print("sample reverse ssh command:")
    print(f"  ssh -o StrictHostKeyChecking=no -N -R 22001:localhost:22 root@1.2.3.4")
    print("✅ panel_ssh basic test OK (برای تست واقعی، آدرس سرور و ssh client لازم است)")

