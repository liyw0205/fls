from ._common import *


@bp.route("/api/online-scripts/log/<install_id>")
def api_online_install_log(install_id):
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        return jsonify({
            "running": False,
            "status": "记录不存在或面板已重启",
            "log": "安装记录不存在或面板已重启。请到日志管理中查找 online-script-install-*.log。",
        })

    log_file = info.get("log_file", "")
    lines = int(request.args.get("lines", "1200") or 1200)

    return jsonify({
        "running": bool(info.get("running")),
        "status": info.get("status") or "-",
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "log_file": log_file,
        "log": tail_file(log_file, lines),
    })


@bp.route("/online-scripts/log/<install_id>")
def online_install_log(install_id):
    back_url = get_back_url("/online-scripts")
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        body = f"""
<div class="card">
    <div class="card-title">在线脚本日志</div>
    <div class="help">
        安装记录不存在或面板已重启。<br>
        可以到日志管理中查找 online-script-install-*.log。
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/logs?back={h(back_url)}">查看日志管理</a>
</div>
"""
        return layout("在线脚本日志", "online_scripts", body)

    stop_install_button = ""

    if info.get("running"):
        stop_install_button = f"""
<form method="post" action="/online-scripts/install-stop/{h(install_id)}" style="display:inline;">
    <button class="btn btn-red" type="submit" onclick="return confirm('确定停止该安装任务吗？')">停止安装</button>
</form>
"""

    body = f"""
<div class="card">
    <div class="card-title">在线脚本下载安装日志：{h(info.get("script_name") or install_id)}</div>
    <div class="help">
        状态：<b id="installStatus">{h(info.get("status") or "-")}</b><br>
        日志文件：{h(info.get("log_file") or "-")}
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/pull">脚本管理</a>
    <a class="btn btn-orange" href="/tasks">任务管理</a>
    {stop_install_button}
</div>

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

        const res = await fetch("/api/online-scripts/log/{h(install_id)}?lines=1600", {{cache:"no-store"}});
        const json = await res.json();

        document.getElementById("installStatus").textContent = json.status || "-";

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
loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""

    return layout("在线脚本日志", "online_scripts", body)

