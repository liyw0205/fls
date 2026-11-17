import json
import re
import uuid

from . import notify
from .notify_actions import notification_config
from .paths import DATA_DIR
from .proxy_actions import proxy_version
from .storage import storage_transaction, write_json
from .utils import now_str


def delivery_version(row):
    return proxy_version(row)


def retry_request(row):
    return {"request_id": uuid.uuid4().hex, "expected_attempt": row["attempt"], "version": delivery_version(row)}


def _content_version(row):
    return proxy_version({key: row.get(key) for key in ("id", "item_id", "title", "content", "attempt")})


def submit_notification_retry(delivery_id, data):
    def result(message, status, **extra):
        return {"ok": status == 200, "msg": message, "resolved": True, **extra}, status

    if not isinstance(delivery_id, str) or not delivery_id or len(delivery_id) > 200 or any(ord(c) < 32 for c in delivery_id):
        return result("通知投递选择无效", 400)
    if not isinstance(data, dict):
        return result("请求必须是对象", 400)
    selected_item = data.get("item_id", "")
    if not isinstance(selected_item, str) or len(selected_item) > 200 or any(ord(c) < 32 for c in selected_item):
        return result("通知渠道选择无效", 400)
    request_id, version, attempt = (data.get(key) for key in ("request_id", "version", "expected_attempt"))
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return result("重试标识无效，请重新选择投递", 400)
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        return result("投递版本无效，请重新选择投递", 400)
    if not (type(attempt) is int or isinstance(attempt, str) and re.fullmatch(r"[0-9]{1,9}", attempt)) or not 1 <= int(attempt) < 10**9:
        return result("原投递尝试无效", 400)
    attempt = int(attempt)
    confirm = data.get("confirm_retry", False)
    if not (type(confirm) is bool or isinstance(confirm, str) and confirm in ("0", "1")):
        return result("重试确认无效", 400)
    intent = proxy_version({"delivery_id": delivery_id, "version": version, "attempt": attempt, "item_id": selected_item})
    path = DATA_DIR / "notify_retry_requests" / (request_id + ".json")

    def rows_and_target():
        rows, _ = notify._notify_history_rows(notify.read_notify_history_rows())
        return rows, next((row for row in rows if row["id"] == delivery_id), None)

    def current_result(row, status_override=None):
        state = row.get("status", "unknown")
        status = {"succeeded": 200, "failed": 502, "running": 409, "unknown": 409}.get(state, 409)
        messages = {"succeeded": "本次重试已发送成功", "failed": "本次重试发送失败",
                    "running": "本次重试正在发送", "unknown": "本次重试结果未知，请核对接收端后重新选择重试"}
        return result(messages.get(state, messages["unknown"]), status_override or status,
                      delivery=dict(row), delivery_status=state, delivery_id=delivery_id, attempt=row["attempt"])

    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        record, claimed = None, False
        try:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            else:
                if (not isinstance(record, dict) or record.get("request_id") != request_id
                    or record.get("state") not in ("prepared", "failed", "claimed", "completed")
                    or not isinstance(record.get("intent"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["intent"])
                    or not isinstance(record.get("content_version"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["content_version"])
                    or type(record.get("attempt")) is not int or record["attempt"] < 2):
                    raise RuntimeError("通知重试请求损坏")
                if record["intent"] != intent:
                    return result("提交标识已用于其他重试，请查询原请求", 409, resolved=False)
                if record["attempt"] != attempt + 1:
                    raise RuntimeError("通知重试尝试损坏")
                if record["state"] == "completed" and record.get("delivery_status") not in notify.NOTIFY_HISTORY_STATUSES:
                    raise RuntimeError("通知重试结果损坏")
            rows, row = rows_and_target()
            if record and record["state"] == "completed":
                if row and row.get("retry_request_id") == request_id and row["attempt"] == record["attempt"] and _content_version(row) == record["content_version"]:
                    return current_result(row)
                state = record.get("delivery_status", "unknown")
                return result("原重试已结束，投递记录已变化或清理，未重复发送", 200 if state == "succeeded" else 502 if state == "failed" else 409,
                              delivery_id=delivery_id, delivery_status=state, attempt=record["attempt"])
            if row is None:
                return result("通知投递不存在，未发送", 404)
            if record and row.get("retry_request_id") == request_id and row["attempt"] == record["attempt"]:
                if _content_version(row) != record["content_version"]:
                    return result("原重试正文或渠道已变化，请核对接收端后重新选择", 409, delivery_id=delivery_id)
                return current_result(row)
            if record and record["state"] == "claimed":
                return result("原重试结果未知，投递记录已变化，未重复发送", 409, delivery_id=delivery_id,
                              delivery_status="unknown", attempt=record["attempt"])
            if row["attempt"] != attempt or delivery_version(row) != version:
                return result("投递状态或尝试已变化，未发送，请重新选择", 409, delivery_id=delivery_id)
            if row["status"] not in ("failed", "unknown"):
                return result("通知投递已成功，无需重试" if row["status"] == "succeeded" else "通知投递正在发送", 409, delivery_id=delivery_id)
            cfg = notification_config()
            item = next((item for item in cfg.get("notify_items", []) if item["id"] == (selected_item or row["item_id"])), None)
            if item is None or not item.get("enabled", True):
                return result("通知渠道已删除或停用，未发送，请修复或重新选择渠道", 400 if selected_item else 409,
                              item_id=item["id"] if item else "", delivery_id=delivery_id,
                              channels=[{"id": item["id"], "name": item.get("name") or item["id"]}
                                        for item in cfg.get("notify_items", []) if item.get("enabled", True)])
            if record and record["state"] == "prepared" and confirm not in (True, "1"):
                return result("上次重试在发送前中断，请确认后继续原请求", 409, resolved=False, confirm_required=True)
            updated = dict(row, attempt=attempt + 1, status="running", message="重试发送中", updated_at=now_str(), retry_request_id=request_id,
                           item_id=item["id"], item_name=item.get("name") or item["id"], channel=item["channel"])
            record = {"request_id": request_id, "intent": intent, "attempt": attempt + 1,
                      "content_version": _content_version(updated), "state": "prepared"}
            write_json(path, record)
            row.update(updated)
            claimed = True
            notify.write_json(notify.NOTIFY_HISTORY_FILE, rows)
            notify.NOTIFY_ACTIVE_DELIVERIES.add(delivery_id)
            notify._invalidate_notify_history_cache()
            record["state"] = "claimed"
            write_json(path, record)
        except Exception:
            if claimed:
                notify.NOTIFY_ACTIVE_DELIVERIES.discard(delivery_id)
                notify._invalidate_notify_history_cache()
                try:
                    _, actual = rows_and_target()
                    if actual and actual.get("retry_request_id") == request_id and actual["attempt"] == attempt + 1:
                        return current_result(actual, 503)
                    if actual and delivery_version(actual) == version:
                        record["state"] = "failed"
                        write_json(path, record)
                except Exception:
                    pass
            return result("重试请求或投递记录无法确认，请修复存储后查询原请求", 500, resolved=False)

    try:
        ok, message = notify.send_one(item, row["title"], row["content"])
    except Exception as exc:
        ok, message = False, str(exc)
    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        verification_error = False
        try:
            _, current = rows_and_target()
            if (not current or current.get("retry_request_id") != request_id or current["attempt"] != attempt + 1
                or delivery_version(current) != delivery_version(row)):
                notify.NOTIFY_ACTIVE_DELIVERIES.discard(delivery_id)
                notify._invalidate_notify_history_cache()
                return result("本次发送已执行，但投递记录已变化，请核对接收端", 503, resolved=False)
            if not notify.finish_notify_delivery(delivery_id, ok, message):
                verification_error = True
        except Exception:
            notify.NOTIFY_ACTIVE_DELIVERIES.discard(delivery_id)
            notify._invalidate_notify_history_cache()
            verification_error = True
        try:
            _, actual = rows_and_target()
            if not actual or actual.get("retry_request_id") != request_id or actual["attempt"] != attempt + 1:
                return result("本次发送已执行，但投递记录已变化，请核对接收端", 503, resolved=False)
            record.update(state="completed", delivery_status=actual["status"])
            try:
                write_json(path, record)
            except Exception:
                verification_error = True
            return current_result(actual, 503 if verification_error else None)
        except Exception:
            return result("本次发送已执行，结果记录无法读取，请修复后查询原请求", 503, resolved=False)
