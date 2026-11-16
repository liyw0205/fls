from ._common import *
from urllib.parse import urlencode
from ...ui.components import empty_state, empty_table_row, page_header_card, table_card


def _diagnostic_status_badge(status):
    labels = {
        "running": ("检测中", "blue"),
        "succeeded": ("成功", "green"),
        "failed": ("失败", "red"),
        "unknown": ("结果未知", "orange"),
    }
    label, color = labels.get(status, ("结果未知", "orange"))
    return f'<span class="badge {color} status-badge status-{h(status)}" role="status">{label}</span>'


def _diagnostic_items_text(items):
    parts = []
    for item in items if isinstance(items, list) else []:
        result = "成功" if item.get("ok") else "失败"
        parts.append(f'{item.get("url", "-")}：{result}，{item.get("elapsed", "-")}')
    return "；".join(parts) or "-"


def proxy_diagnostics_page(notice="", error=False):
    limit, status, proxy_id = diagnostic_filters(request.args)
    filter_query = urlencode({"status": status, "proxy_id": proxy_id, "limit": limit})
    records = load_proxy_diagnostics(limit=limit, status=status, proxy_id=proxy_id)
    rows = ""
    for record in records:
        retry = ""
        if record.get("status") in ("failed", "unknown"):
            retry = f'''<form class="inline-form" method="post" action="/proxy/diagnostics/retry/{h(record.get("id"))}?{h(filter_query)}">
                <button class="btn btn-orange" type="submit">重新检测</button>
            </form>'''
        else:
            retry = '<span class="muted">-</span>'
        kind = "连接测试" if record.get("kind") == "test" else "质量检测"
        message = record.get("error") or record.get("message") or "-"
        rows += f"""
<tr>
    <td>{h(record.get("updated_at") or record.get("created_at") or "-")}</td>
    <td>{h(record.get("proxy_name") or record.get("proxy_id") or "-")}</td>
    <td>{kind}</td>
    <td>{_diagnostic_status_badge(record.get("status"))}</td>
    <td>{h(record.get("attempt", 1))}</td>
    <td>{h(_diagnostic_items_text(record.get("items")))}</td>
    <td>{h(message)}</td>
    <td>{retry}</td>
</tr>
"""
    if not rows:
        rows = empty_table_row(8, "暂无代理诊断记录", '<a class="btn btn-gray" href="/proxy">返回代理管理</a>')

    proxies = load_proxies()
    options = "".join(
        f'<option value="{h(item.get("id"))}" {"selected" if item.get("id") == proxy_id else ""}>{h(item.get("name") or item.get("id"))}</option>'
        for item in proxies
    )
    if proxy_id and not any(item.get("id") == proxy_id for item in proxies):
        options += f'<option value="{h(proxy_id)}" selected>{h(proxy_id)}（已删除）</option>'
    status_options = "".join(
        f'<option value="{h(value)}" {"selected" if value == status else ""}>{h(label)}</option>'
        for value, label in (("", "全部状态"), ("succeeded", "成功"), ("failed", "失败"), ("unknown", "结果未知"), ("running", "检测中"))
    )
    notice_html = f'<section class="section" role="alert" data-diagnostic-notice><p>{h(notice)}</p></section>' if notice else ""
    header = page_header_card(
        "代理诊断记录",
        "查看保存代理的连接测试和质量检测结果；失败或结果未知的记录可以重新检测。",
        '<a class="btn btn-gray" href="/proxy">返回代理管理</a>',
    )
    filters = f"""
<form method="get" action="/proxy/diagnostics">
<section class="section fls-form-section">
    <div class="form-grid">
        <div class="form-item"><label for="diagnostic-proxy">代理</label><select id="diagnostic-proxy" name="proxy_id"><option value="">全部代理</option>{options}</select></div>
        <div class="form-item"><label for="diagnostic-status">状态</label><select id="diagnostic-status" name="status">{status_options}</select></div>
        <div class="form-item"><label for="diagnostic-limit">显示条数</label><input id="diagnostic-limit" name="limit" type="number" min="1" max="100" value="{limit}"></div>
    </div>
    <br><button class="btn btn-blue" type="submit">筛选记录</button>
</section>
</form>
"""
    table = table_card(
        "最近诊断",
        ("时间", "代理", "类型", "状态", "尝试", "分项结果", "说明", "操作"),
        rows,
    )
    return layout("代理诊断记录", "proxy", f"{header}{notice_html}{filters}{table}")


def _diagnostic_page_error(message):
    body = page_header_card("代理诊断记录", "记录未读取或操作未确认，请检查诊断记录文件和权限后重新读取。")
    body += f'<section class="section" role="alert" data-diagnostic-notice><p>{h(message)}</p><a class="btn btn-blue" href="/proxy/diagnostics">重新读取诊断记录</a><a class="btn btn-gray" href="/proxy">返回代理管理</a></section>'
    return layout("代理诊断记录", "proxy", body)


def _diagnostic_page_response(notice="", error=False, status=200):
    try:
        return proxy_diagnostics_page(notice, error), status
    except ValueError as e:
        return _diagnostic_page_error(str(e)), 400
    except Exception as e:
        return _diagnostic_page_error(f"{notice}；记录读取失败：{e}" if notice else f"记录读取失败：{e}"), 500


@bp.route("/proxy/diagnostics")
def proxy_diagnostics():
    return _diagnostic_page_response()


@bp.route("/proxy/diagnostics/retry/<diagnostic_id>", methods=["POST"])
def proxy_diagnostics_retry(diagnostic_id):
    try:
        diagnostic_filters(request.args)
    except ValueError as e:
        return _diagnostic_page_error(str(e)), 400
    try:
        ok, message, _row = retry_proxy_diagnostic(diagnostic_id)
    except Exception as exc:
        return _diagnostic_page_response(f"重新检测失败：{exc}", True, 500)
    if ok:
        return _diagnostic_page_response(f"重新检测成功：{message}")
    if message == "代理诊断不存在":
        return _diagnostic_page_response(message, True, 404)
    if message in ("代理诊断正在检测", "代理诊断已成功，无需重试"):
        return _diagnostic_page_response(message, True, 409)
    return _diagnostic_page_response(f"重新检测失败：{message}", True, 500)


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
            mobile_toggle_action = f'''
        <form class="inline-form" method="post" action="/proxy/toggle/{h(proxy_id)}">
            <button class="btn {toggle_class}" type="submit">{toggle_text}</button>
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
    <div class="proxy-mobile-content">
      <div class="fls-card-head">
        <div class="fls-card-main">
            <div class="fls-card-title-main">{h(proxy_name)}</div>
            <div class="fls-card-sub">{h(ptype)}：{h(addr)}</div>
        </div>
        <div class="fls-card-badges">{badge}</div>
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
    </div>
    <div class="proxy-mobile-action-rail">
      <div class="fls-card-actions fls-card-primary-action">
        <button class="btn btn-primary" type="button" onclick="flsProxyTest('{h(proxy_id)}', this)">测试代理连接</button>
      </div>
      <div class="fls-card-actions">
        <div class="row-actions" aria-label="代理 {h(proxy_name)} 行操作">
            <div class="row-actions-secondary">
                <details class="detail-disclosure proxy-mobile-more">
                    <summary>更多操作</summary>
                    <div class="proxy-mobile-more-actions">
                        <a class="btn btn-blue" href="/proxy/edit/{h(proxy_id)}">编辑代理设置</a>
                        <button class="btn btn-blue" type="button" onclick="flsProxyQuality('{h(proxy_id)}', this)">检测代理质量</button>
                        {mobile_toggle_action}
                    </div>
                </details>
            </div>
            <div class="row-actions-danger">{danger_actions}</div>
        </div>
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
        '<a class="btn btn-primary" href="/proxy/new">新增代理</a><a class="btn btn-gray" href="/proxy/diagnostics">诊断记录</a>',
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

function showProxyFailure(error){{
    let html = escapeHtml(flsActionFailure("请求失败：" + String(error), "检查服务状态后重试"));
    if(error.payload && error.payload.recovery_url === "/proxy/diagnostics"){{
        html += '<br><a class="btn btn-orange" href="/proxy/diagnostics">查看诊断记录并恢复</a>';
    }}
    showProxyResult(html);
}}

async function flsProxyTest(proxyId, source){{
    if(!flsMarkButtonBusy(source, "测试中...")) return;
    showProxyResult("正在测试代理，请稍候...");
    try {{
        const res = await fetch("/api/proxy/test/" + encodeURIComponent(proxyId), {{
            method: "POST",
            headers: {{"X-Requested-With":"XMLHttpRequest"}}
        }});
        const json = await flsReadJsonResponse(res);
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
        showProxyFailure(e);
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
        const json = await flsReadJsonResponse(res);
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
        showProxyFailure(e);
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
