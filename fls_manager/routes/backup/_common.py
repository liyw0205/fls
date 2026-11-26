from .bp import bp
import os
import json
import hashlib
import stat
import sys
import time
import uuid
import shutil
import tarfile
import zipfile
import tempfile
import threading
import subprocess
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from urllib.parse import quote

from flask import request, Response, redirect, url_for, jsonify, abort

from ...paths import BASE_DIR, DATA_DIR, SCRIPT_DIR, LOG_DIR
from ... import backup_commit
from ...backup_lease import try_process_lease, release_process_lease
from ...storage import mutate_json, storage_transaction, write_json
from ...ui.layout import layout
from ...ui.components import page_header, empty_table_row
from ...utils import h, now_str, safe_name, prune_completed_records
from ...scheduler import reload_scheduler

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_JOBS_FILE = DATA_DIR / "backup_jobs.json"
BACKUP_RESTORE_REQUESTS_DIR = BASE_DIR / ".backup_restore_requests"
BACKUP_JOBS = {}
BACKUP_CANCEL_EVENTS = {}
BACKUP_JOB_LOCK = threading.RLock()
BACKUP_RESTORE_REQUEST_LOCK = threading.RLock()
BACKUP_ACTIVITY_CONDITION = threading.Condition(threading.RLock())
ACTIVE_BACKUP_WORKERS = 0
RESTORE_ACTIVE = False
BACKUP_ACTIVITY_LEASE = None
RESTORE_ACTIVITY_LEASE = None
BACKUP_ACTIVITY_LOCK_PATH = BASE_DIR / ".backup_native_restore.lock"
BACKUP_JOB_LIMIT = 100
BACKUP_DIRECTORY_FD_SUPPORTED = os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")
BACKUP_DEPENDENCY_TIMEOUT_SECONDS = 120
MIB = 1024 * 1024


def _positive_env_limit(name, default):
    try:
        value = int(os.environ.get(name, ""))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


MAX_BACKUP_UPLOAD_BYTES = _positive_env_limit("FLS_BACKUP_MAX_UPLOAD_MB", 512) * MIB
MAX_BACKUP_EXTRACTED_BYTES = _positive_env_limit("FLS_BACKUP_MAX_EXTRACTED_MB", 2048) * MIB
MAX_BACKUP_ARCHIVE_ENTRIES = _positive_env_limit("FLS_BACKUP_MAX_ARCHIVE_ENTRIES", 10000)


class BackupCancelled(Exception):
    pass


class BackupTimeout(Exception):
    pass


class BackupJobStartError(RuntimeError):
    def __init__(self, message, job_id=""):
        super().__init__(message)
        self.job_id = job_id


class BackupLimitExceeded(ValueError):
    pass


class BackupArchivePublishUnknown(RuntimeError):
    pass


def _try_begin_backup_activity():
    global ACTIVE_BACKUP_WORKERS, BACKUP_ACTIVITY_LEASE
    if RESTORE_ACTIVE:
        return False
    lease = None
    if not ACTIVE_BACKUP_WORKERS:
        lease = try_process_lease(BACKUP_ACTIVITY_LOCK_PATH, shared=True)
        if lease is None:
            return False
    try:
        if backup_commit.pending(BASE_DIR):
            raise BackupJobStartError("先完成原恢复请求的目录现场恢复，再创建备份")
        ACTIVE_BACKUP_WORKERS += 1
        if lease is not None:
            BACKUP_ACTIVITY_LEASE = lease
            lease = None
        return True
    finally:
        release_process_lease(lease)


def begin_backup_activity():
    with BACKUP_ACTIVITY_CONDITION:
        while not _try_begin_backup_activity():
            BACKUP_ACTIVITY_CONDITION.wait(timeout=0.1)


def end_backup_activity():
    global ACTIVE_BACKUP_WORKERS, BACKUP_ACTIVITY_LEASE
    with BACKUP_ACTIVITY_CONDITION:
        ACTIVE_BACKUP_WORKERS = max(0, ACTIVE_BACKUP_WORKERS - 1)
        if not ACTIVE_BACKUP_WORKERS:
            lease, BACKUP_ACTIVITY_LEASE = BACKUP_ACTIVITY_LEASE, None
            release_process_lease(lease)
        BACKUP_ACTIVITY_CONDITION.notify_all()


def try_begin_restore_activity(*, process_lease_fd=None):
    global RESTORE_ACTIVE, RESTORE_ACTIVITY_LEASE
    with BACKUP_ACTIVITY_CONDITION:
        if RESTORE_ACTIVE or ACTIVE_BACKUP_WORKERS:
            return False
        lease = None
        # Native workers already own this lease and release it themselves.
        if process_lease_fd is None:
            lease = try_process_lease(BACKUP_ACTIVITY_LOCK_PATH)
            if lease is None:
                return False
        try:
            if backup_commit.pending(BASE_DIR):
                return False
            if lease is not None:
                # Legacy work must not advertise a previous native lease owner.
                os.ftruncate(lease, 1)
                RESTORE_ACTIVITY_LEASE, lease = lease, None
            RESTORE_ACTIVE = True
            return True
        finally:
            release_process_lease(lease)


def restore_activity_active():
    with BACKUP_ACTIVITY_CONDITION:
        if RESTORE_ACTIVE:
            return True
        try:
            lease = try_process_lease(BACKUP_ACTIVITY_LOCK_PATH, create=False, shared=True)
        except FileNotFoundError:
            return False
        if lease is None:
            return True
        release_process_lease(lease)
        return False


def end_restore_activity():
    global RESTORE_ACTIVE, RESTORE_ACTIVITY_LEASE
    with BACKUP_ACTIVITY_CONDITION:
        lease, RESTORE_ACTIVITY_LEASE = RESTORE_ACTIVITY_LEASE, None
        release_process_lease(lease)
        RESTORE_ACTIVE = False
        BACKUP_ACTIVITY_CONDITION.notify_all()


# ============================================================
# 基础工具
# ============================================================

def backup_dir():
    if BACKUP_DIR.is_symlink():
        raise ValueError("备份目录不能是符号链接")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def validate_backup_filename(filename):
    if (not isinstance(filename, str) or not filename.endswith(".tar.gz")
        or filename != filename.strip() or "/" in filename or "\\" in filename or ":" in filename
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
        raise ValueError("备份文件名无效，请刷新备份列表后重新选择")
    return filename


@contextmanager
def backup_directory_fd():
    if not BACKUP_DIRECTORY_FD_SUPPORTED:
        yield backup_dir()
        return
    directory = os.open(backup_dir(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield directory
    finally:
        os.close(directory)


def backup_file_version(info):
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return hashlib.sha256(repr(identity).encode("ascii")).hexdigest()


def backup_entry_stat(directory, filename):
    if isinstance(directory, Path):
        backup_dir()
        return (directory / filename).lstat()
    return os.stat(filename, dir_fd=directory, follow_symlinks=False)


def backup_entry_unlink(directory, filename):
    if isinstance(directory, Path):
        backup_safe_file(filename).unlink()
    else:
        os.unlink(filename, dir_fd=directory)


def _backup_staging_name(job_id):
    identity = hashlib.sha256(str(job_id or "").encode("utf-8")).hexdigest()
    return f".fls-backup-{identity}.part"


def _remove_orphaned_backup_staging(job_id):
    name = _backup_staging_name(job_id)
    try:
        with backup_directory_fd() as directory:
            try:
                expected = backup_entry_stat(directory, name)
            except FileNotFoundError:
                return ""
            if not stat.S_ISREG(expected.st_mode):
                return "遗留暂存归档不是常规文件，未删除"
            try:
                current = backup_entry_stat(directory, name)
            except FileNotFoundError:
                return ""
            if not os.path.samestat(expected, current):
                return "遗留暂存归档身份已变化，未删除"
            if isinstance(directory, int):
                os.unlink(name, dir_fd=directory)
            else:
                (directory / name).unlink()
        return ""
    except Exception as exc:
        return f"未能回收遗留暂存归档：{exc}"


def open_backup_entry(directory, filename):
    expected = backup_entry_stat(directory, filename)
    if not stat.S_ISREG(expected.st_mode):
        raise ValueError("备份文件不是常规文件")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    if isinstance(directory, Path):
        descriptor = os.open(backup_safe_file(filename), flags)
    else:
        descriptor = os.open(filename, flags, dir_fd=directory)
    source = os.fdopen(descriptor, "rb")
    try:
        actual = os.fstat(source.fileno())
        if (not stat.S_ISREG(actual.st_mode)
            or backup_file_version(actual) != backup_file_version(expected)
            or backup_file_version(backup_entry_stat(directory, filename)) != backup_file_version(actual)):
            raise ValueError("备份文件已变化，请刷新列表后重新下载")
        return source
    except BaseException:
        source.close()
        raise


def _backup_job_snapshot(info):
    fields = (
        "id",
        "items",
        "type_text",
        "running",
        "status",
        "filename",
        "size",
        "size_text",
        "error",
        "created_at",
        "updated_at",
        "cancel_requested",
        "cancel_requested_at",
        "cancel_error",
        "staged_cleanup_pending",
        "staged_cleanup_error",
    )
    return {field: info.get(field) for field in fields}


def persist_backup_job(info):
    with BACKUP_JOB_LOCK:
        snapshot = _backup_job_snapshot(info)
        job_id = str(snapshot.get("id") or "")
        if not job_id:
            return

        def update(data):
            jobs = validate_backup_jobs(data)
            jobs[job_id] = snapshot
            ordered = sorted(
                jobs.items(),
                key=lambda pair: str(pair[1].get("updated_at") or pair[1].get("created_at") or ""),
                reverse=True,
            )
            protected, history = [], []
            for pair in ordered:
                info = pair[1]
                if (
                    info.get("running") is True
                    or info.get("status") in ("准备中", "正在压缩", "正在取消", "结果未知")
                    or info.get("staged_cleanup_pending") is True
                ):
                    protected.append(pair)
                else:
                    history.append(pair)
            return dict(protected + history[:BACKUP_JOB_LIMIT])

        mutate_json(BACKUP_JOBS_FILE, {}, update, strict=True)


def validate_backup_jobs(data):
    if not isinstance(data, dict) or any(not isinstance(value, dict) or value.get("id") != key for key, value in data.items()):
        raise RuntimeError("备份作业记录损坏，未覆盖原文件")
    if any(value.get("cancel_requested") is not None and type(value["cancel_requested"]) is not bool
           or value.get("cancel_requested_at") is not None and not isinstance(value["cancel_requested_at"], str)
           or value.get("cancel_error") is not None and not isinstance(value["cancel_error"], str)
           or value.get("staged_cleanup_pending") is not None and type(value["staged_cleanup_pending"]) is not bool
           or value.get("staged_cleanup_error") is not None and not isinstance(value["staged_cleanup_error"], str)
           for value in data.values()):
        raise RuntimeError("备份取消或暂存清理记录损坏，未覆盖原文件")
    return data


def _load_persisted_backup_job(job_id):
    with storage_transaction():
        try:
            jobs = validate_backup_jobs(json.loads(BACKUP_JOBS_FILE.read_text(encoding="utf-8")))
        except FileNotFoundError:
            jobs = {}
    info = jobs.get(str(job_id or ""))
    return dict(info) if isinstance(info, dict) else None


def get_backup_job(job_id):
    """Return a live or persisted job, converting lost running handles to unknown."""
    with BACKUP_JOB_LOCK:
        info = BACKUP_JOBS.get(job_id)
        if not isinstance(info, dict):
            info = _load_persisted_backup_job(job_id)
            if not info:
                return None

        if info.get("running") and job_id in BACKUP_CANCEL_EVENTS:
            BACKUP_JOBS[job_id] = info
            return dict(info)

        changed = False
        if info.get("running"):
            info["running"] = False
            info["status"] = "结果未知"
            info["error"] = (
                "面板重启后无法确认备份进程是否完成，请检查备份列表和日志；"
                "如未生成可用归档，请重新创建备份。"
            )
            info["updated_at"] = now_str()
            info["staged_cleanup_pending"] = True
            info["staged_cleanup_error"] = ""
            changed = True

        if info.get("staged_cleanup_pending") is True and job_id not in BACKUP_CANCEL_EVENTS:
            cleanup_error = _remove_orphaned_backup_staging(job_id)
            previous_cleanup_error = str(info.get("staged_cleanup_error") or "")
            current_error = str(info.get("error") or "")
            if cleanup_error:
                info["staged_cleanup_error"] = cleanup_error
                if previous_cleanup_error and previous_cleanup_error in current_error:
                    info["error"] = current_error.replace(previous_cleanup_error, cleanup_error)
                elif cleanup_error not in current_error:
                    info["error"] = "；".join(filter(None, (current_error, cleanup_error)))
            else:
                info["staged_cleanup_pending"] = False
                info["staged_cleanup_error"] = ""
                if previous_cleanup_error:
                    recovered_error = current_error.replace(previous_cleanup_error, "暂存归档清理重试成功")
                    if recovered_error == current_error:
                        recovered_error = "；".join(filter(None, (current_error, "暂存归档清理重试成功")))
                    info["error"] = recovered_error
            info["updated_at"] = now_str()
            changed = True

        if changed:
            BACKUP_JOBS[job_id] = info
            try:
                persist_backup_job(info)
            except Exception as exc:
                info["error"] = "；".join(filter(None, (
                    str(info.get("error") or ""), f"未知状态或暂存清理结果未能持久化：{exc}"
                )))

        return dict(info)


def backup_safe_file(filename):
    validate_backup_filename(filename)

    base = backup_dir().resolve()
    candidate = base / filename

    if candidate.is_symlink():
        raise ValueError("备份文件不能是符号链接")

    target = candidate.resolve()

    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("备份文件路径非法")

    if target.exists() and not target.is_file():
        raise ValueError("备份文件不是常规文件")

    return target


def save_backup_upload(file_storage, target):
    target = Path(target)
    total = 0
    try:
        with target.open("wb") as output:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BACKUP_UPLOAD_BYTES:
                    raise BackupLimitExceeded(
                        "备份文件超过上传上限（{} MiB）".format(
                            MAX_BACKUP_UPLOAD_BYTES // MIB
                        )
                    )
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    return total


def fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "-"

    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0

    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1

    return f"{n:.1f} {units[i]}"


def backup_type_text(items):
    items = set(items or [])

    if "data" in items and "scripts" in items:
        return "配置 + 脚本"

    if "data" in items:
        return "仅配置"

    if "scripts" in items:
        return "仅脚本"

    return "未选择"


def parse_items_from_form(prefix="items"):
    items = request.form.getlist(prefix)
    result = []

    if "data" in items:
        result.append("data")

    if "scripts" in items:
        result.append("scripts")

    return result


def list_backup_files():
    files = []
    with backup_directory_fd() as directory:
        for name in os.listdir(directory):
            try:
                validate_backup_filename(name)
            except ValueError:
                continue
            try:
                info = backup_entry_stat(directory, name)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            files.append({
                "name": name,
                "version": backup_file_version(info),
                "size": info.st_size,
                "size_text": fmt_size(info.st_size),
                "mtime": info.st_mtime,
                "mtime_text": datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })

    files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return files


def write_dependencies_file(path, cancel_event=None):
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "pip", "freeze"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + BACKUP_DEPENDENCY_TIMEOUT_SECONDS
        while True:
            try:
                output, _ = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise BackupCancelled("备份任务已取消")
                if time.monotonic() < deadline:
                    continue
                process.terminate()
                try:
                    output, _ = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise subprocess.TimeoutExpired(
                    [sys.executable, "-m", "pip", "freeze"],
                    BACKUP_DEPENDENCY_TIMEOUT_SECONDS,
                )

        deps_text = output or ""

        if process.returncode != 0:
            deps_text = "# pip freeze 执行失败，以下为输出：\n" + deps_text

        Path(path).write_text(deps_text, encoding="utf-8")

    except BackupCancelled:
        raise
    except subprocess.TimeoutExpired as e:
        raise BackupTimeout(
            "备份依赖列表采集超时（超过 {} 秒）".format(
                BACKUP_DEPENDENCY_TIMEOUT_SECONDS
            )
        ) from e
    except Exception as e:
        Path(path).write_text(
            "# pip freeze 执行失败：{}\n".format(e),
            encoding="utf-8",
        )


def tar_filter_exclude_backups(tarinfo):
    """
    排除备份归档和运行时认证会话，避免递归归档或恢复失效/敏感会话。
    """
    try:
        name = str(tarinfo.name or "").replace("\\", "/")

        if (
            name == "data/backups"
            or name.startswith("data/backups/")
            or name == "data/native_sessions.sqlite3"
            or name.startswith("data/native_sessions.sqlite3-")
        ):
            return None
    except Exception:
        pass

    return tarinfo


# ============================================================
# 后台创建备份
# ============================================================

def _check_backup_cancelled(cancel_event):
    if cancel_event.is_set():
        raise BackupCancelled("备份任务已取消")


def _backup_tar_filter(cancel_event):
    def apply_filter(tarinfo):
        _check_backup_cancelled(cancel_event)
        filtered = tar_filter_exclude_backups(tarinfo)
        if filtered is None:
            return None
        if not (filtered.isfile() or filtered.isdir()):
            raise RuntimeError("备份目录包含不支持的文件类型")
        return filtered

    return apply_filter


def _update_backup_job(job_id, **updates):
    with BACKUP_JOB_LOCK:
        info = BACKUP_JOBS.get(job_id)
        if not isinstance(info, dict):
            return None
        if updates.get("running") and info.get("cancel_requested"):
            updates["status"] = "正在取消"
        info.update(updates)
        persist_backup_job(info)
        return info


def _finish_backup_job(job_id, status, error="", **updates):
    updates.update({
        "running": False,
        "status": status,
        "error": error,
        "updated_at": now_str(),
        "cancel_error": "",
    })
    try:
        return _update_backup_job(job_id, **updates)
    except Exception as persist_error:
        with BACKUP_JOB_LOCK:
            info = BACKUP_JOBS.get(job_id)
            if not isinstance(info, dict):
                return None
            detail = f"备份结果持久化失败：{persist_error}"
            info["error"] = "；".join(
                value for value in (str(info.get("error") or ""), detail) if value
            )
            return info


def _remove_backup_target(target):
    if target is None:
        return ""
    try:
        Path(target).unlink(missing_ok=True)
        return ""
    except Exception as e:
        return f"未能清理备份文件 {Path(target).name}：{e}"


def _publish_backup_archive(staged, target):
    identity = staged.stat()
    if target.exists() or target.is_symlink():
        raise RuntimeError("备份目标已存在，未覆盖原归档")
    try:
        if os.name == "nt":
            os.rename(staged, target)
        else:
            os.link(staged, target, follow_symlinks=False)
    except OSError as exc:
        try:
            actual = target.lstat()
        except FileNotFoundError:
            raise exc
        except OSError as check_error:
            raise BackupArchivePublishUnknown(
                f"备份归档发布结果无法确认，请检查备份列表和日志：{exc}；{check_error}"
            ) from exc
        if not stat.S_ISREG(actual.st_mode) or not os.path.samestat(identity, actual):
            raise RuntimeError("备份目标已存在，未覆盖原归档") from exc
        warning = f"归档已发布，发布确认异常：{exc}"
    else:
        warning = ""

    try:
        staged.unlink(missing_ok=True)
    except OSError as exc:
        warning = "；".join(filter(None, (warning, f"归档已发布，但暂存文件清理失败：{exc}")))
    return warning


def _create_backup_worker(job_id, items, cancel_event):
    with BACKUP_JOB_LOCK:
        info = BACKUP_JOBS.get(job_id)

    if not info:
        return

    items = list(items or [])

    target = staged = staging_path = None
    published = False
    try:
        _update_backup_job(
            job_id,
            running=True,
            status="正在压缩",
            error="",
            staged_cleanup_pending=True,
            staged_cleanup_error="",
            updated_at=now_str(),
        )
        _check_backup_cancelled(cancel_event)
        if not items:
            raise RuntimeError("未选择备份内容")

        backup_dir()

        type_name = "all"

        if items == ["data"]:
            type_name = "config"
        elif items == ["scripts"]:
            type_name = "scripts"
        elif set(items) == {"data", "scripts"}:
            type_name = "all"

        filename = f"fls-backup-{type_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{job_id}.tar.gz"
        target = backup_safe_file(filename)
        staging_path = backup_dir() / _backup_staging_name(job_id)
        staging_fd = os.open(staging_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(staging_fd)
        staged = staging_path

        deps_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        deps_tmp.close()

        try:
            write_dependencies_file(deps_tmp.name, cancel_event)

            with tarfile.open(staged, "w:gz") as tar:
                if "data" in items and DATA_DIR.exists():
                    tar.add(
                        DATA_DIR,
                        arcname="data",
                        filter=_backup_tar_filter(cancel_event),
                    )

                if "scripts" in items and SCRIPT_DIR.exists():
                    tar.add(
                        SCRIPT_DIR,
                        arcname="scripts",
                        filter=_backup_tar_filter(cancel_event),
                    )

                if Path(deps_tmp.name).exists():
                    _check_backup_cancelled(cancel_event)
                    tar.add(deps_tmp.name, arcname="dependencies.txt")

        finally:
            try:
                os.remove(deps_tmp.name)
            except Exception:
                pass

        _check_backup_cancelled(cancel_event)
        archive_stat = staged.stat()

        with BACKUP_JOB_LOCK:
            if cancel_event.is_set():
                cancelled_after_finish = True
            else:
                cancelled_after_finish = False
                publication_warning = _publish_backup_archive(staged, target)
                published = True
                try:
                    staged_cleanup_pending = staged.exists()
                except OSError:
                    staged_cleanup_pending = True
                staged_cleanup_error = (
                    f"暂存归档仍存在，待查询时重试清理：{staged.name}"
                    if staged_cleanup_pending else ""
                )
                info.update({
                    "running": False,
                    "status": "已完成",
                    "filename": target.name,
                    "size": archive_stat.st_size,
                    "size_text": fmt_size(archive_stat.st_size),
                    "error": publication_warning,
                    "staged_cleanup_pending": staged_cleanup_pending,
                    "staged_cleanup_error": staged_cleanup_error,
                    "updated_at": now_str(),
                })
                try:
                    persist_backup_job(info)
                except Exception as exc:
                    try:
                        actual = _load_persisted_backup_job(job_id)
                    except Exception as check_error:
                        raise BackupArchivePublishUnknown(
                            f"完整归档已发布，但作业状态保存无法确认，请核对备份列表：{exc}；{check_error}"
                        ) from exc
                    if actual != _backup_job_snapshot(info):
                        raise BackupArchivePublishUnknown(
                            f"完整归档已发布，但作业状态与完成回执不一致，请核对备份列表：{exc}"
                        ) from exc
                    info["error"] = "；".join(filter(None, (publication_warning, f"备份已完成，状态保存确认异常：{exc}")))

        if cancelled_after_finish:
            raise BackupCancelled("备份任务已取消")

    except BackupCancelled:
        cleanup_error = _remove_backup_target(staged)
        _finish_backup_job(
            job_id,
            "已取消",
            error=cleanup_error,
            filename="",
            size=0,
            size_text="-",
            staged_cleanup_pending=bool(cleanup_error),
            staged_cleanup_error=cleanup_error,
        )
    except BackupTimeout as e:
        cleanup_error = _remove_backup_target(staged)
        error = str(e)
        if cleanup_error:
            error += "；" + cleanup_error
        _finish_backup_job(
            job_id,
            "超时",
            error=error,
            filename="",
            size=0,
            size_text="-",
            staged_cleanup_pending=bool(cleanup_error),
            staged_cleanup_error=cleanup_error,
        )
    except Exception as e:
        staged_cleanup_error = _remove_backup_target(staged)
        staged_cleanup_pending = bool(staged_cleanup_error)
        if staged is None and staging_path is not None:
            try:
                staged_cleanup_pending = staging_path.exists()
            except OSError:
                staged_cleanup_pending = True
            if staged_cleanup_pending:
                staged_cleanup_error = "同一请求的遗留暂存归档尚待安全回收"
        cleanup_error = staged_cleanup_error
        if published and not isinstance(e, BackupArchivePublishUnknown):
            cleanup_error = "；".join(filter(None, (cleanup_error, _remove_backup_target(target))))
        error = str(e)
        if cleanup_error:
            error += "；" + cleanup_error
        _finish_backup_job(
            job_id,
            "结果未知" if isinstance(e, BackupArchivePublishUnknown) else "失败",
            error=error,
            filename="",
            size=0,
            size_text="-",
            staged_cleanup_pending=staged_cleanup_pending,
            staged_cleanup_error=staged_cleanup_error,
        )
    finally:
        with BACKUP_JOB_LOCK:
            BACKUP_CANCEL_EVENTS.pop(job_id, None)


def create_backup_worker(job_id, items, cancel_event, reserved=False):
    if not reserved:
        begin_backup_activity()
    try:
        _create_backup_worker(job_id, items, cancel_event)
    finally:
        end_backup_activity()


def start_backup_job(items, *, job_id=None):
    job_id = job_id or uuid.uuid4().hex

    with BACKUP_JOB_LOCK:
        prune_completed_records(BACKUP_JOBS)
        for old_id in list(BACKUP_CANCEL_EVENTS):
            if old_id not in BACKUP_JOBS:
                BACKUP_CANCEL_EVENTS.pop(old_id, None)
        cancel_event = threading.Event()
        now = now_str()
        with BACKUP_ACTIVITY_CONDITION:
            try:
                if not _try_begin_backup_activity():
                    raise BackupJobStartError("正在恢复备份，请恢复结束后重试")
            except BackupJobStartError:
                raise
            except Exception as exc:
                raise BackupJobStartError("备份执行门禁无法确认，任务未启动") from exc
        BACKUP_JOBS[job_id] = {
            "id": job_id,
            "items": list(items or []),
            "type_text": backup_type_text(items),
            "running": True,
            "status": "准备中",
            "filename": "",
            "size": 0,
            "size_text": "-",
            "error": "",
            "staged_cleanup_pending": False,
            "staged_cleanup_error": "",
            "created_at": now,
            "updated_at": now,
        }
        BACKUP_CANCEL_EVENTS[job_id] = cancel_event
        try:
            persist_backup_job(BACKUP_JOBS[job_id])
        except Exception as e:
            BACKUP_JOBS.pop(job_id, None)
            BACKUP_CANCEL_EVENTS.pop(job_id, None)
            end_backup_activity()
            raise BackupJobStartError(
                f"备份状态保存失败，任务未启动：{e}"
            ) from e

        try:
            th = threading.Thread(
                target=create_backup_worker,
                args=(job_id, items, cancel_event, True),
                daemon=True,
                name=f"fls-backup-{job_id[:8]}",
            )
            th.start()
        except Exception as e:
            end_backup_activity()
            info = BACKUP_JOBS[job_id]
            info.update({
                "running": False,
                "status": "启动失败",
                "error": str(e),
                "staged_cleanup_pending": False,
                "staged_cleanup_error": "",
                "updated_at": now_str(),
            })
            BACKUP_CANCEL_EVENTS.pop(job_id, None)
            try:
                persist_backup_job(info)
            except Exception as persist_error:
                info["error"] += f"；启动失败状态保存失败：{persist_error}"
            raise BackupJobStartError(
                f"备份任务启动失败：{info['error']}",
                job_id=job_id,
            ) from e

    return job_id


# ============================================================
# 安全解压
# ============================================================

def _archive_member_target(base, name):
    base = Path(base).resolve()
    member_name = str(name or "").replace("\\", "/")

    if (
        member_name.startswith("/")
        or member_name.startswith("//")
        or (len(member_name) >= 2 and member_name[1] == ":" and member_name[0].isalpha())
    ):
        raise RuntimeError("备份文件包含非法路径")

    target = (base / member_name).resolve()

    try:
        target.relative_to(base)
    except ValueError:
        raise RuntimeError("备份文件包含非法路径")

    return target


def safe_extract_tar(tar, path):
    base = Path(path).resolve()
    members = tar.getmembers()
    if len(members) > MAX_BACKUP_ARCHIVE_ENTRIES:
        raise BackupLimitExceeded(
            "备份文件条目过多（最多 {} 个）".format(MAX_BACKUP_ARCHIVE_ENTRIES)
        )

    total_size = 0

    for member in members:
        _archive_member_target(base, member.name)

        if member.size < 0:
            raise RuntimeError("备份文件包含非法大小")
        total_size += member.size
        if total_size > MAX_BACKUP_EXTRACTED_BYTES:
            raise BackupLimitExceeded(
                "备份文件解压后超过允许大小（{} MiB）".format(
                    MAX_BACKUP_EXTRACTED_BYTES // MIB
                )
            )

        if member.issym() or member.islnk():
            raise RuntimeError("备份文件包含不安全链接")

        if not (member.isfile() or member.isdir()):
            raise RuntimeError("备份文件包含不支持的文件类型")

    try:
        tar.extractall(path, filter="data")
    except TypeError as e:
        if "filter" not in str(e):
            raise
        tar.extractall(path)


def safe_extract_zip(zip_obj, path):
    base = Path(path).resolve()
    members = zip_obj.infolist()
    if len(members) > MAX_BACKUP_ARCHIVE_ENTRIES:
        raise BackupLimitExceeded(
            "备份文件条目过多（最多 {} 个）".format(MAX_BACKUP_ARCHIVE_ENTRIES)
        )

    total_size = 0

    for member in members:
        _archive_member_target(base, member.filename)

        mode = (int(member.external_attr or 0) >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise RuntimeError("备份文件包含不安全链接或特殊文件")

        total_size += member.file_size
        if total_size > MAX_BACKUP_EXTRACTED_BYTES:
            raise BackupLimitExceeded(
                "备份文件解压后超过允许大小（{} MiB）".format(
                    MAX_BACKUP_EXTRACTED_BYTES // MIB
                )
            )

    zip_obj.extractall(path)


def extract_archive(file_path, dest_dir):
    file_path = Path(file_path)
    if file_path.stat().st_size > MAX_BACKUP_UPLOAD_BYTES:
        raise BackupLimitExceeded(
            "备份文件超过上传上限（{} MiB）".format(MAX_BACKUP_UPLOAD_BYTES // MIB)
        )
    name = file_path.name.lower()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if name.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if name.endswith((".tar.gz", ".tgz")) else "r:"

        with tarfile.open(file_path, mode) as tar:
            safe_extract_tar(tar, dest_dir)

        return True

    if name.endswith(".zip"):
        with zipfile.ZipFile(file_path, "r") as z:
            safe_extract_zip(z, dest_dir)

        return True

    raise RuntimeError("不支持的备份格式，仅支持 tar.gz / tgz / tar / zip")


def find_backup_root(root):
    root = Path(root)
    candidates = []

    all_dirs = [root]

    try:
        all_dirs += [x for x in root.rglob("*") if x.is_dir()]
    except Exception:
        pass

    for p in all_dirs:
        if (p / "data").exists() or (p / "scripts").exists():
            candidates.append(p)

    if not candidates:
        return None

    candidates.sort(key=lambda x: len(str(x)))
    return candidates[0]


def find_dependency_file(extract_dir, backup_root=None):
    candidates = []
    extract_dir = Path(extract_dir)

    if backup_root:
        backup_root = Path(backup_root)
        candidates.extend([
            backup_root / "dependencies.txt",
            backup_root / "requirements.txt",
            backup_root / "data" / "dependencies.txt",
            backup_root / "data" / "requirements.txt",
        ])

    candidates.extend([
        extract_dir / "dependencies.txt",
        extract_dir / "requirements.txt",
        extract_dir / "data" / "dependencies.txt",
        extract_dir / "data" / "requirements.txt",
    ])

    for item in candidates:
        try:
            if item.exists() and item.is_file() and item.stat().st_size > 0:
                return item
        except Exception:
            pass

    try:
        for item in extract_dir.rglob("*"):
            if item.is_file() and item.name in ("dependencies.txt", "requirements.txt"):
                if item.stat().st_size > 0:
                    return item
    except Exception:
        pass

    return None


def install_dependencies(dep_file):
    dep_file = Path(dep_file)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = LOG_DIR / "backup-restore-deps-{}.log".format(
        datetime.now().strftime("%Y%m%d-%H%M%S")
    )

    with open(log_file, "ab", buffering=0) as log_fp:
        header = (
            "===== 备份恢复依赖 =====\n"
            "时间: {}\n"
            "Python: {}\n"
            "依赖列表: {}\n"
            "命令: {} -m pip install -r {}\n"
            "============================================================\n"
        ).format(now_str(), sys.executable, dep_file, sys.executable, dep_file)

        log_fp.write(header.encode("utf-8"))

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(dep_file)],
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                cwd=str(BASE_DIR),
                env=os.environ.copy(),
                timeout=1800,
            )

            footer = "\n===== 依赖恢复结束：{}，退出码：{} =====\n".format(
                now_str(),
                proc.returncode,
            )

            log_fp.write(footer.encode("utf-8"))

            return proc.returncode == 0, str(log_file)

        except Exception as e:
            log_fp.write(("\n依赖恢复异常: {}\n".format(e)).encode("utf-8"))
            return False, str(log_file)


# ============================================================
# 页面
# ============================================================

def backup_rows_html():
    try:
        files = list_backup_files()
    except Exception:
        return empty_table_row(4, "备份列表无法读取，请检查目录权限后刷新列表")

    if not files:
        return empty_table_row(4, "暂无备份", '<a class="btn btn-primary" href="#backup-create">创建备份</a>')

    rows = ""

    for item in files:
        name = item.get("name", "")

        rows += f"""
<tr>
    <td>
        <b>{h(name)}</b>
    </td>
    <td>{h(item.get("size_text", "-"))}</td>
    <td>{h(item.get("mtime_text", "-"))}</td>
    <td>
        <div class="row-actions" aria-label="备份 {h(name)} 行操作">
            <div class="row-actions-primary">
                <a class="btn btn-primary" href="/backup/download/{h(quote(name, safe=''))}">下载备份</a>
            </div>
            <div class="row-actions-danger">
                <button class="btn btn-red" type="button" data-filename="{h(name)}" data-version="{h(item['version'])}" onclick="flsDeleteBackup(this.dataset.filename, this.dataset.version, this)">删除备份</button>
                <noscript><form method="post" action="/backup/delete">
                    <input type="hidden" name="filename" value="{h(name)}">
                    <input type="hidden" name="version" value="{h(item['version'])}">
                    <button class="btn btn-red" type="submit">确认删除备份</button>
                </form></noscript>
            </div>
        </div>
    </td>
</tr>
"""

    return rows


# ============================================================
# API：创建 / 状态 / 列表 / 删除
# ============================================================


# ============================================================
# 下载 / 导入
# ============================================================
