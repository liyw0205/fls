import errno
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import request

from . import bp
from .helpers import build_repo_pull_command
from ...directory_jobs import directory_job_operation, start_directory_job
from .native_pull import (
    SCRIPT_PULL_ID_RE,
    _error,
    _json_body,
    _open_script_parent,
    _renameat2_no_replace,
    _request_fingerprint,
    _response,
    _safe_url,
    _script_path_still_matches,
)
from ...paths import SCRIPT_DIR, SCRIPT_REPO_PULL_JOBS_FILE
from ...proxy import apply_proxy_env, validate_proxy_id
from ...script_files import script_file_target
from ...storage import write_json
from ...utils import now_str

SCRIPT_REPO_PULL_HISTORY_LIMIT = 200
SCRIPT_REPO_PULL_ACTIVE_LIMIT = 2
SCRIPT_REPO_PULL_ACTIVE = ("pending", "cloning", "publishing", "cancelling")
SCRIPT_REPO_PULL_TERMINAL = ("complete", "failed", "cancelled", "unknown")
SCRIPT_REPO_PULL_LOCK = threading.RLock()
SCRIPT_REPO_PULL_JOBS = {}
SCRIPT_REPO_PULL_CANCEL = {}
SCRIPT_REPO_PULL_INITIALIZED = False

_PROGRESS_RE = re.compile(
    r"^(Enumerating|Counting|Compressing|Receiving|Resolving|Updating) "
    r"(objects|deltas|files):\s*(?:\d+%\s*)?"
    r"(?:\((\d+)(?:/(\d+))?\)|(\d+)(?:,\s|$))"
)
_PROGRESS_LABELS = {
    "queued": "等待开始",
    "connecting": "连接仓库",
    "enumerating": "枚举对象",
    "counting": "统计对象",
    "compressing": "压缩对象",
    "receiving": "接收对象",
    "resolving": "解析差异",
    "updating": "更新文件",
    "publishing": "发布仓库",
    "complete": "已完成",
    "failed": "拉取失败",
    "cancelled": "已取消",
    "cancelling": "正在取消",
    "unknown": "结果未知",
}


class _RepoCancelled(Exception):
    pass


class _RepoCloneFailed(Exception):
    pass


class _RepoPersistenceError(Exception):
    pass


class _RepoPathChanged(Exception):
    pass


def _stage_name(job_id):
    return f".fls-repo-{job_id}.tmp"


def _parent_path(target, parent_fd):
    if parent_fd is not None:
        proc_fd = f"/proc/self/fd/{parent_fd}"
        if os.path.isdir(proc_fd):
            return proc_fd
        raise OSError(errno.ENOTSUP, "descriptor-based repository staging is unavailable")
    return str(target.parent)


def _stage_path(target, parent_fd, job_id):
    return os.path.join(_parent_path(target, parent_fd), _stage_name(job_id))


def _remove_tree_contents(directory_fd):
    with os.scandir(directory_fd) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                _remove_tree_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _remove_readonly(function, path, error):
    exc = error[1]
    if os.name != "nt" or not isinstance(exc, PermissionError) or function not in (os.unlink, os.rmdir):
        raise exc
    metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if not attributes & stat.FILE_ATTRIBUTE_READONLY or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise exc
    os.chmod(path, metadata.st_mode | stat.S_IWRITE)
    function(path)


def _remove_entry(target, parent_fd, name, expected_identity=None):
    try:
        if parent_fd is not None:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            identity = (metadata.st_dev, metadata.st_ino)
            if expected_identity is not None and identity != expected_identity:
                return False
            if stat.S_ISDIR(metadata.st_mode):
                directory_fd = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                try:
                    opened = os.fstat(directory_fd)
                    if (opened.st_dev, opened.st_ino) != identity:
                        return False
                    _remove_tree_contents(directory_fd)
                finally:
                    os.close(directory_fd)
                os.rmdir(name, dir_fd=parent_fd)
            else:
                os.unlink(name, dir_fd=parent_fd)
            return True

        path = target.parent / name
        metadata = path.lstat()
        if expected_identity is not None and (metadata.st_dev, metadata.st_ino) != expected_identity:
            return False
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(path, onerror=_remove_readonly)
        else:
            path.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _target_exists(target, parent_fd, name):
    try:
        if parent_fd is not None:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        else:
            os.lstat(target)
        return True
    except FileNotFoundError:
        return False


def _repo_head(directory_fd, directory_path=None):
    proc_fd = f"/proc/self/fd/{directory_fd}" if directory_fd is not None else ""
    pass_fds = ()
    if directory_fd is not None and os.name == "posix" and os.path.isdir(proc_fd):
        cwd = proc_fd
        pass_fds = (directory_fd,)
    elif directory_path is not None:
        try:
            path_metadata = os.stat(directory_path, follow_symlinks=False)
            fd_metadata = os.fstat(directory_fd) if directory_fd is not None else None
        except OSError:
            return None
        if not stat.S_ISDIR(path_metadata.st_mode):
            return None
        if fd_metadata is not None and (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ) != (fd_metadata.st_dev, fd_metadata.st_ino):
            return None
        cwd = str(directory_path)
    else:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            pass_fds=pass_fds,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return ""
    value = result.stdout.strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{40,64}", value) else None


def _repo_target_matches(job):
    parent_fd = None
    repo_fd = None
    try:
        target, parent_fd, name, parent_identity = _open_script_parent(job["path"])
        expected = (job.get("target_dev"), job.get("target_ino"))
        if not all(type(value) is int for value in expected):
            return False
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if parent_fd is not None:
            repo_fd = os.open(name, flags, dir_fd=parent_fd)
            metadata = os.fstat(repo_fd)
        else:
            metadata = os.stat(target, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != expected:
            return False
        head = _repo_head(repo_fd, target)
        if head is None or head != job.get("head"):
            return False
        try:
            git_metadata = (
                os.stat(".git", dir_fd=repo_fd, follow_symlinks=False)
                if repo_fd is not None
                else os.lstat(target / ".git")
            )
        except OSError:
            return False
        if not stat.S_ISDIR(git_metadata.st_mode):
            return False
        return _script_path_still_matches(job["path"], target, parent_fd, parent_identity)
    except (OSError, KeyError, ValueError):
        return False
    finally:
        if repo_fd is not None:
            os.close(repo_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _save_locked():
    ordered = sorted(
        SCRIPT_REPO_PULL_JOBS.values(),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    keep_ids = {item["id"] for item in ordered[:SCRIPT_REPO_PULL_HISTORY_LIMIT]}
    keep_ids.update(item["id"] for item in ordered if item.get("status") in SCRIPT_REPO_PULL_ACTIVE)
    rows = [item for item in ordered if item["id"] in keep_ids]
    write_json(SCRIPT_REPO_PULL_JOBS_FILE, rows)
    for job_id in tuple(SCRIPT_REPO_PULL_JOBS):
        if job_id not in keep_ids:
            SCRIPT_REPO_PULL_JOBS.pop(job_id, None)


def _ensure_loaded():
    global SCRIPT_REPO_PULL_INITIALIZED
    with SCRIPT_REPO_PULL_LOCK:
        if SCRIPT_REPO_PULL_INITIALIZED:
            return
        try:
            raw = json.loads(SCRIPT_REPO_PULL_JOBS_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = []
        except (OSError, ValueError) as exc:
            raise RuntimeError("仓库拉取记录无法读取，未覆盖原记录") from exc
        if not isinstance(raw, list):
            raise RuntimeError("仓库拉取记录不是列表，未覆盖原记录")

        SCRIPT_REPO_PULL_JOBS.clear()
        for row in raw[:SCRIPT_REPO_PULL_HISTORY_LIMIT]:
            if not isinstance(row, dict):
                continue
            job_id = str(row.get("id") or "")
            request_id = str(row.get("request_id") or "")
            if not SCRIPT_PULL_ID_RE.fullmatch(job_id) or not SCRIPT_PULL_ID_RE.fullmatch(request_id):
                continue
            status = str(row.get("status") or "unknown")
            if status in SCRIPT_REPO_PULL_ACTIVE:
                if status == "publishing" and _repo_target_matches(row):
                    status = "complete"
                else:
                    status = "unknown"
                    try:
                        target = script_file_target(row.get("path", ""))
                        parent_fd = None
                        try:
                            target, parent_fd, _name, _identity = _open_script_parent(row.get("path", ""))
                            stage_identity = (
                                row.get("stage_dev"),
                                row.get("stage_ino"),
                            )
                            if all(type(value) is int for value in stage_identity):
                                _remove_entry(
                                    target,
                                    parent_fd,
                                    _stage_name(job_id),
                                    stage_identity,
                                )
                        finally:
                            if parent_fd is not None:
                                os.close(parent_fd)
                    except (OSError, ValueError):
                        pass
                    row = dict(row, error_code="worker_interrupted")
            if status not in SCRIPT_REPO_PULL_TERMINAL:
                status = "unknown"
            normalized = {
                "id": job_id,
                "request_id": request_id,
                "fingerprint": str(row.get("fingerprint") or ""),
                "kind": "repo",
                "path": str(row.get("path") or ""),
                "source_host": str(row.get("source_host") or "")[:255],
                "status": status,
                "phase": str(row.get("phase") or "unknown")[:32],
                "current": _optional_nonnegative_int(row.get("current")),
                "total": _optional_nonnegative_int(row.get("total")),
                "target_dev": row.get("target_dev") if type(row.get("target_dev")) is int else None,
                "target_ino": row.get("target_ino") if type(row.get("target_ino")) is int else None,
                "stage_dev": row.get("stage_dev") if type(row.get("stage_dev")) is int else None,
                "stage_ino": row.get("stage_ino") if type(row.get("stage_ino")) is int else None,
                "head": str(row.get("head") or "")[:64],
                "error_code": str(row.get("error_code") or "")[:80],
                "created_at": str(row.get("created_at") or ""),
                "updated_at": str(row.get("updated_at") or ""),
            }
            SCRIPT_REPO_PULL_JOBS[job_id] = normalized
        try:
            _save_locked()
        except Exception as exc:
            SCRIPT_REPO_PULL_JOBS.clear()
            raise RuntimeError("仓库拉取记录无法保存，未覆盖原记录") from exc
        SCRIPT_REPO_PULL_INITIALIZED = True


def _optional_nonnegative_int(value):
    return value if type(value) is int and value >= 0 else None


def _payload(job, *, replayed=False):
    status = job.get("status", "unknown")
    phase = str(job.get("phase") or "unknown")
    current = job.get("current")
    total = job.get("total")
    return {
        "id": job.get("id", ""),
        "request_id": job.get("request_id", ""),
        "kind": "repo",
        "path": job.get("path", ""),
        "source_host": job.get("source_host", ""),
        "status": status,
        "progress": {
            "phase": phase,
            "current": current,
            "total": total,
            "label": _PROGRESS_LABELS.get(phase, "等待 Git 操作" if phase == "queued" else "准备中"),
        },
        "error_code": job.get("error_code", ""),
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        "replayed": replayed,
    }


def _update(job_id, *, persist=True, **updates):
    with SCRIPT_REPO_PULL_LOCK:
        job = SCRIPT_REPO_PULL_JOBS.get(job_id)
        if job is None:
            return None
        job.update(updates)
        job["updated_at"] = now_str()
        if persist:
            try:
                _save_locked()
            except Exception as exc:
                raise _RepoPersistenceError from exc
        return dict(job)


def _parse_progress(line):
    match = _PROGRESS_RE.match(line.strip())
    if not match:
        return None
    phase = match.group(1)
    current = match.group(3) or match.group(5)
    total = match.group(4)
    return phase, int(current) if current is not None else None, int(total) if total is not None else None


def _drain_stderr(stream, output_queue):
    buffer = b""
    try:
        while True:
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            buffer += chunk
            while True:
                boundary = min(
                    (position for marker in (b"\r", b"\n") if (position := buffer.find(marker)) >= 0),
                    default=-1,
                )
                if boundary < 0:
                    break
                output_queue.put(buffer[:boundary].decode("utf-8", errors="replace")[:512])
                buffer = buffer[boundary + 1:]
        if buffer:
            output_queue.put(buffer.decode("utf-8", errors="replace")[:512])
    finally:
        stream.close()


def _stop_process(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        process.wait()


def _run_clone(job_id, url, proxy_id, target, parent_fd, parent_identity, cancel):
    stage = _stage_name(job_id)
    stage_path = None
    process = None
    reader = None
    output_queue = queue.Queue()
    created_stage = False
    stage_identity = None
    outcome = None

    try:
        if cancel.is_set():
            raise _RepoCancelled
        if not _script_path_still_matches(
            SCRIPT_REPO_PULL_JOBS[job_id]["path"], target, parent_fd, parent_identity,
        ):
            raise _RepoPathChanged
        if _target_exists(target, parent_fd, target.name):
            raise FileExistsError(target.name)
        if parent_fd is not None:
            os.mkdir(stage, 0o700, dir_fd=parent_fd)
        else:
            os.mkdir(target.parent / stage, 0o700)
        created_stage = True
        stage_metadata = (
            os.stat(stage, dir_fd=parent_fd, follow_symlinks=False)
            if parent_fd is not None
            else os.lstat(target.parent / stage)
        )
        stage_identity = (stage_metadata.st_dev, stage_metadata.st_ino)
        _update(job_id, stage_dev=stage_identity[0], stage_ino=stage_identity[1])
        if cancel.is_set():
            raise _RepoCancelled
        stage_path = _stage_path(target, parent_fd, job_id)
        clone_target = os.path.join(stage_path, "repo")
        command = build_repo_pull_command(url, clone_target, proxy_id, progress=True)
        env = apply_proxy_env(os.environ.copy(), proxy_id)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        kwargs = {
            "cwd": str(SCRIPT_DIR),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
            "start_new_session": os.name == "posix",
        }
        if parent_fd is not None and os.name == "posix":
            kwargs["pass_fds"] = (parent_fd,)
        process = subprocess.Popen(command, **kwargs)
        reader = threading.Thread(
            target=_drain_stderr,
            args=(process.stderr, output_queue),
            daemon=True,
            name=f"fls-repo-progress-{job_id[:8]}",
        )
        reader.start()
        _update(job_id, status="cloning", phase="connecting", current=None, total=None)
        last_saved = time.monotonic()
        while process.poll() is None or reader.is_alive() or not output_queue.empty():
            if cancel.is_set():
                _stop_process(process)
                raise _RepoCancelled
            try:
                line = output_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            parsed = _parse_progress(line)
            if parsed is None:
                continue
            git_phase, current, total = parsed
            phase = git_phase.lower()
            persist = time.monotonic() - last_saved >= 0.5
            _update(job_id, persist=persist, phase=phase, current=current, total=total)
            if persist:
                last_saved = time.monotonic()
        return_code = process.wait()
        reader.join(timeout=1)
        if cancel.is_set():
            raise _RepoCancelled
        if return_code != 0:
            raise _RepoCloneFailed

        if parent_fd is not None:
            stage_fd = _open_dir_at(target, parent_fd, stage)
            try:
                repo_fd = os.open(
                    "repo",
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=stage_fd,
                )
                try:
                    staged = os.fstat(repo_fd)
                    head = _repo_head(repo_fd, os.path.join(stage_path, "repo"))
                    try:
                        os.stat(".git", dir_fd=repo_fd, follow_symlinks=False)
                    except OSError as exc:
                        raise _RepoCloneFailed from exc
                finally:
                    os.close(repo_fd)
            finally:
                os.close(stage_fd)
        else:
            staged = os.stat(clone_target, follow_symlinks=False)
            if not stat.S_ISDIR(staged.st_mode):
                raise _RepoCloneFailed
            head = _repo_head(None, clone_target)
            try:
                git_metadata = os.lstat(os.path.join(clone_target, ".git"))
            except OSError as exc:
                raise _RepoCloneFailed from exc
            if not stat.S_ISDIR(git_metadata.st_mode):
                raise _RepoCloneFailed
        if head is None:
            raise _RepoCloneFailed
        _update(
            job_id,
            status="publishing",
            phase="publishing",
            current=None,
            total=None,
            target_dev=staged.st_dev,
            target_ino=staged.st_ino,
            head=head,
        )

        with SCRIPT_REPO_PULL_LOCK:
            if cancel.is_set():
                raise _RepoCancelled
            if not _script_path_still_matches(
                SCRIPT_REPO_PULL_JOBS[job_id]["path"],
                target,
                parent_fd,
                parent_identity,
            ):
                raise _RepoPathChanged
            if _target_exists(target, parent_fd, target.name):
                raise FileExistsError(target.name)
            if os.name == "nt":
                os.rename(os.path.join(stage_path, "repo"), target)
            elif parent_fd is not None:
                if not _renameat2_no_replace(f"{stage}/repo", target.name, parent_fd):
                    raise OSError(errno.ENOTSUP, "atomic repository publish is unavailable")
            elif not _renameat2_no_replace(os.path.join(stage_path, "repo"), str(target), None):
                raise OSError(errno.ENOTSUP, "atomic repository publish is unavailable")
            published = _stat_at(target, parent_fd, target.name)
            if (
                published is None
                or (published.st_dev, published.st_ino) != (staged.st_dev, staged.st_ino)
                or not _script_path_still_matches(
                    SCRIPT_REPO_PULL_JOBS[job_id]["path"],
                    target,
                    parent_fd,
                    parent_identity,
                )
            ):
                _remove_entry(target, parent_fd, target.name, (staged.st_dev, staged.st_ino))
                raise _RepoPathChanged
            outcome = ("complete", "")
    except _RepoCancelled:
        outcome = ("cancelled", "")
    except FileExistsError:
        outcome = ("failed", "target_exists")
    except _RepoPathChanged:
        outcome = ("failed", "path_changed")
    except _RepoCloneFailed:
        outcome = ("failed", "clone_failed")
    except _RepoPersistenceError:
        outcome = ("failed", "job_persistence_failed")
    except Exception:
        outcome = ("failed", "clone_failed")
    finally:
        try:
            if process is not None:
                _stop_process(process)
            if reader is not None and reader.ident is not None:
                reader.join()
            elif process is not None and process.stderr is not None:
                process.stderr.close()
            if created_stage and target is not None:
                if not _remove_entry(target, parent_fd, stage, stage_identity):
                    outcome = ("unknown", "cleanup_failed")
        finally:
            if parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
            with SCRIPT_REPO_PULL_LOCK:
                SCRIPT_REPO_PULL_CANCEL.pop(job_id, None)
        if outcome is not None:
            _finish_or_fail(job_id, target, None, *outcome)


def _open_dir_at(target, parent_fd, name):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(name, flags, dir_fd=parent_fd) if parent_fd is not None else os.open(target.parent / name, flags)


def _stat_at(target, parent_fd, name):
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False) if parent_fd is not None else os.lstat(target)
    except OSError:
        return None


def _finish_or_fail(job_id, target, parent_fd, status, error_code):
    with SCRIPT_REPO_PULL_LOCK:
        current = SCRIPT_REPO_PULL_JOBS.get(job_id)
        if current is None:
            return
        snapshot = dict(current)
    published = bool(error_code != "cleanup_failed" and target is not None
                     and snapshot.get("head") is not None and _repo_target_matches(snapshot))
    with SCRIPT_REPO_PULL_LOCK:
        job = SCRIPT_REPO_PULL_JOBS.get(job_id)
        if job is None:
            return
        job.update(
            status="complete" if published else status,
            phase="complete" if published else status,
            error_code="" if published else error_code,
            updated_at=now_str(),
        )
        try:
            _save_locked()
        except Exception:
            if not published:
                job.update(status="unknown", phase="unknown", error_code="job_persistence_failed")


def _safe_repo_url(raw):
    try:
        value, host = _safe_url(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_url") from exc
    if urlparse(value).query:
        raise ValueError("invalid_url")
    return value, host


@bp.route("/api/scripts/repo-pull", methods=["POST"])
@directory_job_operation
def api_script_repo_pull_submit():
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("仓库拉取记录无法读取，未启动新作业", 503, "history_unavailable")
    body, error_response = _json_body()
    if error_response is not None:
        return error_response
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not SCRIPT_PULL_ID_RE.fullmatch(request_id):
        return _error("request_id 必须为 32 位小写十六进制字符串", 400, "invalid_request_id")
    try:
        url, source_host = _safe_repo_url(body.get("url"))
    except (TypeError, ValueError):
        return _error("仅支持不含账号、查询参数或片段的 HTTP/HTTPS 仓库 URL", 400, "invalid_url")
    raw_path = body.get("path")
    if not isinstance(raw_path, str):
        return _error("目标路径必须是 scripts 内的安全相对目录路径", 400, "invalid_path")
    try:
        target = script_file_target(raw_path)
        relative = target.relative_to(SCRIPT_DIR.resolve()).as_posix()
    except (OSError, ValueError):
        return _error("目标路径必须是 scripts 内的安全相对目录路径", 400, "invalid_path")
    if not target.parent.is_dir() or target.parent.is_symlink():
        return _error("目标父目录不存在或不安全", 404, "invalid_parent")
    proxy_id = body.get("proxy_id", "")
    if not isinstance(proxy_id, str) or len(proxy_id) > 128:
        return _error("proxy_id 无效", 400, "invalid_proxy")
    try:
        validate_proxy_id(proxy_id)
    except ValueError:
        return _error("所选代理无效或已停用", 400, "invalid_proxy")

    fingerprint = _request_fingerprint(url, relative, proxy_id)
    with SCRIPT_REPO_PULL_LOCK:
        previous = next(
            (item for item in SCRIPT_REPO_PULL_JOBS.values() if item.get("request_id") == request_id),
            None,
        )
        if previous is not None:
            if previous.get("fingerprint") != fingerprint:
                return _error("request_id 已用于不同请求", 409, "request_id_conflict")
            return _response({"ok": True, "job": _payload(previous, replayed=True)}, 200)
        try:
            target, parent_fd, target_name, parent_identity = _open_script_parent(relative)
        except (OSError, ValueError):
            return _error("目标父目录已变化或不安全", 409, "path_changed")
        if not _script_path_still_matches(relative, target, parent_fd, parent_identity):
            if parent_fd is not None:
                os.close(parent_fd)
            return _error("目标父目录已变化或不安全", 409, "path_changed")
        if _target_exists(target, parent_fd, target_name):
            if parent_fd is not None:
                os.close(parent_fd)
            return _error("目标目录已存在，未覆盖", 409, "target_exists")
        if sum(item.get("status") in SCRIPT_REPO_PULL_ACTIVE for item in SCRIPT_REPO_PULL_JOBS.values()) >= SCRIPT_REPO_PULL_ACTIVE_LIMIT:
            if parent_fd is not None:
                os.close(parent_fd)
            return _error("仓库拉取作业过多，请等待当前作业结束", 429, "too_many_active_jobs")
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "request_id": request_id,
            "fingerprint": fingerprint,
            "kind": "repo",
            "path": relative,
            "source_host": source_host,
            "status": "pending",
            "phase": "queued",
            "current": None,
            "total": None,
            "target_dev": None,
            "target_ino": None,
            "head": None,
            "error_code": "",
            "created_at": now_str(),
            "updated_at": now_str(),
        }
        SCRIPT_REPO_PULL_JOBS[job_id] = job
        cancel = threading.Event()
        SCRIPT_REPO_PULL_CANCEL[job_id] = cancel
        try:
            _save_locked()
        except Exception:
            SCRIPT_REPO_PULL_JOBS.pop(job_id, None)
            SCRIPT_REPO_PULL_CANCEL.pop(job_id, None)
            if parent_fd is not None:
                os.close(parent_fd)
            return _error("作业回执无法保存，未开始克隆", 503, "job_persistence_failed")
        try:
            start_directory_job(
                _start_repo_worker,
                args=(job_id, url, proxy_id, relative, target, parent_fd, parent_identity),
                name=f"fls-repo-pull-{job_id[:8]}",
            )
        except Exception:
            SCRIPT_REPO_PULL_CANCEL.pop(job_id, None)
            job.update(status="failed", phase="failed", error_code="worker_start_failed", updated_at=now_str())
            if parent_fd is not None:
                os.close(parent_fd)
            try:
                _save_locked()
            except Exception:
                job.update(status="unknown", phase="unknown", error_code="job_persistence_failed")
            return _response({"ok": False, "job": _payload(job)}, 503)
    return _response({"ok": True, "job": _payload(job)}, 202)


def _start_repo_worker(job_id, url, proxy_id, relative, target, parent_fd, parent_identity):
    with SCRIPT_REPO_PULL_LOCK:
        cancel = SCRIPT_REPO_PULL_CANCEL.get(job_id)
    if cancel is None:
        if parent_fd is not None:
            os.close(parent_fd)
        return
    _run_clone(job_id, url, proxy_id, target, parent_fd, parent_identity, cancel)


@bp.route("/api/scripts/repo-pull/<job_id>")
@directory_job_operation
def api_script_repo_pull_status(job_id):
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("仓库拉取记录无法读取", 503, "history_unavailable")
    if not SCRIPT_PULL_ID_RE.fullmatch(job_id):
        return _error("job_id 无效", 400, "invalid_job_id")
    with SCRIPT_REPO_PULL_LOCK:
        job = SCRIPT_REPO_PULL_JOBS.get(job_id)
        return _response({"ok": True, "job": _payload(job)}) if job else _error("作业不存在", 404, "not_found")


@bp.route("/api/scripts/repo-pull-history")
@directory_job_operation
def api_script_repo_pull_history():
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("仓库拉取记录无法读取", 503, "history_unavailable")
    try:
        limit = int(request.args.get("limit", "20"))
    except (TypeError, ValueError):
        return _error("limit 必须为整数", 400, "invalid_limit")
    if limit < 1 or limit > 100:
        return _error("limit 必须在 1 到 100 之间", 400, "invalid_limit")
    with SCRIPT_REPO_PULL_LOCK:
        jobs = sorted(
            SCRIPT_REPO_PULL_JOBS.values(),
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )[:limit]
        return _response({"ok": True, "items": [_payload(job) for job in jobs]})


@bp.route("/api/scripts/repo-pull/<job_id>/cancel", methods=["POST"])
@directory_job_operation
def api_script_repo_pull_cancel(job_id):
    try:
        _ensure_loaded()
    except RuntimeError:
        return _error("仓库拉取记录无法读取", 503, "history_unavailable")
    if not SCRIPT_PULL_ID_RE.fullmatch(job_id):
        return _error("job_id 无效", 400, "invalid_job_id")
    with SCRIPT_REPO_PULL_LOCK:
        job = SCRIPT_REPO_PULL_JOBS.get(job_id)
        if job is None:
            return _error("作业不存在", 404, "not_found")
        if job.get("status") not in SCRIPT_REPO_PULL_ACTIVE:
            return _error("作业已结束，不能取消", 409, "not_cancellable")
        cancel = SCRIPT_REPO_PULL_CANCEL.get(job_id)
        if cancel is None:
            return _error("作业状态无法确认，请检查目标目录", 409, "state_unknown")
        previous_status, previous_phase, previous_updated = (
            job.get("status"), job.get("phase"), job.get("updated_at"),
        )
        job.update(status="cancelling", phase="cancelling", updated_at=now_str())
        try:
            _save_locked()
        except Exception:
            job.update(status=previous_status, phase=previous_phase, updated_at=previous_updated)
            return _error("取消状态无法保存，作业未取消", 503, "job_persistence_failed")
        cancel.set()
        return _response({"ok": True, "job": _payload(job)}, 202)


@bp.route("/api/scripts/repo-pull-options")
def api_script_repo_pull_options():
    from ...models import load_proxies

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
