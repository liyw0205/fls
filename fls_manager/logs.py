import os
import re
from pathlib import Path
from datetime import datetime
from .paths import LOG_DIR
from .utils import safe_name
from .config import load_config
from .sensitive import mask_sensitive_text
from .log_access import LogFileBusy, delete_log_file, file_version, open_log_file

MAX_TAIL_LINES = 5000
MAX_TAIL_BYTES = 1024 * 1024
TAIL_TRUNCATION_NOTICE = "===== 日志过长，已省略超出读取范围的内容 ====="

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

    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = 800

    lines = max(1, min(lines, MAX_TAIL_LINES))

    p = Path(file_path)
    try:
        with open_log_file(p, "rb", legacy_root=True) as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            chunks = []
            read_bytes = 0
            newline_count = 0
            while size > 0 and newline_count <= lines and read_bytes < MAX_TAIL_BYTES:
                step = min(block, size, MAX_TAIL_BYTES - read_bytes)
                size -= step
                f.seek(size)
                chunk = f.read(step)
                chunks.append(chunk)
                read_bytes += step
                newline_count += chunk.count(b"\n")
            truncated = size > 0 and newline_count <= lines
            text_lines = b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()
            if truncated:
                # Without the line prefix, a sensitive value cannot be reliably masked.
                text_lines = text_lines[1:]
            text = mask_sensitive_text("\n".join(text_lines[-lines:]))
            if truncated:
                return TAIL_TRUNCATION_NOTICE + ("\n" + text if text else "")
            return text
    except LogFileBusy:
        raise
    except Exception as e:
        return f"读取日志失败: {e}"

def parse_task_name_from_log(file_path, *, strict=False):
    try:
        with open_log_file(file_path, "rb") as f:
            data = f.read(8192).decode("utf-8", errors="ignore")
        m = re.search(r"===== 启动任务:\s*(.*?)\s*=====", data)
        if m:
            return m.group(1).strip() or None
    except Exception:
        if strict:
            raise
    return None

def cleanup_logs():
    cfg = load_config()
    keep = int(cfg.get("log_keep_per_task", 10))
    max_bytes = int(cfg.get("log_max_size_mb", 10)) * 1024 * 1024

    counts = dict.fromkeys(("deleted", "missing", "busy", "conflict", "failed", "unknown"), 0)

    def remove(path, version):
        try:
            outcome, _, _ = delete_log_file(path, version)
        except Exception:
            outcome = "failed"
        counts[outcome] += 1

    files = []
    for path in list(LOG_DIR.glob("*.log")):
        try:
            info = path.lstat()
            version = file_version(info)
        except FileNotFoundError:
            counts["missing"] += 1
            continue
        except Exception:
            counts["failed"] += 1
            continue
        files.append((path, version, info.st_mtime_ns, info.st_size))

    groups = {}
    for entry in files:
        if entry[3] > max_bytes:
            remove(entry[0], entry[1])
            continue
        try:
            key = parse_task_name_from_log(entry[0], strict=True) or "其他日志"
        except FileNotFoundError:
            counts["missing"] += 1
            continue
        except LogFileBusy:
            counts["busy"] += 1
            continue
        except Exception:
            counts["failed"] += 1
            continue
        groups.setdefault(key, []).append(entry)

    # Keep one snapshot per path; changed or newly created files wait for the next cycle.
    for entries in groups.values():
        entries.sort(key=lambda entry: (entry[2], entry[0].name), reverse=True)
        for path, version, _, _ in entries[keep:]:
            remove(path, version)
    return counts
