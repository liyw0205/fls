import re

from ._common import *
from ...online_scripts.constants import (
    get_online_refresh_history,
    get_online_refresh_request,
    get_online_refresh_task_link_failures,
)


@bp.route("/online-scripts")
def online_scripts_page():
    selected_refresh_request = request.args.get("refresh_request_id", "").strip()
    if selected_refresh_request and not re.fullmatch(r"[a-f0-9]{32}", selected_refresh_request):
        return layout("刷新历史", "online_scripts", page_header_card(
            "刷新请求标识无效", help_html="请返回刷新状态重新选择。",
            actions_html='<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>',
        )), 400
    refresh_history_url = "/api/online-scripts/refresh-history?limit=8"
    if selected_refresh_request:
        refresh_history_url += "&request_id=" + selected_refresh_request
    try:
        refresh_history = get_online_refresh_history(limit=8, request_id=selected_refresh_request)
        refresh_history_error = ""
    except LookupError as exc:
        return layout("刷新历史", "online_scripts", page_header_card(
            "原刷新记录不可用", help_html=h(exc),
            actions_html='<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>',
        )), 404
    except Exception as exc:
        refresh_history = []
        refresh_history_error = f"在线脚本源刷新历史无法读取，未覆盖当前状态：{exc}"
    source = get_online_script_source()
    cache_error = ""
    try:
        all_items = load_online_script_cache()
    except Exception as exc:
        all_items = []
        cache_error = f"在线脚本源缓存无法读取，未覆盖原文件：{exc}。请检查源数据后重试。"

    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = 12

    filtered_items = filter_online_scripts_for_page(all_items, q)

    total = len(filtered_items)
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)

    start = (page - 1) * per_page
    end = page * per_page

    items = filtered_items[start:end]
    page_links_html = online_scripts_page_links(q, page, pages)

    msg = request.args.get("msg", "").strip()
    err = request.args.get("err", "").strip()

    try:
        refresh_state = get_online_refresh_state()
    except Exception as exc:
        refresh_state = {
            "running": False,
            "status": "状态不可读",
            "message": "",
            "error": f"在线脚本源同步状态无法读取，未覆盖原文件：{exc}",
            "updated_at": "",
            "log_file": "",
        }
    refresh_running = refresh_state.get("running")
    refresh_message = refresh_state.get("message", "")
    refresh_error = refresh_state.get("error", "")
    refresh_updated_at = refresh_state.get("updated_at", "")
    refresh_log = refresh_state.get("log_file", "")
    refresh_request_id = str(refresh_state.get("request_id") or "").strip()
    refresh_log_href = (
        f"/online-scripts/refresh-log/{quote(refresh_request_id, safe='')}"
        if re.fullmatch(r"[a-f0-9]{32}", refresh_request_id) else ""
    )
    failure_request_id = selected_refresh_request or refresh_request_id
    failure_snapshot = refresh_state
    failure_detail_error = ""
    failure_recovery_maintenance = {}
    if selected_refresh_request:
        try:
            failure_snapshot = get_online_refresh_request(selected_refresh_request) or {}
        except Exception as exc:
            failure_snapshot = {}
            failure_detail_error = f"原刷新失败源状态无法读取：{exc}"
    try:
        failure_offset = int(request.args.get("failure_offset", "0") or 0)
    except (TypeError, ValueError):
        return layout("刷新失败源", "online_scripts", page_header_card(
            "失败源分页参数无效", help_html="请重新选择失败源分页。",
            actions_html='<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>',
        )), 400
    if not 0 <= failure_offset <= 5000:
        return layout("刷新失败源", "online_scripts", page_header_card(
            "失败源分页参数超出范围", help_html="请重新选择失败源分页。",
            actions_html='<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>',
        )), 400
    try:
        failure_count = int(failure_snapshot.get("task_link_failure_count") or len(failure_snapshot.get("task_link_failures") or []))
    except (TypeError, ValueError):
        failure_count = 0
    if failure_count and failure_offset >= failure_count:
        failure_offset = ((failure_count - 1) // 100) * 100
    if re.fullmatch(r"[a-f0-9]{32}", failure_request_id or ""):
        try:
            failure_page = get_online_refresh_task_link_failures(
                failure_request_id, offset=failure_offset, limit=100,
            )
            refresh_task_link_failures = failure_page["items"]
            failure_count = failure_page["total"]
            failure_recovery_maintenance = failure_page.get("maintenance") or {}
        except Exception as exc:
            refresh_task_link_failures = (failure_snapshot.get("task_link_failures") or []) if failure_offset == 0 else []
            failure_detail_error = failure_detail_error or f"原刷新失败源明细无法读取，未清除已知首屏：{exc}"
            refresh_task_link_failures = [
                {**item, "recovery_error": failure_detail_error}
                for item in refresh_task_link_failures
            ]
    else:
        refresh_task_link_failures = failure_snapshot.get("task_link_failures") or []
        failure_count = len(refresh_task_link_failures)
        failure_offset = 0
    failure_recovery_maintenance_html = ""
    if failure_recovery_maintenance.get("repaired"):
        reread_url = (
            f"/online-scripts?refresh_request_id={quote(failure_request_id, safe='')}"
            f"&failure_offset={failure_offset}#online-status"
        )
        failure_recovery_maintenance_html = (
            '<div class="notice green" role="status">恢复回执副本已自动修复。'
            f'<a class="btn btn-gray" href="{h(reread_url)}">重新读取恢复状态</a></div>'
        )
    refresh_persistence_html = ""
    if refresh_state.get("persistence_error") and re.fullmatch(r"[a-f0-9]{32}", refresh_request_id):
        persistence_diagnosis = "<br>".join(
            h(value) for value in (refresh_state.get("error"), refresh_state.get("persistence_error")) if value
        )
        refresh_persistence_html = (
            f'<div class="notice red">{persistence_diagnosis}</div>'
            f'<form class="inline-form fls-online-refresh-recover-form" method="post" '
            f'action="/online-scripts/refresh-recover/{h(refresh_request_id)}" '
            f'data-api-action="/api/online-scripts/refresh-recover/{h(refresh_request_id)}" '
            f'data-refresh-recover-id="{h(refresh_request_id)}">'
            '<button class="btn btn-primary" type="submit">修复刷新回执</button></form>'
        )
    def refresh_history_html(items):
        rows = []
        for item in items:
            request_id = str(item.get("request_id") or "").strip()
            if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                continue
            status = h(item.get("status") or "-")
            updated_at = h(item.get("updated_at") or "-")
            log_link = (
                f'<a class="btn btn-gray" href="/online-scripts/refresh-log/{quote(request_id, safe="")}">'
                f'查看 {status} 刷新（{updated_at}）</a>'
            )
            try:
                failure_count = int(item.get("task_link_failure_count") or 0)
            except (TypeError, ValueError):
                failure_count = 0
            failure_link = (
                f'<a class="btn btn-orange" href="/online-scripts?refresh_request_id={quote(request_id, safe="")}&failure_offset=0#online-status">'
                f'查看 {failure_count} 个失败源</a>'
                if failure_count else ""
            )
            if item.get("status") in ("失败", "部分完成", "结果未知", "启动失败"):
                rows.append(
                    f'<div class="fls-online-refresh-history-row" data-refresh-retry-of="{h(request_id)}">'
                    f'{log_link}{failure_link}<form class="inline-form fls-online-refresh-retry-form" method="post" '
                    f'action="/online-scripts/refresh-retry/{h(request_id)}" data-refresh-retry-of="{h(request_id)}">'
                    f'<input type="hidden" name="retry_request_id" value="{uuid.uuid4().hex}">'
                    f'<input type="hidden" name="proxy_id" value="{h(item.get("proxy_id") or "")}">'
                    '<button class="btn btn-orange" type="submit">明确重新同步</button></form></div>'
                )
            else:
                rows.append(log_link + failure_link)
            if item.get("status") == "结果未知" and not item.get("confirmation"):
                rows.append(
                    f'<form class="inline-form fls-online-refresh-confirm-form" method="post" '
                    f'action="/online-scripts/refresh-confirm/{h(request_id)}" data-refresh-confirm-id="{h(request_id)}">'
                    '<button class="btn btn-gray" type="submit">检查当前缓存结果</button></form>'
                )
        return "".join(rows) or '<span class="help">暂无可查询的刷新历史</span>'

    def refresh_task_link_actions(failures, failure_request_id, failure_count, failure_offset):
        if not failures and not failure_count:
            return ""
        rows = []
        for failure in failures:
            script_id = str(failure.get("script_id") or "").strip()
            if not script_id:
                continue
            recovery = failure.get("recovery") or {}
            if recovery.get("state") == "completed":
                recovered_count = int(recovery.get("added") or 0)
                recovered_total = int(recovery.get("total") or 0)
                rows.append(
                    f'<div class="help" style="margin-top:6px;">'
                    f'{h(failure.get("name") or script_id)}：已恢复，新增 {recovered_count} 个任务，'
                    f'当前共 {recovered_total} 个任务</div>'
                )
                continue
            if failure.get("recovery_error"):
                rows.append(
                    f'<div class="help" style="margin-top:6px;">'
                    f'{h(failure.get("name") or script_id)}：恢复状态不可读取，请先修复回执后重新查询</div>'
                )
                continue
            bound = bool(re.fullmatch(r"[a-f0-9]{32}", failure_request_id or ""))
            task_link_request_id = str(failure.get("task_link_request_id") or "")
            if not re.fullmatch(r"[a-f0-9]{32}", task_link_request_id):
                continue
            if bound:
                action = url_for(
                    "online_scripts.online_scripts_pull_refresh_task_link",
                    refresh_request_id=failure_request_id,
                    script_id=script_id,
                )
                api_action = url_for(
                    "online_scripts.api_online_scripts_pull_refresh_task_link",
                    refresh_request_id=failure_request_id,
                    script_id=script_id,
                )
                origin_input = f'<input type="hidden" name="origin_refresh_request_id" value="{h(failure_request_id)}">'
            else:
                action = url_for("online_scripts.online_scripts_pull_task_link", script_id=script_id)
                api_action = url_for("online_scripts.api_online_scripts_pull_task_link", script_id=script_id)
                origin_input = ""
            label = h(failure.get("name") or script_id)
            detail = h(failure.get("error") or "外部任务源加载失败")
            proxy_id = h(failure.get("proxy_id") or "")
            rows.append(
                f'<div class="help" style="margin-top:6px;">{label}：{detail} '
                f'<form class="inline-form fls-online-task-link-form" data-script-id="{h(script_id)}" '
                f'data-api-action="{h(api_action)}" method="post" action="{h(action)}">'
                f'<input type="hidden" name="request_id" value="{h(task_link_request_id)}">'
                f'{origin_input}'
                f'<input type="hidden" name="proxy_id" value="{proxy_id}">'
                '<button class="btn btn-orange" type="submit">重新同步外部任务源</button>'
                '</form></div>'
            )
        end = min(failure_count, failure_offset + len(failures))
        summary = f'<div class="help">失败源 {failure_offset + 1 if failures else 0}-{end} / {failure_count}</div>'
        page_links = []
        base = f"/online-scripts?refresh_request_id={quote(failure_request_id, safe='')}"
        if failure_offset > 0:
            previous = max(0, failure_offset - 100)
            page_links.append(f'<a class="btn btn-gray" href="{base}&failure_offset={previous}#online-status">上一页</a>')
        if failure_offset + len(failures) < failure_count:
            page_links.append(f'<a class="btn btn-gray" href="{base}&failure_offset={failure_offset + len(failures)}#online-status">下一页</a>')
        controls = f'<div class="action-row">{"".join(page_links)}</div>' if page_links else ""
        return summary + "".join(rows) + controls

    proxy_options = proxy_select_options("")

    refresh_display = (
        "display:block;"
        if refresh_running or refresh_message or refresh_error or refresh_task_link_failures
        else "display:none;"
    )

    task_total = sum(len(online_script_task_crons(x)) for x in filtered_items)
    install_total = sum(1 for x in filtered_items if script_has_install(x))

    refresh_status_text = (
        "正在后台同步在线脚本源，请稍候..."
        if refresh_running
        else (
            f"{h(refresh_state.get('status'))}：{h(refresh_message or refresh_error)}"
            if refresh_state.get("status") and (refresh_message or refresh_error)
            else h(refresh_state.get("status") or refresh_message or refresh_error or "")
        )
    )
    header = page_header_card(
        "在线脚本",
        help_html=f"""
默认读取本地缓存，不会因为脚本源网络问题卡住。<br>
点击“同步在线脚本源”后会后台更新缓存，页面不会变白，也不影响其它操作。<br>
脚本源支持 <code>doc_link</code> 字段，可在面板内查看 Markdown 文档或网页文档。<br>
<code>task_cron.var</code> 可预设任务变量，导入任务时会自动写入任务变量。
<div class="fls-source-code">{h(source)}</div>
        """,
        actions_html=f"""
<form method="post" action="/online-scripts/refresh" class="action-row fls-online-refresh-form">
    <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
    <select name="proxy_id" aria-label="刷新脚本源使用的代理" style="width:auto;min-width:180px;">{proxy_options}</select>
    <button class="btn btn-primary" type="submit" id="onlineRefreshBtn">同步在线脚本源</button>
</form>

<a class="btn btn-blue" href="{h(source)}" target="_blank">打开源地址</a>
<a class="btn btn-orange" href="/online-scripts/source">在线脚本源数据</a>
<a class="btn btn-gray" href="/online-scripts/task-link-history">外部任务源历史</a>
<a class="btn btn-gray" href="/online-scripts/import-tasks-history">任务导入历史</a>
<a class="btn btn-gray" href="/online-scripts/install-history">安装历史</a>
<a class="btn btn-gray" href="/config">修改源地址</a>
        """,
        content_style="min-width:0;flex:1 1 360px;",
    )

    body = f"""
{header}

<nav class="fls-section-nav" aria-label="在线脚本区块导航">
    <span class="fls-section-nav-label">在线脚本</span>
    <a href="#online-search">搜索筛选</a>
    <a href="#online-status">刷新状态</a>
    <a href="#online-list">脚本列表</a>
</nav>

<section class="section fls-section" id="online-search">
<form method="get">
<div class="data-toolbar" id="online-search-toolbar">
    <div class="form-grid">
        <div class="form-item">
            <label>搜索在线脚本</label>
            <input name="q" value="{h(q)}" placeholder="脚本名 / ID / 保存名 / 链接 / 任务名 / 命令" aria-label="搜索在线脚本">
        </div>

        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="submit">查询在线脚本</button>
            <a class="btn btn-gray" href="/online-scripts">重置</a>
        </div>
    </div>

    <div class="help" style="margin-top:10px;">
        共 {len(all_items)} 个脚本，当前匹配 {total} 个，每页 {per_page} 个。
    </div>
</div>
</form>
</section>

<div class="fls-summary-grid">
    {summary_item("缓存脚本数", len(items))}
    {summary_item("可导入任务", task_total)}
    {summary_item("有安装命令", install_total)}
</div>

{message_card(msg, "success", strong=True)}
{message_card(err, "error", strong=True)}
{message_card(cache_error, "error", strong=True)}
<div class="notice red" id="onlineTaskLinkRecovery" role="status" hidden></div>
{f'<div class="section"><a class="btn btn-orange" href="/online-scripts/source">检查在线脚本源数据</a></div>' if cache_error else ''}

<section class="section fls-section" id="online-status">
<div class="card" id="onlineRefreshStatusCard" style="{refresh_display}">
    <h2 class="section-title">在线脚本源同步状态</h2>
    <div class="help" id="onlineRefreshStatusText">
        {refresh_status_text}<br>
        更新时间：{h(refresh_updated_at or "-")}<br>
        日志：{h(refresh_log or "-")} {f'<a class="btn btn-blue" href="{h(refresh_log_href)}">查看刷新日志</a>' if refresh_log_href else ''}
    </div>
    <div id="onlineRefreshTaskLinkFailures">{failure_recovery_maintenance_html}{h(failure_detail_error)}{refresh_task_link_actions(refresh_task_link_failures, failure_request_id, failure_count, failure_offset)}</div>
    <a id="onlineRefreshLogLink" class="btn btn-blue" href="{h(refresh_log_href)}"{' hidden' if not refresh_log_href else ''}>查看刷新日志</a>
    <a id="onlineRefreshLogin" class="btn btn-gray" href="/login?next=%2Fonline-scripts" hidden>重新登录</a>
    <button id="onlineRefreshQuery" class="btn btn-primary" type="button" hidden>重新查询同步状态</button>
    <div id="onlineRefreshPersistence">{refresh_persistence_html}</div>
    <div id="onlineRefreshHistory" class="action-row" style="margin-top:8px;">{refresh_history_html(refresh_history)}</div>
    <div id="onlineRefreshHistoryError" class="notice red" role="status"{' hidden' if not refresh_history_error else ''}>{h(refresh_history_error)}</div>
</div>
</section>

<section class="section fls-section" id="online-list">
    <h2 class="section-title">脚本列表，本地缓存</h2>
    <div class="help">
        缓存文件：{h(ONLINE_SCRIPT_CACHE_FILE)}
    </div>
    <br>

    <div class="fls-card-grid mobile-list">
        {render_online_script_rows(items)}
    </div>
</section>
{page_links_html}

<script>
window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ = false;
window.__FLS_ONLINE_REFRESH_RELOADED__ = false;
// AJAX navigation can execute this page script again in the same document.
var refreshStorageKey = "fls-online-refresh-request";

function readOnlineRefreshPending(){{
    try{{
        const value = JSON.parse(sessionStorage.getItem(refreshStorageKey) || "null");
        if(!value || !/^[a-f0-9]{{32}}$/.test(String(value.request_id || ""))) return null;
        return value;
    }}catch(_error){{ return null; }}
}}

function saveOnlineRefreshPending(value){{
    try{{ sessionStorage.setItem(refreshStorageKey, JSON.stringify(value)); }}catch(_error){{}}
}}

function clearOnlineRefreshPending(){{
    try{{ sessionStorage.removeItem(refreshStorageKey); }}catch(_error){{}}
}}

var onlineRefreshPending = readOnlineRefreshPending();
if(onlineRefreshPending) window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ = true;
var onlineRefreshStatusPending = false;
var onlineRefreshStatusGeneration = typeof onlineRefreshStatusGeneration === "number" ? onlineRefreshStatusGeneration + 1 : 0;
var onlineRefreshMissingRequest = false;

function invalidateOnlineRefreshStatus(){{
    onlineRefreshStatusGeneration++;
    onlineRefreshStatusPending = false;
}}

function escapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c];
    }});
}}

function renderOnlineRefreshPersistence(info){{
    const element = document.getElementById("onlineRefreshPersistence");
    if(!element) return;
    const requestId = String(info.request_id || "");
    if(!info.persistence_error || !/^[a-f0-9]{{32}}$/.test(requestId)){{ element.innerHTML = ""; return; }}
    const diagnosis = [info.error, info.persistence_error].filter(Boolean).map(escapeHtml).join("<br>");
    element.innerHTML = "<div class='notice red'>" + diagnosis + "</div>" +
        "<" + "form class='inline-form fls-online-refresh-recover-form' method='post' action='/online-scripts/refresh-recover/" + requestId +
        "' data-api-action='/api/online-scripts/refresh-recover/" + requestId + "' data-refresh-recover-id='" + requestId + "'>" +
        "<button class='btn btn-primary' type='submit'>修复刷新回执</button></form>";
}}

async function submitOnlineRefreshRecoverForm(form, event){{
    if(event){{ event.preventDefault(); event.stopImmediatePropagation(); }}
    if(form.dataset.pending) return;
    const requestId = String(form.getAttribute("data-refresh-recover-id") || "");
    if(!/^[a-f0-9]{{32}}$/.test(requestId)) return;
    form.dataset.pending = "1";
    const button = form.querySelector("button");
    if(button) button.disabled = true;
    const pending = readOnlineRefreshPending();
    saveOnlineRefreshPending(pending && pending.request_id === requestId ? pending : {{request_id:requestId}});
    invalidateOnlineRefreshStatus();
    try{{
        const headers = {{"X-Requested-With":"XMLHttpRequest"}};
        const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
        if(csrf) headers["X-CSRF-Token"] = csrf;
        const response = await fetch("/api/online-scripts/refresh-recover/" + requestId, {{method:"POST", headers, credentials:"same-origin"}});
        const result = await flsReadJsonResponse(response);
        if((readOnlineRefreshPending() || {{}}).request_id !== requestId) return;
        if(!result || result.ok !== true || result.request_id !== requestId) throw new Error("回执修复响应无效，请查询原请求");
        if(typeof flsClearPageCache === "function") flsClearPageCache();
        await updateOnlineRefreshStatus();
        await updateOnlineRefreshHistory();
    }}catch(error){{
        if((readOnlineRefreshPending() || {{}}).request_id !== requestId) return;
        const text = document.getElementById("onlineRefreshStatusText");
        if(text) text.textContent = "回执修复未确认：" + (error && error.message || String(error)) + "。请先查询原请求，未重新抓取源。";
        const query = document.getElementById("onlineRefreshQuery");
        if(query){{ query.hidden = false; query.disabled = false; }}
        const login = document.getElementById("onlineRefreshLogin");
        const denied = error && (error.status === 401 || error.status === 403);
        if(login){{ login.hidden = !denied; if(denied) login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); }}
    }}finally{{
        delete form.dataset.pending;
        if(button) button.disabled = false;
    }}
}}

async function updateOnlineRefreshHistory(){{
    const history = document.getElementById("onlineRefreshHistory");
    const error = document.getElementById("onlineRefreshHistoryError");
    if(!history) return;
    try{{
        const response = await fetch({json.dumps(refresh_history_url)}, {{cache:"no-store", credentials:"same-origin"}});
        const json = await flsReadJsonResponse(response);
        if(!json || !Array.isArray(json.items)) throw new Error("刷新历史响应无效");
        const rows = json.items.map(function(item){{
            const requestId = String(item && item.request_id || "");
            if(!/^[a-f0-9]{{32}}$/.test(requestId)) return "";
            const status = escapeHtml(item.status || "-");
            const updatedAt = escapeHtml(item.updated_at || "-");
            const link = "<a class='btn btn-gray' href='/online-scripts/refresh-log/" + encodeURIComponent(requestId) + "'>查看 " + status + " 刷新（" + updatedAt + "）</a>";
            const failureCount = Number(item.task_link_failure_count || 0);
            const failureLink = failureCount > 0
                ? " <a class='btn btn-orange' href='/online-scripts?refresh_request_id=" + encodeURIComponent(requestId) + "&failure_offset=0#online-status'>查看 " + failureCount + " 个失败源</a>"
                : "";
            let actions = link + failureLink;
            if(String(item.status || "") === "结果未知" && !item.confirmation){{
                actions += "<" + "form class='inline-form fls-online-refresh-confirm-form' method='post' action='/online-scripts/refresh-confirm/" + requestId + "' data-refresh-confirm-id='" + requestId + "'>" +
                    "<" + "button class='btn btn-gray' type='submit'>检查当前缓存结果</button><" + "/form>";
            }}
            if(["失败","部分完成","结果未知","启动失败"].indexOf(String(item.status || "")) < 0){{
                return "<div class='fls-online-refresh-history-row'>" + actions + "</div>";
            }}
            const proxyId = escapeHtml(item.proxy_id || "");
            return "<div class='fls-online-refresh-history-row' data-refresh-retry-of='" + requestId + "'>" + actions +
                "<" + "form class='inline-form fls-online-refresh-retry-form' method='post' action='/online-scripts/refresh-retry/" + requestId + "' data-refresh-retry-of='" + requestId + "'>" +
                "<" + "input type='hidden' name='retry_request_id' value='" + randomOnlineRefreshRequestId() + "'>" +
                "<" + "input type='hidden' name='proxy_id' value='" + proxyId + "'>" +
                "<" + "button class='btn btn-orange' type='submit'>明确重新同步</button><" + "/form></div>";
        }}).filter(Boolean).join("");
        history.innerHTML = rows || "<span class='help'>暂无可查询的刷新历史</span>";
        bindOnlineRefreshRetryForms(history);
        bindOnlineRefreshConfirmForms(history);
        if(error){{ error.hidden = true; error.textContent = ""; }}
    }}catch(e){{
        if(error){{
            const status = e && e.status ? "HTTP " + e.status : "请求失败";
            error.hidden = false;
            error.textContent = "刷新历史读取失败（" + status + "）：" + (e && e.message || String(e || "未知错误")) + "。当前已显示的历史未清除。";
        }}
    }}
}}

function randomOnlineRefreshRequestId(){{
    if(typeof crypto !== "undefined" && crypto.getRandomValues) return Array.from(crypto.getRandomValues(new Uint8Array(16)), function(value){{return value.toString(16).padStart(2, "0");}}).join("");
    return Array.from({{length:32}}, function(){{return Math.floor(Math.random() * 16).toString(16);}}).join("");
}}

function onlineRefreshRetryStorageKey(original){{ return "fls-online-refresh-retry:" + original; }}
function readOnlineRefreshRetry(original){{
    try{{ const value = JSON.parse(sessionStorage.getItem(onlineRefreshRetryStorageKey(original)) || "null");
        return value && /^[a-f0-9]{{32}}$/.test(String(value.request_id || "")) ? value : null;
    }}catch(_error){{ return null; }}
}}
function saveOnlineRefreshRetry(original, value){{ try{{ sessionStorage.setItem(onlineRefreshRetryStorageKey(original), JSON.stringify(value)); }}catch(_error){{}} }}

async function submitOnlineRefreshRetryForm(form, event){{
    if(event){{ event.preventDefault(); event.stopImmediatePropagation(); }}
    const original = String(form.getAttribute("data-refresh-retry-of") || "");
    const input = form.querySelector('[name="retry_request_id"]');
    const proxy = form.querySelector('[name="proxy_id"]');
    const button = form.querySelector('button[type="submit"]');
    if(!/^[a-f0-9]{{32}}$/.test(original)) return;
    const saved = readOnlineRefreshRetry(original);
    const retryId = saved && saved.proxy_id === (proxy && proxy.value || "") ? saved.request_id : (input && input.value || randomOnlineRefreshRequestId());
    if(input) input.value = retryId;
    const pending = {{request_id: retryId, proxy_id: proxy && proxy.value || "", original_request_id: original}};
    saveOnlineRefreshRetry(original, pending);
    saveOnlineRefreshPending(pending);
    invalidateOnlineRefreshStatus();
    if(button) button.disabled = true;
    try{{
        const headers = {{"X-Requested-With":"XMLHttpRequest"}};
        const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
        if(csrf) headers["X-CSRF-Token"] = csrf;
        const response = await fetch(form.action.replace("/online-scripts/refresh-retry/", "/api/online-scripts/refresh-retry/"), {{method:"POST", body:new FormData(form), headers, credentials:"same-origin"}});
        const json = await flsReadJsonResponse(response);
        if(!json || json.ok !== true) throw Object.assign(new Error(json && (json.error || json.msg) || "重试未启动"), {{status: response.status}});
        pending.server_id = String(json.id || "");
        saveOnlineRefreshPending(pending);
        await updateOnlineRefreshStatus();
        await updateOnlineRefreshHistory();
    }}catch(error){{
        if(button) button.disabled = false;
        const retryNotStarted = !!(error && error.status === 409 && error.payload && error.payload.retry_not_started === true);
        if(retryNotStarted){{
            const current = readOnlineRefreshPending();
            if(current && current.request_id === retryId) clearOnlineRefreshPending();
            const savedRetry = readOnlineRefreshRetry(original);
            if(savedRetry && savedRetry.request_id === retryId){{
                try{{ sessionStorage.removeItem(onlineRefreshRetryStorageKey(original)); }}catch(_error){{}}
            }}
            if(input) input.value = randomOnlineRefreshRequestId();
        }}
        const errorEl = document.getElementById("onlineRefreshHistoryError");
        const denied = error && (error.status === 401 || error.status === 403);
        if(errorEl){{
            errorEl.hidden = false;
            errorEl.textContent = (denied ? "登录状态已失效" : retryNotStarted ? "此次重试未启动" : "在线脚本源重试未确认") + "：" + (error && error.message || String(error || "请求失败")) + (retryNotStarted ? "。可发起新的完整同步。" : "。请先查询原重试状态，原历史未覆盖。");
            if(retryNotStarted){{
                const link = document.createElement("a");
                link.href = "/online-scripts#online-status";
                link.textContent = "发起新的完整同步";
                errorEl.appendChild(document.createTextNode(" "));
                errorEl.appendChild(link);
            }}
        }}
        const query = document.getElementById("onlineRefreshQuery");
        const card = document.getElementById("onlineRefreshStatusCard");
        if(card) card.style.display = "block";
        if(query){{ query.hidden = retryNotStarted; query.disabled = false; }}
        const login = document.getElementById("onlineRefreshLogin");
        if(login){{ login.hidden = !denied; if(denied) login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); }}
    }}
}}

function bindOnlineRefreshRetryForms(root){{
    const scope = root || document;
    if(!scope || !scope.querySelectorAll) return;
    scope.querySelectorAll(".fls-online-refresh-retry-form button[type='submit']").forEach(function(button){{
        if(button.__flsOnlineRefreshRetryBound) return;
        button.__flsOnlineRefreshRetryBound = true;
        button.onclick = function(event){{ event.preventDefault(); submitOnlineRefreshRetryForm(button.form || button.closest(".fls-online-refresh-retry-form"), event); return false; }};
    }});
}}

async function submitOnlineRefreshConfirmForm(form, event){{
    if(event){{ event.preventDefault(); event.stopImmediatePropagation(); }}
    const requestId = String(form.getAttribute("data-refresh-confirm-id") || "");
    const button = form.querySelector('button[type="submit"]');
    if(!/^[a-f0-9]{{32}}$/.test(requestId)) return;
    if(button) button.disabled = true;
    try{{
        const headers = {{"X-Requested-With":"XMLHttpRequest"}};
        const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
        if(csrf) headers["X-CSRF-Token"] = csrf;
        const response = await fetch(form.action.replace("/online-scripts/refresh-confirm/", "/api/online-scripts/refresh-confirm/"), {{method:"POST", body:new FormData(form), headers, credentials:"same-origin"}});
        const json = await flsReadJsonResponse(response);
        if(!json || json.ok !== true) throw Object.assign(new Error(json && (json.error || json.msg) || "当前结果仍未知"), {{status:response.status}});
        const error = document.getElementById("onlineRefreshHistoryError");
        if(error){{ error.hidden = false; error.textContent = json.status === "已确认发布" ? "已确认：当前缓存与刷新发布回执一致。" : "已确认：当前缓存仍为刷新前内容，未发现缓存发布。"; }}
        await updateOnlineRefreshHistory();
    }}catch(e){{
        if(button) button.disabled = false;
        const error = document.getElementById("onlineRefreshHistoryError");
        const denied = e && (e.status === 401 || e.status === 403);
        if(error){{ error.hidden = false; error.textContent = (denied ? "登录状态已失效" : "刷新结果仍未知") + "：" + (e && e.message || String(e || "请求失败")) + "。请保留原日志并重新检查。"; }}
        const login = document.getElementById("onlineRefreshLogin");
        if(login){{ login.hidden = !denied; if(denied) login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search); }}
    }}
}}

function bindOnlineRefreshConfirmForms(root){{
    const scope = root || document;
    if(!scope || !scope.querySelectorAll) return;
    scope.querySelectorAll(".fls-online-refresh-confirm-form button[type='submit']").forEach(function(button){{
        if(button.__flsOnlineRefreshConfirmBound) return;
        button.__flsOnlineRefreshConfirmBound = true;
        button.onclick = function(event){{ event.preventDefault(); submitOnlineRefreshConfirmForm(button.form || button.closest(".fls-online-refresh-confirm-form"), event); return false; }};
    }});
}}

async function updateOnlineRefreshStatus(){{
    if(onlineRefreshStatusPending) return;
    const generation = ++onlineRefreshStatusGeneration;
    const statusText = document.getElementById("onlineRefreshStatusText");
    const queryButton = document.getElementById("onlineRefreshQuery");
    onlineRefreshStatusPending = true;
    if(queryButton) queryButton.disabled = true;
    try {{
        const currentPending = readOnlineRefreshPending();
        const statusUrl = currentPending && currentPending.request_id
            ? "/api/online-scripts/refresh-status/" + encodeURIComponent(currentPending.request_id)
            : "/api/online-scripts/refresh-status";
        const res = await fetch(statusUrl, {{cache:"no-store", credentials:"same-origin"}});
        const json = await flsReadJsonResponse(res);
        if(generation !== onlineRefreshStatusGeneration || document.getElementById("onlineRefreshStatusText") !== statusText) return;
        if(!json || typeof json.running !== "boolean" || json.ok === false){{
            throw new Error("同步状态响应无效，请重新查询原请求");
        }}
        if(currentPending && json.request_id !== currentPending.request_id){{
            throw new Error("同步状态与原请求不一致，请重新查询原请求");
        }}
        onlineRefreshMissingRequest = false;

        if(currentPending && json.id && !currentPending.server_id){{
            currentPending.server_id = String(json.id);
            saveOnlineRefreshPending(currentPending);
        }}

        const card = document.getElementById("onlineRefreshStatusCard");
        const text = document.getElementById("onlineRefreshStatusText");
        const btn = document.getElementById("onlineRefreshBtn");
        const login = document.getElementById("onlineRefreshLogin");
        const logLink = document.getElementById("onlineRefreshLogLink");
        const failures = document.getElementById("onlineRefreshTaskLinkFailures");

        if(!card || !text) return;
        renderOnlineRefreshPersistence(json);
        if(queryButton) queryButton.hidden = true;
        if(login) login.hidden = true;
        if(logLink && json.request_id && /^[a-f0-9]{{32}}$/.test(String(json.request_id))){{
            logLink.href = "/online-scripts/refresh-log/" + encodeURIComponent(String(json.request_id));
            logLink.hidden = false;
        }}

        const taskFailures = Array.isArray(json.task_link_failures) ? json.task_link_failures : [];
        if(failures && !{json.dumps(bool(selected_refresh_request))} && {failure_offset} === 0) {{
            const totalFailures = Number(json.task_link_failure_count || taskFailures.length);
            const failureRequestId = String(json.request_id || "");
            const recoveryMaintenance = json.task_link_recovery_maintenance || {{}};
            const recoveryMaintenanceHtml = recoveryMaintenance.repaired && /^[a-f0-9]{{32}}$/.test(failureRequestId)
                ? "<div class='notice green' role='status'>恢复回执副本已自动修复。<a class='btn btn-gray' href='/online-scripts?refresh_request_id=" +
                    encodeURIComponent(failureRequestId) + "&failure_offset=0#online-status'>重新读取恢复状态</a></div>"
                : "";
            failures.innerHTML = recoveryMaintenanceHtml + taskFailures.map(function(item) {{
                const id = String(item && item.script_id || "").trim();
                if(!id) return "";
                const name = escapeHtml(item.name || id);
                const recovery = item.recovery || {{}};
                if(recovery.state === "completed"){{
                    return "<div class='help' style='margin-top:6px;'>" + name + "：已恢复，新增 " +
                        Number(recovery.added || 0) + " 个任务，当前共 " + Number(recovery.total || 0) + " 个任务</div>";
                }}
                if(item.recovery_error){{
                    return "<div class='help' style='margin-top:6px;'>" + name + "：恢复状态不可读取，请先修复回执后重新查询</div>";
                }}
                const detail = escapeHtml(item.error || "外部任务源加载失败");
                const bound = /^[a-f0-9]{{32}}$/.test(failureRequestId);
                const href = bound
                    ? "/online-scripts/refresh-task-link/" + encodeURIComponent(failureRequestId) + "/task-link/" + encodeURIComponent(id)
                    : "/online-scripts/task-link/" + encodeURIComponent(id);
                const apiHref = bound
                    ? "/api/online-scripts/refresh-task-link/" + encodeURIComponent(failureRequestId) + "/task-link/" + encodeURIComponent(id)
                    : "/api/online-scripts/task-link/" + encodeURIComponent(id);
                const proxy = escapeHtml(item.proxy_id || "");
                const csrf = typeof flsGetCsrfToken === "function" ? escapeHtml(flsGetCsrfToken()) : "";
                const requestId = /^[a-f0-9]{{32}}$/.test(String(item.task_link_request_id || ""))
                    ? String(item.task_link_request_id)
                    : (typeof crypto !== "undefined" && crypto.getRandomValues)
                    ? Array.from(crypto.getRandomValues(new Uint8Array(16)), function(value){{return value.toString(16).padStart(2, "0");}}).join("")
                    : Array.from({{length:32}}, function(){{return Math.floor(Math.random() * 16).toString(16);}}).join("");
                return "<div class='help' style='margin-top:6px;'>" + name + "：" + detail +
                    " <" + "form class='inline-form fls-online-task-link-form' data-script-id='" + escapeHtml(id) + "' data-api-action='" + escapeHtml(apiHref) + "' method='post' action='" + escapeHtml(href) + "'>" +
                    "<input type='hidden' name='csrf_token' value='" + csrf + "'>" +
                    "<input type='hidden' name='request_id' value='" + requestId + "'>" +
                    (bound ? "<input type='hidden' name='origin_refresh_request_id' value='" + failureRequestId + "'>" : "") +
                    "<input type='hidden' name='proxy_id' value='" + proxy + "'>" +
                    "<button class='btn btn-orange' type='submit'>重新同步外部任务源</button></form></div>";
            }}).join("") +
                "<div class='help'>失败源 1-" + Math.min(taskFailures.length, totalFailures) + " / " + totalFailures + "</div>" +
                (failureRequestId && totalFailures > taskFailures.length
                    ? "<div class='action-row'><a class='btn btn-gray' href='/online-scripts?refresh_request_id=" + encodeURIComponent(failureRequestId) + "&failure_offset=" + taskFailures.length + "#online-status'>下一页</a></div>"
                    : "");
            if(window.__FLS_BIND_ONLINE_TASK_LINK_FORMS__){{
                window.__FLS_BIND_ONLINE_TASK_LINK_FORMS__(failures);
            }}
        }}

        if(json.running){{
            window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ = true;
        }}

        if(currentPending && !json.running && json.id &&
            currentPending.server_id === String(json.id) &&
            (json.status === "已完成" || json.status === "部分完成")){{
            clearOnlineRefreshPending();
        }}

        if(json.running || json.message || json.error || taskFailures.length){{
            card.style.display = "block";

            let color = json.error ? "#dc2626" : (taskFailures.length ? "#b45309" : "#18a058");
            let detail = json.message || json.error || "";
            let first = json.running ? "正在后台同步在线脚本源，请稍候..." :
                ((json.status ? json.status + "：" : "") + detail);

            text.innerHTML =
                "<span style='color:" + color + ";font-weight:900;'>" + escapeHtml(first) + "</span><br>" +
                "更新时间：" + escapeHtml(json.updated_at || "-") + "<br>" +
                "日志：" + escapeHtml(json.log_file || "-");

            if(btn){{
                btn.disabled = !!json.running;
                btn.textContent = json.running ? "正在同步中..." : "同步在线脚本源";
            }}
        }}else{{
            if(btn){{
                btn.disabled = false;
                btn.textContent = "同步在线脚本源";
            }}
        }}

        if(
            window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ &&
            !json.running &&
            !json.error &&
            json.message &&
            !window.__FLS_ONLINE_REFRESH_RELOADED__
        ){{
            window.__FLS_ONLINE_REFRESH_RELOADED__ = true;

            if(btn){{
                btn.textContent = "同步成功，正在更新列表...";
            }}

            setTimeout(() => {{
                location.href = "/online-scripts";
            }}, 800);

            return;
        }}

        if(json.running){{
            setTimeout(updateOnlineRefreshStatus, 2000);
        }}

    }} catch(e) {{
        if(generation !== onlineRefreshStatusGeneration || document.getElementById("onlineRefreshStatusText") !== statusText) return;
        if(e && e.status === 404 && readOnlineRefreshPending()) onlineRefreshMissingRequest = true;
        const card = document.getElementById("onlineRefreshStatusCard");
        const text = document.getElementById("onlineRefreshStatusText");
        const btn = document.getElementById("onlineRefreshBtn");
        const login = document.getElementById("onlineRefreshLogin");
        const status = e && e.status ? "HTTP " + e.status : "请求失败";
        const detail = e && e.message ? e.message : String(e || "未知错误");
        const denied = e && (e.status === 401 || e.status === 403);
        const next = denied
            ? "登录状态已失效，请重新登录后重试"
            : (e && e.status === 404
                ? "原刷新记录不存在或已被替换；确认后可重新发起同步"
                : "点击重新查询同步状态；已有日志和恢复操作已保留");
        if(card) card.style.display = "block";
        if(queryButton) queryButton.hidden = false;
        if(text) text.innerHTML =
            "<span style='color:#dc2626;font-weight:900;'>同步状态读取失败（" +
            escapeHtml(status) + "）：" + escapeHtml(detail) +
            "。下一步：" + escapeHtml(next) + "</span>";
        if(btn) {{
            btn.disabled = false;
            btn.textContent = onlineRefreshMissingRequest ? "明确重新同步在线脚本源" : "同步在线脚本源";
        }}
        if(login) {{
            login.hidden = !denied;
            if(denied) login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        }}
    }} finally {{
        if(generation === onlineRefreshStatusGeneration){{
            onlineRefreshStatusPending = false;
            if(queryButton) queryButton.disabled = false;
        }}
    }}
}}

var onlineRefreshQuery = document.getElementById("onlineRefreshQuery");
if(onlineRefreshQuery) onlineRefreshQuery.onclick = updateOnlineRefreshStatus;
updateOnlineRefreshStatus();
updateOnlineRefreshHistory();
bindOnlineRefreshRetryForms(document);
bindOnlineRefreshConfirmForms(document);

async function submitOnlineRefreshForm(form, event){{
    if(event){{ event.preventDefault(); event.stopImmediatePropagation(); }}
    const requestInput = form.querySelector('input[name="request_id"]');
    const proxyInput = form.querySelector('[name="proxy_id"]');
    const button = form.querySelector('button[type="submit"]');
    const proxyId = proxyInput && proxyInput.value || "";
    if(onlineRefreshMissingRequest && readOnlineRefreshPending()){{
        if(!window.confirm("原刷新回执已不存在，无法确认先前结果。确定创建新的同步请求吗？这会重新抓取并更新当前缓存。")) return;
        clearOnlineRefreshPending();
        if(requestInput) requestInput.value = randomOnlineRefreshRequestId();
        onlineRefreshMissingRequest = false;
    }}
    const savedPending = readOnlineRefreshPending();
    const requestId = savedPending && savedPending.proxy_id === proxyId
        ? savedPending.request_id
        : (requestInput && requestInput.value || "");
    if(!/^[a-f0-9]{{32}}$/.test(requestId)){{
        const text = document.getElementById("onlineRefreshStatusText");
        if(text) text.textContent = "刷新请求标识无效，请刷新页面后重试";
        return;
    }}
    if(requestInput) requestInput.value = requestId;
    const pending = {{request_id: requestId, proxy_id: proxyId, server_id: savedPending && savedPending.proxy_id === proxyId ? (savedPending.server_id || "") : ""}};
    saveOnlineRefreshPending(pending);
    invalidateOnlineRefreshStatus();
    window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ = true;
    if(button) button.disabled = true;
    try{{
        const headers = {{"X-Requested-With":"XMLHttpRequest"}};
        const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
        if(csrf) headers["X-CSRF-Token"] = csrf;
        const response = await fetch(form.action.replace("/online-scripts/refresh", "/api/online-scripts/refresh"), {{
            method: "POST", body: new FormData(form), headers, credentials: "same-origin"
        }});
        const json = await flsReadJsonResponse(response);
        if(!json || json.ok !== true) throw Object.assign(new Error(json && (json.error || json.msg) || "刷新未启动"), {{status: response.status}});
        pending.server_id = String(json.id || "");
        saveOnlineRefreshPending(pending);
        await updateOnlineRefreshStatus();
    }}catch(error){{
        if(button) button.disabled = false;
        const card = document.getElementById("onlineRefreshStatusCard");
        const text = document.getElementById("onlineRefreshStatusText");
        const login = document.getElementById("onlineRefreshLogin");
        const denied = error && (error.status === 401 || error.status === 403);
        if(card) card.style.display = "block";
        if(text) text.textContent = "刷新请求失败：" + (error && error.message || String(error || "请求失败")) + "。请查询状态后重试。";
        const query = document.getElementById("onlineRefreshQuery");
        if(query){{ query.hidden = false; query.disabled = false; }}
        if(login){{
            login.hidden = !denied;
            if(denied) login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        }}
    }}
}}

if(document.querySelectorAll){{
    document.querySelectorAll(".fls-online-refresh-form button[type='submit']").forEach(function(button){{
        button.onclick = function(event){{
            event.preventDefault();
            const form = button.form || (button.closest ? button.closest(".fls-online-refresh-form") : null);
            if(form) submitOnlineRefreshForm(form, event);
            return false;
        }};
    }});
}}
if(!window.__FLS_ONLINE_REFRESH_SUBMIT_BOUND__ && document.addEventListener){{
    window.__FLS_ONLINE_REFRESH_SUBMIT_BOUND__ = true;
    document.addEventListener("submit", function(event){{
        const form = event.target && event.target.closest ? event.target.closest(".fls-online-refresh-form") : null;
        if(form) return submitOnlineRefreshForm(form, event);
    }}, true);
    document.addEventListener("submit", function(event){{
        const form = event.target && event.target.closest ? event.target.closest(".fls-online-refresh-retry-form") : null;
        if(form) return submitOnlineRefreshRetryForm(form, event);
    }}, true);
    document.addEventListener("submit", function(event){{
        const form = event.target && event.target.closest ? event.target.closest(".fls-online-refresh-confirm-form") : null;
        if(form) return submitOnlineRefreshConfirmForm(form, event);
    }}, true);
    document.addEventListener("submit", function(event){{
        const form = event.target && event.target.closest ? event.target.closest(".fls-online-refresh-recover-form") : null;
        if(form) return submitOnlineRefreshRecoverForm(form, event);
    }}, true);
}}

(function(){{
const recovery = document.getElementById("onlineTaskLinkRecovery");
const storageKey = "fls-online-task-link-request";

function readPending(){{
    let raw = null;
    try{{ raw = sessionStorage.getItem(storageKey); }}catch(_error){{ return null; }}
    if(!raw) return null;
    try{{
        const value = JSON.parse(raw);
        if(!value || !/^[a-f0-9]{{32}}$/.test(String(value.request_id || "")) || !value.script_id) return null;
        return value;
    }}catch(_error){{ return null; }}
}}

function writeMessage(text, denied, pending, allowContinue, allowConfirm, historyFull){{
    if(!recovery || !pending || !/^[a-f0-9]{{32}}$/.test(String(pending.request_id || ""))) return;
    recovery.hidden = false;
    recovery.textContent = "";
    const message = document.createElement("span");
    message.textContent = String(text || "外部任务源同步未完成");
    recovery.appendChild(message);
    if(historyFull){{
        const history = document.createElement("a");
        history.className = "btn btn-primary";
        history.href = "/online-scripts/task-link-history?recovery=1";
        history.textContent = "处理待恢复同步";
        recovery.appendChild(history);
        return;
    }}
    const status = document.createElement("a");
    status.className = "btn btn-blue";
    status.href = "/online-scripts/task-link-status/" + encodeURIComponent(pending.request_id);
    status.textContent = "查询原同步请求";
    recovery.appendChild(status);
    if(denied){{
        const login = document.createElement("a");
        login.className = "btn btn-primary";
        login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        login.textContent = "重新登录";
        recovery.appendChild(login);
    }}else if(allowConfirm){{
        const confirmButton = document.createElement("button");
        confirmButton.type = "button";
        confirmButton.className = "btn btn-primary";
        confirmButton.textContent = "检查当前任务合并";
        confirmButton.addEventListener("click", function(){{ confirmPending(pending, confirmButton); }});
        recovery.appendChild(confirmButton);
    }}else if(allowContinue){{
        const form = document.createElement("form");
        form.className = "fls-online-task-link-form inline-form";
        form.method = "post";
        const originRefreshId = String(pending.origin_refresh_request_id || "");
        const hasRefreshOrigin = /^[a-f0-9]{{32}}$/.test(originRefreshId);
        form.action = hasRefreshOrigin
            ? "/online-scripts/refresh-task-link/" + encodeURIComponent(originRefreshId) + "/task-link/" + encodeURIComponent(pending.script_id)
            : "/online-scripts/task-link/" + encodeURIComponent(pending.script_id);
        form.dataset.apiAction = hasRefreshOrigin
            ? "/api/online-scripts/refresh-task-link/" + encodeURIComponent(originRefreshId) + "/task-link/" + encodeURIComponent(pending.script_id)
            : "/api/online-scripts/task-link/" + encodeURIComponent(pending.script_id);
        form.setAttribute("data-script-id", pending.script_id);
        const requestInput = document.createElement("input");
        requestInput.type = "hidden";
        requestInput.name = "request_id";
        requestInput.value = pending.request_id;
        form.appendChild(requestInput);
        if(hasRefreshOrigin){{
            const originInput = document.createElement("input");
            originInput.type = "hidden";
            originInput.name = "origin_refresh_request_id";
            originInput.value = originRefreshId;
            form.appendChild(originInput);
        }}
        const proxyInput = document.createElement("input");
        proxyInput.type = "hidden";
        proxyInput.name = "proxy_id";
        proxyInput.value = pending.proxy_id || "";
        form.appendChild(proxyInput);
        const continueButton = document.createElement("button");
        continueButton.type = "submit";
        continueButton.className = "btn btn-primary";
        continueButton.textContent = "继续原同步请求";
        continueButton.onclick = function(event){{
            event.preventDefault();
            submitTaskLinkForm(form, event);
            return false;
        }};
        form.appendChild(continueButton);
        recovery.appendChild(form);
    }}else{{
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn btn-gray";
        retry.textContent = "重新读取同步状态";
        retry.addEventListener("click", function(){{ queryPending(pending); }});
        recovery.appendChild(retry);
    }}
}}

async function confirmPending(pending, button){{
    if(pending.confirming) return;
    pending.confirming = true;
    if(button) button.disabled = true;
    try{{
        const headers = {{"X-Requested-With":"XMLHttpRequest"}};
        const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
        if(csrf) headers["X-CSRF-Token"] = csrf;
        const response = await fetch("/api/online-scripts/task-link-confirm/" + encodeURIComponent(pending.request_id), {{method:"POST", headers, credentials:"same-origin"}});
        const json = await flsReadJsonResponse(response);
        if(!json || json.ok !== true || json.request_id !== pending.request_id || !["merged", "not_merged"].includes(json.confirmation)){{
            throw Object.assign(new Error(json && json.msg || "确认响应无效，请查询原请求"), {{status: response.status}});
        }}
        if(json.confirmation === "merged"){{
            sessionStorage.removeItem(storageKey);
            window.location.href = "/online-scripts?msg=" + encodeURIComponent(json.msg || "已确认任务已合并");
            return;
        }}
        writeMessage(json.msg || "任务尚未合并，请继续原同步请求", false, pending, true, false);
    }}catch(error){{
        const denied = error && (error.status === 401 || error.status === 403);
        const payload = error && error.payload;
        const stale = payload && payload.request_id === pending.request_id && payload.confirmation_stale === true;
        writeMessage("任务合并确认失败：" + (error && error.message || String(error || "请求失败")), denied, pending, false, !stale && !(payload && payload.archived));
    }}finally{{ pending.confirming = false; }}
}}

async function queryPending(pending){{
    try{{
        const response = await fetch("/api/online-scripts/task-link-status/" + encodeURIComponent(pending.request_id), {{cache:"no-store", credentials:"same-origin"}});
        const json = await flsReadJsonResponse(response);
        if(!json || json.request_id !== pending.request_id) throw new Error("同步状态响应无效");
        if(json.confirmation_stale === true || json.confirmation_check_error){{
            writeMessage(json.confirmation_error || ("原同步请求确认状态无法复核：" + json.confirmation_check_error), false, pending, false, false);
            return;
        }}
        if(json.state === "completed" || json.archived === true){{
            sessionStorage.removeItem(storageKey);
            window.location.href = "/online-scripts/task-link-status/" + encodeURIComponent(pending.request_id);
            return;
        }}
        writeMessage("原同步请求：" + (json.error || json.state || "未知"), false, pending,
            json.state === "failed" && json.retry_allowed !== false, json.state === "unknown");
    }}catch(error){{
        const denied = error && (error.status === 401 || error.status === 403);
        writeMessage("原同步状态读取失败：" + (error && error.message || String(error || "查询失败")), denied, pending);
    }}
}}

async function submitTaskLinkForm(form, event){{
        event.preventDefault();
        event.stopImmediatePropagation();
        if(form.__flsTaskLinkPending) return;
        const requestInput = form.querySelector('input[name="request_id"]');
        const button = form.querySelector('button[type="submit"]');
        const requestId = requestInput && requestInput.value || "";
        const scriptId = form.getAttribute("data-script-id") || "";
        const originInput = form.querySelector('[name="origin_refresh_request_id"]');
        const originRefreshRequestId = originInput && originInput.value || "";
        const proxyInput = form.querySelector('[name="proxy_id"]');
        const proxyId = proxyInput && proxyInput.value || "";
        const pending = {{request_id: requestId, script_id: scriptId, proxy_id: proxyId, origin_refresh_request_id: originRefreshRequestId}};
        if(!/^[a-f0-9]{{32}}$/.test(requestId) || !scriptId){{
            writeMessage("同步请求标识无效，请刷新页面后重试", false, pending);
            return;
        }}
        try{{ sessionStorage.setItem(storageKey, JSON.stringify(pending)); }}catch(_error){{}}
        form.__flsTaskLinkPending = true;
        if(button) button.disabled = true;
        try{{
            const headers = {{"X-Requested-With":"XMLHttpRequest"}};
            const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
            if(csrf) headers["X-CSRF-Token"] = csrf;
            const apiAction = form.dataset.apiAction || form.action.replace("/online-scripts/task-link/", "/api/online-scripts/task-link/");
            const response = await fetch(apiAction, {{method:"POST", body:new FormData(form), headers, credentials:"same-origin"}});
            const json = await flsReadJsonResponse(response);
            if(!json || json.ok !== true || json.request_id !== requestId) throw Object.assign(new Error(json && json.msg || "同步响应无效，请查询原请求"), {{status: response.status}});
            sessionStorage.removeItem(storageKey);
            window.location.href = json.replayed
                ? (json.status_url || ("/online-scripts/task-link-status/" + encodeURIComponent(requestId)))
                : "/online-scripts?msg=" + encodeURIComponent(json.msg || "外部任务源同步完成");
        }}catch(error){{
            if(button) button.disabled = false;
            const denied = error && (error.status === 401 || error.status === 403);
            const payload = error && error.payload;
            const bound = payload && payload.request_id === requestId;
            const historyFull = bound && payload.history_full === true && payload.accepted === false;
            if(historyFull){{ try{{ sessionStorage.removeItem(storageKey); }}catch(_error){{}} }}
            writeMessage("外部任务源同步失败：" + (error && error.message || String(error || "请求失败")), denied, pending,
                bound && payload.retry_allowed === true, bound && !payload.archived && payload.state === "unknown", historyFull);
        }}finally{{ form.__flsTaskLinkPending = false; }}
}}

function bindOnlineTaskLinkForms(root){{
    const scope = root || document;
    if(!scope || !scope.querySelectorAll) return;
    scope.querySelectorAll(".fls-online-task-link-form button[type='submit']").forEach(function(button){{
        if(button.__flsOnlineTaskLinkBound) return;
        button.__flsOnlineTaskLinkBound = true;
        button.onclick = function(event){{
            event.preventDefault();
            const form = button.form || (button.closest ? button.closest(".fls-online-task-link-form") : null);
            if(form) submitTaskLinkForm(form, event);
            return false;
        }};
    }});
}}

window.__FLS_BIND_ONLINE_TASK_LINK_FORMS__ = bindOnlineTaskLinkForms;
window.__FLS_SUBMIT_ONLINE_TASK_LINK_FORM__ = submitTaskLinkForm;
bindOnlineTaskLinkForms(document);

if(!window.__FLS_TASK_LINK_RECOVERY_BOUND__ && document.addEventListener){{
    window.__FLS_TASK_LINK_RECOVERY_BOUND__ = true;
    document.addEventListener("submit", function(event){{
        const form = event.target && event.target.closest ? event.target.closest(".fls-online-task-link-form") : null;
        if(form) return window.__FLS_SUBMIT_ONLINE_TASK_LINK_FORM__(form, event);
    }}, true);
}}

const pending = readPending();
if(pending) queryPending(pending);
}})();
</script>
"""

    return layout("在线脚本", "online_scripts", body)
