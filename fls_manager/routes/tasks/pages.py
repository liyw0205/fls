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
)

from ...models import load_tasks, save_tasks
from ...utils import h, now_str
from ...ui.layout import layout
from ...ui.tables import tasks_table
from ...scheduler import reload_scheduler, cron_to_trigger


@bp.route("/tasks")
def tasks_page():
    all_tasks = load_tasks()

    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = 20

    filtered_tasks = filter_tasks_for_page(all_tasks, q)

    total = len(filtered_tasks)
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)

    start = (page - 1) * per_page
    end = page * per_page
    show_tasks = filtered_tasks[start:end]

    page_links_html = tasks_page_links(q, page, pages)

    body = f"""
<div class="card">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">任务管理</div>
            <div class="help">
                Cron 留空表示手动任务。<br>
                共 {len(all_tasks)} 个任务，当前匹配 {total} 个，每页 {per_page} 个。
            </div>
        </div>
        <a class="btn btn-primary" href="/task/new">新建任务</a>
    </div>
</div>

<form method="get">
<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>搜索任务</label>
            <input name="q" value="{h(q)}" placeholder="任务名 / 备注 / 命令 / Cron / 配置路径">
        </div>

        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="submit">搜索</button>
            <a class="btn btn-gray" href="/tasks">重置</a>
        </div>
    </div>
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

        if not name:
            return "任务名不能为空", 400

        if not command:
            return "命令不能为空", 400

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return f"Cron 不合法：{e}", 400

        tasks = load_tasks()

        task = {
            "id": uuid.uuid4().hex,
            "name": name,
            "remark": request.form.get("remark", "").strip(),
            "command": command,
            "cron": cron_expr,
            "config_path": request.form.get("config_path", "").strip(),
            "enabled": request.form.get("enabled") == "1",
            "env": parse_task_env_from_form(),
            "proxy_id": request.form.get("proxy_id", "").strip(),
            "notify": parse_notify_from_form(),
            "random_delay": parse_random_delay_from_form(),
            "run_count": 0,
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

        if not name:
            return "任务名不能为空", 400

        if not command:
            return "命令不能为空", 400

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return f"Cron 不合法：{e}", 400

        task["name"] = name
        task["remark"] = request.form.get("remark", "").strip()
        task["command"] = command
        task["cron"] = cron_expr
        task["config_path"] = request.form.get("config_path", "").strip()
        task["enabled"] = request.form.get("enabled") == "1"
        task["env"] = parse_task_env_from_form()
        task["proxy_id"] = request.form.get("proxy_id", "").strip()
        task["notify"] = parse_notify_from_form()
        task["random_delay"] = parse_random_delay_from_form()
        task["updated_at"] = now_str()
        task.setdefault("run_count", 0)

        save_tasks(tasks)
        reload_scheduler()

        return redirect(url_for("tasks.tasks_page"))

    return layout("编辑任务", "tasks", task_form(task))