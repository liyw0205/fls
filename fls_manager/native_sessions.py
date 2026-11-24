import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

from flask import current_app, request

from .paths import DATA_DIR


NATIVE_SESSION_COOKIE = "fls_native_session"
NATIVE_SESSION_SECONDS = 3600
NATIVE_SESSION_REMEMBER_SECONDS = 14 * 24 * 3600
NATIVE_SESSION_FILE = DATA_DIR / "native_sessions.sqlite3"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


def _token_fingerprint(token):
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key, str(token).encode("utf-8"), hashlib.sha256).hexdigest()


def _session_hash(session_id):
    return hashlib.sha256(session_id.encode("ascii")).hexdigest()


def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(NATIVE_SESSION_FILE), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS native_sessions (
            session_hash TEXT PRIMARY KEY,
            token_fingerprint TEXT NOT NULL,
            csrf_token TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            verified INTEGER NOT NULL,
            remember INTEGER NOT NULL
        )
        """
    )
    try:
        os.chmod(NATIVE_SESSION_FILE, 0o600)
    except OSError:
        pass
    return connection


@contextmanager
def _database():
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def create_native_session(token, remember=False, verified=False):
    now = int(time.time())
    max_age = (
        NATIVE_SESSION_REMEMBER_SECONDS if remember else NATIVE_SESSION_SECONDS
    )
    session_id = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    session_hash = _session_hash(session_id)

    with _database() as connection:
        connection.execute(
            "DELETE FROM native_sessions WHERE expires_at <= ?",
            (now,),
        )
        connection.execute(
            """
            INSERT INTO native_sessions (
                session_hash, token_fingerprint, csrf_token,
                expires_at, verified, remember
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_hash,
                _token_fingerprint(token),
                csrf,
                now + max_age,
                int(bool(verified)),
                int(bool(remember)),
            ),
        )
    return session_id, csrf, max_age


def native_session_from_request(token=None):
    session_id = request.cookies.get(NATIVE_SESSION_COOKIE, "")
    if not _SESSION_ID_RE.fullmatch(session_id):
        return None

    session_hash = _session_hash(session_id)
    with _database() as connection:
        row = connection.execute(
            "SELECT * FROM native_sessions WHERE session_hash = ?",
            (session_hash,),
        ).fetchone()
        if row is None:
            return None

        if int(row["expires_at"]) <= int(time.time()):
            connection.execute(
                "DELETE FROM native_sessions WHERE session_hash = ?",
                (session_hash,),
            )
            return None

        if token is not None and not hmac.compare_digest(
            row["token_fingerprint"], _token_fingerprint(token)
        ):
            connection.execute(
                "DELETE FROM native_sessions WHERE session_hash = ?",
                (session_hash,),
            )
            return None

        return {
            "session_id": session_id,
            "session_hash": session_hash,
            "csrf_token": row["csrf_token"],
            "expires_at": int(row["expires_at"]),
            "verified": bool(row["verified"]),
            "remember": bool(row["remember"]),
        }


def mark_native_session_verified(native_session, token):
    if not native_session:
        return False
    now = int(time.time())
    with _database() as connection:
        cursor = connection.execute(
            """
            UPDATE native_sessions
            SET verified = 1
            WHERE session_hash = ? AND token_fingerprint = ? AND expires_at > ?
            """,
            (
                native_session["session_hash"],
                _token_fingerprint(token),
                now,
            ),
        )
        return cursor.rowcount == 1


def delete_native_session(session_id):
    if not _SESSION_ID_RE.fullmatch(str(session_id or "")):
        return
    with _database() as connection:
        connection.execute(
            "DELETE FROM native_sessions WHERE session_hash = ?",
            (_session_hash(session_id),),
        )


def set_native_session_cookie(response, session_id, max_age=None):
    response.set_cookie(
        NATIVE_SESSION_COOKIE,
        session_id,
        max_age=max_age,
        httponly=True,
        secure=request.is_secure,
        samesite="Lax",
        path="/",
    )
    return response


def clear_native_session_cookie(response):
    response.delete_cookie(
        NATIVE_SESSION_COOKIE,
        httponly=True,
        secure=request.is_secure,
        samesite="Lax",
        path="/",
    )
    return response
