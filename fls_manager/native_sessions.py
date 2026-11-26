import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager

from flask import current_app, request

from .paths import DATA_DIR
from .config import load_config
from .data_access import directory_access, PanelStorageBusy
from .storage import storage_transaction


NATIVE_SESSION_COOKIE = "fls_native_session"
NATIVE_SESSION_SECONDS = 3600
NATIVE_SESSION_REMEMBER_SECONDS = 14 * 24 * 3600
NATIVE_SESSION_FILE = DATA_DIR / "native_sessions.sqlite3"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
NATIVE_SESSION_LOCK = threading.RLock()


def _token_fingerprint(token):
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode("utf-8")
    cfg = load_config()
    credentials = [str(token), cfg["security_verify_enabled"],
                   cfg["security_verify_type"], cfg["totp_secret"]]
    return hmac.new(key, json.dumps(credentials).encode("utf-8"), hashlib.sha256).hexdigest()


def _session_hash(session_id):
    return hashlib.sha256(session_id.encode("ascii")).hexdigest()


def _connect(*, read_only=False):
    if read_only:
        connection = sqlite3.connect(NATIVE_SESSION_FILE.absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(NATIVE_SESSION_FILE), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
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
    except BaseException:
        connection.close()
        raise
    try:
        os.chmod(NATIVE_SESSION_FILE, 0o600)
    except OSError:
        pass
    return connection


@contextmanager
def _database(*, read_only=False):
    # Fingerprint reads and configuration changes share storage -> session order.
    with directory_access(write=not read_only), storage_transaction(live_data=False), NATIVE_SESSION_LOCK:
        if read_only and not NATIVE_SESSION_FILE.exists():
            yield None
            return
        connection = _connect(read_only=read_only)
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
        fingerprint = _token_fingerprint(token)
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
                fingerprint,
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
    with _database(read_only=True) as connection:
        if connection is None:
            return None
        fingerprint = _token_fingerprint(token) if token is not None else None
        row = connection.execute(
            "SELECT * FROM native_sessions WHERE session_hash = ?",
            (session_hash,),
        ).fetchone()
        if row is None:
            return None

        invalid = int(row["expires_at"]) <= int(time.time()) or (
            token is not None and not hmac.compare_digest(row["token_fingerprint"], fingerprint)
        )
        if not invalid:
            return {
                "session_id": session_id,
                "session_hash": session_hash,
                "csrf_token": row["csrf_token"],
                "expires_at": int(row["expires_at"]),
                "verified": bool(row["verified"]),
                "remember": bool(row["remember"]),
            }

    # Invalid sessions stay invalid if credentials are later changed back.
    try:
        with _database() as connection:
            connection.execute(
                "DELETE FROM native_sessions WHERE session_hash = ? AND token_fingerprint = ? AND expires_at = ?",
                (session_hash, row["token_fingerprint"], row["expires_at"]),
            )
    except PanelStorageBusy:
        pass
    return None


def mark_native_session_verified(native_session, token):
    if not native_session:
        return False
    now = int(time.time())
    with _database() as connection:
        fingerprint = _token_fingerprint(token)
        cursor = connection.execute(
            """
            UPDATE native_sessions
            SET verified = 1
            WHERE session_hash = ? AND token_fingerprint = ? AND expires_at > ?
            """,
            (
                native_session["session_hash"],
                fingerprint,
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


def delete_all_native_sessions():
    with _database() as connection:
        connection.execute("DELETE FROM native_sessions")


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
