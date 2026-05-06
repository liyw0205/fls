from ..paths import DATA_DIR

DEFAULT_ONLINE_SCRIPT_SOURCE = "https://cdn.jsdelivr.net/gh/liyw0205/fls-scripts@main/index.json"
ONLINE_SCRIPT_CACHE_FILE = DATA_DIR / "online_scripts_cache.json"

ONLINE_INSTALL_RUNNING = {}
ONLINE_INSTALL_STOPPING = set()

ONLINE_REFRESH_STATE = {
    "running": False,
    "message": "",
    "error": "",
    "updated_at": "",
    "log_file": "",
}