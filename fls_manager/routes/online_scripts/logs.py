from ._common import *
import re
import stat
from pathlib import Path
from urllib.parse import urlencode

from ...online_scripts.constants import (
    ONLINE_INSTALL_LIMIT,
    get_online_install_job,
    list_online_install_jobs,
    online_install_retry_relation,
    ONLINE_INSTALL_STATUSES,
    get_online_refresh_request,
    get_online_refresh_state,
)
from ...online_scripts.logs import online_refresh_log_file
from ...paths import LOG_DIR


RECOVERY_STATUSES = {"结果未知", "失败", "超时", "已停止", "启动失败"}
REFRESH_ID_RE = re.compile(r"^[a-f0-9]{32}$")
REFRESH_TERMINAL_STATUSES = {"已完成", "部分完成", "失败", "结果未知", "已确认发布", "已确认未发布", "超时", "已停止", "启动失败"}


def _install_log_file(info):
    """Resolve one persisted install log without following an uncontrolled path."""
    install_id = str(info.get("id") or "")
    raw_path = info.get("log_file")
    if not install_id or not isinstance(raw_path, str) or not raw_path.strip():
        return None, ("日志缺失", "安装记录没有日志身份，未读取未知路径。", 404)

    candidate = Path(raw_path)
    try:
        base_dir = LOG_DIR.parent.resolve()
        log_dir = LOG_DIR.resolve()
        if not candidate.is_absolute():
            return None, ("日志边界错误", "安装日志路径不是绝对路径，未读取该路径。", 500)
        actual = candidate.resolve(strict=False)
        if (
            candidate.is_symlink()
            or candidate.parent.resolve(strict=False) != candidate.parent
            or actual.parent not in (base_dir, log_dir)
            or install_id not in actual.name
        ):
            return None, ("日志边界错误", "安装日志路径不在受控日志目录内，未读取该文件。", 500)
        if not actual.exists():
            return None, ("日志不存在", "安装日志尚未生成或已被清理，请返回安装历史后重试。", 404)
        if not stat.S_ISREG(actual.stat().st_mode):
            return None, ("日志类型错误", "安装日志不是普通文件，未读取该路径。", 500)
        with actual.open("rb") as handle:
            handle.read(1)
    except FileNotFoundError:
        return None, ("日志不存在", "安装日志尚未生成或已被清理，请返回安装历史后重试。", 404)
    except OSError as exc:
        return None, ("日志不可读", f"安装日志无法读取：{exc}。请修复日志权限后重试。", 500)
    return actual, None


def _refresh_log_state(request_id):
    if not REFRESH_ID_RE.fullmatch(str(request_id or "")):
        return None, ("参数错误", "在线脚本源刷新请求标识无效，请从刷新状态重新打开日志。", 400)
    try:
        state = get_online_refresh_state()
    except Exception as exc:
        if "刷新历史" in str(exc):
            return None, ("历史不可读", f"刷新历史无法读取，未覆盖原记录：{exc}", 500)
        return None, ("状态不可读", f"在线脚本源刷新状态无法读取，未覆盖原记录：{exc}", 500)
    if state.get("request_id") != request_id:
        try:
            state = get_online_refresh_request(request_id)
        except Exception as exc:
            return None, ("历史不可读", f"刷新历史无法读取，未覆盖原记录：{exc}", 500)
        if not state:
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
            return (state, None), ("日志边界错误", "刷新日志路径不在受控日志目录内，未读取该文件。", 500)
        try:
            candidate_stat = candidate.stat()
        except FileNotFoundError:
            if state.get("running"):
                return (state, None), None
            return (state, None), ("日志不存在", "刷新日志尚未生成或已被清理，请返回刷新状态后重试。", 404)
        except OSError as exc:
            return (state, None), ("日志不可读", f"刷新日志无法读取：{exc}。请修复日志权限后重试。", 500)
        if not stat.S_ISREG(candidate_stat.st_mode):
            return (state, None), ("日志类型错误", "刷新日志不是普通文件，未读取该路径。", 500)
        with candidate.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        return (state, None), ("日志不可读", f"刷新日志无法读取：{exc}。请修复日志权限后重试。", 500)
    return (state, candidate), None


def _refresh_log_error_payload(request_id, title, message, status, state=None):
    valid_id = bool(REFRESH_ID_RE.fullmatch(str(request_id or "")))
    return {
        "ok": False, "request_id": request_id, "status": title, "error": message, "log": "",
        "retry_url": f"/api/online-scripts/refresh-log/{request_id}" if valid_id else "",
        "log_url": f"/online-scripts/refresh-log/{request_id}" if valid_id else "/online-scripts#online-status",
        "state_url": f"/api/online-scripts/refresh-status/{request_id}" if valid_id else "",
        "history_url": f"/online-scripts?refresh_request_id={request_id}#onlineRefreshHistory" if valid_id else "/online-scripts#online-status",
        "refresh_state": {
            key: state.get(key) for key in ("id", "request_id", "status", "running", "error", "cache_published", "updated_at")
        } if state else None,
    }, status


def _refresh_log_payload(request_id, lines):
    resolved, failure = _refresh_log_state(request_id)
    if failure:
        title, message, status = failure
        return _refresh_log_error_payload(request_id, title, message, status, resolved[0] if resolved else None)
    state, candidate = resolved
    if candidate is None:
        return {
            "ok": True,
            "request_id": request_id,
            "id": state.get("id", ""),
            "running": True,
            "terminal": False,
            "status": state.get("status") or "运行中",
            "updated_at": state.get("updated_at") or "",
            "error": state.get("error") or "",
            "cache_published": bool(state.get("cache_published")),
            "log_file": str(online_refresh_log_file(state["id"])),
            "log": "暂无日志",
            "log_pending": True,
            "log_status": 404,
        }, 200
    try:
        log = tail_file(str(candidate), lines)
    except Exception as exc:
        return _refresh_log_error_payload(
            request_id, "日志不可读", f"刷新日志读取失败：{exc}。请修复日志权限后重试。", 500, state,
        )
    if str(log).startswith("读取日志失败:"):
        return _refresh_log_error_payload(request_id, "日志不可读", str(log), 500, state)
    return {
        "ok": True,
        "request_id": request_id,
        "id": state.get("id", ""),
        "running": bool(state.get("running")),
        "terminal": not bool(state.get("running")) and state.get("status") in REFRESH_TERMINAL_STATUSES,
        "status": state.get("status") or "-",
        "updated_at": state.get("updated_at") or "",
        "error": state.get("error") or "",
        "cache_published": bool(state.get("cache_published")),
        "log_file": str(candidate),
        "log": str(log or "暂无日志"),
        "log_pending": False,
        "log_status": 200,
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
                '<button class="btn btn-primary" type="button" onclick="flsRefreshCurrentPage()">重新读取日志</button>'
                f'<noscript><a class="btn btn-primary" href="{h(payload["log_url"])}">重新读取日志</a></noscript>'
                f'<a class="btn btn-blue" href="{h(payload["history_url"])}">返回原刷新记录</a>'
                '<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>'
                + ('<button id="refreshLogQuery" class="btn btn-gray" type="button">查询原请求状态</button>' if payload.get("state_url") else "")
            ),
        )
        if payload.get("state_url"):
            receipt = payload.get("refresh_state") or {}
            known = "；".join(str(value) for value in (receipt.get("status"), receipt.get("error")) if value)
            body += f"""
<div class="notice" id="refreshLogQueryResult" role="status">{h(known)}</div>
<div class="notice red" id="refreshLogQueryError" role="status" hidden></div>
<a class="btn btn-primary" id="refreshLogQueryLogin" hidden>重新登录</a>
<script>
(function(){{
const button = document.getElementById("refreshLogQuery");
const result = document.getElementById("refreshLogQueryResult");
const error = document.getElementById("refreshLogQueryError");
const login = document.getElementById("refreshLogQueryLogin");
let pending = false;
async function loadRefreshReceipt(){{
    if(pending) return;
    pending = true;
    button.disabled = true;
    try{{
        const response = await fetch({json.dumps(payload["state_url"])}, {{cache:"no-store", credentials:"same-origin"}});
        const info = await flsReadJsonResponse(response);
        if(!info || info.request_id !== {json.dumps(request_id)} || typeof info.status !== "string" || typeof info.running !== "boolean") throw new Error("原刷新状态响应无效");
        result.textContent = "原刷新状态：" + info.status + (info.error ? "；" + info.error : "");
        error.hidden = true;
        login.hidden = true;
    }}catch(e){{
        error.hidden = false;
        error.textContent = "原请求状态查询失败：" + (e && e.message || String(e)) + "。已知结果保留，请重新查询或返回原刷新记录。";
        login.hidden = !(e && (e.status === 401 || e.status === 403));
        if(!login.hidden) login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    }}finally{{ pending = false; button.disabled = false; }}
}}
button.addEventListener("click", loadRefreshReceipt);
}})();
</script>
"""
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
        + '<span id="refreshLogConfirm"'
        + (' hidden' if payload.get("status") != "结果未知" else '')
        + f'><form class="inline-form" method="post" action="/online-scripts/refresh-confirm/{h(payload.get("request_id"))}">'
        + '<button class="btn btn-gray" type="submit">检查当前缓存结果</button></form></span>'
        + f'<a id="refreshLogHistory" class="btn btn-orange" href="/online-scripts?refresh_request_id={h(payload.get("request_id"))}#onlineRefreshHistory"'
        + (' hidden' if payload.get("status") not in ("结果未知", "失败", "启动失败", "部分完成") else '')
        + '>从历史重新同步</a>'
        + '<button class="btn btn-primary" type="button" id="refreshLogRetry">重新读取日志</button>'
    ),
)}
<div class="notice red" id="refreshLogError" role="status" hidden></div>
<div class="notice red" id="refreshLogDiagnosis" role="status"{' hidden' if not payload.get('error') else ''}>{h(payload.get('error') or '')}</div>
<div class="notice" id="refreshLogWaiting" role="status"{' hidden' if not payload.get('log_pending') else ''}>刷新仍在运行，日志尚未生成；系统会继续轮询。</div>
<a id="refreshLogLogin" class="btn btn-primary" hidden>重新登录</a>
<pre class="log" id="refreshLogText">{h(payload.get("log") or "暂无日志")}</pre>
<script>
(function(){{
const errorEl = document.getElementById("refreshLogError");
const loginEl = document.getElementById("refreshLogLogin");
const retryEl = document.getElementById("refreshLogRetry");
const textEl = document.getElementById("refreshLogText");
const diagnosisEl = document.getElementById("refreshLogDiagnosis");
const waitingEl = document.getElementById("refreshLogWaiting");
const confirmEl = document.getElementById("refreshLogConfirm");
const historyEl = document.getElementById("refreshLogHistory");
let pending = false;
let pollTimer = null;
let pollingStopped = false;
let pageHidden = false;
let transientFailures = 0;
let lastKnownRunning = {str(bool(payload.get("running"))).lower()};
const MAX_TRANSIENT_RETRIES = 5;

function stopPolling(){{
    pollingStopped = true;
    if(pollTimer){{ clearTimeout(pollTimer); pollTimer = null; }}
}}

function schedulePolling(delay){{
    if(pollingStopped || pageHidden || pollTimer) return;
    pollTimer = setTimeout(function(){{
        pollTimer = null;
        loadRefreshLog();
    }}, delay);
}}

function isRetryableReadFailure(error){{
    const status = Number(error && error.status);
    if(Number.isInteger(status) && status > 0)
        return status === 408 || status === 429 || (status >= 500 && status <= 599);
    return !!error && (error.name === "TypeError" || error.name === "NetworkError");
}}

async function loadRefreshLog(){{
    if(pending) return;
    pending = true;
    if(retryEl) retryEl.disabled = true;
    try{{
        const response = await fetch("/api/online-scripts/refresh-log/{h(payload.get("request_id"))}?lines=1200", {{cache:"no-store", credentials:"same-origin"}});
        const result = await flsReadJsonResponse(response);
        if(!result || typeof result.log !== "string") throw new Error("刷新日志响应无效");
        transientFailures = 0;
        lastKnownRunning = result.running === true || result.terminal !== true;
        if(errorEl){{ errorEl.hidden = true; errorEl.textContent = ""; }}
        if(loginEl) loginEl.hidden = true;
        if(textEl) textEl.textContent = result.log || "暂无日志";
        if(diagnosisEl){{ diagnosisEl.textContent = result.error || ""; diagnosisEl.hidden = !result.error; }}
        if(waitingEl) waitingEl.hidden = result.log_pending !== true;
        if(confirmEl) confirmEl.hidden = result.status !== "结果未知";
        if(historyEl) historyEl.hidden = ["结果未知", "失败", "启动失败", "部分完成"].indexOf(result.status) < 0;
        const status = document.getElementById("refreshLogStatus");
        const updated = document.getElementById("refreshLogUpdatedAt");
        if(status) status.textContent = result.status || "-";
        if(updated) updated.textContent = result.updated_at || "-";
        if(result.running || !result.terminal){{
            if(!pageHidden){{
                pollingStopped = false;
                schedulePolling(2000);
            }}
        }}else{{
            stopPolling();
        }}
    }}catch(error){{
        const denied = error && (error.status === 401 || error.status === 403);
        const state = error && error.payload && error.payload.refresh_state;
        if(state && typeof state.running === "boolean") lastKnownRunning = state.running;
        const retryable = !denied && lastKnownRunning && isRetryableReadFailure(error);
        const failureMessage = "刷新日志读取失败：" + (error && error.message || String(error || "请求失败"));
        if(retryable && !pageHidden && transientFailures < MAX_TRANSIENT_RETRIES){{
            transientFailures += 1;
            const delay = Math.min(2000 * Math.pow(2, transientFailures - 1), 30000);
            pollingStopped = false;
            if(errorEl){{
                errorEl.hidden = false;
                errorEl.textContent = failureMessage + "。系统将在 " + Math.ceil(delay / 1000)
                    + " 秒后自动重试（" + transientFailures + "/" + MAX_TRANSIENT_RETRIES + "），也可手动重新读取。";
            }}
            if(loginEl) loginEl.hidden = true;
            schedulePolling(delay);
        }}else{{
            stopPolling();
            if(waitingEl) waitingEl.hidden = true;
            if(errorEl){{
                errorEl.hidden = false;
                errorEl.textContent = failureMessage + (denied
                    ? "。请重新登录后继续读取。"
                    : (retryable && transientFailures >= MAX_TRANSIENT_RETRIES
                        ? "。自动重试次数已达上限，请手动重新读取或返回刷新状态。"
                        : "。请重新读取或返回刷新状态。"));
            }}
        }}
        if(loginEl){{ loginEl.hidden = !denied; if(denied) loginEl.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); }}
    }}finally{{ pending = false; if(retryEl) retryEl.disabled = false; }}
}}
if(retryEl) retryEl.addEventListener("click", function(){{
    if(pollTimer){{ clearTimeout(pollTimer); pollTimer = null; }}
    transientFailures = 0;
    pollingStopped = false;
    if(!pending) return loadRefreshLog();
}});
window.addEventListener("pagehide", function(){{ pageHidden = true; stopPolling(); }}, {{once:true}});
if({str(bool(payload.get("running"))).lower()}){{
    pollingStopped = false;
    loadRefreshLog();
}}
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
    relation = online_install_retry_relation(info)
    if relation.get("retry_relation_error"):
        return {"available": False, "reason": relation["retry_relation_error"]}
    if (info or {}).get("phase") == "importing_tasks" and (info or {}).get("import_task"):
        consistency = verify_install_outputs(info)
        if consistency.get("changed"):
            return {
                "available": False,
                "reason": "安装后的脚本或任务已被后来修改，未自动恢复任务导入，请明确重新选择。",
                "script_id": info.get("script_id"),
            }
        if status in RECOVERY_STATUSES:
            return {
                "available": True,
                "action": url_for("online_scripts.online_scripts_install_task_retry", install_id=info.get("id")),
                "api_action": url_for("online_scripts.api_online_scripts_install_task_retry", install_id=info.get("id")),
                "label": "仅恢复任务导入",
            }
        return {"available": False}

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


def _online_install_history_url(script_id="", status="", limit=50, offset=0):
    query = {}
    if script_id:
        query["script_id"] = script_id
    if status:
        query["status"] = status
    if limit != 50:
        query["limit"] = limit
    if offset:
        query["offset"] = offset
    path = url_for("online_scripts.online_install_history")
    return f"{path}?{urlencode(query)}" if query else path


def _install_history_filter_form(script_id="", status="", limit=50):
    try:
        limit = int(limit)
        if not 1 <= limit <= ONLINE_INSTALL_LIMIT:
            limit = 50
    except (TypeError, ValueError):
        limit = 50
    status_options = ['<option value="">全部状态</option>']
    for value in ONLINE_INSTALL_STATUSES:
        selected = " selected" if value == status else ""
        status_options.append(f'<option value="{h(value)}"{selected}>{h(value)}</option>')
    limit_values = sorted({1, 25, 50, 100, limit})
    limit_options = "".join(
        f'<option value="{value}"{" selected" if limit == value else ""}>{value} 条</option>'
        for value in limit_values
    )
    return f"""
<form method="get" action="/online-scripts/install-history">
<section class="section fls-form-section">
    <div class="form-grid">
        <div class="form-item"><label for="install-history-script">脚本 ID</label><input id="install-history-script" name="script_id" value="{h(script_id)}" maxlength="200"></div>
        <div class="form-item"><label for="install-history-status">安装状态</label><select id="install-history-status" name="status">{"".join(status_options)}</select></div>
        <div class="form-item"><label for="install-history-limit">每页条数</label><select id="install-history-limit" name="limit">{limit_options}</select></div>
    </div>
    <br><button class="btn btn-primary" type="submit">筛选历史</button>
    <a class="btn btn-gray" href="/online-scripts/install-history">重置</a>
</section>
</form>
"""


@bp.route("/api/online-scripts/install-history")
def api_online_install_history():
    requested_snapshot = request.args.get("snapshot")
    if requested_snapshot is not None and not re.fullmatch(r"[a-f0-9]{64}", requested_snapshot):
        return jsonify(ok=False, msg="在线脚本安装历史快照无效"), 400
    try:
        items, total, snapshot = list_online_install_jobs(
            request.args.get("limit", 50),
            request.args.get("script_id", ""),
            request.args.get("status", ""),
            request.args.get("offset", 0),
            include_snapshot=True,
        )
    except ValueError as exc:
        return jsonify(ok=False, msg=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, msg=f"安装历史无法读取：{exc}"), 500
    if requested_snapshot is not None and requested_snapshot != snapshot:
        return jsonify(ok=False, msg="安装历史在分页期间发生变化，请重新读取完整历史"), 409
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    return jsonify(
        ok=True, items=items, total=total, offset=offset, limit=limit,
        has_more=offset + len(items) < total,
        snapshot=snapshot,
    )


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

    recovery = _install_recovery(info)
    if request.args.get("state_only") == "1":
        return jsonify(
            ok=True, install_id=install_id, running=bool(info.get("running")),
            status=info.get("status") or "-", phase=_install_phase(info),
            returncode=info.get("returncode"), error=_install_error(info), recovery=recovery,
        )
    log_file, log_failure = _install_log_file(info)
    if log_failure:
        title, message, failure_status = log_failure
        payload = {
            "ok": False,
            "running": bool(info.get("running")),
            "status": info.get("status") or title,
            "phase": _install_phase(info),
            "returncode": info.get("returncode"),
            "error": "；".join(filter(None, (info.get("error"), message))),
            "log_file": "",
            "log": "",
            "recovery_url": "/online-scripts/install-history",
            "log_status": failure_status,
        }
        if recovery.get("available") or recovery.get("reason"):
            payload["recovery"] = recovery
        # A not-yet-created log is a normal early install state. Keep the
        # historical 200 response for existing polling clients while exposing
        # the real missing-log status in the JSON payload.
        response_status = 200 if failure_status == 404 else failure_status
        return jsonify(payload), response_status

    payload = {
        "ok": True,
        "install_id": install_id,
        "running": bool(info.get("running")),
        "status": info.get("status") or "-",
        "phase": _install_phase(info),
        "returncode": info.get("returncode"),
        "error": _install_error(info),
        "log_file": str(log_file),
        "log": tail_file(str(log_file), lines),
    }
    relation = online_install_retry_relation(info)
    payload.update(relation)
    if str(payload["log"]).startswith("读取日志失败:"):
        payload.update(
            ok=False,
            error=payload["log"],
            log="",
            recovery_url="/online-scripts/install-history",
        )
        return jsonify(payload), 500
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
    history_path = "/online-scripts/install-history"
    history_url = back_url if back_url == history_path or back_url.startswith(history_path + "?") else history_path
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

    log_path, log_failure = _install_log_file(info)
    log_notice = ""
    if log_failure:
        title, message, failure_status = log_failure
        log_notice = f'<div class="notice red" role="alert">{h(message)}</div>'
        info = dict(info)
        info["log_file"] = ""
    else:
        info = dict(info)
        info["log_file"] = str(log_path)
    recovery = _install_recovery(info)
    if recovery.get("action") and back_url != "/online-scripts":
        recovery["action"] += "?back=" + quote(back_url, safe="")
    recovery_html = _install_recovery_html(recovery)
    retry_relation = online_install_retry_relation(info)
    retry_navigation_html = ""
    if retry_relation.get("retry_relation_error"):
        retry_navigation_html = f'<span class="help" style="color:#dc2626;">{h(retry_relation["retry_relation_error"])}</span>'
    else:
        if retry_relation.get("retry_parent_id"):
            parent_url = url_for(
                "online_scripts.online_install_log",
                install_id=retry_relation["retry_parent_id"],
                back=history_url,
            )
            retry_navigation_html += f'<a class="btn btn-gray" href="{h(parent_url)}">查看原安装</a>'
        if retry_relation.get("retry_child_id"):
            child_url = url_for(
                "online_scripts.online_install_log",
                install_id=retry_relation["retry_child_id"],
                back=history_url,
            )
            retry_navigation_html += f'<a class="btn btn-blue" href="{h(child_url)}">查看后续重试</a>'
    consistency = verify_install_outputs(info)
    consistency_note = ""
    if consistency.get("changed"):
        consistency_note = "<span style=\"color:#dc2626;\">安装后的脚本或任务已被后来修改，恢复不会自动覆盖。</span><br>"

    stop_install_button = ""

    if info.get("running"):
        stop_url = url_for("online_scripts.online_scripts_install_stop", install_id=install_id, back=back_url)
        stop_api_url = url_for("online_scripts.api_online_scripts_install_stop", install_id=install_id)
        stop_install_button = f"""
<div class="row-actions-danger" id="installStopAction">
    <form method="post" action="{h(stop_url)}" data-api-action="{h(stop_api_url)}" style="display:inline;">
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
{retry_navigation_html}
{stop_install_button}
""",
    )

    body = f"""
<style>
#installStopAction[hidden], #installRecoveryAction[hidden] {{ display: none !important; }}
</style>
{header_card}
{log_notice}
<div class="notice red" id="installPollError" role="status" hidden></div>
<a id="installPollLogin" class="btn btn-primary" hidden>重新登录</a>
<a id="installPollLogs" class="btn btn-orange" href="/logs" hidden>查看日志管理</a>
<button id="installPollRetry" class="btn btn-primary" type="button" hidden>重新读取安装状态</button>
<button id="installStopQuery" class="btn btn-gray" type="button" hidden>查询停止结果</button>
<pre class="log" id="log">{"暂无日志" if log_failure else "加载中..."}</pre>
{log_controls()}

<script>
(function(){{
const pollError = document.getElementById("installPollError");
const loginAction = document.getElementById("installPollLogin");
const logsAction = document.getElementById("installPollLogs");
const retryAction = document.getElementById("installPollRetry");
const stopQueryAction = document.getElementById("installStopQuery");
const stopActionInitial = document.querySelector("#installStopAction form");
const stopActionButton = stopActionInitial ? stopActionInitial.querySelector("button") : null;
const installId = {json.dumps(install_id)};
const logUrl = "/api/online-scripts/log/" + encodeURIComponent(installId);
let pending = false;
let postingStop = false;
let needsStopQuery = false;
let logRequestGeneration = 0;
let transientFailures = 0;
let retryTimer = null;
let pageHidden = false;
let resumeOnPageshow = false;
let autoPollingPaused = false;
let lastKnownRunning = {str(bool(info.get("running"))).lower()};
const retryDelays = [2000, 4000, 8000, 16000, 30000];
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function clearLogTimers(){{
    clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
    window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
    if(retryTimer !== null){{
        clearTimeout(retryTimer);
        retryTimer = null;
    }}
}}

function isTransientLogError(error){{
    if(!error || error.status === undefined || error.status === null) return true;
    const status = Number(error.status);
    return status === 0 || status === 408 || status === 429 || (status >= 500 && status <= 599);
}}

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

async function loadLog(stateOnly = false){{
    if(pageHidden || pending || postingStop || (!stateOnly && needsStopQuery) || document.getElementById("installPollError") !== pollError) return;
    const requestGeneration = ++logRequestGeneration;
    pending = true;
    if(retryAction) retryAction.disabled = true;
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch(logUrl + (stateOnly ? "?state_only=1" : "?lines=1600"), {{cache:"no-store"}});
        const json = await flsReadJsonResponse(res);
        if(requestGeneration !== logRequestGeneration || postingStop || (!stateOnly && needsStopQuery) || document.getElementById("installPollError") !== pollError) return;
        if(!json || typeof json.running !== "boolean" || (stateOnly ? json.install_id !== installId : typeof json.log !== "string")){{
            const invalid = new Error("安装状态响应无效，请重新读取安装状态");
            invalid.status = res.status;
            throw invalid;
        }}
        if(json.ok === false){{
            const missingError = new Error(json.error || "安装日志暂不可读");
            missingError.status = json.log_status || 500;
            missingError.payload = json;
            if(json.running && missingError.status === 404) transientFailures = 0;
            throw missingError;
        }}
        transientFailures = 0;
        autoPollingPaused = false;
        lastKnownRunning = json.running;
        if(stateOnly){{
            needsStopQuery = false;
            if(pollError){{
                pollError.textContent = "原安装状态已确认：" + (json.status || "-");
                pollError.hidden = false;
            }}
        }}
        if(pollError && !stateOnly){{
            pollError.hidden = true;
            pollError.textContent = "";
        }}
        if(loginAction) loginAction.hidden = true;
        if(logsAction) logsAction.hidden = true;
        if(retryAction) retryAction.hidden = true;
        if(stopQueryAction) stopQueryAction.hidden = true;

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

        if(!stateOnly){{
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

        }}
        const stopAction = document.getElementById("installStopAction");
        const recoveryEl = document.getElementById("installRecoveryAction");
        if(stopAction) stopAction.hidden = !json.running;
        if(recoveryEl) recoveryEl.hidden = json.running;
        if(json.running){{
            if(retryTimer !== null){{ clearTimeout(retryTimer); retryTimer = null; }}
            if(stopActionButton) stopActionButton.disabled = json.status === "停止中";
            if(stopQueryAction) stopQueryAction.hidden = json.status !== "停止中";
            if(!window.__FLS_ACTIVE_LOG_INTERVAL__){{
                window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
            }}
        }}else{{
            const recovery = json.recovery || {{}};
            if(recoveryEl && recovery.available){{
                const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
                const csrfInput = csrf ?
                    "<" + "input type=\\"hidden\\" name=\\"csrf_token\\" value=\\"" + escapeHtml(csrf) + "\\">" : "";
                let recoveryAction = recovery.action || "";
                const historyBack = typeof location !== "undefined" && typeof URLSearchParams !== "undefined"
                    ? new URLSearchParams(location.search).get("back") || "" : "";
                if(historyBack === "/online-scripts/install-history" || historyBack.startsWith("/online-scripts/install-history?")){{
                    recoveryAction += (recoveryAction.includes("?") ? "&" : "?") + "back=" + encodeURIComponent(historyBack);
                }}
                recoveryEl.innerHTML =
                    "<" + "form class='inline-form' method='post' action='" + escapeHtml(recoveryAction) + "' data-api-action='" + escapeHtml(recovery.api_action || "") + "'>" +
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
                            if(!result || result.ok !== true){{
                                const error = new Error(result && result.msg || "调度器恢复失败");
                                error.payload = result;
                                throw error;
                            }}
                            await loadLog();
                        }}catch(error){{
                            const payload = error && error.payload || {{}};
                            if(payload.scheduler_reload_required){{
                                await loadLog();
                                const currentForm = document.querySelector("#installRecoveryAction form[data-api-action]");
                                const currentButton = currentForm && currentForm.querySelector("button");
                                if(currentButton) currentButton.disabled = false;
                                return;
                            }}
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
            clearLogTimers();
            lastKnownRunning = false;
        }}
    }} catch(e) {{
        if(requestGeneration !== logRequestGeneration || postingStop || (!stateOnly && needsStopQuery) || document.getElementById("installPollError") !== pollError) return;
        const message = e && e.message ? e.message : String(e);
        const denied = e && (e.status === 401 || e.status === 403);
        const missing = e && e.status === 404;
        const payload = e && e.payload || {{}};
        if((payload.install_id === installId || typeof payload.log_status === "number")
            && typeof payload.running === "boolean")
            lastKnownRunning = payload.running;
        const startingLog = missing && payload.running === true && payload.log_status === 404;
        if(startingLog) lastKnownRunning = true;
        const next = denied ? "重新登录后继续读取" :
            (startingLog ? "安装仍在运行，日志尚未生成；系统将继续轮询，可随时停止安装" :
                (missing ? "到日志管理检查已保留的安装日志" : "检查安装日志文件和服务状态，稍后会重新读取"));
        let scheduledRetry = false;
        if(!denied && !startingLog && lastKnownRunning && isTransientLogError(e)
            && transientFailures < retryDelays.length && !pageHidden){{
            const delay = retryDelays[transientFailures++];
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
            if(retryTimer !== null) clearTimeout(retryTimer);
            if(pollError){{
                pollError.textContent = "日志读取失败：" + message + "。已保留现有日志，将在 "
                    + Math.ceil(delay / 1000) + " 秒后自动重试（" + transientFailures + "/" + retryDelays.length + "）。";
            }}
            retryTimer = setTimeout(() => {{ retryTimer = null; loadLog(needsStopQuery); }}, delay);
            scheduledRetry = true;
        }}
        if(pollError){{
            if(!scheduledRetry){{
                const exhausted = lastKnownRunning && isTransientLogError(e) && transientFailures >= retryDelays.length;
                pollError.textContent = "日志读取失败：" + message + (exhausted
                    ? "。自动重试已用尽（" + retryDelays.length + " 次）；可手动重新读取。"
                    : "。下一步：" + next);
            }}
            pollError.hidden = false;
        }}
        if(loginAction){{
            loginAction.hidden = !denied;
            if(denied){{
                loginAction.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
            }}
        }}
        if(logsAction){{
            logsAction.hidden = !missing || startingLog;
            if(missing && !startingLog) logsAction.href = payload.recovery_url || "/logs";
        }}
        if(retryAction) retryAction.hidden = missing && !startingLog;
        if(stopQueryAction) stopQueryAction.hidden = !needsStopQuery;
        if(startingLog){{
            autoPollingPaused = false;
            const stopAction = document.getElementById("installStopAction");
            const recoveryEl = document.getElementById("installRecoveryAction");
            if(stopAction) stopAction.hidden = false;
            if(recoveryEl) recoveryEl.hidden = true;
            const statusEl = document.getElementById("installStatus");
            const phaseEl = document.getElementById("installPhase");
            if(statusEl) statusEl.textContent = payload.status || "运行中";
            if(phaseEl) phaseEl.textContent = payload.phase || "running";
            if(!window.__FLS_ACTIVE_LOG_INTERVAL__){{
                window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
            }}
        }}else if(denied || missing){{
            autoPollingPaused = true;
            const stopAction = document.getElementById("installStopAction");
            const recoveryEl = document.getElementById("installRecoveryAction");
            if(stopAction) stopAction.hidden = true;
            if(recoveryEl) recoveryEl.hidden = true;
            clearLogTimers();
        }}else if(scheduledRetry){{
            autoPollingPaused = false;
        }}else{{
            autoPollingPaused = true;
            clearLogTimers();
        }}
    }}finally{{
        if(requestGeneration === logRequestGeneration){{
            pending = false;
            if(retryAction) retryAction.disabled = false;
        }}
    }}
}}

async function requestStop(event){{
    event.preventDefault();
    if(postingStop || needsStopQuery || stopActionButton.disabled) return;
    postingStop = true;
    logRequestGeneration++;
    pending = false;
    if(typeof flsClearPageCache === "function") flsClearPageCache();
    clearLogTimers();
    if(stopActionButton) stopActionButton.disabled = true;
    if(pollError){{ pollError.textContent = "停止请求已发送，正在确认状态..."; pollError.hidden = false; }}
    try{{
        const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
        const headers = {{"X-Requested-With":"XMLHttpRequest"}};
        if(csrf) headers["X-CSRF-Token"] = csrf;
        const response = await fetch(stopActionInitial.dataset.apiAction, {{method:"POST", headers, credentials:"same-origin"}});
        const result = await flsReadJsonResponse(response);
        if(!result || result.ok !== true || result.install_id !== installId) throw new Error("停止响应格式无效");
        if(document.getElementById("installPollError") !== pollError) return;
        if(pollError) pollError.textContent = result.msg || "停止请求已提交";
    }}catch(error){{
        if(document.getElementById("installPollError") !== pollError) return;
        const message = error && error.message || String(error || "请求失败");
        const denied = error && (error.status === 401 || error.status === 403);
        if(pollError) pollError.textContent = "停止请求未确认：" + message + "。请查询原安装状态。";
        if(denied && loginAction){{
            loginAction.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
            loginAction.hidden = false;
        }}
        return;
    }}finally{{
        postingStop = false;
        needsStopQuery = true;
        if(stopQueryAction) stopQueryAction.hidden = false;
    }}
    await queryStop();
}}

async function queryStop(){{
    if(stopQueryAction) stopQueryAction.disabled = true;
    try{{ await loadLog(true); }}
    finally{{ if(stopQueryAction) stopQueryAction.disabled = false; }}
}}

if(stopActionInitial) stopActionInitial.addEventListener("submit", requestStop);
if(stopQueryAction) stopQueryAction.addEventListener("click", queryStop);
if(retryAction) retryAction.onclick = () => {{
    if(retryTimer !== null){{ clearTimeout(retryTimer); retryTimer = null; }}
    transientFailures = 0;
    autoPollingPaused = false;
    return loadLog(needsStopQuery);
}};
window.addEventListener("pagehide", function(){{
    resumeOnPageshow = lastKnownRunning && !autoPollingPaused;
    pageHidden = true;
    logRequestGeneration++;
    pending = false;
    clearLogTimers();
}});
window.addEventListener("pageshow", function(event){{
    if(!event || !event.persisted) return;
    pageHidden = false;
    if(resumeOnPageshow && lastKnownRunning){{
        resumeOnPageshow = false;
        loadLog(needsStopQuery);
    }}
}});
if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
if(retryTimer !== null) clearTimeout(retryTimer);
retryTimer = null;
loadLog();
}})();
</script>
"""

    return layout("在线脚本日志", "online_scripts", body)


@bp.route("/online-scripts/install-history")
def online_install_history():
    script_id = str(request.args.get("script_id") or "").strip()
    status = str(request.args.get("status") or "").strip()
    raw_limit = request.args.get("limit", "50")
    raw_offset = request.args.get("offset", "0")
    try:
        items, total = list_online_install_jobs(raw_limit, script_id, status, raw_offset)
        limit = int(raw_limit)
        offset = int(raw_offset)
        history_url = _online_install_history_url(script_id, status, limit, offset)
    except ValueError as exc:
        body = page_header_card(
            "在线脚本安装历史",
            help_html=h(exc),
            actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        ) + _install_history_filter_form(script_id, status if status in ONLINE_INSTALL_STATUSES else "", raw_limit)
        return layout("在线脚本安装历史", "online_scripts", body), 400
    except Exception as exc:
        body = page_header_card(
            "在线脚本安装历史",
            help_html=f"安装历史无法读取：{h(exc)}。请修复安装回执存储后重试。",
            actions_html=f'<a class="btn btn-blue" href="{h(request.full_path.rstrip("?"))}">重新读取</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        ) + _install_history_filter_form(script_id, status if status in ONLINE_INSTALL_STATUSES else "", raw_limit)
        return layout("在线脚本安装历史", "online_scripts", body), 500
    labels = {"结果未知": "结果未知", "失败": "失败，可重试", "已完成": "已完成", "停止中": "停止中"}
    rows = []
    for info in items:
        install_id = str(info.get("id") or "")
        relation_error = str(info.get("retry_relation_error") or "")
        relation_html = f'<span class="help" style="color:#dc2626;">{h(relation_error)}</span>' if relation_error else ""
        if not relation_error and info.get("retry_parent_id"):
            parent_url = url_for(
                "online_scripts.online_install_log",
                install_id=info["retry_parent_id"],
                back=history_url,
            )
            relation_html += f'<a href="{h(parent_url)}">原安装</a> '
        if not relation_error and info.get("retry_child_id"):
            child_url = url_for(
                "online_scripts.online_install_log",
                install_id=info["retry_child_id"],
                back=history_url,
            )
            relation_html += f'<a href="{h(child_url)}">后续重试</a>'
        if not relation_html:
            relation_html = "-"
        rows.append(
            "<tr>"
            f"<td><code>{h(install_id)}</code></td>"
            f"<td>{h(info.get('script_id') or '-')}</td>"
            f"<td>{h(labels.get(info.get('status'), info.get('status') or '-'))}</td>"
            f"<td>{h(info.get('phase') or '-')}</td>"
            f"<td>{h(info.get('updated_at') or '-')}</td>"
            f"<td>{relation_html}</td>"
            f'<td><a class="btn btn-blue" href="{h(url_for("online_scripts.online_install_log", install_id=install_id, back=history_url))}">查看安装日志</a></td>'
            "</tr>"
        )
    empty_message = "当前页没有记录，请返回上一页或重新筛选。" if total else "暂无安装历史"
    table = (
        '<div class="table-wrap"><table><thead><tr><th>安装 ID</th><th>脚本</th><th>状态</th>'
        '<th>阶段</th><th>更新时间</th><th>重试关系</th><th>操作</th></tr></thead><tbody>'
        + ("".join(rows) or f'<tr><td colspan="7">{empty_message}</td></tr>')
        + "</tbody></table></div>"
    )
    displayed = f"{offset + 1}–{offset + len(items)}" if items else "0"
    body = page_header_card(
        "在线脚本安装历史",
        help_html=f"当前显示 {displayed} / {total} 条。",
        actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
    ) + _install_history_filter_form(script_id, status, limit) + f'<section class="section"><div class="card">{table}</div></section>'
    if offset > 0 or offset + len(items) < total:
        last_offset = ((total - 1) // limit) * limit if total else 0
        previous = _online_install_history_url(script_id, status, limit, max(0, min(offset - limit, last_offset)))
        next_page = _online_install_history_url(script_id, status, limit, offset + len(items))
        body += '<div class="section row-actions" aria-label="安装历史分页">'
        body += f'<a class="btn btn-gray" href="{h(previous)}">上一页</a>' if offset > 0 else ""
        body += f'<a class="btn btn-primary" href="{h(next_page)}">下一页</a>' if offset + len(items) < total else ""
        body += '</div>'
    return layout("在线脚本安装历史", "online_scripts", body)
