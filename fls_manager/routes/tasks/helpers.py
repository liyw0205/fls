import os
from urllib.parse import quote

from flask import request

from ...paths import SCRIPT_DIR
from ...utils import h


def task_config_safe_path(rel_path):
    """
    任务配置文件安全路径。

    只允许编辑 scripts 目录下的文件，例如：
      checkbox/config.yml
      kgcheckin/config.json

    不允许：
      /etc/passwd
      ../../xxx
    """
    rel_path = str(rel_path or "").strip().lstrip("/")

    if not rel_path:
        raise ValueError("配置文件路径为空")

    target = (SCRIPT_DIR / rel_path).resolve()
    base = SCRIPT_DIR.resolve()

    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("配置文件路径非法")

    return target


def parse_task_env_from_form():
    keys = request.form.getlist("env_key")
    values = request.form.getlist("env_value")
    env = {}

    for idx, key in enumerate(keys):
        key = str(key or "").strip()

        if not key:
            continue

        value = values[idx] if idx < len(values) else ""
        env[key] = value

    return env


def tasks_page_links(q, page, pages):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"/tasks?page={int(p)}"

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

    items.append(
        page_btn(page - 1, "上一页", disabled=(page <= 1))
    )

    show = {1, pages}

    for p in range(page - 2, page + 3):
        if 1 <= p <= pages:
            show.add(p)

    show = sorted(show)

    last = 0

    for p in show:
        if last and p - last > 1:
            items.append(
                '<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>'
            )

        items.append(
            page_btn(p, active=(p == page))
        )

        last = p

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


def filter_tasks_for_page(tasks, q):
    q = str(q or "").strip().lower()

    if not q:
        return tasks

    result = []

    for task in tasks:
        fields = [
            task.get("name", ""),
            task.get("remark", ""),
            task.get("command", ""),
            task.get("cron", ""),
            task.get("config_path", ""),
            task.get("id", ""),
        ]

        text = "\n".join(str(x or "") for x in fields).lower()

        if q in text:
            result.append(task)

    return result