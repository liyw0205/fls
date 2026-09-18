from .bp import bp
import uuid
from flask import request, redirect, url_for, abort, jsonify
from ...models import load_proxies, save_proxies
from ...utils import h, now_str
from ...ui.layout import layout
from ...ui.components import page_header_card
from ...proxy import (
    proxy_from_form,
    build_proxy_url,
    test_proxy_object,
    quality_proxy_object,
    parse_quality_urls,
    get_proxy_for_test,
)

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
    title = "新增代理" if mode == "new" else "编辑代理"
    help_html = (
        "代理可用于任务运行、脚本导入和 GitHub 加速。保存前可先使用测试或质量检测。"
        if mode == "new"
        else (
            f'当前代理：<b>{h(proxy.get("name", "") or proxy.get("id", ""))}</b>。'
            "保存前可先使用测试或质量检测。"
        )
    )
    header = page_header_card(title, help_html)

    body = f"""
<form method="post" id="proxyForm" action="{h(action)}">
{header}
<section class="section fls-form-section">
    <h2 class="section-title">代理连接设置</h2>
    <input type="hidden" name="id" value="{h(proxy.get('id', ''))}">

    <div class="form-grid">
        <div class="form-item">
            <label>代理名称</label>
            <input name="name" value="{h(proxy.get('name', ''))}" placeholder="例如：本地 SOCKS5" aria-label="代理名称">
        </div>

        <div class="form-item">
            <label>代理类型</label>
            <select name="type" id="proxyType" onchange="toggleGithubProxyBox()" aria-label="代理类型">
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
            <input name="host" value="{h(proxy.get('host', ''))}" placeholder="127.0.0.1" aria-label="代理 Host">
        </div>

        <div class="form-item">
            <label>Port</label>
            <input name="port" value="{h(proxy.get('port', ''))}" placeholder="1080" aria-label="代理 Port">
        </div>

        <div class="form-item">
            <label>用户名，可空</label>
            <input name="username" value="{h(proxy.get('username', ''))}" aria-label="代理用户名">
        </div>

        <div class="form-item">
            <label>密码，可空</label>
            <input name="password" type="password" value="{h(proxy.get('password', ''))}" autocomplete="new-password" aria-label="代理密码">
        </div>
    </div>

    <br>

    <div class="form-item" id="githubProxyBox" style="display:none;">
        <label>GitHub 代理地址</label>
        <input name="url" value="{h(proxy.get('url', ''))}" placeholder="例如：https://gh-proxy.com/" aria-label="GitHub 代理地址">
        <div class="help">会把 GitHub URL 转为：代理地址/原始URL。</div>
    </div>

    <br>

    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;" aria-label="启用此代理">
        启用此代理
    </label>
</section>

<section class="section fls-form-section fls-save-section">
    <h2 class="section-title">质量检测与保存</h2>
    <div class="form-item">
        <label>自定义质量检测地址，可空</label>
        <textarea name="quality_urls" style="min-height:110px;" placeholder="每行一个，例如：
https://www.baidu.com
https://www.github.com
https://raw.githubusercontent.com" aria-label="自定义质量检测地址"></textarea>
    </div>

    <br>

    <div class="row-actions" aria-label="代理设置操作">
        <div class="row-actions-primary">
            <button class="btn btn-primary" type="submit">保存代理设置</button>
        </div>
        <div class="row-actions-secondary">
            <button class="btn btn-blue" type="button" onclick="testProxyRealtime(this)">测试代理连接</button>
            <button class="btn btn-orange" type="button" onclick="qualityProxyRealtime(this)">检测代理质量</button>
            <a class="btn btn-gray" href="/proxy">返回代理列表</a>
        </div>
    </div>
</section>
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

async function testProxyRealtime(source){{
    if(!flsMarkButtonBusy(source, "测试中...")) return;
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
            showProxyResult("状态：<b style='color:#dc2626'>失败</b><br>" + escapeHtml(flsActionFailure("错误：" + (json.error || "未知错误"), "检查代理配置后重试")));
        }}
    }} catch(e) {{
        showProxyResult(escapeHtml(flsActionFailure("请求失败：" + String(e), "检查服务状态后重试")));
    }} finally {{
        flsRestoreButton(source);
    }}
}}

async function qualityProxyRealtime(source){{
    if(!flsMarkButtonBusy(source, "检测中...")) return;
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
            showProxyResult(escapeHtml(flsActionFailure("检测失败：" + (json.error || "未知错误"), "检查代理配置后重试")));
            return;
        }}

        let html = "<div class='table-wrap'><table><thead><tr>" +
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

toggleGithubProxyBox();
</script>
"""
    return layout("代理配置", "proxy", body)
