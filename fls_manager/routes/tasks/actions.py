from flask import redirect, url_for, abort

from . import bp

from ...models import load_tasks, save_tasks
from ...utils import h, now_str, get_back_url
from ...scheduler import reload_scheduler
from ...task_runner import run_task_now, stop_task_now
from ...ui.components import message_card
from ...ui.layout import layout


@bp.route("/task/delete/<task_id>", methods=["POST"])
def task_delete(task_id):
    tasks = load_tasks()
    found = any(t.get("id") == task_id for t in tasks)

    if not found:
        abort(404)

    ok, msg = stop_task_now(task_id)

    if not ok and msg != "任务未运行":
        back_url = get_back_url("/tasks")
        body = f"""
{message_card(f"删除失败：{msg}", "error", strong=True, title="删除失败")}
<div class="card">
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
"""
        return layout("删除任务", "tasks", body), 409

    tasks = [
        t for t in tasks
        if t.get("id") != task_id
    ]

    save_tasks(tasks)
    reload_scheduler()

    return redirect(get_back_url("/tasks"))


@bp.route("/task/toggle/<task_id>", methods=["POST"])
def task_toggle(task_id):
    tasks = load_tasks()
    found = False

    for task in tasks:
        if task.get("id") == task_id:
            task["enabled"] = not task.get("enabled", True)
            task["updated_at"] = now_str()
            found = True
            break

    if not found:
        abort(404)

    save_tasks(tasks)
    reload_scheduler()

    return redirect(get_back_url("/tasks"))


@bp.route("/task/pin/<task_id>", methods=["POST"])
def task_pin(task_id):
    tasks = load_tasks()
    target = None

    for task in tasks:
        if task.get("id") == task_id:
            target = task
            break

    if not target:
        abort(404)

    pinned_count = sum(1 for t in tasks if t.get("pinned"))

    if not target.get("pinned") and pinned_count >= 5:
        back_url = get_back_url("/tasks")
        body = f"""
{message_card("最多只能置顶 5 个任务，请先取消一个置顶任务", "error", strong=True, title="置顶失败")}
<div class="card">
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
"""
        return layout("置顶任务", "tasks", body), 400

    target["pinned"] = not target.get("pinned", False)
    target["updated_at"] = now_str()

    save_tasks(tasks)

    return redirect(get_back_url("/tasks"))


@bp.route("/task/collection/clear/<task_id>", methods=["POST"])
def task_collection_clear(task_id):
    tasks = load_tasks()
    found = False
    changed = False

    for task in tasks:
        if task.get("id") == task_id:
            found = True
            if str(task.get("collection_id") or ""):
                task["collection_id"] = ""
                task["updated_at"] = now_str()
                changed = True
            break

    if not found:
        abort(404)

    if changed:
        save_tasks(tasks)

    return redirect(get_back_url("/collections"))


@bp.route("/run/<task_id>", methods=["POST"])
def run_task_route(task_id):
    ok, msg = run_task_now(task_id, source="manual")
    back_url = get_back_url("/tasks")

    if not ok:
        status_code = 404 if msg == "任务不存在" else 400
        body = f"""
{message_card(msg, "error", strong=True, title="运行失败")}
<div class="card">
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
"""
        return layout("运行任务", "tasks", body), status_code

    return redirect(url_for("tasks.log_view", task_id=task_id, back=back_url))


@bp.route("/stop/<task_id>", methods=["POST"])
def stop_task_route(task_id):
    if not any(task.get("id") == task_id for task in load_tasks()):
        abort(404)

    stop_task_now(task_id)
    return redirect(get_back_url("/tasks"))
