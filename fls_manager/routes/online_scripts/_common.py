from .bp import bp
import json
import uuid
import time
from math import ceil
from urllib.parse import quote

from flask import Blueprint, request, redirect, url_for, abort, jsonify

from ...utils import h, get_back_url, LazyModule, prune_completed_records
from ...ui.components import message_card, page_header_card, pagination_card, summary_item, data_toolbar
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
    get_online_refresh_state,
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

requests = LazyModule("requests")


def online_scripts_page_links(q, page, pages):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"/online-scripts?page={int(p)}"
        if q:
            url += "&q=" + quote(q)
        return url

    return pagination_card(page, pages, href_for=build_url)


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
        excluded_values = [
            value.strip() for value in excluded_task_indexes.replace("，", ",").split(",")
            if value.strip()
        ]
        excluded_set = {
            int(index) for index in validated_install_task_indexes(item, excluded_values)
        } if excluded_values else set()

        selected_task_indexes = [
            str(i)
            for i in range(1, all_count + 1)
            if i not in excluded_set
        ]
    elif select_mode:
        raise ValueError("任务选择模式无效")

    return selected_task_indexes


def validated_install_task_indexes(item, values):
    if not isinstance(values, list) or not values:
        raise ValueError("任务选择为空或已失效")
    task_count = len(online_script_task_crons(item))
    indexes = []
    for value in values:
        raw = str(value).strip()
        if not raw.isdecimal():
            raise ValueError("任务序号必须为整数")
        index = int(raw)
        if not 1 <= index <= task_count:
            raise ValueError("任务选择已失效")
        if str(index) not in indexes:
            indexes.append(str(index))
    return indexes


def validated_install_retry_task_indexes(item, info):
    indexes = validated_install_task_indexes(item, info.get("selected_task_indexes"))
    task_crons = online_script_task_crons(item)
    selected_tasks = [task_crons[int(index) - 1] for index in indexes]
    previous = info.get("selected_tasks")
    if previous is not None and previous != selected_tasks:
        raise ValueError("脚本源任务已变更，请重新选择任务")
    return indexes

def install_task_page_links(script_id, task_page, task_pages, excluded="", task_q=""):
    if task_pages <= 1:
        return ""

    return pagination_card(
        task_page,
        task_pages,
        onclick_for=lambda p: f"flsInstallGoTaskPage({int(p)})",
        page_label="任务第",
    )
