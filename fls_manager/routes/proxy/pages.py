from ._common import *
from ...ui.components import page_header_card, table_card


@bp.route("/proxy")
def proxy_page():
    proxies = load_proxies()
    rows = ""

    if not proxies:
        rows = '<tr><td colspan="6">暂无代理，请点击新增代理</td></tr>'
    else:
        for p in proxies:
            proxy_id = p.get("id")
            ptype = p.get("type", "")
            addr = p.get("url", "") if ptype == "github" else f'{p.get("host", "")}:{p.get("port", "")}'
            badge = '<span class="badge green">启用</span>' if p.get("enabled", True) else '<span class="badge gray">禁用</span>'
            toggle_text = "禁用" if p.get("enabled", True) else "启用"
            toggle_class = "btn-gray" if p.get("enabled", True) else "btn-primary"

            rows += f"""
<tr>
    <td>{h(p.get("name", ""))}</td>
    <td>{h(ptype)}</td>
    <td>{h(addr)}</td>
    <td>{badge}</td>
    <td>{h(p.get("created_at", "-"))}</td>
    <td>
        <a class="btn btn-blue" href="/proxy/edit/{h(proxy_id)}">编辑</a>
        <button class="btn btn-orange" type="button" onclick="flsProxyTest('{h(proxy_id)}')">测试</button>
        <button class="btn btn-primary" type="button" onclick="flsProxyQuality('{h(proxy_id)}')">质量检测</button>
        <a class="btn {toggle_class}" href="/proxy/toggle/{h(proxy_id)}">{toggle_text}</a>
        <form class="inline-form" method="post" action="/proxy/delete/{h(proxy_id)}">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定删除代理吗？')">删除</button>
        </form>
    </td>
</tr>
"""

    header = page_header_card(
        "代理管理",
        """
                代理可用于任务运行、脚本拉取、GitHub 加速。<br>
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
{table}

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
    document.getElementById("proxyResultCard").style.display = "block";
    document.getElementById("proxyResultText").innerHTML = html;
}}

async function flsProxyTest(proxyId){{
    showProxyResult("正在测试代理，请稍候...");
    try {{
        const res = await fetch("/api/proxy/test/" + encodeURIComponent(proxyId), {{
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
            showProxyResult("失败：" + escapeHtml(json.error || "未知错误"));
        }}
    }} catch(e) {{
        showProxyResult("请求失败：" + escapeHtml(String(e)));
    }}
}}

async function flsProxyQuality(proxyId){{
    showProxyResult("正在质量检测，请稍候...");
    try {{
        const res = await fetch("/api/proxy/quality/" + encodeURIComponent(proxyId), {{
            headers: {{"X-Requested-With":"XMLHttpRequest"}}
        }});
        const json = await res.json();
        if(!json.ok){{
            showProxyResult("失败：" + escapeHtml(json.error || "未知错误"));
            return;
        }}

        let html = "代理：<b>" + escapeHtml(json.name || "") + "</b><br><br>";
        html += "<div class='table-wrap'><table><thead><tr>" +
            "<th>测试地址</th><th>结果</th><th>状态码</th><th>耗时 / 错误</th>" +
            "</tr></thead><tbody>";

        for(const item of json.items){{
            html += "<tr>" +
                "<td>" + escapeHtml(item.url) + "</td>" +
                "<td>" + (item.ok ? "<span class='badge green'>成功</span>" : "<span class='badge red'>失败</span>") + "</td>" +
                "<td>" + escapeHtml(String(item.status_code)) + "</td>" +
                "<td>" + escapeHtml(String(item.elapsed)) + "</td>" +
                "</tr>";
        }}

        html += "</tbody></table></div>";
        showProxyResult(html);
    }} catch(e) {{
        showProxyResult("请求失败：" + escapeHtml(String(e)));
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
