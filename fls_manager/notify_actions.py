import json
import re
import uuid

from .config import normalize_config_data
from .models import TASKS_MUTATION_LOCK
from .paths import CONFIG_FILE, DATA_DIR
from .proxy_actions import proxy_version
from .storage import storage_transaction, write_json
from .utils import now_str


def notification_config(*, allow_legacy=False):
    from .notify import NOTIFY_CHANNELS
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    if not isinstance(raw, dict):
        raise RuntimeError("通知配置不是对象，请修复配置后重试")
    items, defaults = raw.get("notify_items", []), raw.get("notify_default_ids", [])
    if (not isinstance(items, list) or any(not isinstance(item, dict)
        or not (allow_legacy and item.get("id") in (None, "") or isinstance(item.get("id"), str) and item["id"])
        or item.get("channel") not in NOTIFY_CHANNELS or not isinstance(item.get("config", {}), dict)
        for item in items) or len({item["id"] for item in items if item.get("id")}) != sum(bool(item.get("id")) for item in items)
        or not isinstance(defaults, list) or any(not isinstance(value, str) for value in defaults)):
        raise RuntimeError("通知配置或默认选择损坏，未覆盖原文件")
    return normalize_config_data(raw)


def notification_snapshot():
    from .notify import notify_items
    with storage_transaction():
        notification_config(allow_legacy=True)
        notify_items()
        return notification_config()


def notification_version(config, item_id=None):
    items, defaults = config.get("notify_items", []), config.get("notify_default_ids", [])
    if item_id is None:
        return proxy_version({"items": items, "defaults": defaults})
    return proxy_version({"item": next((item for item in items if item["id"] == item_id), None),
                          "default": item_id in defaults})


def notification_values(data):
    from .notify import NOTIFY_CHANNELS, NOTIFY_LOG_MODES
    channel, mode = data.get("channel"), data.get("log_mode", "full")
    if not isinstance(channel, str) or channel not in NOTIFY_CHANNELS:
        raise ValueError("通知渠道无效，请重新选择")
    if not isinstance(mode, str) or mode not in NOTIFY_LOG_MODES:
        raise ValueError("通知模式无效，请重新选择")
    enabled = data.get("enabled")
    if not (type(enabled) is bool or isinstance(enabled, str) and enabled in ("0", "1")):
        raise ValueError("请选择明确的启用或停用状态")
    name = data.get("name", "")
    fields = {field for field, *_ in NOTIFY_CHANNELS[channel]["fields"]}
    config = data.get("config", {field: data.get(field, "") for field in fields})
    if not isinstance(name, str) or "\x00" in name or not isinstance(config, dict) or set(config) - fields:
        raise ValueError("通知名称或渠道参数无效")
    if any(not isinstance(value, str) or "\x00" in value for value in config.values()):
        raise ValueError("通知参数必须为文本且不能包含 NUL 字符")
    return {"name": name.strip() or NOTIFY_CHANNELS[channel]["name"], "channel": channel,
            "log_mode": mode, "enabled": enabled in (True, "1"),
            "config": {field: config.get(field, "") for field in fields}}


def apply_notification(data, *, load_tasks, save_config, sender=None):
    payload, status = mutate_notification(data, load_tasks=load_tasks, save_config=save_config)
    if status == 200 and payload.get("test_request"):
        from .notify_test_actions import submit_notification_test
        tested, status = submit_notification_test(payload["test_request"], sender=sender)
        payload.update(ok=status == 200, msg="通知已保存；" + tested["msg"], test_result=tested)
    return payload, status


def mutate_notification(data, *, load_tasks, save_config, native=False, intent_key=None):
    from .notify_native import fingerprint, native_input, read_receipt, resolve_values
    def result(message, status, saved=False, **extra):
        return {"ok": status == 200, "msg": message, "saved": saved, "resolved": saved is not None, **extra}, status
    if not isinstance(data, dict):
        return result("请求必须是对象", 400)
    action, item_id = data.get("action"), data.get("item_id", "")
    request_id, version = data.get("request_id"), data.get("version")
    if action not in ("default", "toggle", "delete", "new", "edit"):
        return result("未知通知操作", 400)
    saving = action in ("new", "edit")
    if action != "default" and (not isinstance(item_id, str) or (not item_id and action != "new")
        or (item_id and action == "new") or len(item_id) > 200 or any(ord(c) < 32 for c in item_id)):
        return result("通知选择无效", 400)
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return result("提交标识无效，请重新选择通知操作", 400)
    if action != "new" and (not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version)):
        return result("通知版本无效，请重新选择", 400, invalid_field="version")
    if action == "new":
        version = None
    values = {"action": action, "item_id": item_id, "version": version}
    native_intent = None
    if native:
        try:
            native_intent = native_input(data)
        except (ValueError, TypeError) as exc:
            return result(str(exc), 400)
    if saving and not native:
        try:
            values["item"] = notification_values(data)
        except ValueError as exc:
            return result(str(exc), 400)
        test = data.get("test", False)
        if not (type(test) is bool or isinstance(test, str) and test in ("0", "1")):
            return result("测试选择无效", 400)
        values["test"] = test in (True, "1")
        if values["test"] and not values["item"]["enabled"]:
            return result("停用通知不能发送测试，请启用或仅保存", 400)
    elif saving:
        values["test"] = False
    if action == "default":
        ids = data.get("default_ids")
        if not isinstance(ids, list) or any(not isinstance(value, str) or not value for value in ids):
            return result("默认通知选择无效，整批未保存", 400)
        values["default_ids"] = list(dict.fromkeys(ids))
    if action == "toggle":
        enabled = data.get("enabled")
        if not (type(enabled) is bool or isinstance(enabled, str) and enabled in ("0", "1")):
            return result("请选择明确的启用或停用状态", 400)
        values["enabled"] = enabled in (True, "1")
    confirm = data.get("confirm_retry", False)
    if not (type(confirm) is bool or isinstance(confirm, str) and confirm in ("0", "1")):
        return result("恢复确认无效", 400)
    intent = fingerprint(native_intent, intent_key) if native else proxy_version(values)
    target_id = uuid.uuid5(uuid.NAMESPACE_URL, "fls-notify:" + request_id).hex if action == "new" else item_id
    operation_scope = action + ":" + target_id if saving or native else ""
    path = DATA_DIR / "notify_requests" / (request_id + ".json")
    with TASKS_MUTATION_LOCK, storage_transaction():
        record = None
        def write_record():
            if native:
                record["signature"] = fingerprint({k: v for k, v in record.items() if k != "signature"}, intent_key)
            write_json(path, record)

        def saved_result(message, status):
            extra = {"item_id": target_id, "action": action} if saving or native else {}
            if saving and values["test"]:
                extra["test_request"] = {"item_id": target_id, "version": record["after"],
                    "request_id": uuid.uuid5(uuid.NAMESPACE_URL, "fls-notify-save-test:" + request_id).hex}
            return result(message, status, True, **extra)

        def complete(status=200, message="通知设置已保存"):
            record["state"] = "completed"
            try:
                write_record()
            except Exception:
                return saved_result("通知已保存，结果记录失败，请查询原提交", 503)
            return saved_result(message, status)
        try:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            else:
                if ("intent_kind" in record if isinstance(record, dict) else False) != native:
                    return result("提交标识属于另一接口，请查询原提交", 409, None)
                if native:
                    record = read_receipt(request_id, intent_key)
                if (not isinstance(record, dict) or record.get("request_id") != request_id
                    or record.get("state") not in ("prepared", "failed", "completed")
                    or any(not isinstance(record.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                           for key in ("intent", "before", "after"))):
                    raise RuntimeError("通知操作记录损坏")
                if (saving or native) and (record.get("scope") != operation_scope or not isinstance(record.get("updated_at"), str)):
                    raise RuntimeError("通知保存记录损坏")
                if record["intent"] != intent and not (not native and saving and record["state"] == "failed" and record.get("scope") == operation_scope):
                    return result("提交标识已用于其他操作，请先查询原提交", 409, None)
                if record["state"] == "completed":
                    return saved_result("通知操作已完成，未重复执行", 200)
            cfg = notification_config()
            scope = None if action == "default" else target_id
            actual = notification_version(cfg, scope)
            items = cfg.get("notify_items", [])
            item = next((item for item in items if item["id"] == target_id), None)
            if record:
                if actual == record["after"] and record["intent"] == intent:
                    return complete()
                if actual != record["before"]:
                    return result("原提交结果无法确认，通知设置已变化，请核对后重新选择", 409, None, resolved=not native,
                                  **({"current": item, "current_version": actual, "conflict": True} if saving else {}))
                if record["state"] == "prepared" and confirm not in (True, "1"):
                    return result("上次通知操作中断，请核对后确认重试", 409, None, confirm_required=True)
            if action not in ("default", "new") and item is None:
                return result("通知渠道已不存在，未执行操作", 404)
            if action == "new" and item is not None:
                return result("新建目标已存在，请核对原提交结果", 409, None)
            if action != "new" and actual != version:
                return result("通知设置或默认选择已变化，未覆盖，请重新选择", 409,
                              **({"current": item, "current_version": actual, "conflict": True} if saving or native else {}))
            if native and saving:
                try:
                    values["item"] = resolve_values(item, data)
                except (ValueError, TypeError) as exc:
                    return result(str(exc), 400)
            desired = dict(cfg)
            if action == "default":
                enabled_ids = {item["id"] for item in items if item.get("enabled", True)}
                if any(value not in enabled_ids for value in values["default_ids"]):
                    return result("选择包含已删除或停用的通知，整批未保存", 400)
                desired["notify_default_ids"] = values["default_ids"]
            else:
                enabled = values["item"]["enabled"] if saving else values.get("enabled")
                if action != "new" and (action == "delete" or not enabled):
                    references = [{"id": task["id"], "name": task.get("name") or task["id"]} for task in load_tasks()
                                  if task.get("notify", {}).get("mode") == "custom" and item_id in task["notify"].get("ids", [])]
                    if references:
                        return result("通知仍被任务单独引用，请先更换任务通知选择", 409, references=references)
                    desired["notify_default_ids"] = [value for value in cfg.get("notify_default_ids", []) if value != item_id]
                if saving:
                    stamp = record["updated_at"] if record else now_str()
                    updated = dict(item or {"id": target_id, "created_at": stamp}, **values["item"], updated_at=stamp)
                    names = {row.get("name") for row in items if row["id"] != target_id}
                    base, index = updated["name"], 1
                    while updated["name"] in names:
                        updated["name"] = f"{base}-{index}"
                        index += 1
                    desired["notify_items"] = [updated if row["id"] == target_id else row for row in items]
                    if action == "new":
                        desired["notify_items"].append(updated)
                else:
                    desired["notify_items"] = [dict(item, enabled=values["enabled"]) if action == "toggle" and item["id"] == item_id else item
                                               for item in items if action != "delete" or item["id"] != item_id]
            if (saving or native) and record and record["intent"] == intent and notification_version(desired, scope) != record["after"]:
                return result("原提交目标内容已变化，请核对后重新选择", 409, None, resolved=not native, conflict=True,
                              current=item, current_version=actual)
            record = {"request_id": request_id, "intent": intent, "state": "prepared", "before": actual,
                      "after": notification_version(desired, scope)}
            if saving:
                record.update(scope=operation_scope, updated_at=stamp)
            if native:
                record.update(scope=operation_scope, updated_at=record.get("updated_at", now_str()),
                              intent_kind="hmac-sha256-v1", input_version="" if action == "new" else version)
            try:
                write_record()
            except Exception:
                return result("请求记录保存失败，通知未更改，请确认后重试", 500, confirm_required=True)
            try:
                save_config(desired)
            except Exception:
                actual = notification_version(notification_config(), scope)
                if actual == record["after"]:
                    return complete(503, "通知已保存，保存确认失败，请查看当前设置")
                if actual != record["before"]:
                    return result("通知保存结果未知，请查询原提交", 500, None)
                record["state"] = "failed"
                try:
                    write_record()
                except Exception:
                    return result("通知未保存，恢复记录写入失败，请确认后重试", 500, confirm_required=True)
                return result("通知保存失败，原设置未更改，请重试", 500)
            return complete()
        except Exception:
            return result("通知配置或操作记录无法读取，请修复文件后查询原提交", 500, None)
