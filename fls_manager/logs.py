import os
import re
from pathlib import Path
from datetime import datetime
from .paths import LOG_DIR
from .utils import safe_name
from .config import load_config
from .sensitive import mask_sensitive_text

def log_file_for_task(task):
    display_name = task.get("name") or Path(task.get("command", "task")).stem or "未命名任务"
    name = safe_name(display_name)
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    return LOG_DIR / f"{name}-{ts}.log"

def latest_log_for_task(task):
    display_name = task.get("name") or Path(task.get("command", "task")).stem or "未命名任务"
    name = safe_name(display_name)
    files = sorted(
        LOG_DIR.glob(f"{name}-*.log"),
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )
    return str(files[0]) if files else ""

def tail_file(file_path, lines=800):
    if not file_path or not Path(file_path).exists():
        return "暂无日志"
    p = Path(file_path)
    try:
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
            text = data.decode("utf-8", errors="replace")
            return mask_sensitive_text("\n".join(text.splitlines()[-lines:]))
    except Exception as e:
        return f"读取日志失败: {e}"

def parse_task_name_from_log(file_path):
    try:
        with open(file_path, "rb") as f:
            data = f.read(8192).decode("utf-8", errors="ignore")
        m = re.search(r"===== 启动任务:\s*(.*?)\s*=====", data)
        if m:
            return m.group(1).strip() or None
    except Exception:
        pass
    return None

def cleanup_logs():
    cfg = load_config()
    keep = int(cfg.get("log_keep_per_task", 10))
    max_bytes = int(cfg.get("log_max_size_mb", 10)) * 1024 * 1024

    files = [f for f in LOG_DIR.glob("*.log") if f.is_file()]

    for f in list(files):
        try:
            if f.stat().st_size > max_bytes:
                f.unlink()
        except Exception:
            pass

    groups = {}
    for f in LOG_DIR.glob("*.log"):
        key = parse_task_name_from_log(f) or "其他日志"
        groups.setdefault(key, []).append(f)

    for _, fs in groups.items():
        fs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        for old in fs[keep:]:
            try:
                old.unlink()
            except Exception:
                pass
