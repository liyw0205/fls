import uuid
from math import ceil
from urllib.parse import quote

from flask import request, redirect, url_for, abort

from . import bp
from .helpers import sort_tasks_for_display, task_matches_query
from ...models import (
    load_tasks,
    save_tasks,
    load_collections,
    save_collections,
    get_collection,
    unique_collection_name,
)
from ...utils import h, now_str, get_back_url
from ...ui.layout import layout
from ...task_runner import is_running
from ...state import RUNNING


COLLECTIONS_PER_PAGE = 10


def _current_back_url(default="/collections"):
    try:
        if request.query_string:
            return request.path + "?" + request.query_string.decode("utf-8", errors="ignore")
        return request.path
    except Exception:
        return default


def _collections_page_links(q, task_q, page, pages):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"/collections?page={int(p)}"

        if q:
            url += "&q=" + quote(q)

        if task_q:
            url += "&task_q=" + quote(task_q)

        return url

    def page_btn(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"
        return f'<a class="btn {cls}" href="{h(build_url(p))}">{h(text)}</a>'

    page = max(1, min(int(page), int(pages)))

    items = []
    items.append(page_btn(page - 1, "上一页", disabled=(page <= 1)))

    show = {1, pages}
    for p in range(page - 2, page + 3):
        if 1 <= p <= pages:
            show.add(p)

    show = sorted(show)
    last = 0

    for p in show:
        if last and p - last > 1:
            items.append(
                '<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>'
            )

        items.append(page_btn(p, active=(p == page)))
        last = p

    items.append(page_btn(page + 1, "下一页", disabled=(page >= pages)))

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            第 <b>{page}</b> / <b>{pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
</div>
"""


def _task_card(task, back_url):
    task_id = task.get("id", "")
    name = task.get("name") or task.get("command") or "未命名任务"
    remark = str(task.get("remark", "") or "").strip()
    command = str(task.get("command", "") or "").strip()
    enabled = task.get("enabled", True)
    pinned = bool(task.get("pinned", False))
    config_path = str(task.get("config_path", "") or "").strip()

    running = is_running(task_id)
    pid = RUNNING.get(task_id, {}).get("pid", "-") if running else "-"

    status_badge = '<span class="badge blue">运行中</span>' if running else '<span class="badge red">已停止</span>'
    enabled_badge = '<span class="badge green">启用</span>' if enabled else '<span class="badge gray">禁用</span>'
    pin_badge = '<span class="badge orange">置顶</span>' if pinned else ""

    pin_text = "取消置顶" if pinned else "置顶"
    pin_class = "btn-gray" if pinned else "btn-blue"

    config_btn = ""
    if config_path:
        config_btn = f'<a class="btn btn-blue" href="/task/config/{h(task_id)}?back={h(back_url)}">配置</a>'

    return f"""
<div class="fls-fold-card">
    <div style="padding:14px;">
        <div class="fls-card-head">
            <div class="fls-card-main">
                <div class="fls-card-title-main">{h(name)} {pin_badge}</div>
                <div class="fls-card-sub">
                    {f'<div>备注：{h(remark)}</div>' if remark else ''}
                    <div>PID：{h(pid)}</div>
                    <div>状态：{status_badge} {enabled_badge}</div>
                </div>
            </div>
        </div>

        <div class="fls-card-section">
            <div class="fls-source-code">{h(command)}</div>
        </div>

        <div class="fls-card-actions">
            <div class="fls-btn-line">
                <a class="btn btn-primary" href="/run/{h(task_id)}?back={h(back_url)}">运行</a>
                <a class="btn btn-red" href="/stop/{h(task_id)}?back={h(back_url)}" onclick="return confirm('确定结束该任务吗？')">结束</a>
                <a class="btn btn-orange" href="/log/{h(task_id)}?back={h(back_url)}">日志</a>
                {config_btn}
                <a class="btn btn-blue" href="/task/edit/{h(task_id)}">编辑</a>
                <a class="btn {pin_class}" href="/task/pin/{h(task_id)}?back={h(back_url)}">{pin_text}</a>
                <a class="btn btn-gray" href="/task/collection/clear/{h(task_id)}?back={h(back_url)}">取出</a>
            </div>
        </div>
    </div>
</div>
"""


def _collection_form(item=None):
    item = item or {
        "id": "",
        "name": "",
        "remark": "",
    }

    title = "编辑合集" if item.get("id") else "新建合集"

    return f"""
<form method="post">
<div class="card">
    <div class="card-title">{h(title)}</div>
    <div class="help">合集只负责组织任务，不影响任务调度。</div>
</div>

<div class="card">
    <div class="form-item">
        <label>合集名称</label>
        <input name="name" required value="{h(item.get('name', ''))}" placeholder="例如：每日签到">
    </div>

    <br>

    <div class="form-item">
        <label>备注，可空</label>
        <input name="remark" value="{h(item.get('remark', ''))}" placeholder="例如：主号 / 备用 / 测试">
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/collections">返回</a>
</div>
</form>
"""


@bp.route("/collections")
def collections_page():
    q = request.args.get("q", "").strip()
    task_q = request.args.get("task_q", "").strip()

    try:
        page = max(1, int(request.args.get("page", "1") or 1))
    except Exception:
        page = 1

    collections = load_collections()
    tasks = load_tasks()

    collections = sorted(
        collections,
        key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""),
        reverse=True,
    )

    q_lower = q.lower()
    filtered_collections = []

    for c in collections:
        cid = c.get("id", "")
        cname = str(c.get("name", "") or "").strip() or "未命名合集"
        cremark = str(c.get("remark", "") or "").strip()

        col_tasks = [
            t for t in tasks
            if str(t.get("collection_id") or "") == cid
        ]

        if q_lower:
            col_match = q_lower in (cname + "\n" + cremark).lower()
            task_match = any(task_matches_query(t, q_lower) for t in col_tasks)

            if not col_match and not task_match:
                continue

        filtered_collections.append(c)

    total = len(filtered_collections)
    pages = max(1, ceil(total / COLLECTIONS_PER_PAGE))
    page = min(page, pages)

    start = (page - 1) * COLLECTIONS_PER_PAGE
    end = page * COLLECTIONS_PER_PAGE
    show_collections = filtered_collections[start:end]

    page_links_html = _collections_page_links(q, task_q, page, pages)

    body_cols = ""
    current_back = _current_back_url("/collections")
    task_q_lower = task_q.lower()

    for c in show_collections:
        cid = c.get("id", "")
        cname = str(c.get("name", "") or "").strip() or "未命名合集"
        cremark = str(c.get("remark", "") or "").strip()

        col_tasks = [
            t for t in tasks
            if str(t.get("collection_id") or "") == cid
        ]

        show_tasks = sort_tasks_for_display(col_tasks, "default")

        available_tasks = [
            t for t in tasks
            if str(t.get("collection_id") or "") != cid
        ]

        if task_q_lower:
            available_tasks = [
                t for t in available_tasks
                if task_matches_query(t, task_q_lower)
            ]

        available_tasks = sort_tasks_for_display(available_tasks, "default")

        task_options = ""

        for t in available_tasks:
            source_text = ""

            old_cid = str(t.get("collection_id") or "").strip()
            if old_cid:
                old_collection = get_collection(old_cid)
                if old_collection:
                    source_text = f" / 来自合集：{old_collection.get('name', '')}"

            task_options += (
                f'<option value="{h(t.get("id"))}">'
                f'{h(t.get("name") or t.get("command") or "未命名任务")}'
                f'{h(source_text)}'
                f'</option>'
            )

        if not task_options:
            task_options = '<option value="" disabled>暂无可加入任务</option>'

        task_items = ""

        if show_tasks:
            for task in show_tasks:
                task_items += _task_card(task, current_back)
        else:
            task_items = '<div class="help">该合集暂无任务。</div>'

        body_cols += f"""
<div class="card" id="collection-{h(cid)}">
    <div class="fls-card-head">
        <div class="fls-card-main">
            <div class="card-title" style="margin-bottom:8px;">{h(cname)}</div>
            <div class="help">{h(cremark or "暂无备注")}</div>
        </div>
        <div class="fls-card-badges">
            <span class="badge blue">任务 {len(col_tasks)}</span>
        </div>
    </div>

    <div class="action-row" style="margin-top:12px;">
        <a class="btn btn-orange" href="/collection/edit/{h(cid)}">编辑合集</a>
        <a class="btn btn-red" href="/collection/delete/{h(cid)}?back={h(current_back)}" onclick="return confirm('确定删除该合集吗？合集内任务会自动取出。')">删除合集</a>
    </div>

    <hr style="border:0;border-top:1px solid #eef2f7;margin:14px 0;">

    <form method="post" action="/collection/add-task/{h(cid)}?back={h(current_back)}">
        <div class="form-item">
            <label>搜索并加入任务</label>
            <select name="task_id" size="8">{task_options}</select>
            <div class="help" style="margin-top:6px;">
                这里会列出当前不在本合集中的任务。可用上方“搜索可加入任务”过滤任务。
            </div>
        </div>
        <br>
        <button class="btn btn-primary" type="submit">放入合集</button>
    </form>

    <hr style="border:0;border-top:1px solid #eef2f7;margin:14px 0;">

    <div class="fls-card-grid">
        {task_items}
    </div>
</div>
"""

    if not body_cols:
        body_cols = """
<div class="card">
    <div class="help">暂无匹配合集，请尝试调整搜索条件，或点击“新建合集”创建一个。</div>
</div>
"""

    body = f"""
<div class="card">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">合集管理</div>
            <div class="help">
                这里可以新建/编辑/删除合集，也可以把任务放入合集，或从合集里取出任务。<br>
                当前匹配 <b>{total}</b> 个合集，每页 <b>{COLLECTIONS_PER_PAGE}</b> 个。
            </div>
        </div>
        <div class="action-row">
            <a class="btn btn-primary" href="/collection/new">新建合集</a>
            <a class="btn btn-gray" href="/tasks">返回任务管理</a>
        </div>
    </div>
</div>

<form method="get">
<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>搜索合集</label>
            <input name="q" value="{h(q)}" placeholder="合集名 / 备注 / 合集内任务">
        </div>

        <div class="form-item">
            <label>搜索可加入任务</label>
            <input name="task_q" value="{h(task_q)}" placeholder="任务名 / 备注 / 命令 / Cron">
        </div>
    </div>

    <br>

    <button class="btn btn-primary" type="submit">搜索</button>
    <a class="btn btn-gray" href="/collections">重置</a>
</div>
</form>

{body_cols}

{page_links_html}
"""
    return layout("合集管理", "tasks", body)


@bp.route("/collection/new", methods=["GET", "POST"])
def collection_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        remark = request.form.get("remark", "").strip()

        collections = load_collections()

        item = {
            "id": uuid.uuid4().hex,
            "name": unique_collection_name(name),
            "remark": remark,
            "created_at": now_str(),
            "updated_at": now_str(),
        }

        collections.append(item)
        save_collections(collections)

        return redirect(url_for("tasks.collections_page"))

    return layout("新建合集", "tasks", _collection_form())


@bp.route("/collection/edit/<collection_id>", methods=["GET", "POST"])
def collection_edit(collection_id):
    collections = load_collections()
    item = None

    for c in collections:
        if c.get("id") == collection_id:
            item = c
            break

    if not item:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        remark = request.form.get("remark", "").strip()

        item["name"] = unique_collection_name(name, exclude_id=collection_id)
        item["remark"] = remark
        item["updated_at"] = now_str()

        save_collections(collections)

        return redirect(url_for("tasks.collections_page"))

    return layout("编辑合集", "tasks", _collection_form(item))


@bp.route("/collection/delete/<collection_id>")
def collection_delete(collection_id):
    collections = load_collections()
    exists = any(c.get("id") == collection_id for c in collections)

    if not exists:
        abort(404)

    collections = [c for c in collections if c.get("id") != collection_id]

    tasks = load_tasks()

    for task in tasks:
        if str(task.get("collection_id") or "") == collection_id:
            task["collection_id"] = ""
            task["updated_at"] = now_str()

    save_collections(collections)
    save_tasks(tasks)

    return redirect(get_back_url("/collections"))


@bp.route("/collection/add-task/<collection_id>", methods=["POST"])
def collection_add_task(collection_id):
    collection = get_collection(collection_id)

    if not collection:
        abort(404)

    task_id = request.form.get("task_id", "").strip()

    if not task_id:
        return redirect(get_back_url("/collections"))

    tasks = load_tasks()
    found = False

    for task in tasks:
        if task.get("id") == task_id:
            task["collection_id"] = collection_id
            task["updated_at"] = now_str()
            task["pinned"] = False
            found = True
            break

    if not found:
        abort(404)

    save_tasks(tasks)

    return redirect(get_back_url("/collections"))