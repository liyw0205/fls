from ._common import *


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
