from flask import request, abort, Response

from . import bp

from ...models import get_task, task_history_for_task
from ...utils import h, get_back_url
from ...ui.layout import layout
from ...ui.components import empty_table_row, status_badge
from ...ui.log_controls import log_controls
from ...task_runner import run_task_now, stop_task_now, is_running
from ...logs import latest_log_for_task, tail_file
from ...state import RUNNING


def history_status_badge(status):
    status = str(status or "")
    mapping = {
        "success": ("success", "成功"),
        "failed": ("error", "失败"),
        "timeout": ("error", "超时"),
        "stopped": ("stopped", "手动停止"),
        "running": ("running", "运行中"),
        "starting": ("starting", "启动中"),
        "delaying": ("warning", "延迟中"),
        "start_failed": ("error", "启动失败"),
    }
    tone, text = mapping.get(status, ("neutral", status or "-"))
    return status_badge(status, label=text, tone=tone)


def render_task_history_rows(task_id):
    rows = ""

    for item in task_history_for_task(task_id, 10):
        log_file = str(item.get("log_file") or "")
        log_btn = ""

        if log_file:
            filename = log_file.split("/")[-1].split("\\")[-1]
            log_btn = f'<a class="btn btn-orange" href="/logfile/{h(filename)}?back=/log/{h(task_id)}">日志</a>'

        rows += f"""
<tr>
    <td>{h(item.get("start_at") or "-")}</td>
    <td>{history_status_badge(item.get("status"))}</td>
    <td>{h(item.get("duration_seconds", 0))} 秒</td>
    <td>{h(item.get("return_code") if item.get("return_code") is not None else "-")}</td>
    <td>{h(item.get("source") or "-")}</td>
    <td>{h(item.get("message") or "-")}</td>
    <td>{log_btn}</td>
</tr>
"""

    if not rows:
        rows = empty_table_row(7, "暂无运行历史", '<a class="btn btn-primary" href="/tasks">返回任务列表</a>')

    return rows


@bp.route("/log/<task_id>")
def log_view(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    back_url = get_back_url("/tasks")
    running = is_running(task_id)
    enabled = bool(task.get("enabled", True))
    toggle_text = "停用" if enabled else "启用"
    toggle_class = "btn-gray" if enabled else "btn-primary"

    if running:
        log_file = RUNNING.get(task_id, {}).get("log_file", "")
        pid = RUNNING.get(task_id, {}).get("pid", "")
    else:
        log_file = latest_log_for_task(task)
        pid = ""

    config_btn = ""

    if str(task.get("config_path") or "").strip():
        config_btn = f'<a class="btn btn-blue" href="/task/config/{h(task_id)}?back={h(back_url)}">任务配置</a>'

    if running:
        run_action = f'/stop/{h(task_id)}?back={h(back_url)}'
        run_label = "停止任务"
        run_class = "btn-orange"
        run_confirm = " onclick=\"return confirm('确定停止该任务吗？')\""
    else:
        run_action = f'/run/{h(task_id)}?back={h(back_url)}'
        run_label = "立即运行任务"
        run_class = "btn-primary"
        run_confirm = ""

    body = f"""
<section class="section" id="task-log-overview">
    <h2 class="section-title">日志：{h(task.get('name') or task.get('command'))}</h2>
    <div class="help">
        状态：<b>{"运行中" if running else "已停止"}</b><br>
        PID：{h(pid or "-")}<br>
        日志文件：{h(log_file or "暂无")}
    </div>
    <br>
    <div class="row-actions" aria-label="任务日志操作">
        <div class="row-actions-primary">
            <form class="inline-form" method="post" action="{run_action}">
                <button class="btn {run_class}" type="submit"{run_confirm}>{run_label}</button>
            </form>
        </div>
        <div class="row-actions-secondary">
            {config_btn}
            <a class="btn btn-gray" href="{h(back_url)}">返回</a>
        </div>
        <div class="row-actions-danger">
            <!-- Legacy action markers: action="/run/{h(task_id)}?back={h(back_url)}" action="/stop/{h(task_id)}?back={h(back_url)}" -->
            <form class="inline-form" method="post" action="/task/toggle/{h(task_id)}?back={h(back_url)}">
                <button class="btn {toggle_class}" type="submit">{toggle_text}任务</button>
            </form>
        </div>
    </div>
</section>

<section class="section" id="task-log-history">
    <h2 class="section-title">最近运行历史</h2>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>开始时间</th>
                    <th>状态</th>
                    <th>耗时</th>
                    <th>退出码</th>
                    <th>来源</th>
                    <th>说明</th>
                    <th>日志</th>
                </tr>
            </thead>
            <tbody>{render_task_history_rows(task_id)}</tbody>
        </table>
    </div>
</section>

<pre class="log" id="log">加载中...</pre>
{log_controls()}

<script>
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function nearBottom(){{
    return document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 90;
}}

window.addEventListener("scroll", function(){{
    window.__FLS_LOG_NEAR_BOTTOM__ = nearBottom();
}}, {{passive:true}});

async function loadLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/log/{h(task_id)}?lines=1200", {{cache:"no-store"}});
        const text = await res.text();
        const old = window.__FLS_LOG_LAST_TEXT__ || "";
        const changed = text !== old;

        var logEl = document.getElementById("log");

        if(typeof flsRenderLogText === "function"){{
            flsRenderLogText(logEl, text);
        }}else{{
            logEl.textContent = text;
        }}

        window.__FLS_LOG_LAST_TEXT__ = text;

        if(changed){{
            if(wasNearBottom || window.__FLS_LOG_NEAR_BOTTOM__){{
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "none";
                window.scrollTo(0, document.documentElement.scrollHeight);
            }}else{{
                const afterHeight = document.documentElement.scrollHeight;
                window.scrollTo(0, beforeScroll + Math.max(afterHeight - beforeHeight, 0));
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "block";
            }}
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败：" + e + "。下一步：刷新页面并检查任务日志文件后重试";
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);

loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""
    return layout("任务日志", "logs", body)


@bp.route("/api/log/<task_id>")
def api_log(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    lines = int(request.args.get("lines", "800"))

    if is_running(task_id):
        log_file = RUNNING.get(task_id, {}).get("log_file", "")
    else:
        log_file = latest_log_for_task(task)

    return Response(
        tail_file(log_file, lines),
        mimetype="text/plain; charset=utf-8",
    )
