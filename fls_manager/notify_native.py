import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime

from .notify import NOTIFY_CHANNELS, NOTIFY_LOG_MODES
from .paths import DATA_DIR


# Everything outside this allowlist is write-only, including addresses and recipients.
PUBLIC_FIELDS = {
    "BARK_GROUP": ("text", []), "BARK_SOUND": ("text", []),
    "PUSH_PLUS_TEMPLATE": ("select", ["html", "markdown", "txt"]),
    "SMTP_SSL": ("boolean", ["true", "false"]), "SMTP_NAME": ("text", []),
    "NTFY_PRIORITY": ("select", ["1", "2", "3", "4", "5"]),
    "GOTIFY_PRIORITY": ("integer", []),
    "WEBHOOK_METHOD": ("select", ["POST", "GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
    "WEBHOOK_CONTENT_TYPE": ("text", []),
}
MULTILINE_FIELDS = {"WEBHOOK_HEADERS", "WEBHOOK_BODY"}


def schema():
    return [{"id": channel, "name": meta["name"], "fields": [
        {"key": field, "label": label, "sensitive": field not in PUBLIC_FIELDS,
         "kind": PUBLIC_FIELDS.get(field, ("secret", []))[0],
         "options": PUBLIC_FIELDS.get(field, ("secret", []))[1],
         "multiline": field in MULTILINE_FIELDS, "max_length": field_limit(field)}
        for field, label, _ in meta["fields"]]} for channel, meta in NOTIFY_CHANNELS.items()]


def field_limit(field):
    return 16384 if field in MULTILINE_FIELDS else 4096 if field not in PUBLIC_FIELDS else 500


def valid_text(value, limit, *, multiline=False):
    return (isinstance(value, str) and len(value) <= limit
            and not any(ord(c) < 32 and not (multiline and c in "\n\r\t") for c in value))


def valid_value(field, value):
    if not valid_text(value, field_limit(field), multiline=field in MULTILINE_FIELDS):
        return False
    if not value or field not in PUBLIC_FIELDS:
        return True
    kind, options = PUBLIC_FIELDS[field]
    if options:
        return value in options
    if kind == "integer":
        return bool(re.fullmatch(r"-?[0-9]{1,10}", value)) and -(2 ** 31) <= int(value) < 2 ** 31
    return True


def native_input(data):
    action = data["action"]
    allowed = {"action", "request_id", "confirm_retry"}
    if action != "new":
        allowed.add("version")
    if action not in ("new", "default"):
        allowed.add("item_id")
    if action in ("new", "edit"):
        allowed.update(("settings", "values", "credentials", "reset_channel"))
    elif action == "default":
        allowed.add("default_ids")
    elif action == "toggle":
        allowed.add("enabled")
    if set(data) - allowed or type(data.get("confirm_retry", False)) is not bool:
        raise ValueError("通知提交字段无效")
    if action == "toggle" and type(data.get("enabled")) is not bool:
        raise ValueError("请选择明确的启停状态")
    if action == "default":
        ids = data.get("default_ids")
        if (not isinstance(ids, list) or len(ids) > 1000
            or any(not valid_text(value, 200) or not value for value in ids)
            or len(set(ids)) != len(ids)):
            raise ValueError("默认通知选择无效或超过 1000 项")
    settings, values, credentials = (data.get(key, {}) for key in ("settings", "values", "credentials"))
    if any(not isinstance(value, dict) for value in (settings, values, credentials)):
        raise ValueError("通知设置必须为对象")
    if set(settings) - {"name", "channel", "log_mode", "enabled"}:
        raise ValueError("通知设置字段无效")
    for key, value in settings.items():
        if key == "enabled":
            if type(value) is not bool:
                raise ValueError("请选择明确的启停状态")
        elif not valid_text(value, 200):
            raise ValueError("通知名称或选项无效")
    if "channel" in settings and settings["channel"] not in NOTIFY_CHANNELS:
        raise ValueError("通知渠道无效")
    if "log_mode" in settings and settings["log_mode"] not in NOTIFY_LOG_MODES:
        raise ValueError("通知日志模式无效")
    if action == "new" and not {"channel", "enabled"} <= set(settings):
        raise ValueError("请选择渠道和启用状态")
    if type(data.get("reset_channel", False)) is not bool:
        raise ValueError("更换渠道确认无效")
    for field, value in values.items():
        if field not in PUBLIC_FIELDS or not valid_value(field, value):
            raise ValueError("通知公开字段无效")
    all_fields = {field for meta in NOTIFY_CHANNELS.values() for field, *_ in meta["fields"]}
    for field, intent in credentials.items():
        if field not in all_fields - set(PUBLIC_FIELDS) or not isinstance(intent, dict):
            raise ValueError("通知凭据字段无效")
        choice = intent.get("action")
        if choice not in ("keep", "replace", "clear") or set(intent) != ({"action", "value"} if choice == "replace" else {"action"}):
            raise ValueError("通知凭据操作无效")
        if choice == "replace" and (not valid_value(field, intent["value"]) or not intent["value"].strip()):
            raise ValueError("替换的通知凭据不能为空或超过长度限制")
    return {key: value for key, value in data.items() if key not in ("request_id", "confirm_retry")}


def resolve_values(current, data):
    from .notify_actions import notification_values
    source = {key: (current or {}).get(key, default) for key, default in
              (("name", ""), ("channel", ""), ("enabled", True), ("log_mode", "full"))}
    source.update(data.get("settings", {}))
    channel = source["channel"]
    changed = current is not None and channel != current["channel"]
    if changed and not data.get("reset_channel", False):
        raise ValueError("更换渠道需要确认清除原渠道的全部字段")
    if data.get("reset_channel", False) and not changed:
        raise ValueError("渠道未变化，不能重置渠道字段")
    fields = {field for field, *_ in NOTIFY_CHANNELS[channel]["fields"]}
    values, credentials = data.get("values", {}), data.get("credentials", {})
    if (set(values) | set(credentials)) - fields:
        raise ValueError("字段不属于所选通知渠道")
    config = {} if changed or current is None else dict(current.get("config", {}))
    config.update(values)
    for field, intent in credentials.items():
        if intent["action"] == "replace":
            config[field] = intent["value"]
        elif intent["action"] == "clear":
            config[field] = ""
    source["config"] = {key: config.get(key, "") for key in fields}
    result = notification_values(source)
    # Preserve unknown extension fields when the channel itself is unchanged.
    result["config"] = {**config, **result["config"]}
    return result


def fingerprint(value, key):
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, bytes) or not key:
        raise ValueError("通知回执密钥不可用")
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, b"fls-native-notify-v1:" + raw, hashlib.sha256).hexdigest()


def read_receipt(request_id, key):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("提交标识无效")
    try:
        record = json.loads((DATA_DIR / "notify_requests" / (request_id + ".json")).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(record, dict) or record.get("request_id") != request_id
        or record.get("intent_kind") != "hmac-sha256-v1"
        or record.get("state") not in ("prepared", "failed", "completed")
        or any(not isinstance(record.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", record[field])
               for field in ("intent", "before", "after", "signature"))
        or not isinstance(record.get("scope"), str) or not isinstance(record.get("updated_at"), str)
        or not hmac.compare_digest(record["signature"], fingerprint({k: v for k, v in record.items() if k != "signature"}, key))):
        raise ValueError("通知回执损坏")
    action, separator, item_id = record["scope"].partition(":")
    if (not separator or action not in ("new", "edit", "toggle", "delete", "default")
        or (action == "default" and item_id) or (action != "default" and (not valid_text(item_id, 200) or not item_id))
        or (action == "new" and item_id != uuid.uuid5(uuid.NAMESPACE_URL, "fls-notify:" + request_id).hex)
        or record.get("input_version") != ("" if action == "new" else record["before"])):
        raise ValueError("通知回执内容无效")
    datetime.strptime(record["updated_at"], "%Y-%m-%d %H:%M:%S")
    return record
