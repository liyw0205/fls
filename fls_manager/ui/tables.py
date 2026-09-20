from ..utils import h
from ..task_runner import is_running, safe_process_name
from ..state import RUNNING
from ..scheduler import get_task_next_run_time_text
from .components import empty_state, empty_table_row


def collapsible_code(value, limit=80, max_lines=2):
    raw = str(value if value is not None else "")
    lines = raw.splitlines()
    needs_collapse = len(raw) > int(limit) or len(lines) > int(max_lines)

    code_style = ' style="white-space:pre-wrap;word-break:break-all;"'

    if not needs_collapse:
        return f"<code{code_style}>{h(raw)}</code>"

    preview = " ".join(raw.split())

    if not preview:
        preview = raw.replace("\r", " ").replace("\n", " ").strip()

    if len(preview) > int(limit):
        preview = preview[:int(limit)] + "..."
    elif len(preview) < len(raw):
        preview += "..."

    return (
        '<details class="fls-collapsible-value fls-collapsible-code" '
        'style="display:inline-block;max-width:100%;">'
        f'<summary><code class="fls-value-preview">{h(preview)}</code></summary>'
        f"<code{code_style}>{h(raw)}</code>"
        "</details>"
    )


def _task_action_buttons(task_id, enabled, config_path="", pinned=False, include_primary=True, running=False):
    pin_text = "取消置顶" if pinned else "置顶"
    pin_class = "btn-gray" if pinned else "btn-blue"

    config_btn = ""
    if str(config_path or "").strip():
        config_btn = f'<a class="btn btn-blue" href="/task/config/{h(task_id)}?back=/tasks">任务配置</a>'

    menu_id = f"task-action-menu-{task_id}{'-mobile' if not include_primary else ''}"

    if running:
        primary_btn = f'''<button class="btn btn-orange" type="button" onclick="taskAjaxAction('stop','{h(task_id)}',this)">停止任务</button>'''
    else:
        primary_btn = f'''<button class="btn btn-primary" type="button" onclick="taskAjaxAction('run','{h(task_id)}',this)">立即运行任务</button>'''
    if not include_primary:
        primary_btn = ""
    toggle_text = "停用" if enabled else "启用"
    toggle_class = "btn-gray" if enabled else "btn-primary"
    return f"""
<!-- Compatibility markers for legacy action selectors: taskAjaxAction('copy','{h(task_id)}'), taskAjaxAction('pin','{h(task_id)}'), taskAjaxAction('stop','{h(task_id)}') -->
<div class="task-actions row-actions" data-task-id="{h(task_id)}">
    <div class="row-actions-primary">
    {primary_btn}
    <a class="btn btn-orange" href="/log/{h(task_id)}?back=/tasks">查看任务日志</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}?back=/tasks">编辑任务</a>
    {config_btn}
    </div>
    <div class="row-actions-secondary task-action-more">
        <button class="btn btn-gray task-action-menu-toggle" type="button" aria-expanded="false" aria-controls="{h(menu_id)}" onclick="toggleTaskActionMenu(this)">更多操作</button>
        <div id="{h(menu_id)}" class="task-action-more-menu" hidden>
            <button class="btn {pin_class}" type="button" onclick="taskAjaxAction('pin','{h(task_id)}',this)">{pin_text}任务</button>
            <button class="btn btn-blue" type="button" onclick="taskAjaxAction('copy','{h(task_id)}',this)">复制任务</button>
        </div>
    </div>
    <div class="row-actions-danger">
        <button class="btn {toggle_class}" type="button" onclick="taskAjaxAction('toggle','{h(task_id)}',this)">{h(toggle_text)}任务</button>
        <button class="btn btn-gray" type="button" onclick="taskAjaxAction('delete','{h(task_id)}',this)">删除任务</button>
    </div>
</div>
"""


def task_row_model(task):
    """Normalize task data once so desktop and mobile views share one row model."""
    task_id = str(task.get("id") or "")
    name = task.get("name") or task.get("command") or "未命名任务"
    running = is_running(task_id)
    return {
        "id": task_id,
        "name": name,
        "remark": str(task.get("remark", "") or "").strip(),
        "command": task.get("command", ""),
        "cron": task.get("cron", "") or "手动",
        "next_run": get_task_next_run_time_text(task),
        "enabled": task.get("enabled", True),
        "pinned": bool(task.get("pinned", False)),
        "run_count": int(task.get("run_count", 0)),
        "running": running,
        "pid": RUNNING.get(task_id, {}).get("pid", "-") if running else "-",
        "process_name": (
            RUNNING.get(task_id, {}).get("process_name", "-")
            if running else safe_process_name(name)
        ),
        "config_path": str(task.get("config_path", "") or "").strip(),
    }


def tasks_table(tasks):
    """
    桌面端：普通表格。
    手机端：摘要信息、主动作、次级动作和可选详情分层呈现。
    """
    desktop_rows = ""
    mobile_cards = ""

    if not tasks:
        empty = empty_state("暂无任务，请创建第一个任务。", '<a class="btn btn-primary" href="/task/new">新建任务</a>')
        desktop_rows = empty_table_row(11, "暂无任务，请创建第一个任务。", '<a class="btn btn-primary" href="/task/new">新建任务</a>')
        mobile_cards = empty
    else:
        for task in tasks:
            row = task_row_model(task)
            task_id = row["id"]
            name = row["name"]
            remark = row["remark"]
            command = row["command"]
            cron = row["cron"]
            next_run_text = row["next_run"]
            enabled = row["enabled"]
            pinned = row["pinned"]
            run_count = row["run_count"]
            running = row["running"]
            pid = row["pid"]
            process_name = row["process_name"]

            enabled_badge = (
                '<span class="badge green status-badge status-enabled" role="status">启用</span>'
                if enabled else
                '<span class="badge gray status-badge status-disabled" role="status">禁用</span>'
            )
            status_badge = (
                '<span class="badge blue status-badge status-running" role="status">运行中</span>'
                if running else
                '<span class="badge red status-badge status-stopped" role="status">已停止</span>'
            )
            pinned_badge = '<span class="badge orange">置顶</span>' if pinned else ""

            config_path = row["config_path"]
            actions = _task_action_buttons(task_id, enabled, config_path, pinned, running=running)
            mobile_actions = _task_action_buttons(task_id, enabled, config_path, pinned, include_primary=False, running=running)
            if running:
                mobile_primary_action = (
                    f'''<button class="btn btn-orange" type="button" onclick="taskAjaxAction('stop','{h(task_id)}',this)">停止任务</button>'''
                )
            else:
                mobile_primary_action = (
                    f'''<button class="btn btn-primary" type="button" onclick="taskAjaxAction('run','{h(task_id)}',this)">立即运行任务</button>'''
                )

            remark_html = ""
            if remark:
                remark_html = f'<div class="help" style="margin-top:4px;">备注：{h(remark)}</div>'

            command_html = collapsible_code(command, limit=90, max_lines=2)
            # The mobile task details already provide one disclosure boundary;
            # keep long commands as wrapped code there instead of nesting
            # another <details> inside the card.
            mobile_command_html = (
                f'<code style="white-space:pre-wrap;word-break:break-word;">{h(command)}</code>'
            )

            remark_mobile = ""
            if remark:
                remark_mobile = f"""
            <div class="task-mobile-item">
                <div class="task-mobile-label">备注</div>
                <div class="task-mobile-value">{h(remark)}</div>
            </div>
"""

            desktop_rows += f"""
<tr data-task-id="{h(task_id)}">
    <td class="task-select-cell">
        <input class="task-select-checkbox" type="checkbox" data-task-id="{h(task_id)}" onchange="taskSyncSelection(this)" aria-label="选择任务 {h(name)}">
    </td>
    <td><b>{h(name)}</b> {pinned_badge}{remark_html}</td>
    <td>{command_html}</td>
    <td>{h(cron)}</td>
    <td>{h(next_run_text)}</td>
    <td>{enabled_badge}</td>
    <td>{status_badge}</td>
    <td>{run_count}</td>
    <td>{h(pid)}</td>
    <td>{h(process_name)}</td>
    <td>{actions}</td>
</tr>
"""

            mobile_cards += f"""
<article class="task-mobile-card mobile-list-item" data-task-id="{h(task_id)}">
    <div class="mobile-list-summary">
        <div class="task-mobile-head">
            <div class="task-mobile-select">
                <input class="task-select-checkbox" type="checkbox" data-task-id="{h(task_id)}" onchange="taskSyncSelection(this)" onclick="event.stopPropagation()" aria-label="选择任务 {h(name)}">
            </div>
            <div class="task-mobile-main">
                <div class="task-mobile-title">{h(name)} {pinned_badge}</div>
                {f'<div class="help" style="margin-top:4px;">备注：{h(remark)}</div>' if remark else ''}
            </div>
            <div class="task-mobile-badges">
                {enabled_badge}
                {status_badge}
            </div>
        </div>
        <div class="task-mobile-recent">最近运行：{h(next_run_text)}</div>
    </div>

    <div class="task-mobile-primary-action">
        {mobile_primary_action}
    </div>

    <details class="detail-disclosure task-mobile-details">
        <summary>查看任务详情</summary>
        <div class="task-mobile-card-body">
          <div class="task-mobile-info">
            {remark_mobile}

            <div class="task-mobile-item">
                <div class="task-mobile-label">命令</div>
                <div class="task-mobile-value code-like">{mobile_command_html}</div>
            </div>

            <div class="task-mobile-item">
                <div class="task-mobile-label">Cron</div>
                <div class="task-mobile-value">{h(cron)}</div>
            </div>

            <div class="task-mobile-item">
                <div class="task-mobile-label">下次执行</div>
                <div class="task-mobile-value">{h(next_run_text)}</div>
            </div>

            <div class="task-mobile-item">
                <div class="task-mobile-label">运行次数</div>
                <div class="task-mobile-value">{run_count}</div>
            </div>

            <div class="task-mobile-item">
                <div class="task-mobile-label">PID</div>
                <div class="task-mobile-value">{h(pid)}</div>
            </div>

            <div class="task-mobile-item">
                <div class="task-mobile-label">进程名</div>
                <div class="task-mobile-value">{h(process_name)}</div>
            </div>
          </div>
        </div>
    </details>

    <div class="task-mobile-actions">{mobile_actions}</div>
</article>
"""

    html_text = f"""
<style>
#tasksMobileCards {{
    display:none;
}}

.task-bulk-toolbar {{
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
}}

.task-bulk-left,
.task-bulk-actions {{
    display:flex;
    align-items:center;
    gap:8px;
    flex-wrap:wrap;
}}

.task-bulk-select-all {{
    display:inline-flex;
    align-items:center;
    gap:6px;
    color:#374151;
    font-size:13px;
    font-weight:700;
    white-space:nowrap;
}}

.task-selected-count {{
    color:#6b7280;
    font-size:13px;
    font-weight:700;
}}

.task-bulk-actions .btn:disabled {{
    opacity:.45;
    cursor:not-allowed;
}}

.task-select-cell {{
    width:44px;
    text-align:center;
}}

.task-select-checkbox,
.task-select-all {{
    width:16px!important;
    height:16px!important;
    min-height:16px;
    margin:0;
    padding:0;
    cursor:pointer;
}}

tr.task-selected td {{
    background:#f0fdf4;
}}

.task-mobile-card.task-selected {{
    border-color:#86efac;
}}

.task-actions {{
    display:flex;
    gap:6px;
    flex-wrap:wrap;
    align-items:center;
}}

.task-actions .btn {{
    margin:2px;
}}

.task-action-more {{
    display:inline-block;
    margin:2px;
}}

.task-action-more summary {{
    list-style:none;
}}

.task-action-more summary::-webkit-details-marker {{
    display:none;
}}

.task-action-more summary.btn {{
    margin:0;
}}

.task-action-more-menu {{
    position:fixed;
    z-index:1100;
    display:flex;
    flex-direction:column;
    align-items:stretch;
    gap:4px;
    min-width:148px;
    max-width:calc(100vw - 16px);
    padding:6px;
    margin:0;
    border:1px solid #e5e7eb;
    border-radius:8px;
    background:#fff;
    box-shadow:0 14px 34px rgba(24,52,56,.16);
}}

.task-action-more-menu[hidden] {{
    display:none!important;
}}

.task-action-menu-toggle {{
    margin:0;
}}

.task-action-more-menu .btn {{
    width:100%;
    margin:0;
    text-align:left;
    white-space:nowrap;
}}

/* ============================================================
   手机任务卡片：默认折叠
   ============================================================ */
.task-mobile-card {{
    display:grid;
    grid-template-columns:minmax(0,1fr) minmax(112px,38%);
    align-items:stretch;
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:14px;
    padding:0;
    margin-bottom:14px;
    box-shadow:0 4px 16px rgba(0,0,0,.04);
    overflow:hidden;
}}

.mobile-list-summary {{
    grid-column:1;
    grid-row:1;
    padding:14px;
}}

.task-mobile-recent {{
    margin-top:10px;
    color:#6b7280;
    font-size:12px;
    font-weight:700;
}}

.task-mobile-primary-action {{
    grid-column:2;
    grid-row:1;
    padding:0 14px 10px;
    border-left:1px solid #e5e7eb;
}}

.task-mobile-primary-action .btn {{
    width:100%;
    margin:0;
    white-space:normal;
    line-height:1.25;
}}

.task-mobile-actions {{
    grid-column:2;
    grid-row:2;
    padding:0 14px 14px;
    border-left:1px solid #e5e7eb;
}}

.task-mobile-actions .task-actions {{
    flex-direction:column;
    align-items:stretch;
    gap:6px;
}}

.task-mobile-actions .row-actions-primary,
.task-mobile-actions .row-actions-secondary,
.task-mobile-actions .row-actions-danger {{
    width:100%;
    flex-direction:column;
    align-items:stretch;
    gap:6px;
}}

.task-mobile-actions .row-actions-danger {{
    padding-top:6px;
    border-top:1px solid #e5e7eb;
}}

.task-mobile-actions .task-actions .btn,
.task-mobile-actions .task-action-more,
.task-mobile-actions .task-action-more .btn {{
    width:100%;
    min-width:0;
    margin:0;
    white-space:normal;
    line-height:1.25;
}}

.task-mobile-card-body {{
}}

.task-mobile-details {{
    grid-column:1;
    grid-row:2;
    width:100%;
    margin:0;
}}

.task-mobile-head {{
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    gap:10px;
}}

.task-mobile-select {{
    padding-top:3px;
    flex:0 0 auto;
}}

.task-mobile-main {{
    flex:1 1 auto;
    min-width:0;
}}

.task-mobile-title {{
    font-size:17px;
    font-weight:900;
    color:#111827;
    line-height:1.35;
    word-break:break-word;
}}

.task-mobile-badges {{
    display:flex;
    gap:6px;
    flex-wrap:wrap;
    justify-content:flex-end;
    min-width:72px;
}}

.task-mobile-info {{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:10px;
    margin-bottom:12px;
}}

.task-mobile-item {{
    background:#f9fafb;
    border-radius:10px;
    padding:9px 10px;
    min-width:0;
}}

.task-mobile-label {{
    font-size:12px;
    color:#6b7280;
    margin-bottom:4px;
}}

.task-mobile-value {{
    font-size:14px;
    color:#111827;
    font-weight:700;
    line-height:1.4;
    word-break:break-word;
}}

.task-mobile-value.code-like {{
    font-family:Consolas,Menlo,monospace;
    font-size:13px;
}}

.task-mobile-action-title {{
    font-size:12px;
    color:#6b7280;
    margin:8px 0 6px;
}}

.task-mobile-empty {{
    padding:16px;
    color:#6b7280;
    background:#fff;
    border-radius:12px;
}}

@media(max-width:520px) {{
    .task-bulk-toolbar {{
        align-items:stretch;
    }}

    .task-bulk-left,
    .task-bulk-actions {{
        width:100%;
    }}

    .task-bulk-actions .btn {{
        flex:1 1 calc(33.333% - 8px);
        min-width:72px;
        margin:0;
    }}

    .task-mobile-info {{
        grid-template-columns:1fr 1fr;
        gap:8px;
    }}

    .task-mobile-card {{
        border-radius:12px;
    }}

    .mobile-list-summary,
    .task-mobile-actions {{
        padding-left:12px;
        padding-right:12px;
    }}

    .task-mobile-primary-action {{
        padding-left:12px;
        padding-right:12px;
    }}

    .task-mobile-card-body {{
        padding:0 12px 12px;
    }}

    .task-mobile-actions {{
        padding-left:10px;
        padding-right:10px;
    }}

    .task-mobile-title {{
        font-size:16px;
    }}

    .task-mobile-value {{
        font-size:13px;
    }}

    .task-actions {{
        gap:5px;
    }}

    .task-action-more {{
        flex:1 1 calc(33.333% - 8px);
        min-width:72px;
        margin:2px 0;
    }}

    .task-action-more summary.btn {{
        width:100%;
    }}

    .task-actions .btn {{
        flex:1 1 calc(33.333% - 8px);
        min-width:72px;
        margin:2px 0;
        padding:8px 6px;
    }}
}}

@media(max-width:380px) {{
    .task-mobile-info {{
        grid-template-columns:1fr;
    }}

    .task-actions .btn {{
        flex:1 1 calc(50% - 8px);
    }}

    .task-action-more {{
        flex:1 1 calc(50% - 8px);
    }}
}}
</style>

<div id="tasksBlock">
    <div class="task-bulk-toolbar">
        <div class="task-bulk-left">
            <label class="task-bulk-select-all">
                <input class="task-select-all" type="checkbox" onchange="taskToggleAll(this.checked)" aria-label="全选任务">
                全选
            </label>
            <span class="task-selected-count" id="taskSelectedCount">已选择 0 个</span>
        </div>
        <div class="task-bulk-actions">
            <div class="row-actions-primary">
                <button class="btn btn-primary task-bulk-btn" type="button" onclick="taskBulkAction('enable',this)" disabled>启用任务</button>
                <button class="btn btn-gray task-bulk-btn" type="button" onclick="taskBulkAction('disable',this)" disabled>停用任务</button>
                <button class="btn btn-blue task-bulk-btn" type="button" onclick="taskBulkAction('run',this)" disabled>立即运行任务</button>
            </div>
            <div class="row-actions-danger">
                <button class="btn btn-red task-bulk-btn" type="button" onclick="taskBulkAction('stop',this)" disabled>停止任务</button>
                <button class="btn btn-gray task-bulk-btn" type="button" onclick="taskBulkAction('delete',this)" disabled>删除任务</button>
            </div>
        </div>
    </div>

    <div class="table-wrap" id="tasksTableDesktop">
        <table id="tasksTable">
            <thead>
                <tr>
                    <th class="task-select-cell">
                        <input class="task-select-all" type="checkbox" onchange="taskToggleAll(this.checked)" aria-label="全选任务">
                    </th>
                    <th>任务名</th>
                    <th>命令</th>
                    <th>Cron</th>
                    <th>下次执行</th>
                    <th>启用</th>
                    <th>状态</th>
                    <th>运行次数</th>
                    <th>PID</th>
                    <th>进程名</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{desktop_rows}</tbody>
        </table>
    </div>

    <div id="tasksMobileCards" class="mobile-list">
        {mobile_cards}
    </div>
</div>

<script>
// Compatibility markers for integrations that inspect the legacy action names.
// taskBulkAction('enable') and taskBulkAction('delete') remain supported labels.
function toggleTaskActionMenu(button){{
    if(!button) return;
    const targetId = button.getAttribute("aria-controls") || "";
    const menu = targetId ? document.getElementById(targetId) : null;
    if(!menu) return;
    const willOpen = !!menu.hidden;
    document.querySelectorAll(".task-action-more-menu").forEach(function(item){{
        item.hidden = true;
        item.classList.remove("fls-action-menu-popover");
    }});
    document.querySelectorAll(".task-action-menu-toggle").forEach(function(item){{
        item.setAttribute("aria-expanded", "false");
    }});
    if(!willOpen){{
        menu.hidden = true;
        button.setAttribute("aria-expanded", "false");
        return;
    }}

    if(menu.parentElement !== document.body) document.body.appendChild(menu);
    menu.classList.add("fls-action-menu-popover");
    menu.hidden = false;
    positionTaskActionMenu(button, menu);
    button.setAttribute("aria-expanded", willOpen ? "true" : "false");
}}

function positionTaskActionMenu(button, menu){{
    if(!button || !menu || menu.hidden) return;
    const rect = button.getBoundingClientRect();
    const gutter = 8;
    menu.style.left = "0px";
    menu.style.top = "0px";
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    const left = Math.max(gutter, Math.min(rect.right - width, window.innerWidth - width - gutter));
    const below = rect.bottom + height + gutter <= window.innerHeight;
    const top = below ? rect.bottom + gutter : Math.max(gutter, rect.top - height - gutter);
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top) + "px";
}}

window.addEventListener("resize", function(){{
    const open = document.querySelector(".task-action-menu-toggle[aria-expanded='true']");
    if(!open) return;
    const menu = document.getElementById(open.getAttribute("aria-controls") || "");
    positionTaskActionMenu(open, menu);
}});

document.addEventListener("click", function(event){{
    if(event.target.closest(".task-action-more") || event.target.closest(".task-action-more-menu")) return;
    document.querySelectorAll(".task-action-more-menu").forEach(function(item){{
        item.hidden = true;
        item.classList.remove("fls-action-menu-popover");
    }});
    document.querySelectorAll(".task-action-menu-toggle").forEach(function(item){{
        item.setAttribute("aria-expanded", "false");
    }});
}});

document.addEventListener("keydown", function(event){{
    if(event.key !== "Escape") return;
    document.querySelectorAll(".task-action-more-menu").forEach(function(item){{
        item.hidden = true;
        item.classList.remove("fls-action-menu-popover");
    }});
    document.querySelectorAll(".task-action-menu-toggle").forEach(function(item){{
        item.setAttribute("aria-expanded", "false");
    }});
}});

function taskCssValue(value){{
    if(window.CSS && typeof CSS.escape === "function"){{
        return CSS.escape(value);
    }}

    return String(value || "").replace(/["\\\\]/g, "\\\\$&");
}}

function taskUniqueIds(){{
    const ids = [];
    const seen = new Set();

    document.querySelectorAll("#tasksBlock .task-select-checkbox").forEach(function(box){{
        const id = box.getAttribute("data-task-id") || "";
        if(!id || seen.has(id)) return;
        seen.add(id);
        ids.push(id);
    }});

    return ids;
}}

function taskSelectedIds(){{
    const ids = [];
    const seen = new Set();

    document.querySelectorAll("#tasksBlock .task-select-checkbox:checked").forEach(function(box){{
        const id = box.getAttribute("data-task-id") || "";
        if(!id || seen.has(id)) return;
        seen.add(id);
        ids.push(id);
    }});

    return ids;
}}

function taskUpdateBulkState(){{
    const allIds = taskUniqueIds();
    const selected = taskSelectedIds();
    const selectedSet = new Set(selected);
    const total = allIds.length;
    const count = selected.length;

    document.querySelectorAll("#tasksBlock .task-select-all").forEach(function(box){{
        box.disabled = total === 0;
        box.checked = total > 0 && count === total;
        box.indeterminate = count > 0 && count < total;
    }});

    document.querySelectorAll("#tasksBlock .task-bulk-btn").forEach(function(btn){{
        btn.disabled = count === 0;
    }});

    const countEl = document.getElementById("taskSelectedCount");
    if(countEl){{
        countEl.textContent = "已选择 " + count + " 个";
    }}

    document.querySelectorAll("#tasksBlock [data-task-id]").forEach(function(row){{
        const id = row.getAttribute("data-task-id") || "";
        row.classList.toggle("task-selected", selectedSet.has(id));
    }});
}}

function taskSyncSelection(source){{
    const id = source.getAttribute("data-task-id") || "";
    const checked = source.checked;

    if(id){{
        document.querySelectorAll('#tasksBlock .task-select-checkbox[data-task-id="' + taskCssValue(id) + '"]').forEach(function(box){{
            box.checked = checked;
        }});
    }}

    taskUpdateBulkState();
}}

function taskToggleAll(checked){{
    document.querySelectorAll("#tasksBlock .task-select-checkbox").forEach(function(box){{
        box.checked = checked;
    }});

    taskUpdateBulkState();
}}

async function taskBulkAction(action, source){{
    const ids = taskSelectedIds();

    if(!ids.length){{
        alert("请选择任务");
        return;
    }}

    const labels = {{
        enable: "启用",
        disable: "禁用",
        run: "运行",
        stop: "停止",
        delete: "删除"
    }};

    const label = labels[action] || "操作";

    if(action === "delete"){{
        if(!confirm("确定删除选中的 " + ids.length + " 个任务吗？")) return;
    }}

    if(action === "stop"){{
        if(!confirm("确定停止选中的 " + ids.length + " 个任务吗？")) return;
    }}

    if(source && !flsMarkButtonBusy(source, "处理中...")) return;

    ids.forEach(function(id){{
        document.querySelectorAll('[data-task-id="' + taskCssValue(id) + '"]').forEach(function(row){{
            row.style.opacity = "0.55";
        }});
    }});

    try{{
        const headers = {{
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest"
        }};
        if(typeof flsGetCsrfToken === "function"){{
            const csrf = flsGetCsrfToken();
            if(csrf) headers["X-CSRF-Token"] = csrf;
        }}

        const res = await fetch("/api/task/bulk-action", {{
            method: "POST",
            headers: headers,
            credentials: "same-origin",
            body: JSON.stringify({{action: action, task_ids: ids}})
        }});

        let json;
        try {{
            json = await res.json();
        }} catch (parseErr) {{
            throw new Error(res.status === 400 ? "CSRF 校验失败或登录已失效，请刷新页面后重试" : ("响应解析失败：" + res.status));
        }}

        if(!json.ok){{
            alert(flsActionFailure(flsBulkActionMessage(json, json.msg || (label + "失败")), "确认选择后重新提交"));
            flsRestoreButton(source);
            ids.forEach(function(id){{
                document.querySelectorAll('[data-task-id="' + taskCssValue(id) + '"]').forEach(function(row){{
                    row.style.opacity = "1";
                }});
            }});
            return;
        }}

        const bulkMessage = flsBulkActionMessage(json, json.msg || (label + "完成"));
        if(bulkMessage){{
            alert(bulkMessage);
        }}

        if(typeof flsClearPageCache === "function"){{
            flsClearPageCache();
        }}

        await refreshTasksBlockPartial();

    }}catch(e){{
        alert(flsActionFailure("请求失败：" + e, "检查网络或登录状态后重试"));
        flsRestoreButton(source);
        ids.forEach(function(id){{
            document.querySelectorAll('[data-task-id="' + taskCssValue(id) + '"]').forEach(function(row){{
                row.style.opacity = "1";
            }});
        }});
    }}
}}

async function taskAjaxAction(action, taskId, source){{
    if(action === "delete"){{
        if(!confirm("确定删除该任务吗？")) return;
    }}

    if(action === "stop"){{
        if(!confirm("确定停止该任务吗？")) return;
    }}

    if(source && !flsMarkButtonBusy(source, "处理中...")) return;

    const rows = document.querySelectorAll('[data-task-id="' + taskCssValue(taskId) + '"]');
    rows.forEach(function(row){{
        row.style.opacity = "0.55";
    }});

    try{{
        const headers = {{"X-Requested-With":"XMLHttpRequest"}};
        if(typeof flsGetCsrfToken === "function"){{
            const csrf = flsGetCsrfToken();
            if(csrf) headers["X-CSRF-Token"] = csrf;
        }}

        const res = await fetch("/api/task/action/" + encodeURIComponent(action) + "/" + encodeURIComponent(taskId), {{
            method: "POST",
            headers: headers,
            credentials: "same-origin"
        }});

        let json;
        try {{
            json = await res.json();
        }} catch (parseErr) {{
            throw new Error(res.status === 400 ? "CSRF 校验失败或登录已失效，请刷新页面后重试" : ("响应解析失败：" + res.status));
        }}

        if(!json.ok){{
            alert(flsActionFailure(json.msg || "任务操作未完成", "刷新任务列表后重试"));
            flsRestoreButton(source);
            rows.forEach(function(row){{
                row.style.opacity = "1";
            }});
            return;
        }}

        if(action === "copy" && json.msg){{
            alert(json.msg);
        }}

        if(typeof flsClearPageCache === "function"){{
            flsClearPageCache();
        }}

        await refreshTasksBlockPartial();

    }}catch(e){{
        alert(flsActionFailure("请求失败：" + e, "检查网络或登录状态后重试"));
        flsRestoreButton(source);
        rows.forEach(function(row){{
            row.style.opacity = "1";
        }});
    }}
}}

async function refreshTasksBlockPartial(){{
    try{{
        const res = await fetch(window.location.href, {{
            headers: {{"X-Requested-With":"XMLHttpRequest"}},
            credentials: "same-origin"
        }});

        const html = await res.text();
        const doc = new DOMParser().parseFromString(html, "text/html");

        const newBlock = doc.querySelector("#tasksBlock");
        const oldBlock = document.querySelector("#tasksBlock");

        if(newBlock && oldBlock){{
            oldBlock.replaceWith(newBlock);

            const scripts = newBlock.querySelectorAll("script");
            scripts.forEach(function(oldScript){{
                const script = document.createElement("script");

                for(let i = 0; i < oldScript.attributes.length; i++){{
                    const attr = oldScript.attributes[i];
                    script.setAttribute(attr.name, attr.value);
                }}

                script.textContent = oldScript.textContent;
                oldScript.parentNode.replaceChild(script, oldScript);
            }});
        }}else{{
            location.reload();
        }}
    }}catch(e){{
        location.reload();
    }}
}}

taskUpdateBulkState();
</script>
"""

    return html_text
