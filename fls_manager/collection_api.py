from math import ceil

from .collection_delete import collection_version
from .task_runner import running_task_snapshot


TASK_FIELDS = ("id", "name", "remark", "command", "cron", "config_path", "collection_id",
              "enabled", "run_count", "created_at", "updated_at", "pinned")


def _page(data):
    try:
        page = max(1, int(data.get("page", 1)))
        limit = max(1, min(100, int(data.get("limit", 50))))
    except (TypeError, ValueError):
        raise ValueError("page 和 limit 必须是整数")
    return page, limit


def _matches(task, query):
    query = str(query or "").strip().lower()
    if not query:
        return True
    values = (task.get("name"), task.get("remark"), task.get("command"),
              task.get("cron"), task.get("config_path"), task.get("id"))
    return query in "\n".join(str(value or "") for value in values).lower()


def _task_rows(tasks):
    runtime = running_task_snapshot([str(task.get("id") or "") for task in tasks])
    rows = []
    for task in tasks:
        row = {key: task[key] for key in TASK_FIELDS if key in task}
        row["running"] = str(task.get("id") or "") in runtime
        info = runtime.get(str(task.get("id") or ""), {})
        if info.get("pid") is not None:
            row["pid"] = info["pid"]
        rows.append(row)
    return rows


def list_collections(data, *, load_collections, load_tasks):
    try:
        page, limit = _page(data)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc), "items": []}, 400
    query = str(data.get("q") or "").strip().lower()
    tasks = load_tasks()
    collections = sorted(load_collections(),
                         key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
                         reverse=True)
    rows = []
    for collection in collections:
        collection_id = collection["id"]
        members = [task for task in tasks if str(task.get("collection_id") or "") == collection_id]
        collection_text = "\n".join((collection.get("name", ""), collection.get("remark", ""))).lower()
        if query and query not in collection_text and not any(_matches(task, query) for task in members):
            continue
        rows.append({"id": collection_id, "name": collection.get("name", "未命名合集"),
                     "remark": collection.get("remark", ""), "created_at": collection.get("created_at", ""),
                     "updated_at": collection.get("updated_at", ""), "member_count": len(members)})
    total = len(rows)
    pages = max(1, ceil(total / limit))
    page = min(page, pages)
    start = (page - 1) * limit
    return {"ok": True, "items": rows[start:start + limit], "page": page,
            "limit": limit, "total": total, "pages": pages}, 200


def collection_tasks(collection_id, data, *, load_collections, load_tasks):
    collection = next((item for item in load_collections() if item.get("id") == collection_id), None)
    if collection is None:
        return {"ok": False, "msg": "合集不存在"}, 404
    try:
        page, limit = _page(data)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc), "items": []}, 400
    view = data.get("view", "members")
    if view not in ("members", "available"):
        return {"ok": False, "msg": "view 必须是 members 或 available", "items": []}, 400
    query = str(data.get("q") or "").strip()
    tasks = load_tasks()
    all_members = [task for task in tasks if str(task.get("collection_id") or "") == collection_id]
    if view == "members":
        selected = all_members
        member_ids = sorted(str(task["id"]) for task in all_members)
    else:
        selected = [task for task in tasks if str(task.get("collection_id") or "") != collection_id]
        member_ids = None
    selected = [task for task in selected if _matches(task, query)]
    selected.sort(key=lambda task: (bool(task.get("pinned")),
                                    str(task.get("updated_at") or ""),
                                    str(task.get("created_at") or "")), reverse=True)
    total = len(selected)
    pages = max(1, ceil(total / limit))
    page = min(page, pages)
    start = (page - 1) * limit
    rows = _task_rows(selected[start:start + limit])
    payload = {"ok": True, "collection": {"id": collection["id"], "name": collection.get("name", ""),
                "remark": collection.get("remark", ""), "created_at": collection.get("created_at", ""),
                "updated_at": collection.get("updated_at", ""), "member_count": len(all_members)},
               "collection_version": collection_version(collection), "view": view, "items": rows,
               "page": page, "limit": limit, "total": total, "pages": pages}
    if member_ids is not None:
        payload["member_ids"] = member_ids
    return payload, 200
