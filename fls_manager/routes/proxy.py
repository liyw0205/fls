import uuid
from flask import Blueprint, request, redirect, url_for, abort, jsonify
from ..models import load_proxies, save_proxies
from ..utils import h, now_str
from ..ui.layout import layout
from ..proxy import (
    proxy_from_form,
    build_proxy_url,
    test_proxy_object,
    quality_proxy_object,
    parse_quality_urls,
    get_proxy_for_test,
)

bp = Blueprint("proxy", __name__)


def proxy_form(proxy=None, mode="new"):
    if proxy is None:
        proxy = {
            "id": "",
            "name": "",
            "type": "socks5",
            "host": "",
            "port": "",
            "username": "",
            "password": "",
            "url": "",
            "enabled": True,
        }

    checked = "checked" if proxy.get("enabled", True) else ""

    def selected(v):
        return "selected" if proxy.get("type") == v else ""

    action = "/proxy/new" if mode == "new" else f"/proxy/edit/{proxy.get('id')}"

    body = f"""
<form method="post" id="proxyForm" action="{h(action)}">
<div class="card">
    <div class="card-title">{"新增代理" if mode == "new" else "编辑代理"}</div>

    <input type="hidden" name="id" value="{h(proxy.get('id', ''))}">

    <div class="form-grid">
        <div class="form-item">
            <label>代理名称</label>
            <input name="name" value="{h(proxy.get('name', ''))}" placeholder="例如：本地 SOCKS5">
        </div>

        <div class="form-item">
            <label>代理类型</label>
            <select name="type" id="proxyType" onchange="toggleGithubProxyBox()">
                <option value="socks5" {selected("socks5")}>SOCKS5</option>
                <option value="http" {selected("http")}>HTTP</option>
                <option value="https" {selected("https")}>HTTPS</option>
                <option value="github" {selected("github")}>GitHub 代理</option>
            </select>
        </div>
    </div>

    <br>

    <div class="form-grid" id="normalProxyBox">
        <div class="form-item">
            <label>Host</label>
            <input name="host" value="{h(proxy.get('host', ''))}" placeholder="127.0.0.1">
        </div>

        <div class="form-item">
            <label>Port</label>
            <input name="port" value="{h(proxy.get('port', ''))}" placeholder="1080">
        </div>

        <div class="form-item">
            <label>用户名，可空</label>
            <input name="username" value="{h(proxy.get('username', ''))}">
        </div>

        <div class="form-item">
            <label>密码，可空</label>
            <input name="password" value="{h(proxy.get('password', ''))}">
        </div>
    </div>

    <br>

    <div class="form-item" id="githubProxyBox" style="display:none;">
        <label>GitHub 代理地址</label>
        <input name="url" value="{h(proxy.get('url', ''))}" placeholder="例如：https://gh-proxy.com/">
        <div class="help">会把 GitHub URL 转为：代理地址/原始URL。</div>
    </div>

    <br>

    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用此代理
    </label>
</div>

<div class="card">
    <div class="form-item">
        <label>自定义质量检测地址，可空</label>
        <textarea name="quality_urls" style="min-height:110px;" placeholder="每行一个，例如：
https://www.baidu.com
https://www.github.com
https://raw.githubusercontent.com"></textarea>
    </div>

    <br>

    <button class="btn btn-primary" type="submit">保存代理</button>
    <button class="btn btn-blue" type="button" onclick="testProxyRealtime()">测试</button>
    <button class="btn btn-orange" type="button" onclick="qualityProxyRealtime()">质量检测</button>
    <a class="btn btn-gray" href="/proxy">返回</a>
</div>
</form>

<div class="card" id="proxyRealtimeResult" style="display:none;">
    <div class="card-title">实时结果</div>
    <div class="help" id="proxyRealtimeText">等待操作</div>
</div>

<script>
function toggleGithubProxyBox(){{
    const type = document.getElementById("proxyType").value;
    document.getElementById("githubProxyBox").style.display = type === "github" ? "block" : "none";
    document.getElementById("normalProxyBox").style.display = type === "github" ? "none" : "grid";
}}

function escapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c];
    }});
}}

function showProxyResult(html){{
    document.getElementById("proxyRealtimeResult").style.display = "block";
    document.getElementById("proxyRealtimeText").innerHTML = html;
}}

async function testProxyRealtime(){{
    const form = document.getElementById("proxyForm");
    const data = new FormData(form);
    showProxyResult("正在测试代理，请稍候...");

    try {{
        const res = await fetch("/api/proxy/test-form", {{
            method: "POST",
            body: data,
            headers: {{"X-Requested-With":"XMLHttpRequest"}}
        }});
        const json = await res.json();

        if(json.ok){{
            showProxyResult(
                "状态：<b style='color:#18a058'>成功</b><br>" +
                "状态码：" + json.status_code + "<br>" +
                "耗时：" + json.elapsed_ms + " ms"
            );
        }} else {{
            showProxyResult("状态：<b style='color:#dc2626'>失败</b><br>错误：" + escapeHtml(json.error || "未知错误"));
        }}
    }} catch(e) {{
        showProxyResult("请求失败：" + escapeHtml(String(e)));
    }}
}}

async function qualityProxyRealtime(){{
    const form = document.getElementById("proxyForm");
    const data = new FormData(form);
    showProxyResult("正在进行质量检测，请稍候...");

    try {{
        const res = await fetch("/api/proxy/quality-form", {{
            method: "POST",
            body: data,
            headers: {{"X-Requested-With":"XMLHttpRequest"}}
        }});
        const json = await res.json();

        if(!json.ok){{
            showProxyResult("检测失败：" + escapeHtml(json.error || "未知错误"));
            return;
        }}

        let html = "<div class='table-wrap'><table><thead><tr>" +
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

toggleGithubProxyBox();
</script>
"""
    return layout("代理配置", "proxy", body)


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
        <a class="btn btn-red" href="/proxy/delete/{h(proxy_id)}" onclick="return confirm('确定删除代理吗？')">删除</a>
    </td>
</tr>
"""

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">代理管理</div>
            <div class="help">
                代理可用于任务运行、脚本拉取、GitHub 加速。<br>
                禁用代理后，任务编辑页不再显示，已选择该代理的任务运行时会自动跳过。
            </div>
        </div>
        <a class="btn btn-primary" href="/proxy/new">新增代理</a>
    </div>
</div>

<div class="card">
    <div class="card-title">代理列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>名称</th>
                    <th>类型</th>
                    <th>地址</th>
                    <th>状态</th>
                    <th>创建时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>

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


@bp.route("/proxy/toggle/<proxy_id>")
def proxy_toggle(proxy_id):
    proxies = load_proxies()

    for p in proxies:
        if p.get("id") == proxy_id:
            p["enabled"] = not p.get("enabled", True)
            p["updated_at"] = now_str()
            break

    save_proxies(proxies)
    return redirect(url_for("proxy.proxy_page"))


@bp.route("/proxy/delete/<proxy_id>")
def proxy_delete(proxy_id):
    proxies = [p for p in load_proxies() if p.get("id") != proxy_id]
    save_proxies(proxies)
    return redirect(url_for("proxy.proxy_page"))


@bp.route("/api/proxy/test-form", methods=["POST"])
def api_proxy_test_form():
    proxy = proxy_from_form(request.form)

    try:
        result = test_proxy_object(proxy)
        return jsonify({
            "ok": True,
            "status_code": result["status_code"],
            "elapsed_ms": result["elapsed_ms"],
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        })


@bp.route("/api/proxy/quality-form", methods=["POST"])
def api_proxy_quality_form():
    proxy = proxy_from_form(request.form)

    try:
        items = quality_proxy_object(
            proxy,
            parse_quality_urls(request.form.get("quality_urls"))
        )
        return jsonify({
            "ok": True,
            "items": items,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        })


@bp.route("/api/proxy/test/<proxy_id>")
def api_proxy_test_saved(proxy_id):
    proxy = get_proxy_for_test(proxy_id)

    if not proxy:
        return jsonify({
            "ok": False,
            "name": "",
            "error": "代理不存在",
        }), 404

    try:
        result = test_proxy_object(proxy)
        return jsonify({
            "ok": True,
            "name": proxy.get("name", ""),
            "status_code": result["status_code"],
            "elapsed_ms": result["elapsed_ms"],
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "name": proxy.get("name", ""),
            "error": str(e),
        })


@bp.route("/api/proxy/quality/<proxy_id>")
def api_proxy_quality_saved(proxy_id):
    proxy = get_proxy_for_test(proxy_id)

    if not proxy:
        return jsonify({
            "ok": False,
            "name": "",
            "error": "代理不存在",
        }), 404

    try:
        items = quality_proxy_object(
            proxy,
            parse_quality_urls(request.args.get("urls"))
        )
        return jsonify({
            "ok": True,
            "name": proxy.get("name", ""),
            "items": items,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "name": proxy.get("name", ""),
            "error": str(e),
        })
