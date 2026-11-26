import json
import re
from collections import Counter
from urllib.parse import urlsplit

from flask import current_app, jsonify, request

from .bp import bp
from ... import models
from ...models import PROXY_TYPES, TASKS_MUTATION_LOCK, load_proxies, load_tasks
from ...proxy_actions import mutate_proxy, proxy_references, proxy_version, read_proxy_receipt
from ...storage import storage_transaction


MAX_BODY = 64 * 1024
REFERENCE_LIMIT = 50


def _response(payload, status=200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _error(message, status=400, **extra):
    payload = {"ok": False, "msg": message, **extra}
    if request.method == "POST":
        payload.setdefault("saved", False)
        payload.setdefault("resolved", True)
    return _response(payload, status)


def _query(allowed):
    if set(request.args) - set(allowed) or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise ValueError


def _safe_url(value):
    if not value:
        return "", False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.port == 0
            or len(value) > 4096 or any(c.isspace() or ord(c) < 32 for c in value)):
            return "", True
        return value, False
    except ValueError:
        return "", True


def _proxy_payload(proxy, reference_count=0):
    host = proxy.get("host", "")
    host_hidden = len(host) > 255 or any(c.isspace() or ord(c) < 32 or c in "/@?#\\" for c in host)
    url, url_hidden = _safe_url(proxy.get("url", ""))
    return {
        "id": proxy["id"], "name": proxy.get("name", "")[:200], "type": proxy["type"],
        "host": "" if host_hidden else host, "port": proxy.get("port", "")[:5], "url": url,
        "host_requires_replace": host_hidden, "url_requires_replace": url_hidden,
        "enabled": proxy["enabled"], "version": proxy_version(proxy),
        "username_configured": bool(proxy.get("username")), "password_configured": bool(proxy.get("password")),
        "reference_count": reference_count,
    }


def _references(items):
    return [{"id": item["id"], "name": str(item["name"])[:200]} for item in items[:REFERENCE_LIMIT]]


@bp.route("/api/proxy/settings")
def native_proxy_settings():
    try:
        _query(("q", "type", "enabled", "page", "limit"))
        query = request.args.get("q", "").casefold()
        kind, enabled = request.args.get("type", ""), request.args.get("enabled", "")
        page, limit = int(request.args.get("page", "1")), int(request.args.get("limit", "50"))
        if (len(query) > 200 or page < 1 or not 1 <= limit <= 100 or enabled not in ("", "0", "1")
            or (kind and kind not in PROXY_TYPES)):
            raise ValueError
        with TASKS_MUTATION_LOCK, storage_transaction():
            counts = Counter(task.get("proxy_id") for task in load_tasks())
            items = [_proxy_payload(proxy, counts[proxy["id"]]) for proxy in load_proxies()]
            items = [item for item in items if (not kind or item["type"] == kind)
                     and (not enabled or item["enabled"] == (enabled == "1"))
                     and query in " ".join(item[key] for key in ("name", "type", "host", "port", "url")).casefold()]
            items.sort(key=lambda item: (item["name"].casefold(), item["id"]))
            total = len(items)
            pages = max(1, (total + limit - 1) // limit)
            page = min(page, pages)
            return _response({"ok": True, "items": items[(page - 1) * limit:page * limit],
                              "page": page, "pages": pages, "total": total, "types": list(PROXY_TYPES)})
    except (ValueError, TypeError):
        return _error("代理搜索或分页参数无效")
    except Exception:
        return _error("代理列表读取失败", 503)


@bp.route("/api/proxy/settings/<path:proxy_id>")
def native_proxy_detail(proxy_id):
    try:
        _query(())
        if len(proxy_id) > 200 or any(ord(c) < 32 for c in proxy_id):
            raise ValueError
        with TASKS_MUTATION_LOCK, storage_transaction():
            proxy = next((p for p in load_proxies() if p["id"] == proxy_id), None)
            if proxy is None:
                return _error("代理不存在或已删除", 404)
            references = proxy_references(load_tasks(), proxy_id)
            payload = _proxy_payload(proxy, len(references))
            payload["references"] = _references(references)
            return _response({"ok": True, "proxy": payload})
    except (ValueError, TypeError):
        return _error("代理参数无效")
    except Exception:
        return _error("代理详情读取失败", 503)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


@bp.route("/api/proxy/settings", methods=["POST"])
def native_proxy_mutate():
    if not request.is_json:
        return _error("请求必须使用 JSON", 415)
    if request.content_length is not None and request.content_length > MAX_BODY:
        return _error("代理请求超过 64 KiB 限制", 413)
    raw = request.stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return _error("代理请求超过 64 KiB 限制", 413)
    try:
        _query(())
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(data, dict):
            raise ValueError
    except (ValueError, TypeError):
        return _error("代理 JSON 或提交字段无效")
    try:
        payload, status = mutate_proxy(data, load_proxies=load_proxies, save_proxies=models.save_proxies,
                                      load_tasks=load_tasks, native=True, intent_key=current_app.secret_key)
        safe = {key: value for key, value in payload.items() if key in {
            "ok", "msg", "saved", "resolved", "proxy_id", "conflict", "confirm_required", "invalid_field"}}
        if isinstance(data.get("request_id"), str) and re.fullmatch(r"[a-f0-9]{32}", data["request_id"]):
            safe["request_id"] = data["request_id"]
        if "references" in payload:
            safe.update(references=_references(payload["references"]), reference_count=len(payload["references"]))
        return _response(safe, status)
    except Exception:
        return _error("代理保存结果尚未确认，请查询原提交", 503, saved=None, resolved=False)


@bp.route("/api/proxy/requests/<request_id>")
def native_proxy_request(request_id):
    try:
        _query(())
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError
    except ValueError:
        return _error("提交查询参数无效")
    try:
        with TASKS_MUTATION_LOCK, storage_transaction():
            record = read_proxy_receipt(request_id, intent_key=current_app.secret_key)
            if record is None:
                return _response({"ok": True, "request_id": request_id, "state": "missing", "saved": None,
                                  "resolved": False, "retry_allowed": False})
            action, _, target_id = record["scope"].partition(":")
            current = next((p for p in load_proxies(read_only=True) if p["id"] == target_id), None)
            actual = proxy_version(current)
            matches = actual == record["after"]
            if record["state"] == "completed" or matches:
                state, saved = "completed", True
            elif actual == record["before"]:
                state, saved = record["state"], False
            else:
                state, saved = "unknown", None
            return _response({"ok": True, "request_id": request_id, "action": action, "proxy_id": target_id,
                              "state": state, "receipt_state": record["state"], "saved": saved,
                              "resolved": saved is not None, "historical": saved is True,
                              "current_matches": matches, "retry_allowed": saved is False,
                              "confirm_required": saved is False and record["state"] == "prepared"})
    except Exception:
        return _error("代理回执或存储无法读取，请修复后重新查询", 503, saved=None, resolved=False, state="unknown")
