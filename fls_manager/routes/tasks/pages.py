import uuid
from math import ceil
from urllib.parse import urlparse

from flask import request, redirect, abort

from . import bp
from .forms import (
    task_form,
    parse_notify_from_form,
    parse_random_delay_from_form,
    parse_retry_from_form,
)
from .helpers import (
    parse_task_env_from_form,
    tasks_page_links,
    filter_tasks_for_page,
    sort_tasks_for_display,
)
from ...models import (
    load_tasks,
    save_tasks,
    load_collections,
    get_collection,
)
from ...utils import h, now_str
from ...ui.layout import layout
from ...ui.components import message_card, page_header, section, data_toolbar, empty_state
from ...ui.tables import tasks_table
from ...scheduler import reload_scheduler, cron_to_trigger


SORT_OPTIONS = [
    ("default", "默认"),
    ("recent_run", "最近运行"),
    ("last_run", "最后运行"),
    ("name", "任务名"),
    ("created", "创建时间"),
]

TASK_PAGE_COLLECTION_LIMIT = 3


def _clean_task_back_url(value, default="/tasks"):
    value = str(value or "").strip()

    if not value or value.startswith("//"):
        return default

    parsed = urlparse(value)

    if parsed.scheme or parsed.netloc:
        return default

    if not value.startswith("/"):
        return default

    return value


def _default_task_back_url(task=None):
    collection_id = str((task or {}).get("collection_id") or "").strip()

    if collection_id:
        return f"/collections#collection-{collection_id}"

    return "/tasks"


def _task_form_back_url(task=None):
    default = _default_task_back_url(task)
    raw = request.form.get("back") if request.method == "POST" else request.args.get("back")

    return _clean_task_back_url(raw, default)


def _render_collection_cards(collections, all_tasks):
    cards = ""

    task_count_map = {}

    for task in all_tasks:
        cid = str(task.get("collection_id") or "").strip()
        if not cid:
            continue
        task_count_map[cid] = task_count_map.get(cid, 0) + 1

    for c in collections:
        cid = c.get("id", "")
        cname = str(c.get("name", "") or "").strip() or "未命名合集"
        cremark = str(c.get("remark", "") or "").strip()
        task_count = task_count_map.get(cid, 0)

        cards += f"""
<div class="fls-summary-item" id="collection-{h(cid)}">
    <div class="fls-summary-label">{h(cname)}</div>
    <div class="help">{h(cremark or "暂无备注")}</div>
    <div style="margin-top:8px;line-height:1.7;">
        任务：<b>{task_count}</b>
    </div>
    <div class="action-row" style="margin-top:10px;">
        <a class="btn btn-blue" href="/collections#collection-{h(cid)}">查看</a>
        <a class="btn btn-orange" href="/collection/edit/{h(cid)}">编辑任务合集</a>
    </div>
</div>
"""

    if not cards:
        cards = empty_state(
            "暂无任务合集。",
            '<a class="btn btn-primary" href="/collection/new">新建任务合集</a>',
        )

    return f'<div class="fls-summary-grid">{cards}</div>'


def _task_from_post(base=None):
    task = dict(base or {})
    task.update({
        "name": request.form.get("name", "").strip(),
        "remark": request.form.get("remark", "").strip(),
        "command": request.form.get("command", "").strip(),
        "cron": request.form.get("cron", "").strip(),
        "config_path": request.form.get("config_path", "").strip(),
        "collection_id": request.form.get("collection_id", "").strip(),
        "enabled": request.form.get("enabled") == "1",
        "env": parse_task_env_from_form(),
        "proxy_id": request.form.get("proxy_id", "").strip(),
        "notify": parse_notify_from_form(),
        "random_delay": parse_random_delay_from_form(),
        "retry": parse_retry_from_form(),
    })

    return task


def _task_form_error(title, back_url, message, task):
    body = message_card(message, "error", strong=True) + task_form(
        task,
        back_url=back_url,
    )

    return layout(title, "tasks", body), 400


@bp.route("/tasks")
def tasks_page():
    all_tasks = load_tasks()
    collections = load_collections()

    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "default").strip()
    try:
        page = max(1, int(request.args.get("page", "1") or 1))
    except (TypeError, ValueError):
        page = 1
    per_page = 20

    sort_keys = {x[0] for x in SORT_OPTIONS}
    if sort not in sort_keys:
        sort = "default"

    # 普通任务列表只显示未放入合集的任务。
    uncollected_tasks = [
        t for t in all_tasks
        if not str(t.get("collection_id") or "").strip()
    ]

    filtered_tasks = filter_tasks_for_page(uncollected_tasks, q)
    filtered_tasks = sort_tasks_for_display(filtered_tasks, sort)

    total = len(filtered_tasks)
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)

    start = (page - 1) * per_page
    end = page * per_page
    show_tasks = filtered_tasks[start:end]

    page_links_html = tasks_page_links(q, page, pages, sort)

    # 任务管理页合集
    collections_sorted = sorted(
        collections,
        key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""),
        reverse=True,
    )
    collections_show = collections_sorted[:TASK_PAGE_COLLECTION_LIMIT]
    collection_cards_html = _render_collection_cards(collections_show, all_tasks)

    hidden_count = len(all_tasks) - len(uncollected_tasks)
    pinned_count = sum(1 for t in all_tasks if t.get("pinned"))

    sort_options_html = ""

    for key, text in SORT_OPTIONS:
        s = "selected" if key == sort else ""
        sort_options_html += f'<option value="{h(key)}" {s}>{h(text)}</option>'

    more_collection_tip = ""

    if len(collections_sorted) > TASK_PAGE_COLLECTION_LIMIT:
        more_collection_tip = f"""
<div class="help" style="margin-top:8px;">
    共 {len(collections_sorted)} 个任务合集，完整内容请到
    <a href="/collections">任务合集</a> 查看。
</div>
"""

    header = page_header(
        "任务管理",
        help_html=(
            f"Cron 留空表示手动任务。共 {len(all_tasks)} 个任务，其中 {hidden_count} 个已放入合集，"
            f"当前置顶 {pinned_count} 个。放入合集的任务仍会正常运行。"
        ),
        actions_html=(
            '<a class="btn btn-primary" href="/task/new">新建任务</a>'
            '<a class="btn btn-blue" href="/collection/new">新建合集</a>'
            '<a class="btn btn-gray" href="/collections">任务合集</a>'
        ),
    )
    collection_section = section(
        "任务合集",
        f'<div class="help">完整任务合集请进入“任务合集”。</div>{collection_cards_html}{more_collection_tip}',
        section_id="task-collections",
    )
    search_controls = f"""
    <div class="form-item">
        <label for="task-search">搜索任务</label>
        <input id="task-search" name="q" value="{h(q)}" placeholder="任务名 / 备注 / 命令 / Cron / 配置路径">
    </div>
    <div class="form-item">
        <label for="task-sort">排序方式</label>
        <select id="task-sort" name="sort">{sort_options_html}</select>
    </div>
    <div class="action-row">
        <button class="btn btn-primary" type="submit">查询任务</button>
        <a class="btn btn-gray" href="/tasks">重置筛选</a>
    </div>
"""
    toolbar = data_toolbar(search_controls, toolbar_id="task-filter-toolbar")
    body = f"""
{header}
{collection_section}
<form method="get" aria-label="任务查询">{toolbar}</form>
{section("任务列表", tasks_table(show_tasks), section_id="task-list")}
{page_links_html}
"""
    return layout("任务管理", "tasks", body)


@bp.route("/task/new", methods=["GET", "POST"])
def task_new():
    back_url = _task_form_back_url()

    if request.method == "POST":
        task = _task_from_post()
        name = task.get("name", "")
        command = task.get("command", "")
        cron_expr = task.get("cron", "")
        collection_id = task.get("collection_id", "")

        if not name:
            return _task_form_error("新建任务", back_url, "任务名不能为空", task)

        if not command:
            return _task_form_error("新建任务", back_url, "命令不能为空", task)

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return _task_form_error("新建任务", back_url, f"Cron 不合法：{e}", task)

        if collection_id and not get_collection(collection_id):
            return _task_form_error("新建任务", back_url, "合集不存在", task)

        tasks = load_tasks()

        task.update({
            "id": uuid.uuid4().hex,
            "run_count": 0,
            "pinned": False,
            "created_at": now_str(),
            "updated_at": now_str(),
        })

        tasks.append(task)
        save_tasks(tasks)
        reload_scheduler()

        return redirect(back_url)

    return layout("新建任务", "tasks", task_form(back_url=back_url))


@bp.route("/task/edit/<task_id>", methods=["GET", "POST"])
def task_edit(task_id):
    tasks = load_tasks()
    task = None

    for t in tasks:
        if t.get("id") == task_id:
            task = t
            break

    if not task:
        abort(404)

    back_url = _task_form_back_url(task)

    if request.method == "POST":
        draft = _task_from_post(task)
        name = draft.get("name", "")
        command = draft.get("command", "")
        cron_expr = draft.get("cron", "")
        collection_id = draft.get("collection_id", "")

        if not name:
            return _task_form_error("编辑任务", back_url, "任务名不能为空", draft)

        if not command:
            return _task_form_error("编辑任务", back_url, "命令不能为空", draft)

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return _task_form_error("编辑任务", back_url, f"Cron 不合法：{e}", draft)

        if collection_id and not get_collection(collection_id):
            return _task_form_error("编辑任务", back_url, "合集不存在", draft)

        task.update({
            "name": draft.get("name", ""),
            "remark": draft.get("remark", ""),
            "command": draft.get("command", ""),
            "cron": draft.get("cron", ""),
            "config_path": draft.get("config_path", ""),
            "collection_id": draft.get("collection_id", ""),
            "enabled": draft.get("enabled", True),
            "env": draft.get("env", {}),
            "proxy_id": draft.get("proxy_id", ""),
            "notify": draft.get("notify"),
            "random_delay": draft.get("random_delay"),
            "retry": draft.get("retry"),
        })
        task["updated_at"] = now_str()
        task.setdefault("run_count", 0)
        task.setdefault("pinned", False)

        save_tasks(tasks)
        reload_scheduler()

        return redirect(back_url)

    return layout("编辑任务", "tasks", task_form(task, back_url=back_url))
