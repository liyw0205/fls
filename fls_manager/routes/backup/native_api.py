import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, jsonify, request, send_file

from . import bp
from . import _common as backup
from .actions import submit_backup_creation, submit_backup_cancellation
from ...storage import storage_transaction, write_json

NATIVE_PREFIX = "/api/backup/native"
NATIVE_BODY_LIMIT = 4096
NATIVE_JOB_FILE_LIMIT = 8 * 1024 * 1024
NATIVE_JOB_COUNT_LIMIT = 1000
NATIVE_FILE_SCAN_LIMIT = 10000
REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DELETE_RECEIPT_LIMIT = 4096
DELETE_RECEIPT_DIR = backup.BASE_DIR / ".backup_native_delete_requests"
PREFLIGHT_DIR = backup.BASE_DIR / ".backup_native_preflights"
PREFLIGHT_ARCHIVE_DIR = backup.BASE_DIR / ".backup_native_staging"
PREFLIGHT_WORK_DIR = backup.BASE_DIR / ".backup_native_work"
PREFLIGHT_TTL_SECONDS = 60 * 60
PREFLIGHT_TERMINAL_RETENTION_SECONDS = 24 * 60 * 60
PREFLIGHT_ACTIVE_LIMIT = 2
PREFLIGHT_EXECUTION_LIMIT = 1
PREFLIGHT_RECORD_LIMIT = 1000
PREFLIGHT_RECORD_BYTES = 16 * 1024
PREFLIGHT_ID_RE = REQUEST_ID_RE
PREFLIGHT_LOCK = threading.RLock()
PREFLIGHT_ACTIVE = set()
PREFLIGHT_CANCEL_EVENTS = {}
PREFLIGHT_FORMATS = (".tar.gz", ".tgz", ".tar", ".zip")


class PreflightStorageError(RuntimeError):
    pass


class PreflightStartError(RuntimeError):
    pass


def _response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(code, message, status):
    return _response({"ok": False, "code": code, "msg": message}, status)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _json_body(allowed, required):
    if not request.is_json:
        return None, _error("json_required", "请求必须使用 JSON", 415)
    if request.content_length is not None and request.content_length > NATIVE_BODY_LIMIT:
        return None, _error("request_too_large", "请求内容超过限制", 413)
    raw = request.stream.read(NATIVE_BODY_LIMIT + 1)
    if len(raw) > NATIVE_BODY_LIMIT:
        return None, _error("request_too_large", "请求内容超过限制", 413)
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None, _error("invalid_json", "JSON 格式无效或包含重复字段", 400)
    if not isinstance(body, dict) or set(body) - set(allowed) or set(required) - set(body):
        return None, _error("invalid_body", "请求字段无效", 400)
    return body, None


def _single_query(allowed):
    if set(request.args) - set(allowed) or any(len(request.args.getlist(key)) != 1 for key in request.args):
        return None, _error("invalid_query", "查询参数无效或重复", 400)
    return request.args, None


def _page_args():
    args, error = _single_query({"page", "limit"})
    if error:
        return None, error
    try:
        page = int(args.get("page", "1"))
        limit = int(args.get("limit", "25"))
    except (TypeError, ValueError):
        return None, _error("invalid_page", "分页参数无效", 400)
    if not 1 <= page <= 100000 or not 1 <= limit <= 100:
        return None, _error("invalid_page", "分页参数超出范围", 400)
    return (page, limit), None


@contextmanager
def _native_backup_directory_fd():
    path = backup.BACKUP_DIR
    try:
        info = path.lstat()
    except FileNotFoundError:
        yield None
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("备份目录无效")
    if not backup.BACKUP_DIRECTORY_FD_SUPPORTED:
        yield path
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _state(info, live):
    status = str(info.get("status") or "")
    if info.get("running") is True:
        if not live:
            return "unknown"
        if status == "准备中":
            return "preparing"
        if status == "正在取消":
            return "cancelling"
        return "running"
    return {
        "已完成": "succeeded",
        "已取消": "cancelled",
        "启动失败": "failed",
        "失败": "failed",
        "结果未知": "unknown",
    }.get(status, "unknown")


def _job_payload(info, *, live=False):
    if not isinstance(info, dict) or not JOB_ID_RE.fullmatch(str(info.get("id") or "")):
        raise ValueError("备份作业记录无效")
    items = info.get("items")
    if not isinstance(items, list) or any(item not in ("data", "scripts") for item in items):
        raise ValueError("备份作业范围无效")
    filename = info.get("filename") if isinstance(info.get("filename"), str) else ""
    try:
        backup.validate_backup_filename(filename) if filename else None
    except ValueError:
        filename = ""
    size = info.get("size")
    if type(size) is not int or size < 0:
        size = 0
    state = _state(info, live)
    active = state in ("preparing", "running", "cancelling")
    return {
        "id": info["id"],
        "items": [item for item in ("data", "scripts") if item in items],
        "status": state,
        "active": active,
        "cancel_requested": info.get("cancel_requested") is True,
        "cancel_allowed": bool(live and active and info["id"] in backup.BACKUP_CANCEL_EVENTS),
        "filename": filename,
        "size_bytes": size,
        "created_at": str(info.get("created_at") or "")[:64],
        "updated_at": str(info.get("updated_at") or "")[:64],
        "cleanup_required": info.get("staged_cleanup_pending") is True,
    }


def _read_jobs():
    with backup.BACKUP_JOB_LOCK, storage_transaction():
        path = backup.BACKUP_JOBS_FILE
        try:
            info = path.lstat()
        except FileNotFoundError:
            rows = {}
        else:
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_size > NATIVE_JOB_FILE_LIMIT):
                raise RuntimeError("备份作业记录文件无效")
            rows = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            rows = backup.validate_backup_jobs(rows)
        if len(rows) > NATIVE_JOB_COUNT_LIMIT:
            raise RuntimeError("备份作业历史超过读取上限")
        merged = {key: dict(value) for key, value in rows.items()}
        for key, value in backup.BACKUP_JOBS.items():
            merged[key] = backup._backup_job_snapshot(value)
        result = []
        for key, info in merged.items():
            live = key in backup.BACKUP_JOBS and key in backup.BACKUP_CANCEL_EVENTS
            if info.get("id") != key:
                raise RuntimeError("备份作业身份不一致")
            result.append(_job_payload(info, live=live))
    result.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
    return result


def _read_create_receipt(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("备份请求标识无效")
    directory = backup.DATA_DIR / "backup_requests"
    if directory.is_symlink():
        raise RuntimeError("备份请求目录无效")
    path = directory / (request_id + ".json")
    if path.is_symlink():
        raise RuntimeError("备份请求回执无效")
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
        raise RuntimeError("备份请求回执无效")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("备份请求回执损坏") from exc
    if (not isinstance(receipt, dict) or receipt.get("request_id") != request_id
        or receipt.get("state") not in ("prepared", "launching", "failed", "submitted")
        or receipt.get("items") not in (["data"], ["scripts"], ["data", "scripts"])):
        raise RuntimeError("备份请求回执损坏")
    return receipt


def _delete_receipt_path(request_id):
    return DELETE_RECEIPT_DIR / (request_id + ".json")


def _read_delete_receipt(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("备份请求标识无效")
    directory = DELETE_RECEIPT_DIR
    if directory.is_symlink():
        raise RuntimeError("备份删除请求目录无效")
    path = _delete_receipt_path(request_id)
    if path.is_symlink():
        raise RuntimeError("备份删除请求回执无效")
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > DELETE_RECEIPT_LIMIT:
        raise RuntimeError("备份删除请求回执无效")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("备份删除请求回执损坏") from exc
    if (not isinstance(receipt, dict) or set(receipt) != {
            "request_id", "kind", "filename", "file_version", "state"
        } or receipt.get("request_id") != request_id
        or receipt.get("kind") != "delete" or receipt.get("state") not in ("prepared", "deleted")
        or not isinstance(receipt.get("filename"), str)
        or not isinstance(receipt.get("file_version"), str)
        or not REQUEST_ID_RE.fullmatch(str(receipt.get("request_id") or ""))
        or not re.fullmatch(r"[a-f0-9]{64}", receipt["file_version"])):
        raise RuntimeError("备份删除请求回执损坏")
    try:
        backup.validate_backup_filename(receipt["filename"])
    except ValueError as exc:
        raise RuntimeError("备份删除请求回执损坏") from exc
    return receipt


def _preflight_record_path(preflight_id):
    if not PREFLIGHT_ID_RE.fullmatch(str(preflight_id or "")):
        raise ValueError("备份预检查标识无效")
    return PREFLIGHT_DIR / (preflight_id + ".json")


def _preflight_archive_path(preflight_id):
    if not PREFLIGHT_ID_RE.fullmatch(str(preflight_id or "")):
        raise ValueError("备份预检查标识无效")
    return PREFLIGHT_ARCHIVE_DIR / (preflight_id + ".archive")


def _ensure_private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("备份预检查存储目录无效")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _read_preflight_record(preflight_id):
    path = _preflight_record_path(preflight_id)
    if PREFLIGHT_DIR.is_symlink() or path.is_symlink():
        raise RuntimeError("备份预检查回执无效")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > PREFLIGHT_RECORD_BYTES:
        raise RuntimeError("备份预检查回执无效")
    try:
        record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("备份预检查回执损坏") from exc
    allowed_states = {"uploading", "checking", "ready", "failed", "cancelled", "expired"}
    if (not isinstance(record, dict) or record.get("request_id") != preflight_id
        or record.get("state") not in allowed_states
        or type(record.get("created_at")) is not int
        or type(record.get("expires_at")) is not int
        or type(record.get("retention_until")) is not int
        or type(record.get("archive_size_bytes")) is not int
        or record["archive_size_bytes"] < 0
        or (record.get("archive_sha256", "") != ""
            and not re.fullmatch(r"[a-f0-9]{64}", str(record.get("archive_sha256"))))
        or record.get("archive_format") not in ("tar.gz", "tgz", "tar", "zip")):
        raise RuntimeError("备份预检查回执损坏")
    try:
        _, expected_suffix = _preflight_archive_filename(record.get("filename"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("备份预检查回执损坏") from exc
    if expected_suffix.lstrip(".") != record["archive_format"]:
        raise RuntimeError("备份预检查回执损坏")
    items = record.get("items", [])
    if (not isinstance(items, list) or len(items) > 2
        or any(item not in ("data", "scripts") for item in items)
        or len(items) != len(set(items))):
        raise RuntimeError("备份预检查回执损坏")
    for field in ("entry_count", "expanded_bytes"):
        value = record.get(field, 0)
        if type(value) is not int or value < 0:
            raise RuntimeError("备份预检查回执损坏")
    for field in ("filename", "archive_format", "error_code"):
        if not isinstance(record.get(field, ""), str) or len(record.get(field, "")) > 255:
            raise RuntimeError("备份预检查回执损坏")
    allowed_errors = {
        "", "archive_limit_exceeded", "insufficient_storage", "unsupported_format",
        "invalid_filename", "missing_backup_scope", "unsafe_archive_entry", "empty_archive",
        "preflight_cancelled", "preflight_start_failed", "invalid_archive", "preflight_failed",
    }
    if record.get("error_code", "") not in allowed_errors:
        raise RuntimeError("备份预检查回执损坏")
    if record["state"] == "ready" and (
        record["archive_size_bytes"] < 1
        or not record.get("archive_sha256")
        or not record["items"]
    ):
        raise RuntimeError("备份预检查回执损坏")
    dependencies = record.get("dependency_files", [])
    warnings = record.get("warnings", [])
    if (not isinstance(dependencies, list) or len(dependencies) > 100
        or any(item not in ("requirements.txt", "dependencies.txt") for item in dependencies)
        or not isinstance(warnings, list) or len(warnings) > 10
        or any(item not in ("data_restore_may_change_panel_access", "contains_native_session_store")
               for item in warnings)):
        raise RuntimeError("备份预检查回执损坏")
    return record


def _write_preflight_record(preflight_id, record):
    _ensure_private_directory(PREFLIGHT_DIR)
    path = _preflight_record_path(preflight_id)
    if path.is_symlink():
        raise RuntimeError("备份预检查回执无效")
    write_json(path, record)


def _preflight_payload(record):
    state = record["state"]
    active = record["request_id"] in PREFLIGHT_ACTIVE
    if state in ("uploading", "checking") and not active:
        state = "unknown"
    return {
        "preflight_id": record["request_id"],
        "request_id": record["request_id"],
        "status": state,
        "active": active,
        "cancel_allowed": active and record["state"] in ("uploading", "checking"),
        "filename": record.get("filename", ""),
        "format": record.get("archive_format", ""),
        "archive_size_bytes": record.get("archive_size_bytes", 0),
        "archive_sha256": record.get("archive_sha256", ""),
        "items": list(record.get("items", [])),
        "entry_count": record.get("entry_count", 0),
        "expanded_bytes": record.get("expanded_bytes", 0),
        "dependency_files": list(record.get("dependency_files", [])),
        "warnings": list(record.get("warnings", [])),
        "error_code": record.get("error_code", ""),
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
    }


def _preflight_archive_filename(filename):
    if (not isinstance(filename, str) or not filename or len(filename) > 255
        or filename != filename.strip() or "/" in filename or "\\" in filename or ":" in filename
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
        raise ValueError("invalid_filename")
    lowered = filename.lower()
    for suffix in PREFLIGHT_FORMATS:
        if lowered.endswith(suffix):
            return filename, suffix
    raise ValueError("unsupported_format")


def _validate_archive_member_path(name, root):
    normalized = str(name or "").replace("\\", "/")
    if ("\x00" in normalized or any(ord(char) < 32 for char in normalized)
        or any(part == ".." for part in normalized.split("/"))):
        raise ValueError("unsafe_archive_entry")
    try:
        backup._archive_member_target(root, normalized)
    except (RuntimeError, ValueError, OSError) as exc:
        raise ValueError("unsafe_archive_entry") from exc


def _copy_preflight_member(source, target, cancel_event):
    written = 0
    with target.open("xb") as destination:
        while True:
            if cancel_event and cancel_event.is_set():
                raise ValueError("preflight_cancelled")
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            destination.write(chunk)
            written += len(chunk)
            if written > backup.MAX_BACKUP_EXTRACTED_BYTES:
                raise backup.BackupLimitExceeded("archive exceeds extraction limit")
    return written


def _extract_preflight_archive(archive_path, suffix, extracted, cancel_event):
    expanded = 0

    def extract_members(archive, members, name_for, is_directory, open_member):
        nonlocal expanded
        for member in members:
            if cancel_event and cancel_event.is_set():
                raise ValueError("preflight_cancelled")
            name = name_for(member)
            _validate_archive_member_path(name, extracted)
            target = backup._archive_member_target(extracted, name)
            if is_directory(member):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = open_member(archive, member)
            if source is None:
                raise ValueError("invalid_archive")
            with source:
                member_size = _copy_preflight_member(source, target, cancel_event)
            expanded += member_size
            if expanded > backup.MAX_BACKUP_EXTRACTED_BYTES:
                raise backup.BackupLimitExceeded("archive exceeds extraction limit")
            declared_size = member.size if isinstance(member, tarfile.TarInfo) else member.file_size
            if member_size != declared_size:
                raise ValueError("invalid_archive")

    if suffix in (".tar.gz", ".tgz", ".tar"):
        mode = "r:" if suffix == ".tar" else "r:gz"
        with tarfile.open(archive_path, mode) as archive:
            extract_members(
                archive,
                archive.getmembers(),
                lambda member: member.name,
                lambda member: member.isdir(),
                lambda current, member: current.extractfile(member),
            )
    else:
        with zipfile.ZipFile(archive_path, "r") as archive:
            extract_members(
                archive,
                archive.infolist(),
                lambda member: member.filename,
                lambda member: member.is_dir(),
                lambda current, member: current.open(member, "r"),
            )


def _inspect_preflight_archive(archive_path, suffix, work_dir, cancel_event=None):
    if cancel_event and cancel_event.is_set():
        raise ValueError("preflight_cancelled")
    declared_entries, declared_bytes = _archive_declared_limits(archive_path, suffix, cancel_event)
    free_bytes = shutil.disk_usage(work_dir).free
    if free_bytes < declared_bytes + min(backup.MAX_BACKUP_UPLOAD_BYTES, 64 * 1024 * 1024):
        raise PreflightStorageError("insufficient preflight storage")
    extracted = work_dir / "extracted"
    extracted.mkdir(mode=0o700)
    if cancel_event and cancel_event.is_set():
        raise ValueError("preflight_cancelled")
    _extract_preflight_archive(archive_path, suffix, extracted, cancel_event)
    if cancel_event and cancel_event.is_set():
        raise ValueError("preflight_cancelled")

    root = backup.find_backup_root(extracted)
    if root is None:
        raise ValueError("missing_backup_scope")
    items = [item for item in ("data", "scripts") if (root / item).is_dir()]
    if not items:
        raise ValueError("missing_backup_scope")

    entries = 0
    expanded_bytes = 0
    dependency_files = set()
    has_native_sessions = False
    for current, directories, files in os.walk(extracted, followlinks=False):
        for name in directories:
            entry = os.path.join(current, name)
            info = os.lstat(entry)
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("unsafe_archive_entry")
        for name in files:
            entry = os.path.join(current, name)
            info = os.lstat(entry)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("unsafe_archive_entry")
            entries += 1
            expanded_bytes += info.st_size
            if expanded_bytes > backup.MAX_BACKUP_EXTRACTED_BYTES:
                raise backup.BackupLimitExceeded("archive exceeds extraction limit")
            if name in ("requirements.txt", "dependencies.txt"):
                dependency_files.add(name)
            relative = os.path.relpath(entry, root).replace(os.sep, "/")
            if relative == "data/native_sessions.sqlite3" or relative.startswith("data/native_sessions.sqlite3-"):
                has_native_sessions = True
    if entries > declared_entries or expanded_bytes != declared_bytes:
        raise ValueError("invalid_archive")

    warnings = []
    if "data" in items:
        warnings.append("data_restore_may_change_panel_access")
    if has_native_sessions:
        warnings.append("contains_native_session_store")
    return {
        "items": items,
        "entry_count": declared_entries,
        "expanded_bytes": expanded_bytes,
        "dependency_files": sorted(dependency_files),
        "warnings": warnings,
    }


def _archive_declared_limits(archive_path, suffix, cancel_event=None):
    entries = 0
    expanded = 0
    if suffix in (".tar.gz", ".tgz", ".tar"):
        mode = "r:gz" if suffix != ".tar" else "r:"
        with tarfile.open(archive_path, mode) as archive:
            members = archive.getmembers()
            if len(members) > backup.MAX_BACKUP_ARCHIVE_ENTRIES:
                raise backup.BackupLimitExceeded("archive exceeds entry limit")
            root = Path(tempfile.gettempdir()) / "fls-archive-preflight-root"
            for member in members:
                if cancel_event and cancel_event.is_set():
                    raise ValueError("preflight_cancelled")
                _validate_archive_member_path(member.name, root)
                if member.size < 0:
                    raise ValueError("unsafe_archive_entry")
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise ValueError("unsafe_archive_entry")
                entries += 1
                expanded += member.size
                if expanded > backup.MAX_BACKUP_EXTRACTED_BYTES:
                    raise backup.BackupLimitExceeded("archive exceeds extraction limit")
    else:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = archive.infolist()
            if len(members) > backup.MAX_BACKUP_ARCHIVE_ENTRIES:
                raise backup.BackupLimitExceeded("archive exceeds entry limit")
            root = Path(tempfile.gettempdir()) / "fls-archive-preflight-root"
            for member in members:
                if cancel_event and cancel_event.is_set():
                    raise ValueError("preflight_cancelled")
                _validate_archive_member_path(member.filename, root)
                mode = (int(member.external_attr or 0) >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError("unsafe_archive_entry")
                entries += 1
                expanded += member.file_size
                if expanded > backup.MAX_BACKUP_EXTRACTED_BYTES:
                    raise backup.BackupLimitExceeded("archive exceeds extraction limit")
    return entries, expanded


def _preflight_error_code(exc):
    if isinstance(exc, PreflightStartError):
        return "preflight_start_failed"
    if isinstance(exc, backup.BackupLimitExceeded):
        return "archive_limit_exceeded"
    if isinstance(exc, PreflightStorageError):
        return "insufficient_storage"
    if isinstance(exc, ValueError):
        allowed = {
            "unsupported_format", "invalid_filename", "missing_backup_scope",
            "unsafe_archive_entry", "empty_archive", "preflight_cancelled",
        }
        return str(exc) if str(exc) in allowed else "invalid_archive"
    if isinstance(exc, (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, RuntimeError)):
        return "invalid_archive"
    return "preflight_failed"


def _run_preflight_check(app, preflight_id, suffix, archive_path, cancel_event):
    record = None
    work_dir = None
    error_code = ""
    inspected = None
    with app.app_context():
        try:
            with PREFLIGHT_LOCK:
                record = _read_preflight_record(preflight_id)
            if record is None or record["state"] != "checking":
                raise RuntimeError("preflight request is no longer checking")
            if cancel_event.is_set():
                raise ValueError("preflight_cancelled")
            work_dir = Path(tempfile.mkdtemp(prefix=preflight_id + "-", dir=str(PREFLIGHT_WORK_DIR)))
            inspected = _inspect_preflight_archive(archive_path, suffix, work_dir, cancel_event)
            if cancel_event.is_set():
                raise ValueError("preflight_cancelled")
        except Exception as exc:
            error_code = _preflight_error_code(exc)
        finally:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)
            try:
                with PREFLIGHT_LOCK:
                    record = _read_preflight_record(preflight_id)
                    if record is not None and record["state"] == "checking":
                        if error_code:
                            _preflight_archive_path(preflight_id).unlink(missing_ok=True)
                            record["state"] = "cancelled" if error_code == "preflight_cancelled" else "failed"
                            record["error_code"] = error_code
                        else:
                            record.update(inspected)
                            record["state"] = "ready"
                            record["error_code"] = ""
                        _write_preflight_record(preflight_id, record)
            except Exception:
                pass
            finally:
                with PREFLIGHT_LOCK:
                    PREFLIGHT_ACTIVE.discard(preflight_id)
                    PREFLIGHT_CANCEL_EVENTS.pop(preflight_id, None)


def _expire_preflight_records(now):
    if PREFLIGHT_DIR.is_symlink() or PREFLIGHT_ARCHIVE_DIR.is_symlink():
        raise RuntimeError("备份预检查存储目录无效")
    if not PREFLIGHT_DIR.exists():
        return 0
    records = sorted(PREFLIGHT_DIR.glob("*.json"))
    if len(records) > PREFLIGHT_RECORD_LIMIT:
        raise RuntimeError("备份预检查历史超过安全读取上限")
    expired = 0
    for path in records:
        preflight_id = path.stem
        if not PREFLIGHT_ID_RE.fullmatch(preflight_id):
            raise RuntimeError("备份预检查历史包含无效记录")
        record = _read_preflight_record(preflight_id)
        if record is None:
            continue
        if preflight_id in PREFLIGHT_ACTIVE:
            continue
        if record["state"] in ("expired", "failed", "cancelled"):
            if record["retention_until"] <= now:
                _preflight_archive_path(preflight_id).unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            continue
        if record["expires_at"] <= now:
            archive_path = _preflight_archive_path(preflight_id)
            if archive_path.is_symlink():
                raise RuntimeError("备份预检查暂存文件无效")
            archive_path.unlink(missing_ok=True)
            record["state"] = "expired"
            record["error_code"] = "preflight_expired"
            record["retention_until"] = now + PREFLIGHT_TERMINAL_RETENTION_SECONDS
            _write_preflight_record(preflight_id, record)
            expired += 1
    return expired


def _delete_request_payload(request_id, receipt):
    deleted = receipt["state"] == "deleted"
    return {
        "request_id": request_id,
        "kind": "delete",
        "status": "succeeded" if deleted else "unknown",
        "active": False,
        "historical": deleted,
        "accepted": True if deleted else None,
        "filename": receipt["filename"],
        "version": receipt["file_version"],
        "deleted": deleted if deleted else None,
    }


def _native_delete_response(request_id, filename, version, *, status, deleted,
                            replayed=False, accepted=True, code=None):
    resolved_status = "deleted" if deleted is True else "not_deleted" if deleted is False else "unknown"
    payload = {
        "ok": status == 200 and deleted is True,
        "accepted": accepted,
        "request_id": request_id,
        "replayed": replayed,
        "status": resolved_status,
        "deleted": deleted,
        "filename": filename,
        "version": version,
    }
    if code:
        payload["code"] = code
    return _response(payload, status)


def _delete_receipt_conflict(request_id):
    return _read_create_receipt(request_id) is not None


def _verified_file_matches(filename, version):
    with _native_backup_directory_fd() as directory:
        if directory is None:
            raise FileNotFoundError(filename)
        info = backup.backup_entry_stat(directory, filename)
    return stat.S_ISREG(info.st_mode) and backup.backup_file_version(info) == version


def _request_payload(request_id, receipt, jobs):
    job = next((item for item in jobs if item["id"] == request_id), None)
    state = job["status"] if job else {
        "failed": "failed",
        "prepared": "unknown",
        "launching": "unknown",
        "submitted": "unknown",
    }[receipt["state"]]
    return {
        "request_id": request_id,
        "kind": "create",
        "status": state,
        "active": bool(job and job["active"]),
        "historical": bool(job and not job["active"] and state != "unknown"),
        "accepted": receipt["state"] in ("launching", "submitted") or bool(job),
        "items": list(receipt["items"]),
        "job": job,
    }


def _fingerprint_upload(upload):
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = upload.stream.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > backup.MAX_BACKUP_UPLOAD_BYTES:
            raise backup.BackupLimitExceeded("archive exceeds upload limit")
        digest.update(chunk)
    upload.stream.seek(0)
    return size, digest.hexdigest()


@bp.route(NATIVE_PREFIX + "/capabilities")
def api_backup_native_capabilities():
    return _response({
        "ok": True,
        "items": ["data", "scripts"],
        "formats": ["tar.gz", "tgz", "tar", "zip"],
        "upload_limit_bytes": backup.MAX_BACKUP_UPLOAD_BYTES,
        "extracted_limit_bytes": backup.MAX_BACKUP_EXTRACTED_BYTES,
        "entry_limit": backup.MAX_BACKUP_ARCHIVE_ENTRIES,
        "job_page_limit": 100,
        "file_page_limit": 100,
        "preflight_enabled": True,
        "preflight_limit": 2,
        "preflight_ttl_seconds": PREFLIGHT_TTL_SECONDS,
        "restore_enabled": False,
    })


@bp.route(NATIVE_PREFIX + "/preflights", methods=["POST"])
def api_backup_native_create_preflight():
    if request.mimetype != "multipart/form-data":
        return _error("multipart_required", "归档必须通过 multipart 文件上传", 415)
    if request.content_length is None:
        return _error("content_length_required", "上传请求必须声明内容长度", 411)
    if request.content_length > backup.MAX_BACKUP_UPLOAD_BYTES + 256 * 1024:
        return _error("upload_too_large", "备份文件超过面板有效上传上限", 413)
    try:
        if len(request.files) != 1 or len(request.files.getlist("file")) != 1:
            return _error("invalid_upload", "必须恰好上传一个归档文件", 400)
        if set(request.files) != {"file"} or set(request.form) != {"preflight_id"}:
            return _error("invalid_upload", "上传字段无效或重复", 400)
        ids = request.form.getlist("preflight_id")
        if len(ids) != 1 or not REQUEST_ID_RE.fullmatch(ids[0]):
            return _error("invalid_preflight_id", "预检查请求标识无效", 400)
        preflight_id = ids[0]
        upload = request.files["file"]
        filename, suffix = _preflight_archive_filename(upload.filename or "")
    except (AttributeError, TypeError, ValueError):
        return _error("invalid_upload", "上传字段无效", 400)

    now = int(time.time())
    existing_ready = None
    existing_checking = None
    with PREFLIGHT_LOCK:
        try:
            _expire_preflight_records(now)
            existing = _read_preflight_record(preflight_id)
        except Exception:
            return _error("preflight_unavailable", "预检查存储无法安全读取", 503)
        if existing is not None:
            payload = _preflight_payload(existing)
            if existing["filename"] != filename:
                return _error("preflight_id_conflict", "该预检查标识已绑定另一个文件", 409)
            if existing["state"] == "ready":
                existing_ready = existing
            elif existing["state"] == "checking":
                existing_checking = existing
            elif existing["state"] == "uploading":
                return _error("preflight_in_progress", "原预检查仍在接收文件，请查询原请求状态", 409)
            elif existing_ready is None:
                return _error("preflight_id_conflict", "该预检查标识已使用，请创建新的预检查", 409)
        if existing_ready is not None or existing_checking is not None:
            archive_path = None
        else:
            archive_path = _preflight_archive_path(preflight_id)
        if existing_ready is not None or existing_checking is not None:
            pass
        else:
            retained_count = 0
            if PREFLIGHT_DIR.exists():
                for path in PREFLIGHT_DIR.glob("*.json"):
                    if not path.is_file() or path.is_symlink():
                        return _error("preflight_unavailable", "预检查历史包含无效记录", 503)
                    try:
                        prior = _read_preflight_record(path.stem)
                    except Exception:
                        return _error("preflight_unavailable", "预检查历史无法安全读取", 503)
                    if (prior is not None and prior["state"] in ("uploading", "checking", "ready")
                        and prior["expires_at"] > now):
                        retained_count += 1
            if retained_count >= PREFLIGHT_ACTIVE_LIMIT:
                return _error("preflight_capacity", "已有两个未过期预检查，请先完成恢复或等待预检查过期", 429)
            if len(PREFLIGHT_ACTIVE) >= PREFLIGHT_EXECUTION_LIMIT:
                return _error("preflight_busy", "当前预检查任务正在运行，请稍后重试", 429)
            try:
                _ensure_private_directory(PREFLIGHT_DIR)
                _ensure_private_directory(PREFLIGHT_ARCHIVE_DIR)
                _ensure_private_directory(PREFLIGHT_WORK_DIR)
                if archive_path.exists() or archive_path.is_symlink():
                    return _error("preflight_evidence_exists", "存在未确认的预检查暂存文件，请检查存储后重试", 503)
                record = {
                    "request_id": preflight_id,
                    "state": "uploading",
                    "filename": filename,
                    "archive_format": suffix.lstrip("."),
                    "archive_size_bytes": 0,
                    "archive_sha256": "",
                    "items": [],
                    "entry_count": 0,
                    "expanded_bytes": 0,
                    "dependency_files": [],
                    "warnings": [],
                    "error_code": "",
                    "created_at": now,
                    "expires_at": now + PREFLIGHT_TTL_SECONDS,
                    "retention_until": now + PREFLIGHT_TTL_SECONDS + PREFLIGHT_TERMINAL_RETENTION_SECONDS,
                }
                _write_preflight_record(preflight_id, record)
                PREFLIGHT_CANCEL_EVENTS[preflight_id] = threading.Event()
                PREFLIGHT_ACTIVE.add(preflight_id)
            except Exception:
                return _error("preflight_unavailable", "无法为预检查分配暂存空间", 503)

    if existing_ready is not None:
        try:
            size, digest = _fingerprint_upload(upload)
        except backup.BackupLimitExceeded:
            return _error("upload_too_large", "备份文件超过面板有效上传上限", 413)
        if size != existing_ready["archive_size_bytes"] or digest != existing_ready["archive_sha256"]:
            return _error("preflight_id_conflict", "该预检查标识已绑定另一份归档", 409)
        payload = _preflight_payload(existing_ready)
        payload["replayed"] = True
        return _response({"ok": True, "preflight": payload})

    if existing_checking is not None:
        try:
            size, digest = _fingerprint_upload(upload)
        except backup.BackupLimitExceeded:
            return _error("upload_too_large", "备份文件超过面板有效上传上限", 413)
        if size != existing_checking["archive_size_bytes"] or digest != existing_checking["archive_sha256"]:
            return _error("preflight_id_conflict", "该预检查标识已绑定另一份归档", 409)
        with PREFLIGHT_LOCK:
            try:
                latest = _read_preflight_record(preflight_id)
            except Exception:
                return _error("preflight_unavailable", "预检查结果无法安全读取", 503)
        if latest is None:
            return _error("preflight_not_found", "备份预检查不存在", 404)
        payload = _preflight_payload(latest)
        payload["replayed"] = True
        return _response(
            {"ok": True, "accepted": payload["active"], "preflight": payload},
            202 if payload["active"] else 200,
        )

    archive_created = False
    try:
        digest = hashlib.sha256()
        size = 0
        cancel_event = PREFLIGHT_CANCEL_EVENTS[preflight_id]
        with archive_path.open("xb") as destination:
            archive_created = True
            while True:
                if cancel_event.is_set():
                    raise ValueError("preflight_cancelled")
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > backup.MAX_BACKUP_UPLOAD_BYTES:
                    raise backup.BackupLimitExceeded("archive exceeds upload limit")
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if size == 0:
            raise ValueError("empty_archive")
        record.update(archive_size_bytes=size, archive_sha256=digest.hexdigest(), state="checking")
        with PREFLIGHT_LOCK:
            _write_preflight_record(preflight_id, record)
        app = current_app._get_current_object()
        worker = threading.Thread(
            target=_run_preflight_check,
            args=(app, preflight_id, suffix, archive_path, cancel_event),
            name="backup-preflight-" + preflight_id[:8],
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            raise PreflightStartError("preflight worker could not start") from exc
    except Exception as exc:
        error_code = _preflight_error_code(exc)
        try:
            with PREFLIGHT_LOCK:
                if archive_created:
                    archive_path.unlink(missing_ok=True)
                record["state"] = "cancelled" if error_code == "preflight_cancelled" else "failed"
                record["error_code"] = error_code
                _write_preflight_record(preflight_id, record)
        except Exception:
            pass
        finally:
            with PREFLIGHT_LOCK:
                PREFLIGHT_ACTIVE.discard(preflight_id)
                PREFLIGHT_CANCEL_EVENTS.pop(preflight_id, None)
        status_code = (
            413 if error_code == "archive_limit_exceeded"
            else 507 if error_code == "insufficient_storage"
            else 503 if error_code in ("preflight_start_failed", "preflight_failed")
            else 422
        )
        return _response({"ok": False, "preflight": _preflight_payload(record)}, status_code)
    return _response({
        "ok": True,
        "accepted": True,
        "preflight": _preflight_payload(record),
    }, 202)


@bp.route(NATIVE_PREFIX + "/preflights/<preflight_id>")
def api_backup_native_preflight(preflight_id):
    if not REQUEST_ID_RE.fullmatch(preflight_id):
        return _error("invalid_preflight_id", "备份预检查标识无效", 400)
    try:
        with PREFLIGHT_LOCK:
            record = _read_preflight_record(preflight_id)
            if record is None:
                return _error("preflight_not_found", "备份预检查不存在", 404)
            payload = _preflight_payload(record)
            if (record["expires_at"] <= int(time.time())
                and record["state"] not in ("expired", "failed", "cancelled")):
                payload["status"] = record["state"] if payload["active"] else "expired"
    except Exception:
        return _error("preflight_unavailable", "备份预检查状态无法读取", 503)
    return _response({"ok": True, "preflight": payload})


@bp.route(NATIVE_PREFIX + "/preflights/<preflight_id>/cancel", methods=["POST"])
def api_backup_native_cancel_preflight(preflight_id):
    if not REQUEST_ID_RE.fullmatch(preflight_id):
        return _error("invalid_preflight_id", "备份预检查标识无效", 400)
    with PREFLIGHT_LOCK:
        try:
            record = _read_preflight_record(preflight_id)
        except Exception:
            return _error("preflight_unavailable", "备份预检查状态无法读取", 503)
        if record is None:
            return _error("preflight_not_found", "备份预检查不存在", 404)
        cancel_event = PREFLIGHT_CANCEL_EVENTS.get(preflight_id)
        if (record["state"] not in ("uploading", "checking")
            or preflight_id not in PREFLIGHT_ACTIVE or cancel_event is None):
            return _error("preflight_not_active", "预检查已不在可取消阶段", 409)
        cancel_event.set()
        payload = _preflight_payload(record)
    return _response({"ok": True, "accepted": True, "preflight": payload}, 202)


@bp.route(NATIVE_PREFIX + "/files")
def api_backup_native_files():
    paging, error = _page_args()
    if error:
        return error
    page, limit = paging
    try:
        files = []
        with _native_backup_directory_fd() as directory:
            if directory is None:
                return _response({"ok": True, "items": [], "page": page, "pages": 1,
                                  "limit": limit, "total": 0})
            names = os.listdir(directory)
            if len(names) > NATIVE_FILE_SCAN_LIMIT:
                return _error("file_scan_limit", "备份目录条目超过安全扫描上限", 503)
            for name in names:
                try:
                    backup.validate_backup_filename(name)
                    info = backup.backup_entry_stat(directory, name)
                except (ValueError, FileNotFoundError):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                files.append({
                    "name": name,
                    "version": backup.backup_file_version(info),
                    "size_bytes": info.st_size,
                    "modified_at": backup.datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "modified_epoch": info.st_mtime,
                })
        files.sort(key=lambda item: (item["modified_epoch"], item["name"]), reverse=True)
    except Exception:
        return _error("files_unavailable", "备份列表无法完整读取，请检查存储后重试", 500)
    total = len(files)
    pages = max(1, (total + limit - 1) // limit)
    start = (page - 1) * limit
    items = [{key: value for key, value in item.items() if key != "modified_epoch"}
             for item in files[start:start + limit]]
    return _response({"ok": True, "items": items, "page": page, "pages": pages,
                      "limit": limit, "total": total})


@bp.route(NATIVE_PREFIX + "/jobs")
def api_backup_native_jobs():
    paging, error = _page_args()
    if error:
        return error
    page, limit = paging
    try:
        jobs = _read_jobs()
    except Exception:
        return _error("jobs_unavailable", "备份作业历史无法完整读取，请修复存储后重试", 500)
    total = len(jobs)
    pages = max(1, (total + limit - 1) // limit)
    start = (page - 1) * limit
    return _response({"ok": True, "items": jobs[start:start + limit], "page": page,
                      "pages": pages, "limit": limit, "total": total})


@bp.route(NATIVE_PREFIX + "/jobs/<job_id>")
def api_backup_native_job(job_id):
    if not JOB_ID_RE.fullmatch(job_id):
        return _error("invalid_job_id", "备份作业标识无效", 400)
    try:
        job = next((item for item in _read_jobs() if item["id"] == job_id), None)
    except Exception:
        return _error("jobs_unavailable", "备份作业记录无法读取，请修复存储后重试", 500)
    if job is None:
        return _error("job_not_found", "备份作业不存在", 404)
    return _response({"ok": True, "job": job})


@bp.route(NATIVE_PREFIX + "/requests/<request_id>")
def api_backup_native_request(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "备份请求标识无效", 400)
    try:
        receipt = _read_create_receipt(request_id)
        deletion = _read_delete_receipt(request_id)
        if receipt is not None and deletion is not None:
            return _error("request_ambiguous", "备份请求标识冲突，请检查原请求", 409)
        if receipt is None and deletion is not None:
            return _response({"ok": True, "request": _delete_request_payload(request_id, deletion)})
        if receipt is None:
            return _error("request_not_found", "备份请求回执不存在", 404)
        jobs = _read_jobs()
    except Exception:
        return _error("request_unavailable", "备份请求无法确认，请修复存储后重试", 500)
    return _response({"ok": True, "request": _request_payload(request_id, receipt, jobs)})


@bp.route(NATIVE_PREFIX + "/jobs", methods=["POST"])
def api_backup_native_create():
    body, error = _json_body({"request_id", "items", "confirm_retry"}, {"request_id", "items"})
    if error:
        return error
    if not isinstance(body["request_id"], str) or not REQUEST_ID_RE.fullmatch(body["request_id"]):
        return _error("invalid_request_id", "备份请求标识无效", 400)
    if (not isinstance(body["items"], list) or not body["items"]
        or any(not isinstance(item, str) or item not in ("data", "scripts") for item in body["items"])
        or len(set(body["items"])) != len(body["items"])):
        return _error("invalid_items", "备份范围无效", 400)
    try:
        if _read_delete_receipt(body["request_id"]) is not None:
            return _error("request_id_conflict", "该请求标识已用于备份文件删除", 409)
    except Exception:
        return _error("request_unavailable", "无法确认备份请求标识，请修复存储后重试", 500)
    payload, status = submit_backup_creation(body)
    safe = {"ok": payload.get("ok") is True,
            "accepted": True if status == 200 else (False if payload.get("resolved") is False or status < 500 else None),
            "request_id": body["request_id"],
            "code": "accepted" if status == 200 else "unknown" if status >= 500 else "rejected"}
    if status != 200:
        safe["msg"] = "备份请求未完成，请查询原请求" if safe["accepted"] is None else "备份请求未受理，请核对后重试"
        return _response(safe, status)
    try:
        job = next((item for item in _read_jobs() if item["id"] == body["request_id"]), None)
    except Exception:
        job = None
    safe.update({"replayed": payload.get("job_id") == body["request_id"] and payload.get("msg") != "备份已提交",
                 "job": job})
    return _response(safe, status)


@bp.route(NATIVE_PREFIX + "/files/delete", methods=["POST"])
def api_backup_native_delete():
    body, error = _json_body({"request_id", "filename", "version"},
                             {"request_id", "filename", "version"})
    if error:
        return error
    request_id, filename, version = body["request_id"], body["filename"], body["version"]
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "备份请求标识无效", 400)
    try:
        backup.validate_backup_filename(filename)
    except ValueError as exc:
        return _error("invalid_filename", str(exc), 400)
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        return _error("invalid_version", "请刷新备份列表后重新选择文件", 400)

    try:
        with backup.BACKUP_JOB_LOCK, storage_transaction():
            if _delete_receipt_conflict(request_id):
                return _error("request_id_conflict", "该请求标识已用于备份创建", 409)
            receipt = _read_delete_receipt(request_id)
            replayed = receipt is not None
            if receipt is not None and (receipt["filename"] != filename or receipt["file_version"] != version):
                return _error("request_id_conflict", "该请求标识已绑定另一项删除操作", 409)
            if receipt and receipt["state"] == "deleted":
                return _native_delete_response(request_id, filename, version, status=200,
                                               deleted=True, replayed=True)

            try:
                matches = _verified_file_matches(filename, version)
            except FileNotFoundError:
                matches = False
                missing = True
            else:
                missing = False
            if receipt is None:
                if missing:
                    return _error("file_not_found", "备份文件不存在，请刷新列表", 404)
                if not matches:
                    return _error("stale_file", "备份文件已变化，请刷新列表后重新选择", 409)
                receipt = {
                    "request_id": request_id,
                    "kind": "delete",
                    "filename": filename,
                    "file_version": version,
                    "state": "prepared",
                }
                if DELETE_RECEIPT_DIR.is_symlink():
                    return _error("request_unavailable", "备份删除请求目录无效", 500)
                try:
                    write_json(_delete_receipt_path(request_id), receipt)
                except Exception:
                    return _error("request_unavailable", "删除意图无法保存，文件未删除", 500)
            elif missing or not matches:
                return _native_delete_response(request_id, filename, version, status=503,
                                               deleted=None, replayed=True, accepted=None,
                                               code="delete_result_unknown")

            from .actions import submit_backup_deletion
            result, delete_status = submit_backup_deletion(
                {"filename": filename, "version": version}, missing_is_unknown=True,
            )
            deleted = result.get("deleted")
            if deleted is True:
                receipt["state"] = "deleted"
                try:
                    write_json(_delete_receipt_path(request_id), receipt)
                except Exception:
                    return _native_delete_response(request_id, filename, version, status=503,
                                                   deleted=None, replayed=replayed, accepted=None,
                                                   code="receipt_write_unknown")
                return _native_delete_response(request_id, filename, version, status=200,
                                               deleted=True, replayed=replayed)
            if deleted is False:
                return _native_delete_response(request_id, filename, version, status=delete_status,
                                               deleted=False, replayed=replayed,
                                               accepted=True, code="delete_not_confirmed")
            return _native_delete_response(request_id, filename, version,
                                           status=delete_status if delete_status >= 400 else 503,
                                           deleted=None, replayed=replayed,
                                           accepted=None, code="delete_result_unknown")
    except Exception:
        return _error("delete_unavailable", "备份删除状态无法确认，请查询原请求后再操作", 500)


@bp.route(NATIVE_PREFIX + "/jobs/<job_id>/cancel", methods=["POST"])
def api_backup_native_cancel(job_id):
    if not JOB_ID_RE.fullmatch(job_id):
        return _error("invalid_job_id", "备份作业标识无效", 400)
    body, error = _json_body(set(), set())
    if error:
        return error
    payload, status = submit_backup_cancellation(job_id)
    safe = {"ok": payload.get("ok") is True,
            "accepted": payload.get("accepted"),
            "job_id": job_id,
            "code": "accepted" if payload.get("accepted") is True else "unknown" if payload.get("accepted") is None else "rejected"}
    if status != 200:
        safe["msg"] = "取消请求状态无法确认，请查询原作业" if payload.get("accepted") is None else "未能取消备份，请查询作业状态"
    try:
        safe["job"] = next((item for item in _read_jobs() if item["id"] == job_id), None)
    except Exception:
        safe["job"] = None
    return _response(safe, status)


@bp.route(NATIVE_PREFIX + "/files/<path:filename>/download")
def api_backup_native_download(filename):
    args, error = _single_query({"version"})
    if error:
        return error
    version = args.get("version", "")
    if not re.fullmatch(r"[a-f0-9]{64}", version):
        return _error("invalid_version", "请刷新备份列表后重新选择文件", 400)
    try:
        backup.validate_backup_filename(filename)
        with _native_backup_directory_fd() as directory:
            if directory is None:
                return _error("file_not_found", "备份文件不存在，请刷新列表", 404)
            current = backup.backup_entry_stat(directory, filename)
            if backup.backup_file_version(current) != version:
                return _error("stale_file", "备份文件已变化，请刷新列表后重新选择", 409)
            source = backup.open_backup_entry(directory, filename)
        response = send_file(source, mimetype="application/gzip", as_attachment=True,
                             download_name=filename, conditional=False)
        response.content_length = os.fstat(source.fileno()).st_size
        response.headers["Cache-Control"] = "no-store"
        response.call_on_close(source.close)
        return response
    except FileNotFoundError:
        return _error("file_not_found", "备份文件不存在，请刷新列表", 404)
    except (ValueError, OSError):
        return _error("file_unavailable", "备份文件已变化或无法读取，请刷新列表", 409)
