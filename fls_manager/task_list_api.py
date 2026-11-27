from math import ceil

from .task_runner import running_task_snapshot


TASK_FIELDS = (
    "id", "name", "remark", "command", "cron", "collection_id", "enabled",
    "run_count", "created_at", "updated_at", "last_run_at", "pinned",
)
TASK_SORTS = {"default", "name", "recent_run", "last_run", "created"}
TASK_STATUSES = {"all", "running", "enabled", "disabled"}


def _page(data):
    try:
        page = max(1, int(data.get("page", 1)))
        limit = max(1, min(100, int(data.get("limit", 50))))
    except (TypeError, ValueError):
        raise ValueError("page 和 limit 必须是整数")
    return page, limit


def _matches(task, query):
    return query in "\n".join(
        str(task.get(key) or "")
        for key in ("id", "name", "remark", "command", "cron", "config_path")
    ).lower()


def list_tasks(data, *, load_tasks):
    try:
        page, limit = _page(data)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc), "items": []}, 400

    query = str(data.get("q") or "").strip().lower()
    collection_id = str(data.get("collection_id") or "").strip()
    status_filter = str(data.get("status") or "all").strip()
    sort = str(data.get("sort") or "default").strip()
    order = str(data.get("order") or "desc").strip().lower()
    if status_filter not in TASK_STATUSES:
        return {"ok": False, "msg": "未知任务状态筛选", "items": []}, 400
    if sort not in TASK_SORTS:
        return {"ok": False, "msg": "未知任务排序字段", "items": []}, 400
    if order not in ("asc", "desc"):
        return {"ok": False, "msg": "order 必须是 asc 或 desc", "items": []}, 400

    tasks = load_tasks()
    running = running_task_snapshot(str(task.get("id") or "") for task in tasks)
    selected = []
    for task in tasks:
        task_id = str(task.get("id") or "")
        is_running = task_id in running
        if query and not _matches(task, query):
            continue
        if collection_id and str(task.get("collection_id") or "") != collection_id:
            continue
        if status_filter == "running" and not is_running:
            continue
        if status_filter == "enabled" and not task.get("enabled", True):
            continue
        if status_filter == "disabled" and task.get("enabled", True):
            continue
        selected.append((task, is_running))

    if sort == "default":
        selected.sort(key=lambda row: (
            bool(row[0].get("pinned")),
            str(row[0].get("updated_at") or row[0].get("created_at") or ""),
            str(row[0].get("name") or "").lower(),
        ), reverse=order == "desc")
    elif sort == "name":
        selected.sort(key=lambda row: (
            str(row[0].get("name") or row[0].get("command") or "").strip().lower(),
            str(row[0].get("remark") or "").strip().lower(),
            str(row[0].get("id") or ""),
        ), reverse=order == "desc")
    elif sort == "created":
        selected.sort(key=lambda row: (
            str(row[0].get("created_at") or ""),
            str(row[0].get("updated_at") or ""),
        ), reverse=order == "desc")
    elif sort == "last_run":
        selected.sort(key=lambda row: (
            str(row[0].get("last_run_at") or ""),
            str(row[0].get("updated_at") or ""),
            str(row[0].get("created_at") or ""),
        ), reverse=order == "desc")
    else:
        selected.sort(key=lambda row: (
            int(row[0].get("run_count", 0) or 0),
            str(row[0].get("last_run_at") or ""),
            str(row[0].get("updated_at") or ""),
            str(row[0].get("created_at") or ""),
        ), reverse=order == "desc")

    total = len(selected)
    pages = max(1, ceil(total / limit))
    page = min(page, pages)
    start = (page - 1) * limit
    rows = []
    for task, is_running in selected[start:start + limit]:
        row = {key: task[key] for key in TASK_FIELDS if key in task}
        row["running"] = is_running
        info = running.get(str(task.get("id") or ""), {})
        row["execution_status"] = info.get("status", "stopped")
        if info.get("pid") is not None:
            row["pid"] = info["pid"]
        rows.append(row)

    return {
        "ok": True,
        "items": rows,
        "page": page,
        "limit": limit,
        "total": total,
        "pages": pages,
        "query": query,
        "collection_id": collection_id,
        "status": status_filter,
        "sort": sort,
        "order": order,
    }, 200
