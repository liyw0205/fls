import secrets

from flask import abort, jsonify, request, session

from .config import fls_get_admin_token


CSRF_SESSION_KEY = "csrf_token"


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)

    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token

    return token


def _wants_json_response():
    if request.path.startswith("/api/"):
        return True

    accept = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept:
        return True

    requested_with = (request.headers.get("X-Requested-With") or "").lower()
    if requested_with == "xmlhttprequest":
        return True

    content_type = (request.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type:
        return True

    return False


def csrf_before_request():
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return None

    header_token = request.headers.get("X-Token", "")
    if header_token and header_token == fls_get_admin_token():
        return None

    expected = session.get(CSRF_SESSION_KEY)
    submitted = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or ""
    )

    if not expected or not submitted or not secrets.compare_digest(str(expected), str(submitted)):
        msg = "CSRF token 校验失败，请刷新页面后重试"
        if _wants_json_response():
            return jsonify({"ok": False, "msg": msg}), 400
        abort(400, msg)

    return None
