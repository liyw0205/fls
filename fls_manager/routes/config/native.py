import hashlib
import hmac
import json
import os
import re
from urllib.parse import urlsplit

from flask import current_app, jsonify, request

from .bp import bp
from ...data_access import PanelStorageBusy
from ...config import DEFAULT_CONFIG, THEME_OPTIONS, normalize_config_data, save_config
from ...models import TASKS_MUTATION_LOCK
from ...native_sessions import clear_native_session_cookie, delete_all_native_sessions
from ...paths import CONFIG_REQUESTS_DIR
from ...security import generate_totp_secret, verify_totp
from ...storage import storage_transaction, write_json
from ...utils import now_str
from .page import _apply_revision, _apply_saved_config, _config_apply_state, _config_snapshot, cleanup_logs, reload_scheduler


_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_REVISION_RE = re.compile(r"^[a-f0-9]{64}$")
_TASK_TYPES = tuple(DEFAULT_CONFIG["task_types"])
_SETTING_KEYS = (
    "theme", "port", "online_script_source", "log_cleanup_minutes",
    "log_max_size_mb", "log_keep_per_task", "task_timeout_seconds",
    "random_delay_seconds", "task_types",
)
_LIMITS = {
    "port": (1, 65535),
    "log_cleanup_minutes": (1, 1440),
    "log_max_size_mb": (1, None),
    "log_keep_per_task": (1, None),
    "task_timeout_seconds": (0, None),
    "random_delay_seconds": (0, 120),
}
class ReceiptError(RuntimeError):
    pass


def _response(payload, status=200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _error(message, status=400, **extra):
    return _response({"ok": False, "msg": message, **extra}, status)


def _json_body(max_bytes=256 * 1024):
    if not request.is_json:
        return None, _error("请求必须使用 JSON", 415)
    if request.content_length is not None and request.content_length > max_bytes:
        return None, _error("请求内容超过限制", 413)
    raw = request.stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return None, _error("请求内容超过限制", 413)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _error("JSON 格式无效")
    if not isinstance(value, dict):
        return None, _error("请求字段无效")
    return value, None


def _secret_fingerprint(data):
    key = current_app.secret_key
    key = key.encode("utf-8") if isinstance(key, str) else key
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _receipt_path(request_id):
    return CONFIG_REQUESTS_DIR / (request_id + ".json")


def _read_receipt(request_id):
    path = _receipt_path(request_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise ReceiptError("提交回执无效") from exc
    if (
        not isinstance(record, dict)
        or record.get("request_id") != request_id
        or not {"kind", "state", "fingerprint", "input_revision", "saved_revision", "saved", "apply_status", "reauth_required"}.issubset(record)
        or record.get("kind") not in ("save", "apply")
        or record.get("state") not in ("prepared", "completed", "failed", "unknown")
        or not isinstance(record.get("fingerprint"), str)
        or not _REVISION_RE.fullmatch(record["fingerprint"])
        or not _REVISION_RE.fullmatch(str(record.get("input_revision", "")))
        or not (type(record.get("saved")) is bool or record.get("saved") is None)
        or record.get("apply_status") not in ("pending", "succeeded", "failed", "unknown", None)
        or type(record.get("reauth_required")) is not bool
        or (record.get("saved_revision", "") != "" and not _REVISION_RE.fullmatch(str(record["saved_revision"])))
        or (record.get("saved") is True and not _REVISION_RE.fullmatch(str(record.get("saved_revision", ""))))
        or (record.get("state") == "completed" and record.get("saved") is not True)
        or (record.get("apply_status") in ("succeeded", "failed") and
            (record.get("state") != "completed" or record.get("saved") is not True))
        or (record.get("kind") == "save" and not _REVISION_RE.fullmatch(str(record.get("target_revision", ""))))
    ):
        raise ReceiptError("提交回执无效")
    return record


def _write_receipt(record):
    write_json(_receipt_path(record["request_id"]), record)


def _validate_common(data, allowed):
    if set(data) - allowed:
        raise ValueError
    request_id = data.get("request_id")
    revision = data.get("expected_revision")
    if not isinstance(request_id, str) or not _ID_RE.fullmatch(request_id):
        raise ValueError
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise ValueError
    return request_id, revision


def _source_has_secrets(source):
    try:
        parsed = urlsplit(source)
        return bool(parsed.username or parsed.password or parsed.query or parsed.fragment)
    except ValueError:
        return True


def _settings_payload(cfg, reveal_source=False):
    settings = {key: cfg[key] for key in _SETTING_KEYS}
    source = str(settings["online_script_source"] or "")
    requires_reveal = _source_has_secrets(source)
    if requires_reveal and not reveal_source:
        settings["online_script_source"] = ""
    settings["online_script_source_requires_reveal"] = requires_reveal
    state = _config_apply_state(cfg)
    apply_state = {
        "status": state.get("status", "unknown"),
        "updated_at": state.get("updated_at", ""),
        "revision": state.get("revision", ""),
        "error_count": len(state.get("errors", [])),
        "message": "" if state.get("status") in ("", "succeeded") else "已保存配置尚未确认应用成功，可显式重新应用。",
    }
    return {
        "ok": True,
        "settings": settings,
        "revision": _apply_revision(cfg),
        "schema": _schema(),
        "credentials": {
            "token_configured": bool(cfg.get("admin_token") or os.environ.get("FLS_TOKEN", "").strip()),
            "token_managed_by_environment": bool(os.environ.get("FLS_TOKEN", "").strip()),
            "totp_configured": bool(cfg.get("totp_secret")),
            "security_verify_enabled": bool(cfg.get("security_verify_enabled")),
            "security_verify_type": cfg.get("security_verify_type", "code"),
        },
        "apply": apply_state,
    }


def _schema():
    schema = {
        "theme": {"type": "enum", "options": list(THEME_OPTIONS)},
        "online_script_source": {"type": "string", "max_length": 4096},
        "task_types": {"type": "boolean_map", "keys": list(_TASK_TYPES)},
    }
    for key, (minimum, maximum) in _LIMITS.items():
        schema[key] = {"type": "integer", "minimum": minimum, "maximum": maximum}
    schema["credentials"] = {
        "token_action": ["keep", "replace", "clear"],
        "security_verify_enabled": {"type": "boolean"},
        "security_verify_type": ["code", "totp"],
        "totp_action": ["keep", "replace", "clear"],
        "totp_requires_confirmation": True,
    }
    return schema


def _validate_settings(settings, current):
    if not isinstance(settings, dict) or set(settings) - set(_SETTING_KEYS):
        raise ValueError
    result = {}
    for key, value in settings.items():
        if key == "theme":
            if not isinstance(value, str) or value not in THEME_OPTIONS:
                raise ValueError
            result[key] = value
        elif key in _LIMITS:
            minimum, maximum = _LIMITS[key]
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                raise ValueError
            result[key] = value
        elif key == "online_script_source":
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise ValueError
            result[key] = value.strip() or DEFAULT_CONFIG["online_script_source"]
        elif key == "task_types":
            if not isinstance(value, dict) or not value or set(value) - set(_TASK_TYPES):
                raise ValueError
            if any(type(enabled) is not bool for enabled in value.values()):
                raise ValueError
            result[key] = {**current["task_types"], **value}
    return result


def _validate_credentials(credentials, cfg):
    if not isinstance(credentials, dict) or set(credentials) - {
        "token_action", "admin_token", "enabled", "type", "totp_action", "totp_secret", "totp_code",
    }:
        raise ValueError
    updated = {}
    token_action = credentials.get("token_action", "keep")
    if token_action not in ("keep", "replace", "clear"):
        raise ValueError
    if token_action == "keep":
        if "admin_token" in credentials:
            raise ValueError
    elif token_action == "replace":
        token = credentials.get("admin_token")
        if not isinstance(token, str) or not 1 <= len(token.strip()) <= 4096 or "\x00" in token:
            raise ValueError
        if os.environ.get("FLS_TOKEN", "").strip():
            return None, "environment_token"
        updated["admin_token"] = token.strip()
    else:
        if "admin_token" in credentials:
            raise ValueError
        if os.environ.get("FLS_TOKEN", "").strip():
            return None, "environment_token"
        updated["admin_token"] = ""

    has_security_change = any(key in credentials for key in ("enabled", "type", "totp_action", "totp_secret", "totp_code"))
    if not has_security_change:
        return updated, None
    enabled = credentials.get("enabled", cfg["security_verify_enabled"])
    security_type = credentials.get("type", cfg["security_verify_type"])
    if type(enabled) is not bool or security_type not in ("code", "totp"):
        raise ValueError
    action = credentials.get("totp_action", "keep")
    if action not in ("keep", "replace", "clear"):
        raise ValueError
    if action == "replace":
        secret = credentials.get("totp_secret")
        code = credentials.get("totp_code")
        if not isinstance(secret, str) or not re.fullmatch(r"[A-Z2-7]{16,128}", secret) or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError
        if not verify_totp(secret, code):
            return None, "invalid_totp"
        updated["totp_secret"] = secret
    elif "totp_secret" in credentials or "totp_code" in credentials:
        raise ValueError
    elif action == "clear":
        updated["totp_secret"] = ""

    if enabled and security_type == "totp":
        candidate = updated.get("totp_secret", cfg.get("totp_secret", ""))
        is_new = not cfg.get("security_verify_enabled") or cfg.get("security_verify_type") != "totp" or candidate != cfg.get("totp_secret")
        if not candidate or (is_new and action != "replace"):
            raise ValueError
    else:
        updated["totp_secret"] = ""
        updated["security_verify_type"] = "code"
    updated["security_verify_enabled"] = enabled
    if enabled:
        updated["security_verify_type"] = security_type
    return updated, None


def _apply_summary(state):
    status = state.get("status")
    return {
        "status": status if status in ("succeeded", "failed", "unknown") else "unknown",
        "updated_at": state.get("updated_at", ""),
        "revision": state.get("revision", ""),
        "errors": ["配置应用失败，请显式重试。"] if status == "failed" else [],
    }


def _saved_response(record, replay=False):
    status = 500 if record["saved"] is False else 503 if record["saved"] is None else 200
    return _response({
        "ok": record["saved"] is True,
        "request_id": record["request_id"],
        "saved": record["saved"],
        "state": record.get("state", "unknown"),
        "revision": record.get("saved_revision", ""),
        "apply_status": record.get("apply_status"),
        "applied": record.get("apply_status") == "succeeded",
        "reauth_required": record["reauth_required"],
        "replay": replay,
        "msg": "配置已保存" if record["saved"] else "配置保存结果尚未确认",
    }, status)


def _apply_config(cfg):
    return _apply_saved_config(cfg, cleanup_operation=cleanup_logs, scheduler_operation=reload_scheduler)


def _resolve_prepared_save(record):
    if record["state"] == "completed" and record["apply_status"] == "pending":
        return {**record, "apply_status": "unknown"}
    if record["kind"] != "save" or record["state"] != "prepared":
        return record
    target_revision = record.get("target_revision", "")
    if not _REVISION_RE.fullmatch(str(target_revision)):
        return {**record, "state": "unknown", "saved": None, "apply_status": "unknown"}
    try:
        _cfg, current_revision = _config_snapshot()
    except Exception:
        return {**record, "state": "unknown", "saved": None, "apply_status": "unknown"}
    if current_revision == target_revision:
        return {
            **record,
            "state": "unknown",
            "saved": True,
            "saved_revision": target_revision,
            "apply_status": "unknown",
        }
    if current_revision == record["input_revision"]:
        return {**record, "state": "failed", "saved": False, "apply_status": "unknown"}
    return {**record, "state": "unknown", "saved": None, "apply_status": "unknown"}


@bp.route("/api/config/settings", methods=["GET"])
def native_config_settings():
    reveal = request.args.get("reveal_source", "0")
    if reveal not in ("0", "1"):
        return _error("查看参数无效")
    try:
        cfg, _revision = _config_snapshot()
        return _response(_settings_payload(cfg, reveal_source=reveal == "1"))
    except PanelStorageBusy:
        raise
    except Exception:
        return _error("配置读取失败", 500)


@bp.route("/api/config/settings", methods=["POST"])
def native_config_save():
    data, error = _json_body()
    if error:
        return error
    try:
        request_id, expected_revision = _validate_common(data, {"request_id", "expected_revision", "settings", "credentials"})
        settings = data.get("settings", {})
        credentials = data.get("credentials", {})
        if not isinstance(settings, dict) or not isinstance(credentials, dict) or not (settings or credentials):
            raise ValueError
        fingerprint = _secret_fingerprint(data)
        with TASKS_MUTATION_LOCK, storage_transaction():
            previous = _read_receipt(request_id)
            if previous:
                if previous["fingerprint"] != fingerprint or previous["kind"] != "save":
                    return _error("提交标识已用于其他操作", 409, saved=False)
                return _saved_response(_resolve_prepared_save(previous), replay=True)
            cfg, revision = _config_snapshot()
            if revision != expected_revision:
                return _error("配置已变化，请重新读取并合并修改", 409, conflict=True, current_revision=revision)
            updates = _validate_settings(settings, cfg)
            credential_updates, credential_error = _validate_credentials(credentials, cfg)
            if credential_error == "environment_token":
                return _error("Token 由服务环境管理，不能通过面板配置修改", 409, saved=False)
            if credential_error == "invalid_totp":
                return _error("TOTP 验证码无效，未保存", 400, saved=False)
            updates.update(credential_updates or {})
            old_token = cfg.get("admin_token", "")
            new_token = updates.get("admin_token", old_token)
            auth_keys = ("security_verify_enabled", "security_verify_type", "totp_secret")
            auth_changed = old_token != new_token or any(
                updates.get(key, cfg.get(key)) != cfg.get(key) for key in auth_keys
            )
            record = {
                "request_id": request_id, "kind": "save", "fingerprint": fingerprint,
                "state": "prepared", "input_revision": expected_revision,
                "target_revision": _apply_revision(normalize_config_data({**cfg, **updates})),
                "saved_revision": "", "saved": None, "apply_status": "pending",
                "reauth_required": auth_changed, "updated_at": now_str(),
            }
            _write_receipt(record)
            try:
                save_config(updates)
                saved_cfg, saved_revision = _config_snapshot()
            except Exception:
                resolved = _resolve_prepared_save(record)
                record.update(
                    state=resolved["state"], saved=resolved["saved"],
                    saved_revision=resolved.get("saved_revision", ""),
                    apply_status="unknown", updated_at=now_str(),
                )
                _write_receipt(record)
                return _saved_response(record)
            if saved_revision != record["target_revision"]:
                record.update(state="unknown", saved=None, apply_status="unknown", updated_at=now_str())
                _write_receipt(record)
                return _saved_response(record)
            record.update(state="completed", saved=True, saved_revision=saved_revision, updated_at=now_str())
            if auth_changed:
                delete_all_native_sessions()
                from ...auth import auth_clear_session
                auth_clear_session()
            _write_receipt(record)
            try:
                apply_state = _apply_config(saved_cfg)
            except Exception:
                apply_state = {"status": "unknown", "errors": []}
            record["apply_status"] = _apply_summary(apply_state)["status"]
            record["updated_at"] = now_str()
            try:
                _write_receipt(record)
            except Exception:
                record["apply_status"] = "unknown"
        result, status = _saved_response(record)
        if record["reauth_required"]:
            clear_native_session_cookie(result)
        return result, status
    except (ValueError, TypeError, KeyError):
        return _error("配置字段无效，请检查类型和范围", 400, saved=False)
    except PanelStorageBusy:
        raise
    except Exception:
        return _error("配置保存结果无法确认，请查询原提交状态", 503, saved=None)


@bp.route("/api/config/totp/enrollment", methods=["POST"])
def native_config_totp_enrollment():
    data, error = _json_body(16 * 1024)
    if error:
        return error
    if data:
        return _error("请求字段无效")
    try:
        secret = generate_totp_secret()
        return _response({"ok": True, "secret": secret})
    except Exception:
        return _error("TOTP 密钥生成失败", 500)


def _receipt_payload(record):
    return {
        "ok": True, "request_id": record["request_id"],
        "saved": record["saved"], "state": record["state"],
        "apply_status": record["apply_status"],
        "revision": record.get("saved_revision", ""),
        "reauth_required": record["reauth_required"],
    }


@bp.route("/api/config/requests/<request_id>", methods=["GET"])
def native_config_request(request_id):
    if not _ID_RE.fullmatch(request_id):
        return _error("提交标识无效")
    try:
        with storage_transaction():
            record = _read_receipt(request_id)
            if record is None:
                return _response({"ok": True, "request_id": request_id, "state": "missing", "saved": None})
            return _response(_receipt_payload(_resolve_prepared_save(record)))
    except PanelStorageBusy:
        raise
    except Exception:
        return _error("提交回执损坏或无法确认", 503, request_id=request_id, state="unknown", saved=None)


def apply_json_config():
    data, error = _json_body()
    if error:
        return error
    try:
        request_id, expected_revision = _validate_common(data, {"request_id", "expected_revision"})
        fingerprint = _secret_fingerprint(data)
        with TASKS_MUTATION_LOCK, storage_transaction():
            previous = _read_receipt(request_id)
            if previous:
                if previous["fingerprint"] != fingerprint or previous["kind"] != "apply":
                    return _error("提交标识已用于其他操作", 409, saved=False)
                return _response({**_receipt_payload(previous), "applied": previous["apply_status"] == "succeeded", "replay": True})
            cfg, revision = _config_snapshot()
            if revision != expected_revision:
                return _error("配置已变化，请重新读取后再应用", 409, conflict=True, saved=True, current_revision=revision)
            record = {
                "request_id": request_id, "kind": "apply", "fingerprint": fingerprint,
                "state": "prepared", "input_revision": expected_revision,
                "saved_revision": revision, "saved": True, "apply_status": "unknown",
                "reauth_required": False, "updated_at": now_str(),
            }
            _write_receipt(record)
            try:
                state = _apply_config(cfg)
                record["apply_status"] = _apply_summary(state)["status"]
                record["state"] = "completed"
            except Exception:
                record["apply_status"] = "unknown"
                record["state"] = "unknown"
            record["updated_at"] = now_str()
            _write_receipt(record)
            payload = _receipt_payload(record)
            payload.update(applied=record["apply_status"] == "succeeded", replay=False)
            return _response(payload)
    except (ValueError, TypeError, KeyError):
        return _error("应用请求字段无效", 400, saved=True)
    except PanelStorageBusy:
        raise
    except Exception:
        return _error("配置应用结果无法确认，请查询原提交状态", 503, saved=None)
