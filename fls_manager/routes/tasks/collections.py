import json
import uuid
from math import ceil
from urllib.parse import quote

from flask import request, redirect, url_for, abort

from . import bp
from .helpers import sort_tasks_for_display, task_matches_query
from ...models import (
    load_tasks,
    save_tasks,
    mutate_tasks,
    load_collections,
    save_collections,
    get_collection,
    unique_collection_name,
)
from ...utils import h, now_str, get_back_url
from ...ui.layout import layout
from ...ui.tables import collapsible_code, task_row_model
from ...ui.components import page_header, data_toolbar, empty_state
from ...collection_delete import collection_version, delete_collection
from ...collection_save import save_collection


COLLECTIONS_PER_PAGE = 10


def _current_back_url(default="/collections"):
    try:
        if request.query_string:
            return request.path + "?" + request.query_string.decode("utf-8", errors="ignore")
        return request.path
    except Exception:
        return default


def _back_param(back_url):
    return h(quote(str(back_url or ""), safe="/"))


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


def _task_card(task, back_url, collection_id=""):
    row = task_row_model(task)
    task_id = row["id"]
    name = row["name"]
    remark = row["remark"]
    command = row["command"]
    command_html = collapsible_code(command, limit=90, max_lines=2)
    enabled = row["enabled"]
    pinned = row["pinned"]
    config_path = row["config_path"]
    running = row["running"]
    pid = row["pid"]

    status_badge = '<span class="badge blue status-badge status-running" role="status">运行中</span>' if running else '<span class="badge red status-badge status-stopped" role="status">已停止</span>'
    enabled_badge = '<span class="badge green status-badge status-enabled" role="status">启用</span>' if enabled else '<span class="badge gray status-badge status-disabled" role="status">禁用</span>'
    pin_badge = '<span class="badge orange">置顶</span>' if pinned else ""

    pin_text = "取消置顶" if pinned else "置顶"
    pin_class = "btn-gray" if pinned else "btn-blue"
    toggle_text = "停用" if enabled else "启用"
    toggle_class = "btn-gray" if enabled else "btn-primary"

    if running:
        run_action = f'/stop/{h(task_id)}?back={_back_param(back_url)}'
        run_label = "停止任务"
        run_class = "btn-orange"
        run_confirm = " onclick=\"return confirm('确定停止该任务吗？')\""
    else:
        run_action = f'/run/{h(task_id)}?back={_back_param(back_url)}'
        run_label = "立即运行任务"
        run_class = "btn-primary"
        run_confirm = ""

    config_btn = ""
    if config_path:
        config_btn = f'<a class="btn btn-blue" href="/task/config/{h(task_id)}?back={_back_param(back_url)}">任务配置</a>'

    return f"""
<article class="fls-fold-card mobile-list-item collection-task-card" data-collection-id="{h(collection_id)}" data-task-id="{h(task_id)}">
    <div style="padding:14px;">
        <div class="fls-card-head">
            <div class="collection-task-select-wrap">
                <input
                    class="collection-task-select"
                    type="checkbox"
                    data-collection-id="{h(collection_id)}"
                    data-task-id="{h(task_id)}"
                    onchange="flsCollectionSyncSelection(this)"
                    aria-label="选择任务 {h(name)}"
                >
            </div>
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
            <div class="fls-source-code">{command_html}</div>
        </div>

        <div class="fls-card-actions row-actions collection-task-actions" aria-label="任务 {h(name)} 行操作">
            <div class="row-actions-primary">
                <form class="inline-form" method="post" action="{run_action}">
                    <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
                    <button class="btn {run_class}" type="submit"{run_confirm}>{run_label}</button>
                </form>
                <a class="btn btn-orange" href="/log/{h(task_id)}?back={_back_param(back_url)}">查看任务日志</a>
                {config_btn}
                <a class="btn btn-blue" href="/task/edit/{h(task_id)}?back={_back_param(back_url)}">编辑任务</a>
            </div>
            <div class="row-actions-secondary">
                <form class="inline-form" method="post" action="/task/pin/{h(task_id)}?back={_back_param(back_url)}">
                    <button class="btn {pin_class}" type="submit">{pin_text}</button>
                </form>
            </div>
            <div class="row-actions-danger">
                <!-- Legacy action markers: action="/run/{h(task_id)}?back={_back_param(back_url)}" action="/stop/{h(task_id)}?back={_back_param(back_url)}" -->
                <form class="inline-form" method="post" action="/task/toggle/{h(task_id)}?back={_back_param(back_url)}">
                    <input type="hidden" name="enabled" value="{'0' if enabled else '1'}">
                    <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
                    <button class="btn {toggle_class}" type="submit">{toggle_text}任务</button>
                </form>
                <form class="inline-form" method="post" action="/task/collection/clear/{h(task_id)}?back={_back_param(back_url)}">
                    <input type="hidden" name="expected_collection" value="{h(collection_id)}">
                    <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
                    <button class="btn btn-gray" type="submit">移出合集</button>
                </form>
                <form class="inline-form" method="post" action="/task/delete/{h(task_id)}?back={_back_param(back_url)}">
                    <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
                    <button class="btn btn-red" type="submit" onclick="return confirm('确定删除该任务吗？')">删除任务</button>
                </form>
            </div>
        </div>
    </div>
</article>
"""


def _collection_task_bulk_toolbar(collection_id, has_tasks):
    if not has_tasks:
        return ""

    cid = h(collection_id)

    return f"""
<div class="collection-bulk-toolbar" data-collection-id="{cid}">
    <div class="collection-bulk-left">
        <label class="collection-bulk-select-all">
            <input
                class="collection-select-all"
                type="checkbox"
                data-collection-id="{cid}"
                onchange="flsCollectionToggleAll(this)"
                aria-label="全选本合集任务"
            >
            全选本合集
        </label>
        <span class="collection-selected-count">已选择 0 个</span>
    </div>
    <div class="collection-bulk-actions">
        <div class="row-actions-primary">
            <button class="btn btn-primary collection-bulk-btn" type="button" data-collection-id="{cid}" onclick="flsCollectionTaskBulkAction(this.dataset.collectionId, 'enable', this)" disabled>启用任务</button>
            <button class="btn btn-gray collection-bulk-btn" type="button" data-collection-id="{cid}" onclick="flsCollectionTaskBulkAction(this.dataset.collectionId, 'disable', this)" disabled>停用任务</button>
            <button class="btn btn-blue collection-bulk-btn" type="button" data-collection-id="{cid}" onclick="flsCollectionTaskBulkAction(this.dataset.collectionId, 'run', this)" disabled>立即运行任务</button>
        </div>
        <div class="row-actions-danger">
            <button class="btn btn-red collection-bulk-btn" type="button" data-collection-id="{cid}" onclick="flsCollectionTaskBulkAction(this.dataset.collectionId, 'stop', this)" disabled>停止任务</button>
            <button class="btn btn-gray collection-bulk-btn" type="button" data-collection-id="{cid}" onclick="flsCollectionTaskBulkAction(this.dataset.collectionId, 'clear_collection', this)" disabled>移出合集</button>
            <button class="btn btn-gray collection-bulk-btn" type="button" data-collection-id="{cid}" onclick="flsCollectionTaskBulkAction(this.dataset.collectionId, 'delete', this)" disabled>删除任务</button>
        </div>
    </div>
</div>
"""


def _collections_bulk_assets():
    return r"""
<style>
.collection-panel {
    padding:0 0 24px;
    margin-bottom:24px;
    border-bottom:1px solid var(--border);
}

.collection-panel:last-child {
    margin-bottom:0;
    border-bottom:0;
}

.collection-panel-title {
    margin:0 0 8px;
    color:#111827;
    font-size:20px;
    line-height:1.3;
}

.collection-bulk-toolbar {
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:12px;
    flex-wrap:wrap;
    padding:12px;
    margin-bottom:12px;
    border:1px solid #e5e7eb;
    border-radius:12px;
    background:#f9fafb;
}

.collection-bulk-left,
.collection-bulk-actions {
    display:flex;
    align-items:center;
    gap:8px;
    flex-wrap:wrap;
}

.collection-bulk-select-all {
    display:inline-flex;
    align-items:center;
    gap:6px;
    color:#374151;
    font-size:13px;
    font-weight:700;
    white-space:nowrap;
}

.collection-selected-count {
    color:#6b7280;
    font-size:13px;
    font-weight:700;
}

.collection-bulk-btn:disabled {
    opacity:.45;
    cursor:not-allowed;
}

.collection-task-select-wrap {
    flex:0 0 auto;
    padding-top:2px;
}

.collection-task-select,
.collection-select-all {
    width:16px!important;
    height:16px!important;
    min-height:16px;
    margin:0;
    padding:0;
    cursor:pointer;
}

.collection-task-card.collection-task-selected {
    border-color:#86efac;
}

.collection-task-card.collection-task-selected .fls-card-title-main {
    color:#166534;
}

@media(max-width:520px) {
    .collection-bulk-toolbar {
        align-items:stretch;
    }

    .collection-bulk-left,
    .collection-bulk-actions {
        width:100%;
    }

    .collection-bulk-actions .btn {
        flex:1 1 calc(33.333% - 8px);
        min-width:72px;
        margin:0;
    }
}
</style>

<script>
function flsCollectionTaskBoxes(collectionId){
    return Array.prototype.filter.call(
        document.querySelectorAll(".collection-task-select"),
        function(box){
            return box.dataset.collectionId === collectionId;
        }
    );
}

function flsCollectionTaskCards(collectionId){
    return Array.prototype.filter.call(
        document.querySelectorAll(".collection-task-card"),
        function(card){
            return card.dataset.collectionId === collectionId;
        }
    );
}

function flsCollectionSelectedIds(collectionId){
    var ids = [];
    var seen = {};

    flsCollectionTaskBoxes(collectionId).forEach(function(box){
        var id = box.dataset.taskId || "";

        if(!box.checked || !id || seen[id]) return;

        seen[id] = true;
        ids.push(id);
    });

    return ids;
}

function flsCollectionUpdateBulkState(collectionId){
    var boxes = flsCollectionTaskBoxes(collectionId);
    var selected = flsCollectionSelectedIds(collectionId);
    var selectedSet = {};

    selected.forEach(function(id){
        selectedSet[id] = true;
    });

    document.querySelectorAll(".collection-select-all").forEach(function(box){
        if(box.dataset.collectionId !== collectionId) return;

        box.disabled = boxes.length === 0;
        box.checked = boxes.length > 0 && selected.length === boxes.length;
        box.indeterminate = selected.length > 0 && selected.length < boxes.length;
    });

    document.querySelectorAll(".collection-bulk-btn").forEach(function(btn){
        if(btn.dataset.collectionId !== collectionId) return;
        btn.disabled = selected.length === 0;
    });

    document.querySelectorAll(".collection-bulk-toolbar").forEach(function(toolbar){
        if(toolbar.dataset.collectionId !== collectionId) return;

        var countEl = toolbar.querySelector(".collection-selected-count");
        if(countEl){
            countEl.textContent = "已选择 " + selected.length + " 个";
        }
    });

    flsCollectionTaskCards(collectionId).forEach(function(card){
        var id = card.dataset.taskId || "";
        card.classList.toggle("collection-task-selected", !!selectedSet[id]);
    });
}

function flsCollectionUpdateAllBulkStates(){
    var ids = {};

    document.querySelectorAll(".collection-bulk-toolbar, .collection-task-select").forEach(function(el){
        var collectionId = el.dataset.collectionId || "";
        if(collectionId) ids[collectionId] = true;
    });

    Object.keys(ids).forEach(flsCollectionUpdateBulkState);
}

function flsCollectionToggleAll(source){
    var collectionId = source.dataset.collectionId || "";

    flsCollectionTaskBoxes(collectionId).forEach(function(box){
        box.checked = source.checked;
    });

    flsCollectionUpdateBulkState(collectionId);
}

function flsCollectionSyncSelection(source){
    flsCollectionUpdateBulkState(source.dataset.collectionId || "");
}

function flsCollectionBulkOptions(collectionId){
    return {
        scope:"collection:" + collectionId, block:"#collectionsPageBlock",
        ids:() => flsCollectionSelectedIds(collectionId),
        refresh:() => flsRefreshCollectionsBlockPartial(true),
        select:ids => {
            flsCollectionTaskBoxes(collectionId).forEach(box => { box.checked = ids.includes(box.dataset.taskId); });
            flsCollectionUpdateBulkState(collectionId);
        }
    };
}

async function flsCollectionTaskBulkAction(collectionId, action, source){
    const ids = flsCollectionSelectedIds(collectionId);
    if(action === "delete" && !confirm("确定删除选中的 " + ids.length + " 个任务吗？")) return;
    if(action === "stop" && !confirm("确定停止选中的 " + ids.length + " 个任务吗？")) return;
    if(action === "clear_collection" && !confirm("确定从当前合集中取出选中的 " + ids.length + " 个任务吗？")) return;
    await flsSubmitTaskBulk(flsCollectionBulkOptions(collectionId), action, source);
}

async function flsRefreshCollectionsBlockPartial(keepSelection){
    try {
        var res = await fetch(window.location.href, {
            headers: {"X-Requested-With":"XMLHttpRequest"},
            credentials: "same-origin"
        });

        var html = await flsReadTextResponse(res);
        var doc = new DOMParser().parseFromString(html, "text/html");
        var newBlock = doc.querySelector("#collectionsPageBlock");
        var oldBlock = document.querySelector("#collectionsPageBlock");

        if(newBlock && oldBlock){
            oldBlock.replaceWith(newBlock);
            flsCollectionUpdateAllBulkStates();

            if(typeof flsInitFloatingFormActions === "function"){
                flsInitFloatingFormActions(document);
            }
        }else{
            if(keepSelection) throw new Error("合集列表不可用，请重新登录后刷新");
            location.reload();
        }
    } catch(e) {
        if(keepSelection) throw e;
        location.reload();
    }
}

flsCollectionUpdateAllBulkStates();
document.querySelectorAll(".collection-bulk-toolbar").forEach(toolbar => {
    flsRestoreTaskBulk(flsCollectionBulkOptions(toolbar.dataset.collectionId));
});
</script>
"""


def _collection_form(item=None, *, request_id=None, version=None, confirm_retry=False, conflict=False):
    item = item or {
        "id": "",
        "name": "",
        "remark": "",
    }

    title = "编辑任务合集" if item.get("id") else "新建任务合集"
    request_id = request_id or uuid.uuid4().hex
    action = "/collection/edit/" + quote(item["id"], safe="") if item.get("id") else "/collection/new"
    fields = f'<input type="hidden" name="request_id" value="{h(request_id)}">'
    if version is not None:
        fields += f'<input type="hidden" name="collection_version" value="{h(version)}">'
    if confirm_retry:
        fields += '<input type="hidden" name="confirm_retry" value="1">'
    confirmation = ' onclick="return confirm(\'确认当前合集状态后保存此草稿？\')"' if confirm_retry or conflict else ""
    label = "合并后保存合集设置" if conflict else "确认后重试保存" if confirm_retry else "保存合集设置"

    header = page_header(
        title,
        help_html="合集只负责组织任务，不影响任务调度。",
        actions_html='<a class="btn btn-gray" href="/collections">返回任务合集</a>',
    )

    return f"""
{header}
<form method="post" action="{h(action)}">
{fields}
<section class="section fls-form-section">
    <h2 class="section-title">合集信息</h2>
    <div class="form-item">
        <label>合集名称</label>
        <input name="name" required value="{h(item.get('name', ''))}" placeholder="例如：每日签到" aria-label="合集名称">
    </div>

    <br>

    <div class="form-item">
        <label>备注，可空</label>
        <input name="remark" value="{h(item.get('remark', ''))}" placeholder="例如：主号 / 备用 / 测试" aria-label="合集备注">
    </div>
</section>

<section class="section fls-form-section fls-save-section">
    <button class="btn btn-primary" type="submit"{confirmation}>{label}</button>
    <a class="btn btn-gray" href="/collections">返回</a>
</section>
</form>
"""


def _collection_task_ids_from_form():
    raw_ids = list(request.form.getlist("task_ids"))
    legacy_id = request.form.get("task_id", "").strip()

    if legacy_id:
        raw_ids.append(legacy_id)

    task_ids = []
    seen = set()

    for raw_task_id in raw_ids:
        task_id = str(raw_task_id or "").strip()

        if not task_id or task_id in seen:
            continue

        seen.add(task_id)
        task_ids.append(task_id)

    return task_ids


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
        collection_back = current_back + "#collection-" + cid

        if show_tasks:
            for task in show_tasks:
                task_items += _task_card(task, collection_back, collection_id=cid)
        else:
            task_items = empty_state("该合集暂无任务。")

        bulk_toolbar = _collection_task_bulk_toolbar(cid, bool(show_tasks))

        body_cols += f"""
<section class="collection-panel" id="collection-{h(cid)}">
    <div class="fls-card-head">
        <div class="fls-card-main">
            <h3 class="collection-panel-title">{h(cname)}</h3>
            <div class="help">{h(cremark or "暂无备注")}</div>
        </div>
        <div class="fls-card-badges">
            <span class="badge blue">任务 {len(col_tasks)}</span>
        </div>
    </div>

    <div class="row-actions" style="margin-top:12px;" aria-label="合集 {h(cname)} 行操作">
        <div class="row-actions-secondary">
            <a class="btn btn-orange" href="/collection/edit/{h(cid)}">编辑任务合集</a>
        </div>
        <div class="row-actions-danger">
            <form class="inline-form" method="post" action="/collection/delete/{h(cid)}?back={_back_param(current_back)}">
                <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
                <input type="hidden" name="expected_members" value="{h(json.dumps(sorted(t['id'] for t in col_tasks)))}">
                <input type="hidden" name="collection_version" value="{collection_version(c)}">
                <button class="btn btn-red" type="submit" onclick="return confirm('确定删除该合集吗？合集内任务会自动取出。')">删除任务合集</button>
            </form>
        </div>
    </div>

    <hr style="border:0;border-top:1px solid #eef2f7;margin:14px 0;">

    <form method="post" action="/collection/add-task/{h(cid)}?back={_back_param(current_back + '#collection-' + cid)}">
        <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
        <input type="hidden" name="expected_collections" value="{h(json.dumps({t['id']: str(t.get('collection_id') or '') for t in available_tasks}, ensure_ascii=False))}">
        <div class="form-item">
            <label>搜索并加入任务</label>
            <select name="task_ids" size="8" multiple aria-label="选择要加入合集的任务">{task_options}</select>
            <div class="help" style="margin-top:6px;">
                这里会列出当前不在本合集中的任务。可用上方“搜索可加入任务”过滤任务。
            </div>
        </div>
        <br>
        <button class="btn btn-primary" type="submit">放入合集</button>
    </form>

    <hr style="border:0;border-top:1px solid #eef2f7;margin:14px 0;">

    {bulk_toolbar}

    <div class="fls-card-grid mobile-list">
        {task_items}
    </div>
</section>
        """

    if not body_cols:
        body_cols = empty_state(
            "暂无匹配任务合集，请尝试调整搜索条件，或点击“新建任务合集”创建一个。",
            '<a class="btn btn-primary" href="/collection/new">新建任务合集</a>',
        )

    bulk_assets = _collections_bulk_assets()

    header = page_header(
        "任务合集",
        help_html=f"管理任务合集并调整任务归属；当前匹配 <b>{total}</b> 个合集，每页 <b>{COLLECTIONS_PER_PAGE}</b> 个。",
        actions_html='<a class="btn btn-primary" href="/collection/new">新建任务合集</a><a class="btn btn-gray" href="/tasks">返回任务</a>',
    )
    body = f"""
{header}
<nav class="fls-section-nav" aria-label="任务合集区块导航">
    <span class="fls-section-nav-label">任务合集</span>
    <a href="#collection-filters">查询合集</a>
    <a href="#collection-list">合集列表</a>
</nav>
<section class="section" id="collection-filters">
<form method="get">
<div class="data-toolbar" id="collection-filter-toolbar">
    <div class="form-grid">
        <div class="form-item">
            <label>搜索合集</label>
            <input name="q" value="{h(q)}" placeholder="合集名 / 备注 / 合集内任务" aria-label="搜索合集">
        </div>

        <div class="form-item">
            <label>搜索可加入任务</label>
            <input name="task_q" value="{h(task_q)}" placeholder="任务名 / 备注 / 命令 / Cron" aria-label="搜索可加入任务">
        </div>
    </div>

    <br>

    <div class="action-row">
        <button class="btn btn-primary" type="submit">查询任务合集</button>
        <a class="btn btn-gray" href="/collections">重置筛选</a>
    </div>
</div>
</form>
</section>

<section class="section" id="collection-list">
<div id="collectionsPageBlock">
{body_cols}

{page_links_html}
</div>
</section>

{bulk_assets}
"""
    return layout("任务合集", "collections", body)


@bp.route("/collection/new", methods=["GET", "POST"])
def collection_new():
    if request.method == "POST":
        return _save_collection_form("")

    return layout("新建任务合集", "collections", _collection_form())


@bp.route("/collection/edit/<collection_id>", methods=["GET", "POST"])
def collection_edit(collection_id):
    if request.method == "POST":
        return _save_collection_form(collection_id)
    collections = load_collections()
    item = None

    for c in collections:
        if c.get("id") == collection_id:
            item = c
            break

    if not item:
        abort(404)

    return layout("编辑任务合集", "collections", _collection_form(item, version=collection_version(item)))


def _save_collection_form(collection_id):
    data = dict(request.form.to_dict(), collection_id=collection_id)
    data.setdefault("request_id", uuid.uuid4().hex)
    payload, status = save_collection(data, load_collections=load_collections,
        save_collections=save_collections, unique_collection_name=unique_collection_name)
    if status == 200:
        return redirect(url_for("tasks.collections_page"))
    body = f'''<section class="section" role="alert" data-collection-save-result="{h(json.dumps(payload, ensure_ascii=False))}">
<p>{h(payload['msg'])}</p></section>'''
    if payload.get("saved"):
        body += f'<a class="btn btn-primary" href="/collection/edit/{h(payload["collection_id"])}">查看已保存合集</a>'
    else:
        draft = {"id": collection_id, "name": data.get("name", ""), "remark": data.get("remark", "")}
        if status == 404:
            draft["id"] = ""
            body += '<p>原合集已删除，可将草稿保存为新合集。</p>'
        current = payload.get("current")
        if status == 400:
            if payload.get("invalid_field") == "request_id":
                data["request_id"] = uuid.uuid4().hex
            if payload.get("invalid_field") == "collection_version":
                if not collection_id:
                    data.pop("collection_version", None)
                else:
                    try:
                        current = get_collection(collection_id)
                        payload["conflict"] = bool(current)
                    except Exception as exc:
                        body += f'<p>当前合集读取失败：{h(exc)}</p>'
        conflict = bool(payload.get("conflict") and current and collection_id)
        if conflict:
            body += f'<section class="section"><h3>当前已保存内容</h3><p>{h(current["name"])}</p><p>{h(current["remark"])}</p></section>'
        body += _collection_form(draft, request_id=uuid.uuid4().hex if conflict or status == 404 else data["request_id"],
            version=collection_version(current) if conflict else data.get("collection_version") if status != 404 else None,
            confirm_retry=payload.get("confirm_required", False), conflict=conflict)
    body += '<a class="btn btn-gray" href="/collections">查看任务合集</a>'
    return layout("合集保存恢复", "collections", body), status


@bp.route("/collection/delete/<collection_id>", methods=["POST"])
def collection_delete(collection_id):
    data = request.form.to_dict()
    data.setdefault("request_id", uuid.uuid4().hex)
    payload, status = delete_collection(collection_id, data, load_collections=load_collections,
        save_collections=save_collections, load_tasks=load_tasks, mutate_tasks=mutate_tasks)
    back = get_back_url("/collections")
    if status == 200:
        return redirect(back)
    recovery = ""
    if not payload.get("saved") and not payload.get("reselection_required"):
        request_id = data["request_id"]
        if isinstance(request_id, str) and len(request_id) == 32:
            fields = "".join(f'<input type="hidden" name="{name}" value="{h(data[name])}">'
                             for name in ("expected_members", "collection_version") if name in data)
            recovery = f'''<form method="post" action="/collection/delete/{h(collection_id)}?back={_back_param(back)}">
<input type="hidden" name="request_id" value="{h(request_id)}">{fields}
<button class="btn btn-primary" type="submit" onclick="return confirm('确认继续删除该合集？')">继续删除合集</button></form>'''
    body = f'''<section class="section" role="alert" data-task-execution-result="{h(json.dumps(payload, ensure_ascii=False))}">
<h3>合集删除恢复</h3><p>{h(payload['msg'])}</p>{recovery}
<a class="btn btn-gray" href="{h(back)}">查看任务合集</a></section>'''
    return layout("合集删除恢复", "collections", body), status


@bp.route("/collection/add-task/<collection_id>", methods=["POST"])
def collection_add_task(collection_id):
    from ...task_bulk import execute_assign_collection
    from ...ui.components import message_card

    back_url = get_back_url("/collections")
    task_ids = _collection_task_ids_from_form()
    if not task_ids and "request_id" not in request.form:
        if not get_collection(collection_id):
            abort(404)
        return redirect(back_url)
    data = dict(request.form.to_dict(), task_ids=task_ids)
    data.setdefault("request_id", uuid.uuid4().hex)
    try:
        payload, status = execute_assign_collection(collection_id, data, get_collection=get_collection,
                                                    load_tasks=load_tasks, mutate_tasks=mutate_tasks)
    except Exception as exc:
        payload, status = {"ok": False, "msg": str(exc)}, 500
    if status == 200:
        return redirect(back_url)
    retry_ids = payload.get("retry_ids") or payload.get("unknown_ids")
    recovery = ""
    if status != 404 and not (payload.get("saved") and not retry_ids):
        request_id = uuid.uuid4().hex if retry_ids else data["request_id"]
        if isinstance(request_id, str) and len(request_id) == 32:
            selected = retry_ids or task_ids
            fields = "".join(f'<input type="hidden" name="task_ids" value="{h(task_id)}">' for task_id in selected)
            expected = payload.get("expected_collections", data.get("expected_collections"))
            if expected is not None:
                encoded = json.dumps(expected, ensure_ascii=False) if isinstance(expected, dict) else expected
                fields += f'<input type="hidden" name="expected_collections" value="{h(encoded)}">'
            recovery = f'''<form method="post" action="/collection/add-task/{h(collection_id)}?back={_back_param(back_url)}">
<input type="hidden" name="request_id" value="{h(request_id)}">{fields}
<button class="btn btn-primary" type="submit" onclick="return confirm('确认任务合集归属后继续加入？')">{'重试加入合集' if retry_ids else '查询本次操作结果'}</button></form>'''
    body = message_card(payload["msg"], "error", strong=True, title="合集加入恢复") + f'''
<section class="section" role="alert" data-task-execution-result="{h(json.dumps(payload, ensure_ascii=False))}">
<p>{h(payload['msg'])}</p>{recovery}<a class="btn btn-gray" href="{h(back_url)}">查看任务合集</a></section>'''
    return layout("合集加入恢复", "collections", body), status
