import json
import re

from flask import current_app, request

from .bp import bp
from ...data_access import PanelStorageBusy
from ... import config
from ...models import TASKS_MUTATION_LOCK, load_tasks
from ...notify import NOTIFY_CHANNELS, NOTIFY_LOG_MODES
from ...notify_actions import notification_config, notification_version, mutate_notification
from ...notify_native import PUBLIC_FIELDS, schema, valid_value, read_receipt
from ...storage import storage_transaction


MAX_BODY = 64 * 1024


def response(payload, status=200):
    from flask import jsonify
    result = jsonify(payload)
    result.headers["Cache-Control"] = "no-store"
    return result, status


def error(message, status=400, **extra):
    if request.method == "POST":
        extra.setdefault("saved", False)
        extra.setdefault("resolved", True)
    return response({"ok": False, "msg": message, **extra}, status)


def query(allowed):
    if set(request.args) - set(allowed) or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise ValueError


def references(tasks, item_id):
    return [{"id": row["id"], "name": str(row.get("name") or row["id"])[:200]}
            for row in tasks if row.get("notify", {}).get("mode") == "custom"
            and item_id in row["notify"].get("ids", [])]


def item_payload(cfg, item, tasks, *, detail=False):
    fields = {field for field, *_ in NOTIFY_CHANNELS[item["channel"]]["fields"]}
    values = item.get("config", {})
    refs = references(tasks, item["id"])
    payload = {"id": item["id"], "name": str(item.get("name") or NOTIFY_CHANNELS[item["channel"]]["name"])[:200],
               "channel": item["channel"], "enabled": bool(item.get("enabled", True)),
               "log_mode": item.get("log_mode", "full"), "default": item["id"] in cfg["notify_default_ids"],
               "version": notification_version(cfg, item["id"]), "reference_count": len(refs)}
    if detail:
        payload.update(
            values={field: values.get(field, "") if valid_value(field, values.get(field, "")) else ""
                    for field in fields & set(PUBLIC_FIELDS)},
            configured={field: bool(values.get(field)) for field in fields - set(PUBLIC_FIELDS)},
            invalid_fields=[field for field in fields & set(PUBLIC_FIELDS) if not valid_value(field, values.get(field, ""))],
            references=refs[:50])
    return payload


@bp.route("/api/notify/settings/schema")
def native_notify_schema():
    try:
        query(())
    except ValueError:
        return error("通知 schema 参数无效")
    return response({"ok": True, "channels": schema(), "log_modes": NOTIFY_LOG_MODES})


@bp.route("/api/notify/settings")
def native_notify_list():
    try:
        query(("q", "channel", "enabled", "page", "limit"))
        q, channel, enabled = request.args.get("q", ""), request.args.get("channel", ""), request.args.get("enabled", "")
        page, limit = int(request.args.get("page", "1")), int(request.args.get("limit", "50"))
        if len(q) > 200 or page < 1 or not 1 <= limit <= 100 or enabled not in ("", "0", "1") or (channel and channel not in NOTIFY_CHANNELS):
            raise ValueError
        with TASKS_MUTATION_LOCK, storage_transaction():
            cfg, tasks = notification_config(), load_tasks()
            rows = [item_payload(cfg, item, tasks) for item in cfg["notify_items"]]
            rows = [row for row in rows if (not channel or row["channel"] == channel)
                    and (not enabled or row["enabled"] == (enabled == "1"))
                    and q.casefold() in (row["name"] + " " + row["channel"]).casefold()]
            rows.sort(key=lambda row: (row["name"].casefold(), row["id"]))
            total, pages = len(rows), max(1, (len(rows) + limit - 1) // limit)
            page = min(page, pages)
            return response({"ok": True, "items": rows[(page - 1) * limit:page * limit],
                             "total": total, "pages": pages, "page": page})
    except (ValueError, TypeError):
        return error("通知筛选或分页参数无效")
    except PanelStorageBusy:
        raise
    except Exception:
        return error("通知列表读取失败", 503)


@bp.route("/api/notify/settings/defaults")
def native_notify_defaults():
    try:
        query(())
        with TASKS_MUTATION_LOCK, storage_transaction():
            cfg = notification_config()
            ids = cfg["notify_default_ids"]
            if len(ids) > 1000:
                return error("默认通知超过 1000 项，请先在面板整理", 409)
            return response({"ok": True, "default_ids": ids, "version": notification_version(cfg)})
    except ValueError:
        return error("默认通知参数无效")
    except PanelStorageBusy:
        raise
    except Exception:
        return error("默认通知读取失败", 503)


@bp.route("/api/notify/settings/<item_id>")
def native_notify_detail(item_id):
    try:
        query(())
        with TASKS_MUTATION_LOCK, storage_transaction():
            cfg = notification_config()
            item = next((row for row in cfg["notify_items"] if row["id"] == item_id), None)
            if item is None:
                return error("通知渠道已不存在", 404)
            return response({"ok": True, "item": item_payload(cfg, item, load_tasks(), detail=True)})
    except ValueError:
        return error("通知详情参数无效")
    except PanelStorageBusy:
        raise
    except Exception:
        return error("通知详情读取失败", 503)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


@bp.route("/api/notify/settings", methods=["POST"])
def native_notify_save():
    if not request.is_json:
        return error("请求必须使用 JSON", 415)
    if request.content_length is not None and request.content_length > MAX_BODY:
        return error("通知请求超过 64 KiB", 413)
    raw = request.stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return error("通知请求超过 64 KiB", 413)
    try:
        query(())
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, TypeError):
        return error("通知 JSON 无效")
    try:
        payload, status = mutate_notification(data, load_tasks=load_tasks, save_config=config.save_config,
                                              native=True, intent_key=current_app.secret_key)
        safe = {key: value for key, value in payload.items() if key in {
            "ok", "msg", "saved", "resolved", "action", "item_id", "conflict", "confirm_required", "invalid_field"}}
        if isinstance(data.get("request_id"), str) and re.fullmatch(r"[a-f0-9]{32}", data["request_id"]):
            safe["request_id"] = data["request_id"]
        if "references" in payload:
            safe.update(reference_count=len(payload["references"]), references=[
                {"id": row["id"], "name": str(row["name"])[:200]} for row in payload["references"][:50]])
        return response(safe, status)
    except PanelStorageBusy:
        raise
    except Exception:
        return error("通知保存结果未知，请查询原提交", 503, saved=None, resolved=False)


@bp.route("/api/notify/requests/<request_id>")
def native_notify_receipt(request_id):
    try:
        query(())
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError
    except ValueError:
        return error("通知提交查询参数无效")
    try:
        with TASKS_MUTATION_LOCK, storage_transaction():
            record = read_receipt(request_id, current_app.secret_key)
            if record is None:
                return response({"ok": True, "request_id": request_id, "state": "missing", "saved": None,
                                 "resolved": False, "retry_allowed": False})
            action, _, item_id = record["scope"].partition(":")
            actual = notification_version(notification_config(), None if action == "default" else item_id)
            matches = actual == record["after"]
            saved = True if record["state"] == "completed" or matches else False if actual == record["before"] else None
            state = "completed" if saved else record["state"] if saved is False else "unknown"
            return response({"ok": True, "request_id": request_id, "action": action, "item_id": item_id,
                             "state": state, "saved": saved, "resolved": saved is not None,
                             "current_matches": matches, "historical": saved is True,
                             "retry_allowed": saved is False, "confirm_required": saved is False and record["state"] == "prepared"})
    except PanelStorageBusy:
        raise
    except Exception:
        return error("通知回执或配置无法读取，请修复后查询", 503, saved=None, resolved=False, state="unknown")
