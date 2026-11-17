import json
import re
import uuid

from . import notify
from .notify_actions import notification_config, notification_version
from .paths import DATA_DIR, NOTIFY_HISTORY_FILE
from .proxy_actions import proxy_version
from .storage import storage_transaction, write_json
from .utils import now_str


def _strict_history():
    try:
        rows = json.loads(NOTIFY_HISTORY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if (not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
        or not row["id"] for row in rows) or len({row["id"] for row in rows}) != len(rows)):
        raise ValueError("通知投递记录损坏")
    return rows


def submit_notification_test(data, *, sender=None):
    def result(message, status, **extra):
        return {"ok": status == 200, "msg": message, "resolved": True, **extra}, status

    if not isinstance(data, dict):
        return result("请求必须是对象", 400)
    item_id, request_id, version = (data.get(key) for key in ("item_id", "request_id", "version"))
    if not isinstance(item_id, str) or not item_id or len(item_id) > 200 or any(ord(c) < 32 for c in item_id):
        return result("通知选择无效", 400)
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return result("提交标识无效，请重新选择通知测试", 400)
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        return result("通知版本无效，请重新选择通知测试", 400)
    confirm = data.get("confirm_retry", False)
    if not (type(confirm) is bool or isinstance(confirm, str) and confirm in ("0", "1")):
        return result("恢复确认无效", 400)
    intent = proxy_version({"item_id": item_id, "version": version})
    path = DATA_DIR / "notify_test_requests" / (request_id + ".json")
    delivery_id = uuid.uuid5(uuid.NAMESPACE_URL, "fls-notify-test:" + request_id).hex

    def delivery_result(row, *, verification_error=False, history_available=True):
        state = row.get("status", "unknown")
        status = 200 if state == "succeeded" else 502 if state == "failed" else 409
        if verification_error:
            status = 503
        labels = {"succeeded": "测试通知已发送", "failed": "测试通知发送失败",
                  "running": "测试通知正在发送，请查看投递记录", "unknown": "测试通知结果未知，请核对后重试投递"}
        message = labels.get(state, labels["unknown"])
        if verification_error:
            message += "；结果确认异常，请查看投递记录"
        return result(message, status, delivery_id=delivery_id, history_available=history_available,
                      delivery_status=state, detail=row.get("message", ""),
                      sent=True if state == "succeeded" else False if state == "failed" else None)

    # History always precedes storage; never hold either lock during network I/O.
    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        record = None
        creating = False
        try:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            else:
                if (not isinstance(record, dict) or record.get("request_id") != request_id
                    or record.get("delivery_id") != delivery_id
                    or record.get("state") not in ("prepared", "sending", "completed")
                    or not isinstance(record.get("intent"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["intent"])):
                    raise ValueError("通知测试请求损坏")
                if record["intent"] != intent:
                    return result("提交标识已用于其他测试，请先查询原提交", 409, resolved=False)
            rows = _strict_history()
            existing = next((row for row in rows if row["id"] == delivery_id), None)
            if existing is not None:
                if existing.get("item_id") != item_id:
                    return result("提交标识对应其他通知，请先查询原提交", 409, resolved=False)
                return delivery_result(notify.get_notify_delivery(delivery_id))
            if record and record["state"] != "prepared":
                if record.get("delivery_status") in ("succeeded", "failed"):
                    return delivery_result({"status": record["delivery_status"], "message": "投递明细已清理，未重复发送"}, history_available=False)
                return result("投递明细不存在，原测试结果未知；请核对接收端后重新选择测试", 409,
                              delivery_status="unknown", sent=None)
            cfg = notification_config()
            item = next((item for item in cfg.get("notify_items", []) if item["id"] == item_id), None)
            if item is None:
                return result("通知渠道已删除，未发送测试", 404)
            if not item.get("enabled", True):
                return result("通知渠道已停用，未发送测试", 409)
            if notification_version(cfg, item_id) != version:
                return result("通知设置已变化，未发送测试，请重新选择", 409)
            if record and confirm not in (True, "1"):
                return result("上次测试在发送前中断，请确认后继续", 409, confirm_required=True, resolved=False)
            record = {"request_id": request_id, "intent": intent, "delivery_id": delivery_id, "state": "prepared"}
            write_json(path, record)
            title, content = "FLS 通知测试", f"这是一条测试通知。\n时间：{now_str()}"
            creating = True
            notify.create_notify_delivery(item, title, content, source="test", delivery_id=delivery_id)
            record["state"] = "sending"
            write_json(path, record)
        except Exception:
            if creating:
                notify.NOTIFY_ACTIVE_DELIVERIES.discard(delivery_id)
                notify._invalidate_notify_history_cache()
            return result("测试请求或投递记录无法确认，请修复存储后查询原提交", 500,
                          resolved=False, sent=None)

    try:
        ok, message = (sender or notify.send_one)(item, title, content)
    except Exception as exc:
        ok, message = False, str(exc)
    verification_error = False
    try:
        with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
            _strict_history()
            if not notify.finish_notify_delivery(delivery_id, ok, message):
                verification_error = True
    except Exception:
        with notify.NOTIFY_HISTORY_LOCK:
            notify.NOTIFY_ACTIVE_DELIVERIES.discard(delivery_id)
            notify._invalidate_notify_history_cache()
        verification_error = True
    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        try:
            _strict_history()
            row = notify.get_notify_delivery(delivery_id)
            if row is None:
                return result("测试已执行，但投递记录不存在；请核对接收端后重新选择测试", 503,
                              delivery_status="unknown", sent=None)
            record.update(state="completed", delivery_status=row["status"])
            try:
                write_json(path, record)
            except Exception:
                verification_error = True
            return delivery_result(row, verification_error=verification_error)
        except Exception:
            return result("测试已执行，结果记录无法读取，请修复后查询原提交", 503,
                          delivery_id=delivery_id, sent=None, resolved=False)
