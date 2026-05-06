from ..paths import LOG_DIR
from ..utils import safe_name


def online_refresh_log_file(refresh_id):
    return LOG_DIR / f"online-script-source-refresh-{refresh_id}.log"


def online_install_log_file(install_id, script_name):
    safe_pkg = safe_name(script_name or "online-script")
    return LOG_DIR / f"online-script-install-{safe_pkg}-{install_id}.log"


def append_log(log_file, text):
    try:
        with open(log_file, "ab") as f:
            f.write(str(text).encode("utf-8", errors="replace"))

            if not str(text).endswith("\n"):
                f.write(b"\n")

    except Exception:
        pass