from flask import redirect, url_for

from . import bp

from ...models import load_tasks, save_tasks
from ...utils import h, now_str, get_back_url
from ...scheduler import reload_scheduler
from ...task_runner import run_task_now, stop_task_now


@bp.route("/task/delete/<task_id>")
def task_delete(task_id):
    stop_task_now(task_id)

    tasks = [
        t for t in load_tasks()
        if t.get("id") != task_id
    ]

    save_tasks(tasks)
    reload_scheduler()

    return redirect(url_for("tasks.tasks_page"))


@bp.route("/task/toggle/<task_id>")
def task_toggle(task_id):
    tasks = load_tasks()

    for task in tasks:
        if task.get("id") == task_id:
            task["enabled"] = not task.get("enabled", True)
            task["updated_at"] = now_str()
            break

    save_tasks(tasks)
    reload_scheduler()

    return redirect(url_for("tasks.tasks_page"))


@bp.route("/run/<task_id>")
def run_task_route(task_id):
    ok, msg = run_task_now(task_id, source="manual")
    back_url = get_back_url("/tasks")

    if not ok:
        return f"{h(msg)}<br><a href='{h(back_url)}'>返回</a>", 400

    return redirect(url_for("tasks.log_view", task_id=task_id, back=back_url))


@bp.route("/stop/<task_id>")
def stop_task_route(task_id):
    stop_task_now(task_id)
    return redirect(get_back_url("/tasks"))