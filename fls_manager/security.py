import os
import json
import time
import hmac
import base64
import struct
import hashlib
import secrets
import urllib.parse
from pathlib import Path

from .paths import DATA_DIR
from .config import load_config


FLS_CODE_FILE = DATA_DIR / "fls_code.json"
FLS_CODE_EXPIRE_SECONDS = 300


def security_config():
    cfg = load_config()

    return {
        "enabled": bool(cfg.get("security_verify_enabled", False)),
        "type": str(cfg.get("security_verify_type", "code") or "code").strip(),
        "totp_secret": str(cfg.get("totp_secret", "") or "").strip(),
    }


def security_enabled():
    return bool(security_config().get("enabled"))


def security_type():
    t = security_config().get("type") or "code"

    if t not in ("code", "totp"):
        t = "code"

    return t


def random_code_file():
    return FLS_CODE_FILE


def generate_random_code():
    """
    生成 6 位随机验证码。
    """
    return f"{secrets.randbelow(1000000):06d}"


def save_random_code(code, ip="", ua=""):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    now = int(time.time())

    data = {
        "code": str(code),
        "created_at": now,
        "expire_at": now + FLS_CODE_EXPIRE_SECONDS,
        "ip": ip,
        "ua": ua,
    }

    FLS_CODE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    try:
        os.chmod(FLS_CODE_FILE, 0o600)
    except Exception:
        pass

    return data


def load_random_code():
    try:
        if not FLS_CODE_FILE.exists():
            return {}

        data = json.loads(FLS_CODE_FILE.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            return {}

        return data

    except Exception:
        return {}


def random_code_valid(input_code):
    input_code = str(input_code or "").strip()
    data = load_random_code()

    code = str(data.get("code", "") or "").strip()
    expire_at = int(data.get("expire_at", 0) or 0)

    if not code:
        return False, "验证码不存在，请重新登录获取"

    if int(time.time()) > expire_at:
        return False, "验证码已过期，请重新登录获取"

    if not hmac.compare_digest(input_code, code):
        return False, "验证码错误"

    return True, "验证成功"


def clear_random_code():
    try:
        if FLS_CODE_FILE.exists():
            FLS_CODE_FILE.unlink()
    except Exception:
        pass


def generate_totp_secret():
    """
    生成 TOTP Base32 密钥。
    """
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("utf-8").replace("=", "")


def normalize_totp_secret(secret):
    secret = str(secret or "").strip().replace(" ", "").upper()

    if not secret:
        return ""

    padding = "=" * ((8 - len(secret) % 8) % 8)
    return secret + padding


def hotp(secret, counter, digits=6):
    key = base64.b32decode(normalize_totp_secret(secret), casefold=True)
    msg = struct.pack(">Q", int(counter))

    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F

    code = struct.unpack(">I", digest[offset:offset + 4])[0]
    code = code & 0x7FFFFFFF
    code = code % (10 ** digits)

    return str(code).zfill(digits)


def totp_now(secret, for_time=None, interval=30, digits=6):
    if for_time is None:
        for_time = int(time.time())

    counter = int(for_time // interval)
    return hotp(secret, counter, digits=digits)


def verify_totp(secret, code, window=1):
    """
    验证 TOTP。

    window=1 表示允许前后 30 秒误差。
    """
    secret = str(secret or "").strip()
    code = str(code or "").strip().replace(" ", "")

    if not secret:
        return False

    if not code.isdigit():
        return False

    now = int(time.time())

    for i in range(-int(window), int(window) + 1):
        t = now + i * 30
        expected = totp_now(secret, for_time=t)

        if hmac.compare_digest(expected, code):
            return True

    return False


def totp_otpauth_uri(secret, account="FLS Panel", issuer="FLS"):
    label = urllib.parse.quote(f"{issuer}:{account}")
    issuer_q = urllib.parse.quote(issuer)

    return (
        f"otpauth://totp/{label}"
        f"?secret={urllib.parse.quote(secret)}"
        f"&issuer={issuer_q}"
        f"&algorithm=SHA1"
        f"&digits=6"
        f"&period=30"
    )


def totp_qr_url(secret, account="FLS Panel", issuer="FLS"):
    """
    二维码图片地址。

    这里使用在线二维码服务生成二维码。
    如果你不想依赖外网，可以只显示 otpauth 链接和密钥。
    """
    uri = totp_otpauth_uri(secret, account=account, issuer=issuer)

    return (
        "https://api.qrserver.com/v1/create-qr-code/"
        "?size=220x220&data=" + urllib.parse.quote(uri)
    )