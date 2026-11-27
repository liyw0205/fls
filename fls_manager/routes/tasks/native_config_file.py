"""Task-bound UTF-8 editing with durable, content-free submission receipts."""

import hashlib
import hmac
import json
import os
import re
import uuid

from flask import current_app, jsonify, request

from . import bp, config_file
from .helpers import task_config_safe_path
from ...data_access import PanelStorageBusy
from ...directory_jobs import directory_job_operation
from ...models import TASKS_MUTATION_LOCK, get_task
from ...paths import BASE_DIR
from ...script_rename import pending_script_rename
from ...storage import storage_transaction
from ...task_file_coordination import TASK_FILES_LOCK
from ...utils import now_str


REQUESTS_DIR = BASE_DIR / ".task_config_requests"
ID_RE = re.compile(r"^[a-f0-9]{32}$")
REVISION_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_REQUEST_BYTES = 6 * 1024 * 1024 + 8192


class ConfigRequestError(ValueError):
    pass


class ConfigReceiptError(RuntimeError):
    pass


def _response(payload, status=200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _error(message, status=400, code="invalid_request", **extra):
    return _response({"ok": False, "msg": message, "code": code, **extra}, status)


def _fingerprint(value):
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode("utf-8")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _receipt_path(request_id):
    if REQUESTS_DIR.is_symlink():
        raise ConfigReceiptError("配置提交回执目录无效")
    return REQUESTS_DIR / (request_id + ".json")


def _read_receipt(request_id):
    path = _receipt_path(request_id)
    if path.is_symlink():
        raise ConfigReceiptError("配置提交回执无效")
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
    except FileNotFoundError:
        return None
    try:
        if len(raw) > 8192:
            raise ValueError
        row = json.loads(raw)
        signature = row.pop("signature")
        if (
            set(row) != {"request_id", "task_id", "path", "fingerprint", "state", "created_at", "cleanup_required"}
            or row["request_id"] != request_id
            or not all(isinstance(row[key], str) for key in row if key != "cleanup_required")
            or type(row["cleanup_required"]) is not bool
            or row["state"] not in ("prepared", "saved", "not_saved")
            or not REVISION_RE.fullmatch(row["fingerprint"])
            or not isinstance(signature, str)
            or not hmac.compare_digest(signature, _fingerprint(row))
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ConfigReceiptError("配置提交回执无效，不能重新提交原请求") from exc
    return row


def _write_receipt(row):
    REQUESTS_DIR.mkdir(mode=0o700, exist_ok=True)
    path = _receipt_path(row["request_id"])
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    encoded = json.dumps({**row, "signature": _fingerprint(row)}, ensure_ascii=True).encode("utf-8")
    if len(encoded) > 8192:
        raise ConfigReceiptError("配置提交回执超过大小限制")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(REQUESTS_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def _result(task_id, request_id, row):
    state = row["state"] if row else "missing"
    state = "unknown" if state == "prepared" else state
    return {
        "ok": True, "task_id": task_id, "request_id": request_id,
        "status": state, "saved": True if state == "saved" else False if state == "not_saved" else None,
        "path": row["path"] if row else "", "created_at": row["created_at"] if row else "",
        "cleanup_required": row["cleanup_required"] if row else False,
    }


def _snapshot(task_id):
    task = get_task(task_id)
    if not task:
        return None, None, _error("任务不存在", 404, "task_not_found")
    path = str(task.get("config_path") or "").strip()
    if not path:
        return None, None, _error("任务尚未设置配置文件路径", 400, "config_path_missing")
    target = task_config_safe_path(path)
    if pending_script_rename(task_id=task_id, path=target):
        return None, None, _error("相关脚本重命名待恢复，请先处理原重命名", 409, "rename_pending")
    content, revision, metadata = config_file._read_config_snapshot(target, path)
    return {
        "task_id": task_id, "path": path, "content": content,
        "revision": revision, "exists": metadata is not None,
        "max_bytes": config_file.MAX_CONFIG_BYTES,
    }, (target, metadata), None


def _body():
    if not request.is_json:
        raise ConfigRequestError("请求必须使用 JSON")
    if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
        raise config_file.TaskConfigLimitError("配置请求超过限制")
    raw = request.stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise config_file.TaskConfigLimitError("配置请求超过限制")

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ConfigRequestError("JSON 字段重复")
            result[key] = value
        return result

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ConfigRequestError("JSON 格式无效") from exc
    if not isinstance(data, dict) or set(data) != {"request_id", "path", "expected_revision", "content"}:
        raise ConfigRequestError("配置请求字段无效")
    if not all(isinstance(value, str) for value in data.values()):
        raise ConfigRequestError("配置请求字段无效")
    if not ID_RE.fullmatch(data["request_id"]) or not REVISION_RE.fullmatch(data["expected_revision"]):
        raise ConfigRequestError("配置请求身份或版本无效")
    try:
        if len(data["content"]) > config_file.MAX_CONFIG_BYTES or len(data["content"].encode("utf-8")) > config_file.MAX_CONFIG_BYTES:
            raise config_file.TaskConfigLimitError("配置内容超过 1 MiB 编辑限制")
    except UnicodeError as exc:
        raise ConfigRequestError("配置内容不是有效 UTF-8 文本") from exc
    return data


@bp.route("/api/tasks/<task_id>/config-file", methods=["GET", "POST"])
@directory_job_operation
def api_task_config_file(task_id):
    prepared = None
    published = False
    try:
        data = _body() if request.method == "POST" else None
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK, storage_transaction():
            if data is not None:
                previous = _read_receipt(data["request_id"])
                fingerprint = _fingerprint({"task_id": task_id, **data})
                if previous:
                    if previous["task_id"] != task_id or previous["fingerprint"] != fingerprint:
                        return _error("原请求已绑定其他配置内容，不能复用", 409, "request_mismatch")
                    return _response(_result(task_id, data["request_id"], previous))
            snapshot, file_state, error = _snapshot(task_id)
            if error:
                return error
            if data is None:
                return _response({"ok": True, "file": snapshot})
            if data["path"] != snapshot["path"] or data["expected_revision"] != snapshot["revision"]:
                return _error("任务配置已变更，请对照当前内容合并草稿", 409, "revision_conflict", saved=False, current=snapshot)
            prepared = {
                "request_id": data["request_id"], "task_id": task_id, "path": snapshot["path"],
                "fingerprint": fingerprint, "state": "prepared", "created_at": now_str(), "cleanup_required": False,
            }
            _write_receipt(prepared)
            target, metadata = file_state
            try:
                config_file._save_config_atomic(target, snapshot["path"], data["content"], snapshot["revision"], metadata)
                published = True
            except config_file.TaskConfigSaveError as exc:
                published = exc.saved
                raise
            prepared["state"] = "saved"
            _write_receipt(prepared)
            return _response(_result(task_id, data["request_id"], prepared))
    except PanelStorageBusy:
        raise
    except config_file.TaskConfigLimitError:
        return _error("配置超过 1 MiB 编辑限制", 413, "file_too_large", saved=False)
    except config_file.TaskConfigConflict:
        if prepared:
            prepared["state"] = "not_saved"
            try:
                _write_receipt(prepared)
            except OSError:
                return _error("提交结果待核对，请查询原请求", 503, "request_unknown")
        return _error("配置在保存期间已变更，请重新读取并合并草稿", 409, "revision_conflict", saved=False)
    except ConfigRequestError as exc:
        return _error(str(exc), 400, "invalid_request", saved=False)
    except ConfigReceiptError:
        return _error("配置提交回执无法核对，不能重新发送原请求", 503, "request_unavailable")
    except UnicodeError:
        return _error("配置文件不是 UTF-8 文本", 415, "not_utf8_text", saved=False)
    except ValueError:
        return _error("配置文件路径无效或不是普通文件", 400, "invalid_config_path", saved=False)
    except OSError:
        if prepared and not published:
            prepared["state"] = "not_saved"
            try:
                _write_receipt(prepared)
                return _error("配置未保存，请检查文件权限和存储空间", 500, "save_failed", saved=False)
            except OSError:
                pass
        return _error("配置读取或提交回执失败，请核对原请求", 503, "request_unknown")
    except config_file.TaskConfigSaveError:
        if prepared:
            prepared["state"] = "saved" if published else "not_saved"
            prepared["cleanup_required"] = True
            try:
                _write_receipt(prepared)
                return _response(_result(task_id, prepared["request_id"], prepared))
            except OSError:
                pass
        return _error("配置临时文件清理失败，请查询原请求", 503, "request_unknown")
    except RuntimeError:
        return _error("任务配置存储状态无法核对，请检查面板存储", 503, "storage_unavailable")


@bp.route("/api/tasks/<task_id>/config-file/requests/<request_id>")
@directory_job_operation
def api_task_config_request(task_id, request_id):
    if not ID_RE.fullmatch(request_id):
        return _error("提交 ID 无效")
    try:
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            row = _read_receipt(request_id)
            if row and row["task_id"] != task_id:
                return _error("提交不属于当前任务", 409, "request_mismatch")
            return _response(_result(task_id, request_id, row))
    except PanelStorageBusy:
        raise
    except (OSError, ConfigReceiptError):
        return _error("提交回执无法核对，不能重新发送原请求", 503, "request_unavailable")
