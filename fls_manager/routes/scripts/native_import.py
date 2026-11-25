import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from flask import jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from . import bp
from .native_pull import SCRIPT_PULL_ID_RE, _renameat2_no_replace
from ...paths import DATA_DIR, SCRIPT_DIR, SCRIPT_IMPORT_JOBS_FILE
from ...script_files import script_file_target
from ...storage import write_json
from ...utils import now_str

SCRIPT_IMPORT_MAX_BYTES = 64 * 1024 * 1024
SCRIPT_IMPORT_MAX_REQUEST_BYTES = SCRIPT_IMPORT_MAX_BYTES + 1024 * 1024
SCRIPT_IMPORT_MAX_ENTRIES = 10_000
SCRIPT_IMPORT_MAX_EXPANDED_BYTES = 256 * 1024 * 1024
SCRIPT_IMPORT_HISTORY_LIMIT = 200
SCRIPT_IMPORT_ACTIVE_LIMIT = 2
SCRIPT_IMPORT_ACTIVE = ("pending", "processing", "publishing", "cancelling")
SCRIPT_IMPORT_TERMINAL = ("complete", "failed", "cancelled", "unknown")
_WINDOWS_RESERVED_NAME_RE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
SCRIPT_IMPORT_LOCK = threading.RLock()
SCRIPT_IMPORT_JOBS = {}
SCRIPT_IMPORT_CANCEL = {}
SCRIPT_IMPORT_INITIALIZED = False


class _ImportCancelled(Exception):
    pass


class _ImportTooLarge(Exception):
    pass


def _response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message, status, code):
    return _response({"ok": False, "code": code, "msg": message}, status)


def _upload_path(job_id):
    return DATA_DIR / f".script-import-{job_id}.upload"


def _stage_name(job_id):
    return f".fls-import-{job_id}.stage"


def _kind_for_filename(filename):
    lower = filename.lower()
    if lower.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if lower.endswith(".tar"):
        return "tar"
    if lower.endswith(".zip"):
        return "zip"
    return "file"


def _safe_filename(raw):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("invalid_filename")
    normalized = raw.replace("\\", "/").split("/")[-1]
    if normalized in ("", ".", "..") or len(normalized.encode("utf-8")) > 255:
        raise ValueError("invalid_filename")
    return normalized


def _safe_archive_name(raw, *, directory=False):
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("压缩包包含非法路径")
    if raw.startswith("/") or PureWindowsPath(raw).drive:
        raise ValueError("压缩包包含非法路径")
    value = raw[:-1] if directory and raw.endswith("/") else raw
    parts = value.split("/")
    if not value or any(part in ("", ".", "..") for part in parts):
        raise ValueError("压缩包包含非法路径")
    for part in parts:
        if (
            any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or part.endswith((".", " "))
            or _WINDOWS_RESERVED_NAME_RE.fullmatch(part)
        ):
            raise ValueError("压缩包包含当前平台不安全的文件名")
    path = PurePosixPath(*parts)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError("压缩包包含非法路径")
    return path


def _check_cancel(cancel):
    if cancel.is_set():
        raise _ImportCancelled


def _mkdir_parents(path):
    path = Path(path)
    missing = []
    parent = path.parent
    while not os.path.lexists(parent):
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir()
    return missing


def _remove_stage(target, parent_fd, job_id):
    name = _stage_name(job_id)
    try:
        if parent_fd is not None:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                try:
                    _remove_tree_contents(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=parent_fd)
            else:
                os.unlink(name, dir_fd=parent_fd)
            return
        stage = Path(target).parent / name
        metadata = stage.lstat()
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            shutil.rmtree(stage)
        else:
            stage.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _remove_tree_contents(directory_fd):
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                _remove_tree_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _open_parent(relative):
    target = script_file_target(relative)
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("目标父目录不存在或不安全")
    if os.open not in getattr(os, "supports_dir_fd", set()) or not hasattr(os, "O_DIRECTORY"):
        metadata = parent.stat(follow_symlinks=False)
        return target, None, (metadata.st_dev, metadata.st_ino)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    root = SCRIPT_DIR.resolve()
    fd = os.open(str(root), flags)
    try:
        for part in parent.relative_to(root).parts:
            next_fd = os.open(part, flags | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        metadata = os.fstat(fd)
        return target, fd, (metadata.st_dev, metadata.st_ino)
    except Exception:
        os.close(fd)
        raise


def _parent_matches(target, parent_fd, identity):
    try:
        if parent_fd is not None:
            metadata = os.fstat(parent_fd)
        else:
            metadata = target.parent.stat(follow_symlinks=False)
        return (metadata.st_dev, metadata.st_ino) == identity
    except OSError:
        return False


def _publish_file_no_replace(staged, target, parent_fd):
    if parent_fd is not None and _renameat2_no_replace(staged.name, target.name, parent_fd):
        return
    link = getattr(os, "link", None)
    if callable(link):
        try:
            if parent_fd is None:
                link(staged, target, follow_symlinks=False)
            else:
                link(
                    staged.name,
                    target.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            return
        except (OSError, NotImplementedError) as exc:
            unsupported = {errno.ENOSYS, errno.EPERM, errno.EACCES}
            unsupported.update(
                code for code in (getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None))
                if code is not None
            )
            if not isinstance(exc, NotImplementedError) and exc.errno not in unsupported:
                raise

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    if parent_fd is None:
        fd = os.open(str(target), flags, 0o600)
    else:
        fd = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
    reservation = os.fstat(fd)
    os.close(fd)
    try:
        if parent_fd is None:
            os.replace(staged, target)
        else:
            os.replace(staged.name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except Exception:
        try:
            current = (
                os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                if parent_fd is not None else os.stat(target, follow_symlinks=False)
            )
            if (current.st_dev, current.st_ino) == (reservation.st_dev, reservation.st_ino):
                if parent_fd is None:
                    os.unlink(target)
                else:
                    os.unlink(target.name, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _publish_directory_no_replace(staged, staged_name, target, parent_fd):
    if parent_fd is not None:
        if _renameat2_no_replace(staged_name, target.name, parent_fd):
            return
        raise OSError(errno.ENOTSUP, "当前文件系统不支持安全目录发布")
    if os.name == "nt":
        os.rename(staged, target)
        return
    raise OSError(errno.ENOTSUP, "当前文件系统不支持安全目录发布")


def _extract_zip(source, destination, cancel, on_progress):
    with zipfile.ZipFile(source, "r") as archive:
        members = archive.infolist()
        if len(members) > SCRIPT_IMPORT_MAX_ENTRIES:
            raise ValueError("压缩包文件数量超过限制")
        entries = []
        seen = set()
        expanded = 0
        for member in members:
            is_dir = member.is_dir()
            path = _safe_archive_name(member.filename, directory=is_dir)
            key = path.as_posix()
            if key in seen:
                raise ValueError("压缩包包含重复路径")
            seen.add(key)
            mode = member.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("压缩包包含链接或特殊文件")
            if member.file_size < 0:
                raise ValueError("压缩包文件大小无效")
            if not is_dir:
                expanded += member.file_size
            if expanded > SCRIPT_IMPORT_MAX_EXPANDED_BYTES:
                raise _ImportTooLarge
            entries.append((member, path, is_dir))

        processed = 0
        for member, path, is_dir in entries:
            _check_cancel(cancel)
            output = destination.joinpath(*path.parts)
            if is_dir:
                output.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as outgoing:
                with archive.open(member, "r") as incoming:
                    while True:
                        _check_cancel(cancel)
                        chunk = incoming.read(128 * 1024)
                        if not chunk:
                            break
                        processed += len(chunk)
                        if processed > SCRIPT_IMPORT_MAX_EXPANDED_BYTES:
                            raise _ImportTooLarge
                        outgoing.write(chunk)
                        on_progress(processed)


def _extract_tar(source, destination, compressed, cancel, on_progress):
    mode = "r:gz" if compressed else "r:"
    with tarfile.open(source, mode=mode) as archive:
        members = archive.getmembers()
        if len(members) > SCRIPT_IMPORT_MAX_ENTRIES:
            raise ValueError("压缩包文件数量超过限制")
        entries = []
        seen = set()
        expanded = 0
        for member in members:
            is_dir = member.isdir()
            path = _safe_archive_name(member.name, directory=is_dir)
            key = path.as_posix()
            if key in seen:
                raise ValueError("压缩包包含重复路径")
            seen.add(key)
            if not (is_dir or member.isreg()) or member.issparse():
                raise ValueError("压缩包包含链接或特殊文件")
            if member.size < 0:
                raise ValueError("压缩包文件大小无效")
            if not is_dir:
                expanded += member.size
            if expanded > SCRIPT_IMPORT_MAX_EXPANDED_BYTES:
                raise _ImportTooLarge
            entries.append((member, path, is_dir))

        processed = 0
        for member, path, is_dir in entries:
            _check_cancel(cancel)
            output = destination.joinpath(*path.parts)
            if is_dir:
                output.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            incoming = archive.extractfile(member)
            if incoming is None:
                raise ValueError("压缩包文件内容无法读取")
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as outgoing:
                with incoming:
                    while True:
                        _check_cancel(cancel)
                        chunk = incoming.read(128 * 1024)
                        if not chunk:
                            break
                        processed += len(chunk)
                        if processed > SCRIPT_IMPORT_MAX_EXPANDED_BYTES:
                            raise _ImportTooLarge
                        outgoing.write(chunk)
                        on_progress(processed)


def _update(job_id, *, persist=True, **updates):
    with SCRIPT_IMPORT_LOCK:
        job = SCRIPT_IMPORT_JOBS.get(job_id)
        if job is None:
            return None
        job.update(updates)
        job["updated_at"] = now_str()
        if persist:
            _save_locked()
        return dict(job)


def _save_locked():
    ordered = sorted(
        SCRIPT_IMPORT_JOBS.values(),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    keep_ids = {item["id"] for item in ordered[:SCRIPT_IMPORT_HISTORY_LIMIT]}
    keep_ids.update(item["id"] for item in ordered if item.get("status") in SCRIPT_IMPORT_ACTIVE)
    write_json(SCRIPT_IMPORT_JOBS_FILE, [item for item in ordered if item["id"] in keep_ids])
    for job_id in tuple(SCRIPT_IMPORT_JOBS):
        if job_id not in keep_ids:
            SCRIPT_IMPORT_JOBS.pop(job_id, None)


def _cleanup_job_artifacts(job):
    job_id = job.get("id", "")
    try:
        _upload_path(job_id).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        target = script_file_target(job.get("path", ""))
        parent_fd = None
        try:
            target, parent_fd, _identity = _open_parent(job["path"])
            _remove_stage(target, parent_fd, job_id)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
    except (OSError, ValueError, KeyError):
        pass


def _ensure_loaded():
    global SCRIPT_IMPORT_INITIALIZED
    with SCRIPT_IMPORT_LOCK:
        if SCRIPT_IMPORT_INITIALIZED:
            return
        try:
            raw = json.loads(SCRIPT_IMPORT_JOBS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = []
        except (OSError, ValueError) as exc:
            raise RuntimeError("脚本导入记录无法读取，未覆盖原记录") from exc
        if not isinstance(raw, list):
            raise RuntimeError("脚本导入记录格式无效，未覆盖原记录")
        SCRIPT_IMPORT_JOBS.clear()
        for row in raw[:SCRIPT_IMPORT_HISTORY_LIMIT]:
            if not isinstance(row, dict):
                continue
            job_id = str(row.get("id") or "")
            request_id = str(row.get("request_id") or "")
            if not SCRIPT_PULL_ID_RE.fullmatch(job_id) or not SCRIPT_PULL_ID_RE.fullmatch(request_id):
                continue
            status = str(row.get("status") or "unknown")
            if status in SCRIPT_IMPORT_ACTIVE:
                status = "unknown"
                row = dict(row, error_code="worker_interrupted")
                _cleanup_job_artifacts(row)
            if status not in SCRIPT_IMPORT_TERMINAL:
                status = "unknown"
            try:
                target = script_file_target(row.get("path", ""))
                relative = target.relative_to(SCRIPT_DIR.resolve()).as_posix()
            except (OSError, ValueError):
                continue
            SCRIPT_IMPORT_JOBS[job_id] = {
                "id": job_id,
                "request_id": request_id,
                "fingerprint": str(row.get("fingerprint") or ""),
                "kind": str(row.get("kind") or "file"),
                "path": relative,
                "source_host": "local-upload",
                "status": status,
                "phase": str(row.get("phase") or status)[:32],
                "uploaded_bytes": _nonnegative_int(row.get("uploaded_bytes")),
                "processed_bytes": _nonnegative_int(row.get("processed_bytes")),
                "total_bytes": _optional_nonnegative_int(row.get("total_bytes")),
                "total_files": _optional_nonnegative_int(row.get("total_files")),
                "error_code": str(row.get("error_code") or "")[:80],
                "created_at": str(row.get("created_at") or ""),
                "updated_at": str(row.get("updated_at") or ""),
            }
        try:
            _save_locked()
        except Exception as exc:
            SCRIPT_IMPORT_JOBS.clear()
            raise RuntimeError("脚本导入记录无法保存，未覆盖原记录") from exc
        for artifact in DATA_DIR.glob(".script-import-*.upload"):
            if SCRIPT_PULL_ID_RE.fullmatch(artifact.name[len(".script-import-"):-len(".upload")]):
                try:
                    artifact.unlink()
                except OSError:
                    pass
        SCRIPT_IMPORT_INITIALIZED = True


def _nonnegative_int(value):
    return value if type(value) is int and value >= 0 else 0


def _optional_nonnegative_int(value):
    return value if type(value) is int and value >= 0 else None


def _payload(job, *, replayed=False):
    return {
        "id": job.get("id", ""),
        "request_id": job.get("request_id", ""),
        "kind": job.get("kind", "file"),
        "path": job.get("path", ""),
        "source_host": "local-upload",
        "status": job.get("status", "unknown"),
        "progress": {
            "phase": job.get("phase", "unknown"),
            "uploaded_bytes": _nonnegative_int(job.get("uploaded_bytes")),
            "processed_bytes": _nonnegative_int(job.get("processed_bytes")),
            "total_bytes": job.get("total_bytes"),
            "total_files": job.get("total_files"),
        },
        "error_code": job.get("error_code", ""),
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        "replayed": replayed,
    }


def _copy_upload(source, target, cancel, on_progress):
    total = 0
    digest = hashlib.sha256()
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as destination:
        while True:
            _check_cancel(cancel)
            chunk = source.read(128 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > SCRIPT_IMPORT_MAX_BYTES:
                raise _ImportTooLarge
            digest.update(chunk)
            destination.write(chunk)
            on_progress(total)
        destination.flush()
        os.fsync(destination.fileno())
    return total, digest.hexdigest()


def _run_import(job_id):
    with SCRIPT_IMPORT_LOCK:
        job = SCRIPT_IMPORT_JOBS.get(job_id)
        cancel = SCRIPT_IMPORT_CANCEL.get(job_id)
        if job is None or cancel is None:
            return
        snapshot = dict(job)
    target = None
    parent_fd = None
    staged = None
    try:
        target = script_file_target(snapshot["path"])
        if cancel.is_set():
            raise _ImportCancelled
        if snapshot["kind"] == "file":
            target, parent_fd, parent_identity = _open_parent(snapshot["path"])
            stage_name = _stage_name(job_id)
            if parent_fd is None:
                staged = target.parent / stage_name
            else:
                staged = Path(f"/proc/self/fd/{parent_fd}") / stage_name
            _update(job_id, status="processing", phase="copying")
            source_path = _upload_path(job_id)
            with source_path.open("rb") as source:
                total, _digest = _copy_upload(
                    source,
                    staged,
                    cancel,
                    lambda count: _update(job_id, persist=False, processed_bytes=count),
                )
            _check_cancel(cancel)
            with SCRIPT_IMPORT_LOCK:
                current = SCRIPT_IMPORT_JOBS.get(job_id)
                if current is None or current.get("status") == "cancelling" or cancel.is_set():
                    raise _ImportCancelled
                current.update(status="publishing", phase="publishing", processed_bytes=total, updated_at=now_str())
                _save_locked()
            if not _parent_matches(target, parent_fd, parent_identity):
                raise ValueError("目标目录已变化")
            _publish_file_no_replace(staged, target, parent_fd)
            _update(job_id, status="complete", phase="complete", processed_bytes=total, error_code="")
        else:
            target, parent_fd, parent_identity = _open_parent(snapshot["path"])
            if parent_fd is None:
                stage_root = target.parent / _stage_name(job_id)
            else:
                stage_root = Path(f"/proc/self/fd/{parent_fd}") / _stage_name(job_id)
            stage_root.mkdir(mode=0o700)
            staged = stage_root
            payload_dir = stage_root / "payload"
            payload_dir.mkdir(mode=0o700)
            _update(job_id, status="processing", phase="extracting")
            source_path = _upload_path(job_id)
            on_progress = lambda count: _update(
                job_id, persist=False, processed_bytes=count,
            )
            if snapshot["kind"] == "zip":
                _extract_zip(source_path, payload_dir, cancel, on_progress)
            else:
                _extract_tar(source_path, payload_dir, snapshot["kind"] == "tar.gz", cancel, on_progress)
            total_files = sum(1 for item in payload_dir.rglob("*") if item.is_file())
            total_bytes = sum(item.stat().st_size for item in payload_dir.rglob("*") if item.is_file())
            _check_cancel(cancel)
            with SCRIPT_IMPORT_LOCK:
                current = SCRIPT_IMPORT_JOBS.get(job_id)
                if current is None or current.get("status") == "cancelling" or cancel.is_set():
                    raise _ImportCancelled
                current.update(
                    status="publishing", phase="publishing", processed_bytes=total_bytes,
                    total_files=total_files, updated_at=now_str(),
                )
                _save_locked()
            if not _parent_matches(target, parent_fd, parent_identity):
                raise ValueError("目标目录已变化")
            _publish_directory_no_replace(
                payload_dir,
                f"{_stage_name(job_id)}/payload",
                target,
                parent_fd,
            )
            _update(
                job_id, status="complete", phase="complete", processed_bytes=total_bytes,
                total_files=total_files, error_code="",
            )
    except _ImportCancelled:
        _update(job_id, status="cancelled", phase="cancelled", error_code="cancelled")
    except _ImportTooLarge:
        _update(job_id, status="failed", phase="failed", error_code="too_large")
    except FileExistsError:
        _update(job_id, status="failed", phase="failed", error_code="target_exists")
    except Exception:
        _update(job_id, status="failed", phase="failed", error_code="import_failed")
    finally:
        try:
            _upload_path(job_id).unlink(missing_ok=True)
        except OSError:
            pass
        if target is not None:
            _remove_stage(target, parent_fd, job_id)
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        with SCRIPT_IMPORT_LOCK:
            SCRIPT_IMPORT_CANCEL.pop(job_id, None)


@bp.route("/api/scripts/import", methods=["POST"])
def api_script_import_submit():
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("脚本导入记录无法读取，未启动新作业", 503, "history_unavailable")
    with SCRIPT_IMPORT_LOCK:
        if sum(item.get("status") in SCRIPT_IMPORT_ACTIVE for item in SCRIPT_IMPORT_JOBS.values()) >= SCRIPT_IMPORT_ACTIVE_LIMIT:
            return _error("脚本导入作业过多，请等待当前作业结束", 429, "too_many_active_jobs")
    request.max_content_length = SCRIPT_IMPORT_MAX_REQUEST_BYTES
    try:
        files = request.files.getlist("file")
        if len(files) != 1 or len(request.files) != 1:
            return _error("每次只能上传一个文件", 400, "invalid_file_count")
        if set(request.form.keys()) != {"request_id", "path"}:
            return _error("导入请求字段无效", 400, "invalid_fields")
        request_ids = request.form.getlist("request_id")
        paths = request.form.getlist("path")
        if len(request_ids) != 1 or len(paths) != 1:
            return _error("导入请求字段不能重复", 400, "invalid_fields")
        upload = files[0]
        request_id = request_ids[0]
        raw_path = paths[0]
    except RequestEntityTooLarge:
        return _error("上传文件超过 64 MiB 限制", 413, "too_large")
    if upload is None or not upload.filename:
        return _error("请选择要导入的文件", 400, "missing_file")
    if not SCRIPT_PULL_ID_RE.fullmatch(request_id):
        return _error("request_id 必须为 32 位小写十六进制字符串", 400, "invalid_request_id")
    try:
        filename = _safe_filename(upload.filename)
        kind = _kind_for_filename(filename)
        if not isinstance(raw_path, str) or "\x00" in raw_path:
            raise ValueError("invalid_path")
        if not raw_path:
            raise ValueError("invalid_path")
        target = script_file_target(raw_path)
        relative = target.relative_to(SCRIPT_DIR.resolve()).as_posix()
        if not target.parent.is_dir() or target.parent.is_symlink():
            raise ValueError("invalid_parent")
    except (OSError, ValueError):
        return _error("目标路径无效或不安全", 400, "invalid_path")

    upload_id = uuid.uuid4().hex
    staged_upload = _upload_path(upload_id)
    try:
        upload_fd = os.open(
            staged_upload,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(upload_fd, "wb") as output:
            total = 0
            digest = hashlib.sha256()
            while True:
                chunk = upload.stream.read(128 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > SCRIPT_IMPORT_MAX_BYTES:
                    raise _ImportTooLarge
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except _ImportTooLarge:
        staged_upload.unlink(missing_ok=True)
        return _error("上传文件超过 64 MiB 限制", 413, "too_large")
    except RequestEntityTooLarge:
        staged_upload.unlink(missing_ok=True)
        return _error("上传文件超过 64 MiB 限制", 413, "too_large")
    except Exception:
        staged_upload.unlink(missing_ok=True)
        return _error("无法暂存上传文件", 500, "upload_staging_failed")

    fingerprint = hashlib.sha256(
        json.dumps([relative, kind, filename, digest.hexdigest()], ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    with SCRIPT_IMPORT_LOCK:
        previous = next(
            (item for item in SCRIPT_IMPORT_JOBS.values() if item.get("request_id") == request_id),
            None,
        )
        if previous is not None:
            staged_upload.unlink(missing_ok=True)
            if previous.get("fingerprint") != fingerprint:
                return _error("request_id 已用于不同请求", 409, "request_id_conflict")
            return _response({"ok": True, "job": _payload(previous, replayed=True)}, 200)
        if os.path.lexists(target):
            staged_upload.unlink(missing_ok=True)
            message = "目标已存在，未覆盖现有文件" if kind == "file" else "压缩包目标目录已存在，请选择新目录"
            return _error(message, 409, "target_exists")
        if sum(item.get("status") in SCRIPT_IMPORT_ACTIVE for item in SCRIPT_IMPORT_JOBS.values()) >= SCRIPT_IMPORT_ACTIVE_LIMIT:
            staged_upload.unlink(missing_ok=True)
            return _error("脚本导入作业过多，请等待当前作业结束", 429, "too_many_active_jobs")
        job_id = upload_id
        final_upload = _upload_path(job_id)
        staged_upload.rename(final_upload)
        job = {
            "id": job_id,
            "request_id": request_id,
            "fingerprint": fingerprint,
            "kind": kind,
            "path": relative,
            "status": "pending",
            "phase": "queued",
            "uploaded_bytes": total,
            "processed_bytes": 0,
            "total_bytes": total,
            "total_files": None,
            "error_code": "",
            "created_at": now_str(),
            "updated_at": now_str(),
        }
        SCRIPT_IMPORT_JOBS[job_id] = job
        cancel = threading.Event()
        SCRIPT_IMPORT_CANCEL[job_id] = cancel
        try:
            _save_locked()
        except Exception:
            SCRIPT_IMPORT_JOBS.pop(job_id, None)
            SCRIPT_IMPORT_CANCEL.pop(job_id, None)
            final_upload.unlink(missing_ok=True)
            return _error("作业回执无法保存，未开始导入", 503, "job_persistence_failed")
        worker = threading.Thread(
            target=_run_import,
            args=(job_id,),
            daemon=True,
            name=f"fls-script-import-{job_id[:8]}",
        )
        try:
            worker.start()
        except Exception:
            SCRIPT_IMPORT_CANCEL.pop(job_id, None)
            job.update(status="failed", phase="failed", error_code="worker_start_failed", updated_at=now_str())
            _save_locked()
            final_upload.unlink(missing_ok=True)
            return _response({"ok": False, "job": _payload(job)}, 503)
    return _response({"ok": True, "job": _payload(job)}, 202)


@bp.route("/api/scripts/import/<job_id>")
def api_script_import_status(job_id):
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("脚本导入记录无法读取", 503, "history_unavailable")
    if not SCRIPT_PULL_ID_RE.fullmatch(job_id):
        return _error("job_id 无效", 400, "invalid_job_id")
    with SCRIPT_IMPORT_LOCK:
        job = SCRIPT_IMPORT_JOBS.get(job_id)
        return _response({"ok": True, "job": _payload(job)}) if job else _error("作业不存在", 404, "not_found")


@bp.route("/api/scripts/import-history")
def api_script_import_history():
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("脚本导入记录无法读取", 503, "history_unavailable")
    try:
        limit = int(request.args.get("limit", "20"))
    except (TypeError, ValueError):
        return _error("limit 必须为整数", 400, "invalid_limit")
    if not 1 <= limit <= 100:
        return _error("limit 必须在 1 到 100 之间", 400, "invalid_limit")
    with SCRIPT_IMPORT_LOCK:
        jobs = sorted(SCRIPT_IMPORT_JOBS.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)[:limit]
        return _response({"ok": True, "items": [_payload(job) for job in jobs]})


@bp.route("/api/scripts/import/<job_id>/cancel", methods=["POST"])
def api_script_import_cancel(job_id):
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("脚本导入记录无法读取", 503, "history_unavailable")
    if not SCRIPT_PULL_ID_RE.fullmatch(job_id):
        return _error("job_id 无效", 400, "invalid_job_id")
    with SCRIPT_IMPORT_LOCK:
        job = SCRIPT_IMPORT_JOBS.get(job_id)
        if job is None:
            return _error("作业不存在", 404, "not_found")
        if job.get("status") not in ("pending", "processing"):
            return _error("作业已进入发布或结束阶段，不能取消", 409, "not_cancellable")
        cancel = SCRIPT_IMPORT_CANCEL.get(job_id)
        if cancel is None:
            return _error("作业状态无法确认，请检查目标路径", 409, "state_unknown")
        previous = (job.get("status"), job.get("phase"), job.get("updated_at"))
        job.update(status="cancelling", phase="cancelling", updated_at=now_str())
        try:
            _save_locked()
        except Exception:
            job.update(status=previous[0], phase=previous[1], updated_at=previous[2])
            return _error("取消状态无法保存，作业未取消", 503, "job_persistence_failed")
        cancel.set()
        return _response({"ok": True, "job": _payload(job)}, 202)
