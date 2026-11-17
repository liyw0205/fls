import copy
import json
import re
import threading
import uuid

from .models import TaskSchedulerReloadError
from .paths import DATA_DIR
from .storage import write_json
from .utils import now_str


BULK_LOCK = threading.RLock()
BULK_ACTIONS = ("enable", "disable", "run", "stop", "delete", "clear_collection", "copy", "assign_collection")


class TaskBulkSelectionChanged(ValueError):
    pass


def parse_bulk_request(data):
    if not isinstance(data, dict):
        raise ValueError("批量请求必须是对象")
    action = data.get("action", "")
    if not isinstance(action, str) or action.strip() not in BULK_ACTIONS:
        raise ValueError("未知批量操作")
    values = data.get("task_ids", [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or any(not isinstance(value, str) or "\x00" in value for value in values):
        raise ValueError("任务选择必须是任务 ID 列表")
    task_ids = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not task_ids:
        raise ValueError("请选择任务")
    if action.strip() == "copy" and len(task_ids) != 1:
        raise ValueError("一次只能复制一个任务")
    request_id = data.get("request_id", "")
    if not isinstance(request_id, str) or (request_id and not re.fullmatch(r"[a-f0-9]{32}", request_id)):
        raise ValueError("提交标识无效，请刷新页面后重试")
    return action.strip(), task_ids, request_id


def _result(task_id, status, msg):
    return {"task_id": task_id, "status": status, "msg": msg}


def _with_results(payload, rows):
    payload["results"] = copy.deepcopy(rows)
    payload["completed_ids"] = [row["task_id"] for row in rows if row["status"] in ("succeeded", "skipped")]
    payload["retry_ids"] = [row["task_id"] for row in rows if row["status"] in ("failed", "not_attempted")]
    payload["unknown_ids"] = [row["task_id"] for row in rows if row["status"] == "unknown"]
    return payload


def _record_path(request_id):
    return DATA_DIR / "task_bulk_requests" / (request_id + ".json")


def execute_single(action, task_id, data, **operations):
    if action == "clear_collection":
        return execute_clear_collection(task_id, data, **operations)
    if not isinstance(data, dict):
        return {"ok": False, "msg": "请求必须是对象"}, 400
    if action in ("run", "stop") and "request_id" in data and not data["request_id"]:
        return {"ok": False, "msg": "提交标识不能为空，请刷新页面后重试"}, 400
    try:
        action, ids, request_id = parse_bulk_request({
            "action": action, "task_ids": [task_id], "request_id": data.get("request_id", ""),
        })
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}, 400
    payload, status = execute_bulk(action, ids, request_id, **operations)
    if action in ("run", "stop") and payload.get("results"):
        row = payload["results"][0]
        if action == "stop" and row["status"] == "skipped" and status == 409:
            payload.update(ok=True, msg=row["msg"])
            status = 200
        elif status in (200, 400, 409) and row["status"] != "unknown":
            payload["msg"] = row["msg"]
        if action == "run" and "脚本重命名待恢复" in row["msg"]:
            from .script_rename import pending_script_rename
            try:
                pending = pending_script_rename(task_id=task_id)
            except Exception as exc:
                payload["msg"] += f"；恢复入口读取失败：{exc}"
                status = 500
            else:
                if pending:
                    payload["recovery_url"] = f"/scripts/rename-recovery/{pending['id']}"
    if status == 404:
        payload["msg"] = "任务不存在"
    elif action == "delete":
        if status == 200:
            payload["msg"] = "已删除"
        elif status == 409 and payload.get("results"):
            payload["msg"] = "删除失败：" + payload["results"][0]["msg"]
    return payload, status


def execute_toggle(task_id, data, **operations):
    if not isinstance(data, dict):
        return {"ok": False, "msg": "请求必须是对象"}, 400
    value = data.get("enabled")
    if "enabled" in data and value is None:
        return {"ok": False, "msg": "启用状态不能为空，未执行操作"}, 400
    if value is not None and not (isinstance(value, bool) or isinstance(value, str) and value in ("0", "1")):
        return {"ok": False, "msg": "启用状态无效，未执行操作"}, 400
    if data.get("request_id") and value is None:
        return {"ok": False, "msg": "使用提交标识时必须指定启用状态"}, 400
    try:
        action, ids, request_id = parse_bulk_request({
            "action": "enable" if value in ("1", True) else "disable",
            "task_ids": [task_id], "request_id": data.get("request_id", ""),
        })
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}, 400
    with BULK_LOCK:
        if value is None:
            task = next((task for task in operations["load_tasks"]() if task["id"] == task_id), None)
            if task is None:
                return {"ok": False, "msg": "任务不存在"}, 404
            action = "disable" if task.get("enabled", True) else "enable"
        payload, status = execute_bulk(action, ids, request_id, **operations)
        if status == 404:
            payload["msg"] = "任务不存在"
        if payload.get("saved"):
            payload["enabled"] = action == "enable"
        if status == 200:
            payload["msg"] = "已切换"
        return payload, status


def execute_clear_collection(task_id, data, **operations):
    if not isinstance(data, dict):
        return {"ok": False, "msg": "请求必须是对象"}, 400
    if "request_id" in data and not data["request_id"]:
        return {"ok": False, "msg": "提交标识不能为空，请刷新页面后重试"}, 400
    try:
        action, ids, request_id = parse_bulk_request({
            "action": "clear_collection", "task_ids": [task_id], "request_id": data.get("request_id", ""),
        })
        expected = data.get("expected_collection")
        if "expected_collection" in data and (not isinstance(expected, str) or "\x00" in expected):
            raise ValueError("原合集选择无效，未移出任务")
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}, 400
    payload, status = execute_bulk(action, ids, request_id,
                                   expected_collections={task_id: expected} if expected is not None else None,
                                   **operations)
    if status == 404:
        payload["msg"] = "任务不存在"
    return payload, status


def execute_assign_collection(collection_id, data, *, get_collection, **operations):
    from .models import COLLECTION_MUTATION_LOCK

    try:
        if not isinstance(data, dict):
            raise ValueError("请求必须是对象")
        if "request_id" in data and not data["request_id"]:
            raise ValueError("提交标识不能为空，请刷新页面后重试")
        action, ids, request_id = parse_bulk_request(dict(data, action="assign_collection"))
        expected = data.get("expected_collections")
        if isinstance(expected, str):
            try:
                expected = json.loads(expected)
            except (ValueError, TypeError):
                raise ValueError("原合集选择记录无效，未加入任务") from None
        if "expected_collections" in data:
            if (not isinstance(expected, dict) or any(
                task_id not in expected or not isinstance(expected[task_id], str) or "\x00" in expected[task_id]
                for task_id in ids
            )):
                raise ValueError("原合集选择不完整或无效，请刷新后重新选择")
            expected = {task_id: expected[task_id] for task_id in ids}
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}, 400
    with COLLECTION_MUTATION_LOCK:
        return execute_bulk(action, ids, request_id, target_collection=collection_id,
                            expected_collections=expected, get_collection=get_collection,
                            run_task=None, stop_task=None, cancel_retry=None, reload_scheduler=None, **operations)


def _read_record(request_id):
    try:
        record = json.loads(_record_path(request_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(record, dict) or record.get("request_id") != request_id or not isinstance(record.get("results"), list):
        raise RuntimeError("批量操作记录损坏，未重复执行，请修复记录后重试")
    ids = record.get("task_ids")
    rows = record["results"]
    if (not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids)
            or len(set(ids)) != len(ids) or len(rows) != len(ids)
            or any(not isinstance(row, dict) or row.get("task_id") != task_id
                   or row.get("status") not in ("not_attempted", "unknown", "succeeded", "skipped", "failed")
                   or not isinstance(row.get("msg"), str) for task_id, row in zip(ids, rows))):
        raise RuntimeError("批量操作记录损坏，未重复执行，请修复记录后重试")
    if record.get("action") == "copy":
        copy_ids = record.get("copy_ids")
        if (not isinstance(copy_ids, dict) or set(copy_ids) != set(ids)
                or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value)
                       for value in copy_ids.values())):
            raise RuntimeError("副本标识记录损坏，未重复复制，请修复记录后重试")
    if record.get("action") in ("clear_collection", "assign_collection") and "expected_collections" in record:
        expected = record["expected_collections"]
        if not isinstance(expected, dict) or set(expected) != set(ids) or any(not isinstance(value, str) for value in expected.values()):
            raise RuntimeError("合集选择记录损坏，未重复移出，请修复记录后重试")
    if record.get("action") == "assign_collection" and (
        "expected_collections" not in record or not isinstance(record.get("target_collection"), str)
        or not record["target_collection"]
    ):
        raise RuntimeError("目标合集记录损坏，未重复执行，请修复记录后重试")
    if "payload" in record and (not isinstance(record["payload"], dict)
                               or type(record.get("http_status")) is not int
                               or not 200 <= record["http_status"] <= 599):
        raise RuntimeError("批量操作结果记录损坏，未重复执行，请修复记录后重试")
    return record


def execute_bulk(action, task_ids, request_id, *, load_tasks, mutate_tasks, run_task,
                 stop_task, cancel_retry, reload_scheduler, expected_collections=None,
                 target_collection=None, get_collection=None):
    # Serialize submissions without holding task/storage locks while waiting for processes.
    with BULK_LOCK:
        collection_action = action in ("clear_collection", "assign_collection")
        record = _read_record(request_id) if request_id else None
        if record:
            if record.get("action") != action or record.get("task_ids") != task_ids:
                return {"ok": False, "msg": "提交标识已用于其他选择，未执行操作"}, 409
            if expected_collections is not None and record.get("expected_collections") != expected_collections:
                return {"ok": False, "msg": "提交标识已用于其他合集选择，未执行操作"}, 409
            if action == "assign_collection" and record.get("target_collection") != target_collection:
                return {"ok": False, "msg": "提交标识已用于其他目标合集，未执行操作"}, 409
            if "payload" in record:
                return record["payload"], record["http_status"]
            created_ids = []
            if action in ("copy", "delete", "clear_collection", "assign_collection"):
                actual = {task["id"]: task for task in load_tasks()}
                for row in record["results"]:
                    if action == "copy" and record.get("copy_ids", {}).get(row["task_id"]) in actual:
                        created_ids.append(record["copy_ids"][row["task_id"]])
                        row.update(status="succeeded", msg="副本已保存，请重新加载调度器")
                    elif action == "delete" and row["task_id"] not in actual:
                        row.update(status="succeeded", msg="任务已删除，请重新加载调度器")
                    elif action == "clear_collection" and row["task_id"] in actual and not actual[row["task_id"]].get("collection_id"):
                        row.update(status="succeeded", msg="任务已移出合集")
                    elif action == "assign_collection" and row["task_id"] in actual and actual[row["task_id"]].get("collection_id") == target_collection:
                        row.update(status="succeeded", msg="任务已加入合集")
            payload = _with_results({
                "ok": False, "action": action, "count": len(task_ids), "request_id": request_id,
                "msg": "上次请求中断；已完成项不会重复执行，结果未知项请先查看任务历史。",
            }, record["results"])
            if action in ("copy", "delete"):
                payload["saved"] = bool(payload["completed_ids"]) or None
                payload["created_ids"] = created_ids
                payload["msg"] = "上次请求中断，已核对任务文件；未重复复制或删除。已完成项只需重载调度器，未知项请确认状态后重试。"
            if action == "clear_collection":
                payload["saved"] = bool(payload["completed_ids"]) or None
                if len(task_ids) == 1:
                    payload["expected_collection"] = record.get("expected_collections", {}).get(task_ids[0])
                payload["msg"] = "上次请求中断，已核对任务合集归属，未重复移出；未知项请刷新确认后重试。"
            if action == "assign_collection":
                payload.update(saved=True if len(payload["completed_ids"]) == len(task_ids) else None,
                               target_collection=target_collection, expected_collections=record["expected_collections"],
                               msg="上次请求中断，已核对任务合集归属，未重复加入；未知项请刷新确认后重试。")
            if action in ("enable", "disable", "delete", "copy") and (payload["unknown_ids"] or payload["completed_ids"]):
                payload["reload_required"] = True
            return payload, 409

        if action == "assign_collection":
            if not target_collection or get_collection is None:
                return {"ok": False, "msg": "请通过合集加入入口选择目标合集"}, 400
            if not get_collection(target_collection):
                return _with_results({"ok": False, "saved": False, "action": action,
                                      "target_collection": target_collection, "reselection_required": True,
                                      "msg": "目标合集不存在，未加入任务，请重新选择合集和任务"},
                                     [_result(task_id, "not_attempted", "目标合集不存在，未执行") for task_id in task_ids]), 404
        task_map = {str(task.get("id") or ""): task for task in load_tasks()}
        missing = [task_id for task_id in task_ids if task_id not in task_map]
        if missing:
            return _with_results({"ok": False, "msg": f"有 {len(missing)} 个任务不存在，请刷新后重试",
                    "action": action, "count": len(task_ids), "missing_count": len(missing),
                    "missing_ids": missing}, [_result(task_id, "not_attempted", "选择包含不存在的任务，未执行") for task_id in task_ids]), 404
        if action == "clear_collection" and expected_collections is not None and any(
            str(task_map[task_id].get("collection_id") or "") not in ("", expected_collections[task_id])
            for task_id in task_ids
        ):
            return _with_results({"ok": False, "saved": False, "action": action,
                                  "expected_collection": expected_collections[task_ids[0]] if len(task_ids) == 1 else None,
                                  "msg": "任务已加入其他合集，未移出，请刷新后重新选择"},
                                 [_result(task_id, "failed", "原合集选择已变化") for task_id in task_ids]), 409
        if action == "assign_collection" and expected_collections is not None and any(
            str(task_map[task_id].get("collection_id") or "") not in (target_collection, expected_collections[task_id])
            for task_id in task_ids
        ):
            return _with_results({"ok": False, "saved": False, "action": action,
                                  "target_collection": target_collection, "expected_collections": expected_collections,
                                  "msg": "任务合集归属已变化，未加入，请刷新后重新选择"},
                                 [_result(task_id, "failed", "原合集选择已变化") for task_id in task_ids]), 409

        rows = [_result(task_id, "not_attempted", "尚未执行") for task_id in task_ids]
        record = {"request_id": request_id, "action": action, "task_ids": task_ids,
                  "results": rows, "created_at": now_str()}
        if action in ("clear_collection", "assign_collection"):
            record["expected_collections"] = expected_collections or {
                task_id: str(task_map[task_id].get("collection_id") or "") for task_id in task_ids
            }
        if action == "assign_collection":
            record["target_collection"] = target_collection
        if action == "copy":
            record["copy_ids"] = {task_id: uuid.uuid4().hex for task_id in task_ids}

        def checkpoint():
            if request_id:
                write_json(_record_path(request_id), record)

        def finish(payload, status):
            _with_results(payload, rows)
            if action == "clear_collection" and len(task_ids) == 1:
                payload["expected_collection"] = record["expected_collections"][task_ids[0]]
            if action == "assign_collection":
                payload.update(target_collection=target_collection, expected_collections=record["expected_collections"])
            failures = [f"{task_map[row['task_id']].get('name') or row['task_id']}: {row['msg']}"
                        for row in rows if row["status"] in ("failed", "unknown")]
            payload.setdefault("failed_count", len(failures))
            payload.setdefault("failures", failures)
            if action in ("run", "stop", "delete"):
                field = {"run": "submitted_count", "stop": "stopped_count", "delete": "deleted_count"}[action]
                payload.setdefault(field, sum(row["status"] == "succeeded" for row in rows))
            if action == "stop":
                payload.setdefault("skipped_count", sum(row["status"] == "skipped" for row in rows))
            if action == "delete":
                payload["stopped_ids"] = stopped_ids
            if action == "copy":
                payload["created_ids"] = [record["copy_ids"][row["task_id"]] for row in rows if row["status"] == "succeeded"]
                payload["copied_count"] = len(payload["created_ids"])
            if request_id:
                payload["request_id"] = request_id
                record.update(payload=payload, http_status=status)
                try:
                    checkpoint()
                except Exception as exc:
                    payload = dict(payload, ok=False, msg=payload["msg"] + f"；操作结果记录保存失败：{exc}")
                    return payload, 500
            return payload, status

        # A failed claim must not start tasks or modify the task file.
        checkpoint()
        stopped_ids = []
        count = 0
        skipped = 0
        payload = {"ok": True, "action": action, "count": len(task_ids)}
        status = 200
        try:
            if action in ("run", "stop", "delete"):
                for row in rows:
                    task_id = row["task_id"]
                    row.update(status="unknown", msg="执行结果尚未确认，请查看任务历史")
                    checkpoint()
                    try:
                        ok, msg = run_task(task_id, source="manual") if action == "run" else stop_task(task_id)
                    except Exception as exc:
                        ok, msg = False, f"执行结果未知：{exc}"
                    else:
                        row["status"] = "succeeded" if ok else "failed"
                        if action != "run" and msg == "任务未运行":
                            row["status"] = "skipped"
                            skipped += 1
                    row["msg"] = msg
                    if row["status"] in ("succeeded", "skipped"):
                        if action == "delete":
                            stopped_ids.append(task_id)
                            row.update(status="not_attempted", msg="任务已停止，尚未删除")
                        elif ok:
                            count += 1
                    checkpoint()

            if action in ("enable", "disable", "clear_collection", "assign_collection", "delete", "copy"):
                selected = set(stopped_ids if action == "delete" else task_ids)
                selected_rows = [row for row in rows if row["task_id"] in selected]
                state = {"saved": False, "changed": 0}
                field = "collection_id" if collection_action else "enabled"
                value = (target_collection or "") if collection_action else action == "enable"

                def update(tasks):
                    existing = {task["id"] for task in tasks}
                    if not selected.issubset(existing):
                        raise TaskBulkSelectionChanged("任务选择已变化，未保存，请刷新后重试")
                    if action == "clear_collection" and any(
                        str(task.get("collection_id") or "") not in ("", record["expected_collections"][task["id"]])
                        for task in tasks if task["id"] in selected
                    ):
                        raise TaskBulkSelectionChanged("任务已加入其他合集，未移出，请刷新后重新选择")
                    if action == "assign_collection" and any(
                        str(task.get("collection_id") or "") not in (target_collection, record["expected_collections"][task["id"]])
                        for task in tasks if task["id"] in selected
                    ):
                        raise TaskBulkSelectionChanged("任务合集归属已变化，未加入，请刷新后重新选择")
                    if action == "delete":
                        from .task_runner import is_running
                        if any(is_running(task_id) for task_id in selected):
                            raise TaskBulkSelectionChanged("任务在删除前重新启动，未删除，请停止后重试")
                        tasks[:] = [task for task in tasks if task["id"] not in selected]
                        state["changed"] = len(selected)
                    elif action == "copy":
                        from .script_rename import pending_script_rename
                        source = next(task for task in tasks if task["id"] in selected)
                        if record["copy_ids"][source["id"]] in existing:
                            raise TaskBulkSelectionChanged("副本 ID 已存在，未复制任务")
                        pending = pending_script_rename(task_id=source["id"], task=source)
                        if pending:
                            payload["recovery_url"] = f"/scripts/rename-recovery/{pending['id']}"
                            raise TaskBulkSelectionChanged("脚本重命名待恢复，未复制任务")
                        new_task = copy.deepcopy(source)
                        base_name = str(source.get("name") or "未命名任务").strip() or "未命名任务"
                        new_task.update(id=record["copy_ids"][source["id"]],
                                        name=base_name + "-copy",
                                        run_count=0, pinned=False, created_at=now_str(), updated_at=now_str())
                        new_task.pop("last_run_at", None)
                        tasks.append(new_task)
                        state.update(changed=1, copy_name=new_task["name"])
                    else:
                        for task in tasks:
                            if task["id"] not in selected:
                                continue
                            if task.get(field, True if field == "enabled" else "") != value:
                                task[field] = value
                                task["updated_at"] = now_str()
                                if action == "assign_collection":
                                    task["pinned"] = False
                                state["changed"] += 1
                    return state["changed"] > 0

                def after_write():
                    state["saved"] = True
                    if action == "disable":
                        for task_id in task_ids:
                            cancel_retry(task_id, "任务已禁用")

                if selected:
                    for row in selected_rows:
                        row.update(status="unknown", msg="保存结果尚未确认")
                    checkpoint()
                    try:
                        mutate_tasks(update, None if collection_action else reload_scheduler, after_write)
                    except Exception as exc:
                        # Atomic publication may succeed before a subsequent confirmation fails.
                        if not state["saved"] and not isinstance(exc, TaskBulkSelectionChanged):
                            try:
                                actual = {task["id"]: task for task in load_tasks()}
                                state["saved"] = all(
                                    record["copy_ids"][task_id] in actual if action == "copy" else
                                    task_id not in actual if action == "delete" else
                                    task_id in actual and actual[task_id].get(field) == value
                                    for task_id in selected
                                )
                            except Exception:
                                state["saved"] = None
                        if state["saved"] or isinstance(exc, TaskSchedulerReloadError):
                            for row in selected_rows:
                                row.update(status="succeeded", msg="已保存")
                            payload.update(saved=True, reload_required=not collection_action)
                            payload["msg"] = f"任务已保存，后续应用失败：{exc}"
                            count = state["changed"]
                            status = 503
                        else:
                            for row in selected_rows:
                                row.update(status="unknown" if state["saved"] is None else "failed", msg=str(exc))
                            prefix = "任务保存失败"
                            if collection_action and state["saved"] is False:
                                prefix = "合集成员更新失败，任务未更改" if action == "assign_collection" else "任务合集归属未更改，保存失败"
                            payload.update(saved=state["saved"], msg=f"{prefix}：{exc}")
                            status = 409 if isinstance(exc, TaskBulkSelectionChanged) else 500
                        payload["ok"] = False
                    else:
                        for row in selected_rows:
                            row.update(status="succeeded", msg="已保存")
                        payload["saved"] = True
                        count = state["changed"]

            failures = [f"{task_map[row['task_id']].get('name') or row['task_id']}: {row['msg']}"
                        for row in rows if row["status"] in ("failed", "unknown")]
            if action == "delete":
                payload["stopped_ids"] = stopped_ids

            if action == "copy":
                parts = [f"已复制为 {state.get('copy_name', '')}" if count else "任务未复制"]
            elif action in ("enable", "disable", "clear_collection", "assign_collection"):
                verb = {"enable": "启用", "disable": "禁用", "clear_collection": "取出", "assign_collection": "加入合集"}[action]
                payload.update(updated_count=count)
                parts = [f"已{verb} {count} 个任务"]
            else:
                verb, field, failed_label = {
                    "run": ("提交运行", "submitted_count", "未运行"),
                    "stop": ("结束", "stopped_count", "结束失败"),
                    "delete": ("删除", "deleted_count", "删除失败"),
                }[action]
                payload.update({field: count, "failed_count": len(failures), "failures": failures})
                parts = [f"已{verb} {count} 个任务"]
                if action == "stop":
                    payload["skipped_count"] = skipped
                    if skipped:
                        parts.append(f"跳过 {skipped} 个未运行任务")
                if failures:
                    parts.append(f"{len(failures)} 个{failed_label}：{'；'.join(failures[:3])}")
                    if len(failures) > 3:
                        parts.append(f"等 {len(failures)} 个")
                if status == 200 and count == 0 and (failures or (action == "stop" and skipped)):
                    payload["ok"] = False
                    status = 409
                    if action == "run" and failures:
                        if all("任务启动失败" in item for item in failures):
                            status = 500
                        elif all("命令解析失败" in item for item in failures):
                            status = 400
            payload.setdefault("msg", "；".join(parts))
        except Exception as exc:
            payload.update(ok=False, msg=f"批量操作记录保存失败，未继续执行：{exc}")
            status = 500
        return finish(payload, status)
