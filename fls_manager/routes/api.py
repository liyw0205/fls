import copy
import uuid

from flask import Blueprint, jsonify, request

from ..models import load_tasks, save_tasks
from ..task_runner import run_task_now, stop_task_now, is_running, safe_process_name
from ..scheduler import reload_scheduler, scheduler
from ..state import RUNNING
from ..utils import now_str

bp = Blueprint("api", __name__)


def _unique_task_ids(values):
    if isinstance(values, str):
        values = [values]

    result = []
    seen = set()

    for value in values or []:
        task_id = str(value or "").strip()

        if not task_id or task_id in seen:
            continue

        seen.add(task_id)
        result.append(task_id)

    return result


def _copy_task(tasks, task_id):
    for task in tasks:
        if task.get("id") != task_id:
            continue

        now = now_str()
        base_name = str(task.get("name") or "未命名任务").strip() or "未命名任务"
        new_task = copy.deepcopy(task)

        new_task["id"] = uuid.uuid4().hex
        new_task["name"] = f"{base_name}-copy"
        new_task["run_count"] = 0
        new_task["pinned"] = False
        new_task["created_at"] = now
        new_task["updated_at"] = now
        new_task.pop("last_run_at", None)

        tasks.append(new_task)

        return new_task

    return None


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

        if action == "copy":
            tasks = load_tasks()
            new_task = _copy_task(tasks, task_id)

            if not new_task:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            save_tasks(tasks)
            reload_scheduler()

            return jsonify({
                "ok": True,
                "msg": f"已复制为 {new_task.get('name')}",
            })

        if action == "pin":
            tasks = load_tasks()
            target = None

            for task in tasks:
                if task.get("id") == task_id:
                    target = task
                    break

            if not target:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            pinned_count = sum(1 for task in tasks if task.get("pinned"))

            if not target.get("pinned") and pinned_count >= 5:
                return jsonify({"ok": False, "msg": "最多只能置顶 5 个任务，请先取消一个置顶任务"}), 400

            target["pinned"] = not target.get("pinned", False)
            target["updated_at"] = now_str()

            save_tasks(tasks)

            return jsonify({
                "ok": True,
                "msg": "已置顶" if target.get("pinned") else "已取消置顶",
            })

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


@bp.route("/api/task/bulk-action", methods=["POST"])
def api_task_bulk_action():
    try:
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or request.form.get("action") or "").strip()

        raw_task_ids = data.get("task_ids")
        if raw_task_ids is None:
            raw_task_ids = request.form.getlist("task_ids")

        task_ids = _unique_task_ids(raw_task_ids)

        if action not in ("enable", "disable", "run", "stop", "delete", "clear_collection"):
            return jsonify({"ok": False, "msg": "未知批量操作"}), 400

        if not task_ids:
            return jsonify({"ok": False, "msg": "请选择任务"}), 400

        tasks = load_tasks()
        task_map = {str(task.get("id") or ""): task for task in tasks}
        missing = [task_id for task_id in task_ids if task_id not in task_map]

        if missing:
            return jsonify({
                "ok": False,
                "msg": f"有 {len(missing)} 个任务不存在，请刷新后重试",
            }), 404

        selected = set(task_ids)

        if action in ("enable", "disable"):
            enabled = action == "enable"

            for task in tasks:
                if task.get("id") in selected:
                    task["enabled"] = enabled
                    task["updated_at"] = now_str()

            save_tasks(tasks)
            reload_scheduler()

            return jsonify({
                "ok": True,
                "msg": f"已{'启用' if enabled else '禁用'} {len(task_ids)} 个任务",
            })

        if action == "run":
            ok_count = 0
            failed = []

            for task_id in task_ids:
                ok, msg = run_task_now(task_id, source="manual")
                if ok:
                    ok_count += 1
                else:
                    task = task_map.get(task_id) or {}
                    name = task.get("name") or task.get("command") or task_id
                    failed.append(f"{name}: {msg}")

            parts = [f"已提交运行 {ok_count} 个任务"]
            if failed:
                parts.append(f"{len(failed)} 个未运行：{'；'.join(failed[:3])}")
                if len(failed) > 3:
                    parts.append(f"等 {len(failed)} 个")

            return jsonify({"ok": True, "msg": "；".join(parts)})

        if action == "stop":
            ok_count = 0
            skipped = 0
            failed = []

            for task_id in task_ids:
                ok, msg = stop_task_now(task_id)
                if ok:
                    ok_count += 1
                elif msg == "任务未运行":
                    skipped += 1
                else:
                    task = task_map.get(task_id) or {}
                    name = task.get("name") or task.get("command") or task_id
                    failed.append(f"{name}: {msg}")

            parts = [f"已结束 {ok_count} 个任务"]
            if skipped:
                parts.append(f"跳过 {skipped} 个未运行任务")
            if failed:
                parts.append(f"{len(failed)} 个结束失败：{'；'.join(failed[:3])}")
                if len(failed) > 3:
                    parts.append(f"等 {len(failed)} 个")

            return jsonify({"ok": True, "msg": "；".join(parts)})

        if action == "delete":
            for task_id in task_ids:
                stop_task_now(task_id)

            tasks = [
                task for task in tasks
                if task.get("id") not in selected
            ]

            save_tasks(tasks)
            reload_scheduler()

            return jsonify({
                "ok": True,
                "msg": f"已删除 {len(task_ids)} 个任务",
            })

        if action == "clear_collection":
            for task in tasks:
                if task.get("id") in selected:
                    task["collection_id"] = ""
                    task["updated_at"] = now_str()

            save_tasks(tasks)

            return jsonify({
                "ok": True,
                "msg": f"已取出 {len(task_ids)} 个任务",
            })

        return jsonify({"ok": False, "msg": "未知批量操作"}), 400

    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500
