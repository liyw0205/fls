import hashlib
import re

from ._common import *

from ...directory_jobs import directory_job_operation
from ...online_scripts.source_coordination import source_cache_transaction

SOURCE_CACHE_MAX_BYTES = 4 * 1024 * 1024
SOURCE_REQUEST_MAX_BYTES = 8 * 1024 * 1024


def _source_json_response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _source_editor_text(cache_text):
    return cache_text if cache_text else "[]"


def _source_cache_revision(cache_text):
    return hashlib.sha256(cache_text.encode("utf-8")).hexdigest()


@bp.route("/api/online-scripts/source", methods=["GET", "PUT"])
@directory_job_operation
def api_online_scripts_source():
    if request.method == "GET":
        try:
            with source_cache_transaction():
                cache_text = read_cache_text(strict=True)
        except Exception:
            return _source_json_response({"ok": False, "msg": "在线脚本源缓存无法读取"}, 500)
        if len(cache_text.encode("utf-8")) > SOURCE_CACHE_MAX_BYTES:
            return _source_json_response({"ok": False, "msg": "在线脚本源缓存超过 4 MiB 限制"}, 413)
        return _source_json_response({
            "ok": True,
            "json_text": _source_editor_text(cache_text),
            "cache_revision": _source_cache_revision(cache_text),
        })

    if request.content_length is not None and request.content_length > SOURCE_REQUEST_MAX_BYTES:
        return _source_json_response({"ok": False, "msg": "在线脚本源请求超过 8 MiB 限制"}, 413)
    raw_body = request.stream.read(SOURCE_REQUEST_MAX_BYTES + 1)
    if len(raw_body) > SOURCE_REQUEST_MAX_BYTES:
        return _source_json_response({"ok": False, "msg": "在线脚本源请求超过 8 MiB 限制"}, 413)
    try:
        data = json.loads(raw_body) if request.is_json else None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return _source_json_response({"ok": False, "msg": "请求必须为 JSON 对象"}, 400)
    submitted_text = data.get("json_text")
    revision = data.get("cache_revision")
    if not isinstance(submitted_text, str):
        return _source_json_response({"ok": False, "msg": "缺少在线脚本源 JSON 文本"}, 400)
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        return _source_json_response({"ok": False, "msg": "缓存版本无效，请重新读取后保存"}, 400)
    try:
        submitted_size = len(submitted_text.encode("utf-8"))
    except UnicodeEncodeError:
        return _source_json_response({"ok": False, "msg": "在线脚本源 JSON 包含无效 Unicode 文本"}, 400)
    if submitted_size > SOURCE_CACHE_MAX_BYTES:
        return _source_json_response({"ok": False, "msg": "在线脚本源缓存超过 4 MiB 限制"}, 413)

    with source_cache_transaction():
        try:
            current_text = read_cache_text(strict=True)
        except Exception:
            return _source_json_response({"ok": False, "msg": "在线脚本源缓存无法读取，未保存"}, 500)
        current_revision = _source_cache_revision(current_text)
        if revision != current_revision:
            return _source_json_response({
                "ok": False,
                "code": "revision_conflict",
                "msg": "缓存已变更，未保存。请合并当前内容后重试。",
                "current_json_text": _source_editor_text(current_text),
                "cache_revision": current_revision,
            }, 409)
        try:
            if not submitted_text.strip():
                raise ValueError("JSON 内容不能为空")
            source_data = json.loads(submitted_text)
            items = normalize_online_scripts(source_data)
            if not isinstance(source_data, list) or len(items) != len(source_data):
                raise ValueError("存在无效脚本条目，请检查 id、name、type 和 link")
            if len({item["id"] for item in items}) != len(items):
                raise ValueError("脚本 id 重复，请为每个脚本使用不同 id")
            serialized = json.dumps(items, ensure_ascii=False, indent=2)
            if len(serialized.encode("utf-8")) > SOURCE_CACHE_MAX_BYTES:
                return _source_json_response({"ok": False, "msg": "规范化后的在线脚本源缓存超过 4 MiB 限制"}, 413)
        except (TypeError, ValueError, RuntimeError, UnicodeEncodeError):
            return _source_json_response({"ok": False, "msg": "在线脚本源 JSON 无效，未保存"}, 400)
        try:
            save_online_script_cache(items)
            saved_text = read_cache_text(strict=True)
        except Exception:
            return _source_json_response({"ok": False, "msg": "在线脚本源保存失败，未确认缓存状态"}, 500)

    return _source_json_response({
        "ok": True,
        "count": len(items),
        "json_text": _source_editor_text(saved_text),
        "cache_revision": _source_cache_revision(saved_text),
    })


@bp.route("/online-scripts/source", methods=["GET", "POST"])
@directory_job_operation
def online_scripts_source():
    msg = request.args.get("msg", "")
    err = ""
    status_code = 200
    cache_text = ""
    revision = ""
    submitted_text = request.form.get("json_text", "") if request.method == "POST" else None

    # Version comparison and persistence share the cache writer's storage lock.
    with source_cache_transaction():
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
