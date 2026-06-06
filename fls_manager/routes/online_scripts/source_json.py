from ._common import *


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
