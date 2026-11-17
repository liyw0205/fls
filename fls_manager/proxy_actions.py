import hashlib
import json
import re

from .models import TASKS_MUTATION_LOCK
from .paths import DATA_DIR
from .storage import storage_transaction, write_json
from .utils import now_str


def proxy_version(proxy):
    return hashlib.sha256(json.dumps(proxy, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def proxy_references(tasks, proxy_id):
    return [{"id": task["id"], "name": task.get("name") or task["id"]}
            for task in tasks if task.get("proxy_id") == proxy_id]


def mutate_proxy(data, *, load_proxies, save_proxies, load_tasks):
    def result(message, status, saved=False, **extra):
        return {"ok": status == 200, "msg": message, "saved": saved,
                "resolved": saved is not None, **extra}, status

    if not isinstance(data, dict):
        return result("请求必须是对象", 400)
    action, proxy_id = data.get("action"), data.get("proxy_id")
    request_id, version = data.get("request_id"), data.get("version")
    if action not in ("toggle", "delete"):
        return result("未知代理操作", 400)
    if not isinstance(proxy_id, str) or not proxy_id or len(proxy_id) > 200 or any(ord(c) < 32 for c in proxy_id):
        return result("代理选择无效", 400)
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return result("提交标识无效，请重新选择代理操作", 400)
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        return result("原代理版本无效，请重新选择代理操作", 400)
    enabled = data.get("enabled")
    if action == "toggle" and not (type(enabled) is bool or isinstance(enabled, str) and enabled in ("0", "1")):
        return result("请选择明确的启用或停用状态", 400)
    enabled = enabled in (True, "1") if action == "toggle" else None
    confirm = data.get("confirm_retry", False)
    if not (type(confirm) is bool or isinstance(confirm, str) and confirm in ("0", "1")):
        return result("恢复确认无效", 400)
    intent = proxy_version({"action": action, "proxy_id": proxy_id, "version": version, "enabled": enabled})
    path = DATA_DIR / "proxy_requests" / (request_id + ".json")
    # Match task-save's lock order so references cannot change during deletion.
    with TASKS_MUTATION_LOCK, storage_transaction():
        record = None

        def complete(status=200, message="代理操作已完成"):
            record["state"] = "completed"
            try:
                write_json(path, record)
            except Exception:
                return result("代理已保存，结果记录失败；请查询原提交", 503, True)
            return result(message, status, True)

        try:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except ValueError:
                raise RuntimeError("代理操作记录损坏") from None
            else:
                if (not isinstance(record, dict) or record.get("request_id") != request_id
                    or record.get("state") not in ("prepared", "failed", "completed")
                    or any(not isinstance(record.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                           for key in ("intent", "before", "after"))
                    or not isinstance(record.get("updated_at"), str)):
                    raise RuntimeError("代理操作记录损坏")
                if record["intent"] != intent:
                    return result("提交标识已用于其他操作，请先查询原提交", 409, None)
                if record["state"] == "completed":
                    return result("代理操作已完成，未重复执行", 200, True)
            proxies = load_proxies()
            current = next((p for p in proxies if p["id"] == proxy_id), None)
            actual = proxy_version(current)
            if record:
                if actual == record["after"]:
                    return complete()
                if actual != record["before"]:
                    return result("原提交结果无法确认，代理已变化；请核对后重新选择", 409, None, resolved=True)
                if record["state"] == "prepared" and confirm not in (True, "1"):
                    return result("上次代理操作中断，请核对后确认重试", 409, None, confirm_required=True)
            if current is None:
                return result("代理已不存在，未执行操作", 404)
            if actual != version:
                return result("代理设置已变化，未覆盖；请查看当前设置后重新选择", 409)
            if action == "delete" or not enabled:
                references = proxy_references(load_tasks(), proxy_id)
                if references:
                    return result("代理仍被任务引用，请先在任务设置中更换或明确取消代理", 409, references=references)
            desired = None if action == "delete" else dict(current, enabled=enabled,
                updated_at=record["updated_at"] if record else now_str())
            record = {"request_id": request_id, "intent": intent, "before": actual,
                      "after": proxy_version(desired), "updated_at": desired["updated_at"] if desired else "",
                      "state": "prepared"}
            try:
                write_json(path, record)
            except Exception:
                return result("请求记录保存失败，代理未更改；请确认后重试", 500, confirm_required=True)
            updated = [desired if p["id"] == proxy_id else p for p in proxies if p["id"] != proxy_id or desired is not None]
            try:
                save_proxies(updated)
            except Exception:
                actual = proxy_version(next((p for p in load_proxies() if p["id"] == proxy_id), None))
                if actual == record["after"]:
                    return complete(503, "代理已保存，保存确认失败，请查看当前代理")
                if actual != record["before"]:
                    return result("代理保存结果未知，请查询原提交", 500, None)
                record["state"] = "failed"
                try:
                    write_json(path, record)
                except Exception:
                    return result("代理未保存，恢复记录写入失败；请确认后重试", 500, confirm_required=True)
                return result("代理保存失败，原设置未更改，请重试", 500)
            return complete()
        except Exception:
            return result("代理存储或恢复记录无法读取，请修复文件后查询原提交", 500, None)
