import json
import threading
import os

from ..paths import DATA_DIR
from ..storage import read_json, mutate_json
from ..utils import now_str

DEFAULT_ONLINE_SCRIPT_SOURCE = "https://cdn.jsdelivr.net/gh/liyw0205/fls-scripts@main/index.json"
ONLINE_SCRIPT_CACHE_FILE = DATA_DIR / "online_scripts_cache.json"

ONLINE_INSTALL_RUNNING = {}
ONLINE_INSTALL_STOPPING = set()
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
    "updated_at": "",
    "log_file": "",
}


def _refresh_snapshot(info):
    fields = ("id", "running", "status", "message", "error", "updated_at", "log_file")
    return {field: info.get(field, "") for field in fields}


def persist_online_refresh(info):
    snapshot = _refresh_snapshot(info)
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
        ONLINE_REFRESH_STATE.update(updates)
        persist_online_refresh(ONLINE_REFRESH_STATE)
        return dict(ONLINE_REFRESH_STATE)


def get_online_refresh_state():
    with ONLINE_REFRESH_LOCK:
        current = dict(ONLINE_REFRESH_STATE)
        loaded_from_file = False
        if not current.get("id"):
            data = read_json(ONLINE_REFRESH_STATE_FILE, {})
            if isinstance(data, dict):
                current.update(_refresh_snapshot(data))
                loaded_from_file = bool(data)
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
        "running",
        "status",
        "start_time",
        "returncode",
        "error",
        "updated_at",
    )
    return {field: info.get(field) for field in fields}


def persist_online_install(info):
    with ONLINE_INSTALL_LOCK:
        snapshot = _install_snapshot(info)
        install_id = str(snapshot.get("id") or "")
        if not install_id:
            return

        def update(data):
            jobs = data if isinstance(data, dict) else {}
            jobs = {
                str(key): value
                for key, value in jobs.items()
                if isinstance(value, dict)
            }
            jobs.pop(install_id, None)
            jobs = {install_id: snapshot, **jobs}
            ordered = sorted(
                jobs.items(),
                key=lambda pair: str(pair[1].get("updated_at") or ""),
                reverse=True,
            )
            return dict(ordered[:ONLINE_INSTALL_LIMIT])

        mutate_json(ONLINE_INSTALL_JOBS_FILE, {}, update)


def _load_persisted_online_install(install_id):
    data = read_json(ONLINE_INSTALL_JOBS_FILE, {})
    jobs = data if isinstance(data, dict) else {}
    info = jobs.get(str(install_id or ""))
    return dict(info) if isinstance(info, dict) else None


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
            persist_online_install(info)

        return info


def active_online_install_for_script(script_id):
    script_id = str(script_id or "")
    with ONLINE_INSTALL_LOCK:
        for install_id, info in ONLINE_INSTALL_RUNNING.items():
            if info.get("script_id") == script_id and info.get("running"):
                return install_id, dict(info)
    return "", None
