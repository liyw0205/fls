"""Fixed log snapshots and durable, non-repeating native cleanup requests."""

import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager

from flask import current_app

from .backup_lease import release_process_lease, try_process_lease
from .log_access import (
    LogFileBusy, delete_log_file, file_version, log_directory_version,
    log_file_access, log_file_busy,
)
from .paths import BASE_DIR, LOG_DIR
from .receipt_io import open_receipt as _open_receipt, publish_receipt as _publish_receipt


MAX_FILES = 200
MAX_GROUPS = 20
PREVIEW_TTL = 600
MAX_PREVIEWS = 32
MAX_REQUESTS = 512
MAX_RECORD_BYTES = 512 * 1024
MAX_SCAN_FILES = 10000
ID_RE = re.compile(r"[a-f0-9]{32}\Z")
VERSION_RE = re.compile(r"[a-f0-9]{64}\Z")
RESULTS = ("deleted", "missing", "busy", "conflict", "failed", "unknown")
STORE = BASE_DIR / ".log_cleanup"


class CleanupError(RuntimeError):
    def __init__(self, message, code="cleanup_unavailable", status=503):
        super().__init__(message)
        self.code, self.status = code, status


def _invalid(message="日志清理请求无效", *, status=400):
    raise CleanupError(message, "invalid_cleanup", status)


def valid_id(value):
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def valid_version(value):
    return isinstance(value, str) and VERSION_RE.fullmatch(value) is not None


def _filename(value):
    if (not isinstance(value, str) or not value.endswith(".log") or value != value.strip()
            or any(c in value for c in ("/", "\\", ":", "\0"))
            or any(ord(c) < 32 for c in value)):
        _invalid("日志文件名无效")
    try:
        if len(value.encode("utf-8")) > 255:
            _invalid("日志文件名过长")
    except UnicodeError:
        _invalid("日志文件名编码无效")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _signature(row):
    key = current_app.secret_key
    return hmac.new(key.encode("utf-8") if isinstance(key, str) else key, _encoded(row), hashlib.sha256).hexdigest()


def _store_info():
    info = STORE.lstat()
    if (not stat.S_ISDIR(info.st_mode) or STORE.is_symlink()
            or (hasattr(STORE, "is_junction") and STORE.is_junction())):
        raise CleanupError("日志清理回执目录无效")
    return info.st_dev, info.st_ino


def _validate(kind, row, identifier):
    fields = {"preview_id", "digest", "created_at", "directory_version", "items"}
    fields |= {"expires_at", "groups", "group_versions"} if kind == "preview" else {"request_id", "reconciled"}
    if (not isinstance(row, dict) or set(row) != fields or row[kind + "_id"] != identifier
            or not valid_id(row["preview_id"]) or not valid_version(row["digest"])
            or not valid_version(row["directory_version"])
            or type(row["created_at"]) not in (int, float) or not math.isfinite(row["created_at"])
            or not isinstance(row["items"], list) or not 1 <= len(row["items"]) <= MAX_FILES):
        raise ValueError("invalid cleanup receipt")
    names = set()
    states = {"ready", "busy", "conflict", "missing"} if kind == "preview" else set(RESULTS) | {"pending", "deleting"}
    for item in row["items"]:
        if (not isinstance(item, dict) or set(item) != {"filename", "version", "group", "size_bytes", "status"}
                or not valid_version(item["version"]) or not isinstance(item["group"], str)
                or len(item["group"]) > 512 or type(item["size_bytes"]) is not int or item["size_bytes"] < 0
                or item["status"] not in states):
            raise ValueError("invalid cleanup item")
        name = os.path.normcase(_filename(item["filename"]))
        if name in names:
            raise ValueError("duplicate cleanup item")
        names.add(name)
    if kind == "preview":
        if (type(row["expires_at"]) not in (int, float) or not math.isfinite(row["expires_at"])
                or not isinstance(row["groups"], list) or len(row["groups"]) > MAX_GROUPS
                or any(not isinstance(group, str) for group in row["groups"])
                or not isinstance(row["group_versions"], dict)
                or set(row["groups"]) != set(row["group_versions"])
                or any(not valid_version(value) for value in row["group_versions"].values())):
            raise ValueError("invalid cleanup preview")
    elif not valid_id(row["request_id"]) or type(row["reconciled"]) is not bool:
        raise ValueError("invalid cleanup request")


def _read(kind, identifier):
    if not valid_id(identifier):
        _invalid("日志清理请求标识无效")
    directory = None
    try:
        directory = _store_info()
        path = STORE / f"{kind}-{identifier}.json"
        fd = _open_receipt(path)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("invalid cleanup receipt")
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if _store_info() != directory or len(raw) > MAX_RECORD_BYTES:
            raise ValueError("invalid cleanup receipt")
        row = json.loads(raw, object_pairs_hook=_unique)
        signature = row.pop("signature")
        if not isinstance(signature, str) or not hmac.compare_digest(signature, _signature(row)):
            raise ValueError("invalid cleanup signature")
        _validate(kind, row, identifier)
        return row
    except FileNotFoundError:
        if directory is not None:
            try:
                if _store_info() != directory:
                    raise CleanupError("日志清理回执目录已变化")
            except OSError as exc:
                raise CleanupError("日志清理回执目录在读取期间变化") from exc
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, RecursionError) as exc:
        raise CleanupError("日志清理回执无法核对，保留原文件与原请求") from exc


def _write(kind, row):
    STORE.mkdir(mode=0o700, exist_ok=True)
    directory = _store_info()
    _validate(kind, row, row[kind + "_id"])
    encoded = _encoded({**row, "signature": _signature(row)})
    if len(encoded) > MAX_RECORD_BYTES:
        raise CleanupError("日志清理回执超过容量限制")
    fd, temporary = tempfile.mkstemp(prefix=".cleanup-", dir=STORE)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if _store_info() != directory:
            raise CleanupError("日志清理回执目录已变化")
        _publish_receipt(temporary, STORE / (kind + "-" + row[kind + "_id"] + ".json"))
        if os.name == "posix":
            directory_fd = os.open(STORE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _transaction():
    fd = None
    try:
        fd = try_process_lease(BASE_DIR / ".log_cleanup.lock")
        if fd is None:
            raise CleanupError("另一个日志清理操作正在处理，请查询原请求或稍后预检查", "cleanup_busy", 409)
        yield
    except CleanupError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise CleanupError("日志清理存储暂不可用，请查询原请求") from exc
    finally:
        release_process_lease(fd)


def _reserve(kind):
    try:
        _store_info()
    except FileNotFoundError:
        return
    records = []
    # Never silently discard uncertain requests or overwrite corrupt receipts.
    for path in STORE.iterdir():
        if len(records) >= MAX_REQUESTS + MAX_PREVIEWS:
            raise CleanupError("日志清理存储已满，保留现有请求", "cleanup_capacity", 409)
        if path.name.startswith(("request-", "preview-")):
            prefix, _, suffix = path.name.partition("-")
            identifier = suffix.removesuffix(".json")
            if not path.name.endswith(".json") or not valid_id(identifier):
                raise CleanupError("日志清理回执名称无效")
            record = _read(prefix, identifier)
            if record is None:
                raise CleanupError("日志清理回执在读取期间变化")
            records.append((prefix, path, record))
        else:
            raise CleanupError("日志清理存储存在未完成现场，需保留核对")
    count = 0
    for prefix, path, record in records:
        if kind == "preview" and prefix == "preview" and record["expires_at"] <= time.time():
            path.unlink()
        elif prefix == kind:
            count += 1
    if count >= (MAX_PREVIEWS if kind == "preview" else MAX_REQUESTS):
        raise CleanupError("日志清理存储已满，请保留原请求并稍后处理", "cleanup_capacity", 409)


def _directory_version():
    if LOG_DIR.is_symlink() or (hasattr(LOG_DIR, "is_junction") and LOG_DIR.is_junction()):
        raise CleanupError("日志目录无效")
    return log_directory_version(LOG_DIR.lstat())


def _snapshot(filename, expected, group_name):
    path = LOG_DIR / filename
    try:
        with log_file_access(path) as access:
            info = access.info()
            version = file_version(info)
            group = group_name(path)
            changed = file_version(access.info()) != version
        return {"filename": filename, "version": expected or version, "group": group[:512],
                "size_bytes": info.st_size,
                "status": "conflict" if changed or expected and expected != version
                else "busy" if log_file_busy(path) else "ready"}
    except FileNotFoundError:
        if expected is None:
            raise CleanupError("日志分组在预检查期间变化，请重新选择", "snapshot_conflict", 409)
        return {"filename": filename, "version": expected, "group": "", "size_bytes": 0, "status": "missing"}
    except LogFileBusy as exc:
        raise CleanupError("日志正在清理，请稍后重新预检查", "log_file_busy", 409) from exc
    except ValueError as exc:
        raise CleanupError("日志目标不是安全的常规文件", "invalid_log_file", 409) from exc


def preview_cleanup(selection, group_name):
    if set(selection) not in ({"files"}, {"groups"}):
        _invalid("请选择文件或分组进行预检查")
    files, groups = selection.get("files"), selection.get("groups", [])
    if files is not None:
        if not isinstance(files, list) or not files:
            _invalid("请选择日志文件")
        if len(files) > MAX_FILES:
            _invalid("每次最多选择 200 个文件", status=413)
        names = []
        for item in files:
            if not isinstance(item, dict) or set(item) != {"filename", "version"} or not valid_version(item["version"]):
                _invalid("文件版本或选择无效")
            names.append(os.path.normcase(_filename(item["filename"])))
        if len(set(names)) != len(names):
            _invalid("文件选择重复")
    else:
        if not isinstance(groups, list) or not groups:
            _invalid("请选择日志分组")
        if len(groups) > MAX_GROUPS:
            _invalid("每次最多选择 20 个分组", status=413)
        if (any(not isinstance(group, str) or not group or group != group.strip()
                or len(group) > 512 or any(ord(c) < 32 for c in group) for group in groups)
                or len(set(groups)) != len(groups)):
            _invalid("日志分组选择无效")
    with _transaction():
        _reserve("preview")
        directory = _directory_version()
        if files is not None:
            items = [_snapshot(item["filename"], item["version"], group_name) for item in files]
        else:
            items = []
            scanned = 0
            for path in LOG_DIR.iterdir():
                scanned += 1
                if scanned > MAX_SCAN_FILES:
                    _invalid("日志过多，请改为选择具体文件", status=413)
                if not path.name.endswith(".log"):
                    continue
                _filename(path.name)
                item = _snapshot(path.name, None, group_name)
                if item["group"] in groups:
                    items.append(item)
                    if len(items) > MAX_FILES:
                        _invalid("分组超过 200 个文件，请缩小选择", status=413)
            if not items:
                raise CleanupError("所选分组已无日志，请刷新列表", "logs_missing", 409)
        if _directory_version() != directory:
            raise CleanupError("日志目录在预检查期间变化", "snapshot_conflict", 409)
        items.sort(key=lambda item: item["filename"])
        versions = {group: hashlib.sha256(_encoded([(item["filename"], item["version"])
                    for item in items if item["group"] == group])).hexdigest() for group in groups}
        row = {"preview_id": uuid.uuid4().hex, "created_at": time.time(), "expires_at": time.time() + PREVIEW_TTL,
               "directory_version": directory, "groups": groups, "group_versions": versions, "items": items}
        row["digest"] = hashlib.sha256(_encoded(row)).hexdigest()
        _write("preview", row)
        return {key: value for key, value in row.items() if key != "directory_version"} | {
            "total_files": len(items), "total_bytes": sum(item["size_bytes"] for item in items)}


def _request_payload(row):
    items = [{**item, "status": "unknown" if item["status"] in ("pending", "deleting") else item["status"]}
             for item in row["items"]]
    return {"request_id": row["request_id"], "preview_id": row["preview_id"], "digest": row["digest"],
            "created_at": row["created_at"], "reconciled": row["reconciled"], "items": items,
            "status": "unknown" if any(item["status"] == "unknown" for item in items) else "completed",
            "counts": {status: sum(item["status"] == status for item in items) for status in RESULTS}}


def get_cleanup_request(request_id):
    row = _read("request", request_id)
    return _request_payload(row) if row else {"request_id": request_id, "status": "missing", "items": [],
                                             "counts": dict.fromkeys(RESULTS, 0)}


def execute_cleanup(request_id, preview_id, digest):
    if not valid_id(request_id) or not valid_id(preview_id) or not valid_version(digest):
        _invalid("日志清理请求身份无效")
    with _transaction():
        previous = _read("request", request_id)
        if previous:
            if previous["preview_id"] != preview_id or previous["digest"] != digest:
                raise CleanupError("请求标识已绑定另一份日志清单", "request_mismatch", 409)
            return _request_payload(previous)
        preview = _read("preview", preview_id)
        if preview is None or preview["expires_at"] <= time.time():
            raise CleanupError("预检查已过期或不存在，请重新预检查并确认", "preview_expired", 409)
        if preview["digest"] != digest:
            raise CleanupError("预检查清单不匹配", "preview_mismatch", 409)
        _reserve("request")
        row = {"request_id": request_id, "preview_id": preview_id, "digest": digest,
               "created_at": time.time(), "directory_version": preview["directory_version"],
               "reconciled": False, "items": [{**item, "status": "pending"} for item in preview["items"]]}
        _write("request", row)
        for item in row["items"]:
            def prepare():
                item["status"] = "deleting"
                try:
                    _write("request", row)
                except (OSError, ValueError, RuntimeError) as exc:
                    raise CleanupError("删除意图未能确认保存，停止清理并保留原请求") from exc

            status, _, _ = delete_log_file(LOG_DIR / item["filename"], item["version"], prepare=prepare,
                                          expected_directory=row["directory_version"])
            item["status"] = status
            _write("request", row)
        return _request_payload(row)


def reconcile_cleanup(request_id):
    with _transaction():
        row = _read("request", request_id)
        if row is None:
            return get_cleanup_request(request_id)
        for item in row["items"]:
            if item["status"] not in ("pending", "deleting", "unknown"):
                continue
            item["status"] = "unknown"
            try:
                with log_file_access(LOG_DIR / item["filename"]) as access:
                    if log_directory_version(access.expected) != row["directory_version"]:
                        item["status"] = "conflict"
                    elif file_version(access.info()) != item["version"]:
                        item["status"] = "conflict"
            except FileNotFoundError:
                item["status"] = "missing"
            except (LogFileBusy, OSError, ValueError):
                pass
        row["reconciled"] = True
        _write("request", row)
        return _request_payload(row)
