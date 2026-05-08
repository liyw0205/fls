import os
import shutil
from datetime import datetime, timezone, timedelta
from .paths import CONFIG_FILE
from .storage import read_json, write_json
from .constants import DEFAULT_HOST, DEFAULT_PORT, LOG_CLEANUP_INTERVAL_MINUTES, LOG_MAX_SIZE_MB, LOG_KEEP_PER_TASK

DEFAULT_CONFIG = {
    "admin_token": "",
    # 登录后安全验证
    # security_verify_type:
    #   code = 随机验证码
    #   totp = 2FA / TOTP
    "security_verify_enabled": False,
    "security_verify_type": "code",
    "totp_secret": "",
    "port": DEFAULT_PORT,
    "log_cleanup_minutes": LOG_CLEANUP_INTERVAL_MINUTES,
    "log_max_size_mb": LOG_MAX_SIZE_MB,
    "log_keep_per_task": LOG_KEEP_PER_TASK,

    # 任务超时时间，单位秒；0 表示关闭
    "task_timeout_seconds": 1800,

    # 全局随机延迟，单位秒；0 表示不启用；启用范围 1-120
    "random_delay_seconds": 0,
    
    # 面板虚拟时区，默认 UTC+8
    "timezone_offset_hours": 8,

    # 面板虚拟时间偏移秒数。
    # panel_now = 系统当前时间 + panel_time_offset_seconds
    # 用于校准 Cron。
    "panel_time_offset_seconds": 0,
    
    "online_script_source": "https://raw.githubusercontent.com/liyw0205/fls-scripts/main/index.json",

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

def get_timezone_offset_hours():
    cfg = load_config()

    try:
        offset = int(cfg.get("timezone_offset_hours", 8) or 8)
    except Exception:
        offset = 8

    return max(-24, min(24, offset))


def get_panel_time_offset_seconds():
    cfg = load_config()

    try:
        return int(cfg.get("panel_time_offset_seconds", 0) or 0)
    except Exception:
        return 0


def get_panel_timezone():
    offset = get_timezone_offset_hours()

    return timezone(
        timedelta(hours=offset),
        name=f"UTC{offset:+d}",
    )


def get_panel_timezone_text():
    offset = get_timezone_offset_hours()
    return f"UTC{offset:+d}"


def panel_now():
    """
    返回 FLS 面板虚拟当前时间。

    不修改系统时间，不需要 root。
    """
    tz = get_panel_timezone()
    delta = get_panel_time_offset_seconds()

    return datetime.now(tz) + timedelta(seconds=delta)


def set_panel_time_calibration(offset_hours=8, virtual_now=None):
    """
    设置面板虚拟时区和虚拟当前时间。

    offset_hours:
        UTC 偏移，例如 8 表示 UTC+8。

    virtual_now:
        带 tzinfo 的 datetime。
        表示希望 FLS 面板当前时间是多少。

    如果 virtual_now 为空：
        只设置时区，不设置时间偏移。
    """
    try:
        offset_hours = int(offset_hours)
    except Exception:
        offset_hours = 8

    offset_hours = max(-24, min(24, offset_hours))
    tz = timezone(timedelta(hours=offset_hours), name=f"UTC{offset_hours:+d}")

    if virtual_now is None:
        delta_seconds = 0
    else:
        if virtual_now.tzinfo is None:
            virtual_now = virtual_now.replace(tzinfo=tz)

        virtual_now = virtual_now.astimezone(tz)
        real_now = datetime.now(tz)
        delta_seconds = int((virtual_now - real_now).total_seconds())

    save_config({
        "timezone_offset_hours": offset_hours,
        "panel_time_offset_seconds": delta_seconds,
    })

    return {
        "timezone_offset_hours": offset_hours,
        "panel_time_offset_seconds": delta_seconds,
        "timezone_text": f"UTC{offset_hours:+d}",
    }


def reset_panel_time_calibration(offset_hours=8):
    """
    重置面板时间偏移，仅保留时区。
    """
    return set_panel_time_calibration(
        offset_hours=offset_hours,
        virtual_now=None,
    )
    