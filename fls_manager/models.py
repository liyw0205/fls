import uuid

from .paths import TASK_FILE, GLOBAL_ENV_FILE, PROXY_FILE, COLLECTION_FILE
from .storage import read_json, write_json

MAX_TASK_RETRY_COUNT = 20
TASK_NOTIFY_MODES = ("none", "default", "custom")
TASK_RANDOM_DELAY_MODES = ("none", "default", "custom")
PROXY_TYPES = ("http", "https", "socks4", "socks5", "socks5h", "github")


def _as_text(value, default=""):
    if value is None:
        return default

    return str(value).strip()


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    text = str(value or "").strip().lower()

    if text in ("1", "true", "yes", "on", "enabled", "enable"):
        return True

    if text in ("0", "false", "no", "off", "disabled", "disable", ""):
        return False

    return bool(default)


def _as_int(value, default=0, min_value=None, max_value=None):
    try:
        result = int(value)
    except Exception:
        result = int(default)

    if min_value is not None:
        result = max(int(min_value), result)

    if max_value is not None:
        result = min(int(max_value), result)

    return result


def _id_list(value):
    result = []
    seen = set()

    if not isinstance(value, (list, tuple, set)):
        return result

    for item in value:
        item_id = _as_text(item)

        if not item_id or item_id in seen:
            continue

        seen.add(item_id)
        result.append(item_id)

    return result


def normalize_env_map(env):
    if not isinstance(env, dict):
        return {}

    result = {}

    for key, value in env.items():
        key = _as_text(key)

        if not key:
            continue

        result[key] = "" if value is None else str(value)

    return result


def normalize_task_notify(task):
    task = task if isinstance(task, dict) else {}
    notify = task.get("notify")

    if isinstance(notify, dict):
        mode = _as_text(notify.get("mode", "none"), "none")

        if mode in TASK_NOTIFY_MODES:
            ids = _id_list(notify.get("ids")) if mode == "custom" else []

            return {
                "mode": mode,
                "ids": ids,
            }

    old_ids = _id_list(task.get("notify_ids"))

    if old_ids:
        if "__none__" in old_ids:
            return {
                "mode": "none",
                "ids": [],
            }

        if "__default__" in old_ids:
            return {
                "mode": "default",
                "ids": [],
            }

        return {
            "mode": "custom",
            "ids": old_ids,
        }

    # Missing legacy notify data used to mean no notification at runtime.
    return {
        "mode": "none",
        "ids": [],
    }


def normalize_task_random_delay(task):
    task = task if isinstance(task, dict) else {}
    delay = task.get("random_delay")

    if isinstance(delay, dict):
        mode = _as_text(delay.get("mode", "none"), "none")

        if mode in TASK_RANDOM_DELAY_MODES:
            seconds = (
                _as_int(delay.get("seconds", 0), 0, 0, 120)
                if mode == "custom"
                else 0
            )

            return {
                "mode": mode,
                "seconds": seconds,
            }

    return {
        "mode": "none",
        "seconds": 0,
    }


def normalize_task(task):
    if not isinstance(task, dict):
        return None

    item = dict(task)

    task_id = _as_text(item.get("id"))
    if not task_id:
        task_id = uuid.uuid4().hex

    command = _as_text(item.get("command"))
    name = _as_text(item.get("name")) or command or "未命名任务"

    item["id"] = task_id
    item["name"] = name
    item["remark"] = _as_text(item.get("remark"))
    item["command"] = command
    item["cron"] = _as_text(item.get("cron"))
    item["config_path"] = _as_text(item.get("config_path"))
    item["collection_id"] = _as_text(item.get("collection_id"))
    item["enabled"] = _as_bool(item.get("enabled", True), True)
    item["env"] = normalize_env_map(item.get("env"))
    item["proxy_id"] = _as_text(item.get("proxy_id"))
    item["notify"] = normalize_task_notify(item)
    item.pop("notify_ids", None)
    item["random_delay"] = normalize_task_random_delay(item)
    item["retry_count"] = _as_int(
        item.get("retry_count", 0),
        0,
        0,
        MAX_TASK_RETRY_COUNT,
    )
    item["run_count"] = _as_int(item.get("run_count", 0), 0, 0)
    item["pinned"] = _as_bool(item.get("pinned", False), False)
    item["created_at"] = _as_text(item.get("created_at"))
    item["updated_at"] = _as_text(item.get("updated_at"))

    if "last_run_at" in item:
        item["last_run_at"] = _as_text(item.get("last_run_at"))

    return item


def normalize_proxy(proxy):
    if not isinstance(proxy, dict):
        return None

    item = dict(proxy)
    proxy_id = _as_text(item.get("id")) or uuid.uuid4().hex
    proxy_type = _as_text(item.get("type", "http"), "http").lower()

    if proxy_type not in PROXY_TYPES:
        proxy_type = "http"

    item["id"] = proxy_id
    item["name"] = _as_text(item.get("name")) or "未命名代理"
    item["type"] = proxy_type
    item["host"] = _as_text(item.get("host"))
    item["port"] = _as_text(item.get("port"))
    item["username"] = _as_text(item.get("username"))
    item["password"] = _as_text(item.get("password"))
    item["url"] = _as_text(item.get("url"))
    item["enabled"] = _as_bool(item.get("enabled", True), True)
    item["created_at"] = _as_text(item.get("created_at"))
    item["updated_at"] = _as_text(item.get("updated_at"))

    return item


def normalize_collection(collection):
    if not isinstance(collection, dict):
        return None

    item = dict(collection)
    collection_id = _as_text(item.get("id"))

    if not collection_id:
        return None

    item["id"] = collection_id
    item["name"] = _as_text(item.get("name")) or "未命名合集"
    item["remark"] = _as_text(item.get("remark"))
    item["created_at"] = _as_text(item.get("created_at"))
    item["updated_at"] = _as_text(item.get("updated_at"))

    return item


def _normalize_list(data, normalizer):
    if not isinstance(data, list):
        return [], True

    result = []
    changed = False

    for item in data:
        normalized = normalizer(item)

        if normalized is None:
            changed = True
            continue

        if normalized != item:
            changed = True

        result.append(normalized)

    return result, changed


def load_tasks():
    data = read_json(TASK_FILE, [])
    tasks, changed = _normalize_list(data, normalize_task)

    if changed:
        save_tasks(tasks)

    return tasks


def save_tasks(tasks):
    normalized, _ = _normalize_list(tasks or [], normalize_task)
    write_json(TASK_FILE, normalized)


def get_task(task_id):
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    return None


def load_global_env():
    data = read_json(GLOBAL_ENV_FILE, {})
    env = normalize_env_map(data)

    if env != data:
        save_global_env(env)

    return env


def save_global_env(env):
    write_json(GLOBAL_ENV_FILE, normalize_env_map(env))


def load_proxies():
    data = read_json(PROXY_FILE, [])
    proxies, changed = _normalize_list(data, normalize_proxy)

    if changed:
        save_proxies(proxies)

    return proxies


def save_proxies(proxies):
    normalized, _ = _normalize_list(proxies or [], normalize_proxy)
    write_json(PROXY_FILE, normalized)


def get_proxy(proxy_id):
    if not proxy_id:
        return None

    for p in load_proxies():
        if p.get("id") == proxy_id and p.get("enabled", True):
            return p

    return None


def load_collections():
    data = read_json(COLLECTION_FILE, [])
    collections, changed = _normalize_list(data, normalize_collection)

    if changed:
        save_collections(collections)

    return collections


def save_collections(collections):
    normalized, _ = _normalize_list(collections or [], normalize_collection)
    write_json(COLLECTION_FILE, normalized)


def get_collection(collection_id):
    if not collection_id:
        return None

    for c in load_collections():
        if c.get("id") == collection_id:
            return c

    return None


def unique_collection_name(name="", exclude_id=""):
    base = str(name or "").strip() or "未命名合集"

    exists = {
        str(x.get("name", ""))
        for x in load_collections()
        if x.get("id") != exclude_id
    }

    if base not in exists:
        return base

    idx = 1
    while True:
        candidate = f"{base}-{idx}"
        if candidate not in exists:
            return candidate
        idx += 1
