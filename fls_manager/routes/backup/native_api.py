import json
import os
import re
import stat
from contextlib import contextmanager

from flask import jsonify, request, send_file

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
    })


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
