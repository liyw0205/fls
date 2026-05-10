import os
from urllib.parse import quote

from flask import request

from ...paths import SCRIPT_DIR
from ...utils import h
from ...models import load_collections


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


def collection_select_options(selected_id=""):
    selected_id = str(selected_id or "").strip()
    collections = load_collections()

    options = '<option value="">不放入合集</option>'

    if not collections:
        options += '<option value="" disabled>暂无合集</option>'
        return options

    collections = sorted(
        collections,
        key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""),
        reverse=True,
    )

    for c in collections:
        cid = c.get("id", "")
        s = "selected" if cid == selected_id else ""
        options += (
            f'<option value="{h(cid)}" {s}>'
            f'{h(c.get("name", "未命名合集"))}'
            f'</option>'
        )

    return options


def task_matches_query(task, q):
    q = str(q or "").strip().lower()

    if not q:
        return True

    fields = [
        task.get("name", ""),
        task.get("remark", ""),
        task.get("command", ""),
        task.get("cron", ""),
        task.get("config_path", ""),
        task.get("id", ""),
    ]

    text = "\n".join(str(x or "") for x in fields).lower()

    return q in text


def sort_tasks_for_display(tasks, sort="default"):
    """
    任务显示排序。

    sort:
      default    = 默认，置顶优先 + 更新时间倒序
      recent_run = 最近运行，运行次数倒序 + 最后运行时间倒序
      last_run   = 最后运行时间倒序
      name       = 任务名正序
      created    = 创建时间倒序
    """
    tasks = list(tasks or [])
    sort = str(sort or "default").strip()

    def text_value(v):
        return str(v or "").strip().lower()

    def time_value(v):
        return str(v or "").strip()

    if sort == "name":
        return sorted(
            tasks,
            key=lambda t: (
                text_value(t.get("name") or t.get("command")),
                text_value(t.get("remark")),
                str(t.get("id") or ""),
            ),
        )

    if sort == "created":
        return sorted(
            tasks,
            key=lambda t: (
                time_value(t.get("created_at")),
                time_value(t.get("updated_at")),
            ),
            reverse=True,
        )

    if sort == "last_run":
        return sorted(
            tasks,
            key=lambda t: (
                time_value(t.get("last_run_at")),
                time_value(t.get("updated_at")),
                time_value(t.get("created_at")),
            ),
            reverse=True,
        )

    if sort == "recent_run":
        return sorted(
            tasks,
            key=lambda t: (
                int(t.get("run_count", 0) or 0),
                time_value(t.get("last_run_at")),
                time_value(t.get("updated_at")),
                time_value(t.get("created_at")),
            ),
            reverse=True,
        )

    return sorted(
        tasks,
        key=lambda t: (
            1 if t.get("pinned", False) else 0,
            time_value(t.get("updated_at")),
            time_value(t.get("created_at")),
        ),
        reverse=True,
    )


def tasks_page_links(q, page, pages, sort="default"):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"/tasks?page={int(p)}"

        if q:
            url += "&q=" + quote(q)

        if sort and sort != "default":
            url += "&sort=" + quote(sort)

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
        if task_matches_query(task, q):
            result.append(task)

    return result