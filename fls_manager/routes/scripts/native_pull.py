import errno
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import jsonify, request

from . import bp
from .helpers import requests
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
SCRIPT_PULL_LOCK = threading.RLock()
SCRIPT_PULL_JOBS = {}
SCRIPT_PULL_CANCEL = {}
SCRIPT_PULL_INITIALIZED = False


class _PullCancelled(Exception):
    pass


class _PullTooLarge(Exception):
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


def _target_matches(job):
    try:
        target = script_file_target(job["path"])
        if target.is_symlink() or not target.is_file():
            return False
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == job.get("sha256")
    except (OSError, KeyError, ValueError):
        return False


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
        if SCRIPT_PULL_INITIALIZED:
            return
        try:
            raw = json.loads(SCRIPT_PULL_JOBS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = []
        except (OSError, ValueError) as exc:
            raise RuntimeError("脚本拉取记录无法读取，未覆盖原记录") from exc
        if not isinstance(raw, list):
            raise RuntimeError("脚本拉取记录不是列表，未覆盖原记录")
        rows = raw
        for row in rows[:SCRIPT_PULL_HISTORY_LIMIT]:
            if not isinstance(row, dict):
                continue
            job_id = str(row.get("id") or "")
            request_id = str(row.get("request_id") or "")
            if not SCRIPT_PULL_ID_RE.fullmatch(job_id) or not SCRIPT_PULL_ID_RE.fullmatch(request_id):
                continue
            status = str(row.get("status") or "unknown")
            if status in SCRIPT_PULL_ACTIVE:
                if status == "publishing" and _target_matches(row):
                    status = "complete"
                else:
                    status = "unknown"
                    row = dict(row, error_code="worker_interrupted")
                    try:
                        _clean_temp(script_file_target(row.get("path", "")), job_id)
                    except ValueError:
                        pass
            if status not in SCRIPT_PULL_TERMINAL:
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
                "error_code": str(row.get("error_code") or "")[:80],
                "created_at": str(row.get("created_at") or ""),
                "updated_at": str(row.get("updated_at") or ""),
            }
            SCRIPT_PULL_JOBS[job_id] = normalized
        try:
            _save_locked()
        except Exception as exc:
            SCRIPT_PULL_JOBS.clear()
            raise RuntimeError("脚本拉取记录无法保存，未覆盖原记录") from exc
        SCRIPT_PULL_INITIALIZED = True


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
        job.update(updates)
        job["updated_at"] = now_str()
        if persist:
            _save_locked()
        return dict(job)


def _publish_no_replace(temp, target):
    link = getattr(os, "link", None)
    if callable(link):
        try:
            link(temp, target)
            temp.unlink()
            return
        except (OSError, NotImplementedError) as exc:
            unsupported = {errno.ENOSYS, errno.EPERM, errno.EACCES}
            unsupported.update(
                value for value in (getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None))
                if value is not None
            )
            if not isinstance(exc, NotImplementedError) and getattr(exc, "errno", None) not in unsupported:
                raise

    mode = stat.S_IMODE(temp.stat().st_mode)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(str(target), flags, mode)
    reservation = os.fstat(fd)
    os.close(fd)
    try:
        os.replace(temp, target)
    except Exception:
        try:
            current = os.stat(target, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (reservation.st_dev, reservation.st_ino):
                os.unlink(target)
        except OSError:
            pass
        raise


def _run_pull(job_id, url, proxy_id):
    with SCRIPT_PULL_LOCK:
        cancel = SCRIPT_PULL_CANCEL.get(job_id)
        job = SCRIPT_PULL_JOBS.get(job_id)
    if cancel is None or job is None:
        return

    response = None
    target = None
    try:
        target = script_file_target(job["path"])
        temp = target.with_name(f".fls-pull-{job_id}.tmp")
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

        fd = os.open(
            str(temp),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
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
        with temp.open("rb") as staged:
            for chunk in iter(lambda: staged.read(128 * 1024), b""):
                digest.update(chunk)
        _update(
            job_id,
            status="publishing",
            total_bytes=total,
            downloaded_bytes=downloaded,
            sha256=digest.hexdigest(),
        )
        with SCRIPT_PULL_LOCK:
            if cancel.is_set():
                raise _PullCancelled
            if script_file_target(job["path"]) != target or target.parent.is_symlink():
                raise ValueError("invalid_path")
            _publish_no_replace(temp, target)
            current = SCRIPT_PULL_JOBS[job_id]
            current.update(status="complete", error_code="", total_bytes=total, downloaded_bytes=downloaded)
            current["updated_at"] = now_str()
            _save_locked()
    except _PullCancelled:
        _finish_or_fail(job_id, target, "cancelled", "")
    except _PullTooLarge:
        _finish_or_fail(job_id, target, "failed", "too_large")
    except FileExistsError:
        _finish_or_fail(job_id, target, "failed", "target_exists")
    except Exception:
        _finish_or_fail(job_id, target, "failed", "download_failed")
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        with SCRIPT_PULL_LOCK:
            SCRIPT_PULL_CANCEL.pop(job_id, None)


def _finish_or_fail(job_id, target, status, error_code):
    with SCRIPT_PULL_LOCK:
        job = SCRIPT_PULL_JOBS.get(job_id)
        published = bool(target and job and job.get("sha256") and _target_matches(job))
    if published:
        _update(job_id, status="complete", error_code="")
        if target is not None:
            _clean_temp(target, job_id)
        return
    if target is not None:
        _clean_temp(target, job_id)
    _update(job_id, status=status, error_code=error_code)


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
        cancel = threading.Event()
        SCRIPT_PULL_CANCEL[job_id] = cancel
        try:
            _save_locked()
        except Exception:
            SCRIPT_PULL_JOBS.pop(job_id, None)
            SCRIPT_PULL_CANCEL.pop(job_id, None)
            return _error("作业回执无法保存，未开始下载", 503, "job_persistence_failed")
        thread = threading.Thread(
            target=_run_pull,
            args=(job_id, url, proxy_id),
            daemon=True,
            name=f"fls-script-pull-{job_id[:8]}",
        )
        try:
            thread.start()
        except Exception:
            SCRIPT_PULL_CANCEL.pop(job_id, None)
            job.update(status="failed", error_code="worker_start_failed", updated_at=now_str())
            _save_locked()
            return _response({"ok": False, "job": _payload(job)}, 503)
    return _response({"ok": True, "job": _payload(job)}, 202)


@bp.route("/api/scripts/pull/<job_id>")
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
        if cancel is None:
            return _error("作业状态无法确认，请检查目标文件", 409, "state_unknown")
        cancel.set()
        job.update(status="cancelling", updated_at=now_str())
        _save_locked()
        return _response({"ok": True, "job": _payload(job)}, 202)
