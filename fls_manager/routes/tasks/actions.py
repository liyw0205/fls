from flask import redirect, url_for, abort

from . import bp

from ...models import load_tasks, save_tasks, mutate_tasks
from ...utils import h, now_str, get_back_url
from ...scheduler import reload_scheduler
from ...task_runner import run_task_now, stop_task_now, cancel_task_retry
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

    def remove_task(current_tasks):
        remaining = [task for task in current_tasks if task.get("id") != task_id]
        if len(remaining) == len(current_tasks):
            return False
        current_tasks[:] = remaining
        return True

    _tasks, changed = mutate_tasks(remove_task, reload_scheduler)
    if not changed:
        abort(404)

    return redirect(get_back_url("/tasks"))


@bp.route("/task/toggle/<task_id>", methods=["POST"])
def task_toggle(task_id):
    result = {}

    def toggle(current_tasks):
        task = next((item for item in current_tasks if item.get("id") == task_id), None)
        if task is None:
            return False
        task["enabled"] = not task.get("enabled", True)
        task["updated_at"] = now_str()
        result["enabled"] = task["enabled"]
        return True

    def after_toggle():
        if not result["enabled"]:
            cancel_task_retry(task_id, "任务已禁用")

    _tasks, changed = mutate_tasks(toggle, reload_scheduler, after_toggle)
    if not changed:
        abort(404)

    return redirect(get_back_url("/tasks"))


@bp.route("/task/pin/<task_id>", methods=["POST"])
def task_pin(task_id):
    result = {}

    def pin(current_tasks):
        target = next((item for item in current_tasks if item.get("id") == task_id), None)
        if target is None:
            return False
        result["found"] = True

        pinned_count = sum(1 for task in current_tasks if task.get("pinned"))
        if not target.get("pinned") and pinned_count >= 5:
            result["limit_exceeded"] = True
            return False

        target["pinned"] = not target.get("pinned", False)
        target["updated_at"] = now_str()
        result["pinned"] = target["pinned"]
        return True

    mutate_tasks(pin)

    if not result.get("found"):
        abort(404)

    if result.get("limit_exceeded"):
        back_url = get_back_url("/tasks")
        body = f"""
{message_card("最多只能置顶 5 个任务，请先取消一个置顶任务", "error", strong=True, title="置顶失败")}
<div class="card">
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
"""
        return layout("置顶任务", "tasks", body), 400

    return redirect(get_back_url("/tasks"))


@bp.route("/task/collection/clear/<task_id>", methods=["POST"])
def task_collection_clear(task_id):
    result = {}

    def clear_collection(current_tasks):
        task = next((item for item in current_tasks if item.get("id") == task_id), None)
        if task is None:
            return False
        result["found"] = True
        if not str(task.get("collection_id") or ""):
            return False
        task["collection_id"] = ""
        task["updated_at"] = now_str()
        return True

    mutate_tasks(clear_collection)

    if not result.get("found"):
        abort(404)

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

    ok, msg = stop_task_now(task_id)
    back_url = get_back_url("/tasks")

    if not ok and msg != "任务未运行":
        title = "任务已结束" if msg.startswith("任务已结束") else "停止失败"
        body = f"""
{message_card(msg, "error", strong=True, title=title)}
<div class="card">
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
"""
        return layout("停止任务", "tasks", body), 409

    return redirect(back_url)
