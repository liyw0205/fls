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
            "后台任务日志",
            help_html="""
        任务记录不存在或面板已重启。<br>
        可以到日志管理中查找 about-*.log。
""",
            actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/logs?back={h(back_url)}">查看日志管理</a>
""",
        )
        return layout("后台任务日志", "about", body)

    header_card = page_header_card(
        f'后台任务日志：{info.get("title") or job_id}',
        help_html=f"""
        状态：<b id="aboutJobStatus">{h(info.get("status") or "-")}</b><br>
        日志文件：{h(info.get("log_file") or "-")}<br>
        更新时间：<span id="aboutJobUpdatedAt">{h(info.get("updated_at") or "-")}</span>
""",
        actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回关于页</a>
<a class="btn btn-blue" href="/logs?back={h(back_url)}">日志管理</a>
<a class="btn btn-orange" href="/logfile/fls-manager-daemon.log?back={h(back_url)}">面板日志</a>
""",
    )

    body = f"""
{header_card}
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

async function loadAboutJobLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/about/job-log/{h(job_id)}?lines=1600", {{cache:"no-store"}});
        const json = await res.json();

        document.getElementById("aboutJobStatus").textContent = json.status || "-";
        document.getElementById("aboutJobUpdatedAt").textContent = json.updated_at || "-";

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
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadAboutJobLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadAboutJobLog, 2000);
</script>
"""

    return layout("后台任务日志", "about", body)


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
    lines = int(request.args.get("lines", "1200") or 1200)

    return jsonify({
        "running": bool(info.get("running")),
        "status": info.get("status") or "-",
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "updated_at": info.get("updated_at", ""),
        "log_file": log_file,
        "log": tail_file(log_file, lines),
    })
