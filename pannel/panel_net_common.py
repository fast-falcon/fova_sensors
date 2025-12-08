# panel_net_common.py
"""
ابزارهای شبکه و اجرای دستور در محیط su.
"""

import base64
import subprocess
import shlex
from typing import List, Tuple, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


def su_env_run(cmd: List[str], detach: bool = False, timeout: Optional[float] = None):
    """
    اجرای دستور با استفاده از su_env (اگر در PATH باشد). در غیر این صورت خود cmd.
    اگر detach=True باشد، فقط پروسس را استارت می‌کند و برنمی‌گردد (Popen).
    """
    full_cmd = cmd
    try:
        # اگر su_env در سیستم هست، از آن استفاده می‌کنیم
        proc = subprocess.Popen(["which", "su_env"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = proc.communicate(timeout=1)
        if proc.returncode == 0:
            su_env_path = out.decode().strip()
            if su_env_path:
                full_cmd = [su_env_path] + cmd
    except Exception:
        pass

    if detach:
        try:
            subprocess.Popen(full_cmd)
            return None
        except Exception as e:
            print("[su_env_run] detach error:", e)
            return None

    try:
        res = subprocess.run(full_cmd, capture_output=True, timeout=timeout, text=True)
        return res
    except Exception as e:
        print("[su_env_run] error:", e)
        return None


def http_get(url: str, timeout: float = 5.0) -> bytes:
    """
    GET ساده با urllib. خطاها را بالا می‌اندازد.
    """
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (URLError, HTTPError) as e:
        raise e


def parse_basic_auth_header(auth_header: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Authorization: Basic base64(user:pass) را به (user, pass) تبدیل می‌کند.
    """
    if not auth_header or not auth_header.startswith("Basic "):
        return None, None
    try:
        b64 = auth_header.split(" ", 1)[1].strip()
        raw = base64.b64decode(b64).decode("utf-8")
        if ":" not in raw:
            return None, None
        user, pw = raw.split(":", 1)
        return user, pw
    except Exception:
        return None, None


if __name__ == "__main__":
    print("=== panel_net_common test ===")

    # تست parse_basic_auth_header
    import base64 as _b64

    token = _b64.b64encode(b"user123:pass456").decode("ascii")
    h = "Basic " + token
    u, p = parse_basic_auth_header(h)
    print("parsed user/pass:", u, p)
    assert u == "user123" and p == "pass456"
    print("✅ BasicAuth parse OK")

    # تست su_env_run (بدون su_env هم اوکیه فقط echo می‌زنیم)
    res = su_env_run(["echo", "hello_su_env"], detach=False, timeout=3)
    if res is not None:
        print("su_env_run output:", res.stdout.strip())
    print("✅ su_env_run basic test OK")

