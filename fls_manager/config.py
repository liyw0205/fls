import os
import shutil
from .paths import CONFIG_FILE
from .storage import read_json, write_json
from .constants import DEFAULT_HOST, DEFAULT_PORT, LOG_CLEANUP_INTERVAL_MINUTES, LOG_MAX_SIZE_MB, LOG_KEEP_PER_TASK

DEFAULT_CONFIG = {
    "admin_token": "",
    "port": DEFAULT_PORT,
    "log_cleanup_minutes": LOG_CLEANUP_INTERVAL_MINUTES,
    "log_max_size_mb": LOG_MAX_SIZE_MB,
    "log_keep_per_task": LOG_KEEP_PER_TASK,

    # 任务超时时间，单位秒；0 表示关闭
    "task_timeout_seconds": 1800,

    # 全局随机延迟，单位秒；0 表示不启用；启用范围 1-120
    "random_delay_seconds": 0,
    
    "online_script_source": "https://cdn.jsdelivr.net/gh/liyw0205/fls-scripts@main/index.json",

    "task_types": {
        "py": True,
        "sh": True,
        "js": bool(shutil.which("node")),
        "ts": bool(shutil.which("tsx") or shutil.which("ts-node")),
        "ps1": False,
        "bat": False,
        "php": bool(shutil.which("php")),
        "rb": bool(shutil.which("ruby")),
        "pl": bool(shutil.which("perl")),
        "lua": bool(shutil.which("lua")),
        "jar": bool(shutil.which("java")),
    },
}

def load_config():
    cfg = read_json(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}

    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg)

    types = DEFAULT_CONFIG["task_types"].copy()
    if isinstance(cfg.get("task_types"), dict):
        types.update(cfg.get("task_types"))
    merged["task_types"] = types

    try:
        merged["port"] = max(1, min(65535, int(merged.get("port", DEFAULT_PORT))))
    except Exception:
        merged["port"] = DEFAULT_PORT

    try:
        merged["task_timeout_seconds"] = max(
            0,
            int(merged.get("task_timeout_seconds", 1800) or 0)
        )
    except Exception:
        merged["task_timeout_seconds"] = 1800

    try:
        delay = int(merged.get("random_delay_seconds", 0) or 0)
        if delay <= 0:
            delay = 0
        else:
            delay = max(1, min(120, delay))
        merged["random_delay_seconds"] = delay
    except Exception:
        merged["random_delay_seconds"] = 0

    return merged

def save_config(cfg):
    base = load_config()
    base.update(cfg or {})
    write_json(CONFIG_FILE, base)

def get_host():
    return os.environ.get("FLS_HOST", DEFAULT_HOST)

def get_port():
    env_port = os.environ.get("FLS_PORT", "").strip()
    if env_port:
        return max(1, min(65535, int(env_port)))
    return int(load_config().get("port", DEFAULT_PORT))

def fls_get_admin_token():
    env_token = os.environ.get("FLS_TOKEN", "").strip()
    if env_token:
        return env_token
    return str(load_config().get("admin_token", "") or "").strip()

def task_type_enabled(suffix):
    suffix = str(suffix or "").lower().lstrip(".")
    return bool(load_config().get("task_types", {}).get(suffix, False))
