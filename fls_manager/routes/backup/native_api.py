import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, jsonify, request, send_file

from . import bp
from . import _common as backup
from .actions import submit_backup_creation, submit_backup_cancellation
from .dependency_process import DependencyProcess, cleanup_previous_process
from ...storage import storage_transaction, write_json
from ...sensitive import mask_sensitive_text
from ... import backup_commit
from ...data_access import directory_access
from ...backup_lease import try_process_lease as _try_process_lease, release_process_lease as _release_process_lease
from ...receipt_io import open_receipt, publish_receipt

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
PREFLIGHT_RETAINED_FOR_RESTORE = set()
PREFLIGHT_CANCEL_EVENTS = {}
PREFLIGHT_FORMATS = (".tar.gz", ".tgz", ".tar", ".zip")
RESTORE_DIR = backup.BASE_DIR / ".backup_native_restores"
RESTORE_RECORD_LIMIT = 1000
RESTORE_RECORD_BYTES = 64 * 1024
RESTORE_OPERATION_LIMIT = 16
RESTORE_ACTIVE = set()
RESTORE_LOCK = threading.RLock()
RESTORE_DEPENDENCY_ACTIVE = set()
RESTORE_DEPENDENCY_CLEANUP = {}
RESTORE_DEPENDENCY_CLEANING = set()
RESTORE_DEPENDENCY_OUTPUT_LIMIT = 32 * 1024
RESTORE_WORKER_TIMEOUT_SECONDS = 30 * 60
RESTORE_PROCESS_LOCK_PATH = backup.BASE_DIR / ".backup_native_restore.lock"


class PreflightStorageError(RuntimeError):
    pass


class PreflightStartError(RuntimeError):
    pass


def _process_lease_held(path, *, shared=False):
    try:
        fd = _try_process_lease(path, create=False, shared=shared)
    except FileNotFoundError:
        return False
    if fd is None:
        return True
    _release_process_lease(fd)
    return False


def _mark_restore_lease(fd, request_id, kind, operation_id=""):
    encoded = b"\0" + json.dumps({"request_id": request_id, "kind": kind, "operation_id": operation_id}).encode("ascii")
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, encoded)
    os.ftruncate(fd, len(encoded))
    os.fsync(fd)


def _restore_lease_matches(record, kind):
    if not _process_lease_held(RESTORE_PROCESS_LOCK_PATH, shared=True):
        return False
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(RESTORE_PROCESS_LOCK_PATH, flags)
        with os.fdopen(fd, "rb") as source:
            source.seek(1)
            encoded = source.read(513)
        if len(encoded) > 512:
            return False
        owner = json.loads(encoded)
        operation_kind = "dependency" if kind == "dependency-cleanup" else kind
        return (owner.get("request_id") == record["request_id"] and owner.get("kind") == kind
                and owner.get("operation_id") == (record.get(operation_kind + "_operation_id", "") if kind != "restore" else ""))
    except (OSError, ValueError, AttributeError):
        return False


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
        "preflight_cancelled", "preflight_start_failed", "preflight_expired", "invalid_archive", "preflight_failed",
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
    retained = _restore_retained_preflight_ids()
    expired = 0
    for path in records:
        preflight_id = path.stem
        if not PREFLIGHT_ID_RE.fullmatch(preflight_id):
            raise RuntimeError("备份预检查历史包含无效记录")
        record = _read_preflight_record(preflight_id)
        if record is None:
            continue
        if preflight_id in PREFLIGHT_ACTIVE or preflight_id in retained:
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
        "restore_enabled": True,
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
            retained_for_restore = _restore_retained_preflight_ids()
            if PREFLIGHT_DIR.exists():
                for path in PREFLIGHT_DIR.glob("*.json"):
                    if not path.is_file() or path.is_symlink():
                        return _error("preflight_unavailable", "预检查历史包含无效记录", 503)
                    try:
                        prior = _read_preflight_record(path.stem)
                    except Exception:
                        return _error("preflight_unavailable", "预检查历史无法安全读取", 503)
                    if (prior is not None and prior["state"] in ("uploading", "checking", "ready")
                        and (prior["expires_at"] > now or prior["request_id"] in retained_for_restore)):
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


def _native_restore_path(request_id):
    if not REQUEST_ID_RE.fullmatch(str(request_id or "")):
        raise ValueError("invalid restore request id")
    if RESTORE_DIR.is_symlink():
        raise RuntimeError("恢复作业目录无效")
    return RESTORE_DIR / (request_id + ".json")


def _write_native_restore(record):
    _ensure_private_directory(RESTORE_DIR)
    path = _native_restore_path(record["request_id"])
    temp = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temp.is_symlink():
        raise RuntimeError("恢复作业回执无效")
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > RESTORE_RECORD_BYTES:
        raise RuntimeError("恢复作业回执超过安全大小")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        publish_receipt(temp, path)
        try:
            directory_fd = os.open(RESTORE_DIR, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _read_native_restore(request_id):
    path = _native_restore_path(request_id)
    try:
        fd = open_receipt(path)
    except FileNotFoundError:
        return None
    try:
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > RESTORE_RECORD_BYTES:
                raise RuntimeError("恢复作业回执无效")
            encoded = source.read(RESTORE_RECORD_BYTES + 1)
        if len(encoded) > RESTORE_RECORD_BYTES:
            raise RuntimeError("恢复作业回执超过安全大小")
        record = json.loads(encoded, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("恢复作业回执损坏") from exc
    states = {"accepted", "extracting", "staging", "committing", "restored", "failed", "unknown"}
    scheduler_states = {"not_required", "pending", "running", "succeeded", "failed", "unknown"}
    dependency_states = {"not_requested", "pending", "running", "succeeded", "failed", "unknown"}
    if (not isinstance(record, dict) or record.get("version") != 1
        or record.get("request_id") != request_id
        or not REQUEST_ID_RE.fullmatch(str(record.get("preflight_id") or ""))
        or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("archive_sha256") or ""))
        or record.get("state") not in states
        or record.get("scheduler_state") not in scheduler_states
        or record.get("dependency_state") not in dependency_states
        or record.get("restore_items") not in (["data"], ["scripts"], ["data", "scripts"])
        or type(record.get("created_at")) is not int or record["created_at"] < 0
        or type(record.get("updated_at")) is not int or record["updated_at"] < record["created_at"]
        or type(record.get("restore_dependencies")) is not bool
        or type(record.get("live_state_unchanged")) is not bool
        or type(record.get("archive_size_bytes")) is not int
        or record["archive_size_bytes"] < 1
        or not isinstance(record.get("filename"), str) or len(record["filename"]) > 255
        or not isinstance(record.get("error", ""), str) or len(record.get("error", "")) > 1000
        or not isinstance(record.get("scheduler_error", ""), str) or len(record.get("scheduler_error", "")) > 1000
        or not isinstance(record.get("dependency_error", ""), str) or len(record.get("dependency_error", "")) > 1000
        or not isinstance(record.get("dependency_output", ""), str)
        or len(record.get("dependency_output", "").encode("utf-8")) > RESTORE_DEPENDENCY_OUTPUT_LIMIT
        or not isinstance(record.get("scheduler_operation_id", ""), str)
        or record.get("scheduler_operation_id", "") and not REQUEST_ID_RE.fullmatch(record["scheduler_operation_id"])
        or not isinstance(record.get("dependency_operation_id", ""), str)
        or record.get("dependency_operation_id", "") and not REQUEST_ID_RE.fullmatch(record["dependency_operation_id"])):
        raise RuntimeError("恢复作业回执结构无效")
    operations = record.get("operations", {})
    if not isinstance(operations, dict) or len(operations) > RESTORE_OPERATION_LIMIT:
        raise RuntimeError("恢复后续操作历史无效")
    for operation_id, operation in operations.items():
        if (not REQUEST_ID_RE.fullmatch(operation_id) or not isinstance(operation, dict)
            or operation.get("kind") not in ("scheduler", "dependency")
            or operation.get("state") not in ("running", "succeeded", "failed", "unknown")
            or not isinstance(operation.get("error"), str)
            or len(operation["error"].encode("utf-8")) > 512
            or type(operation.get("created_at")) is not int
            or type(operation.get("updated_at")) is not int
            or operation["updated_at"] < operation["created_at"]):
            raise RuntimeError("恢复后续操作回执无效")
    if type(record.get("dependency_cleanup_pending", False)) is not bool:
        raise RuntimeError("依赖清理状态无效")
    guard = record.get("dependency_guard")
    if guard is not None and (
        not isinstance(guard, dict)
        or guard.get("kind") not in ("posix_group", "windows_job")
        or guard.get("kind") == "posix_group" and (type(guard.get("pid")) is not int or guard["pid"] <= 1)
        or guard.get("kind") == "windows_job" and (
            not isinstance(guard.get("name"), str)
            or not re.fullmatch(r"Local\\FLSBackupDependencies_[a-f0-9]{32}", guard["name"])
            or type(guard.get("pid")) is not int or guard["pid"] <= 1
        )
    ):
        raise RuntimeError("依赖执行身份无效")
    workspace = record.get("dependency_workspace")
    if workspace is not None and (
        not isinstance(workspace, dict)
        or not isinstance(workspace.get("name"), str)
        or not re.fullmatch(r"deps-" + request_id[:8] + r"-[a-z0-9_]{1,64}", workspace["name"])
        or type(workspace.get("device")) is not int or type(workspace.get("inode")) is not int
    ):
        raise RuntimeError("依赖工作目录身份无效")
    return record


def _native_restore_records():
    if RESTORE_DIR.is_symlink():
        raise RuntimeError("恢复作业目录无效")
    records = []
    for path in RESTORE_DIR.glob("*.json"):
        if len(records) >= RESTORE_RECORD_LIMIT or not REQUEST_ID_RE.fullmatch(path.stem):
            raise RuntimeError("恢复历史超过安全读取上限或包含无效回执")
        record = _read_native_restore(path.stem)
        if record is None:
            raise RuntimeError("恢复历史在读取时发生变化")
        records.append(record)
    return records


def _restore_retained_preflight_ids():
    retained = set(PREFLIGHT_RETAINED_FOR_RESTORE)
    for record in _native_restore_records():
        if (record["state"] in ("accepted", "extracting", "staging", "committing", "unknown")
            or record["state"] == "restored" and record["restore_dependencies"]
            and record["dependency_state"] not in ("succeeded", "not_requested")):
            retained.add(record["preflight_id"])
    return retained


def _dependency_cleanup_pending():
    return bool(RESTORE_DEPENDENCY_CLEANUP) or any(
        record.get("dependency_cleanup_pending", False) for record in _native_restore_records()
    )


def _restore_record_update(request_id, **changes):
    with RESTORE_LOCK:
        record = _read_native_restore(request_id)
        if record is None:
            raise RuntimeError("恢复作业回执不存在")
        record.update(changes)
        record["updated_at"] = int(time.time())
        for kind in ("scheduler", "dependency"):
            if kind + "_state" in changes:
                operation = record.get("operations", {}).get(record.get(kind + "_operation_id"))
                if operation is not None:
                    operation.update(
                        state=record[kind + "_state"],
                        error=_bounded_text(record.get(kind + "_error", ""), 512),
                        updated_at=record["updated_at"],
                    )
        _write_native_restore(record)
        return record


def _native_restore_payload(record):
    active_states = {"accepted", "extracting", "staging", "committing"}
    active = record["state"] in active_states and (
        record["request_id"] in RESTORE_ACTIVE or _restore_lease_matches(record, "restore")
    )
    phase = record["state"]
    interrupted_before_commit = not active and phase in ("accepted", "extracting", "staging")
    state = "failed" if interrupted_before_commit else phase if active or phase not in active_states else "unknown"
    scheduler_state = record["scheduler_state"]
    if (scheduler_state == "running" and record["request_id"] not in RESTORE_SCHEDULER_ACTIVE
        and not _restore_lease_matches(record, "scheduler")):
        scheduler_state = "unknown"
    dependency_state = record["dependency_state"]
    if (dependency_state == "running" and record["request_id"] not in RESTORE_DEPENDENCY_ACTIVE
        and not _restore_lease_matches(record, "dependency")):
        dependency_state = "unknown"
    cleanup_required = record["request_id"] in RESTORE_DEPENDENCY_CLEANUP or (
        record.get("dependency_cleanup_pending", False) and record["request_id"] not in RESTORE_DEPENDENCY_ACTIVE
        and (record["dependency_state"] != "running" or not _restore_lease_matches(record, "dependency"))
    )
    if cleanup_required:
        dependency_state = "unknown"
    startup_pending = (
        current_app.config.get("RESTORE_STARTUP_RECONCILIATION_PENDING", False)
        and record["request_id"] in current_app.config.get("RESTORE_STARTUP_REQUEST_IDS", ())
    )
    return {
        "request_id": record["request_id"],
        "preflight_id": record["preflight_id"],
        "filename": record["filename"],
        "archive_sha256": record["archive_sha256"],
        "archive_size_bytes": record["archive_size_bytes"],
        "restore_items": list(record["restore_items"]),
        "restore_dependencies": record["restore_dependencies"],
        "state": state,
        "phase": phase,
        "active": active,
        "committed": phase == "restored",
        "directory_recovery_required": not active and (
            startup_pending or backup_commit.recovery_required(backup.BASE_DIR, record["request_id"])
        ),
        "live_state_unchanged": record["live_state_unchanged"] or interrupted_before_commit,
        "restore_outcome_unknown": state == "unknown" or phase == "unknown",
        "error": _bounded_text(
            record.get("error", "") or ("面板进程在目录提交前中断；原目录未更改" if interrupted_before_commit else ""),
            1000,
        ),
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "scheduler": {
            "state": scheduler_state,
            "required": "data" in record["restore_items"],
            "operation_id": record.get("scheduler_operation_id", ""),
            "error": _bounded_text(record.get("scheduler_error", ""), 1000),
        },
        "dependencies": {
            "state": dependency_state,
            "operation_id": record.get("dependency_operation_id", ""),
            "error": _bounded_text(record.get("dependency_error", ""), 1000),
            "output": _bounded_text(record.get("dependency_output", ""), RESTORE_DEPENDENCY_OUTPUT_LIMIT),
            "cleanup_required": cleanup_required,
        },
    }


def _bounded_text(value, limit):
    if not isinstance(value, str):
        return ""
    value = value.replace(str(backup.BASE_DIR), "[面板]")
    value = re.sub(r"(https?://)[^/@\s]+@", r"\1[redacted]@", value, flags=re.IGNORECASE)
    value = mask_sensitive_text(value)
    value = "".join(char for char in value if ord(char) >= 32 or char in "\n\t")
    return value.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def _native_restore_identity_matches(record, preflight_id, digest, items, restore_dependencies):
    return (record.get("preflight_id") == preflight_id
        and record.get("archive_sha256") == digest
        and record.get("restore_items") == items
        and record.get("restore_dependencies") is restore_dependencies)


def _fingerprint_preflight_archive(preflight_id, expected_size, expected_digest, destination=None):
    if PREFLIGHT_ARCHIVE_DIR.is_symlink():
        raise ValueError("preflight_archive_changed")
    source_path = _preflight_archive_path(preflight_id)
    info = source_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
        raise ValueError("preflight_archive_changed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    source_fd = os.open(source_path, flags)
    digest = hashlib.sha256()
    size = 0
    target = None
    try:
        opened = os.fstat(source_fd)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(info, opened):
            raise ValueError("preflight_archive_changed")
        if destination is not None:
            target = destination.open("xb")
        with os.fdopen(source_fd, "rb") as source:
            source_fd = -1
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
            if target is not None:
                target.flush()
                os.fsync(target.fileno())
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if target is not None:
            target.close()
    actual = digest.hexdigest()
    if size != expected_size or actual != expected_digest:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise ValueError("preflight_archive_changed")
    return size, actual


def _directory_bytes(path):
    path = Path(path)
    if not path.exists():
        return 0
    if path.is_symlink() or not path.is_dir():
        raise ValueError("live_restore_target_invalid")
    total = 0
    for current, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            item = Path(current) / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("live_restore_target_invalid")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("live_restore_target_invalid")
    return total


def _restore_error_code(exc):
    value = str(exc)
    allowed = {
        "preflight_not_found", "preflight_not_ready", "preflight_expired", "preflight_archive_changed",
        "restore_scope_invalid", "dependency_file_missing", "insufficient_restore_storage",
        "live_restore_target_invalid", "restore_busy", "restore_history_full", "restore_start_failed",
    }
    return value if value in allowed else "restore_failed"


def _native_restore_response(record, status=200, *, replayed=False):
    return _response({"ok": True, "restore": _native_restore_payload(record), "replayed": replayed}, status)


@bp.route(NATIVE_PREFIX + "/restores", methods=["POST"])
def api_backup_native_create_restore():
    body, error = _json_body(
        {"request_id", "preflight_id", "archive_sha256", "restore_items", "restore_dependencies", "confirm_data_impact"},
        {"request_id", "preflight_id", "archive_sha256", "restore_items", "restore_dependencies", "confirm_data_impact"},
    )
    if error:
        return error
    request_id, preflight_id = body["request_id"], body["preflight_id"]
    digest, items = body["archive_sha256"], body["restore_items"]
    restore_dependencies = body["restore_dependencies"]
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "恢复请求标识无效", 400)
    if not isinstance(preflight_id, str) or not REQUEST_ID_RE.fullmatch(preflight_id):
        return _error("invalid_preflight_id", "备份预检查标识无效", 400)
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return _error("invalid_archive_sha256", "备份摘要无效", 400)
    if (not isinstance(items, list) or not items or any(item not in ("data", "scripts") for item in items)
        or len(items) != len(set(items)) or type(restore_dependencies) is not bool
        or type(body["confirm_data_impact"]) is not bool):
        return _error("invalid_restore_options", "恢复范围或选项无效", 400)
    items = [item for item in ("data", "scripts") if item in items]
    if "data" in items and body["confirm_data_impact"] is not True:
        return _error("data_impact_confirmation_required", "恢复配置数据必须明确确认可能改变面板访问配置", 400)

    restore_lease_fd = None
    try:
        with PREFLIGHT_LOCK, RESTORE_LOCK:
            existing = _read_native_restore(request_id)
            if existing is not None:
                if not _native_restore_identity_matches(existing, preflight_id, digest, items, restore_dependencies):
                    return _error("restore_id_conflict", "该恢复请求标识已绑定其他预检查或恢复范围", 409)
                active = _native_restore_payload(existing)["active"]
                return _native_restore_response(existing, 202 if active else 200, replayed=True)

            restore_lease_fd = _try_process_lease(RESTORE_PROCESS_LOCK_PATH)
            if restore_lease_fd is None:
                return _error("restore_busy", "另一面板进程正在执行恢复，请等待后查询", 409)
            _mark_restore_lease(restore_lease_fd, request_id, "restore")

            if backup_commit.pending(backup.BASE_DIR):
                return _error("directory_recovery_required", "先完成原恢复请求的目录现场恢复", 409)

            preflight = _read_preflight_record(preflight_id)
            if preflight is None:
                return _error("preflight_not_found", "备份预检查不存在，请重新选择文件", 404)
            if preflight["state"] != "ready":
                return _error("preflight_not_ready", "备份预检查尚未成功，未恢复任何目录", 409)
            if preflight["expires_at"] <= int(time.time()):
                return _error("preflight_expired", "备份预检查已过期，请重新选择归档", 410)
            if preflight["archive_sha256"] != digest or not set(items).issubset(preflight["items"]):
                return _error("preflight_identity_conflict", "预检查摘要或恢复范围与原归档不一致", 409)
            if restore_dependencies and not preflight["dependency_files"]:
                return _error("dependency_file_missing", "归档中没有可恢复的依赖清单", 400)
            _fingerprint_preflight_archive(preflight_id, preflight["archive_size_bytes"], digest)

            if RESTORE_DIR.is_symlink():
                return _error("restore_unavailable", "恢复作业目录无效", 503)
            records = _native_restore_records()
            if len(records) >= RESTORE_RECORD_LIMIT:
                return _error("restore_history_full", "恢复历史达到安全保留上限，请先导出并清理旧历史", 429)
            for prior in records:
                if prior.get("dependency_cleanup_pending", False):
                    return _error("dependency_cleanup_required", "原依赖作业尚需确认清理，请先查询并处理原恢复请求", 409)
                if prior["preflight_id"] == preflight_id:
                    return _error("preflight_already_used", "该预检查已绑定恢复请求；请查询原请求或重新预检查归档", 409)
                unresolved = prior["state"] == "committing" or (
                    prior["state"] == "unknown" and not prior["live_state_unchanged"]
                )
                if unresolved:
                    if prior["request_id"] in RESTORE_ACTIVE:
                        return _error("restore_busy", "另一项恢复作业正在运行，请等待其终态", 409)
                    return _error(
                        "restore_previous_unresolved",
                        "先查询并人工确认未完成恢复请求的目录结果，不得自动启动新恢复",
                        409,
                    )
                if prior["state"] in ("accepted", "extracting", "staging"):
                    if prior["request_id"] in RESTORE_ACTIVE:
                        return _error("restore_busy", "另一项恢复作业正在运行，请等待其终态", 409)
                    prior.update(
                        state="failed",
                        live_state_unchanged=True,
                        error="面板进程在目录提交前中断；原目录未更改",
                        updated_at=int(time.time()),
                    )
                    _write_native_restore(prior)
            required = preflight["expanded_bytes"] * 2 + 16 * 1024 * 1024
            for item in items:
                required += _directory_bytes(backup.DATA_DIR if item == "data" else backup.SCRIPT_DIR)
            if shutil.disk_usage(backup.BASE_DIR).free < required:
                return _error("insufficient_restore_storage", "面板剩余空间不足，未提交恢复", 507)
            if not backup.try_begin_restore_activity(process_lease_fd=restore_lease_fd):
                _release_process_lease(restore_lease_fd)
                restore_lease_fd = None
                return _error("restore_busy", "备份或恢复作业正在运行，请等待后重试", 409)

            now = int(time.time())
            record = {
                "version": 1,
                "request_id": request_id,
                "preflight_id": preflight_id,
                "archive_sha256": digest,
                "archive_size_bytes": preflight["archive_size_bytes"],
                "filename": preflight["filename"],
                "restore_items": items,
                "restore_dependencies": restore_dependencies,
                "state": "accepted",
                "live_state_unchanged": True,
                "scheduler_state": "pending" if "data" in items else "not_required",
                "scheduler_operation_id": "",
                "scheduler_error": "",
                "dependency_state": "pending" if restore_dependencies else "not_requested",
                "dependency_operation_id": "",
                "dependency_error": "",
                "dependency_output": "",
                "error": "",
                "created_at": now,
                "updated_at": now,
            }
            try:
                _write_native_restore(record)
                RESTORE_ACTIVE.add(request_id)
                PREFLIGHT_RETAINED_FOR_RESTORE.add(preflight_id)
            except Exception:
                PREFLIGHT_RETAINED_FOR_RESTORE.discard(preflight_id)
                backup.end_restore_activity()
                _release_process_lease(restore_lease_fd)
                restore_lease_fd = None
                raise

        try:
            app = current_app._get_current_object()
            worker = threading.Thread(
                target=_run_native_restore,
                args=(app, request_id, restore_lease_fd),
                name="backup-native-restore-" + request_id[:8],
                daemon=True,
            )
            worker.start()
            restore_lease_fd = None
        except Exception:
            try:
                with RESTORE_LOCK:
                    current = _read_native_restore(request_id)
                    if current is not None:
                        current.update(state="failed", error="恢复作业未能启动", updated_at=int(time.time()))
                        _write_native_restore(current)
            finally:
                with RESTORE_LOCK:
                    RESTORE_ACTIVE.discard(request_id)
                    PREFLIGHT_RETAINED_FOR_RESTORE.discard(preflight_id)
                backup.end_restore_activity()
            return _error("restore_start_failed", "恢复作业无法启动，目录未更改", 503)
        latest = _read_native_restore(request_id)
        return _native_restore_response(latest or record, 202 if latest and latest["state"] in ("accepted", "extracting", "staging", "committing") else 200)
    except ValueError as exc:
        code = _restore_error_code(exc)
        status = 507 if code == "insufficient_restore_storage" else 409 if code in ("preflight_archive_changed", "live_restore_target_invalid") else 503
        return _error(code, "备份预检查已变化或面板存储状态不安全，未提交恢复", status)
    except Exception:
        return _error("restore_unavailable", "无法安全受理恢复请求；请查询原请求后再操作", 503)
    finally:
        _release_process_lease(restore_lease_fd)


def _run_native_restore(app, request_id, process_lease_fd=None):
    from . import restore as legacy_restore
    from ... import models
    from ...native_sessions import NATIVE_SESSION_LOCK

    work_dir = None
    staged = []
    commit_attempted = False
    directories_restored = False
    try:
        with app.app_context():
            record = _read_native_restore(request_id)
            if record is None:
                raise RuntimeError("restore record disappeared")
            _restore_record_update(request_id, state="extracting", error="")
            preflight = _read_preflight_record(record["preflight_id"])
            if preflight is None or preflight["archive_sha256"] != record["archive_sha256"]:
                raise ValueError("preflight_archive_changed")
            work_dir = Path(tempfile.mkdtemp(prefix="restore-" + request_id[:8] + "-", dir=str(PREFLIGHT_WORK_DIR)))
            suffix = {"tar.gz": ".tar.gz", "tgz": ".tgz", "tar": ".tar", "zip": ".zip"}[preflight["archive_format"]]
            archive_path = work_dir / ("source" + suffix)
            _fingerprint_preflight_archive(
                record["preflight_id"], record["archive_size_bytes"], record["archive_sha256"], archive_path,
            )
            extract_dir = work_dir / "extracted"
            backup.extract_archive(archive_path, extract_dir)
            root = backup.find_backup_root(extract_dir)
            if root is None:
                raise ValueError("restore_scope_invalid")
            restore_sources = []
            for item in record["restore_items"]:
                source = root / item
                if source.is_symlink() or not source.is_dir():
                    raise ValueError("restore_scope_invalid")
                restore_sources.append((source, backup.DATA_DIR if item == "data" else backup.SCRIPT_DIR, item == "data"))
            dep_file = backup.find_dependency_file(extract_dir, root) if record["restore_dependencies"] else None
            if record["restore_dependencies"] and dep_file is None:
                raise ValueError("dependency_file_missing")

            _restore_record_update(request_id, state="staging")
            for source, target, keep_backups in restore_sources:
                staged.append(legacy_restore._stage_restore_directory(source, target, keep_backups))

            with directory_access(exclusive=True), RESTORE_LOCK, models.COLLECTION_MUTATION_LOCK, models.TASKS_MUTATION_LOCK, storage_transaction(), NATIVE_SESSION_LOCK:
                _restore_record_update(request_id, state="committing", live_state_unchanged=False)
                commit_attempted = True
                legacy_restore._commit_restore_directories(staged, preserve_runtime_identity=True, request_id=request_id)
                directories_restored = True
                _restore_record_update(request_id, state="restored", live_state_unchanged=False,
                                       scheduler_state="pending" if "data" in record["restore_items"] else "not_required",
                                       dependency_state="pending" if record["restore_dependencies"] else "not_requested",
                                       error="")
    except legacy_restore.BackupRestoreWriteError as exc:
        try:
            _restore_record_update(
                request_id,
                state="failed" if exc.live_state_unchanged else "unknown",
                live_state_unchanged=bool(exc.live_state_unchanged),
                error=_bounded_text(str(exc), 1000),
            )
        except Exception:
            pass
    except Exception as exc:
        try:
            _restore_record_update(
                request_id,
                state="restored" if directories_restored else "unknown" if commit_attempted else "failed",
                live_state_unchanged=not commit_attempted and not directories_restored,
                error=_bounded_text(str(exc), 1000),
            )
        except Exception:
            pass
    finally:
        try:
            transaction = backup_commit.read(backup.BASE_DIR, request_id)
            record = _read_native_restore(request_id)
            if transaction is not None and record is not None and (
                transaction["outcome"] == "restored" and record["state"] == "restored"
                or transaction["outcome"] == "not_applied" and record["state"] == "failed" and record["live_state_unchanged"]
            ):
                backup_commit.finish(backup.BASE_DIR, request_id)
        except Exception:
            pass
        for entry in staged:
            if not entry.get("preserve_workspace"):
                shutil.rmtree(entry["workspace"], ignore_errors=True)
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        with RESTORE_LOCK:
            RESTORE_ACTIVE.discard(request_id)
            try:
                record = _read_native_restore(request_id)
                PREFLIGHT_RETAINED_FOR_RESTORE.discard(record["preflight_id"] if record else "")
            except Exception:
                pass
            backup.end_restore_activity()
        _release_process_lease(process_lease_fd)


RESTORE_SCHEDULER_ACTIVE = set()


@bp.route(NATIVE_PREFIX + "/restores/<request_id>")
def api_backup_native_restore(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "恢复请求标识无效", 400)
    try:
        with RESTORE_LOCK:
            record = _read_native_restore(request_id)
            if record is None:
                return _error("restore_not_found", "恢复请求不存在", 404)
            payload = _native_restore_payload(record)
    except Exception:
        return _error("restore_unavailable", "恢复请求无法安全读取", 503)
    return _response({"ok": True, "restore": payload})


@bp.route(NATIVE_PREFIX + "/restores")
def api_backup_native_restore_history():
    args, error = _single_query({"limit", "offset"})
    if error:
        return error
    try:
        limit = int(args.get("limit", "25"))
        offset = int(args.get("offset", "0"))
    except (TypeError, ValueError):
        return _error("invalid_page", "恢复历史分页参数无效", 400)
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        return _error("invalid_page", "恢复历史分页参数超出范围", 400)
    try:
        with RESTORE_LOCK:
            records = [_native_restore_payload(record) for record in _native_restore_records()]
            records.sort(key=lambda item: (item["updated_at"], item["request_id"]), reverse=True)
            return _response({
                "ok": True,
                "items": records[offset:offset + limit],
                "total": len(records),
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < len(records),
            })
    except Exception:
        return _error("restore_history_unavailable", "恢复历史无法完整读取，请修复回执后重试", 503)


@bp.route(NATIVE_PREFIX + "/restore-maintenance")
def api_backup_native_restore_maintenance():
    from . import restore as legacy_restore
    try:
        state = legacy_restore._read_restore_archive_maintenance_state()
        if state is None:
            payload = {"ok": True, "maintenance": None}
        else:
            payload = {
                "ok": True,
                "maintenance": {
                    "event_id": state["event_id"],
                    "required": state["required"],
                    "kind": state["kind"],
                    "acknowledged": state.get("acknowledged", False),
                    "notice": legacy_restore._restore_archive_maintenance_notice(state),
                },
            }
        return _response(payload)
    except Exception:
        return _error("restore_maintenance_unavailable", "恢复归档维护状态无法读取，请修复回执后重试", 503)


@bp.route(NATIVE_PREFIX + "/restore-maintenance/<event_id>/ack", methods=["POST"])
def api_backup_native_ack_restore_maintenance(event_id):
    body, error = _json_body(set(), set())
    if error:
        return error
    from . import restore as legacy_restore
    payload, status = legacy_restore._ack_restore_archive_maintenance(event_id)
    return _response(payload, status)


@bp.route(NATIVE_PREFIX + "/restores/<request_id>/resolve", methods=["POST"])
def api_backup_native_resolve_restore(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "恢复请求标识无效", 400)
    body, error = _json_body({"outcome", "confirm"}, {"outcome", "confirm"})
    if error:
        return error
    if body["outcome"] not in ("restored", "not_applied") or type(body["confirm"]) is not bool or not body["confirm"]:
        return _error("invalid_resolution", "必须明确确认已检查的恢复结果", 400)
    process_lease_fd = None
    with RESTORE_LOCK:
        try:
            process_lease_fd = _try_process_lease(RESTORE_PROCESS_LOCK_PATH)
            if process_lease_fd is None:
                return _error("restore_still_active", "另一面板进程仍在执行恢复，不能人工确认结果", 409)
            if backup_commit.pending(backup.BASE_DIR):
                return _error("directory_recovery_required", "先恢复并核对持久记录中的目录现场", 409)
            record = _read_native_restore(request_id)
            if record is None:
                return _error("restore_not_found", "恢复请求不存在", 404)
            if request_id in RESTORE_ACTIVE:
                return _error("restore_still_active", "恢复仍在执行，不能人工确认结果", 409)
            if record["state"] not in ("accepted", "extracting", "staging", "committing", "unknown"):
                return _error("restore_already_resolved", "恢复请求已有明确终态", 409)
            if body["outcome"] == "restored":
                record.update(
                    state="restored",
                    live_state_unchanged=False,
                    scheduler_state="pending" if "data" in record["restore_items"] else "not_required",
                    dependency_state="pending" if record["restore_dependencies"] else "not_requested",
                    error="",
                )
            else:
                record.update(
                    state="failed",
                    live_state_unchanged=True,
                    scheduler_state="not_required",
                    dependency_state="not_requested",
                    error="用户确认检查后目录保持原状；未执行后续处理",
                )
            record["operator_resolution"] = body["outcome"]
            record["updated_at"] = int(time.time())
            _write_native_restore(record)
            return _native_restore_response(record)
        except Exception:
            return _error("restore_unavailable", "恢复结果确认未能持久保存，请查询原请求", 503)
        finally:
            _release_process_lease(process_lease_fd)


@bp.route(NATIVE_PREFIX + "/restores/<request_id>/recover-directories", methods=["POST"])
def api_backup_native_recover_directories(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "恢复请求标识无效", 400)
    body, error = _json_body({"confirm"}, {"confirm"})
    if error:
        return error
    if body["confirm"] is not True:
        return _error("confirmation_required", "必须确认恢复原目录现场", 400)
    from ... import models
    from ...native_sessions import NATIVE_SESSION_LOCK
    fd = None
    with RESTORE_LOCK:
        try:
            fd = _try_process_lease(RESTORE_PROCESS_LOCK_PATH)
            if fd is None or request_id in RESTORE_ACTIVE:
                return _error("restore_still_active", "恢复仍在执行，请等待后查询原请求", 409)
            record = _read_native_restore(request_id)
            if record is None:
                return _error("restore_not_found", "恢复请求不存在", 404)
            transaction = backup_commit.read(backup.BASE_DIR, request_id)
            if transaction is None:
                if current_app.config.get("RESTORE_STARTUP_RECONCILIATION_PENDING"):
                    from ...app import reconcile_startup_state
                    reconcile_startup_state(current_app)
                return _native_restore_response(record, replayed=True)
            if sorted(entry["target"] for entry in transaction["entries"]) != sorted(record["restore_items"]):
                return _error("restore_identity_conflict", "目录现场与原恢复范围不一致", 409)
            with directory_access(exclusive=True, recovery=True), models.COLLECTION_MUTATION_LOCK, models.TASKS_MUTATION_LOCK, storage_transaction(), NATIVE_SESSION_LOCK:
                outcome = backup_commit.recover(backup.BASE_DIR, request_id)
                restored = outcome == "restored"
                record = _restore_record_update(
                    request_id, state="restored" if restored else "failed", live_state_unchanged=not restored,
                    scheduler_state="pending" if restored and "data" in record["restore_items"] else "not_required",
                    dependency_state="pending" if restored and record["restore_dependencies"] else "not_requested",
                    error="" if restored else "中断的目录恢复已回滚，原目录与实例身份已恢复；未执行后续处理",
                )
                backup_commit.finish(backup.BASE_DIR, request_id)
                if current_app.config.get("RESTORE_STARTUP_RECONCILIATION_PENDING"):
                    from ...app import reconcile_startup_state
                    reconcile_startup_state(current_app)
                return _native_restore_response(record)
        except Exception:
            return _error("directory_recovery_unavailable", "目录现场尚未恢复完成；记录和副本保留，请查询原请求", 503)
        finally:
            _release_process_lease(fd)


def _restore_followup_record(request_id):
    record = _read_native_restore(request_id)
    if record is None:
        return None, _error("restore_not_found", "恢复请求不存在", 404)
    if record["state"] not in ("restored", "unknown"):
        return None, _error("restore_not_committed", "目录恢复尚未确认完成，不能执行后续处理", 409)
    if record["state"] == "unknown":
        return None, _error("restore_outcome_unknown", "恢复结果未知；检查目录后不要自动重提", 409)
    return record, None


def _replay_restore_followup(record, kind, operation_id):
    operation = record.get("operations", {}).get(operation_id)
    if operation is not None:
        if operation["kind"] != kind:
            return _error("operation_id_conflict", "操作标识已绑定另一种后续处理", 409)
        snapshot = dict(operation, operation_id=operation_id)
        if snapshot["state"] == "running" and (
            record.get(kind + "_operation_id") != operation_id
            or kind == "dependency" and record["request_id"] in RESTORE_DEPENDENCY_CLEANUP
            or not _restore_lease_matches(record, kind)
        ):
            snapshot["state"] = "unknown"
        return _response({
            "ok": True, "replayed": True, "operation": snapshot,
            "restore": _native_restore_payload(record),
        })
    if record.get(kind + "_operation_id") == operation_id:
        return _native_restore_response(record, replayed=True)
    return None


def _claim_restore_followup(request_id, kind, operation_id, confirm_retry):
    record, response = _restore_followup_record(request_id)
    if response:
        return None, None, response
    response = _replay_restore_followup(record, kind, operation_id)
    if response is not None:
        return None, None, response
    fd = _try_process_lease(RESTORE_PROCESS_LOCK_PATH)
    if fd is None:
        return None, None, _error("restore_busy", "另一恢复或后续处理正在执行，请查询原请求", 409)
    claimed = False
    try:
        if backup_commit.pending(backup.BASE_DIR):
            return None, None, _error("directory_recovery_required", "先完成原恢复请求的目录现场恢复", 409)
        # Another process may have completed an operation since the first read.
        record, response = _restore_followup_record(request_id)
        if response:
            return None, None, response
        response = _replay_restore_followup(record, kind, operation_id)
        if response is not None:
            return None, None, response
        operations = record.setdefault("operations", {})
        if kind == "scheduler" and "data" not in record["restore_items"]:
            return None, None, _error("scheduler_not_required", "本次仅恢复脚本，无需重载调度器", 409)
        if kind == "dependency" and not record["restore_dependencies"]:
            return None, None, _error("dependencies_not_requested", "恢复确认中未选择安装依赖", 409)
        if record[kind + "_state"] in ("succeeded", "failed", "unknown", "running") and not confirm_retry:
            return None, None, _error(kind + "_retry_confirmation_required", "请核对原结果并明确确认新的后续处理尝试", 409)
        if len(operations) >= RESTORE_OPERATION_LIMIT:
            return None, None, _error("restore_operation_capacity", "该恢复已达到后续操作保留上限，请检查面板维护状态", 429)
        if _dependency_cleanup_pending():
            return None, None, _error("dependency_cleanup_required", "原依赖作业尚需清理；清理确认前不能开始新的后续处理", 409)
        if kind == "dependency":
            preflight = _read_preflight_record(record["preflight_id"])
            if (preflight is None or preflight["state"] != "ready"
                or preflight["archive_sha256"] != record["archive_sha256"]):
                return None, None, _error("dependency_archive_unavailable", "恢复所引用的依赖归档不可用，请检查原请求", 409)
        _mark_restore_lease(fd, request_id, kind, operation_id)
        now = int(time.time())
        for prior in operations.values():
            if prior["state"] == "running":
                prior.update(state="unknown", updated_at=now)
        operations[operation_id] = {"kind": kind, "state": "running", "error": "", "created_at": now, "updated_at": now}
        record.update({kind + "_state": "running", kind + "_operation_id": operation_id,
                       kind + "_error": "", "updated_at": now})
        if kind == "dependency":
            record["dependency_output"] = ""
            record["dependency_guard"] = None
            record["dependency_workspace"] = None
            record["dependency_cleanup_pending"] = False
        _write_native_restore(record)
        claimed = True
        return record, fd, None
    finally:
        if not claimed:
            _release_process_lease(fd)


@bp.route(NATIVE_PREFIX + "/restores/<request_id>/scheduler-reload", methods=["POST"])
def api_backup_native_restore_scheduler_reload(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "恢复请求标识无效", 400)
    body, error = _json_body({"operation_id", "confirm_retry"}, {"operation_id", "confirm_retry"})
    if error:
        return error
    operation_id = body["operation_id"]
    if not isinstance(operation_id, str) or not REQUEST_ID_RE.fullmatch(operation_id) or type(body["confirm_retry"]) is not bool:
        return _error("invalid_operation", "调度器重载操作标识或确认无效", 400)
    from ...scheduler import reload_scheduler
    with RESTORE_LOCK:
        try:
            record, process_lease_fd, response = _claim_restore_followup(
                request_id, "scheduler", operation_id, body["confirm_retry"],
            )
            if response:
                return response
            RESTORE_SCHEDULER_ACTIVE.add(request_id)
        except Exception:
            return _error("restore_unavailable", "无法安全开始调度器重载，请查询原请求", 503)
    try:
        try:
            reload_scheduler()
        except Exception as exc:
            record = _restore_record_update(request_id, scheduler_state="failed", scheduler_error=_bounded_text(str(exc), 1000))
            return _response({"ok": False, "restore": _native_restore_payload(record), "msg": "调度器重载失败；目录没有重复恢复"}, 503)
        record = _restore_record_update(request_id, scheduler_state="succeeded", scheduler_error="")
        return _response({"ok": True, "restore": _native_restore_payload(record), "msg": "调度器已重载，恢复目录保持不变"})
    except Exception:
        return _error("scheduler_reload_unknown", "调度器重载结果尚未持久确认；查询原恢复请求后再决定", 503)
    finally:
        with RESTORE_LOCK:
            RESTORE_SCHEDULER_ACTIVE.discard(request_id)
        _release_process_lease(process_lease_fd)


def _drain_restore_dependency_output(stream, output, output_lock, errors=None):
    pending = bytearray()
    dropping_line = False

    def append_line(line):
        sanitized = _bounded_text(line.decode("utf-8", errors="replace"), 8192).encode("utf-8") + b"\n"
        with output_lock:
            output.extend(sanitized)
            if len(output) > RESTORE_DEPENDENCY_OUTPUT_LIMIT:
                del output[:-RESTORE_DEPENDENCY_OUTPUT_LIMIT]

    try:
        read = getattr(stream, "read1", stream.read)
        while True:
            chunk = read(4096)
            if not chunk:
                if pending and not dropping_line:
                    append_line(pending)
                return
            for index, part in enumerate(chunk.replace(b"\r", b"\n").split(b"\n")):
                if index:
                    if not dropping_line:
                        append_line(pending)
                    pending.clear()
                    dropping_line = False
                if dropping_line:
                    continue
                if len(pending) + len(part) > 8192:
                    append_line(b"[output line exceeded limit; omitted]")
                    pending.clear()
                    dropping_line = True
                else:
                    pending.extend(part)
    except Exception:
        if errors is None:
            raise
        errors.append("依赖输出读取中断，无法确认完整执行输出")
    finally:
        try:
            stream.close()
        except Exception:
            if errors is None:
                raise
            errors.append("依赖输出流无法关闭")


def _stop_restore_dependency_process(process):
    process.stop(timeout=10)


def _dependency_workspace_identity(work_dir):
    info = work_dir.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("依赖工作目录无效")
    return {"name": work_dir.name, "device": info.st_dev, "inode": info.st_ino}


def _cleanup_dependency_resources(entry):
    process = entry.get("process")
    if process is not None:
        _stop_restore_dependency_process(process)
    elif entry.get("guard") is not None:
        cleanup_previous_process(entry["guard"])
    reader = entry.get("reader")
    if reader is not None:
        reader.join(timeout=10)
        if reader.is_alive():
            raise RuntimeError("依赖输出仍被子进程占用，尚未完成清理")
    elif process is not None and process.stdout is not None:
        process.stdout.close()
    workspace = entry.get("workspace")
    if workspace is not None:
        if PREFLIGHT_WORK_DIR.is_symlink():
            raise RuntimeError("依赖临时目录已变化")
        work_dir = PREFLIGHT_WORK_DIR / workspace["name"]
        try:
            current = _dependency_workspace_identity(work_dir)
        except FileNotFoundError:
            return
        if current != workspace:
            raise RuntimeError("依赖工作目录身份已变化，未删除替换目录")
        shutil.rmtree(work_dir)


def _dependency_cleanup_intent_path(request_id):
    return _native_restore_path(request_id).with_suffix(".cleanup")


def _publish_dependency_cleanup_intent(request_id, operation_id):
    _ensure_private_directory(RESTORE_DIR)
    path = _dependency_cleanup_intent_path(request_id)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    encoded = json.dumps({"request_id": request_id, "operation_id": operation_id,
                          "intent_id": uuid.uuid4().hex}).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        publish_receipt(temporary, path)
        backup_commit._sync_directory(RESTORE_DIR)
    finally:
        temporary.unlink(missing_ok=True)


def _read_dependency_cleanup_intent(request_id, operation_id):
    try:
        fd = open_receipt(_dependency_cleanup_intent_path(request_id))
    except FileNotFoundError:
        return ""
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
            raise RuntimeError("依赖清理请求无效")
        encoded = stream.read(1025)
    if len(encoded) > 1024:
        raise RuntimeError("依赖清理请求超过安全大小")
    row = json.loads(encoded, object_pairs_hook=_unique_object)
    if (not isinstance(row, dict) or set(row) != {"request_id", "operation_id", "intent_id"}
        or row.get("request_id") != request_id
        or any(not isinstance(row.get(key), str) or not REQUEST_ID_RE.fullmatch(row[key])
               for key in ("operation_id", "intent_id"))):
        raise RuntimeError("依赖清理请求身份无效")
    return row["intent_id"] if row["operation_id"] == operation_id else ""


def _complete_dependency_cleanup(request_id, entry):
    with RESTORE_LOCK:
        record = _read_native_restore(request_id)
        if record is None or record.get("dependency_operation_id") != entry["operation_id"]:
            raise RuntimeError("原依赖操作已变化，未清理其他作业")
    _cleanup_dependency_resources(entry)
    with RESTORE_LOCK:
        record = _restore_record_update(
            request_id, dependency_state=entry["terminal_state"], dependency_error=entry["terminal_error"],
            dependency_cleanup_pending=False, dependency_guard=None, dependency_workspace=None,
        )
        RESTORE_DEPENDENCY_CLEANUP.pop(request_id, None)
        _release_process_lease(entry["lease_fd"])
        entry["lease_fd"] = None
        return record


def _watch_dependency_cleanup_requests(app, request_id, entry):
    with app.app_context():
        with RESTORE_LOCK:
            if RESTORE_DEPENDENCY_CLEANUP.get(request_id) is not entry:
                return
            # Old intents never authorize a fresh cleanup attempt after an owner restart.
            try:
                last_intent = _read_dependency_cleanup_intent(request_id, entry["operation_id"])
            except Exception:
                last_intent = ""
            try:
                _mark_restore_lease(entry["lease_fd"], request_id, "dependency-cleanup", entry["operation_id"])
            except Exception:
                return
        while True:
            with RESTORE_LOCK:
                if RESTORE_DEPENDENCY_CLEANUP.get(request_id) is not entry:
                    return
                busy = request_id in RESTORE_DEPENDENCY_CLEANING
            if not busy:
                try:
                    intent = _read_dependency_cleanup_intent(request_id, entry["operation_id"])
                except Exception:
                    intent = ""
                if intent and intent != last_intent:
                    with RESTORE_LOCK:
                        if RESTORE_DEPENDENCY_CLEANUP.get(request_id) is not entry:
                            return
                        if request_id in RESTORE_DEPENDENCY_CLEANING:
                            continue
                        RESTORE_DEPENDENCY_CLEANING.add(request_id)
                        last_intent = intent
                    try:
                        _complete_dependency_cleanup(request_id, entry)
                        return
                    except Exception as exc:
                        try:
                            _restore_record_update(
                                request_id, dependency_state="unknown", dependency_cleanup_pending=True,
                                dependency_error="依赖清理尚未确认：" + _bounded_text(str(exc), 900),
                            )
                        except Exception:
                            pass
                    finally:
                        with RESTORE_LOCK:
                            RESTORE_DEPENDENCY_CLEANING.discard(request_id)
            time.sleep(0.1)


def _start_dependency_cleanup_owner(app, request_id, entry):
    with RESTORE_LOCK:
        watcher = entry.get("watcher")
        if watcher is not None and watcher.is_alive():
            return
        watcher = threading.Thread(
            target=_watch_dependency_cleanup_requests, args=(app, request_id, entry),
            name="backup-native-cleanup-owner-" + request_id[:8], daemon=True,
        )
        entry["watcher"] = watcher
        try:
            _mark_restore_lease(entry["lease_fd"], request_id, "dependency-cleanup-starting", entry["operation_id"])
            watcher.start()
        except Exception:
            entry["watcher"] = None


def _run_native_restore_dependencies(app, request_id, process_lease_fd=None):
    work_dir = None
    process = None
    reader = None
    reader_started = False
    reader_errors = []
    workspace = None
    record = None
    output = bytearray()
    output_lock = threading.Lock()
    error = ""
    with app.app_context():
        try:
            record = _read_native_restore(request_id)
            if record is None:
                raise RuntimeError("restore record disappeared")
            preflight = _read_preflight_record(record["preflight_id"])
            if preflight is None:
                raise ValueError("preflight_archive_changed")
            work_dir = Path(tempfile.mkdtemp(prefix="deps-" + request_id[:8] + "-", dir=str(PREFLIGHT_WORK_DIR)))
            workspace = _dependency_workspace_identity(work_dir)
            _restore_record_update(request_id, dependency_cleanup_pending=True, dependency_workspace=workspace)
            suffix = {"tar.gz": ".tar.gz", "tgz": ".tgz", "tar": ".tar", "zip": ".zip"}[preflight["archive_format"]]
            archive_path = work_dir / ("source" + suffix)
            _fingerprint_preflight_archive(record["preflight_id"], record["archive_size_bytes"], record["archive_sha256"], archive_path)
            extract_dir = work_dir / "extracted"
            backup.extract_archive(archive_path, extract_dir)
            root = backup.find_backup_root(extract_dir)
            dep_file = backup.find_dependency_file(extract_dir, root) if root else None
            if dep_file is None:
                raise ValueError("dependency_file_missing")
            process = DependencyProcess(
                [sys.executable, "-m", "pip", "install", "-r", str(dep_file)],
                workspace=work_dir,
                cwd=str(backup.BASE_DIR),
                env=os.environ.copy(),
                lease_fd=process_lease_fd,
            )
            _restore_record_update(
                request_id, dependency_cleanup_pending=True,
                dependency_guard=process.identity, dependency_workspace=workspace,
            )
            process.start()
            reader = threading.Thread(
                target=_drain_restore_dependency_output,
                args=(process.stdout, output, output_lock, reader_errors),
                name="backup-native-restore-deps-output-" + request_id[:8],
                daemon=True,
            )
            reader.start()
            reader_started = True
            deadline = time.monotonic() + RESTORE_WORKER_TIMEOUT_SECONDS
            try:
                while True:
                    if reader_errors:
                        raise RuntimeError(reader_errors[0])
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired("pip", RESTORE_WORKER_TIMEOUT_SECONDS)
                    try:
                        return_code = process.wait(timeout=min(2, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        with output_lock:
                            progress_output = bytes(output).decode("utf-8", errors="replace")
                        _restore_record_update(request_id, dependency_output=_bounded_text(progress_output, RESTORE_DEPENDENCY_OUTPUT_LIMIT))
            except subprocess.TimeoutExpired:
                error = "依赖安装超过 30 分钟，已终止子进程"
                return_code = -1
            if return_code != 0 and not error:
                error = "pip 依赖安装失败，查看下方有界输出并检查面板环境"
        except Exception as exc:
            error = _bounded_text(str(exc), 1000)
        finally:
            entry = {
                "process": process, "reader": reader if reader_started else None,
                "workspace": workspace, "guard": process.identity if process is not None else None,
                "lease_fd": process_lease_fd, "operation_id": (record or {}).get("dependency_operation_id", ""),
            }
            cleanup_complete = True
            try:
                _cleanup_dependency_resources(entry)
            except Exception as exc:
                cleanup_complete = False
                cleanup_error = _bounded_text(str(exc), 1000)
            if reader_errors:
                error = reader_errors[0]
            entry.update(terminal_state="failed" if error else "succeeded", terminal_error=error)
            with output_lock:
                output_text = output.decode("utf-8", errors="replace")
            try:
                _restore_record_update(
                    request_id,
                    dependency_state="unknown" if not cleanup_complete else "failed" if error else "succeeded",
                    dependency_error=error if cleanup_complete else cleanup_error,
                    dependency_output=_bounded_text(output_text, RESTORE_DEPENDENCY_OUTPUT_LIMIT),
                    dependency_cleanup_pending=not cleanup_complete,
                    dependency_guard=entry["guard"], dependency_workspace=workspace,
                )
            except Exception:
                cleanup_complete = False
            with RESTORE_LOCK:
                RESTORE_DEPENDENCY_ACTIVE.discard(request_id)
                try:
                    record = _read_native_restore(request_id)
                    if record is not None:
                        PREFLIGHT_RETAINED_FOR_RESTORE.discard(record["preflight_id"])
                except Exception:
                    pass
                if not cleanup_complete:
                    RESTORE_DEPENDENCY_CLEANUP[request_id] = entry
            if cleanup_complete:
                _release_process_lease(process_lease_fd)
            else:
                _start_dependency_cleanup_owner(app, request_id, entry)


@bp.route(NATIVE_PREFIX + "/restores/<request_id>/dependencies/cleanup", methods=["POST"])
def api_backup_native_cleanup_restore_dependencies(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "恢复请求标识无效", 400)
    body, error = _json_body({"operation_id"}, {"operation_id"})
    if error:
        return error
    operation_id = body["operation_id"]
    if not isinstance(operation_id, str) or not REQUEST_ID_RE.fullmatch(operation_id):
        return _error("invalid_operation", "原依赖操作标识无效", 400)
    with RESTORE_LOCK:
        try:
            record = _read_native_restore(request_id)
            if record is None:
                return _error("restore_not_found", "恢复请求不存在", 404)
            if record.get("dependency_operation_id") != operation_id:
                return _error("operation_id_conflict", "该标识不是当前依赖操作，未执行清理", 409)
            if request_id in RESTORE_DEPENDENCY_ACTIVE or request_id in RESTORE_DEPENDENCY_CLEANING:
                return _error("dependency_busy", "该依赖操作仍在执行或清理，请先查询原请求", 409)
            entry = RESTORE_DEPENDENCY_CLEANUP.get(request_id)
            if entry is None:
                if not record.get("dependency_cleanup_pending", False):
                    return _native_restore_response(record, replayed=True)
                fd = _try_process_lease(RESTORE_PROCESS_LOCK_PATH)
                if fd is None:
                    if _restore_lease_matches(record, "dependency-cleanup"):
                        _publish_dependency_cleanup_intent(request_id, operation_id)
                        record = _read_native_restore(request_id)
                        if record is None or record.get("dependency_operation_id") != operation_id:
                            return _error("operation_id_conflict", "依赖操作已变化，请查询原请求", 409)
                        return _response({"ok": True, "cleanup_requested": True,
                                          "restore": _native_restore_payload(record)}, 202)
                    return _error("restore_busy", "依赖监管或其他恢复操作尚未退出，请稍后查询", 409)
                try:
                    record = _read_native_restore(request_id)
                    if record is None or record.get("dependency_operation_id") != operation_id:
                        _release_process_lease(fd)
                        return _error("operation_id_conflict", "依赖操作已变化，未执行清理", 409)
                    if not record.get("dependency_cleanup_pending", False):
                        _release_process_lease(fd)
                        return _native_restore_response(record, replayed=True)
                    _mark_restore_lease(fd, request_id, "dependency-cleanup-active", operation_id)
                except Exception:
                    _release_process_lease(fd)
                    raise
                entry = {
                    "operation_id": operation_id, "process": None, "reader": None,
                    "guard": record.get("dependency_guard"), "workspace": record.get("dependency_workspace"),
                    "lease_fd": fd, "terminal_state": "unknown",
                    "terminal_error": "清理已完成，原依赖安装结果未确认；请检查输出和面板环境",
                }
                RESTORE_DEPENDENCY_CLEANUP[request_id] = entry
            if entry["operation_id"] != operation_id:
                return _error("operation_id_conflict", "清理句柄属于其他依赖操作", 409)
            RESTORE_DEPENDENCY_CLEANING.add(request_id)
        except Exception:
            return _error("dependency_cleanup_unavailable", "无法读取依赖清理身份，请查询原请求", 503)
    try:
        record = _complete_dependency_cleanup(request_id, entry)
        return _native_restore_response(record)
    except Exception as exc:
        try:
            record = _restore_record_update(
                request_id, dependency_state="unknown", dependency_cleanup_pending=True,
                dependency_error="依赖清理尚未确认：" + _bounded_text(str(exc), 900),
            )
        except Exception:
            pass
        _start_dependency_cleanup_owner(current_app._get_current_object(), request_id, entry)
        return _response({
            "ok": False, "code": "dependency_cleanup_incomplete", "msg": "清理尚未完成，保留原作业占用；可以查询后再次清理",
            "restore": _native_restore_payload(record),
        }, 503)
    finally:
        with RESTORE_LOCK:
            RESTORE_DEPENDENCY_CLEANING.discard(request_id)


@bp.route(NATIVE_PREFIX + "/restores/<request_id>/dependencies", methods=["POST"])
def api_backup_native_restore_dependencies(request_id):
    if not REQUEST_ID_RE.fullmatch(request_id):
        return _error("invalid_request_id", "恢复请求标识无效", 400)
    body, error = _json_body({"operation_id", "confirm_retry"}, {"operation_id", "confirm_retry"})
    if error:
        return error
    operation_id = body["operation_id"]
    if not isinstance(operation_id, str) or not REQUEST_ID_RE.fullmatch(operation_id) or type(body["confirm_retry"]) is not bool:
        return _error("invalid_operation", "依赖安装操作标识或确认无效", 400)
    with RESTORE_LOCK:
        try:
            record, process_lease_fd, response = _claim_restore_followup(
                request_id, "dependency", operation_id, body["confirm_retry"],
            )
            if response:
                return response
            RESTORE_DEPENDENCY_ACTIVE.add(request_id)
            PREFLIGHT_RETAINED_FOR_RESTORE.add(record["preflight_id"])
        except Exception:
            return _error("restore_unavailable", "无法安全开始依赖安装，请查询原请求", 503)
    try:
        app = current_app._get_current_object()
        worker = threading.Thread(
            target=_run_native_restore_dependencies,
            args=(app, request_id, process_lease_fd),
            name="backup-native-restore-deps-" + request_id[:8],
            daemon=True,
        )
        worker.start()
    except Exception as exc:
        with RESTORE_LOCK:
            RESTORE_DEPENDENCY_ACTIVE.discard(request_id)
            PREFLIGHT_RETAINED_FOR_RESTORE.discard(record["preflight_id"])
            try:
                record = _read_native_restore(request_id)
                if record is not None:
                    PREFLIGHT_RETAINED_FOR_RESTORE.discard(record["preflight_id"])
            except Exception:
                pass
        try:
            record = _restore_record_update(
                request_id, dependency_state="failed",
                dependency_error="依赖作业无法启动：" + _bounded_text(str(exc), 900),
            )
            return _response({"ok": False, "restore": _native_restore_payload(record), "msg": "依赖安装作业无法启动"}, 503)
        except Exception:
            return _error("dependency_start_unknown", "依赖安装启动状态无法持久确认，请查询原请求", 503)
        finally:
            _release_process_lease(process_lease_fd)
    latest = _read_native_restore(request_id)
    return _native_restore_response(latest or record, 202 if latest and latest["dependency_state"] == "running" else 200)
