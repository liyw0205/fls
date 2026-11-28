import json
import re

from flask import jsonify, request

from .bp import bp
from ...data_access import PanelStorageBusy
from ...global_env import env_version, mutate_global_env, validate_variable
from ...models import TASKS_MUTATION_LOCK, load_global_env, save_global_env, load_tasks
from ...paths import DATA_DIR
from ...storage import storage_transaction
from ...utils import env_to_text


MAX_BYTES = 1024 * 1024


def response(payload, status=200):
    result = jsonify(payload)
    result.headers["Cache-Control"] = "no-store"
    return result, status


def error(message, status=400):
    payload = {"ok": False, "msg": message}
    if request.endpoint == "env.native_env_mutate":
        payload.update(saved=False, resolved=True)
    return response(payload, status)


def page_args():
    query = request.args.get("q", "")
    page = int(request.args.get("page", "1"))
    limit = int(request.args.get("limit", "50"))
    if len(query) > 200 or page < 1 or not 1 <= limit <= 100:
        raise ValueError
    return query.casefold(), page, limit


def paged(items, page, limit, version):
    pages = max(1, (len(items) + limit - 1) // limit)
    page = min(page, pages)
    return response({"ok": True, "items": items[(page - 1) * limit:page * limit],
                     "page": page, "pages": pages, "total": len(items), "version": version})


@bp.route("/api/env/variables")
def native_env_variables():
    try:
        query, page, limit = page_args()
        with storage_transaction():
            env = load_global_env()
            items = [{"key": key, "version": env_version(env, [key])}
                     for key in sorted(env) if query in key.casefold()]
            return paged(items, page, limit, env_version(env))
    except (ValueError, TypeError):
        return error("变量搜索或分页参数无效")
    except PanelStorageBusy:
        raise
    except Exception:
        return error("变量列表读取失败", 500)


@bp.route("/api/env/variable/<key>")
def native_env_variable(key):
    try:
        validate_variable(key)
        if request.args.get("reveal", "0") not in ("0", "1"):
            raise ValueError
        with storage_transaction():
            env = load_global_env()
            if key not in env:
                return error("变量已删除", 404)
            payload = {"ok": True, "key": key, "version": env_version(env, [key])}
            if request.args.get("reveal") == "1":
                if len(env[key].encode("utf-8")) > 64 * 1024:
                    return error("变量值超过 64 KiB 编辑限制", 413)
                payload["value"] = env[key]
            return response(payload)
    except (ValueError, TypeError):
        return error("变量参数无效")
    except PanelStorageBusy:
        raise
    except Exception:
        return error("变量读取失败", 500)


@bp.route("/api/env/snapshot")
def native_env_snapshot():
    if request.args.get("reveal") != "1":
        return error("需要明确选择查看全部变量", 400)
    try:
        with storage_transaction():
            env = load_global_env()
            text = env_to_text(env)
            if len(text.encode("utf-8")) > MAX_BYTES:
                return error("变量文本超过 1 MiB 编辑限制", 413)
            return response({"ok": True, "env_text": text, "version": env_version(env)})
    except PanelStorageBusy:
        raise
    except Exception:
        return error("变量文本读取失败", 500)


@bp.route("/api/env/task-sources")
def native_env_task_sources():
    try:
        query, page, limit = page_args()
        with TASKS_MUTATION_LOCK, storage_transaction():
            env = load_global_env()
            items = []
            for task in load_tasks():
                name = str(task.get("name") or task["id"])[:200]
                for key, value in sorted(task.get("env", {}).items()):
                    if query not in (name + " " + key).casefold():
                        continue
                    items.append({"id": f"{task['id']}::{key}", "key": key,
                                  "task_name": name, "exists": key in env,
                                  "version": env_version({key: value})})
            return paged(items, page, limit, env_version(env))
    except (ValueError, TypeError):
        return error("任务变量搜索或分页参数无效")
    except PanelStorageBusy:
        raise
    except Exception:
        return error("任务变量读取失败", 500)


@bp.route("/api/env/mutate", methods=["POST"])
def native_env_mutate():
    if not request.is_json:
        return error("请求必须使用 JSON", 415)
    raw = request.stream.read(MAX_BYTES + 128 * 1024 + 1)
    if len(raw) > MAX_BYTES + 128 * 1024:
        return error("变量请求超过限制", 413)
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        if not isinstance(data.get("request_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", data["request_id"]):
            raise ValueError
        if not isinstance(data.get("version"), str) or not re.fullmatch(r"[a-f0-9]{64}", data["version"]):
            raise ValueError
        action = data.get("action")
        fields = {"action", "request_id", "version", "confirm_retry"}
        fields.update({"new": {"key", "value"}, "edit": {"key", "value", "original_key"},
                       "delete": {"original_key"}, "replace": {"env_text"},
                       "import": {"items", "sources", "overwrite"}}[action])
        if set(data) - fields:
            raise ValueError
        if action in ("new", "edit"):
            validate_variable(data.get("key"), data.get("value"))
            if len(data["key"]) > 200 or len(data["value"].encode("utf-8")) > 64 * 1024:
                return error("变量名或变量值超过限制", 413)
        if action == "replace" and (not isinstance(data.get("env_text"), str)
                                    or len(data["env_text"].encode("utf-8")) > MAX_BYTES):
            return error("变量文本超过限制或格式无效", 413)
        if action == "import":
            items, sources = data.get("items"), data.get("sources")
            if (not isinstance(items, list) or not 1 <= len(items) <= 200
                or not isinstance(sources, dict) or set(sources) != set(items)
                or type(data.get("overwrite")) is not bool):
                raise ValueError
        payload, status = mutate_global_env(data, load_global_env=load_global_env,
            save_global_env=save_global_env, load_tasks=load_tasks)
        # Conflicts must never send the entire environment back to an error UI.
        safe = {key: value for key, value in payload.items() if key in {
            "ok", "msg", "saved", "resolved", "request_id", "action", "changed_count",
            "skipped_count", "conflict", "confirm_required", "invalid_field"}}
        return response(safe, status)
    except (ValueError, TypeError, KeyError):
        return error("变量提交字段无效，请核对后重试")


@bp.route("/api/env/requests/<request_id>")
def native_env_request(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return error("提交标识无效")
    try:
        with storage_transaction():
            path = DATA_DIR / "global_env_requests" / (request_id + ".json")
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return response({"ok": True, "request_id": request_id, "state": "missing", "saved": None})
            if (not isinstance(record, dict) or record.get("request_id") != request_id
                or record.get("state") not in ("completed", "prepared", "failed")
                or record.get("scope") not in ("all", "keys")
                or record.get("action") not in ("new", "edit", "delete", "replace", "import")
                or not isinstance(record.get("keys"), list)
                or any(not isinstance(key, str) for key in record["keys"])
                or any(not isinstance(record.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                       for key in ("before", "after", "input_version"))
                or any(type(record.get(key)) is not int or record[key] < 0
                       for key in ("changed_count", "skipped_count"))):
                raise ValueError
            actual = env_version(load_global_env(), None if record["scope"] == "all" else record["keys"])
            saved = True if record["state"] == "completed" or actual == record.get("after") else (
                False if actual == record.get("before") else None)
            return response({"ok": True, "request_id": request_id, "saved": saved,
                             "state": "completed" if saved else "pending" if saved is False else "unknown",
                             "changed_count": record.get("changed_count", 0),
                             "skipped_count": record.get("skipped_count", 0)})
    except PanelStorageBusy:
        raise
    except Exception:
        return error("提交结果无法确认，请核对当前变量", 503)
