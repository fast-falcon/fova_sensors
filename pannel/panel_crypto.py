# panel_crypto.py
"""
رمزنگاری هیبریدی روی باکس‌ها (RSA + AES-GCM)

- ensure_box_keypair(): ساخت کلید RSA
- load_box_public_key()
- encrypt_with_public_key(pem, plaintext): خروجی bytes (JSON)
- decrypt_with_private_key(priv_path, cipher_bytes): برعکس بالا

این ساختار هم برای ارسال سنسور→مرکزی و هم مرکزی→سرور استفاده می‌شود.
"""

import os
import json
import base64
from typing import Optional

from panel_paths import CRYPTO_PRIV_KEY, CRYPTO_PUB_KEY

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_OAEP, AES
    from Crypto.Random import get_random_bytes
except ImportError:
    RSA = None
    PKCS1_OAEP = None
    AES = None
    get_random_bytes = None


def ensure_box_keypair() -> None:
    """
    اگر کلید RSA برای این باکس وجود نداشته باشد، می‌سازد.
    """
    if os.path.exists(CRYPTO_PRIV_KEY) and os.path.exists(CRYPTO_PUB_KEY):
        return

    os.makedirs(os.path.dirname(CRYPTO_PRIV_KEY), exist_ok=True)

    if RSA is None:
        # حالت بدون PyCryptodome - فقط برای تست.
        with open(CRYPTO_PRIV_KEY, "w", encoding="utf-8") as f:
            f.write("FAKE_PRIVATE_KEY")
        with open(CRYPTO_PUB_KEY, "w", encoding="utf-8") as f:
            f.write("FAKE_PUBLIC_KEY")
        print("[panel_crypto] PyCryptodome not installed, fake keys created.")
        return

    key = RSA.generate(2048)
    with open(CRYPTO_PRIV_KEY, "wb") as f:
        f.write(key.export_key())
    with open(CRYPTO_PUB_KEY, "wb") as f:
        f.write(key.publickey().export_key())
    print("[panel_crypto] RSA keypair generated.")


def load_box_public_key() -> bytes:
    """
    کلید عمومی این باکس (برای ارسال به بقیه) را برمی‌گرداند.
    """
    with open(CRYPTO_PUB_KEY, "rb") as f:
        return f.read()


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def encrypt_with_public_key(peer_public_pem: bytes, plaintext: bytes) -> bytes:
    """
    plaintext را با یک AES-GCM تصادفی و سپس با RSA-OAEP روی کلید AES رمز می‌کند.
    خروجی: bytes حاوی JSON (برای ارسال روی سوکت یا HTTP)
    """
    if RSA is None or PKCS1_OAEP is None or AES is None or get_random_bytes is None:
        # حالت fallback بدون رمزنگاری (فقط برای تست محیطی بدون PyCryptodome)
        blob = {"alg": "NONE", "data": _b64e(plaintext)}
        return json.dumps(blob).encode("utf-8")

    rsa_key = RSA.import_key(peer_public_pem)
    cipher_rsa = PKCS1_OAEP.new(rsa_key)

    aes_key = get_random_bytes(32)  # AES-256
    cipher_aes = AES.new(aes_key, AES.MODE_GCM)
    ciphertext, tag = cipher_aes.encrypt_and_digest(plaintext)

    enc_key = cipher_rsa.encrypt(aes_key)

    blob = {
        "alg": "RSA+AES-GCM",
        "k": _b64e(enc_key),
        "n": _b64e(cipher_aes.nonce),
        "t": _b64e(tag),
        "c": _b64e(ciphertext),
    }
    return json.dumps(blob).encode("utf-8")


def decrypt_with_private_key(priv_key_path: str, cipher_bytes: bytes) -> bytes:
    """
    cipher_bytes که خروجی encrypt_with_public_key است را decrypt می‌کند.
    """
    try:
        blob = json.loads(cipher_bytes.decode("utf-8"))
    except Exception:
        return cipher_bytes

    alg = blob.get("alg")
    if alg == "NONE":
        return _b64d(blob["data"])

    if alg != "RSA+AES-GCM":
        return cipher_bytes

    if RSA is None or PKCS1_OAEP is None or AES is None:
        return cipher_bytes

    enc_key = _b64d(blob["k"])
    nonce = _b64d(blob["n"])
    tag = _b64d(blob["t"])
    ciphertext = _b64d(blob["c"])

    with open(priv_key_path, "rb") as f:
        priv_pem = f.read()
    rsa_key = RSA.import_key(priv_pem)
    cipher_rsa = PKCS1_OAEP.new(rsa_key)
    aes_key = cipher_rsa.decrypt(enc_key)

    cipher = AES.new(aes_key, AES.MODE_GCM, nonce=nonce)
    plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    return plaintext


def encode_ciphertext(cipher: bytes) -> str:
    """
    برای ارسال روی JSON، خروجی encrypt_with_public_key را base64 می‌کند.
    """
    return _b64e(cipher)


def decode_ciphertext(s: str) -> bytes:
    """
    برعکس encode_ciphertext.
    """
    return _b64d(s)


if __name__ == "__main__":
    print("=== panel_crypto test ===")
    from panel_paths import CRYPTO_PRIV_KEY, CRYPTO_PUB_KEY, ensure_dirs

    ensure_dirs()
    ensure_box_keypair()

    print("CRYPTO_PRIV_KEY:", CRYPTO_PRIV_KEY)
    print("CRYPTO_PUB_KEY :", CRYPTO_PUB_KEY)

    pub = load_box_public_key()
    msg = b"hello crypto!"
    cipher_bytes = encrypt_with_public_key(pub, msg)

    print("cipher length:", len(cipher_bytes))
    plain = decrypt_with_private_key(CRYPTO_PRIV_KEY, cipher_bytes)
    print("decrypted:", plain)
    assert plain == msg, "decrypted != original"
    print("✅ panel_crypto OK")

