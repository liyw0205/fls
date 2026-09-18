from ._common import *
from ...ui.components import empty_state, empty_table_row, page_header_card, table_card


@bp.route("/proxy")
def proxy_page():
    proxies = load_proxies()
    rows = ""
    mobile_cards = ""

    if not proxies:
        empty = empty_state("暂无代理，请创建第一个代理。", '<a class="btn btn-primary" href="/proxy/new">新增代理</a>')
        rows = empty_table_row(6, "暂无代理，请创建第一个代理。", '<a class="btn btn-primary" href="/proxy/new">新增代理</a>')
        mobile_cards = empty
    else:
        for p in proxies:
            proxy_id = p.get("id")
            proxy_name = p.get("name", "")
            ptype = p.get("type", "")
            addr = p.get("url", "") if ptype == "github" else f'{p.get("host", "")}:{p.get("port", "")}'
            enabled = bool(p.get("enabled", True))
            badge = (
                '<span class="badge green status-badge status-enabled" role="status">启用</span>'
                if enabled else
                '<span class="badge gray status-badge status-disabled" role="status">停用</span>'
            )
            toggle_text = "停用代理" if p.get("enabled", True) else "启用代理"
            toggle_class = "btn-gray" if p.get("enabled", True) else "btn-primary"

            primary_action = f'''<button class="btn btn-primary" type="button" onclick="flsProxyTest('{h(proxy_id)}', this)">测试代理连接</button>'''
            secondary_actions = f'''
        <a class="btn btn-blue" href="/proxy/edit/{h(proxy_id)}">编辑代理设置</a>
        <button class="btn btn-blue" type="button" onclick="flsProxyQuality('{h(proxy_id)}', this)">检测代理质量</button>
        <form class="inline-form" method="post" action="/proxy/toggle/{h(proxy_id)}">
            <button class="btn {toggle_class}" type="submit">{toggle_text}</button>
        </form>
'''
            danger_actions = f'''
        <form class="inline-form" method="post" action="/proxy/delete/{h(proxy_id)}">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定删除代理吗？')">删除代理</button>
        </form>
'''
            action_html = f'''
<div class="row-actions" aria-label="代理 {h(proxy_name)} 行操作">
    <div class="row-actions-primary">{primary_action}</div>
    <div class="row-actions-secondary">{secondary_actions}</div>
    <div class="row-actions-danger">{danger_actions}</div>
</div>
'''

            rows += f"""
<tr>
    <td><b>{h(proxy_name)}</b></td>
    <td>{h(ptype)}</td>
    <td>{h(addr)}</td>
    <td>{badge}</td>
    <td>{h(p.get("created_at", "-"))}</td>
    <td>{action_html}</td>
</tr>
"""

            mobile_cards += f"""
<article class="fls-fold-card mobile-list-item proxy-mobile-item" data-proxy-id="{h(proxy_id)}">
    <div class="fls-card-head">
        <div class="fls-card-main">
            <div class="fls-card-title-main">{h(proxy_name)}</div>
            <div class="fls-card-sub">{h(ptype)}：{h(addr)}</div>
        </div>
        <div class="fls-card-badges">{badge}</div>
    </div>
    <div class="fls-card-actions fls-card-primary-action">
        <button class="btn btn-primary" type="button" onclick="flsProxyTest('{h(proxy_id)}', this)">测试代理连接</button>
    </div>
    <details class="detail-disclosure">
        <summary>查看代理详情</summary>
        <div class="fls-card-body">
            <div class="fls-info-grid">
                <div class="fls-info-item"><div class="fls-info-label">代理类型</div><div class="fls-info-value">{h(ptype)}</div></div>
                <div class="fls-info-item"><div class="fls-info-label">代理地址</div><div class="fls-info-value">{h(addr)}</div></div>
                <div class="fls-info-item"><div class="fls-info-label">创建时间</div><div class="fls-info-value">{h(p.get("created_at", "-"))}</div></div>
            </div>
        </div>
    </details>
    <div class="fls-card-actions">
        <div class="row-actions" aria-label="代理 {h(proxy_name)} 行操作">
            <div class="row-actions-secondary">{secondary_actions}</div>
            <div class="row-actions-danger">{danger_actions}</div>
        </div>
    </div>
</article>
"""

    header = page_header_card(
        "代理管理",
        """
                代理可用于任务运行、脚本导入和 GitHub 加速。<br>
                禁用代理后，任务编辑页不再显示，已选择该代理的任务运行时会自动跳过。
            """,
        '<a class="btn btn-primary" href="/proxy/new">新增代理</a>',
    )

    table = table_card(
        "代理列表",
        ("名称", "类型", "地址", "状态", "创建时间", "操作"),
        rows,
    )

    body = f"""
{header}
<div id="proxyDesktopTable">{table}</div>
<section class="section" id="proxyMobileList">
    <h2 class="section-title">代理列表</h2>
    <div class="mobile-list">{mobile_cards}</div>
</section>

<div class="card" id="proxyResultCard" style="display:none;">
    <div class="card-title">代理检测结果</div>
    <div class="help" id="proxyResultText">等待操作</div>
</div>

<script>
function escapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c];
    }});
}}

function showProxyResult(html){{
    if(typeof flsShowFloatingPanel === "function"){{
        flsShowFloatingPanel("proxyResultCard", html);
        return;
    }}
    document.getElementById("proxyResultCard").style.display = "block";
    document.getElementById("proxyResultText").innerHTML = html;
}}

async function flsProxyTest(proxyId, source){{
    if(!flsMarkButtonBusy(source, "测试中...")) return;
    showProxyResult("正在测试代理，请稍候...");
    try {{
        const res = await fetch("/api/proxy/test/" + encodeURIComponent(proxyId), {{
            method: "POST",
            headers: {{"X-Requested-With":"XMLHttpRequest"}}
        }});
        const json = await res.json();
        if(json.ok){{
            showProxyResult(
                "代理：<b>" + escapeHtml(json.name || "") + "</b><br>" +
                "状态：<b style='color:#18a058'>成功</b><br>" +
                "状态码：" + escapeHtml(String(json.status_code)) + "<br>" +
                "耗时：" + escapeHtml(String(json.elapsed_ms)) + " ms"
            );
        }} else {{
            showProxyResult(escapeHtml(flsActionFailure("失败：" + (json.error || "未知错误"), "检查代理配置后重试")));
        }}
    }} catch(e) {{
        showProxyResult(escapeHtml(flsActionFailure("请求失败：" + String(e), "检查服务状态后重试")));
    }} finally {{
        flsRestoreButton(source);
    }}
}}

async function flsProxyQuality(proxyId, source){{
    if(!flsMarkButtonBusy(source, "检测中...")) return;
    showProxyResult("正在质量检测，请稍候...");
    try {{
        const res = await fetch("/api/proxy/quality/" + encodeURIComponent(proxyId), {{
            method: "POST",
            headers: {{"X-Requested-With":"XMLHttpRequest"}}
        }});
        const json = await res.json();
        if(!json.ok){{
            showProxyResult(escapeHtml(flsActionFailure("失败：" + (json.error || "未知错误"), "检查代理配置后重试")));
            return;
        }}

        let html = "代理：<b>" + escapeHtml(json.name || "") + "</b><br><br>";
        html += "<div class='table-wrap'><table><thead><tr>" +
            "<th>测试地址</th><th>结果</th><th>状态码</th><th>耗时 / 错误</th>" +
            "</tr></thead><tbody>";

        for(const item of json.items){{
            html += "<tr>" +
                "<td>" + escapeHtml(item.url) + "</td>" +
                "<td>" + (item.ok ? "<span class='badge green status-badge status-success' role='status'>成功</span>" : "<span class='badge red status-badge status-error' role='status'>失败</span>") + "</td>" +
                "<td>" + escapeHtml(String(item.status_code)) + "</td>" +
                "<td>" + escapeHtml(String(item.elapsed)) + "</td>" +
                "</tr>";
        }}

        html += "</tbody></table></div>";
        showProxyResult(html);
    }} catch(e) {{
        showProxyResult(escapeHtml(flsActionFailure("请求失败：" + String(e), "检查服务状态后重试")));
    }} finally {{
        flsRestoreButton(source);
    }}
}}
</script>
"""
    return layout("代理管理", "proxy", body)


@bp.route("/proxy/new", methods=["GET", "POST"])
def proxy_new():
    if request.method == "POST":
        proxies = load_proxies()
        p = proxy_from_form(request.form)
        p["id"] = uuid.uuid4().hex
        p["created_at"] = now_str()
        p["updated_at"] = now_str()
        proxies.append(p)
        save_proxies(proxies)
        return redirect(url_for("proxy.proxy_page"))

    return proxy_form(mode="new")


@bp.route("/proxy/edit/<proxy_id>", methods=["GET", "POST"])
def proxy_edit(proxy_id):
    proxies = load_proxies()
    proxy = None

    for p in proxies:
        if p.get("id") == proxy_id:
            proxy = p
            break

    if not proxy:
        abort(404)

    if request.method == "POST":
        new_p = proxy_from_form(request.form)
        proxy.update(new_p)
        proxy["updated_at"] = now_str()
        save_proxies(proxies)
        return redirect(url_for("proxy.proxy_page"))

    return proxy_form(proxy, mode="edit")
