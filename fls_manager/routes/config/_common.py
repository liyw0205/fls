from .bp import bp
from flask import request, redirect, url_for

from ...config import load_config, save_config, get_port
from ...scheduler import reload_scheduler
from ...logs import cleanup_logs
from ...utils import h
from ...ui.layout import layout
from ...auth import auth_clear_session
from ...security import (
    generate_totp_secret,
    verify_totp,
    totp_qr_url,
    totp_otpauth_uri,
    random_code_file,
)

def safe_int(value, default=0, min_value=None, max_value=None):
    try:
        n = int(value if value is not None and value != "" else default)
    except Exception:
        n = int(default)

    if min_value is not None:
        n = max(int(min_value), n)

    if max_value is not None:
        n = min(int(max_value), n)

    return n
