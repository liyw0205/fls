import copy
import os
import threading
import uuid

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


def _bulk_failure_status(action, failures):
    if action == "run":
        messages = [str(item or "") for item in failures]
        if messages and all("任务启动失败" in item for item in messages):
            return 500
        if messages and all("命令解析失败" in item for item in messages):
            return 400

    return 409


def _bulk_response(action, task_ids, payload, failures=None, completed_count=0, blocked_count=0):
    failures = failures or []
    if (failures or blocked_count) and completed_count == 0:
        payload["ok"] = False
        return jsonify(payload), _bulk_failure_status(action, failures)
    return jsonify(payload)


def _delete_stop_failed(ok, msg):
    return not ok and msg != "任务未运行"


def _bulk_payload(action, task_ids, msg, **extra):
    payload = {
        "ok": True,
        "msg": msg,
        "action": action,
        "count": len(task_ids),
    }
    payload.update(extra)

    return payload


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
        if action == "run":
            ok, msg = run_task_now(task_id, source="manual")
            return _task_action_result(ok, msg)

        if action == "stop":
            if not _task_exists(task_id):
                return _task_action_result(False, "任务不存在")

            ok, msg = stop_task_now(task_id)
            return _task_action_result(ok, msg)

        if action == "toggle":
            result = {}

            def toggle(tasks):
                task = next((item for item in tasks if item.get("id") == task_id), None)
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
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            return jsonify({
                "ok": True,
                "msg": "已切换",
                "enabled": bool(result["enabled"]),
            })

        if action == "copy":
            result = {}

            def copy_task(tasks):
                result["task"] = _copy_task(tasks, task_id)
                return result["task"] is not None

            _tasks, changed = mutate_tasks(copy_task, reload_scheduler)
            new_task = result.get("task")
            if not changed or not new_task:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            return jsonify({
                "ok": True,
                "msg": f"已复制为 {new_task.get('name')}",
            })

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

        if action == "delete":
            tasks = load_tasks()
            found = any(t.get("id") == task_id for t in tasks)

            if not found:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            ok, msg = stop_task_now(task_id)

            if _delete_stop_failed(ok, msg):
                return jsonify({
                    "ok": False,
                    "msg": f"删除失败：{msg}",
                }), 409

            def remove_task(current_tasks):
                remaining = [task for task in current_tasks if task.get("id") != task_id]
                if len(remaining) == len(current_tasks):
                    return False
                current_tasks[:] = remaining
                return True

            _tasks, changed = mutate_tasks(remove_task, reload_scheduler)
            if not changed:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404

            return jsonify({"ok": True, "msg": "已删除"})

        return jsonify({"ok": False, "msg": "未知操作"}), 400

    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@bp.route("/api/task/bulk-action", methods=["POST"])
def api_task_bulk_action():
    action = ""
    task_ids = []

    try:
        data = request.get_json(silent=True) or {}
        action = str(data.get("action") or request.form.get("action") or "").strip()

        raw_task_ids = data.get("task_ids")
        if raw_task_ids is None:
            raw_task_ids = request.form.getlist("task_ids")

        task_ids = _unique_task_ids(raw_task_ids)

        if action not in ("enable", "disable", "run", "stop", "delete", "clear_collection"):
            return jsonify({
                "ok": False,
                "msg": "未知批量操作",
                "action": action,
                "count": len(task_ids),
            }), 400

        if not task_ids:
            return jsonify({
                "ok": False,
                "msg": "请选择任务",
                "action": action,
                "count": 0,
            }), 400

        tasks = load_tasks()
        task_map = {str(task.get("id") or ""): task for task in tasks}
        missing = [task_id for task_id in task_ids if task_id not in task_map]

        if missing:
            return jsonify({
                "ok": False,
                "msg": f"有 {len(missing)} 个任务不存在，请刷新后重试",
                "action": action,
                "count": len(task_ids),
                "missing_count": len(missing),
                "missing_ids": missing,
            }), 404

        selected = set(task_ids)

        if action in ("enable", "disable"):
            enabled = action == "enable"
            result = {"changed_count": 0}

            def set_enabled(current_tasks):
                for task in current_tasks:
                    if task.get("id") in selected and bool(task.get("enabled", True)) != enabled:
                        task["enabled"] = enabled
                        task["updated_at"] = now_str()
                        result["changed_count"] += 1
                return result["changed_count"] > 0

            def cancel_disabled_retries():
                if not enabled:
                    for task_id in task_ids:
                        cancel_task_retry(task_id, "任务已禁用")

            mutate_tasks(set_enabled, reload_scheduler, cancel_disabled_retries)
            changed_count = result["changed_count"]

            return jsonify(_bulk_payload(
                action,
                task_ids,
                f"已{'启用' if enabled else '禁用'} {changed_count} 个任务",
                updated_count=changed_count,
            ))

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

            return _bulk_response(
                action,
                task_ids,
                _bulk_payload(
                    action,
                    task_ids,
                    "；".join(parts),
                    submitted_count=ok_count,
                    failed_count=len(failed),
                    failures=failed,
                ),
                failures=failed,
                completed_count=ok_count,
            )

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

            return _bulk_response(
                action,
                task_ids,
                _bulk_payload(
                    action,
                    task_ids,
                    "；".join(parts),
                    stopped_count=ok_count,
                    skipped_count=skipped,
                    failed_count=len(failed),
                    failures=failed,
                ),
                failures=failed,
                completed_count=ok_count,
                blocked_count=skipped,
            )

        if action == "delete":
            deleted_ids = []
            failed = []

            for task_id in task_ids:
                ok, msg = stop_task_now(task_id)

                if not _delete_stop_failed(ok, msg):
                    deleted_ids.append(task_id)
                    continue

                task = task_map.get(task_id) or {}
                name = task.get("name") or task.get("command") or task_id
                failed.append(f"{name}: {msg}")

            deleted = set(deleted_ids)

            if deleted:
                def remove_deleted(current_tasks):
                    remaining = [task for task in current_tasks if task.get("id") not in deleted]
                    if len(remaining) == len(current_tasks):
                        return False
                    current_tasks[:] = remaining
                    return True

                mutate_tasks(remove_deleted, reload_scheduler)

            parts = [f"已删除 {len(deleted_ids)} 个任务"]
            if failed:
                parts.append(f"{len(failed)} 个删除失败：{'；'.join(failed[:3])}")
                if len(failed) > 3:
                    parts.append(f"等 {len(failed)} 个")

            return _bulk_response(
                action,
                task_ids,
                _bulk_payload(
                    action,
                    task_ids,
                    "；".join(parts),
                    deleted_count=len(deleted_ids),
                    failed_count=len(failed),
                    failures=failed,
                ),
                failures=failed,
                completed_count=len(deleted_ids),
            )

        if action == "clear_collection":
            result = {"changed_count": 0}

            def clear_collection(current_tasks):
                for task in current_tasks:
                    if task.get("id") in selected and str(task.get("collection_id") or ""):
                        task["collection_id"] = ""
                        task["updated_at"] = now_str()
                        result["changed_count"] += 1
                return result["changed_count"] > 0

            try:
                mutate_tasks(clear_collection)
            except Exception as e:
                return jsonify({
                    "ok": False,
                    "msg": f"任务合集归属未更改，保存失败：{e}",
                    "action": action,
                    "count": len(task_ids),
                }), 500
            changed_count = result["changed_count"]

            return jsonify(_bulk_payload(
                action,
                task_ids,
                f"已取出 {changed_count} 个任务",
                updated_count=changed_count,
            ))

        return jsonify({"ok": False, "msg": "未知批量操作"}), 400

    except Exception as e:
        return jsonify({
            "ok": False,
            "msg": str(e),
            "action": action,
            "count": len(task_ids),
        }), 500
