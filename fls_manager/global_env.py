import hashlib
import json
import re
import uuid
from contextlib import nullcontext

from .models import TASKS_MUTATION_LOCK
from .paths import DATA_DIR
from .storage import storage_transaction, write_json
from .utils import parse_env_text


def env_version(env, keys=None):
    value = env if keys is None else {key: env.get(key) for key in keys}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_variable(key, value=""):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise ValueError("变量名无效，请使用字母、数字和下划线，且不能以数字开头")
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("变量值无效，不能包含 NUL 字符")


def mutate_global_env(data, *, load_global_env, save_global_env, load_tasks):
    if not isinstance(data, dict):
        return {"ok": False, "msg": "请求必须是对象", "saved": False, "resolved": True}, 400
    request_id = data.get("request_id", uuid.uuid4().hex)
    action = data.get("action")
    version = data.get("version")
    try:
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("提交标识无效，请刷新后重试")
        if action not in ("new", "edit", "delete", "replace", "import"):
            raise ValueError("未知变量操作")
        if "version" in data and (not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version)):
            return {"ok": False, "msg": "原变量版本无效，请刷新后重试", "saved": False, "resolved": True,
                    "request_id": request_id, "invalid_field": "version"}, 400
        confirm = data.get("confirm_retry", False)
        if not (isinstance(confirm, bool) or isinstance(confirm, str) and confirm in ("0", "1")):
            raise ValueError("恢复确认无效")
        values = {"action": action, "version": version}
        if action in ("new", "edit"):
            key = data.get("key", "")
            value = data.get("value", "")
            validate_variable(key, value)
            values.update(key=key, value=value)
        if action in ("edit", "delete"):
            original = data.get("original_key", "")
            validate_variable(original)
            values["original_key"] = original
        if action == "replace":
            if not isinstance(data.get("env_text"), str):
                raise ValueError("变量文本无效")
            replacement = parse_env_text(data["env_text"], strict=True)
            for name, value in replacement.items():
                validate_variable(name, value)
            values["env_text"] = data["env_text"]
        if action == "import":
            items = data.get("items")
            if not isinstance(items, list) or not items or any(not isinstance(item, str) or "::" not in item for item in items):
                raise ValueError("请选择有效的任务变量，未导入")
            items = list(dict.fromkeys(items))
            for item in items:
                task_id, key = item.split("::", 1)
                if not task_id or "\x00" in task_id:
                    raise ValueError("任务选择无效，未导入")
                validate_variable(key)
            overwrite = data.get("overwrite", False)
            if not (isinstance(overwrite, bool) or isinstance(overwrite, str) and overwrite in ("0", "1")):
                raise ValueError("覆盖选择无效，未导入")
            sources = data.get("sources")
            if isinstance(sources, str):
                try:
                    sources = json.loads(sources)
                except ValueError:
                    raise ValueError("原任务变量选择无效，未导入") from None
            if "sources" in data:
                if not isinstance(sources, dict) or any(not isinstance(sources.get(item), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", sources[item]) for item in items):
                    raise ValueError("原任务变量选择不完整，未导入")
                sources = {item: sources[item] for item in items}
            values.update(items=items, overwrite=overwrite in (True, "1"), sources=sources)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc), "saved": False, "resolved": True, "request_id": request_id}, 400

    input_version = env_version(values)
    path = DATA_DIR / "global_env_requests" / (request_id + ".json")
    with TASKS_MUTATION_LOCK if action == "import" else nullcontext(), storage_transaction():
        record = None
        def result(message, status, saved=False, **extra):
            return {"ok": status == 200, "msg": message, "saved": saved, "resolved": saved is not None,
                    "request_id": request_id, "action": action, **extra}, status

        def complete(status=200, message="全局变量已保存"):
            record["state"] = "completed"
            try:
                write_json(path, record)
            except Exception:
                return result("全局变量已保存，结果记录失败；请查询原提交", 503, True)
            return result(message, status, True, changed_count=record["changed_count"], skipped_count=record["skipped_count"])

        try:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except ValueError:
                raise RuntimeError("变量操作记录损坏，请修复记录后重试") from None
            else:
                if (not isinstance(record, dict) or record.get("request_id") != request_id
                    or record.get("state") not in ("prepared", "failed", "completed")
                    or record.get("action") not in ("new", "edit", "delete", "replace", "import")
                    or record.get("scope") not in ("keys", "all") or not isinstance(record.get("keys"), list)
                    or any(not isinstance(key, str) for key in record["keys"])
                    or any(not isinstance(record.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                           for key in ("before", "after", "input_version"))
                    or any(type(record.get(key)) is not int or record[key] < 0 for key in ("changed_count", "skipped_count"))):
                    raise RuntimeError("变量操作记录损坏，请修复记录后重试")
                if record["input_version"] != input_version or record["action"] != action:
                    return result("提交标识已用于其他内容，请先查询原提交", 409, None)
                if record["state"] == "completed":
                    return result("变量操作已完成，未重复执行", 200, True,
                                  changed_count=record["changed_count"], skipped_count=record["skipped_count"])

            env = load_global_env()
            if record:
                actual = env_version(env, None if record["scope"] == "all" else record["keys"])
                if actual == record["after"]:
                    return complete()
                if actual != record["before"]:
                    return result("原提交结果无法确认，当前变量已变化，未重复执行；请核对后合并", 409, None,
                                  resolved=True, conflict=True, current=env)
                if record["state"] == "prepared" and confirm not in (True, "1"):
                    return result("上次变量操作中断，请核对当前内容并确认后重试", 409, None, confirm_required=True)

            check_keys = [values["original_key"]] if action in ("edit", "delete") else None
            if action == "edit" and values["original_key"] not in env:
                return result("原变量已删除，草稿未保存", 404)
            if version is not None and env_version(env, check_keys) != version:
                return result("变量已变化，未覆盖当前内容，请核对后重试", 409, conflict=True, current=env)
            desired = dict(env)
            skipped = 0
            if action == "new":
                if values["key"] in env:
                    return result("同名变量已存在，未覆盖；请修改变量名或进入编辑", 409, conflict=True, current=env)
                desired[values["key"]] = values["value"]
            elif action == "edit":
                if values["key"] != values["original_key"] and values["key"] in env:
                    return result("目标变量名已存在，未覆盖，也未删除原变量", 409, conflict=True, current=env)
                desired.pop(values["original_key"])
                desired[values["key"]] = values["value"]
            elif action == "delete":
                desired.pop(values["original_key"], None)
            elif action == "replace":
                desired = replacement
            else:
                tasks = {task["id"]: task for task in load_tasks()}
                selected = {}
                for item in values["items"]:
                    task_id, key = item.split("::", 1)
                    task_env = tasks.get(task_id, {}).get("env", {})
                    if key not in task_env:
                        return result("选择包含已删除的任务或变量，整批未导入", 404)
                    value = task_env[key]
                    validate_variable(key, value)
                    if values["sources"] is not None and values["sources"][item] != env_version({key: value}):
                        return result("所选任务变量已变化，整批未导入，请重新核对选择", 409, conflict=True, current=env)
                    if key in selected and selected[key] != value:
                        return result("选择包含值不同的同名变量，整批未导入，请只选择一个来源", 400)
                    selected[key] = value
                for key, value in selected.items():
                    if key in env and not values["overwrite"]:
                        skipped += 1
                    else:
                        desired[key] = value
            if record and env_version(desired, None if record["scope"] == "all" else record["keys"]) != record["after"]:
                return result("原提交的目标值已变化，未按新内容重试；请重新核对选择", 409,
                              conflict=True, current=env)
            keys = sorted(key for key in set(env) | set(desired) if env.get(key) != desired.get(key))
            scope = "all" if action in ("replace", "import") else "keys"
            watched = keys or check_keys or [values.get("key", "")]
            record = {"request_id": request_id, "action": action, "input_version": input_version,
                      "scope": scope, "keys": watched, "state": "prepared", "changed_count": len(keys), "skipped_count": skipped,
                      "before": env_version(env, None if scope == "all" else watched),
                      "after": env_version(desired, None if scope == "all" else watched)}
            try:
                write_json(path, record)
            except Exception:
                return result("请求记录保存失败，变量未更改；请确认后重试", 500, confirm_required=True)
            try:
                if keys:
                    save_global_env(desired)
            except Exception:
                actual = env_version(load_global_env(), None if scope == "all" else watched)
                if actual == record["after"]:
                    return complete(503, "变量已保存，保存确认失败，请查看当前变量")
                if actual == record["before"]:
                    record["state"] = "failed"
                    try:
                        write_json(path, record)
                    except Exception:
                        return result("变量未保存，恢复记录写入失败；请确认后重试", 500, confirm_required=True)
                    return result("变量保存失败，原有内容未更改；草稿已保留", 500)
                return result("变量保存结果未确认，请先查询原提交", 500, None)
            return complete(message="全局变量已保存" if action != "delete" else "变量已删除")
        except ValueError as exc:
            return result(str(exc), 400)
        except Exception:
            return result("变量存储或恢复记录无法读取，未继续执行，请检查文件后查询原提交", 500, None)
