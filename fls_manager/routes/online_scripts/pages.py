from ._common import *


@bp.route("/online-scripts")
def online_scripts_page():
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
    refresh_task_link_failures = refresh_state.get("task_link_failures") or []

    def refresh_task_link_actions(failures):
        if not failures:
            return ""
        rows = []
        for failure in failures:
            script_id = str(failure.get("script_id") or "").strip()
            if not script_id:
                continue
            action = url_for("online_scripts.online_scripts_pull_task_link", script_id=script_id)
            label = h(failure.get("name") or script_id)
            detail = h(failure.get("error") or "外部任务源加载失败")
            proxy_id = h(failure.get("proxy_id") or "")
            rows.append(
                f'<div class="help" style="margin-top:6px;">{label}：{detail} '
                f'<form class="inline-form" method="post" action="{h(action)}">'
                f'<input type="hidden" name="request_id" value="{uuid.uuid4().hex}">'
                f'<input type="hidden" name="proxy_id" value="{proxy_id}">'
                '<button class="btn btn-orange" type="submit">重新同步外部任务源</button>'
                '</form></div>'
            )
        return "".join(rows)

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
<form method="post" action="/online-scripts/refresh" class="action-row">
    <select name="proxy_id" aria-label="刷新脚本源使用的代理" style="width:auto;min-width:180px;">{proxy_options}</select>
    <button class="btn btn-primary" type="submit" id="onlineRefreshBtn">同步在线脚本源</button>
</form>

<a class="btn btn-blue" href="{h(source)}" target="_blank">打开源地址</a>
<a class="btn btn-orange" href="/online-scripts/source">在线脚本源数据</a>
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
{f'<div class="section"><a class="btn btn-orange" href="/online-scripts/source">检查在线脚本源数据</a></div>' if cache_error else ''}

<section class="section fls-section" id="online-status">
<div class="card" id="onlineRefreshStatusCard" style="{refresh_display}">
    <h2 class="section-title">在线脚本源同步状态</h2>
    <div class="help" id="onlineRefreshStatusText">
        {refresh_status_text}<br>
        更新时间：{h(refresh_updated_at or "-")}<br>
        日志：{h(refresh_log or "-")}
    </div>
    <div id="onlineRefreshTaskLinkFailures">{refresh_task_link_actions(refresh_task_link_failures)}</div>
    <a id="onlineRefreshLogin" class="btn btn-gray" href="/login?next=%2Fonline-scripts" hidden>重新登录</a>
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

function escapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c];
    }});
}}

async function updateOnlineRefreshStatus(){{
    try {{
        const res = await fetch("/api/online-scripts/refresh-status", {{cache:"no-store"}});
        const json = await flsReadJsonResponse(res);

        const card = document.getElementById("onlineRefreshStatusCard");
        const text = document.getElementById("onlineRefreshStatusText");
        const btn = document.getElementById("onlineRefreshBtn");
        const login = document.getElementById("onlineRefreshLogin");
        const failures = document.getElementById("onlineRefreshTaskLinkFailures");

        if(!card || !text) return;
        if(login) login.hidden = true;

        const taskFailures = Array.isArray(json.task_link_failures) ? json.task_link_failures : [];
        if(failures) {{
            failures.innerHTML = taskFailures.map(function(item) {{
                const id = String(item && item.script_id || "").trim();
                if(!id) return "";
                const name = escapeHtml(item.name || id);
                const detail = escapeHtml(item.error || "外部任务源加载失败");
                const href = "/online-scripts/task-link/" + encodeURIComponent(id);
                const proxy = escapeHtml(item.proxy_id || "");
                const csrf = typeof flsGetCsrfToken === "function" ? escapeHtml(flsGetCsrfToken()) : "";
                const requestId = (typeof crypto !== "undefined" && crypto.getRandomValues)
                    ? Array.from(crypto.getRandomValues(new Uint8Array(16)), function(value){{return value.toString(16).padStart(2, "0");}}).join("")
                    : Array.from({{length:32}}, function(){{return Math.floor(Math.random() * 16).toString(16);}}).join("");
                return "<div class='help' style='margin-top:6px;'>" + name + "：" + detail +
                    " <" + "form class='inline-form' method='post' action='" + escapeHtml(href) + "'>" +
                    "<input type='hidden' name='csrf_token' value='" + csrf + "'>" +
                    "<input type='hidden' name='request_id' value='" + requestId + "'>" +
                    "<input type='hidden' name='proxy_id' value='" + proxy + "'>" +
                    "<button class='btn btn-orange' type='submit'>重新同步外部任务源</button></form></div>";
            }}).join("");
        }}

        if(json.running){{
            window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ = true;
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

            setTimeout(function(){{
                location.href = "/online-scripts";
            }}, 800);

            return;
        }}

        if(json.running){{
            setTimeout(updateOnlineRefreshStatus, 2000);
        }}

    }} catch(e) {{
        const card = document.getElementById("onlineRefreshStatusCard");
        const text = document.getElementById("onlineRefreshStatusText");
        const btn = document.getElementById("onlineRefreshBtn");
        const login = document.getElementById("onlineRefreshLogin");
        const status = e && e.status ? "HTTP " + e.status : "请求失败";
        const detail = e && e.message ? e.message : String(e || "未知错误");
        const denied = e && (e.status === 401 || e.status === 403);
        const next = denied
            ? "登录状态已失效，请重新登录后重试"
            : "确认面板服务和网络后点击重新同步";
        if(card) card.style.display = "block";
        const failures = document.getElementById("onlineRefreshTaskLinkFailures");
        if(failures) failures.innerHTML = "";
        if(text) text.innerHTML =
            "<span style='color:#dc2626;font-weight:900;'>同步状态读取失败（" +
            escapeHtml(status) + "）：" + escapeHtml(detail) +
            "。下一步：" + escapeHtml(next) + "</span>";
        if(btn) {{
            btn.disabled = false;
            btn.textContent = "重新同步在线脚本源";
        }}
        if(login) {{
            login.hidden = !denied;
            if(denied) login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        }}
    }}
}}

updateOnlineRefreshStatus();
</script>
"""

    return layout("在线脚本", "online_scripts", body)
