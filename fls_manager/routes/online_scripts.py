import json
import uuid
import time
from math import ceil
from urllib.parse import quote

import requests
from flask import Blueprint, request, redirect, url_for, abort, jsonify

from ..utils import h, get_back_url
from ..ui.layout import layout
from ..ui.log_controls import log_controls
from ..logs import tail_file
from ..proxy import (
    proxy_select_options,
    requests_proxy_dict,
    github_proxy_url,
)

from ..online_scripts.constants import (
    ONLINE_SCRIPT_CACHE_FILE,
    ONLINE_INSTALL_RUNNING,
    ONLINE_REFRESH_STATE,
)

from ..online_scripts.logs import online_install_log_file

from ..online_scripts.source import (
    get_online_script_source,
    normalize_online_scripts,
    load_online_script_cache,
    save_online_script_cache,
    read_cache_text,
    fetch_task_link_tasks,
    start_refresh_thread,
)

from ..online_scripts.tasks import (
    online_script_task_crons,
    online_task_cron_vars,
    guess_task_command,
    script_has_task,
    import_task_if_needed,
)

from ..online_scripts.install import (
    online_script_target,
    script_has_install,
    request_stop_online_install,
    start_install_thread,
)

from ..online_scripts.docs import (
    render_markdown_to_html,
    doc_url_looks_markdown,
    doc_content_looks_markdown,
    doc_response_is_html,
    doc_response_is_text,
)

from ..online_scripts.render import render_online_script_rows


bp = Blueprint("online_scripts", __name__)

def online_scripts_page_links(q, page, pages):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"/online-scripts?page={int(p)}"
        if q:
            url += "&q=" + quote(q)
        return url

    def page_btn(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"
        return f'<a class="btn {cls}" href="{h(build_url(p))}">{h(text)}</a>'

    page = max(1, min(int(page), int(pages)))

    items = []

    # 上一页
    items.append(
        page_btn(page - 1, "上一页", disabled=(page <= 1))
    )

    # 始终显示 1、最后一页、当前页前后 2 页
    show = {1, pages}

    for p in range(page - 2, page + 3):
        if 1 <= p <= pages:
            show.add(p)

    show = sorted(show)

    last = 0

    for p in show:
        if last and p - last > 1:
            items.append('<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>')

        items.append(
            page_btn(p, active=(p == page))
        )

        last = p

    # 下一页
    items.append(
        page_btn(page + 1, "下一页", disabled=(page >= pages))
    )

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            第 <b>{page}</b> / <b>{pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
</div>
"""


def filter_online_scripts_for_page(items, q):
    q = str(q or "").strip().lower()

    if not q:
        return items

    result = []

    for item in items:
        task_names = []
        task_commands = []

        for task in online_script_task_crons(item):
            task_names.append(str(task.get("name") or ""))
            task_commands.append(str(task.get("command") or ""))

        fields = [
            item.get("id", ""),
            item.get("name", ""),
            item.get("type", ""),
            item.get("link", ""),
            item.get("link_name", ""),
            item.get("install", ""),
            item.get("doc_link", ""),
            item.get("task_link", ""),
            "\n".join(task_names),
            "\n".join(task_commands),
        ]

        text = "\n".join(str(x or "") for x in fields).lower()

        if q in text:
            result.append(item)

    return result

def get_online_script(script_id):
    for item in load_online_script_cache():
        if item.get("id") == script_id:
            return item

    return None


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


@bp.route("/online-scripts/refresh", methods=["POST"])
def online_scripts_refresh():
    if ONLINE_REFRESH_STATE.get("running"):
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg="脚本源正在后台拉取中，请稍候",
            )
        )

    proxy_id = request.form.get("proxy_id", "").strip()
    start_refresh_thread(proxy_id)

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
            msg="已提交后台刷新，正在拉取中",
        )
    )


@bp.route("/api/online-scripts/refresh-status")
def api_online_refresh_status():
    return jsonify({
        "running": bool(ONLINE_REFRESH_STATE.get("running")),
        "message": ONLINE_REFRESH_STATE.get("message", ""),
        "error": ONLINE_REFRESH_STATE.get("error", ""),
        "updated_at": ONLINE_REFRESH_STATE.get("updated_at", ""),
        "log_file": ONLINE_REFRESH_STATE.get("log_file", ""),
    })


@bp.route("/online-scripts/source", methods=["GET", "POST"])
def online_scripts_source():
    msg = ""
    err = ""

    if request.method == "POST":
        text = request.form.get("json_text", "").strip()

        if not text:
            err = "JSON 内容不能为空"
        else:
            try:
                data = json.loads(text)
                items = normalize_online_scripts(data)
                save_online_script_cache(items)
                msg = f"脚本源 JSON 保存成功，共 {len(items)} 条"
            except Exception as e:
                err = f"脚本源 JSON 保存失败：{e}"

    cache_text = read_cache_text() or "[]"

    body = f"""
<div class="card">
    <div class="card-title">脚本源 JSON</div>
    <div class="help">
        这里显示当前本地缓存的脚本源 JSON。<br>
        如果服务器无法访问远程源，可以手动复制远程 index.json 内容，粘贴到这里保存。<br>
        保存后“在线脚本”列表会直接使用这份缓存。<br>
        支持字段：<code>doc_link</code>，可用于在线脚本页面查看文档。<br>
        支持字段：<code>task_cron.var</code>，可预设任务变量。
    </div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
</div>

{"<div class='card'><div class='help' style='color:#18a058;'>" + h(msg) + "</div></div>" if msg else ""}
{"<div class='card'><div class='help' style='color:#dc2626;'>" + h(err) + "</div></div>" if err else ""}

<form method="post">
<div class="card">
    <div class="card-title">查看 / 修改缓存 JSON</div>
    <textarea name="json_text" style="min-height:520px;">{h(cache_text)}</textarea>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存脚本源 JSON</button>
    <a class="btn btn-gray" href="/online-scripts">返回</a>
</div>
</form>
"""

    return layout("脚本源 JSON", "online_scripts", body)


@bp.route("/online-scripts/task-link/<script_id>", methods=["POST"])
def online_scripts_pull_task_link(script_id):
    items = load_online_script_cache()

    item = None
    item_index = -1

    for idx, one in enumerate(items):
        if one.get("id") == script_id:
            item = one
            item_index = idx
            break

    if not item:
        abort(404)

    task_link = str(item.get("task_link") or "").strip()

    if not task_link:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="该脚本没有配置外部任务源 task_link",
            )
        )

    proxy_id = request.form.get("proxy_id", "").strip()

    try:
        linked_tasks = fetch_task_link_tasks(
            item,
            proxy_id=proxy_id,
            timeout=20,
        )

        if not linked_tasks:
            return redirect(
                url_for(
                    "online_scripts.online_scripts_page",
                    err="外部任务源已拉取，但没有可用任务",
                )
            )

        old_tasks = online_script_task_crons(item)

        exists_keys = set()
        merged = []

        for task in old_tasks:
            key = (
                str(task.get("name") or ""),
                str(task.get("cron") or ""),
                str(task.get("command") or ""),
            )
            exists_keys.add(key)
            merged.append(task)

        added = 0

        for task in linked_tasks:
            key = (
                str(task.get("name") or ""),
                str(task.get("cron") or ""),
                str(task.get("command") or ""),
            )

            if key in exists_keys:
                continue

            exists_keys.add(key)
            merged.append(task)
            added += 1

        item["task_cron"] = merged
        items[item_index] = item
        save_online_script_cache(items)

        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg=f"外部任务源拉取完成，新增 {added} 个任务，当前共 {len(merged)} 个任务",
            )
        )

    except Exception as e:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err=f"外部任务源拉取失败：{e}",
            )
        )


@bp.route("/online-scripts/doc/<script_id>")
def online_script_doc(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    doc_link = str(item.get("doc_link") or "").strip()

    if not doc_link:
        body = """
<div class="card">
    <div class="card-title">脚本文档</div>
    <div class="help">该脚本未提供 doc_link。</div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
</div>
"""
        return layout("脚本文档", "online_scripts", body)

    proxy_id = request.args.get("proxy_id", "").strip()
    mode = request.args.get("mode", "auto").strip().lower()

    if mode not in ("auto", "render", "web", "raw"):
        mode = "auto"

    proxy_options = proxy_select_options(proxy_id)
    real_url = github_proxy_url(doc_link, proxy_id, verify=True)

    doc_text = ""
    doc_html = ""
    content_type = "-"
    detected = "-"
    err = ""

    if mode == "web":
        detected = "网页窗口"
        doc_html = f"""
<div class="fls-doc-window">
    <iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe>
</div>
<div class="help" style="margin-top:10px;">
    如果网页无法嵌入显示，可能是对方网站禁止 iframe。请点击“打开原文”。
</div>
"""
    else:
        try:
            r = requests.get(
                real_url,
                timeout=25,
                headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
                proxies=requests_proxy_dict(proxy_id),
            )
            r.raise_for_status()

            content_type = r.headers.get("Content-Type", "-")
            doc_text = r.text or ""

            is_html = doc_response_is_html(r, doc_text)
            is_text = doc_response_is_text(r)
            is_md = (
                doc_url_looks_markdown(doc_link)
                or "markdown" in str(content_type).lower()
                or doc_content_looks_markdown(doc_text)
            )

            if mode == "raw":
                detected = "原文源码"
                doc_html = f'<pre class="fls-doc-raw">{h(doc_text or "暂无内容")}</pre>'

            elif mode == "render":
                if is_md:
                    detected = "Markdown 渲染"
                    doc_html = f'<div class="fls-doc-md">{render_markdown_to_html(doc_text)}</div>'
                elif is_text:
                    detected = "文本渲染"
                    doc_html = f'<pre class="fls-doc-raw">{h(doc_text or "暂无内容")}</pre>'
                else:
                    detected = "网页窗口"
                    doc_html = f'<div class="fls-doc-window"><iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe></div>'

            else:
                if is_html and not doc_url_looks_markdown(doc_link):
                    detected = "网页窗口"
                    doc_html = f"""
<div class="fls-doc-window">
    <iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe>
</div>
<div class="help" style="margin-top:10px;">
    已自动识别为网页。如果无法显示，请点击“打开原文”。
</div>
"""
                elif is_md:
                    detected = "Markdown 渲染"
                    doc_html = f'<div class="fls-doc-md">{render_markdown_to_html(doc_text)}</div>'
                elif is_text:
                    detected = "文本渲染"
                    doc_html = f'<pre class="fls-doc-raw">{h(doc_text or "暂无内容")}</pre>'
                else:
                    detected = "网页窗口"
                    doc_html = f"""
<div class="fls-doc-window">
    <iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe>
</div>
<div class="help" style="margin-top:10px;">
    已自动识别为网页或非文本内容。如果无法显示，请点击“打开原文”。
</div>
"""

        except Exception as e:
            err = str(e)

            if mode == "auto":
                detected = "请求失败，尝试网页窗口"
                doc_html = f"""
<div class="fls-doc-window">
    <iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe>
</div>
<div class="help" style="margin-top:10px;">
    文档内容拉取失败，已尝试用网页窗口打开。若仍无法显示，请点击“打开原文”。
</div>
"""

    body = f"""
<style>
.fls-doc-toolbar {{
    display:flex;
    flex-wrap:wrap;
    gap:8px;
    align-items:center;
}}

.fls-doc-toolbar select {{
    width:auto;
    min-width:180px;
}}

.fls-doc-toolbar .btn {{
    margin:0;
}}

.fls-doc-window {{
    width:100%;
    height:calc(100vh - 220px);
    min-height:620px;
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:14px;
    overflow:hidden;
}}

.fls-doc-iframe {{
    width:100%;
    height:100%;
    border:0;
    background:#fff;
}}

.fls-doc-md {{
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:14px;
    padding:18px;
    line-height:1.75;
    color:#111827;
    overflow:auto;
}}

.fls-doc-md h1,
.fls-doc-md h2,
.fls-doc-md h3,
.fls-doc-md h4,
.fls-doc-md h5,
.fls-doc-md h6 {{
    margin:18px 0 10px;
    line-height:1.35;
    color:#111827;
}}

.fls-doc-md h1 {{
    font-size:28px;
    border-bottom:1px solid #e5e7eb;
    padding-bottom:10px;
}}

.fls-doc-md h2 {{
    font-size:23px;
    border-bottom:1px solid #f1f5f9;
    padding-bottom:8px;
}}

.fls-doc-md h3 {{
    font-size:19px;
}}

.fls-doc-md p {{
    margin:10px 0;
}}

.fls-doc-md ul,
.fls-doc-md ol {{
    padding-left:24px;
}}

.fls-doc-md li {{
    margin:5px 0;
}}

.fls-doc-md blockquote {{
    margin:12px 0;
    padding:8px 12px;
    border-left:4px solid #18a058;
    background:#f0fdf4;
    color:#374151;
    border-radius:8px;
}}

.fls-doc-md code {{
    background:#f3f4f6;
    color:#dc2626;
    padding:2px 5px;
    border-radius:6px;
    font-family:Consolas,Menlo,monospace;
}}

.fls-md-code {{
    background:#0b1020;
    color:#d1d5db;
    border-radius:12px;
    padding:14px;
    overflow:auto;
    white-space:pre;
}}

.fls-md-code code {{
    background:transparent;
    color:inherit;
    padding:0;
}}

.fls-doc-raw {{
    background:#0b1020;
    color:#d1d5db;
    border-radius:14px;
    padding:16px;
    min-height:620px;
    white-space:pre-wrap;
    word-break:break-word;
    overflow:auto;
    font-family:Consolas,Menlo,monospace;
    font-size:13px;
    line-height:1.55;
}}

body.fls-mobile .fls-doc-window {{
    height:calc(100vh - 190px);
    min-height:520px;
    border-radius:12px;
}}

body.fls-mobile .fls-doc-md {{
    padding:13px;
    border-radius:12px;
}}

body.fls-mobile .fls-doc-md h1 {{
    font-size:23px;
}}

body.fls-mobile .fls-doc-md h2 {{
    font-size:20px;
}}

body.fls-mobile .fls-doc-raw {{
    min-height:520px;
    font-size:12px;
}}
</style>

<div class="card">
    <div class="card-title">脚本文档：{h(item.get("name") or script_id)}</div>
    <div class="help">
        脚本 ID：{h(item.get("id"))}<br>
        识别结果：<b>{h(detected)}</b><br>
        Content-Type：{h(content_type)}<br>
        文档地址：<a href="{h(doc_link)}" target="_blank">{h(doc_link)}</a><br>
        实际地址：<a href="{h(real_url)}" target="_blank">{h(real_url)}</a>
    </div>
    <br>

    <form method="get" class="fls-doc-toolbar">
        <select name="proxy_id">{proxy_options}</select>
        <select name="mode">
            <option value="auto" {"selected" if mode == "auto" else ""}>自动识别</option>
            <option value="render" {"selected" if mode == "render" else ""}>渲染 Markdown / 文本</option>
            <option value="web" {"selected" if mode == "web" else ""}>网页窗口</option>
            <option value="raw" {"selected" if mode == "raw" else ""}>原文源码</option>
        </select>
        <button class="btn btn-primary" type="submit">重新加载</button>
        <a class="btn btn-blue" href="{h(real_url)}" target="_blank">打开原文</a>
        <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
    </form>
</div>

{"<div class='card'><div class='help' style='color:#dc2626;font-weight:800;'>文档加载失败：" + h(err) + "</div></div>" if err else ""}

<div class="card">
    {doc_html or '<div class="help">暂无文档内容</div>'}
</div>
"""

    return layout("脚本文档", "online_scripts", body)

def parse_excluded_task_indexes(raw):
    result = set()

    for x in str(raw or "").replace("，", ",").split(","):
        x = x.strip()

        if not x:
            continue

        try:
            n = int(x)
            if n > 0:
                result.add(n)
        except Exception:
            pass

    return result


def selected_task_indexes_from_form(item):
    """
    从安装选择页表单中解析最终选择的任务。

    支持：
    1. 新版分页选择：
       select_mode=all
       excluded_task_indexes=1,3,5

    2. 旧版显式选择：
       task_indexes=1&task_indexes=2
    """
    selected_task_indexes = request.form.getlist("task_indexes")

    select_mode = request.form.get("select_mode", "").strip()
    excluded_task_indexes = request.form.get("excluded_task_indexes", "").strip()

    if select_mode == "all":
        all_count = len(online_script_task_crons(item))
        excluded_set = parse_excluded_task_indexes(excluded_task_indexes)

        selected_task_indexes = [
            str(i)
            for i in range(1, all_count + 1)
            if i not in excluded_set
        ]

    return selected_task_indexes

def install_task_page_links(script_id, task_page, task_pages, excluded="", task_q=""):
    if task_pages <= 1:
        return ""

    def page_btn(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"
        return (
            f'<button class="btn {cls}" type="button" '
            f'onclick="flsInstallGoTaskPage({int(p)})">{h(text)}</button>'
        )

    task_page = max(1, min(int(task_page), int(task_pages)))

    items = []

    # 上一页
    items.append(
        page_btn(task_page - 1, "上一页", disabled=(task_page <= 1))
    )

    # 始终显示 1、最后一页、当前页前后 2 页
    show = {1, task_pages}

    for p in range(task_page - 2, task_page + 3):
        if 1 <= p <= task_pages:
            show.add(p)

    show = sorted(show)
    last = 0

    for p in show:
        if last and p - last > 1:
            items.append(
                '<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>'
            )

        items.append(
            page_btn(p, active=(p == task_page))
        )

        last = p

    # 下一页
    items.append(
        page_btn(task_page + 1, "下一页", disabled=(task_page >= task_pages))
    )

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            任务第 <b>{task_page}</b> / <b>{task_pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
</div>
"""

@bp.route("/online-scripts/install-select/<script_id>")
def online_scripts_install_select(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    task_crons = online_script_task_crons(item)
    has_task = script_has_task(item)

    if not has_task:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="该脚本没有可选择的任务",
            )
        )

    proxy_options = proxy_select_options("")

    task_q = request.args.get("task_q", "").strip()
    task_q_lower = task_q.lower()

    task_page = max(1, int(request.args.get("task_page", "1") or 1))
    task_per_page = 10

    excluded_raw = request.args.get("excluded", "").strip()
    excluded_set = parse_excluded_task_indexes(excluded_raw)

    total_tasks = len(task_crons)

    indexed_tasks = []

    for idx, task_cron in enumerate(task_crons, 1):
        env = online_task_cron_vars(task_cron)
        env_text = "\n".join([f"{k}={v}" for k, v in env.items()]) if env else ""

        fields = [
            task_cron.get("name", ""),
            task_cron.get("cron", ""),
            task_cron.get("command", ""),
            task_cron.get("remark", ""),
            task_cron.get("config_path", ""),
            env_text,
            str(idx),
        ]

        search_text = "\n".join(str(x or "") for x in fields).lower()

        if not task_q_lower or task_q_lower in search_text:
            indexed_tasks.append((idx, task_cron))

    filtered_total = len(indexed_tasks)
    task_pages = max(1, ceil(filtered_total / task_per_page))
    task_page = min(task_page, task_pages)

    start = (task_page - 1) * task_per_page
    end = task_page * task_per_page

    show_indexed_tasks = indexed_tasks[start:end]

    task_rows = ""

    if not show_indexed_tasks:
        task_rows = """
<div class="fls-empty-card" style="grid-column:1 / -1;">
    暂无匹配任务
</div>
"""
    else:
        for idx, task_cron in show_indexed_tasks:
            name = str(task_cron.get("name") or f"任务{idx}").strip()
            cron = str(task_cron.get("cron") or "手动").strip()
            command = str(task_cron.get("command") or guess_task_command(item) or "-").strip()
            remark = str(task_cron.get("remark") or "").strip()
            config_path = str(task_cron.get("config_path") or "").strip()
            env = online_task_cron_vars(task_cron)

            env_text = "-"

            if env:
                env_text = "\n".join([f"{k}={v}" for k, v in env.items()])

            checked = "" if idx in excluded_set else "checked"

            remark_html = ""
            if remark:
                remark_html = f"""
                <div class="fls-install-task-meta">
                    <b>备注：</b>{h(remark)}
                </div>
"""

            config_html = ""
            if config_path:
                config_html = f"""
                <div class="fls-install-task-meta">
                    <b>配置：</b>{h(config_path)}
                </div>
"""

            task_rows += f"""
<label class="fls-install-task-row">
    <div class="fls-install-task-check">
        <input
            type="checkbox"
            class="fls-install-task-checkbox"
            data-index="{idx}"
            name="visible_task_indexes"
            value="{idx}"
            {checked}
            style="width:auto;"
        >
    </div>

    <div class="fls-install-task-main">
        <div class="fls-install-task-title">
            {h(name)}
            <span class="badge blue">#{idx}</span>
        </div>

        <div class="fls-install-task-meta">
            <b>Cron：</b>{h(cron)}
        </div>

        {remark_html}
        {config_html}

        <div class="fls-install-task-meta">
            <b>命令：</b>
        </div>
        <pre class="fls-install-task-code">{h(command)}</pre>

        <div class="fls-install-task-meta">
            <b>变量：</b>
        </div>
        <pre class="fls-install-task-code">{h(env_text)}</pre>
    </div>
</label>
"""

    excluded_text = ",".join(str(x) for x in sorted(excluded_set))

    page_links_html = install_task_page_links(
        script_id=script_id,
        task_page=task_page,
        task_pages=task_pages,
        excluded=excluded_text,
        task_q=task_q,
    )

    selected_count = total_tasks - len(excluded_set)
    if selected_count < 0:
        selected_count = 0

    display_start = start + 1 if filtered_total else 0
    display_end = min(end, filtered_total)

    body = f"""
<style>
.fls-install-select-head {{
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    gap:12px;
    flex-wrap:wrap;
}}

.fls-install-task-tools {{
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    align-items:center;
}}

.fls-install-task-list {{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:12px;
}}

.fls-install-task-row {{
    display:flex;
    gap:10px;
    align-items:flex-start;
    border:1px solid #e5e7eb;
    border-radius:14px;
    background:#fff;
    padding:12px;
    cursor:pointer;
    min-width:0;
    box-shadow:0 4px 16px rgba(0,0,0,.03);
}}

.fls-install-task-row:hover {{
    border-color:#93c5fd;
    background:#f8fafc;
}}

.fls-install-task-check {{
    padding-top:3px;
}}

.fls-install-task-main {{
    min-width:0;
    flex:1;
}}

.fls-install-task-title {{
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    align-items:center;
    color:#111827;
    font-size:15px;
    font-weight:900;
    line-height:1.35;
    word-break:break-word;
}}

.fls-install-task-meta {{
    margin-top:7px;
    color:#6b7280;
    font-size:12px;
    line-height:1.5;
    word-break:break-word;
}}

.fls-install-task-code {{
    margin:5px 0 0;
    padding:8px;
    border-radius:8px;
    background:#f3f4f6;
    color:#374151;
    font-family:Consolas,Menlo,monospace;
    font-size:12px;
    line-height:1.45;
    white-space:pre-wrap;
    word-break:break-all;
}}

body.fls-mobile .fls-install-task-list {{
    grid-template-columns:1fr!important;
}}
</style>

<form method="post" action="/online-scripts/install/{h(script_id)}" id="onlineInstallSelectForm">
<input type="hidden" name="select_mode" value="all">
<input type="hidden" name="excluded_task_indexes" id="excludedTaskIndexes" value="{h(excluded_text)}">

<div class="card">
    <div class="fls-install-select-head">
        <div>
            <div class="card-title">选择任务并安装：{h(item.get("name") or script_id)}</div>
            <div class="help">
                脚本 ID：{h(item.get("id"))}<br>
                脚本类型：{h(item.get("type"))}<br>
                保存名：{h(item.get("link_name"))}<br>
                共检测到 <b>{total_tasks}</b> 个可导入任务。默认全选全部任务。<br>
选择约 <b id="selectedTaskCount">{selected_count}</b> 个任务。
            </div>
        </div>

        <div class="action-row">
            <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">安装选项</div>

    <div class="form-grid">
        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id">{proxy_options}</select>
        </div>

        <div class="form-item">
            <label>任务导入</label>
            <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;padding-top:8px;">
                <label class="fls-inline-check">
                    <input type="checkbox" name="import_task" value="1" checked style="width:auto;">
                    导入所选任务
                </label>

                <label class="fls-inline-check">
                    <input type="checkbox" name="enable_task" value="1" style="width:auto;">
                    导入后启用任务
                </label>
            </div>
            <div class="help">
                不勾选“导入后启用任务”时，导入后的任务默认为禁用，需要到任务管理手动启用。
            </div>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">搜索任务</div>
    <div class="form-grid">
        <div class="form-item">
            <label>关键词</label>
            <input id="installTaskSearchInput" value="{h(task_q)}" placeholder="任务名 / Cron / 命令 / 备注 / 变量 / 序号">
        </div>

        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="button" onclick="flsInstallSearchTasks()">搜索</button>
            <button class="btn btn-gray" type="button" onclick="flsInstallClearSearch()">重置搜索</button>
        </div>
    </div>
</div>

<div class="card">
    <div class="fls-install-select-head">
        <div>
            <div class="card-title">选择要导入的任务</div>
            <div class="help">
                当前页显示 {display_start} - {display_end} / {filtered_total} 个匹配任务，每页 10 个。<br>
                默认全选全部任务，可以取消不需要导入的任务。
            </div>
        </div>

        <div class="fls-install-task-tools">
            <button class="btn btn-blue" type="button" onclick="flsInstallSelectCurrentPage(true)">当前页全选</button>
            <button class="btn btn-gray" type="button" onclick="flsInstallSelectCurrentPage(false)">当前页取消</button>
            <button class="btn btn-red" type="button" onclick="flsInstallCancelAllGlobal()">全部取消</button>
            <button class="btn btn-primary" type="button" onclick="flsInstallSelectAllGlobal()">全部任务全选</button>
        </div>
    </div>

    <br>

    <div class="fls-install-task-list">
        {task_rows}
    </div>
</div>

{page_links_html}

<div class="card">
    <button class="btn btn-primary" type="submit">开始下载安装</button>
    <button class="btn btn-orange" type="submit" formaction="/online-scripts/import-tasks/{h(script_id)}">立即导入所选任务</button>
    <a class="btn btn-gray" href="/online-scripts">取消</a>
</div>
</form>

<script>
const FLS_TOTAL_TASKS = {total_tasks};

function flsParseExcluded(){{
    const el = document.getElementById("excludedTaskIndexes");
    if(!el) return new Set();

    const text = el.value || "";
    const set = new Set();

    text.split(",").forEach(function(x){{
        x = String(x || "").trim();
        if(!x) return;

        const n = parseInt(x, 10);
        if(n > 0) set.add(n);
    }});

    return set;
}}

function flsSaveExcluded(set){{
    const el = document.getElementById("excludedTaskIndexes");
    if(!el) return;

    const arr = Array.from(set)
        .filter(function(n){{ return n > 0; }})
        .sort(function(a,b){{ return a-b; }});

    el.value = arr.join(",");

    const countEl = document.getElementById("selectedTaskCount");
    if(countEl){{
        countEl.textContent = Math.max(0, FLS_TOTAL_TASKS - arr.length);
    }}
}}

function flsSyncVisibleCheckboxesToExcluded(){{
    const set = flsParseExcluded();

    document.querySelectorAll(".fls-install-task-checkbox").forEach(function(input){{
        const idx = parseInt(input.getAttribute("data-index") || "0", 10);
        if(idx <= 0) return;

        if(input.checked){{
            set.delete(idx);
        }}else{{
            set.add(idx);
        }}
    }});

    flsSaveExcluded(set);
}}

function flsApplyExcludedToVisible(){{
    const set = flsParseExcluded();

    document.querySelectorAll(".fls-install-task-checkbox").forEach(function(input){{
        const idx = parseInt(input.getAttribute("data-index") || "0", 10);
        input.checked = !set.has(idx);
    }});

    flsSaveExcluded(set);
}}

function flsInstallSelectCurrentPage(checked){{
    document.querySelectorAll(".fls-install-task-checkbox").forEach(function(input){{
        input.checked = !!checked;
    }});

    flsSyncVisibleCheckboxesToExcluded();
}}

function flsInstallSelectAllGlobal(){{
    flsSaveExcluded(new Set());
    flsApplyExcludedToVisible();
}}

function flsInstallCancelAllGlobal(){{
    const set = new Set();

    for(let i = 1; i <= FLS_TOTAL_TASKS; i++){{
        set.add(i);
    }}

    flsSaveExcluded(set);
    flsApplyExcludedToVisible();
}}

function flsInstallBuildUrl(page){{
    flsSyncVisibleCheckboxesToExcluded();

    const excluded = document.getElementById("excludedTaskIndexes").value || "";
    const qEl = document.getElementById("installTaskSearchInput");
    const taskQ = qEl ? qEl.value.trim() : "";

    const url = new URL("/online-scripts/install-select/{h(script_id)}", window.location.origin);
    url.searchParams.set("task_page", page || "1");

    if(taskQ){{
        url.searchParams.set("task_q", taskQ);
    }}

    if(excluded){{
        url.searchParams.set("excluded", excluded);
    }}

    return url.toString();
}}

function flsInstallSearchTasks(){{
    window.location.href = flsInstallBuildUrl("1");
}}

function flsInstallClearSearch(){{
    flsSyncVisibleCheckboxesToExcluded();

    const excluded = document.getElementById("excludedTaskIndexes").value || "";
    const url = new URL("/online-scripts/install-select/{h(script_id)}", window.location.origin);
    url.searchParams.set("task_page", "1");

    if(excluded){{
        url.searchParams.set("excluded", excluded);
    }}

    window.location.href = url.toString();
}}

document.querySelectorAll(".fls-install-task-checkbox").forEach(function(input){{
    input.addEventListener("change", flsSyncVisibleCheckboxesToExcluded);
}});

function flsInstallGoTaskPage(page){{
    window.location.href = flsInstallBuildUrl(String(page || 1));
}}

document.getElementById("onlineInstallSelectForm").addEventListener("submit", function(e){{
    flsSyncVisibleCheckboxesToExcluded();

    var importTask = document.querySelector('#onlineInstallSelectForm input[name="import_task"]');
    var excluded = flsParseExcluded();

    if(importTask && importTask.checked && excluded.size >= FLS_TOTAL_TASKS){{
        e.preventDefault();
        alert("已勾选导入任务，但没有选择任何任务");
        return false;
    }}
}});

flsApplyExcludedToVisible();
</script>
"""

    return layout("选择任务并安装", "online_scripts", body)

@bp.route("/online-scripts/import-tasks/<script_id>", methods=["POST"])
def online_scripts_import_tasks_only(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    if not script_has_task(item):
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="该脚本没有可导入的任务",
            )
        )

    enable_task = request.form.get("enable_task") == "1"
    selected_task_indexes = selected_task_indexes_from_form(item)

    if not selected_task_indexes:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="没有选择任何任务，未导入",
            )
        )

    try:
        ok, msg = import_task_if_needed(
            item,
            log_file=None,
            enable_task=enable_task,
            selected_task_indexes=selected_task_indexes,
        )

        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg=msg if ok else "",
                err="" if ok else msg,
            )
        )

    except Exception as e:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err=f"任务导入失败：{e}",
            )
        )
        
@bp.route("/online-scripts/install/<script_id>", methods=["POST"])
def online_scripts_install(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    for install_id, info in ONLINE_INSTALL_RUNNING.items():
        if info.get("script_id") == script_id and info.get("running"):
            return redirect(
                url_for(
                    "online_scripts.online_scripts_page",
                    msg="该脚本正在安装中",
                )
            )

    proxy_id = request.form.get("proxy_id", "").strip()
    import_task = request.form.get("import_task") == "1"
    enable_task = request.form.get("enable_task") == "1"
    force = request.form.get("force") == "1"

    selected_task_indexes = selected_task_indexes_from_form(item)
    select_mode = request.form.get("select_mode", "").strip()
    excluded_task_indexes = request.form.get("excluded_task_indexes", "").strip()

    if not import_task:
        enable_task = False
        selected_task_indexes = []

    if import_task and script_has_task(item) and not selected_task_indexes:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="已勾选导入任务，但没有选择任何任务",
            )
        )

    try:
        target = online_script_target(item)
    except Exception as e:
        return layout("在线脚本安装失败", "online_scripts", f"""
<div class="card">
    <div class="card-title">目标路径非法</div>
    <div class="help">{h(e)}</div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回</a>
</div>
"""), 400

    selected_task_hidden = ""

    for idx in selected_task_indexes:
        try:
            n = int(idx)
            if n > 0:
                selected_task_hidden += f'<input type="hidden" name="task_indexes" value="{h(n)}">'
        except Exception:
            pass

    install_option_hidden = ""

    if import_task:
        install_option_hidden += '<input type="hidden" name="import_task" value="1">'

    if enable_task:
        install_option_hidden += '<input type="hidden" name="enable_task" value="1">'

    select_mode_hidden = ""

    if select_mode:
        select_mode_hidden += f'<input type="hidden" name="select_mode" value="{h(select_mode)}">'

    if excluded_task_indexes:
        select_mode_hidden += f'<input type="hidden" name="excluded_task_indexes" value="{h(excluded_task_indexes)}">'

    if target.exists() and not force:
        proxy_options = proxy_select_options(proxy_id)
        has_task = script_has_task(item)

        body = f"""
<div class="card">
    <div class="card-title">目标已存在，请确认</div>
    <div class="help" style="color:#dc2626;">
        检测到同名文件或文件夹已经存在。为避免意外覆盖，已暂停操作。<br>
        目标路径：<b>{h(target)}</b>
    </div>
    <br>
    <div class="help">
        如果是 Git 仓库目录，继续后会执行 <code>git pull</code> 更新。<br>
        如果是 raw 文件，继续后会覆盖该文件。<br>
        如果目标是非 Git 文件夹，继续也不会强行覆盖，需要你手动处理。
    </div>
</div>

<div class="card">
    <form method="post" action="/online-scripts/install/{h(script_id)}">
        <input type="hidden" name="force" value="1">
        {install_option_hidden}
        {select_mode_hidden}
        {selected_task_hidden}

        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id">{proxy_options}</select>
        </div>

        <br>

        <div class="help">
            导入任务：{"是" if import_task else "否"}<br>
            导入后启用：{"是" if enable_task else "否"}<br>
            已选择任务数：{len(selected_task_indexes) if import_task and has_task else 0}
        </div>

        <br>

        <button class="btn btn-orange" type="submit" onclick="return confirm('确定继续吗？可能会覆盖文件或更新仓库。')">确认继续</button>
        <a class="btn btn-gray" href="/online-scripts">取消</a>
    </form>
</div>
"""
        return layout("目标已存在", "online_scripts", body)

    install_id = uuid.uuid4().hex
    log_file = online_install_log_file(
        install_id,
        item.get("name") or item.get("id"),
    )

    ONLINE_INSTALL_RUNNING[install_id] = {
        "id": install_id,
        "script_id": script_id,
        "script_name": item.get("name"),
        "log_file": str(log_file),
        "running": True,
        "status": "准备中",
        "start_time": time.time(),
        "returncode": None,
        "error": "",
        "process": None,
    }

    start_install_thread(
        install_id=install_id,
        item=item,
        proxy_id=proxy_id,
        import_task=import_task,
        force=force,
        enable_task=enable_task,
        selected_task_indexes=selected_task_indexes,
    )

    return redirect(
        url_for(
            "online_scripts.online_install_log",
            install_id=install_id,
            back="/online-scripts",
        )
    )


@bp.route("/online-scripts/install-stop/<install_id>", methods=["POST"])
def online_scripts_install_stop(install_id):
    ok, msg = request_stop_online_install(install_id)

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
            msg=msg if ok else "",
            err="" if ok else msg,
        )
    )


@bp.route("/online-scripts/log/<install_id>")
def online_install_log(install_id):
    back_url = get_back_url("/online-scripts")
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        body = f"""
<div class="card">
    <div class="card-title">在线脚本日志</div>
    <div class="help">
        安装记录不存在或面板已重启。<br>
        可以到日志管理中查找 online-script-install-*.log。
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/logs?back={h(back_url)}">查看日志管理</a>
</div>
"""
        return layout("在线脚本日志", "online_scripts", body)

    stop_install_button = ""

    if info.get("running"):
        stop_install_button = f"""
<form method="post" action="/online-scripts/install-stop/{h(install_id)}" style="display:inline;">
    <button class="btn btn-red" type="submit" onclick="return confirm('确定停止该安装任务吗？')">停止安装</button>
</form>
"""

    body = f"""
<div class="card">
    <div class="card-title">在线脚本下载安装日志：{h(info.get("script_name") or install_id)}</div>
    <div class="help">
        状态：<b id="installStatus">{h(info.get("status") or "-")}</b><br>
        日志文件：{h(info.get("log_file") or "-")}
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/pull">脚本管理</a>
    <a class="btn btn-orange" href="/tasks">任务管理</a>
    {stop_install_button}
</div>

<pre class="log" id="log">加载中...</pre>
{log_controls()}

<script>
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function nearBottom(){{
    return document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 90;
}}

window.addEventListener("scroll", function(){{
    window.__FLS_LOG_NEAR_BOTTOM__ = nearBottom();
}}, {{passive:true}});

async function loadLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/online-scripts/log/{h(install_id)}?lines=1600", {{cache:"no-store"}});
        const json = await res.json();

        document.getElementById("installStatus").textContent = json.status || "-";

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

        if(!json.running){{
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""

    return layout("在线脚本日志", "online_scripts", body)


@bp.route("/api/online-scripts/log/<install_id>")
def api_online_install_log(install_id):
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        return jsonify({
            "running": False,
            "status": "记录不存在或面板已重启",
            "log": "安装记录不存在或面板已重启。请到日志管理中查找 online-script-install-*.log。",
        })

    log_file = info.get("log_file", "")
    lines = int(request.args.get("lines", "1200") or 1200)

    return jsonify({
        "running": bool(info.get("running")),
        "status": info.get("status") or "-",
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "log_file": log_file,
        "log": tail_file(log_file, lines),
    })