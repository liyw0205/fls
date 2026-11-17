import json
import re

from .config import normalize_config_data
from .models import TASKS_MUTATION_LOCK
from .paths import CONFIG_FILE, DATA_DIR
from .proxy_actions import proxy_version
from .storage import storage_transaction, write_json


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


def mutate_notification(data, *, load_tasks, save_config):
    def result(message, status, saved=False, **extra):
        return {"ok": status == 200, "msg": message, "saved": saved, "resolved": saved is not None, **extra}, status
    if not isinstance(data, dict):
        return result("请求必须是对象", 400)
    action, item_id = data.get("action"), data.get("item_id", "")
    request_id, version = data.get("request_id"), data.get("version")
    if action not in ("default", "toggle", "delete"):
        return result("未知通知操作", 400)
    if action != "default" and (not isinstance(item_id, str) or not item_id or len(item_id) > 200 or any(ord(c) < 32 for c in item_id)):
        return result("通知选择无效", 400)
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return result("提交标识无效，请重新选择通知操作", 400)
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        return result("通知版本无效，请重新选择", 400)
    values = {"action": action, "item_id": item_id, "version": version}
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
    intent = proxy_version(values)
    path = DATA_DIR / "notify_requests" / (request_id + ".json")
    with TASKS_MUTATION_LOCK, storage_transaction():
        record = None
        def complete(status=200, message="通知设置已保存"):
            record["state"] = "completed"
            try:
                write_json(path, record)
            except Exception:
                return result("通知已保存，结果记录失败，请查询原提交", 503, True)
            return result(message, status, True)
        try:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            else:
                if (not isinstance(record, dict) or record.get("request_id") != request_id
                    or record.get("state") not in ("prepared", "failed", "completed")
                    or any(not isinstance(record.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                           for key in ("intent", "before", "after"))):
                    raise RuntimeError("通知操作记录损坏")
                if record["intent"] != intent:
                    return result("提交标识已用于其他操作，请先查询原提交", 409, None)
                if record["state"] == "completed":
                    return result("通知操作已完成，未重复执行", 200, True)
            cfg = notification_config()
            scope = None if action == "default" else item_id
            actual = notification_version(cfg, scope)
            if record:
                if actual == record["after"]:
                    return complete()
                if actual != record["before"]:
                    return result("原提交结果无法确认，通知设置已变化，请核对后重新选择", 409, None, resolved=True)
                if record["state"] == "prepared" and confirm not in (True, "1"):
                    return result("上次通知操作中断，请核对后确认重试", 409, None, confirm_required=True)
            if actual != version:
                return result("通知设置或默认选择已变化，未覆盖，请重新选择", 409)
            items = cfg.get("notify_items", [])
            item = next((item for item in items if item["id"] == item_id), None)
            if action != "default" and item is None:
                return result("通知渠道已不存在，未执行操作", 404)
            desired = dict(cfg)
            if action == "default":
                enabled_ids = {item["id"] for item in items if item.get("enabled", True)}
                if any(value not in enabled_ids for value in values["default_ids"]):
                    return result("选择包含已删除或停用的通知，整批未保存", 400)
                desired["notify_default_ids"] = values["default_ids"]
            else:
                if action == "delete" or not values["enabled"]:
                    references = [{"id": task["id"], "name": task.get("name") or task["id"]} for task in load_tasks()
                                  if task.get("notify", {}).get("mode") == "custom" and item_id in task["notify"].get("ids", [])]
                    if references:
                        return result("通知仍被任务单独引用，请先更换任务通知选择", 409, references=references)
                    desired["notify_default_ids"] = [value for value in cfg.get("notify_default_ids", []) if value != item_id]
                desired["notify_items"] = [dict(item, enabled=values["enabled"]) if action == "toggle" and item["id"] == item_id else item
                                           for item in items if action != "delete" or item["id"] != item_id]
            record = {"request_id": request_id, "intent": intent, "state": "prepared", "before": actual,
                      "after": notification_version(desired, scope)}
            try:
                write_json(path, record)
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
                    write_json(path, record)
                except Exception:
                    return result("通知未保存，恢复记录写入失败，请确认后重试", 500, confirm_required=True)
                return result("通知保存失败，原设置未更改，请重试", 500)
            return complete()
        except Exception:
            return result("通知配置或操作记录无法读取，请修复文件后查询原提交", 500, None)
