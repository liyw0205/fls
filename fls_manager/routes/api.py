from flask import Blueprint, jsonify

from ..models import load_tasks, save_tasks
from ..task_runner import run_task_now, stop_task_now, is_running, safe_process_name
from ..scheduler import reload_scheduler, scheduler
from ..state import RUNNING
from ..utils import now_str

bp = Blueprint("api", __name__)


@bp.route("/api/status")
def api_status():
    result = []

    for t in load_tasks():
        task_id = t["id"]
        running = is_running(task_id)

        result.append({
            "id": task_id,
            "name": t.get("name"),
            "command": t.get("command"),
            "cron": t.get("cron"),
            "enabled": t.get("enabled", True),
            "running": running,
            "run_count": int(t.get("run_count", 0)),
            "pid": RUNNING.get(task_id, {}).get("pid") if running else None,
            "process_name": RUNNING.get(task_id, {}).get("process_name") if running else safe_process_name(t.get("name") or t.get("command")),
        })

    return jsonify(result)


@bp.route("/api/scheduler/jobs")
def api_scheduler_jobs():
    result = []

    try:
        for job in scheduler.get_jobs():
            result.append({
                "id": job.id,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            })

    except Exception as e:
        return jsonify({
            "ok": False,
            "msg": str(e),
            "jobs": [],
        }), 500

    return jsonify({
        "ok": True,
        "jobs": result,
    })


@bp.route("/api/task/action/<action>/<task_id>", methods=["POST"])
def api_task_action(action, task_id):
    try:
        if action == "run":
            ok, msg = run_task_now(task_id, source="manual")
            return jsonify({"ok": ok, "msg": msg})

        if action == "stop":
            ok, msg = stop_task_now(task_id)
            return jsonify({"ok": ok, "msg": msg})

        if action == "toggle":
            tasks = load_tasks()
            found = False

            for task in tasks:
                if task.get("id") == task_id:
                    task["enabled"] = not task.get("enabled", True)
                    task["updated_at"] = now_str()
                    found = True
                    break

            if not found:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            save_tasks(tasks)
            reload_scheduler()

            return jsonify({"ok": True, "msg": "已切换"})

        if action == "delete":
            stop_task_now(task_id)

            tasks = [
                t for t in load_tasks()
                if t.get("id") != task_id
            ]

            save_tasks(tasks)
            reload_scheduler()

            return jsonify({"ok": True, "msg": "已删除"})

        return jsonify({"ok": False, "msg": "未知操作"}), 400

    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500
