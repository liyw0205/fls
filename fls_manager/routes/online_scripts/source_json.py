from ._common import *
import hashlib

from ...storage import storage_transaction


@bp.route("/online-scripts/source", methods=["GET", "POST"])
def online_scripts_source():
    msg = request.args.get("msg", "")
    err = ""
    status_code = 200
    cache_text = ""
    revision = ""
    submitted_text = request.form.get("json_text", "") if request.method == "POST" else None

    # Version comparison and persistence share the cache writer's storage lock.
    with storage_transaction():
        try:
            cache_text = read_cache_text(strict=True)
            revision = hashlib.sha256(cache_text.encode("utf-8")).hexdigest()
        except Exception as e:
            status_code = 500
            err = f"读取在线脚本源缓存失败：{e}。未保存，请检查文件权限后重试。"
        else:
            if submitted_text is not None and request.form.get("cache_revision") != revision:
                status_code = 409
                err = "缓存已变更或编辑状态已失效，未保存。请对照当前缓存合并草稿后重新保存。"
            elif submitted_text is not None:
                try:
                    if not submitted_text.strip():
                        raise ValueError("JSON 内容不能为空")
                    data = json.loads(submitted_text)
                    items = normalize_online_scripts(data)
                    if len(items) != len(data):
                        raise ValueError("存在无效脚本条目，请检查 id、name、type 和 link，未保存任何条目")
                    if len({item["id"] for item in items}) != len(items):
                        raise ValueError("脚本 id 重复，请为每个脚本使用不同 id")
                except Exception as e:
                    status_code = 400
                    err = f"在线脚本源数据无效：{e}。草稿已保留，请修正后重新保存。"
                else:
                    try:
                        save_online_script_cache(items)
                    except Exception as e:
                        status_code = 500
                        err = f"在线脚本源数据保存失败：{e}。草稿已保留，请检查文件权限和磁盘空间后重试。"
                    else:
                        return redirect(url_for(
                            "online_scripts.online_scripts_source",
                            msg=f"在线脚本源数据保存成功，共 {len(items)} 条",
                        ))

    editor_text = submitted_text if submitted_text is not None else (cache_text or "[]")
    current_cache_html = ""
    if status_code == 409:
        current_cache_html = f'''
<section class="section fls-form-section">
    <details open>
        <summary>当前已保存缓存</summary>
        <textarea readonly aria-label="当前已保存缓存" style="min-height:220px;">{h(cache_text or '[]')}</textarea>
    </details>
</section>'''

    header = page_header_card(
        "在线脚本源数据",
        """
        这里显示当前本地缓存的在线脚本源数据。<br>
        如果服务器无法访问远程源，可以手动复制远程 index.json 内容，粘贴到这里保存。<br>
        保存后“在线脚本”列表会直接使用这份缓存。<br>
        支持字段：<code>doc_link</code>，可用于在线脚本页面查看文档。<br>
        支持字段：<code>task_cron.var</code>，可预设任务变量。
        """,
        '<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
    )

    body = f"""
{header}

{message_card(msg, "success")}
{message_card(err, "error")}

{current_cache_html}

<form method="post" action="/online-scripts/source">
<input type="hidden" name="cache_revision" value="{h(revision)}">
<section class="section fls-form-section">
    <h2 class="section-title">查看 / 修改缓存 JSON</h2>
    <textarea name="json_text" style="min-height:520px;" aria-label="在线脚本源 JSON 数据">{h(editor_text)}</textarea>
</section>

<section class="section fls-form-section fls-save-section">
    <button class="btn btn-primary" type="submit">保存在线脚本源数据</button>
    <a class="btn btn-gray" href="/online-scripts">返回</a>
</section>
</form>
"""

    return layout("在线脚本源数据", "online_scripts", body), status_code
