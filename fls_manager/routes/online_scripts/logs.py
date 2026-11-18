from ._common import *
import re
from pathlib import Path

from ...online_scripts.constants import get_online_install_job, get_online_refresh_state
from ...online_scripts.logs import online_refresh_log_file
from ...paths import LOG_DIR


RECOVERY_STATUSES = {"结果未知", "失败", "超时", "已停止", "启动失败"}
REFRESH_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def _refresh_log_state(request_id):
    if not REFRESH_ID_RE.fullmatch(str(request_id or "")):
        return None, ("参数错误", "在线脚本源刷新请求标识无效，请从刷新状态重新打开日志。", 400)
    try:
        state = get_online_refresh_state()
    except Exception as exc:
        return None, ("状态不可读", f"在线脚本源刷新状态无法读取，未覆盖原记录：{exc}", 500)
    if state.get("request_id") != request_id:
        return None, ("记录不存在", "刷新请求不存在或已被明确的新请求替换，请返回刷新状态后重新选择。", 404)
    refresh_id = str(state.get("id") or "")
    if not REFRESH_ID_RE.fullmatch(refresh_id):
        return None, ("记录损坏", "刷新记录缺少有效作业标识，未读取未知路径。", 500)
    expected = online_refresh_log_file(refresh_id)
    candidate = Path(state.get("log_file") or expected)
    try:
        base = LOG_DIR.resolve()
        actual = candidate.resolve(strict=False)
        if candidate.is_symlink() or actual != expected.resolve(strict=False) or base not in actual.parents:
            return None, ("日志边界错误", "刷新日志路径不在受控日志目录内，未读取该文件。", 500)
        if not candidate.exists():
            return None, ("日志不存在", "刷新日志尚未生成或已被清理，请返回刷新状态后重试。", 404)
        if not candidate.is_file():
            return None, ("日志类型错误", "刷新日志不是普通文件，未读取该路径。", 500)
        with candidate.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return None, ("日志不可读", f"刷新日志无法读取：{exc}。请修复日志权限后重试。", 500)
    return (state, candidate), None


def _refresh_log_payload(request_id, lines):
    resolved, failure = _refresh_log_state(request_id)
    if failure:
        title, message, status = failure
        return {
            "ok": False,
            "request_id": request_id,
            "status": title,
            "error": message,
            "log": "",
            "retry_url": f"/api/online-scripts/refresh-log/{request_id}",
            "state_url": f"/online-scripts#online-status",
        }, status
    state, candidate = resolved
    try:
        log = tail_file(str(candidate), lines)
    except Exception as exc:
        return {
            "ok": False,
            "request_id": request_id,
            "status": "日志不可读",
            "error": f"刷新日志读取失败：{exc}。请修复日志权限后重试。",
            "log": "",
            "retry_url": f"/api/online-scripts/refresh-log/{request_id}",
            "state_url": "/online-scripts#online-status",
        }, 500
    if str(log).startswith("读取日志失败:"):
        return {
            "ok": False,
            "request_id": request_id,
            "status": "日志不可读",
            "error": str(log),
            "log": "",
            "retry_url": f"/api/online-scripts/refresh-log/{request_id}",
            "state_url": "/online-scripts#online-status",
        }, 500
    return {
        "ok": True,
        "request_id": request_id,
        "id": state.get("id", ""),
        "running": bool(state.get("running")),
        "status": state.get("status") or "-",
        "updated_at": state.get("updated_at") or "",
        "error": state.get("error") or "",
        "cache_published": bool(state.get("cache_published")),
        "log_file": str(candidate),
        "log": str(log or "暂无日志"),
    }, 200


@bp.route("/api/online-scripts/refresh-log/<request_id>")
def api_online_refresh_log(request_id):
    try:
        lines = int(request.args.get("lines", "1200"))
    except (TypeError, ValueError):
        return jsonify(ok=False, request_id=request_id, status="参数错误", error="lines 必须为整数", log=""), 400
    payload, status = _refresh_log_payload(request_id, lines)
    return jsonify(payload), status


@bp.route("/online-scripts/refresh-log/<request_id>")
def online_refresh_log(request_id):
    payload, status = _refresh_log_payload(request_id, 1200)
    if not payload.get("ok"):
        body = page_header_card(
            f"在线脚本源刷新日志：{payload.get('status') or '读取失败'}",
            help_html=h(payload.get("error") or "刷新日志无法读取"),
            actions_html=(
                f'<a class="btn btn-primary" href="{h(payload.get("retry_url") or request.path)}">重新读取日志</a>'
                f'<a class="btn btn-blue" href="{h(payload.get("state_url") or "/online-scripts#online-status")}">返回刷新状态</a>'
            ),
        )
        return layout("在线脚本源刷新日志", "online_scripts", body), status
    body = f"""
{page_header_card(
    f'在线脚本源刷新日志：{h(payload.get("id") or payload.get("request_id"))}',
    help_html=(
        f'状态：<b id="refreshLogStatus">{h(payload.get("status") or "-")}</b><br>'
        f'更新时间：<span id="refreshLogUpdatedAt">{h(payload.get("updated_at") or "-")}</span><br>'
        f'日志文件：{h(payload.get("log_file") or "-")}'
    ),
    actions_html=(
        '<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>'
        f'<button class="btn btn-primary" type="button" id="refreshLogRetry">重新读取日志</button>'
    ),
)}
<div class="notice red" id="refreshLogError" role="status" hidden></div>
<a id="refreshLogLogin" class="btn btn-primary" hidden>重新登录</a>
<pre class="log" id="refreshLogText">{h(payload.get("log") or "暂无日志")}</pre>
<script>
(function(){{
const errorEl = document.getElementById("refreshLogError");
const loginEl = document.getElementById("refreshLogLogin");
const retryEl = document.getElementById("refreshLogRetry");
const textEl = document.getElementById("refreshLogText");
async function loadRefreshLog(){{
    if(retryEl) retryEl.disabled = true;
    try{{
        const response = await fetch("/api/online-scripts/refresh-log/{h(payload.get("request_id"))}?lines=1200", {{cache:"no-store", credentials:"same-origin"}});
        const result = await flsReadJsonResponse(response);
        if(!result || typeof result.log !== "string") throw new Error("刷新日志响应无效");
        if(errorEl){{ errorEl.hidden = true; errorEl.textContent = ""; }}
        if(loginEl) loginEl.hidden = true;
        if(textEl) textEl.textContent = result.log || "暂无日志";
        const status = document.getElementById("refreshLogStatus");
        const updated = document.getElementById("refreshLogUpdatedAt");
        if(status) status.textContent = result.status || "-";
        if(updated) updated.textContent = result.updated_at || "-";
    }}catch(error){{
        const denied = error && (error.status === 401 || error.status === 403);
        if(errorEl){{ errorEl.hidden = false; errorEl.textContent = "刷新日志读取失败：" + (error && error.message || String(error || "请求失败")) + "。请重新读取或返回刷新状态。"; }}
        if(loginEl){{ loginEl.hidden = !denied; if(denied) loginEl.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); }}
    }}finally{{ if(retryEl) retryEl.disabled = false; }}
}}
if(retryEl) retryEl.addEventListener("click", loadRefreshLog);
}})();
</script>
"""
    return layout("在线脚本源刷新日志", "online_scripts", body), 200


def _install_phase(info):
    phase = str((info or {}).get("phase") or "").strip()
    if phase:
        return phase
    if (info or {}).get("scheduler_reload_pending"):
        return "scheduler_reload_pending"
    if (info or {}).get("running"):
        return "running"
    return {
        "准备中": "prepared",
        "运行中": "running",
        "部分完成": "scheduler_reload_pending",
        "已完成": "completed",
        "失败": "failed",
        "超时": "timeout",
        "已停止": "stopped",
        "启动失败": "startup_failed",
        "结果未知": "unknown",
    }.get(str((info or {}).get("status") or ""), "unknown")


def _install_error(info):
    errors = [info.get("error") or ""]
    pending_error = info.get("scheduler_reload_error") or ""
    if pending_error and pending_error not in errors[0]:
        errors.append(pending_error)
    return "；".join(filter(None, errors))


def _install_recovery(info):
    status = str((info or {}).get("status") or "")
    if not (info or {}).get("running") and (info or {}).get("scheduler_reload_pending"):
        return {
            "available": True,
            "action": url_for("online_scripts.online_scripts_install_reload", install_id=info.get("id")),
            "api_action": url_for("online_scripts.api_online_scripts_install_reload", install_id=info.get("id")),
            "label": "重新加载调度器",
        }
    if status not in RECOVERY_STATUSES:
        if (info or {}).get("phase") == "importing_tasks" and (info or {}).get("import_task"):
            consistency = verify_install_outputs(info)
            if consistency.get("changed"):
                return {
                    "available": False,
                    "reason": "安装后的脚本或任务已被后来修改，未自动恢复任务导入，请明确重新选择。",
                    "script_id": info.get("script_id"),
                }
            return {
                "available": True,
                "action": url_for("online_scripts.online_scripts_install_task_retry", install_id=info.get("id")),
                "api_action": url_for("online_scripts.api_online_scripts_install_task_retry", install_id=info.get("id")),
                "label": "仅恢复任务导入",
            }
        return {"available": False}

    if (info or {}).get("phase") == "importing_tasks" and (info or {}).get("import_task"):
        consistency = verify_install_outputs(info)
        if consistency.get("changed"):
            return {
                "available": False,
                "reason": "安装后的脚本或任务已被后来修改，未自动恢复任务导入，请明确重新选择。",
                "script_id": info.get("script_id"),
            }

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

    try:
        item = get_online_script(script_id)
    except Exception as exc:
        return {
            "available": False,
            "script_id": script_id,
            "reason": f"在线脚本源缓存无法读取，未覆盖原文件：{exc}",
            "href": "/online-scripts/source",
            "label": "检查在线脚本源数据",
        }
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
            f'<button class="btn btn-primary" type="submit">{h(recovery.get("label") or "重试安装")}</button>'
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

    try:
        info = get_online_install_job(install_id)
    except Exception as exc:
        return jsonify({
            "ok": False,
            "running": False,
            "status": "记录不可读",
            "returncode": None,
            "error": f"在线脚本安装记录无法读取，未覆盖原文件：{exc}",
            "log_file": "",
            "log": "安装记录无法读取。请修复记录文件后重新读取。",
            "recovery_url": "/logs",
        }), 500

    if not info:
        return jsonify({
            "ok": False,
            "running": False,
            "status": "记录不存在",
            "returncode": None,
            "error": "安装记录不存在。请确认安装 ID 后重试。",
            "log_file": "",
            "log": "安装记录不存在。请到日志管理中查找 online-script-install-*.log。",
            "recovery_url": "/logs",
        }), 404

    log_file = info.get("log_file", "")
    recovery = _install_recovery(info)

    payload = {
        "running": bool(info.get("running")),
        "status": info.get("status") or "-",
        "phase": _install_phase(info),
        "returncode": info.get("returncode"),
        "error": _install_error(info),
        "log_file": log_file,
        "log": tail_file(log_file, lines),
    }
    consistency = verify_install_outputs(info)
    if consistency.get("checked"):
        payload["consistency"] = consistency
    if info.get("scheduler_reload_pending"):
        payload["scheduler_reload_pending"] = True
        payload["scheduler_reload_error"] = info.get("scheduler_reload_error") or ""
    if recovery.get("available") or recovery.get("reason"):
        payload["recovery"] = recovery
    return jsonify(payload)


@bp.route("/online-scripts/log/<install_id>")
def online_install_log(install_id):
    back_url = get_back_url("/online-scripts")
    try:
        info = get_online_install_job(install_id)
    except Exception as exc:
        body = page_header_card(
            "在线脚本安装记录无法读取",
            help_html=f"{h(exc)}。原记录未覆盖，请修复记录文件后重新读取。",
            actions_html=(
                f'<a class="btn btn-primary" href="/online-scripts/log/{h(install_id)}">重新读取安装记录</a>'
                '<a class="btn btn-blue" href="/logs">查看日志管理</a>'
            ),
        )
        return layout("在线脚本日志", "online_scripts", body), 500

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
    consistency = verify_install_outputs(info)
    consistency_note = ""
    if consistency.get("changed"):
        consistency_note = "<span style=\"color:#dc2626;\">安装后的脚本或任务已被后来修改，恢复不会自动覆盖。</span><br>"

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
        阶段：<span id="installPhase">{h(_install_phase(info))}</span><br>
        {consistency_note}
        退出码：<span id="installReturncode">{h(info.get("returncode") if info.get("returncode") is not None else "-")}</span><br>
        <span id="installError">{"错误：" + h(_install_error(info)) if _install_error(info) else ""}</span><br>
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
<a id="installPollLogin" class="btn btn-primary" hidden>重新登录</a>
<a id="installPollLogs" class="btn btn-orange" href="/logs" hidden>查看日志管理</a>
<button id="installPollRetry" class="btn btn-primary" type="button" hidden>重新读取安装状态</button>
<pre class="log" id="log">加载中...</pre>
{log_controls()}

<script>
(function(){{
const pollError = document.getElementById("installPollError");
const loginAction = document.getElementById("installPollLogin");
const logsAction = document.getElementById("installPollLogs");
const retryAction = document.getElementById("installPollRetry");
let pending = false;
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
    if(pending || document.getElementById("installPollError") !== pollError) return;
    pending = true;
    if(retryAction) retryAction.disabled = true;
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/online-scripts/log/{h(install_id)}?lines=1600", {{cache:"no-store"}});
        const json = await flsReadJsonResponse(res);
        if(document.getElementById("installPollError") !== pollError) return;
        if(!json || typeof json.running !== "boolean" || typeof json.log !== "string"){{
            throw new Error("安装状态响应无效，请重新读取安装状态");
        }}
        if(pollError){{
            pollError.hidden = true;
            pollError.textContent = "";
        }}
        if(loginAction) loginAction.hidden = true;
        if(logsAction) logsAction.hidden = true;
        if(retryAction) retryAction.hidden = true;

        document.getElementById("installStatus").textContent = json.status || "-";
        const phaseEl = document.getElementById("installPhase");
        if(phaseEl) phaseEl.textContent = json.phase || "unknown";
        const errorEl = document.getElementById("installError");
        let errorText = json.error || "";
        if(json.consistency && json.consistency.changed){{
            errorText = (errorText ? errorText + "；" : "") + "安装后的脚本或任务已被后来修改，恢复不会自动覆盖";
        }}
        if(errorEl) errorEl.textContent = errorText ? "错误：" + errorText : "";
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

        const stopAction = document.getElementById("installStopAction");
        const recoveryEl = document.getElementById("installRecoveryAction");
        if(stopAction) stopAction.hidden = !json.running;
        if(recoveryEl) recoveryEl.hidden = json.running;
        if(json.running){{
            if(!window.__FLS_ACTIVE_LOG_INTERVAL__){{
                window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
            }}
        }}else{{
            const recovery = json.recovery || {{}};
            if(recoveryEl && recovery.available){{
                const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
                const csrfInput = csrf ?
                    "<" + "input type=\\"hidden\\" name=\\"csrf_token\\" value=\\"" + escapeHtml(csrf) + "\\">" : "";
                recoveryEl.innerHTML =
                    "<" + "form class='inline-form' method='post' action='" + escapeHtml(recovery.action || "") + "' data-api-action='" + escapeHtml(recovery.api_action || "") + "'>" +
                    csrfInput +
                    "<button class='btn btn-primary' type='submit'>" +
                    escapeHtml(recovery.label || "重试安装") + "</button><" + "/form>";
                recoveryEl.dataset.ready = "1";
                const recoveryForm = typeof recoveryEl.querySelector === "function"
                    ? recoveryEl.querySelector("form[data-api-action]") : null;
                if(recoveryForm && recovery.api_action){{
                    recoveryForm.addEventListener("submit", async function(event){{
                        event.preventDefault();
                        const button = recoveryForm.querySelector("button");
                        if(button) button.disabled = true;
                        try{{
                            const headers = {{"X-Requested-With": "XMLHttpRequest"}};
                            if(csrf) headers["X-CSRF-Token"] = csrf;
                            const response = await fetch(recovery.api_action, {{
                                method: "POST", headers, credentials: "same-origin",
                            }});
                            const result = await flsReadJsonResponse(response);
                            if(!result || result.ok !== true) throw new Error(result && result.msg || "调度器恢复失败");
                            await loadLog();
                        }}catch(error){{
                            const message = error && error.message ? error.message : String(error || "调度器恢复失败");
                            if(pollError){{
                                pollError.textContent = "调度器恢复失败：" + message + "。请稍后重试。";
                                pollError.hidden = false;
                            }}
                            if(button) button.disabled = false;
                        }}
                    }});
                }}
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
        if(document.getElementById("installPollError") !== pollError) return;
        const message = e && e.message ? e.message : String(e);
        const denied = e && (e.status === 401 || e.status === 403);
        const missing = e && e.status === 404;
        const payload = e && e.payload || {{}};
        const next = denied ? "重新登录后继续读取" :
            (missing ? "到日志管理检查已保留的安装日志" : "检查安装日志文件和服务状态，稍后会重新读取");
        if(pollError){{
            pollError.textContent = "日志读取失败：" + message + "。下一步：" + next;
            pollError.hidden = false;
        }}
        if(loginAction){{
            loginAction.hidden = !denied;
            if(denied){{
                loginAction.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
            }}
        }}
        if(logsAction){{
            logsAction.hidden = !missing;
            if(missing) logsAction.href = payload.recovery_url || "/logs";
        }}
        if(retryAction) retryAction.hidden = missing;
        if(denied || missing){{
            const stopAction = document.getElementById("installStopAction");
            const recoveryEl = document.getElementById("installRecoveryAction");
            if(stopAction) stopAction.hidden = true;
            if(recoveryEl) recoveryEl.hidden = true;
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}else{{
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}
    }}finally{{
        pending = false;
        if(retryAction) retryAction.disabled = false;
    }}
}}

if(retryAction) retryAction.onclick = loadLog;
if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
loadLog();
}})();
</script>
"""

    return layout("在线脚本日志", "online_scripts", body)
