from ._common import *
from ...online_scripts.constants import get_online_install_job


RECOVERY_STATUSES = {"结果未知", "失败", "超时", "已停止", "启动失败"}


def _install_recovery(info):
    status = str((info or {}).get("status") or "")
    if status not in RECOVERY_STATUSES:
        return {"available": False}

    retry_id = str((info or {}).get("retried_by") or "")
    if retry_id:
        return {
            "available": False,
            "reason": "该记录已发起重试，请查看新的安装记录。",
            "href": url_for("online_scripts.online_install_log", install_id=retry_id),
            "label": "查看重试记录",
        }

    script_id = str((info or {}).get("script_id") or "").strip()
    if not script_id:
        return {"available": False, "reason": "安装记录缺少脚本 ID"}

    item = get_online_script(script_id)
    if not item:
        return {
            "available": False,
            "script_id": script_id,
            "reason": "脚本已不在本地缓存中，请先同步在线脚本源，再从脚本列表重试。",
        }

    try:
        online_script_target(item)
        if info.get("import_task"):
            validated_install_retry_task_indexes(item, info)
    except (RuntimeError, ValueError, OSError):
        return {
            "available": False,
            "script_id": script_id,
            "reason": "脚本路径或任务选择已失效，请检查脚本源并重新选择安装选项。",
            "href": (
                url_for("online_scripts.online_scripts_install_select", script_id=script_id)
                if script_has_task(item) else "/online-scripts"
            ),
            "label": "重新选择安装选项",
        }

    return {
        "available": True,
        "script_id": script_id,
        "action": url_for("online_scripts.online_scripts_install_retry", install_id=info.get("id")),
        "label": "重试安装",
    }


def _install_recovery_html(recovery):
    if recovery.get("available"):
        return (
            '<form class="inline-form" method="post" '
            f'action="{h(recovery.get("action"))}">'
            '<button class="btn btn-primary" type="submit">重试安装</button>'
            '</form>'
        )

    reason = recovery.get("reason") or ""
    html = f'<span class="help" style="color:#dc2626;">{h(reason)}</span>' if reason else ""
    if recovery.get("href"):
        html += f'<a class="btn btn-orange" href="{h(recovery["href"])}">{h(recovery["label"])}</a>'
    return html


@bp.route("/api/online-scripts/log/<install_id>")
def api_online_install_log(install_id):
    try:
        lines = int(request.args.get("lines", "1200"))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "msg": "lines 必须为整数",
            "running": False,
            "status": "参数错误",
            "returncode": None,
            "error": "",
            "log_file": "",
            "log": "",
        }), 400

    info = get_online_install_job(install_id)

    if not info:
        return jsonify({
            "running": False,
            "status": "记录不存在",
            "returncode": None,
            "error": "安装记录不存在。请确认安装 ID 后重试。",
            "log_file": "",
            "log": "安装记录不存在。请到日志管理中查找 online-script-install-*.log。",
        }), 404

    log_file = info.get("log_file", "")
    recovery = _install_recovery(info)

    payload = {
        "running": bool(info.get("running")),
        "status": info.get("status") or "-",
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "log_file": log_file,
        "log": tail_file(log_file, lines),
    }
    if recovery.get("available") or recovery.get("reason"):
        payload["recovery"] = recovery
    return jsonify(payload)


@bp.route("/online-scripts/log/<install_id>")
def online_install_log(install_id):
    back_url = get_back_url("/online-scripts")
    info = get_online_install_job(install_id)

    if not info:
        body = page_header_card(
            "在线脚本日志",
            help_html="""
        安装记录不存在。<br>
        可以到日志管理中查找 online-script-install-*.log。
""",
            actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/logs?back={h(back_url)}">查看日志管理</a>
""",
        )
        return layout("在线脚本日志", "online_scripts", body), 404

    recovery = _install_recovery(info)
    recovery_html = _install_recovery_html(recovery)

    stop_install_button = ""

    if info.get("running"):
        stop_install_button = f"""
<div class="row-actions-danger" id="installStopAction">
    <form method="post" action="/online-scripts/install-stop/{h(install_id)}" style="display:inline;">
        <button class="btn btn-red" type="submit" onclick="return confirm('确定停止该安装任务吗？')">停止安装任务</button>
    </form>
</div>
"""

    header_card = page_header_card(
        f'在线脚本下载安装日志：{info.get("script_name") or install_id}',
        help_html=f"""
        状态：<b id="installStatus">{h(info.get("status") or "-")}</b><br>
        退出码：<span id="installReturncode">{h(info.get("returncode") if info.get("returncode") is not None else "-")}</span><br>
        <span id="installError">{"错误：" + h(info.get("error")) if info.get("error") else ""}</span><br>
        日志文件：{h(info.get("log_file") or "-")}
""",
        actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/pull">脚本</a>
<a class="btn btn-orange" href="/tasks">任务</a>
<div id="installRecoveryAction">{recovery_html}</div>
{stop_install_button}
""",
    )

    body = f"""
{header_card}
<div class="notice red" id="installPollError" role="status" hidden></div>
<pre class="log" id="log">加载中...</pre>
{log_controls()}

<script>
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function escapeHtml(value){{
    return String(value).replace(/[&<>"']/g, function(char){{
        return {{"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}}[char];
    }});
}}

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
        const json = await flsReadJsonResponse(res);
        const pollError = document.getElementById("installPollError");
        if(pollError) pollError.hidden = true;

        document.getElementById("installStatus").textContent = json.status || "-";
        const errorEl = document.getElementById("installError");
        if(errorEl) errorEl.textContent = json.error ? "错误：" + json.error : "";
        document.getElementById("installReturncode").textContent = json.returncode == null ? "-" : String(json.returncode);

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
            const stopAction = document.getElementById("installStopAction");
            if(stopAction) stopAction.hidden = true;
            const recovery = json.recovery || {{}};
            const recoveryEl = document.getElementById("installRecoveryAction");
            if(recoveryEl && recovery.available && !recoveryEl.dataset.ready){{
                const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
                const csrfInput = csrf ?
                    "<" + "input type=\\"hidden\\" name=\\"csrf_token\\" value=\\"" + escapeHtml(csrf) + "\\">" : "";
                recoveryEl.innerHTML =
                    "<" + "form class='inline-form' method='post' action='" + escapeHtml(recovery.action || "") + "'>" +
                    csrfInput +
                    "<button class='btn btn-primary' type='submit'>重试安装</button><" + "/form>";
                recoveryEl.dataset.ready = "1";
            }}else if(recoveryEl && !recovery.available){{
                let html = recovery.reason ?
                    "<span class='help' style='color:#dc2626;'>" + escapeHtml(recovery.reason) + "</span>" : "";
                if(recovery.href) html +=
                    "<a class='btn btn-orange' href='" + escapeHtml(recovery.href) + "'>" +
                    escapeHtml(recovery.label || "查看安装选项") + "</a>";
                recoveryEl.innerHTML = html;
                delete recoveryEl.dataset.ready;
            }}
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}
    }} catch(e) {{
        const pollError = document.getElementById("installPollError");
        const message = e && e.message ? e.message : String(e);
        const denied = e && (e.status === 401 || e.status === 403);
        const missing = e && e.status === 404;
        const next = denied ? "重新登录后刷新页面" :
            (missing ? "到日志管理检查已保留的安装日志" : "检查安装日志文件和服务状态，稍后会重新读取");
        if(pollError){{
            pollError.textContent = "日志读取失败：" + message + "。下一步：" + next;
            pollError.hidden = false;
        }}
        if(denied || missing){{
            const stopAction = document.getElementById("installStopAction");
            const recoveryEl = document.getElementById("installRecoveryAction");
            if(stopAction) stopAction.hidden = true;
            if(recoveryEl) recoveryEl.hidden = true;
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""

    return layout("在线脚本日志", "online_scripts", body)
