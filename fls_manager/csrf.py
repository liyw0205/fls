import secrets

from flask import abort, request, session

from .config import fls_get_admin_token


CSRF_SESSION_KEY = "csrf_token"


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)

    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token

    return token


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
        abort(400, "CSRF token 校验失败，请刷新页面后重试")

    return None
