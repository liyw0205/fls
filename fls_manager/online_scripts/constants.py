import json
import threading
import os

from ..paths import DATA_DIR
from ..storage import mutate_json, storage_transaction
from ..utils import now_str

DEFAULT_ONLINE_SCRIPT_SOURCE = "https://cdn.jsdelivr.net/gh/liyw0205/fls-scripts@main/index.json"
ONLINE_SCRIPT_CACHE_FILE = DATA_DIR / "online_scripts_cache.json"

ONLINE_INSTALL_RUNNING = {}
ONLINE_INSTALL_STOPPING = set()
ONLINE_INSTALL_RETRYING = set()
ONLINE_INSTALL_JOBS_FILE = DATA_DIR / "online_install_jobs.json"
ONLINE_INSTALL_LOCK = threading.RLock()
ONLINE_INSTALL_LIMIT = 100
ONLINE_INSTALL_TIMEOUT_SECONDS = 1800
ONLINE_REFRESH_STATE_FILE = DATA_DIR / "online_script_refresh.json"
ONLINE_REFRESH_LOCK = threading.RLock()

ONLINE_REFRESH_STATE = {
    "id": "",
    "running": False,
    "status": "",
    "message": "",
    "error": "",
    "cache_published": False,
    "updated_at": "",
    "log_file": "",
}


def _refresh_snapshot(info):
    fields = (
        "id", "running", "status", "message", "error", "cache_published",
        "updated_at", "log_file",
    )
    return {
        field: info.get(field, False if field in ("running", "cache_published") else "")
        for field in fields
    }


def _validate_refresh_snapshot(info):
    snapshot = _refresh_snapshot(info)
    if (
        type(snapshot["running"]) is not bool
        or type(snapshot["cache_published"]) is not bool
        or any(not isinstance(snapshot[field], str) for field in ("id", "status", "message", "error", "updated_at", "log_file"))
    ):
        raise RuntimeError("在线脚本源同步状态损坏，未覆盖原文件")
    return snapshot


def _read_online_refresh_file():
    with storage_transaction():
        try:
            raw = ONLINE_REFRESH_STATE_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"在线脚本源同步状态损坏，未覆盖原文件：{exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("在线脚本源同步状态损坏，未覆盖原文件：根节点必须是对象")
        return _validate_refresh_snapshot(data)


def persist_online_refresh(info):
    snapshot = _validate_refresh_snapshot(info)
    with storage_transaction():
        if ONLINE_REFRESH_STATE_FILE.is_symlink():
            raise RuntimeError("在线脚本源同步状态文件不能是符号链接")
        _read_online_refresh_file()
        ONLINE_REFRESH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = ONLINE_REFRESH_STATE_FILE.with_name(
            f".{ONLINE_REFRESH_STATE_FILE.name}.{threading.get_ident()}.tmp"
        )
        try:
            temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, ONLINE_REFRESH_STATE_FILE)
        finally:
            temp.unlink(missing_ok=True)


def update_online_refresh_state(**updates):
    with ONLINE_REFRESH_LOCK:
        previous = dict(ONLINE_REFRESH_STATE)
        ONLINE_REFRESH_STATE.update(updates)
        try:
            persist_online_refresh(ONLINE_REFRESH_STATE)
        except Exception:
            ONLINE_REFRESH_STATE.clear()
            ONLINE_REFRESH_STATE.update(previous)
            raise
        return dict(ONLINE_REFRESH_STATE)


def get_online_refresh_state():
    with ONLINE_REFRESH_LOCK:
        current = dict(ONLINE_REFRESH_STATE)
        loaded_from_file = False
        if ONLINE_REFRESH_STATE_FILE.exists():
            data = _read_online_refresh_file()
            if data and not current.get("id"):
                current.update(data)
                loaded_from_file = True
        if loaded_from_file and current.get("running"):
            current["running"] = False
            current["status"] = "结果未知"
            current["error"] = "面板重启后无法确认在线脚本源同步是否完成；保留旧缓存，请查看刷新日志后重试。"
            current["message"] = ""
            current["updated_at"] = now_str()
            ONLINE_REFRESH_STATE.update(current)
            persist_online_refresh(current)
        return current


def reserve_online_refresh(proxy_id=""):
    with ONLINE_REFRESH_LOCK:
        current = get_online_refresh_state()
        if current.get("running"):
            return None
        import uuid
        previous = dict(ONLINE_REFRESH_STATE)
        info = {
            "id": uuid.uuid4().hex,
            "running": True,
            "status": "运行中",
            "message": "正在后台同步在线脚本源，请稍候...",
            "error": "",
            "cache_published": False,
            "updated_at": now_str(),
            "log_file": "",
            "proxy_id": proxy_id,
        }
        ONLINE_REFRESH_STATE.update(info)
        try:
            persist_online_refresh(info)
        except Exception:
            ONLINE_REFRESH_STATE.clear()
            ONLINE_REFRESH_STATE.update(previous)
            raise
        return dict(info)


def _install_snapshot(info):
    fields = (
        "id",
        "script_id",
        "script_name",
        "log_file",
        "proxy_id",
        "import_task",
        "enable_task",
        "selected_task_indexes",
        "selected_tasks",
        "force",
        "retry_of",
        "retried_by",
        "running",
        "status",
        "start_time",
        "returncode",
        "error",
        "scheduler_reload_pending",
        "scheduler_reload_error",
        "updated_at",
    )
    return {field: info.get(field) for field in fields}


def _read_install_jobs():
    """Read install history without treating corruption as an empty history."""
    with storage_transaction():
        try:
            raw = ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"在线脚本安装记录损坏，未覆盖原文件：{exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：根节点必须是对象")
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, dict) or value.get("id") != key:
                raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：记录标识不一致")
        return data


def persist_online_install(info):
    with ONLINE_INSTALL_LOCK:
        snapshot = _install_snapshot(info)
        install_id = str(snapshot.get("id") or "")
        if not install_id:
            return

        def update(data):
            if not isinstance(data, dict):
                raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：根节点必须是对象")
            jobs = {}
            for key, value in data.items():
                if not isinstance(key, str) or not isinstance(value, dict) or value.get("id") != key:
                    raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：记录标识不一致")
                jobs[key] = value
            retry_of = str(snapshot.get("retry_of") or "")
            previous = jobs.get(retry_of)
            if retry_of and isinstance(previous, dict):
                previous = dict(previous)
                previous["retried_by"] = install_id
                jobs[retry_of] = previous
            jobs.pop(install_id, None)
            jobs = {install_id: snapshot, **jobs}
            ordered = sorted(
                jobs.items(),
                key=lambda pair: str(pair[1].get("updated_at") or ""),
                reverse=True,
            )
            return dict(ordered[:ONLINE_INSTALL_LIMIT])

        mutate_json(ONLINE_INSTALL_JOBS_FILE, {}, update, strict=True)
        previous = ONLINE_INSTALL_RUNNING.get(str(snapshot.get("retry_of") or ""))
        if isinstance(previous, dict):
            previous["retried_by"] = install_id


def _load_persisted_online_install(install_id):
    jobs = _read_install_jobs()
    info = jobs.get(str(install_id or ""))
    return dict(info) if isinstance(info, dict) else None


def _load_persisted_online_installs():
    jobs = _read_install_jobs()
    return {
        str(install_id): dict(info)
        for install_id, info in jobs.items()
        if isinstance(info, dict)
    }


def get_online_install_job(install_id):
    """Return a live or persisted install, marking lost processes as unknown."""
    with ONLINE_INSTALL_LOCK:
        info = ONLINE_INSTALL_RUNNING.get(install_id)
        if isinstance(info, dict):
            return dict(info)

        info = _load_persisted_online_install(install_id)
        if not info:
            return None

        if info.get("running"):
            info["running"] = False
            info["status"] = "结果未知"
            info["returncode"] = None
            restart_error = (
                "面板重启后无法确认在线脚本安装进程是否完成，请查看安装日志和脚本目录；"
                "确认状态后再决定是否重试。"
            )
            info["error"] = "；".join(filter(None, (info.get("error"), restart_error)))
            info["updated_at"] = now_str()
            try:
                persist_online_install(info)
            except Exception as e:
                info["error"] += f"；保存恢复状态失败：{e}，请修复文件权限或磁盘空间后重试。"

        return info


def active_online_install_for_script(script_id):
    script_id = str(script_id or "")
    with ONLINE_INSTALL_LOCK:
        for install_id, info in ONLINE_INSTALL_RUNNING.items():
            if info.get("script_id") == script_id and info.get("running"):
                return install_id, dict(info)
    return "", None


def latest_online_install_for_script(script_id):
    """Return the newest install record, including a process lost on restart."""
    script_id = str(script_id or "")
    if not script_id:
        return "", None

    with ONLINE_INSTALL_LOCK:
        jobs = _load_persisted_online_installs()
        for install_id, info in ONLINE_INSTALL_RUNNING.items():
            if isinstance(info, dict):
                jobs[str(install_id)] = dict(info)

        matches = []
        for install_id, info in jobs.items():
            if str(info.get("script_id") or "") != script_id:
                continue
            if install_id not in ONLINE_INSTALL_RUNNING and info.get("running"):
                info = get_online_install_job(install_id) or info
            matches.append((install_id, info))

        if not matches:
            return "", None

        def order(pair):
            try:
                started = float(pair[1].get("start_time") or 0)
            except (ValueError, TypeError):
                started = 0
            return started, str(pair[1].get("updated_at") or "")

        # Recovery changes updated_at, but must not promote an older attempt.
        matches.sort(key=order, reverse=True)
        install_id, info = matches[0]
        return install_id, dict(info)
