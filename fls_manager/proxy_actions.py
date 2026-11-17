import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from .models import TASKS_MUTATION_LOCK, PROXY_TYPES, normalize_proxy
from .paths import DATA_DIR
from .storage import storage_transaction, write_json
from .utils import now_str


def proxy_version(proxy):
    return hashlib.sha256(json.dumps(proxy, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def proxy_references(tasks, proxy_id):
    return [{"id": task["id"], "name": task.get("name") or task["id"]}
            for task in tasks if task.get("proxy_id") == proxy_id]


def proxy_values(data):
    values = {}
    for key in ("name", "type", "host", "port", "username", "password", "url"):
        value = data.get(key, "")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("代理设置必须为文本且不能包含 NUL 字符")
        values[key] = value.strip()
    values["name"] = values["name"] or "未命名代理"
    if values["type"] not in PROXY_TYPES:
        raise ValueError("代理类型无效")
    enabled = data.get("enabled")
    if not (type(enabled) is bool or isinstance(enabled, str) and enabled in ("0", "1")):
        raise ValueError("请选择明确的启用或停用状态")
    values["enabled"] = enabled in (True, "1")
    if values["type"] == "github":
        try:
            url = urlsplit(values["url"])
            valid = (url.scheme in ("http", "https") and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment)
            url.port
        except ValueError:
            valid = False
        if not valid or any(c.isspace() or ord(c) < 32 for c in values["url"]):
            raise ValueError("GitHub 代理地址必须是有效的 HTTP 或 HTTPS 地址")
    else:
        host, port = values["host"], values["port"]
        if not host or any(c.isspace() or ord(c) < 32 or c in "/@?#\\" for c in host):
            raise ValueError("代理 Host 无效，请只填写主机名或 IP 地址")
        if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
            raise ValueError("代理端口必须为 1 到 65535 的整数")
        try:
            parsed = urlsplit("http://" + (host if ":" not in host or host.startswith("[") else "[" + host + "]") + ":" + port)
            if parsed.hostname is None or parsed.port != int(port):
                raise ValueError()
        except ValueError:
            raise ValueError("代理 Host 无效，请只填写主机名或 IP 地址") from None
        values["host"] = parsed.netloc.rsplit(":", 1)[0]
    return values


def mutate_proxy(data, *, load_proxies, save_proxies, load_tasks):
    def result(message, status, saved=False, **extra):
        return {"ok": status == 200, "msg": message, "saved": saved,
                "resolved": saved is not None, **extra}, status

    if not isinstance(data, dict):
        return result("请求必须是对象", 400)
    action, proxy_id = data.get("action"), data.get("proxy_id", "")
    request_id, version = data.get("request_id"), data.get("version")
    if action not in ("toggle", "delete", "new", "edit"):
        return result("未知代理操作", 400)
    if not isinstance(proxy_id, str) or (not proxy_id and action != "new") or (proxy_id and action == "new") or len(proxy_id) > 200 or any(ord(c) < 32 for c in proxy_id):
        return result("代理选择无效", 400)
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return result("提交标识无效，请重新选择代理操作", 400)
    if action != "new" and (not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version)):
        return result("原代理版本无效，请重新选择代理操作", 400, invalid_field="version")
    values = None
    if action in ("new", "edit"):
        try:
            values = proxy_values(data)
        except ValueError as exc:
            return result(str(exc), 400)
    enabled = data.get("enabled")
    if action == "toggle" and not (type(enabled) is bool or isinstance(enabled, str) and enabled in ("0", "1")):
        return result("请选择明确的启用或停用状态", 400)
    enabled = values["enabled"] if values else enabled in (True, "1") if action == "toggle" else None
    confirm = data.get("confirm_retry", False)
    if not (type(confirm) is bool or isinstance(confirm, str) and confirm in ("0", "1")):
        return result("恢复确认无效", 400)
    input_values = {"action": action, "proxy_id": proxy_id, "version": version, "enabled": enabled}
    if values is not None:
        input_values["values"] = values
    intent = proxy_version(input_values)
    target_id = uuid.uuid5(uuid.NAMESPACE_URL, "fls-proxy:" + request_id).hex if action == "new" else proxy_id
    scope = action + ":" + target_id
    path = DATA_DIR / "proxy_requests" / (request_id + ".json")
    # Match task-save's lock order so references cannot change during deletion.
    with TASKS_MUTATION_LOCK, storage_transaction():
        record = None

        def complete(status=200, message="代理操作已完成"):
            record["state"] = "completed"
            try:
                write_json(path, record)
            except Exception:
                return result("代理已保存，结果记录失败；请查询原提交", 503, True, proxy_id=target_id)
            return result(message, status, True, proxy_id=target_id)

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
                if record["intent"] != intent and not (record["state"] == "failed" and record.get("scope") == scope):
                    return result("提交标识已用于其他操作，请先查询原提交", 409, None)
                if record["state"] == "completed":
                    return result("代理操作已完成，未重复执行", 200, True, proxy_id=target_id)
            proxies = load_proxies()
            current = next((p for p in proxies if p["id"] == target_id), None)
            actual = proxy_version(current)
            if record:
                if actual == record["after"] and record["intent"] == intent:
                    return complete()
                if actual != record["before"]:
                    return result("原提交结果无法确认，代理已变化；请核对后重新选择", 409, None, resolved=True, current=current, conflict=True)
                if record["state"] == "prepared" and confirm not in (True, "1"):
                    return result("上次代理操作中断，请核对后确认重试", 409, None, confirm_required=True)
            if current is None and action != "new":
                return result("代理已不存在，未执行操作", 404)
            if action == "new" and current is not None:
                return result("新建目标已存在，请核对原提交结果", 409, None)
            if action != "new" and actual != version:
                return result("代理设置已变化，未覆盖；请查看当前设置后重新选择", 409, current=current, conflict=True)
            if action != "new" and (action == "delete" or not enabled):
                references = proxy_references(load_tasks(), proxy_id)
                if references:
                    return result("代理仍被任务引用，请先在任务设置中更换或明确取消代理", 409, references=references)
            stamp = record["updated_at"] if record else now_str()
            desired = None if action == "delete" else dict(current or {"id": target_id, "created_at": stamp},
                **(values or {"enabled": enabled}), updated_at=stamp)
            if desired is not None:
                desired = normalize_proxy(desired)
            if record and record["intent"] == intent and proxy_version(desired) != record["after"]:
                return result("原提交的目标内容无法确认，请核对后重新选择", 409, None)
            record = {"request_id": request_id, "intent": intent, "before": actual, "scope": scope,
                      "after": proxy_version(desired), "updated_at": desired["updated_at"] if desired else "",
                      "state": "prepared"}
            try:
                write_json(path, record)
            except Exception:
                return result("请求记录保存失败，代理未更改；请确认后重试", 500, confirm_required=True)
            updated = [desired if p["id"] == target_id else p for p in proxies if p["id"] != target_id or desired is not None]
            if action == "new":
                updated.append(desired)
            try:
                save_proxies(updated)
            except Exception:
                actual = proxy_version(next((p for p in load_proxies() if p["id"] == target_id), None))
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
