from ..utils import h
from ..task_runner import is_running, safe_process_name
from ..state import RUNNING
from ..scheduler import get_task_next_run_time_text


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


def _task_action_buttons(task_id, enabled, config_path="", pinned=False):
    toggle_text = "禁用" if enabled else "启用"
    toggle_class = "btn-gray" if enabled else "btn-primary"

    pin_text = "取消置顶" if pinned else "置顶"
    pin_class = "btn-gray" if pinned else "btn-blue"

    config_btn = ""
    if str(config_path or "").strip():
        config_btn = f'<a class="btn btn-blue" href="/task/config/{h(task_id)}?back=/tasks">配置</a>'

    return f"""
<div class="task-actions">
    <button class="btn btn-primary" type="button" onclick="taskAjaxAction('run','{h(task_id)}')">运行</button>
    <a class="btn btn-orange" href="/log/{h(task_id)}?back=/tasks">日志</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}?back=/tasks">编辑</a>
    {config_btn}
    <details class="task-action-more">
        <summary class="btn btn-gray">更多</summary>
        <div class="task-action-more-menu">
            <button class="btn btn-red" type="button" onclick="taskAjaxAction('stop','{h(task_id)}')">结束</button>
            <button class="btn {pin_class}" type="button" onclick="taskAjaxAction('pin','{h(task_id)}')">{pin_text}</button>
            <button class="btn btn-blue" type="button" onclick="taskAjaxAction('copy','{h(task_id)}')">复制</button>
            <button class="btn {toggle_class}" type="button" onclick="taskAjaxAction('toggle','{h(task_id)}')">{h(toggle_text)}</button>
            <button class="btn btn-gray" type="button" onclick="taskAjaxAction('delete','{h(task_id)}')">删除</button>
        </div>
    </details>
</div>
"""


def tasks_table(tasks):
    """
    桌面端：普通表格。
    手机端：折叠卡片布局，默认折叠，避免单个任务卡片过长。
    """
    desktop_rows = ""
    mobile_cards = ""

    if not tasks:
        desktop_rows = '<tr><td colspan="11">暂无任务，请点击新建任务</td></tr>'
        mobile_cards = '<div class="task-mobile-empty">暂无任务，请点击新建任务</div>'
    else:
        for task in tasks:
            task_id = task["id"]
            name = task.get("name") or task.get("command") or "未命名任务"
            remark = str(task.get("remark", "") or "").strip()
            command = task.get("command", "")
            cron = task.get("cron", "") or "手动"
            next_run_text = get_task_next_run_time_text(task)
            enabled = task.get("enabled", True)
            pinned = bool(task.get("pinned", False))
            run_count = int(task.get("run_count", 0))

            running = is_running(task_id)
            pid = RUNNING.get(task_id, {}).get("pid", "-") if running else "-"
            process_name = (
                RUNNING.get(task_id, {}).get("process_name", "-")
                if running
                else safe_process_name(name)
            )

            enabled_badge = '<span class="badge green">启用</span>' if enabled else '<span class="badge gray">禁用</span>'
            status_badge = '<span class="badge blue">运行中</span>' if running else '<span class="badge red">已停止</span>'
            pinned_badge = '<span class="badge orange">置顶</span>' if pinned else ""

            config_path = str(task.get("config_path", "") or "").strip()
            actions = _task_action_buttons(task_id, enabled, config_path, pinned)

            remark_html = ""
            if remark:
                remark_html = f'<div class="help" style="margin-top:4px;">备注：{h(remark)}</div>'

            command_html = collapsible_code(command, limit=90, max_lines=2)

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
<details class="task-mobile-card" data-task-id="{h(task_id)}">
    <summary>
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
                {pinned_badge}
            </div>
        </div>
    </summary>

    <div class="task-mobile-card-body">
        <div class="task-mobile-info">
            {remark_mobile}

            <div class="task-mobile-item">
                <div class="task-mobile-label">命令</div>
                <div class="task-mobile-value code-like">{command_html}</div>
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

        <div class="task-mobile-action-title">操作</div>
        {actions}
    </div>
</details>
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
    display:flex;
    gap:6px;
    flex-wrap:wrap;
    padding:8px;
    margin-top:6px;
    border:1px solid #e5e7eb;
    border-radius:10px;
    background:#f9fafb;
}}

.task-action-more-menu .btn {{
    margin:0;
}}

/* ============================================================
   手机任务卡片：默认折叠
   ============================================================ */
.task-mobile-card {{
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:14px;
    padding:0;
    margin-bottom:14px;
    box-shadow:0 4px 16px rgba(0,0,0,.04);
    overflow:hidden;
}}

.task-mobile-card summary {{
    cursor:pointer;
    list-style:none;
    padding:14px;
}}

.task-mobile-card summary::-webkit-details-marker {{
    display:none;
}}

.task-mobile-card summary::after {{
    content:"点击展开";
    display:block;
    margin-top:8px;
    color:#6b7280;
    font-size:12px;
    font-weight:700;
}}

.task-mobile-card[open] summary::after {{
    content:"点击收起";
}}

.task-mobile-card-body {{
    padding:0 14px 14px;
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

/* JS 判断为手机时强制显示卡片 */
body.fls-mobile #tasksTableDesktop {{
    display:none!important;
}}

body.fls-mobile #tasksMobileCards {{
    display:block!important;
}}

/* 窄屏兜底 */
@media(max-width:900px) {{
    #tasksTableDesktop {{
        display:none!important;
    }}

    #tasksMobileCards {{
        display:block!important;
    }}
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

    .task-mobile-card summary {{
        padding:12px;
    }}

    .task-mobile-card-body {{
        padding:0 12px 12px;
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
                <input class="task-select-all" type="checkbox" onchange="taskToggleAll(this.checked)">
                全选
            </label>
            <span class="task-selected-count" id="taskSelectedCount">已选择 0 个</span>
        </div>
        <div class="task-bulk-actions">
            <button class="btn btn-primary task-bulk-btn" type="button" onclick="taskBulkAction('enable')" disabled>启用</button>
            <button class="btn btn-gray task-bulk-btn" type="button" onclick="taskBulkAction('disable')" disabled>禁用</button>
            <button class="btn btn-blue task-bulk-btn" type="button" onclick="taskBulkAction('run')" disabled>运行</button>
            <button class="btn btn-red task-bulk-btn" type="button" onclick="taskBulkAction('stop')" disabled>停止</button>
            <button class="btn btn-gray task-bulk-btn" type="button" onclick="taskBulkAction('delete')" disabled>删除</button>
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

    <div id="tasksMobileCards">
        {mobile_cards}
    </div>
</div>

<script>
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

async function taskBulkAction(action){{
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

    ids.forEach(function(id){{
        document.querySelectorAll('[data-task-id="' + taskCssValue(id) + '"]').forEach(function(row){{
            row.style.opacity = "0.55";
        }});
    }});

    try{{
        const res = await fetch("/api/task/bulk-action", {{
            method: "POST",
            headers: {{
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            }},
            credentials: "same-origin",
            body: JSON.stringify({{action: action, task_ids: ids}})
        }});

        const json = await res.json();

        if(!json.ok){{
            alert(flsBulkActionMessage(json, json.msg || (label + "失败")));
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
        alert("请求失败：" + e);
        ids.forEach(function(id){{
            document.querySelectorAll('[data-task-id="' + taskCssValue(id) + '"]').forEach(function(row){{
                row.style.opacity = "1";
            }});
        }});
    }}
}}

async function taskAjaxAction(action, taskId){{
    if(action === "delete"){{
        if(!confirm("确定删除该任务吗？")) return;
    }}

    if(action === "stop"){{
        if(!confirm("确定结束该任务吗？")) return;
    }}

    const rows = document.querySelectorAll('[data-task-id="' + taskCssValue(taskId) + '"]');
    rows.forEach(function(row){{
        row.style.opacity = "0.55";
    }});

    try{{
        const res = await fetch("/api/task/action/" + encodeURIComponent(action) + "/" + encodeURIComponent(taskId), {{
            method: "POST",
            headers: {{"X-Requested-With":"XMLHttpRequest"}},
            credentials: "same-origin"
        }});

        const json = await res.json();

        if(!json.ok){{
            alert(json.msg || "操作失败");
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
        alert("请求失败：" + e);
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
