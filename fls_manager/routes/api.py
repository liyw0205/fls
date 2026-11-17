import os
import threading

from flask import Blueprint, Response, jsonify, request

from ..models import load_tasks, load_tasks_for_read, save_tasks, mutate_tasks
from ..task_runner import (
    run_task_now,
    stop_task_now,
    running_task_snapshot,
    safe_process_name,
    cancel_task_retry,
)
from ..scheduler import get_scheduler_state, reload_scheduler, scheduler
from ..utils import now_str
from ..notify import (
    NOTIFY_HISTORY_STATUSES,
    load_notify_history,
    retry_notify_delivery,
)

bp = Blueprint("api", __name__)
_STATUS_STATIC_CACHE_LOCK = threading.RLock()
_STATUS_STATIC_CACHE = None
_STATUS_RESPONSE_CACHE = None


def _notify_history_limit(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(100, value))


@bp.route("/api/notify/history")
def api_notify_history():
    raw_limit = request.args.get("limit", "50")
    limit = _notify_history_limit(raw_limit)
    if limit is None:
        return jsonify({"ok": False, "msg": "limit 必须是整数", "items": []}), 400

    status = str(request.args.get("status") or "").strip()
    if status and status not in NOTIFY_HISTORY_STATUSES:
        return jsonify({"ok": False, "msg": "未知通知状态", "items": []}), 400

    return jsonify({
        "ok": True,
        "items": load_notify_history(limit=limit, status=status),
        "limit": limit,
        "status": status,
    })


@bp.route("/api/notify/retry/<delivery_id>", methods=["POST"])
def api_notify_retry(delivery_id):
    try:
        ok, msg, row = retry_notify_delivery(delivery_id)
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 500

    payload = {"ok": ok, "msg": msg}
    if row is not None:
        payload["delivery"] = row

    if ok:
        return jsonify(payload)
    if msg == "通知投递不存在":
        return jsonify(payload), 404
    if msg in ("通知投递正在发送", "通知投递已成功，无需重试"):
        return jsonify(payload), 409
    return jsonify(payload), 500


def _task_exists(task_id):
    return any(str(task.get("id") or "") == task_id for task in load_tasks())


def _task_action_result(ok, msg):
    payload = {"ok": ok, "msg": msg}

    if not ok:
        if msg == "任务不存在":
            return jsonify(payload), 404

        if msg.startswith("任务启动失败"):
            return jsonify(payload), 500

        if (
            msg in ("任务已在运行", "任务已在运行中", "任务未运行")
            or msg.startswith("停止失败")
            or msg.startswith("任务已结束")
        ):
            return jsonify(payload), 409

        return jsonify(payload), 400

    return jsonify(payload)


def _status_task_signature(tasks):
    return tuple(
        (
            str(task.get("id") or ""),
            str(task.get("name") or ""),
            str(task.get("command") or ""),
            str(task.get("cron") or ""),
            bool(task.get("enabled", True)),
            int(task.get("run_count", 0)),
        )
        for task in tasks
    )


def _status_static_rows(tasks):
    """Cache task fields that cannot change during a status poll."""
    global _STATUS_STATIC_CACHE
    signature = _status_task_signature(tasks)
    with _STATUS_STATIC_CACHE_LOCK:
        if (
            not os.environ.get("FLS_DISABLE_STATUS_CACHE")
            and _STATUS_STATIC_CACHE is not None
            and _STATUS_STATIC_CACHE[0] == signature
        ):
            return _STATUS_STATIC_CACHE[1]

        rows = tuple(
            {
                "id": str(task.get("id") or ""),
                "name": task.get("name"),
                "command": task.get("command"),
                "cron": task.get("cron"),
                "enabled": task.get("enabled", True),
                "run_count": int(task.get("run_count", 0)),
                "idle_process_name": safe_process_name(
                    task.get("name") or task.get("command")
                ),
            }
            for task in tasks
        )
        if not os.environ.get("FLS_DISABLE_STATUS_CACHE"):
            _STATUS_STATIC_CACHE = (signature, rows)
        return rows


def _status_running_signature(tasks, running_tasks):
    return tuple(
        (
            str(task.get("id") or ""),
            bool(running_tasks.get(str(task.get("id") or ""))),
            str((running_tasks.get(str(task.get("id") or "")) or {}).get("pid") or ""),
            str((running_tasks.get(str(task.get("id") or "")) or {}).get("process_name") or ""),
        )
        for task in tasks
    )


@bp.route("/api/status")
def api_status():
    global _STATUS_RESPONSE_CACHE
    result = []

    try:
        tasks = load_tasks_for_read()
        static_rows = _status_static_rows(tasks)
        running_tasks = running_task_snapshot(task.get("id") for task in tasks)
        cache_key = (
            _status_task_signature(tasks),
            _status_running_signature(tasks, running_tasks),
        )
        if not os.environ.get("FLS_DISABLE_STATUS_CACHE"):
            with _STATUS_STATIC_CACHE_LOCK:
                cached = _STATUS_RESPONSE_CACHE
                if cached is not None and cached[0] == cache_key:
                    return Response(cached[1], content_type=cached[2])
        for static in static_rows:
            task_id = static["id"]
            running_info = running_tasks.get(task_id)
            running = running_info is not None
            running_info = running_info or {}
            runtime_process_name = running_info.get("process_name")

            if isinstance(runtime_process_name, str) and runtime_process_name.strip():
                process_name = runtime_process_name
            else:
                process_name = static["idle_process_name"]

            result.append({
                "id": task_id,
                "name": static["name"],
                "command": static["command"],
                "cron": static["cron"],
                "enabled": static["enabled"],
                "running": running,
                "run_count": static["run_count"],
                "pid": running_info.get("pid") if running else None,
                "process_name": process_name,
            })

    except Exception as e:
        return jsonify({
            "ok": False,
            "msg": str(e),
            "tasks": [],
        }), 500

    response = jsonify(result)
    if not os.environ.get("FLS_DISABLE_STATUS_CACHE"):
        with _STATUS_STATIC_CACHE_LOCK:
            _STATUS_RESPONSE_CACHE = (
                cache_key,
                response.get_data(),
                response.content_type,
            )
    return response


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


@bp.route("/api/scheduler/status")
def api_scheduler_status():
    try:
        return jsonify({"ok": True, **get_scheduler_state()})
    except Exception as exc:
        return jsonify({"ok": False, "status": "unknown", "error": str(exc)}), 500


@bp.route("/api/scheduler/reload", methods=["POST"])
def api_scheduler_reload():
    try:
        state = reload_scheduler()
    except Exception as exc:
        try:
            state = get_scheduler_state()
        except Exception:
            state = {
                "status": "failed",
                "status_label": "失败",
                "task_count": 0,
                "scheduled_count": 0,
                "failed_tasks": [],
                "error": str(exc),
                "recovery": "检查调度器状态文件后重新加载。",
                "updated_at": "",
            }
        return jsonify({
            "ok": False,
            "status": state.get("status", "failed"),
            "msg": str(exc),
            "state": state,
        }), 500

    return jsonify({"ok": True, "status": "succeeded", "state": state})


@bp.route("/api/task/action/<action>/<task_id>", methods=["POST"])
def api_task_action(action, task_id):
    try:
        if action in ("run", "stop"):
            data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            if not isinstance(data, dict):
                return jsonify(ok=False, msg="请求必须是对象"), 400
            if "request_id" in data:
                from ..task_bulk import execute_single
                payload, status = execute_single(
                    action, task_id, data, load_tasks=load_tasks, mutate_tasks=mutate_tasks,
                    run_task=run_task_now, stop_task=stop_task_now,
                    cancel_retry=cancel_task_retry, reload_scheduler=reload_scheduler,
                )
                return jsonify(payload), status
        if action == "run":
            ok, msg = run_task_now(task_id, source="manual")
            if not ok and msg.startswith("脚本重命名待恢复"):
                from ..models import get_task
                from ..script_rename import pending_script_rename
                pending = pending_script_rename(task_id=task_id, task=get_task(task_id))
                if pending:
                    return jsonify(ok=False, msg=msg, recovery_url=f"/scripts/rename-recovery/{pending['id']}"), 409
            return _task_action_result(ok, msg)

        if action == "stop":
            if not _task_exists(task_id):
                return _task_action_result(False, "任务不存在")

            ok, msg = stop_task_now(task_id)
            return _task_action_result(ok, msg)

        if action == "toggle":
            from ..task_bulk import execute_toggle
            data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            payload, status = execute_toggle(
                task_id, data, load_tasks=load_tasks, mutate_tasks=mutate_tasks,
                run_task=run_task_now, stop_task=stop_task_now,
                cancel_retry=cancel_task_retry, reload_scheduler=reload_scheduler,
            )
            return jsonify(payload), status

        if action in ("copy", "delete", "clear_collection"):
            from ..task_bulk import execute_single
            data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
            payload, status = execute_single(
                action, task_id, data, load_tasks=load_tasks, mutate_tasks=mutate_tasks,
                run_task=run_task_now, stop_task=stop_task_now,
                cancel_retry=cancel_task_retry, reload_scheduler=reload_scheduler,
            )
            return jsonify(payload), status

        if action == "pin":
            result = {}

            def pin(tasks):
                target = next((item for item in tasks if item.get("id") == task_id), None)
                if target is None:
                    return False
                result["found"] = True

                pinned_count = sum(1 for item in tasks if item.get("pinned"))
                if not target.get("pinned") and pinned_count >= 5:
                    result["limit_exceeded"] = True
                    return False

                target["pinned"] = not target.get("pinned", False)
                target["updated_at"] = now_str()
                result["pinned"] = target["pinned"]
                return True

            _tasks, changed = mutate_tasks(pin)
            if not result.get("found"):
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            if result.get("limit_exceeded"):
                return jsonify({"ok": False, "msg": "最多只能置顶 5 个任务，请先取消一个置顶任务"}), 400

            return jsonify({
                "ok": True,
                "msg": "已置顶" if result["pinned"] else "已取消置顶",
                "pinned": bool(result["pinned"]),
            })

        return jsonify({"ok": False, "msg": "未知操作"}), 400

    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.route("/api/task/bulk-action", methods=["POST"])
def api_task_bulk_action():
    from ..task_bulk import parse_bulk_request, execute_bulk

    try:
        data = request.get_json(silent=True) if request.is_json else {
            "action": request.form.get("action", ""),
            "task_ids": request.form.getlist("task_ids"),
            "request_id": request.form.get("request_id", ""),
        }
        action, task_ids, request_id = parse_bulk_request(data)
    except ValueError as exc:
        invalid = data if isinstance(data, dict) else {}
        values = invalid.get("task_ids", [])
        values = [values] if isinstance(values, str) else values
        count = len({value.strip() for value in values if isinstance(value, str) and value.strip()}) if isinstance(values, list) else 0
        return jsonify({"ok": False, "msg": str(exc), "action": invalid.get("action", ""), "count": count}), 400
    try:
        payload, status = execute_bulk(
            action, task_ids, request_id,
            load_tasks=load_tasks, mutate_tasks=mutate_tasks,
            run_task=run_task_now, stop_task=stop_task_now,
            cancel_retry=cancel_task_retry, reload_scheduler=reload_scheduler,
        )
        if action == "run" and any("脚本重命名待恢复" in row["msg"] for row in payload.get("results", [])):
            from ..models import get_task
            from ..script_rename import pending_script_rename
            for task_id in payload.get("retry_ids", []):
                try:
                    pending = pending_script_rename(task_id=task_id, task=get_task(task_id))
                except Exception as exc:
                    payload["ok"] = False
                    payload["msg"] += f"；恢复入口读取失败：{exc}"
                    status = 500
                    break
                if pending:
                    payload["recovery_url"] = f"/scripts/rename-recovery/{pending['id']}"
                    break
        return jsonify(payload), status
    except Exception as exc:
        return jsonify({"ok": False, "msg": str(exc), "action": action, "count": len(task_ids)}), 500


@bp.route("/api/collection/add-task/<collection_id>", methods=["POST"])
def api_collection_add_task(collection_id):
    from ..models import get_collection
    from ..task_bulk import execute_assign_collection

    data = request.get_json(silent=True) if request.is_json else dict(
        request.form.to_dict(), task_ids=request.form.getlist("task_ids"))
    try:
        payload, status = execute_assign_collection(collection_id, data, get_collection=get_collection,
                                                    load_tasks=load_tasks, mutate_tasks=mutate_tasks)
        return jsonify(payload), status
    except Exception as exc:
        return jsonify(ok=False, msg=str(exc)), 500


@bp.route("/api/collection/delete/<collection_id>", methods=["POST"])
def api_collection_delete(collection_id):
    from ..collection_delete import delete_collection
    from ..models import load_collections, save_collections

    data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    payload, status = delete_collection(collection_id, data, load_collections=load_collections,
        save_collections=save_collections, load_tasks=load_tasks, mutate_tasks=mutate_tasks)
    return jsonify(payload), status


@bp.route("/api/collection/save", methods=["POST"])
def api_collection_save():
    from ..collection_save import save_collection
    from ..models import load_collections, save_collections, unique_collection_name

    data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    payload, status = save_collection(data, load_collections=load_collections,
        save_collections=save_collections, unique_collection_name=unique_collection_name)
    return jsonify(payload), status
