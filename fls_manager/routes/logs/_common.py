from .bp import bp
import hashlib
import stat
from datetime import datetime
from math import ceil
from urllib.parse import quote

from flask import abort, redirect, url_for, request, Response, jsonify

from ...paths import LOG_DIR
from ...logs import parse_task_name_from_log, tail_file
from ...utils import h, get_back_url
from ...ui.layout import layout
from ...ui.log_controls import log_controls
from ...ui.components import pagination_card, page_header, empty_state, data_toolbar


def safe_log_file(filename):
    filename = str(filename or "").split("/")[-1].split("\\")[-1]
    file_path = LOG_DIR / filename

    try:
        resolved = file_path.resolve()
        log_dir = LOG_DIR.resolve()
    except OSError:
        abort(404)

    if resolved.parent != log_dir or not resolved.is_file():
        abort(404)

    return resolved


def log_file_version(file_path):
    """Return an identity bound to one regular log file, not just its name."""
    info = file_path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("日志文件不是常规文件")
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return hashlib.sha256(repr(identity).encode("ascii")).hexdigest()


def log_group_version(log_files):
    entries = []
    for file_path in sorted(log_files, key=lambda item: item.name):
        entries.append((file_path.name, log_file_version(file_path)))
    return hashlib.sha256(repr(entries).encode("utf-8")).hexdigest()


def page_links(base, q, page, pages):
    def build_url(p):
        url = f"{base}?page={int(p)}"

        if q:
            url += "&q=" + quote(q)

        return url

    return pagination_card(page, pages, href_for=build_url)


def log_group_title(task_name, count):
    if task_name == "其他日志":
        title = "其他日志"
    else:
        title = f"任务：{task_name}"

    if int(count or 0) > 1:
        title += f"（{int(count)}）"

    return title


def log_file_group_name(file_path):
    name = file_path.name

    if (
        name.startswith("deps-install-")
        or name.startswith("system-install-")
        or name.startswith("backup-restore-deps-")
        or name.startswith("fls-manager")
    ):
        return "其他日志"

    return parse_task_name_from_log(file_path) or "其他日志"


def load_log_groups():
    files = sorted(
        [f for f in LOG_DIR.glob("*.log") if f.is_file()],
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )

    groups = {}

    for f in files:
        key = log_file_group_name(f)
        groups.setdefault(key, []).append(f)

    return groups
