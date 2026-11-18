import re
import uuid

from ._common import *
from ...online_scripts.source import (
    OnlineScriptCacheConflict,
    get_task_link_request,
    claim_task_link_request,
    merge_task_link_tasks,
    online_task_link_version,
    online_script_task_fingerprint,
    load_online_script_cache,
    list_task_link_requests,
    persist_task_link_request,
)
from ...online_scripts.constants import ONLINE_TASK_LINK_ACTIVE
from ...utils import now_str


def _task_link_error(script_id, message, status_code, proxy_id="", request_id="", retry=False):
    retry_form = ""
    if retry:
        hidden = f'<input type="hidden" name="request_id" value="{h(request_id)}">' if request_id else ""
        retry_form = f'''
<form class="inline-form" method="post" action="/online-scripts/task-link/{h(quote(script_id, safe=''))}">
    {hidden}
    <select name="proxy_id" aria-label="外部任务源代理">{proxy_select_options(proxy_id)}</select>
    <button class="btn btn-primary" type="submit">重新同步外部任务源</button>
</form>'''
    status_link = (
        f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">查询原同步请求</a>'
        if request_id else ""
    )
    body = page_header_card(
        "外部任务源同步未完成",
        help_html=h(message),
        actions_html=(
            retry_form
            + status_link
            + '<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
            + '<a class="btn btn-blue" href="/online-scripts/source">检查脚本源数据</a>'
        ),
    )
    return layout("外部任务源同步", "online_scripts", body), status_code


def _request_id(raw):
    value = str(raw or "").strip() or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("在线任务源同步请求标识无效，请刷新页面后重试")
    return value


def _record_update(record, state, *, error="", added=0, total=0):
    record.update({
        "state": state,
        "error": str(error or ""),
        "added": int(added),
        "total": int(total),
        "updated_at": now_str(),
    })
    return record


def _persist_failure(record, message, status_code, *, retry=True, state="failed"):
    _record_update(record, state, error=message, added=record.get("added", 0), total=record.get("total", 0))
    try:
        persist_task_link_request(record)
    except Exception as exc:
        return {
            "ok": False,
            "request_id": record["request_id"],
            "msg": f"{message}；同步结果回执无法保存，请查询原请求后再重试：{exc}",
            "retry_allowed": False,
            "resolved": False,
        }, 503
    return {
        "ok": False,
        "request_id": record["request_id"],
        "msg": message,
        "retry_allowed": retry,
        "resolved": True,
    }, status_code


def _submit_task_link(script_id, proxy_id, raw_request_id):
    try:
        request_id = _request_id(raw_request_id)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc), "request_id": "", "retry_allowed": False}, 400

    try:
        item = get_online_script(script_id)
    except Exception as exc:
        return {"ok": False, "request_id": request_id,
                "msg": f"在线脚本源缓存无法读取，未覆盖原文件：{exc}。请检查源数据后重试。",
                "retry_allowed": False}, 500
    if not item:
        return {"ok": False, "request_id": request_id,
                "msg": "脚本已不在本地缓存中，请刷新在线脚本列表。", "retry_allowed": False}, 404

    task_link = str(item.get("task_link") or "").strip()
    if not task_link:
        return {"ok": False, "request_id": request_id,
                "msg": "该脚本没有配置外部任务源 task_link，请检查脚本源数据。", "retry_allowed": False}, 400

    try:
        validate_proxy_id(proxy_id)
    except ValueError as exc:
        return {"ok": False, "request_id": request_id, "msg": str(exc), "retry_allowed": True}, 400
    except Exception as exc:
        return {"ok": False, "request_id": request_id,
                "msg": f"读取代理配置失败：{exc}", "retry_allowed": True}, 500

    expected_version = online_task_link_version(item)
    try:
        previous = get_task_link_request(request_id)
    except Exception as exc:
        return {"ok": False, "request_id": request_id,
                "msg": f"同步回执无法读取，未发起外部请求：{exc}", "retry_allowed": False}, 500
    if previous:
        if (previous.get("script_id") != script_id
                or previous.get("proxy_id") != proxy_id
                or previous.get("expected_version") != expected_version):
            return {"ok": False, "request_id": request_id,
                    "msg": "同步请求已绑定其他脚本、代理或旧版本，请刷新页面后重新选择。",
                    "retry_allowed": False}, 409
        if previous.get("state") == "completed":
            return {
                "ok": True,
                "request_id": request_id,
                "added": previous.get("added", 0),
                "total": previous.get("total", 0),
                "msg": "外部任务源同步已完成，未重复请求",
                "replayed": True,
            }, 200
        if previous.get("state") == "running":
            return {"ok": False, "request_id": request_id,
                    "msg": "该同步请求仍在处理中，请查询原请求后再重试。",
                    "retry_allowed": False}, 409
        if previous.get("state") == "unknown":
            return {"ok": False, "request_id": request_id,
                    "msg": "该同步请求的发布结果未知，请先查询原同步请求和当前缓存，确认后再创建新的同步请求。",
                    "retry_allowed": False}, 409

    expected_tasks_fingerprint = online_script_task_fingerprint(item)
    record = {
        "request_id": request_id,
        "script_id": script_id,
        "proxy_id": proxy_id,
        "expected_version": expected_version,
        "state": "prepared",
        "added": 0,
        "total": 0,
        "error": "",
        "updated_at": now_str(),
        "expected_tasks_fingerprint": expected_tasks_fingerprint,
        "published_tasks_fingerprint": "",
        "confirmation": "",
        "confirmation_fingerprint": "",
    }
    try:
        claimed_previous = claim_task_link_request(record)
    except Exception as exc:
        return {"ok": False, "request_id": request_id,
                "msg": f"同步请求无法保存，未发起外部请求：{exc}", "retry_allowed": True}, 500
    if claimed_previous:
        if (claimed_previous.get("script_id") != script_id
                or claimed_previous.get("proxy_id") != proxy_id
                or claimed_previous.get("expected_version") != expected_version):
            return {"ok": False, "request_id": request_id,
                    "msg": "同步请求已绑定其他脚本、代理或旧版本，请刷新页面后重新选择。",
                    "retry_allowed": False}, 409
        if claimed_previous.get("state") == "completed":
            return {
                "ok": True, "request_id": request_id,
                "added": claimed_previous.get("added", 0),
                "total": claimed_previous.get("total", 0),
                "msg": "外部任务源同步已完成，未重复请求", "replayed": True,
            }, 200
        if claimed_previous.get("state") == "unknown":
            return {"ok": False, "request_id": request_id,
                    "msg": "该同步请求的发布结果未知，请先查询原同步请求和当前缓存，确认后再创建新的同步请求。",
                    "retry_allowed": False}, 409
        return {"ok": False, "request_id": request_id,
                "msg": "该同步请求仍在处理中，请查询原请求后再重试。",
                "retry_allowed": False}, 409

    try:
        _record_update(record, "running")
        persist_task_link_request(record)
    except Exception as exc:
        return {"ok": False, "request_id": request_id,
                "msg": f"同步请求无法保存，未发起外部请求：{exc}", "retry_allowed": True}, 500

    ONLINE_TASK_LINK_ACTIVE.add(request_id)
    try:
        try:
            linked_tasks = fetch_task_link_tasks(item, proxy_id=proxy_id, timeout=20)
        except Exception as exc:
            message = f"外部任务源请求失败：{exc}。请检查任务源地址、代理和网络后重试。"
            return _persist_failure(record, message, 502)

        if not linked_tasks:
            return _persist_failure(
                record,
                "外部任务源没有可用任务，请检查任务字段后重新同步。",
                422,
            )

        try:
            added, total = merge_task_link_tasks(item, linked_tasks)
        except OnlineScriptCacheConflict as exc:
            return _persist_failure(record, str(exc), 409, retry=False)
        except Exception as exc:
            message = f"外部任务源发布结果无法确认：{exc}。请查询同步回执和当前缓存后再重试。"
            return _persist_failure(record, message, 503, retry=False, state="unknown")

        try:
            merged_item = next(
                one for one in load_online_script_cache() if one.get("id") == script_id
            )
            published_tasks_fingerprint = online_script_task_fingerprint(merged_item)
        except Exception as exc:
            return _persist_failure(record, f"任务已可能合并，但当前任务指纹无法确认：{exc}", 503, retry=False, state="unknown")
        _record_update(record, "completed", added=added, total=total)
        record["published_tasks_fingerprint"] = published_tasks_fingerprint
        try:
            persist_task_link_request(record)
        except Exception as exc:
            try:
                actual = get_task_link_request(request_id)
            except Exception as read_exc:
                return {
                    "ok": False, "request_id": request_id,
                    "msg": f"外部任务源已发布，但同步回执无法确认：{exc}；{read_exc}",
                    "retry_allowed": False, "resolved": False,
                }, 503
            if not actual or actual.get("state") != "completed":
                # Keep the post-merge fingerprint when the completion receipt itself was lost.
                # A later status query can then confirm the cache without fetching again.
                _record_update(record, "unknown", added=added, total=total)
                record["published_tasks_fingerprint"] = published_tasks_fingerprint
                record["error"] = "外部任务源已可能合并，回执写入失败，请检查当前任务合并"
                try:
                    persist_task_link_request(record)
                except Exception:
                    pass
                return {
                    "ok": False, "request_id": request_id,
                    "msg": f"外部任务源发布结果无法确认：{exc}，请查询当前缓存后再决定是否重试。",
                    "retry_allowed": False, "resolved": False,
                }, 503
            return {
                "ok": True, "request_id": request_id, "added": added, "total": total,
                "msg": f"外部任务源同步完成，新增 {added} 个任务，当前共 {total} 个任务；回执确认异常，请查询原请求",
                "diagnostic": f"同步已完成，但回执确认异常：{exc}",
            }, 200
        return {
            "ok": True,
            "request_id": request_id,
            "added": added,
            "total": total,
            "msg": f"外部任务源同步完成，新增 {added} 个任务，当前共 {total} 个任务",
        }, 200
    finally:
        ONLINE_TASK_LINK_ACTIVE.discard(request_id)


def _render_task_link_result(script_id, payload, status_code, proxy_id):
    if payload.get("ok"):
        return redirect(url_for(
            "online_scripts.online_scripts_page",
            msg=payload.get("msg") or "外部任务源同步完成",
        ))
    return _task_link_error(
        script_id,
        payload.get("msg") or "外部任务源同步未完成",
        status_code,
        proxy_id=proxy_id,
        request_id=payload.get("request_id", ""),
        retry=bool(payload.get("retry_allowed")),
    )


def _confirm_task_link(request_id):
    """Resolve an interrupted merge by comparing the bound cache fingerprints."""
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return {"ok": False, "msg": "在线任务源同步请求标识无效"}, 400
    try:
        record = get_task_link_request(request_id)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"同步回执无法读取：{exc}"}, 500
    if not record:
        return {"ok": False, "request_id": request_id, "msg": "同步请求不存在"}, 404

    confirmation = record.get("confirmation")
    if confirmation in ("merged", "not_merged"):
        return {
            "ok": True,
            "request_id": request_id,
            "state": record.get("state"),
            "confirmation": confirmation,
            "confirmation_fingerprint": record.get("confirmation_fingerprint", ""),
            "retry_allowed": confirmation == "not_merged",
            "replayed": True,
            "msg": "已确认任务已合并" if confirmation == "merged" else "已确认任务尚未合并，可继续原同步请求",
        }, 200
    if record.get("state") != "unknown":
        return {
            "ok": False,
            "request_id": request_id,
            "state": record.get("state"),
            "msg": "当前同步请求没有需要确认的未知发布结果",
            "retry_allowed": record.get("state") == "failed",
        }, 409

    try:
        current_items = load_online_script_cache()
        current_item = next(
            (one for one in current_items if one.get("id") == record.get("script_id")),
            None,
        )
        if current_item is None:
            return {
                "ok": False, "request_id": request_id,
                "msg": "脚本已从当前缓存删除，无法确认任务是否合并；未修改任务。",
            }, 409
        if online_task_link_version(current_item) != record.get("expected_version"):
            return {
                "ok": False, "request_id": request_id,
                "msg": "脚本源或任务源已变化，无法安全确认；未修改任务。",
            }, 409
        current_fingerprint = online_script_task_fingerprint(current_item)
    except Exception as exc:
        return {
            "ok": False, "request_id": request_id,
            "msg": f"当前任务缓存无法确认：{exc}；未修改任务。",
        }, 503

    published = str(record.get("published_tasks_fingerprint") or "")
    expected = str(record.get("expected_tasks_fingerprint") or "")
    if published and current_fingerprint == published:
        _record_update(record, "completed")
        record["confirmation"] = "merged"
        record["confirmation_fingerprint"] = current_fingerprint
        record["error"] = ""
        msg = "已确认外部任务源已合并，未重复抓取或追加任务"
        retry_allowed = False
    elif not published and expected and current_fingerprint == expected:
        _record_update(record, "failed")
        record["confirmation"] = "not_merged"
        record["confirmation_fingerprint"] = current_fingerprint
        record["error"] = "已确认外部任务源尚未合并，可继续原同步请求"
        msg = "已确认任务尚未合并，可继续原同步请求"
        retry_allowed = True
    else:
        return {
            "ok": False, "request_id": request_id,
            "msg": "当前任务指纹与预期结果不一致，无法安全确认；未修改任务。",
        }, 409

    try:
        persist_task_link_request(record)
    except Exception as exc:
        return {
            "ok": False, "request_id": request_id,
            "msg": f"确认结果无法保存：{exc}；未改变任务内容。",
        }, 503
    return {
        "ok": True,
        "request_id": request_id,
        "state": record["state"],
        "confirmation": record["confirmation"],
        "confirmation_fingerprint": record["confirmation_fingerprint"],
        "retry_allowed": retry_allowed,
        "msg": msg,
    }, 200


@bp.route("/online-scripts/task-link/<script_id>", methods=["POST"])
def online_scripts_pull_task_link(script_id):
    proxy_id = request.form.get("proxy_id", "").strip()
    payload, status_code = _submit_task_link(script_id, proxy_id, request.form.get("request_id"))
    return _render_task_link_result(script_id, payload, status_code, proxy_id)


@bp.route("/api/online-scripts/task-link/<script_id>", methods=["POST"])
def api_online_scripts_pull_task_link(script_id):
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    payload, status_code = _submit_task_link(
        script_id,
        str(data.get("proxy_id", "") or "").strip(),
        data.get("request_id"),
    )
    return jsonify(payload), status_code


@bp.route("/api/online-scripts/task-link-status/<request_id>")
def api_online_scripts_task_link_status(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return jsonify(ok=False, msg="在线任务源同步请求标识无效"), 400
    try:
        record = get_task_link_request(request_id)
    except Exception as exc:
        return jsonify(ok=False, msg=f"同步回执无法读取：{exc}"), 500
    if not record:
        return jsonify(ok=False, msg="同步请求不存在"), 404
    return jsonify(ok=True, **record)


@bp.route("/api/online-scripts/task-link-history")
def api_online_scripts_task_link_history():
    try:
        items = list_task_link_requests(
            request.args.get("limit", 50),
            request.args.get("script_id", ""),
        )
    except ValueError as exc:
        return jsonify(ok=False, msg=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, msg=f"同步历史无法读取：{exc}"), 500
    return jsonify(ok=True, items=items)


@bp.route("/api/online-scripts/task-link-confirm/<request_id>", methods=["POST"])
def api_online_scripts_task_link_confirm(request_id):
    payload, status_code = _confirm_task_link(request_id)
    return jsonify(payload), status_code


@bp.route("/online-scripts/task-link-confirm/<request_id>", methods=["POST"])
def online_scripts_task_link_confirm(request_id):
    payload, status_code = _confirm_task_link(request_id)
    if payload.get("ok"):
        return redirect(url_for(
            "online_scripts.online_scripts_task_link_status",
            request_id=request_id,
        ))
    body = page_header_card(
        "外部任务源同步确认失败",
        help_html=h(payload.get("msg") or "无法确认当前任务合并结果"),
        actions_html=(
            f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">返回同步状态</a>'
            '<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
        ),
    )
    return layout("外部任务源同步确认", "online_scripts", body), status_code


@bp.route("/online-scripts/task-link-status/<request_id>")
def online_scripts_task_link_status(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        body = page_header_card(
            "外部任务源同步状态",
            help_html="在线任务源同步请求标识无效，请返回在线脚本页面重新发起请求。",
            actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        )
        return layout("外部任务源同步状态", "online_scripts", body), 400
    try:
        record = get_task_link_request(request_id)
    except Exception as exc:
        body = page_header_card(
            "外部任务源同步状态",
            help_html=f"同步回执无法读取：{h(exc)}。请修复存储后重新查询。",
            actions_html=(
                f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">重新查询</a>'
                '<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
            ),
        )
        return layout("外部任务源同步状态", "online_scripts", body), 500
    if not record:
        body = page_header_card(
            "外部任务源同步状态",
            help_html="同步请求不存在，可能已被清理或请求标识有误。",
            actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        )
        return layout("外部任务源同步状态", "online_scripts", body), 404

    state = str(record.get("state") or "unknown")
    labels = {
        "prepared": "已准备，尚未确认外部请求",
        "running": "正在请求外部任务源",
        "failed": "请求失败，可继续原同步请求",
        "unknown": "发布结果未知，请勿直接重发",
        "completed": "同步已完成",
    }
    action_html = ""
    if state == "failed":
        action_html = f'''
<form class="inline-form" method="post" action="/online-scripts/task-link/{h(quote(record.get("script_id", ""), safe=""))}">
    <input type="hidden" name="request_id" value="{h(request_id)}">
    <input type="hidden" name="proxy_id" value="{h(record.get("proxy_id", ""))}">
    <button class="btn btn-primary" type="submit">继续原同步请求</button>
</form>'''
    elif state == "unknown":
        action_html = f'''
<form class="inline-form" method="post" action="/online-scripts/task-link-confirm/{h(request_id)}">
    <button class="btn btn-primary" type="submit">检查当前任务合并</button>
</form>
<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">刷新查询</a>'''
    elif state in ("prepared", "running"):
        action_html = f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">刷新查询</a>'
    action_html += '<a class="btn btn-gray" href="/online-scripts/task-link-history">同步历史</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
    details = f'''
<section class="section">
    <div class="card">
        <h2 class="section-title">{h(labels.get(state, state))}</h2>
        <dl class="detail-list">
            <dt>请求标识</dt><dd><code>{h(request_id)}</code></dd>
            <dt>脚本</dt><dd>{h(record.get("script_id", "-"))}</dd>
            <dt>代理</dt><dd>{h(record.get("proxy_id") or "直连")}</dd>
            <dt>任务数</dt><dd>{h(record.get("total", 0))}（新增 {h(record.get("added", 0))}）</dd>
            <dt>更新时间</dt><dd>{h(record.get("updated_at") or "-")}</dd>
        </dl>
        {f'<p role="alert">{h(record.get("error"))}</p>' if record.get("error") else ''}
    </div>
</section>'''
    body = page_header_card(
        "外部任务源同步状态",
        help_html="该页面只查询已保存的同步回执，不会重复发起外部请求。未知状态请先检查当前任务合并；确认未合并后再执行重试。",
        actions_html=action_html,
    ) + details
    return layout("外部任务源同步状态", "online_scripts", body)


@bp.route("/online-scripts/task-link-history")
def online_scripts_task_link_history():
    script_id = str(request.args.get("script_id") or "").strip()
    try:
        items = list_task_link_requests(request.args.get("limit", 50), script_id)
    except ValueError as exc:
        body = page_header_card(
            "外部任务源同步历史",
            help_html=h(exc),
            actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        )
        return layout("外部任务源同步历史", "online_scripts", body), 400
    except Exception as exc:
        body = page_header_card(
            "外部任务源同步历史",
            help_html=f"同步历史无法读取：{h(exc)}。请修复回执存储后重试。",
            actions_html='<a class="btn btn-blue" href="/online-scripts/task-link-history">重新读取</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        )
        return layout("外部任务源同步历史", "online_scripts", body), 500

    labels = {
        "prepared": "已准备",
        "running": "运行中",
        "failed": "失败，可重试",
        "unknown": "结果未知",
        "completed": "已完成",
    }
    rows = []
    for record in items:
        request_id = str(record.get("request_id") or "")
        state = str(record.get("state") or "unknown")
        actions = f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">查看状态</a>'
        if state == "completed" or record.get("confirmation") == "merged":
            actions += f'<a class="btn btn-gray" href="/online-scripts/install-select/{h(quote(str(record.get("script_id") or ""), safe=""))}">选择已同步任务</a>'
        rows.append(
            "<tr>"
            f"<td><code>{h(request_id)}</code></td>"
            f"<td>{h(record.get('script_id') or '-')}</td>"
            f"<td>{h(labels.get(state, state))}</td>"
            f"<td>{h(record.get('total', 0))}（新增 {h(record.get('added', 0))}）</td>"
            f"<td>{h(record.get('updated_at') or '-')}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
    table = (
        '<div class="table-wrap"><table><thead><tr><th>请求标识</th><th>脚本</th><th>状态</th>'
        '<th>任务数</th><th>更新时间</th><th>操作</th></tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="6">暂无同步历史</td></tr>')
        + "</tbody></table></div>"
    )
    body = page_header_card(
        "外部任务源同步历史",
        help_html="历史只读取已保存回执，不会发起外部任务源请求；未知状态请进入详情检查当前任务合并。",
        actions_html='<a class="btn btn-blue" href="/online-scripts">返回在线脚本</a>',
    ) + f'<section class="section"><div class="card">{table}</div></section>'
    return layout("外部任务源同步历史", "online_scripts", body)
