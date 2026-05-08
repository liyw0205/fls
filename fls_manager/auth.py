import time
from datetime import timedelta
from urllib.parse import quote, urlencode

from flask import request, redirect, url_for, session, jsonify, current_app

from .config import fls_get_admin_token
from .security import security_enabled

FLS_AUTH_SHORT_SECONDS = 3600
FLS_AUTH_REMEMBER_SECONDS = 7 * 24 * 3600


def _is_api_request():
    path = request.path or ""

    if path.startswith("/api/"):
        return True

    accept = request.headers.get("Accept", "")
    xhr = request.headers.get("X-Requested-With", "")

    return "application/json" in accept or xhr in ("XMLHttpRequest", "FLS-Ajax")


def auth_set_session(app, token, remember=False, security_verified=True):
    now_ts = int(time.time())
    max_age = FLS_AUTH_REMEMBER_SECONDS if remember else FLS_AUTH_SHORT_SECONDS

    session.clear()
    session["token"] = token
    session["token_expire_at"] = now_ts + max_age
    session["remember_login"] = bool(remember)
    session["security_verified"] = bool(security_verified)
    session.permanent = bool(remember)

    app.permanent_session_lifetime = timedelta(seconds=max_age)


def auth_clear_session():
    session.clear()


def auth_session_valid(token):
    try:
        saved = session.get("token")
        exp = int(session.get("token_expire_at", 0) or 0)

        if saved != token:
            return False

        if exp <= int(time.time()):
            auth_clear_session()
            return False

        return True

    except Exception:
        auth_clear_session()
        return False


def auth_security_verified():
    if not security_enabled():
        return True

    return bool(session.get("security_verified"))


def auth_before_request():
    endpoint = request.endpoint or ""

    if endpoint in (
        "auth.login",
        "auth.logout",
        "auth.setup",
        "auth.verify",
        "auth.resend_code",
    ):
        return None

    token = fls_get_admin_token()

    if not token:
        if _is_api_request():
            return jsonify({
                "ok": False,
                "msg": "面板尚未设置 Token，请访问 /setup",
            }), 403

        return redirect(url_for("auth.setup"))

    header_token = request.headers.get("X-Token", "")
    if header_token and header_token == token:
        return None

    arg_token = request.args.get("token", "")
    if arg_token:
        if arg_token == token:
            auth_set_session(
                current_app,
                token,
                remember=False,
                security_verified=not security_enabled(),
            )

            clean_args = dict(request.args)
            clean_args.pop("token", None)

            clean_url = request.path
            if clean_args:
                clean_url += "?" + urlencode(clean_args, doseq=True)

            return redirect(clean_url)

        if _is_api_request():
            return jsonify({
                "ok": False,
                "msg": "Token 错误",
            }), 403

        return redirect(url_for("auth.login"))

    if auth_session_valid(token):
        if not auth_security_verified():
            if _is_api_request():
                return jsonify({
                    "ok": False,
                    "msg": "需要安全验证",
                }), 401

            return redirect(url_for("auth.verify"))

        return None

    if _is_api_request():
        return jsonify({
            "ok": False,
            "msg": "登录已过期",
        }), 401

    next_url = request.full_path if request.query_string else request.path
    return redirect(url_for("auth.login") + "?next=" + quote(next_url))