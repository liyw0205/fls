from flask import request, jsonify

from . import bp
from .state import ABOUT_JOBS

from ...ui.layout import layout
from ...ui.components import page_header_card
from ...ui.log_controls import log_controls
from ...utils import h, get_back_url
from ...logs import tail_file


@bp.route("/about/job-log/<job_id>")
def about_job_log(job_id):
    back_url = get_back_url("/about")
    info = ABOUT_JOBS.get(job_id)

    if not info:
        body = page_header_card(
            "操作进度",
            help_html="""
        操作记录不存在或面板已重启。<br>
        可以返回关于页后重新发起操作。
""",
            actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/logs?back={h(back_url)}">查看日志管理</a>
""",
        )
        return layout("操作进度", "about", body)

    is_update_job = info.get("action") == "update-version"
    completed_successfully = not info.get("running") and info.get("returncode") == 0
    job_actions_hidden = "" if completed_successfully else " hidden"
    restart_action = ""
    if is_update_job:
        restart_action = """
<form method="post" action="/about/restart-panel">
    <button class="btn btn-primary" type="submit" onclick="return confirm('确定重启面板吗？重启期间页面会短暂无法访问。')">重启面板</button>
</form>
"""

    header_card = page_header_card(
        f'操作进度：{info.get("title") or job_id}',
        help_html=f"""
        状态：<b id="aboutJobStatus">{h(info.get("status") or "-")}</b><br>
        更新时间：<span id="aboutJobUpdatedAt">{h(info.get("updated_at") or "-")}</span>
""",
        actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回关于页</a>
<a class="btn btn-blue" href="/logs?back={h(back_url)}">日志管理</a>
""",
    )

    body = f"""
{header_card}
<pre class="log" id="log">加载中...</pre>
{log_controls()}
<div class="fls-job-actions action-row" id="aboutJobActions"{job_actions_hidden}>
    {restart_action}
    <a class="btn btn-gray" href="{h(back_url)}">返回关于页</a>
</div>

<script>
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function nearBottom(){{
    return document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 90;
}}

function updateAboutJobActions(json){{
    const box = document.getElementById("aboutJobActions");
    if(!box) return;
    const done = !json.running && Number(json.returncode) === 0;
    const isUpdate = json.action === "update-version";
    box.hidden = !done;
    box.querySelectorAll("form").forEach(function(form){{
        form.hidden = !isUpdate;
    }});
}}

window.addEventListener("scroll", function(){{
    window.__FLS_LOG_NEAR_BOTTOM__ = nearBottom();
}}, {{passive:true}});

async function loadAboutJobLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/about/job-log/{h(job_id)}?lines=1600", {{cache:"no-store"}});
        const json = await res.json();

        document.getElementById("aboutJobStatus").textContent = json.status || "-";
        document.getElementById("aboutJobUpdatedAt").textContent = json.updated_at || "-";
        updateAboutJobActions(json);

        const text = json.log || "暂无日志";
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

        if(!json.running){{
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "记录读取失败：" + e + "。下一步：返回关于页面后重试";
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadAboutJobLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadAboutJobLog, 2000);
</script>
"""

    return layout("操作进度", "about", body)


@bp.route("/api/about/job-log/<job_id>")
def api_about_job_log(job_id):
    info = ABOUT_JOBS.get(job_id)

    if not info:
        return jsonify({
            "running": False,
            "status": "记录不存在或面板已重启",
            "updated_at": "-",
            "log": "任务记录不存在或面板已重启。请到日志管理中查找 about-*.log。",
        })

    log_file = info.get("log_file", "")

    try:
        lines = int(request.args.get("lines", "1200") or 1200)
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "msg": "lines 必须为整数",
            "running": False,
            "status": "参数错误",
            "updated_at": "-",
            "log": "",
        }), 400

    return jsonify({
        "running": bool(info.get("running")),
        "action": info.get("action") or "",
        "status": info.get("status") or "-",
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "updated_at": info.get("updated_at", ""),
        "log_file": log_file,
        "log": tail_file(log_file, lines),
    })
