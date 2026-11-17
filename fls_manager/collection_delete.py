import hashlib
import json
import re

from .models import COLLECTION_MUTATION_LOCK, TASKS_MUTATION_LOCK
from .paths import DATA_DIR
from .storage import write_json
from .utils import now_str


def collection_version(collection):
    return hashlib.sha256(json.dumps(collection, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def delete_collection(collection_id, data, *, load_collections, save_collections, load_tasks, mutate_tasks):
    if not isinstance(data, dict):
        return {"ok": False, "msg": "请求必须是对象"}, 400
    request_id = data.get("request_id", "")
    if not isinstance(request_id, str) or ("request_id" in data and not re.fullmatch(r"[a-f0-9]{32}", request_id)):
        return {"ok": False, "msg": "提交标识无效，请刷新后重试"}, 400
    expected = data.get("expected_members")
    if isinstance(expected, str):
        try:
            expected = json.loads(expected)
        except ValueError:
            return {"ok": False, "msg": "原成员选择无效，未删除合集"}, 400
    if "expected_members" in data and (
        not isinstance(expected, list) or any(not isinstance(item, str) or not item or "\x00" in item for item in expected)
        or len(set(expected)) != len(expected)
    ):
        return {"ok": False, "msg": "原成员选择无效，未删除合集"}, 400
    version = data.get("collection_version")
    if "collection_version" in data and (not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version)):
        return {"ok": False, "msg": "合集版本无效，未删除合集"}, 400

    path = DATA_DIR / "collection_delete_requests" / (request_id + ".json") if request_id else None
    with COLLECTION_MUTATION_LOCK, TASKS_MUTATION_LOCK:
        record = None

        def result(msg, status, *, saved=False, members_saved=False, reselect=False):
            state = "succeeded" if saved else "unknown" if saved is None else "failed"
            payload = {
                "ok": status == 200, "msg": msg, "action": "delete_collection", "request_id": request_id,
                "saved": saved, "members_saved": members_saved, "reload_required": False,
                "reselection_required": reselect, "resume_request": bool(record),
                "results": [{"task_id": collection_id, "status": state, "msg": msg}],
                "completed_ids": [collection_id] if saved else [],
                "retry_ids": [collection_id] if saved is False else [],
                "unknown_ids": [collection_id] if saved is None else [],
            }
            return payload, status

        try:
            if path:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if record is None:
                        raise RuntimeError("合集删除记录损坏，未继续删除，请修复记录后重试")
                except FileNotFoundError:
                    pass
                if record is not None:
                    if (not isinstance(record, dict) or record.get("request_id") != request_id
                        or not isinstance(record.get("collection"), dict)
                        or not isinstance(record.get("members"), list)
                        or any(not isinstance(item, str) or not item or "\x00" in item for item in record["members"])
                        or len(set(record["members"])) != len(record["members"])
                        or any(key in record and type(record[key]) is not bool for key in ("completed", "members_cleared"))):
                        raise RuntimeError("合集删除记录损坏，未继续删除，请修复记录后重试")
                    if record["collection"].get("id") != collection_id:
                        return result("提交标识已用于其他合集，未删除", 409, reselect=True)
                    if ((expected is not None and sorted(expected) != sorted(record["members"]))
                        or (version is not None and version != collection_version(record["collection"]))):
                        return result("提交标识已用于其他合集选择，未删除", 409, reselect=True)
                    if record.get("completed") is True:
                        return result("合集已删除，未重复执行", 200, saved=True, members_saved=True)

            collections = load_collections()
            collection = next((item for item in collections if item["id"] == collection_id), None)
            if not collection and record is None:
                return result("合集不存在，未删除，请重新选择", 404, reselect=True)
            tasks = load_tasks()
            members = sorted(task["id"] for task in tasks if task.get("collection_id") == collection_id)
            if record is None:
                if ((expected is not None and sorted(expected) != members)
                    or (version is not None and collection_version(collection) != version)):
                    return result("合集或成员已变化，未删除，请刷新后重新选择", 409, reselect=True)
                record = {"request_id": request_id, "collection": collection, "members": members, "created_at": now_str()}
                if path:
                    try:
                        write_json(path, record)
                    except Exception as exc:
                        return result(f"合集删除请求保存失败，合集和任务未更改：{exc}", 500)
            elif ((collection is not None and collection != record["collection"])
                  or not set(members).issubset(record["members"])
                  or (record.get("members_cleared") and members)):
                return result("合集或成员已变化，未继续删除，请刷新后重新选择", 409,
                              members_saved=not members, reselect=True)

            def clear(tasks):
                current = [task for task in tasks if task.get("collection_id") == collection_id]
                if (not {task["id"] for task in current}.issubset(record["members"])
                    or (record.get("members_cleared") and current)):
                    raise ValueError("合集成员已变化，未继续删除，请刷新后重新选择")
                for task in current:
                    task.update(collection_id="", updated_at=now_str())
                return bool(current)

            try:
                mutate_tasks(clear)
            except Exception as exc:
                # Publication can succeed before its caller observes an error.
                remaining = [task for task in load_tasks() if task.get("collection_id") == collection_id]
                return result(("合集未删除，任务成员关系未更改" if remaining else "任务已移出该合集，合集删除未完成") + f"：{exc}",
                              409 if isinstance(exc, ValueError) else 500, members_saved=not remaining,
                              reselect=isinstance(exc, ValueError))
            record["members_cleared"] = True
            if path:
                try:
                    write_json(path, record)
                except Exception as exc:
                    return result(f"任务已移出该合集，恢复记录保存失败，合集尚未删除：{exc}", 500, members_saved=True)
            try:
                if collection is not None:
                    save_collections([item for item in collections if item["id"] != collection_id])
            except Exception as exc:
                remains = any(item["id"] == collection_id for item in load_collections())
                return result(("合集删除未完成，任务已移出该合集；合集记录仍保留" if remains else "合集和成员关系已删除，保存确认失败") + f"：{exc}",
                              500 if remains else 503, saved=not remains, members_saved=True)
            record["completed"] = True
            if path:
                try:
                    write_json(path, record)
                except Exception as exc:
                    return result(f"合集已删除，结果记录失败；可查询本次结果：{exc}", 500, saved=True, members_saved=True)
            return result("合集已删除，任务已移出该合集", 200, saved=True, members_saved=True)
        except Exception as exc:
            payload, status = result(f"合集删除未确认，未继续执行：{exc}", 500, saved=None)
            payload["resume_request"] = True
            return payload, status
