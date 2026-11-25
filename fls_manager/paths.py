import os
from pathlib import Path

def detect_base_dir():
    env_dir = os.environ.get("FLS_BASE_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    if os.name == "nt":
        return Path("C:/fls")

    home = Path.home()
    prefix = os.environ.get("PREFIX", "")
    termux_version = os.environ.get("TERMUX_VERSION", "")

    is_termux = (
        "com.termux" in prefix
        or bool(termux_version)
        or str(home).startswith("/data/data/com.termux")
    )

    if is_termux:
        return (home / "fls").resolve()

    default_root = Path("/root/fls")
    try:
        default_root.mkdir(parents=True, exist_ok=True)
        return default_root.resolve()
    except Exception:
        return (home / "fls").resolve()

BASE_DIR = detect_base_dir()
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "log"
SCRIPT_DIR = BASE_DIR / "scripts"

CONFIG_FILE = DATA_DIR / "config.json"
CONFIG_APPLY_STATE_FILE = DATA_DIR / "config_apply_state.json"
TASK_FILE = DATA_DIR / "tasks.json"
TASK_HISTORY_FILE = DATA_DIR / "task_history.json"
TASK_HISTORY_INDEX_FILE = DATA_DIR / "task_history_index.sqlite3"
GLOBAL_ENV_FILE = DATA_DIR / "global_env.json"
PROXY_FILE = DATA_DIR / "proxies.json"
COLLECTION_FILE = DATA_DIR / "collections.json"
SCHEDULER_STATE_FILE = DATA_DIR / "scheduler_state.json"
NOTIFY_HISTORY_FILE = DATA_DIR / "notify_history.json"
PROXY_DIAGNOSTICS_FILE = DATA_DIR / "proxy_diagnostics.json"
SCRIPT_DEBUG_RUNS_FILE = DATA_DIR / "script_debug_runs.json"
SCRIPT_PULL_JOBS_FILE = DATA_DIR / "script_pull_jobs.json"
SCRIPT_REPO_PULL_JOBS_FILE = DATA_DIR / "script_repo_pull_jobs.json"
SCRIPT_IMPORT_JOBS_FILE = DATA_DIR / "script_import_jobs.json"
SCRIPT_RENAME_FILE = DATA_DIR / "script_renames.json"
PID_FILE = DATA_DIR / "fls-manager.pid"

for d in (BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR):
    d.mkdir(parents=True, exist_ok=True)
