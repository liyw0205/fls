from .bp import bp
import json
import uuid
import time
from math import ceil
from urllib.parse import quote

import requests
from flask import Blueprint, request, redirect, url_for, abort, jsonify

from ...utils import h, get_back_url
from ...ui.layout import layout
from ...ui.log_controls import log_controls
from ...logs import tail_file
from ...proxy import (
    proxy_select_options,
    requests_proxy_dict,
    github_proxy_url,
)

from ...online_scripts.constants import (
    ONLINE_SCRIPT_CACHE_FILE,
    ONLINE_INSTALL_RUNNING,
    ONLINE_REFRESH_STATE,
)

from ...online_scripts.logs import online_install_log_file

from ...online_scripts.source import (
    get_online_script_source,
    normalize_online_scripts,
    load_online_script_cache,
    save_online_script_cache,
    read_cache_text,
    fetch_task_link_tasks,
    start_refresh_thread,
)

from ...online_scripts.tasks import (
    online_script_task_crons,
    online_task_cron_vars,
    guess_task_command,
    script_has_task,
    import_task_if_needed,
)

from ...online_scripts.install import (
    online_script_target,
    script_has_install,
    request_stop_online_install,
    start_install_thread,
)

from ...online_scripts.docs import (
    render_markdown_to_html,
    doc_url_looks_markdown,
    doc_content_looks_markdown,
    doc_response_is_html,
    doc_response_is_text,
)

from ...online_scripts.render import render_online_script_rows


def online_scripts_page_links(q, page, pages):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"/online-scripts?page={int(p)}"
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


def filter_online_scripts_for_page(items, q):
    q = str(q or "").strip().lower()

    if not q:
        return items

    result = []

    for item in items:
        task_names = []
        task_commands = []

        for task in online_script_task_crons(item):
            task_names.append(str(task.get("name") or ""))
            task_commands.append(str(task.get("command") or ""))

        fields = [
            item.get("id", ""),
            item.get("name", ""),
            item.get("type", ""),
            item.get("link", ""),
            item.get("link_name", ""),
            item.get("install", ""),
            item.get("doc_link", ""),
            item.get("task_link", ""),
            "\n".join(task_names),
            "\n".join(task_commands),
        ]

        text = "\n".join(str(x or "") for x in fields).lower()

        if q in text:
            result.append(item)

    return result

def get_online_script(script_id):
    for item in load_online_script_cache():
        if item.get("id") == script_id:
            return item

    return None


def parse_excluded_task_indexes(raw):
    result = set()

    for x in str(raw or "").replace("，", ",").split(","):
        x = x.strip()

        if not x:
            continue

        try:
            n = int(x)
            if n > 0:
                result.add(n)
        except Exception:
            pass

    return result


def selected_task_indexes_from_form(item):
    """
    从安装选择页表单中解析最终选择的任务。

    支持：
    1. 新版分页选择：
       select_mode=all
       excluded_task_indexes=1,3,5

    2. 旧版显式选择：
       task_indexes=1&task_indexes=2
    """
    selected_task_indexes = request.form.getlist("task_indexes")

    select_mode = request.form.get("select_mode", "").strip()
    excluded_task_indexes = request.form.get("excluded_task_indexes", "").strip()

    if select_mode == "all":
        all_count = len(online_script_task_crons(item))
        excluded_set = parse_excluded_task_indexes(excluded_task_indexes)

        selected_task_indexes = [
            str(i)
            for i in range(1, all_count + 1)
            if i not in excluded_set
        ]

    return selected_task_indexes

def install_task_page_links(script_id, task_page, task_pages, excluded="", task_q=""):
    if task_pages <= 1:
        return ""

    def page_btn(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"
        return (
            f'<button class="btn {cls}" type="button" '
            f'onclick="flsInstallGoTaskPage({int(p)})">{h(text)}</button>'
        )

    task_page = max(1, min(int(task_page), int(task_pages)))

    items = []

    # 上一页
    items.append(
        page_btn(task_page - 1, "上一页", disabled=(task_page <= 1))
    )

    # 始终显示 1、最后一页、当前页前后 2 页
    show = {1, task_pages}

    for p in range(task_page - 2, task_page + 3):
        if 1 <= p <= task_pages:
            show.add(p)

    show = sorted(show)
    last = 0

    for p in show:
        if last and p - last > 1:
            items.append(
                '<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>'
            )

        items.append(
            page_btn(p, active=(p == task_page))
        )

        last = p

    # 下一页
    items.append(
        page_btn(task_page + 1, "下一页", disabled=(task_page >= task_pages))
    )

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            任务第 <b>{task_page}</b> / <b>{task_pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
</div>
"""
