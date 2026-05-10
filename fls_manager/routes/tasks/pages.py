import uuid
from math import ceil

from flask import request, redirect, url_for, abort

from . import bp
from .forms import (
    task_form,
    parse_notify_from_form,
    parse_random_delay_from_form,
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
        <a class="btn btn-orange" href="/collection/edit/{h(cid)}">编辑</a>
    </div>
</div>
"""

    if not cards:
        cards = '<div class="fls-empty-card">暂无合集，请点击“新建合集”</div>'

    return f'<div class="fls-summary-grid">{cards}</div>'


@bp.route("/tasks")
def tasks_page():
    all_tasks = load_tasks()
    collections = load_collections()

    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "default").strip()
    page = max(1, int(request.args.get("page", "1") or 1))
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
    共 {len(collections_sorted)} 个合集，完整内容请到
    <a href="/collections">合集管理</a> 查看。
</div>
"""

    body = f"""
<div class="card">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">任务管理</div>
            <div class="help">
                Cron 留空表示手动任务。<br>
                共 {len(all_tasks)} 个任务，其中 {hidden_count} 个已放入合集，当前置顶 {pinned_count} 个。<br>
                放入合集的任务不会在普通列表显示，但仍会正常运行。
            </div>
        </div>
        <div class="action-row">
            <a class="btn btn-primary" href="/task/new">新建任务</a>
            <a class="btn btn-blue" href="/collection/new">新建合集</a>
            <a class="btn btn-gray" href="/collections">合集管理</a>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">合集</div>
    <div class="help">完整合集请进入“合集管理”。</div>
    <br>
    {collection_cards_html}
    {more_collection_tip}
</div>

<form method="get">
<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>搜索任务</label>
            <input name="q" value="{h(q)}" placeholder="任务名 / 备注 / 命令 / Cron / 配置路径">
        </div>

        <div class="form-item">
            <label>排序方式</label>
            <select name="sort">{sort_options_html}</select>
        </div>
    </div>

    <br>

    <button class="btn btn-primary" type="submit">搜索</button>
    <a class="btn btn-gray" href="/tasks">重置</a>
</div>
</form>

<div class="card">
    {tasks_table(show_tasks)}
</div>

{page_links_html}
"""
    return layout("任务管理", "tasks", body)


@bp.route("/task/new", methods=["GET", "POST"])
def task_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        command = request.form.get("command", "").strip()
        cron_expr = request.form.get("cron", "").strip()
        collection_id = request.form.get("collection_id", "").strip()

        if not name:
            return "任务名不能为空", 400

        if not command:
            return "命令不能为空", 400

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return f"Cron 不合法：{e}", 400

        if collection_id and not get_collection(collection_id):
            return "合集不存在", 400

        tasks = load_tasks()

        task = {
            "id": uuid.uuid4().hex,
            "name": name,
            "remark": request.form.get("remark", "").strip(),
            "command": command,
            "cron": cron_expr,
            "config_path": request.form.get("config_path", "").strip(),
            "collection_id": collection_id,
            "enabled": request.form.get("enabled") == "1",
            "env": parse_task_env_from_form(),
            "proxy_id": request.form.get("proxy_id", "").strip(),
            "notify": parse_notify_from_form(),
            "random_delay": parse_random_delay_from_form(),
            "run_count": 0,
            "pinned": False,
            "created_at": now_str(),
            "updated_at": now_str(),
        }

        tasks.append(task)
        save_tasks(tasks)
        reload_scheduler()

        return redirect(url_for("tasks.tasks_page"))

    return layout("新建任务", "tasks", task_form())


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

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        command = request.form.get("command", "").strip()
        cron_expr = request.form.get("cron", "").strip()
        collection_id = request.form.get("collection_id", "").strip()

        if not name:
            return "任务名不能为空", 400

        if not command:
            return "命令不能为空", 400

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return f"Cron 不合法：{e}", 400

        if collection_id and not get_collection(collection_id):
            return "合集不存在", 400

        task["name"] = name
        task["remark"] = request.form.get("remark", "").strip()
        task["command"] = command
        task["cron"] = cron_expr
        task["config_path"] = request.form.get("config_path", "").strip()
        task["collection_id"] = collection_id
        task["enabled"] = request.form.get("enabled") == "1"
        task["env"] = parse_task_env_from_form()
        task["proxy_id"] = request.form.get("proxy_id", "").strip()
        task["notify"] = parse_notify_from_form()
        task["random_delay"] = parse_random_delay_from_form()
        task["updated_at"] = now_str()
        task.setdefault("run_count", 0)
        task.setdefault("pinned", False)

        save_tasks(tasks)
        reload_scheduler()

        return redirect(url_for("tasks.tasks_page"))

    return layout("编辑任务", "tasks", task_form(task))