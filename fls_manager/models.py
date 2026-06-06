from .paths import TASK_FILE, TASK_HISTORY_FILE, GLOBAL_ENV_FILE, PROXY_FILE, COLLECTION_FILE
from .storage import read_json, write_json


TASK_HISTORY_LIMIT = 500


def load_tasks():
    return read_json(TASK_FILE, [])


def save_tasks(tasks):
    write_json(TASK_FILE, tasks)


def get_task(task_id):
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    return None


def load_task_history():
    data = read_json(TASK_HISTORY_FILE, [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_task_history(items):
    write_json(TASK_HISTORY_FILE, list(items or [])[:TASK_HISTORY_LIMIT])


def add_task_history(record):
    history = load_task_history()
    history.insert(0, dict(record or {}))
    save_task_history(history)


def update_task_history(record_id, updates):
    if not record_id:
        return False

    history = load_task_history()
    changed = False

    for item in history:
        if item.get("id") == record_id:
            item.update(updates or {})
            changed = True
            break

    if changed:
        save_task_history(history)

    return changed


def task_history_for_task(task_id, limit=20):
    result = [
        item for item in load_task_history()
        if str(item.get("task_id") or "") == str(task_id or "")
    ]

    return result[:max(1, int(limit or 20))]


def load_global_env():
    return read_json(GLOBAL_ENV_FILE, {})


def save_global_env(env):
    write_json(GLOBAL_ENV_FILE, env)


def load_proxies():
    return read_json(PROXY_FILE, [])


def save_proxies(proxies):
    write_json(PROXY_FILE, proxies)


def get_proxy(proxy_id):
    if not proxy_id:
        return None

    for p in load_proxies():
        if p.get("id") == proxy_id and p.get("enabled", True):
            return p

    return None


def load_collections():
    data = read_json(COLLECTION_FILE, [])

    if not isinstance(data, list):
        return []

    result = []
    changed = False

    for item in data:
        if not isinstance(item, dict):
            changed = True
            continue

        if not item.get("id"):
            changed = True
            continue

        if not item.get("name"):
            item["name"] = "未命名合集"
            changed = True

        item.setdefault("remark", "")
        item.setdefault("created_at", "")
        item.setdefault("updated_at", "")

        result.append(item)

    if changed:
        save_collections(result)

    return result


def save_collections(collections):
    write_json(COLLECTION_FILE, collections or [])


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
