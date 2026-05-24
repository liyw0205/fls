from .bp import bp
from datetime import datetime
from math import ceil
from urllib.parse import quote

from flask import abort, redirect, url_for, request, Response, jsonify

from ...paths import LOG_DIR
from ...logs import parse_task_name_from_log, tail_file
from ...utils import h, get_back_url
from ...ui.layout import layout
from ...ui.log_controls import log_controls

def page_links(base, q, page, pages):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"{base}?page={int(p)}"

        if q:
            url += "&q=" + quote(q)

        return url

    def page_btn(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"
        return f'<a class="btn {cls}" href="{h(build_url(p))}">{h(text)}</a>'

    page = max(1, min(int(page), int(pages)))

    items = []

    # 上一页
    items.append(
        page_btn(page - 1, "上一页", disabled=(page <= 1))
    )

    # 始终显示 1、最后一页、当前页前后 2 页
    show = {1, pages}

    for p in range(page - 2, page + 3):
        if 1 <= p <= pages:
            show.add(p)

    show = sorted(show)

    last = 0

    for p in show:
        if last and p - last > 1:
            items.append('<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>')

        items.append(
            page_btn(p, active=(p == page))
        )

        last = p

    # 下一页
    items.append(
        page_btn(page + 1, "下一页", disabled=(page >= pages))
    )

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            第 <b>{page}</b> / <b>{pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
</div>
"""


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
