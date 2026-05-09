from ._common import *


@bp.route("/online-scripts")
def online_scripts_page():
    source = get_online_script_source()
    all_items = load_online_script_cache()

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

    refresh_running = ONLINE_REFRESH_STATE.get("running")
    refresh_message = ONLINE_REFRESH_STATE.get("message", "")
    refresh_error = ONLINE_REFRESH_STATE.get("error", "")
    refresh_updated_at = ONLINE_REFRESH_STATE.get("updated_at", "")
    refresh_log = ONLINE_REFRESH_STATE.get("log_file", "")

    proxy_options = proxy_select_options("")

    refresh_display = (
        "display:block;"
        if refresh_running or refresh_message or refresh_error
        else "display:none;"
    )

    task_total = sum(len(online_script_task_crons(x)) for x in filtered_items)
    install_total = sum(1 for x in filtered_items if script_has_install(x))

    refresh_status_text = (
        "正在后台拉取中，请稍候..."
        if refresh_running
        else h(refresh_message or refresh_error or "")
    )

    body = f"""
<div class="card">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap;">
        <div style="min-width:0;flex:1 1 360px;">
            <div class="card-title">在线脚本</div>
            <div class="help">
                默认读取本地缓存，不会因为脚本源网络问题卡住。<br>
                点击“刷新远程脚本源”后会后台拉取，页面不会变白，也不影响其它操作。<br>
                脚本源支持 <code>doc_link</code> 字段，可在面板内查看 Markdown 文档或网页文档。<br>
                <code>task_cron.var</code> 可预设任务变量，导入任务时会自动写入任务变量。
            </div>
            <div class="fls-source-code">{h(source)}</div>
        </div>

        <div class="action-row" style="justify-content:flex-end;">
            <form method="post" action="/online-scripts/refresh" class="action-row">
                <select name="proxy_id" style="width:auto;min-width:180px;">{proxy_options}</select>
                <button class="btn btn-primary" type="submit" id="onlineRefreshBtn">刷新远程脚本源</button>
            </form>

            <a class="btn btn-blue" href="{h(source)}" target="_blank">打开源地址</a>
            <a class="btn btn-orange" href="/online-scripts/source">脚本源 JSON</a>
            <a class="btn btn-gray" href="/config">修改源地址</a>
        </div>
    </div>
</div>

<form method="get">
<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>搜索在线脚本</label>
            <input name="q" value="{h(q)}" placeholder="脚本名 / ID / 保存名 / 链接 / 任务名 / 命令">
        </div>

        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="submit">搜索</button>
            <a class="btn btn-gray" href="/online-scripts">重置</a>
        </div>
    </div>

    <div class="help" style="margin-top:10px;">
        共 {len(all_items)} 个脚本，当前匹配 {total} 个，每页 {per_page} 个。
    </div>
</div>
</form>

<div class="fls-summary-grid">
    <div class="fls-summary-item">
        <div class="fls-summary-label">缓存脚本数</div>
        <div class="fls-summary-num">{len(items)}</div>
    </div>

    <div class="fls-summary-item">
        <div class="fls-summary-label">可导入任务</div>
        <div class="fls-summary-num">{task_total}</div>
    </div>

    <div class="fls-summary-item">
        <div class="fls-summary-label">有安装命令</div>
        <div class="fls-summary-num">{install_total}</div>
    </div>
</div>

{"<div class='card'><div class='help' style='color:#18a058;font-weight:800;'>" + h(msg) + "</div></div>" if msg else ""}
{"<div class='card'><div class='help' style='color:#dc2626;font-weight:800;'>" + h(err) + "</div></div>" if err else ""}

<div class="card" id="onlineRefreshStatusCard" style="{refresh_display}">
    <div class="card-title">脚本源刷新状态</div>
    <div class="help" id="onlineRefreshStatusText">
        {refresh_status_text}<br>
        更新时间：{h(refresh_updated_at or "-")}<br>
        日志：{h(refresh_log or "-")}
    </div>
</div>

<div class="card">
    <div class="card-title">脚本列表，本地缓存</div>
    <div class="help">
        缓存文件：{h(ONLINE_SCRIPT_CACHE_FILE)}
    </div>
    <br>

    <div class="fls-card-grid">
        {render_online_script_rows(items)}
    </div>
</div>
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
        const json = await res.json();

        const card = document.getElementById("onlineRefreshStatusCard");
        const text = document.getElementById("onlineRefreshStatusText");
        const btn = document.getElementById("onlineRefreshBtn");

        if(!card || !text) return;

        if(json.running){{
            window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ = true;
        }}

        if(json.running || json.message || json.error){{
            card.style.display = "block";

            let color = json.error ? "#dc2626" : "#18a058";
            let first = json.running ? "正在后台拉取中，请稍候..." : (json.message || json.error || "");

            text.innerHTML =
                "<span style='color:" + color + ";font-weight:900;'>" + escapeHtml(first) + "</span><br>" +
                "更新时间：" + escapeHtml(json.updated_at || "-") + "<br>" +
                "日志：" + escapeHtml(json.log_file || "-");

            if(btn){{
                btn.disabled = !!json.running;
                btn.textContent = json.running ? "正在拉取中..." : "刷新远程脚本源";
            }}
        }}else{{
            if(btn){{
                btn.disabled = false;
                btn.textContent = "刷新远程脚本源";
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
                btn.textContent = "刷新成功，正在更新列表...";
            }}

            setTimeout(function(){{
                location.href = "/online-scripts";
            }}, 800);

            return;
        }}

        if(json.running){{
            setTimeout(updateOnlineRefreshStatus, 2000);
        }}

    }} catch(e) {{}}
}}

updateOnlineRefreshStatus();
</script>
"""

    return layout("在线脚本", "online_scripts", body)

