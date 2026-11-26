import copy
import hmac
import json
import re
import threading
import uuid
from itertools import islice

from . import notify
from .models import TASKS_MUTATION_LOCK
from .notify_actions import notification_config, notification_version
from .notify_native import PUBLIC_FIELDS, fingerprint, valid_text
from .notify_retry_actions import delivery_version
from .notify_transport import SendResult, send_native
from .paths import DATA_DIR
from .sensitive import mask_sensitive_text
from .storage import storage_transaction, write_json
from .utils import now_str

REQUEST_DIR = DATA_DIR / "native_notify_deliveries"
MAX_ACTIVE = 3
MAX_RECEIPTS = 10000
ACTIVE = set()
SLOTS = threading.BoundedSemaphore(MAX_ACTIVE)
LABELS = {
    "accepted": "上游已受理，不代表接收端已读",
    "invalid_config": "渠道配置不完整，未启动发送",
    "upstream_rejected": "上游未确认受理，请核对接收端",
    "invalid_response": "上游响应无法确认，请核对接收端",
    "network_error": "连接中断，可能已经发送，请核对接收端",
    "timeout": "等待响应超时，可能已经发送，请核对接收端",
    "response_limit": "上游响应超过限制，投递结果未知",
    "redirect": "上游返回重定向，未继续请求，投递结果未知",
    "start_failed": "工作线程未启动，未发送",
    "interrupted": "投递结果无法确认，请核对接收端",
}


class DeliveryError(ValueError):
    def __init__(self, message, status=400, *, accepted=False):
        super().__init__(message)
        self.status = status
        self.accepted = accepted


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def _object_id(value):
    return valid_text(value, 200) and bool(value) and value == value.strip()


def _version(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _read_json(path, limit):
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except FileNotFoundError:
        return None
    if len(data) > limit:
        raise RuntimeError("通知记录超过读取上限")
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("通知记录无法读取") from exc


def _history():
    raw = _read_json(notify.NOTIFY_HISTORY_FILE, 32 * 1024 * 1024)
    if raw is None:
        return []
    if (not isinstance(raw, list) or len(raw) > 1000
        or any(not isinstance(row, dict) or not _object_id(row.get("id")) for row in raw)
        or len({row["id"] for row in raw}) != len(raw)):
        raise RuntimeError("通知历史损坏")
    rows, _ = notify._notify_history_rows(raw)
    return rows


def _read(request_id, key):
    if not valid_id(request_id):
        raise DeliveryError("投递提交标识无效")
    record = _read_json(REQUEST_DIR / (request_id + ".json"), 16 * 1024)
    if record is None:
        return None
    if (not isinstance(record, dict) or record.get("request_id") != request_id
        or record.get("kind") not in ("test", "retry")
        or record.get("state") not in ("prepared", "running", "completed")
        or not _object_id(record.get("delivery_id")) or not _object_id(record.get("item_id"))
        or type(record.get("attempt")) is not int or not 1 <= record["attempt"] < 10**9
        or record.get("status") not in ("running", "succeeded", "failed", "unknown")
        or record.get("code") not in ("", *LABELS)
        or any(not _version(record.get(field)) for field in ("signature", "intent", "claim"))
        or not hmac.compare_digest(record["signature"], fingerprint({k: v for k, v in record.items() if k != "signature"}, key))):
        raise RuntimeError("投递回执无法验证")
    return record


def _save(record, key):
    record = {k: v for k, v in record.items() if k != "signature"}
    record["signature"] = fingerprint(record, key)
    write_json(REQUEST_DIR / (record["request_id"] + ".json"), record)


def _public_receipt(record, rows):
    status = record["status"]
    code = record["code"]
    if record["state"] != "completed" and record["request_id"] not in ACTIVE:
        status, code = "unknown", "interrupted"
    current = next((row for row in rows if row["id"] == record["delivery_id"]), None)
    return dict(request_id=record["request_id"], kind=record["kind"], delivery_id=record["delivery_id"],
                item_id=record["item_id"], attempt=record["attempt"], status=status, code=code,
                active=record["state"] != "completed" and record["request_id"] in ACTIVE, historical=record["state"] == "completed",
                history_available=current is not None,
                current_attempt=current["attempt"] if current else None,
                current_matches=current is not None and current["attempt"] == record["attempt"]
                and current.get("retry_request_id") == record["request_id"],
                created_at=record["created_at"], updated_at=record["updated_at"])


def request_status(request_id, key):
    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        record = _read(request_id, key)
        if record is None:
            return dict(request_id=request_id, status="missing", active=False, historical=False)
        return _public_receipt(record, _history())


def _redact(text, cfg, limit):
    text = str(text or "")
    secrets = {str(value) for item in cfg["notify_items"] for field, value in item.get("config", {}).items()
               if field not in PUBLIC_FIELDS and isinstance(value, (str, int)) and str(value)}
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[hidden]")
    text = re.sub(r"https?://[^\s<>\"']+", "[hidden URL]", text, flags=re.I)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[hidden email]", text)
    return mask_sensitive_text(text)[:limit]


def _summary(row, cfg):
    return dict(id=row["id"], item_id=row["item_id"][:200],
                item_name=_redact(row["item_name"], cfg, 200), channel=row["channel"] if row["channel"] in notify.NOTIFY_CHANNELS else "",
                source=row["source"] if row["source"] in ("manual", "test", "task", "system") else "other",
                status=row["status"], attempt=row["attempt"], version=delivery_version(row),
                content_truncated=row["content_truncated"],
                created_at=row["created_at"][:32], updated_at=row["updated_at"][:32],
                retry_allowed=row["status"] in ("failed", "unknown")
                and row["id"] not in notify.NOTIFY_ACTIVE_DELIVERIES and row["attempt"] < 10**9 - 1)


def list_deliveries(*, page=1, limit=25, status="", channel="", item_id=""):
    if (type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 100
        or status not in ("", *notify.NOTIFY_HISTORY_STATUSES)
        or channel not in ("", *notify.NOTIFY_CHANNELS) or (item_id and not _object_id(item_id))):
        raise DeliveryError("通知历史筛选或分页无效")
    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        cfg, rows = notification_config(), _history()
        rows = [row for row in rows if (not status or row["status"] == status)
                and (not channel or row["channel"] == channel) and (not item_id or row["item_id"] == item_id)]
        pages = max(1, (len(rows) + limit - 1) // limit)
        page = min(page, pages)
        return dict(items=[_summary(row, cfg) for row in rows[(page - 1) * limit:page * limit]],
                    total=len(rows), pages=pages, page=page)


def delivery_detail(delivery_id, *, reveal=False):
    if not _object_id(delivery_id):
        raise DeliveryError("通知投递标识无效")
    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        cfg = notification_config()
        row = next((row for row in _history() if row["id"] == delivery_id), None)
        if row is None:
            raise DeliveryError("通知投递不存在或已清理", 404)
        result = _summary(row, cfg)
        item = next((item for item in cfg["notify_items"] if item["id"] == row["item_id"]), None)
        result.update(item_exists=item is not None, item_enabled=bool(item and item.get("enabled", True)),
                      item_version=notification_version(cfg, item["id"]) if item else "")
        if reveal:
            result.update(title=_redact(row["title"], cfg, 1000), content=_redact(row["content"], cfg, notify.NOTIFY_CONTENT_LIMIT))
        return result


def _validate(data, delivery_id):
    fields = {"request_id", "item_id", "item_version"}
    if delivery_id:
        fields.update(("version", "expected_attempt", "confirm_truncated"))
    if (not isinstance(data, dict) or set(data) != fields or not valid_id(data.get("request_id"))
        or not _object_id(data.get("item_id")) or not _version(data.get("item_version"))):
        raise DeliveryError("通知投递提交字段无效")
    if delivery_id and (not _object_id(delivery_id) or not _version(data["version"])
                        or type(data["expected_attempt"]) is not int or not 1 <= data["expected_attempt"] < 10**9 - 1
                        or type(data["confirm_truncated"]) is not bool):
        raise DeliveryError("投递重试版本、尝试或截断确认无效")


def start_delivery(data, key, *, delivery_id="", sender=None, thread_factory=threading.Thread):
    _validate(data, delivery_id)
    request_id = data["request_id"]
    intent = fingerprint(dict(scope="native-delivery", data=data, delivery_id=delivery_id), key)
    # Configuration writes use model -> storage; deliveries use history -> storage.
    # This combined start path only acquires them in model -> history -> storage order.
    with TASKS_MUTATION_LOCK, notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        previous = _read(request_id, key)
        if previous:
            if not hmac.compare_digest(previous["intent"], intent):
                raise DeliveryError("提交标识已用于其他投递，请查询原请求", 409, accepted=None)
            return _public_receipt(previous, _history())
        cfg, rows = notification_config(), _history()
        item = next((item for item in cfg["notify_items"] if item["id"] == data["item_id"]), None)
        if item is None or not item.get("enabled", True):
            raise DeliveryError("通知渠道已删除或停用，未发送", 409)
        if notification_version(cfg, item["id"]) != data["item_version"]:
            raise DeliveryError("通知配置已变化，请核对当前渠道后重新确认", 409)
        now = now_str()
        if delivery_id:
            row = next((row for row in rows if row["id"] == delivery_id), None)
            if row is None:
                raise DeliveryError("投递记录已清理，未发送", 404)
            if delivery_version(row) != data["version"] or row["attempt"] != data["expected_attempt"]:
                raise DeliveryError("投递版本或尝试已变化，请刷新后核对", 409)
            if row["status"] not in ("failed", "unknown") or delivery_id in notify.NOTIFY_ACTIVE_DELIVERIES:
                raise DeliveryError("投递已成功或正在发送，不能重试", 409)
            if row["content_truncated"] and not data["confirm_truncated"]:
                raise DeliveryError("保存的正文已截断，请确认使用截断正文重试", 409)
            row.update(attempt=row["attempt"] + 1, item_id=item["id"], item_name=item.get("name") or item["id"],
                       channel=item["channel"], status="running", message="发送中", updated_at=now, retry_request_id=request_id)
        else:
            delivery_id = uuid.uuid5(uuid.NAMESPACE_URL, "fls-native-notify-test:" + request_id).hex
            if any(row["id"] == delivery_id for row in rows):
                raise DeliveryError("原投递已存在但回执缺失，请查询历史", 409, accepted=None)
            row = dict(id=delivery_id, item_id=item["id"], item_name=item.get("name") or item["id"], channel=item["channel"],
                       title="FLS 通知测试", content="这是一条测试通知。\n时间：" + now, content_truncated=False,
                       status="running", source="test", attempt=1, message="发送中", created_at=now, updated_at=now,
                       retry_of="", retry_request_id=request_id)
            rows.insert(0, row)
        rows = notify._trim_notify_history_rows(rows)
        if sum(1 for _ in islice(REQUEST_DIR.glob("*.json"), MAX_RECEIPTS)) >= MAX_RECEIPTS:
            raise DeliveryError("投递回执已达保留上限，请先整理，未发送", 409)
        if not SLOTS.acquire(blocking=False):
            raise DeliveryError("已有三个通知投递正在执行，请稍后再试，未发送", 429)
        record = dict(request_id=request_id, kind="retry" if data.get("version") else "test", intent=intent,
                      delivery_id=delivery_id, item_id=item["id"], attempt=row["attempt"], claim=fingerprint(row, key),
                      state="prepared", status="running", code="", created_at=now, updated_at=now)
        started = False
        try:
            _save(record, key)
            write_json(notify.NOTIFY_HISTORY_FILE, rows)
            notify._invalidate_notify_history_cache()
            notify.NOTIFY_ACTIVE_DELIVERIES.add(delivery_id)
            ACTIVE.add(request_id)
            record["state"] = "running"
            _save(record, key)
            thread = thread_factory(target=_run, args=(copy.deepcopy(record), copy.deepcopy(item), copy.deepcopy(row), key,
                                                       sender if sender is not None else send_native), daemon=True)
            try:
                thread.start()
            except Exception:
                _finish(record, key, SendResult("failed", "start_failed"))
                raise
            started = True
            return _public_receipt(record, rows)
        finally:
            if not started:
                ACTIVE.discard(request_id)
                notify.NOTIFY_ACTIVE_DELIVERIES.discard(delivery_id)
                notify._invalidate_notify_history_cache()
                SLOTS.release()


def _finish(record, key, result):
    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
        rows = _history()
        row = next((row for row in rows if row["id"] == record["delivery_id"]), None)
        if row is None or fingerprint(row, key) != record["claim"]:
            result = SendResult("unknown", "interrupted")
        else:
            row.update(status=result.status, message=LABELS[result.code], updated_at=now_str())
            write_json(notify.NOTIFY_HISTORY_FILE, rows)
            notify._invalidate_notify_history_cache()
        record.update(state="completed", status=result.status, code=result.code, updated_at=now_str())
        _save(record, key)


def _run(record, item, row, key, sender):
    try:
        # Verify the persisted claim after start releases its transaction.
        with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
            persisted = _read(record["request_id"], key)
            current = next((value for value in _history() if value["id"] == record["delivery_id"]), None)
            if (persisted is None or persisted["state"] != "running"
                or persisted["intent"] != record["intent"] or current is None or fingerprint(current, key) != record["claim"]):
                raise RuntimeError("投递执行权无法确认")
        try:
            result = sender(item, row["title"], row["content"])
            if (not isinstance(result, SendResult) or result.status not in ("succeeded", "failed", "unknown")
                or result.code not in LABELS):
                result = SendResult("unknown", "interrupted")
        except Exception:
            result = SendResult("unknown", "network_error")
        _finish(record, key, result)
    except Exception:
        # The persisted running claim remains unknown after loss of its worker.
        pass
    finally:
        item.clear()
        row.clear()
        with notify.NOTIFY_HISTORY_LOCK:
            ACTIVE.discard(record["request_id"])
            notify.NOTIFY_ACTIVE_DELIVERIES.discard(record["delivery_id"])
            notify._invalidate_notify_history_cache()
        SLOTS.release()
