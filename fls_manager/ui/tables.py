from ..utils import h
from ..task_runner import is_running, safe_process_name
from ..state import RUNNING
from ..scheduler import get_task_next_run_time_text


def _task_action_buttons(task_id, enabled):
    toggle_text = "禁用" if enabled else "启用"
    toggle_class = "btn-gray" if enabled else "btn-primary"

    return f"""
<div class="task-actions">
    <button class="btn btn-primary" type="button" onclick="taskAjaxAction('run','{h(task_id)}')">运行</button>
    <button class="btn btn-red" type="button" onclick="taskAjaxAction('stop','{h(task_id)}')">结束</button>
    <a class="btn btn-orange" href="/log/{h(task_id)}">日志</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}">编辑</a>
    <button class="btn {toggle_class}" type="button" onclick="taskAjaxAction('toggle','{h(task_id)}')">{h(toggle_text)}</button>
    <button class="btn btn-gray" type="button" onclick="taskAjaxAction('delete','{h(task_id)}')">删除</button>
</div>
"""


def tasks_table(tasks):
    """
    桌面端：普通表格
    手机端：卡片布局，操作按钮放到底部第二排。
    """
    desktop_rows = ""
    mobile_cards = ""

    if not tasks:
        desktop_rows = '<tr><td colspan="10">暂无任务，请点击新建任务</td></tr>'
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
            run_count = int(task.get("run_count", 0))

            running = is_running(task_id)
            pid = RUNNING.get(task_id, {}).get("pid", "-") if running else "-"
            process_name = RUNNING.get(task_id, {}).get("process_name", "-") if running else safe_process_name(name)

            enabled_badge = '<span class="badge green">启用</span>' if enabled else '<span class="badge gray">禁用</span>'
            status_badge = '<span class="badge blue">运行中</span>' if running else '<span class="badge red">已停止</span>'

            actions = _task_action_buttons(task_id, enabled)

            remark_html = ""
            if remark:
                remark_html = f'<div class="help" style="margin-top:4px;">备注：{h(remark)}</div>'

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
    <td><b>{h(name)}</b>{remark_html}</td>
    <td>{h(command)}</td>
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
<div class="task-mobile-card" data-task-id="{h(task_id)}">
    <div class="task-mobile-head">
        <div>
            <div class="task-mobile-title">{h(name)}</div>
            {f'<div class="help" style="margin-top:4px;">备注：{h(remark)}</div>' if remark else ''}
        </div>
        <div class="task-mobile-badges">
            {enabled_badge}
            {status_badge}
        </div>
    </div>

    <div class="task-mobile-info">
        {remark_mobile}
        <div class="task-mobile-item">
            <div class="task-mobile-label">命令</div>
            <div class="task-mobile-value code-like">{h(command)}</div>
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
"""

    html_text = f"""
<style>
#tasksMobileCards {{
    display:none;
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

.task-mobile-card {{
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:14px;
    padding:14px;
    margin-bottom:14px;
    box-shadow:0 4px 16px rgba(0,0,0,.04);
}}

.task-mobile-head {{
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    gap:10px;
    margin-bottom:12px;
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

body.fls-mobile #tasksTableDesktop {{
    display:none!important;
}}

body.fls-mobile #tasksMobileCards {{
    display:block!important;
}}

@media(max-width:900px) {{
    #tasksTableDesktop {{
        display:none!important;
    }}

    #tasksMobileCards {{
        display:block!important;
    }}
}}

@media(max-width:520px) {{
    .task-mobile-info {{
        grid-template-columns:1fr 1fr;
        gap:8px;
    }}

    .task-mobile-card {{
        padding:12px;
        border-radius:12px;
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
}}
</style>

<div id="tasksBlock">
    <div class="table-wrap" id="tasksTableDesktop">
        <table id="tasksTable">
            <thead>
                <tr>
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
async function taskAjaxAction(action, taskId){{
    if(action === "delete"){{
        if(!confirm("确定删除该任务吗？")) return;
    }}

    if(action === "stop"){{
        if(!confirm("确定结束该任务吗？")) return;
    }}

    const rows = document.querySelectorAll('[data-task-id="' + CSS.escape(taskId) + '"]');
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
</script>
"""

    return html_text
