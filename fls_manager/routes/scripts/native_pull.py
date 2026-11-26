import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import time
import uuid
from urllib.parse import urlparse

from flask import jsonify, request

from . import bp
from .helpers import requests
from ...directory_jobs import (
    DirectoryJobCancel, DirectoryJobLock, directory_job_operation,
    directory_job_rows, start_directory_job,
)
from ...paths import SCRIPT_DIR, SCRIPT_PULL_JOBS_FILE
from ...models import load_proxies
from ...proxy import github_proxy_url, requests_proxy_dict, validate_proxy_id
from ...script_files import script_file_target
from ...storage import write_json
from ...utils import now_str

SCRIPT_PULL_MAX_BYTES = 64 * 1024 * 1024
SCRIPT_PULL_HISTORY_LIMIT = 200
SCRIPT_PULL_ACTIVE_LIMIT = 4
SCRIPT_PULL_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SCRIPT_PULL_ACTIVE = ("pending", "downloading", "publishing", "cancelling")
SCRIPT_PULL_TERMINAL = ("complete", "failed", "cancelled", "unknown")
SCRIPT_PULL_LOCK = DirectoryJobLock("pull", lambda: _refresh_locked())
SCRIPT_PULL_JOBS = {}
SCRIPT_PULL_CANCEL = {}
SCRIPT_PULL_INITIALIZED = False


class _PullCancelled(Exception):
    pass


class _PullTooLarge(Exception):
    pass


class _PullPersistenceError(Exception):
    pass


class _PullPathChanged(Exception):
    pass


def _response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message, status, code):
    return _response({"ok": False, "code": code, "msg": message}, status)


def _clean_temp(target, job_id):
    temp = target.with_name(f".fls-pull-{job_id}.tmp")
    try:
        if not temp.is_symlink() and temp.is_file():
            temp.unlink()
    except OSError:
        pass


def _open_script_parent(relative):
    target = script_file_target(relative)
    name = target.name
    if os.open not in getattr(os, "supports_dir_fd", set()) or not hasattr(os, "O_DIRECTORY"):
        metadata = target.parent.stat(follow_symlinks=False)
        return target, None, name, (metadata.st_dev, metadata.st_ino)

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    root = SCRIPT_DIR.resolve()
    fd = os.open(str(root), flags)
    try:
        relative_parent = target.parent.relative_to(root)
        for part in relative_parent.parts:
            next_fd = os.open(
                part,
                flags | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
    except Exception:
        os.close(fd)
        raise
    metadata = os.fstat(fd)
    return target, fd, name, (metadata.st_dev, metadata.st_ino)


def _temp_name(job_id):
    return f".fls-pull-{job_id}.tmp"


def _open_temp(target, parent_fd, job_id):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    name = _temp_name(job_id)
    if parent_fd is not None:
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(name, flags, 0o600, dir_fd=parent_fd), name
    return os.open(str(target.with_name(name)), flags, 0o600), name


def _open_staged(target, parent_fd, name):
    if parent_fd is not None:
        return os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    return os.open(str(target.with_name(name)), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))


def _unlink_at(target, parent_fd, name):
    try:
        if parent_fd is not None:
            os.unlink(name, dir_fd=parent_fd)
        else:
            target.with_name(name).unlink(missing_ok=True)
    except OSError:
        pass


def _renameat2_no_replace(source, destination, parent_fd):
    if os.name != "posix":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    source_fd = at_fdcwd if parent_fd is None else parent_fd
    destination_fd = at_fdcwd if parent_fd is None else parent_fd
    result = renameat2(
        source_fd,
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return True
    code = ctypes.get_errno()
    unsupported = {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if code in unsupported:
        return False
    raise OSError(code, os.strerror(code), str(destination))


def _target_matches(job):
    parent_fd = None
    try:
        target, parent_fd, _target_name, parent_identity = _open_script_parent(job["path"])
        expected_identity = (job.get("target_dev"), job.get("target_ino"))
        if not all(type(value) is int for value in expected_identity):
            return False

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        if parent_fd is None:
            fd = os.open(target, flags)
        else:
            fd = os.open(target.name, flags, dir_fd=parent_fd)
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or (metadata.st_dev, metadata.st_ino) != expected_identity
            ):
                return False
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
        return (
            digest.hexdigest() == job.get("sha256")
            and _script_path_still_matches(job["path"], target, parent_fd, parent_identity)
        )
    except (OSError, KeyError, ValueError):
        return False
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _save_locked():
    ordered = sorted(
        SCRIPT_PULL_JOBS.values(),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    keep_ids = {item["id"] for item in ordered[:SCRIPT_PULL_HISTORY_LIMIT]}
    keep_ids.update(item["id"] for item in ordered if item.get("status") in SCRIPT_PULL_ACTIVE)
    rows = [item for item in ordered if item["id"] in keep_ids]
    write_json(SCRIPT_PULL_JOBS_FILE, rows)
    for job_id in tuple(SCRIPT_PULL_JOBS):
        if job_id not in keep_ids:
            SCRIPT_PULL_JOBS.pop(job_id, None)


def _ensure_loaded():
    global SCRIPT_PULL_INITIALIZED
    with SCRIPT_PULL_LOCK:
        SCRIPT_PULL_INITIALIZED = True


def _refresh_locked():
    loaded = {}
    for row in directory_job_rows(SCRIPT_PULL_JOBS_FILE, SCRIPT_PULL_LOCK, SCRIPT_PULL_JOBS, SCRIPT_PULL_CANCEL):
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or "")
        request_id = str(row.get("request_id") or "")
        if not SCRIPT_PULL_ID_RE.fullmatch(job_id) or not SCRIPT_PULL_ID_RE.fullmatch(request_id):
            continue
        status = str(row.get("status") or "unknown")
        if status in SCRIPT_PULL_ACTIVE and not row["_worker_active"]:
            if status == "publishing" and _target_matches(row):
                status = "complete"
            else:
                status = "unknown"
                row = dict(row, error_code="worker_interrupted")
                try:
                    _clean_temp(script_file_target(row.get("path", "")), job_id)
                except ValueError:
                    pass
        if status not in SCRIPT_PULL_TERMINAL + SCRIPT_PULL_ACTIVE:
            status = "unknown"
        normalized = {
            "id": job_id,
            "request_id": request_id,
            "fingerprint": str(row.get("fingerprint") or ""),
            "kind": "file",
            "path": str(row.get("path") or ""),
            "source_host": str(row.get("source_host") or "")[:255],
            "status": status,
            "downloaded_bytes": max(0, _int(row.get("downloaded_bytes"), 0)),
            "total_bytes": _optional_nonnegative_int(row.get("total_bytes")),
            "sha256": str(row.get("sha256") or ""),
            "target_dev": row.get("target_dev") if type(row.get("target_dev")) is int else None,
            "target_ino": row.get("target_ino") if type(row.get("target_ino")) is int else None,
            "error_code": str(row.get("error_code") or "")[:80],
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }
        loaded[job_id] = normalized
    SCRIPT_PULL_JOBS.clear()
    SCRIPT_PULL_JOBS.update(loaded)


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_nonnegative_int(value):
    if type(value) is not int or value < 0:
        return None
    return value


def _payload(job, *, replayed=False):
    status = job.get("status", "unknown")
    total = job.get("total_bytes")
    downloaded = max(0, _int(job.get("downloaded_bytes"), 0))
    progress_state = {
        "pending": "pending",
        "downloading": "downloading" if total is not None else "indeterminate",
        "publishing": "publishing",
        "cancelling": "cancelling",
        "complete": "complete",
        "failed": "failed",
        "cancelled": "cancelled",
        "unknown": "unknown",
    }.get(status, "unknown")
    percent = None
    if total is not None and total > 0 and downloaded <= total:
        percent = min(100, int(downloaded * 100 / total))
    return {
        "id": job.get("id", ""),
        "request_id": job.get("request_id", ""),
        "kind": "file",
        "path": job.get("path", ""),
        "source_host": job.get("source_host", ""),
        "status": status,
        "progress": {
            "state": progress_state,
            "downloaded_bytes": downloaded,
            "total_bytes": total,
            "percent": percent,
        },
        "error_code": job.get("error_code", ""),
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        "replayed": replayed,
    }


def _update(job_id, *, persist=True, **updates):
    with SCRIPT_PULL_LOCK:
        job = SCRIPT_PULL_JOBS.get(job_id)
        if not job:
            return None
        if job.get("status") == "cancelling" and updates.get("status", "cancelling") in SCRIPT_PULL_ACTIVE:
            updates["status"] = "cancelling"
        job.update(updates)
        job["updated_at"] = now_str()
        if persist:
            try:
                _save_locked()
            except Exception as exc:
                raise _PullPersistenceError from exc
        return dict(job)


def _publish_no_replace(target, parent_fd, temp_name, target_name):
    link = getattr(os, "link", None)
    if callable(link):
        try:
            if parent_fd is not None and os.link in getattr(os, "supports_dir_fd", set()):
                os.link(
                    temp_name,
                    target_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            elif parent_fd is None:
                temp = target.with_name(temp_name)
                os.link(temp, target, follow_symlinks=False)
            else:
                raise NotImplementedError
            if parent_fd is not None:
                os.unlink(temp_name, dir_fd=parent_fd)
            else:
                target.with_name(temp_name).unlink()
            return
        except (OSError, NotImplementedError) as exc:
            unsupported = {errno.ENOSYS, errno.EPERM, errno.EACCES, errno.EINVAL}
            unsupported.update(
                value for value in (getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None))
                if value is not None
            )
            if not isinstance(exc, NotImplementedError) and getattr(exc, "errno", None) not in unsupported:
                raise
    source = temp_name if parent_fd is not None else target.with_name(temp_name)
    destination = target_name if parent_fd is not None else target
    if _renameat2_no_replace(source, destination, parent_fd):
        return
    if os.name == "nt":
        os.rename(target.with_name(temp_name), target)
        return
    raise OSError(errno.ENOTSUP, "atomic no-replace publish is unavailable", str(target))


def _script_path_still_matches(relative, target, parent_fd, parent_identity):
    try:
        if script_file_target(relative) != target:
            return False
        metadata = target.parent.stat(follow_symlinks=False)
        current_identity = (metadata.st_dev, metadata.st_ino)
        if parent_fd is not None:
            opened = os.fstat(parent_fd)
            return current_identity == (opened.st_dev, opened.st_ino) == parent_identity
        return current_identity == parent_identity
    except (OSError, ValueError):
        return False


def _unlink_published_if_same(target, parent_fd, name, identity):
    try:
        if parent_fd is not None:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        else:
            current = target.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) == identity:
            _unlink_at(target, parent_fd, name)
    except OSError:
        pass


def _run_pull(job_id, url, proxy_id):
    with SCRIPT_PULL_LOCK:
        cancel = SCRIPT_PULL_CANCEL.get(job_id)
        job = SCRIPT_PULL_JOBS.get(job_id)
    if cancel is None or job is None:
        return

    response = None
    target = None
    parent_fd = None
    parent_identity = None
    temp_name = _temp_name(job_id)
    target_name = None
    temp_created = False
    try:
        try:
            target, parent_fd, target_name, parent_identity = _open_script_parent(job["path"])
        except (OSError, ValueError) as exc:
            raise _PullPathChanged from exc
        if cancel.is_set():
            raise _PullCancelled
        real_url = github_proxy_url(url, proxy_id)
        response = requests.get(
            real_url,
            headers={"User-Agent": "Mozilla/5.0 FLS-Manager", "Accept-Encoding": "identity"},
            timeout=(10, 60),
            proxies=requests_proxy_dict(proxy_id),
            stream=True,
        )
        response.raise_for_status()
        try:
            total = int(response.headers.get("Content-Length", ""))
            if total < 0:
                total = None
            elif total > SCRIPT_PULL_MAX_BYTES:
                raise _PullTooLarge
        except (TypeError, ValueError):
            total = None

        fd, temp_name = _open_temp(target, parent_fd, job_id)
        temp_created = True
        downloaded = 0
        saved_at = time.monotonic()
        with os.fdopen(fd, "wb") as output:
            _update(job_id, status="downloading", total_bytes=total, downloaded_bytes=0)
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if cancel.is_set():
                    raise _PullCancelled
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > SCRIPT_PULL_MAX_BYTES:
                    raise _PullTooLarge
                output.write(chunk)
                _update(
                    job_id,
                    persist=time.monotonic() - saved_at >= 0.5,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                )
                if time.monotonic() - saved_at >= 0.5:
                    saved_at = time.monotonic()
            output.flush()
            os.fsync(output.fileno())

        if total is not None and total != downloaded:
            total = None
        digest = hashlib.sha256()
        staged_fd = _open_staged(target, parent_fd, temp_name)
        with os.fdopen(staged_fd, "rb") as staged:
            for chunk in iter(lambda: staged.read(128 * 1024), b""):
                digest.update(chunk)
            staged_metadata = os.fstat(staged.fileno())
        target_identity = (staged_metadata.st_dev, staged_metadata.st_ino)
        _update(
            job_id,
            status="publishing",
            total_bytes=total,
            downloaded_bytes=downloaded,
            sha256=digest.hexdigest(),
            target_dev=target_identity[0],
            target_ino=target_identity[1],
        )
        with SCRIPT_PULL_LOCK:
            if cancel.is_set():
                raise _PullCancelled
            if not _script_path_still_matches(job["path"], target, parent_fd, parent_identity):
                raise _PullPathChanged
            _publish_no_replace(target, parent_fd, temp_name, target_name)
            temp_created = False
            try:
                if parent_fd is not None:
                    published_metadata = os.stat(
                        target_name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                else:
                    published_metadata = target.stat(follow_symlinks=False)
            except OSError as exc:
                raise _PullPathChanged from exc
            if (
                (published_metadata.st_dev, published_metadata.st_ino) != target_identity
                or not _script_path_still_matches(job["path"], target, parent_fd, parent_identity)
            ):
                _unlink_published_if_same(target, parent_fd, target_name, target_identity)
                raise _PullPathChanged
            current = SCRIPT_PULL_JOBS[job_id]
            current.update(status="complete", error_code="", total_bytes=total, downloaded_bytes=downloaded)
            current["updated_at"] = now_str()
            try:
                _save_locked()
            except Exception as exc:
                raise _PullPersistenceError from exc
    except _PullCancelled:
        _finish_or_fail(job_id, target, "cancelled", "", parent_fd=parent_fd, temp_name=temp_name)
    except _PullTooLarge:
        _finish_or_fail(job_id, target, "failed", "too_large", parent_fd=parent_fd, temp_name=temp_name)
    except FileExistsError:
        _finish_or_fail(job_id, target, "failed", "target_exists", parent_fd=parent_fd, temp_name=temp_name)
    except _PullPathChanged:
        _finish_or_fail(job_id, target, "failed", "path_changed", parent_fd=parent_fd, temp_name=temp_name)
    except _PullPersistenceError:
        _finish_or_fail(job_id, target, "failed", "job_persistence_failed", parent_fd=parent_fd, temp_name=temp_name)
    except Exception:
        _finish_or_fail(job_id, target, "failed", "download_failed", parent_fd=parent_fd, temp_name=temp_name)
    finally:
        if temp_created and target is not None:
            _unlink_at(target, parent_fd, temp_name)
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        with SCRIPT_PULL_LOCK:
            SCRIPT_PULL_CANCEL.pop(job_id, None)


def _finish_or_fail(job_id, target, status, error_code, *, parent_fd=None, temp_name=None):
    if target is not None and temp_name is not None:
        _unlink_at(target, parent_fd, temp_name)
    with SCRIPT_PULL_LOCK:
        job = SCRIPT_PULL_JOBS.get(job_id)
        published = bool(target and job and job.get("sha256") and _target_matches(job))
        if job is None:
            return
        job.update(
            status="complete" if published else status,
            error_code="" if published else error_code,
            updated_at=now_str(),
        )
        try:
            _save_locked()
        except Exception:
            if not published:
                job.update(status="unknown", error_code="job_persistence_failed")


def _json_body():
    if not request.is_json:
        return None, _error("请求必须使用 JSON", 415, "json_required")
    if request.content_length is not None and request.content_length > 64 * 1024:
        return None, _error("请求内容超过限制", 413, "request_too_large")
    try:
        value = request.get_json(silent=False)
    except Exception:
        value = None
    if not isinstance(value, dict):
        return None, _error("JSON 格式或请求字段无效", 400, "invalid_body")
    return value, None


def _safe_url(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise ValueError("invalid_url")
    value = raw.strip()
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("invalid_url")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid_url") from exc
    return value, parsed.hostname.lower()


def _request_fingerprint(url, path, proxy_id):
    data = json.dumps([url, path, proxy_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@bp.route("/api/scripts/pull", methods=["POST"])
@directory_job_operation
def api_script_pull_submit():
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("脚本拉取记录无法读取，未启动新作业", 503, "history_unavailable")
    body, error_response = _json_body()
    if error_response is not None:
        return error_response
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not SCRIPT_PULL_ID_RE.fullmatch(request_id):
        return _error("request_id 必须为 32 位小写十六进制字符串", 400, "invalid_request_id")
    try:
        url, source_host = _safe_url(body.get("url"))
    except (TypeError, ValueError):
        return _error("仅支持有效的 HTTP/HTTPS URL，且 URL 不可包含账号信息或片段", 400, "invalid_url")
    raw_path = body.get("path")
    if not isinstance(raw_path, str):
        return _error("目标路径必须是 scripts 内的安全相对文件路径", 400, "invalid_path")
    try:
        target = script_file_target(raw_path)
        relative = target.relative_to(SCRIPT_DIR.resolve()).as_posix()
    except (OSError, ValueError):
        return _error("目标路径必须是 scripts 内的安全相对文件路径", 400, "invalid_path")
    if not target.parent.is_dir() or target.parent.is_symlink():
        return _error("目标目录不存在或不安全", 404, "invalid_parent")
    proxy_id = body.get("proxy_id", "")
    if not isinstance(proxy_id, str) or len(proxy_id) > 128:
        return _error("proxy_id 无效", 400, "invalid_proxy")

    fingerprint = _request_fingerprint(url, relative, proxy_id)
    with SCRIPT_PULL_LOCK:
        previous = next(
            (item for item in SCRIPT_PULL_JOBS.values() if item.get("request_id") == request_id),
            None,
        )
        if previous is not None:
            if previous.get("fingerprint") != fingerprint:
                return _error("request_id 已用于不同请求", 409, "request_id_conflict")
            return _response({"ok": True, "job": _payload(previous, replayed=True)}, 200)

    try:
        validate_proxy_id(proxy_id)
    except ValueError:
        return _error("所选代理无效或已停用", 400, "invalid_proxy")

    with SCRIPT_PULL_LOCK:
        previous = next(
            (item for item in SCRIPT_PULL_JOBS.values() if item.get("request_id") == request_id),
            None,
        )
        if previous is not None:
            if previous.get("fingerprint") != fingerprint:
                return _error("request_id 已用于不同请求", 409, "request_id_conflict")
            return _response({"ok": True, "job": _payload(previous, replayed=True)}, 200)
        if os.path.lexists(target):
            return _error("目标已存在，未覆盖现有文件", 409, "target_exists")
        if sum(item.get("status") in SCRIPT_PULL_ACTIVE for item in SCRIPT_PULL_JOBS.values()) >= SCRIPT_PULL_ACTIVE_LIMIT:
            return _error("脚本拉取作业过多，请等待当前作业结束", 429, "too_many_active_jobs")

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "request_id": request_id,
            "fingerprint": fingerprint,
            "kind": "file",
            "path": relative,
            "source_host": source_host,
            "status": "pending",
            "downloaded_bytes": 0,
            "total_bytes": None,
            "sha256": "",
            "error_code": "",
            "created_at": now_str(),
            "updated_at": now_str(),
        }
        SCRIPT_PULL_JOBS[job_id] = job
        owner = None
        cancel = DirectoryJobCancel(SCRIPT_PULL_LOCK)
        SCRIPT_PULL_CANCEL[job_id] = cancel
        try:
            owner = SCRIPT_PULL_LOCK.claim(job_id)
            _save_locked()
        except Exception:
            SCRIPT_PULL_JOBS.pop(job_id, None)
            SCRIPT_PULL_CANCEL.pop(job_id, None)
            if owner is not None:
                owner.close()
            return _error("作业回执无法保存，未开始下载", 503, "job_persistence_failed")
        try:
            start_directory_job(
                _run_pull,
                args=(job_id, url, proxy_id),
                name=f"fls-script-pull-{job_id[:8]}",
                owner=owner,
            )
        except Exception:
            owner.close()
            SCRIPT_PULL_CANCEL.pop(job_id, None)
            job.update(status="failed", error_code="worker_start_failed", updated_at=now_str())
            try:
                _save_locked()
            except Exception:
                job.update(status="unknown", error_code="job_persistence_failed")
            return _response({"ok": False, "job": _payload(job)}, 503)
    return _response({"ok": True, "job": _payload(job)}, 202)


@bp.route("/api/scripts/pull/<job_id>")
@directory_job_operation
def api_script_pull_status(job_id):
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("脚本拉取记录无法读取", 503, "history_unavailable")
    if not SCRIPT_PULL_ID_RE.fullmatch(job_id):
        return _error("job_id 无效", 400, "invalid_job_id")
    with SCRIPT_PULL_LOCK:
        job = SCRIPT_PULL_JOBS.get(job_id)
        return _response({"ok": True, "job": _payload(job)}) if job else _error("作业不存在", 404, "not_found")


@bp.route("/api/scripts/pull-history")
@directory_job_operation
def api_script_pull_history():
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("脚本拉取记录无法读取", 503, "history_unavailable")
    try:
        limit = int(request.args.get("limit", "20"))
    except (TypeError, ValueError):
        return _error("limit 必须为整数", 400, "invalid_limit")
    if limit < 1 or limit > 100:
        return _error("limit 必须在 1 到 100 之间", 400, "invalid_limit")
    with SCRIPT_PULL_LOCK:
        jobs = sorted(
            SCRIPT_PULL_JOBS.values(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )[:limit]
        return _response({"ok": True, "items": [_payload(job) for job in jobs]})


@bp.route("/api/scripts/pull-options")
@directory_job_operation
def api_script_pull_options():
    try:
        proxies = load_proxies()
    except Exception:
        return _error("代理选项暂不可读", 503, "proxy_options_unavailable")
    return _response({
        "ok": True,
        "proxies": [
            {
                "id": str(proxy.get("id") or ""),
                "name": str(proxy.get("name") or proxy.get("type") or "代理"),
                "type": str(proxy.get("type") or ""),
            }
            for proxy in proxies
            if isinstance(proxy, dict) and proxy.get("enabled", True) and proxy.get("id")
        ][:100],
    })


@bp.route("/api/scripts/pull/<job_id>/cancel", methods=["POST"])
@directory_job_operation
def api_script_pull_cancel(job_id):
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("脚本拉取记录无法读取", 503, "history_unavailable")
    if not SCRIPT_PULL_ID_RE.fullmatch(job_id):
        return _error("job_id 无效", 400, "invalid_job_id")
    with SCRIPT_PULL_LOCK:
        job = SCRIPT_PULL_JOBS.get(job_id)
        if job is None:
            return _error("作业不存在", 404, "not_found")
        if job.get("status") not in SCRIPT_PULL_ACTIVE:
            return _error("作业已结束，不能取消", 409, "not_cancellable")
        cancel = SCRIPT_PULL_CANCEL.get(job_id)
        previous_status = job.get("status")
        previous_updated = job.get("updated_at")
        job.update(status="cancelling", updated_at=now_str())
        try:
            _save_locked()
        except Exception:
            job.update(status=previous_status, updated_at=previous_updated)
            return _error("取消状态无法保存，作业未取消", 503, "job_persistence_failed")
        if cancel is not None:
            cancel.set()
        return _response({"ok": True, "job": _payload(job)}, 202)
