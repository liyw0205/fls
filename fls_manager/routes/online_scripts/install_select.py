from ._common import *
import re
import uuid

from ...models import TaskSchedulerReloadError
from ...scheduler import reload_scheduler
from ...online_scripts.constants import (
    claim_online_task_import_request,
    get_online_task_import_request,
    list_online_task_import_requests,
    persist_online_task_import_request,
    ONLINE_TASK_IMPORT_LOCK,
    ONLINE_TASK_IMPORT_ACTIVE,
)
from ...online_scripts.tasks import online_task_import_version
from ...online_scripts.source import get_task_link_request, online_task_link_version
from ...utils import now_str


def _task_import_error(script_id, message, status_code, persisted=False, request_id="", selected_task_indexes=None, enable_task=False):
    scheduler_action = ""
    if persisted:
        reload_url = f"/online-scripts/import-tasks-reload/{h(request_id)}" if request_id else "/scheduler/reload?back=/tasks"
        scheduler_action = '''
<form class="inline-form" method="post" action="RELOAD_URL">
    <button class="btn btn-primary" type="submit">重新加载调度器</button>
        </form>'''.replace("RELOAD_URL", h(reload_url))
        if request_id:
            scheduler_action += '''
<form class="inline-form" method="post" action="/scheduler/reload?back=/tasks">
    <button class="btn btn-gray" type="submit">通用重载调度器</button>
</form>'''
    status_link = (
        f'<a class="btn btn-blue" href="/online-scripts/import-tasks-status/{h(request_id)}">查询原导入请求</a>'
        if request_id else ""
    )
    retry_form = ""
    if request_id and selected_task_indexes and not persisted:
        fields = f'<input type="hidden" name="request_id" value="{h(request_id)}">'
        fields += "".join(f'<input type="hidden" name="task_indexes" value="{h(index)}">' for index in selected_task_indexes)
        if enable_task:
            fields += '<input type="hidden" name="enable_task" value="1">'
        retry_form = f'<form class="inline-form" method="post" action="/online-scripts/import-tasks/{h(script_id)}">{fields}<button class="btn btn-orange" type="submit">继续原导入请求</button></form>'
    body = page_header_card(
        "在线任务导入未完成",
        help_html=h(message),
        actions_html=(
            retry_form
            + status_link
            +
            f'<a class="btn btn-gray" href="/online-scripts/install-select/{h(script_id)}">重新选择任务</a>'
            '<a class="btn btn-blue" href="/tasks">查看已保存任务</a>'
            '<a class="btn btn-orange" href="/online-scripts/source">检查脚本源数据</a>'
            + scheduler_action
        ),
    )
    return layout("在线任务导入", "online_scripts", body), status_code


def _import_request_id(raw):
    value = str(raw or "").strip() or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("在线任务导入请求标识无效，请刷新选择页后重试")
    return value


def _data_values(data, key):
    if hasattr(data, "getlist"):
        return data.getlist(key)
    value = data.get(key)
    if isinstance(value, list):
        return value
    return [] if value is None else [value]


def _selected_indexes_from_data(item, data):
    select_mode = str(data.get("select_mode", "") or "").strip()
    excluded_raw = str(data.get("excluded_task_indexes", "") or "").strip()
    if select_mode == "all":
        excluded = set()
        for value in excluded_raw.replace("，", ",").split(","):
            value = value.strip()
            if not value:
                continue
            if not value.isdecimal():
                raise ValueError("任务排除项必须为整数")
            excluded.add(int(value))
        if any(index < 1 or index > len(online_script_task_crons(item)) for index in excluded):
            raise ValueError("任务排除项已失效")
        return [str(index) for index in range(1, len(online_script_task_crons(item)) + 1) if index not in excluded]
    if select_mode:
        raise ValueError("任务选择模式无效")
    return [str(value) for value in _data_values(data, "task_indexes")]


def _import_record_update(record, state, message="", scheduler_reload_error=""):
    record.update({
        "state": state,
        "message": str(message or ""),
        "scheduler_reload_error": str(scheduler_reload_error or ""),
        "updated_at": now_str(),
    })
    return record


def _import_failure(record, message, status_code, *, state="failed", persisted=False):
    _import_record_update(record, state, message, message if state == "scheduler_pending" else "")
    try:
        persist_online_task_import_request(record)
    except Exception as exc:
        return {
            "ok": False,
            "request_id": record["request_id"],
            "msg": f"{message}；导入回执无法保存，请查询任务文件和调度器状态：{exc}",
            "resolved": False,
            "scheduler_reload_required": persisted,
        }, 503
    return {
        "ok": False,
        "request_id": record["request_id"],
        "msg": message,
        "resolved": True,
        "scheduler_reload_required": persisted,
    }, status_code


def _run_online_task_import(record, item, enable_task, selected_task_indexes, request_id):
    ONLINE_TASK_IMPORT_ACTIVE.add(request_id)
    try:
        try:
            _import_record_update(record, "running")
            persist_online_task_import_request(record)
            ok, message = import_task_if_needed(
                item,
                log_file=None,
                enable_task=enable_task,
                selected_task_indexes=selected_task_indexes,
                raise_on_reload_error=True,
            )
        except TaskSchedulerReloadError as exc:
            return _import_failure(
                record,
                f"任务已持久化，但调度器重载失败：{exc}。请重新加载调度器，无需重复导入。",
                503,
                state="scheduler_pending",
                persisted=True,
            )
        except OnlineTaskValidationError as exc:
            return _import_failure(record, str(exc), 400)
        except Exception as exc:
            return _import_failure(record, f"任务导入失败：{exc}。请检查文件权限和磁盘空间后重试。", 500)

        message = message or "任务导入已完成"
        _import_record_update(record, "completed", message)
        try:
            persist_online_task_import_request(record)
        except Exception as exc:
            actual = None
            try:
                actual = get_online_task_import_request(request_id)
            except Exception:
                pass
            if not actual or actual.get("state") != "completed":
                return {"ok": False, "request_id": request_id, "msg": f"任务已导入，但完成回执无法确认：{exc}，请查询原请求。", "retry_allowed": False}, 503
        if not ok:
            return {"ok": False, "request_id": request_id, "msg": message, "retry_allowed": False}, 409
        return {"ok": True, "request_id": request_id, "msg": message}, 200
    finally:
        ONLINE_TASK_IMPORT_ACTIVE.discard(request_id)


def _submit_online_task_import(script_id, data):
    try:
        request_id = _import_request_id(data.get("request_id"))
    except ValueError as exc:
        return {"ok": False, "request_id": "", "msg": str(exc), "retry_allowed": False}, 400
    try:
        item = get_online_script(script_id)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"在线脚本源缓存无法读取，未覆盖原文件：{exc}", "retry_allowed": False}, 500

    if not item:
        return {"ok": False, "request_id": request_id, "msg": "脚本已不在本地缓存中，请同步在线脚本源后重试。", "retry_allowed": False}, 404

    if not script_has_task(item):
        return {"ok": False, "request_id": request_id, "msg": "该脚本没有可导入的任务，请检查在线脚本源。", "retry_allowed": False}, 400

    enable_task = data.get("enable_task") in (True, 1, "1", "true", "True")

    try:
        selected_task_indexes = _selected_indexes_from_data(item, data)
        selected_task_indexes = validated_install_task_indexes(item, selected_task_indexes)
        validate_online_task_import(item, selected_task_indexes)
    except ValueError as exc:
        return {"ok": False, "request_id": request_id, "msg": f"{exc}。请重新选择任务后重试。", "retry_allowed": False}, 400
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"任务校验失败：{exc}。请检查服务状态后重试。", "retry_allowed": False}, 500

    expected_version = online_task_import_version(item)
    try:
        previous = get_online_task_import_request(request_id)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"导入回执无法读取，未写入任务：{exc}", "retry_allowed": False}, 500
    if previous:
        if (previous.get("script_id") != script_id
                or previous.get("expected_version") != expected_version
                or previous.get("selected_task_indexes") != selected_task_indexes
                or previous.get("enable_task") != enable_task):
            return {"ok": False, "request_id": request_id, "msg": "导入请求已绑定其他脚本、版本或任务选择，请重新选择。", "retry_allowed": False}, 409
        if previous.get("state") == "completed":
            return {"ok": True, "request_id": request_id, "msg": previous.get("message") or "任务导入已完成，未重复写入", "replayed": True}, 200
        if previous.get("state") == "scheduler_pending":
            return {"ok": False, "request_id": request_id, "msg": previous.get("message") or "任务已保存，但调度器尚未恢复", "scheduler_reload_required": True, "reload_url": f"/api/online-scripts/import-tasks-reload/{request_id}"}, 503
        if previous.get("state") in ("running", "prepared", "unknown"):
            return {"ok": False, "request_id": request_id, "msg": "原导入请求仍在处理中或结果未知，请先查询原请求。", "retry_allowed": False}, 409

    record = {
        "request_id": request_id,
        "script_id": script_id,
        "expected_version": expected_version,
        "selected_task_indexes": list(selected_task_indexes),
        "enable_task": bool(enable_task),
        "state": "prepared",
        "message": "",
        "scheduler_reload_error": "",
        "updated_at": now_str(),
    }
    try:
        claimed_previous = claim_online_task_import_request(record)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"导入请求无法保存，未写入任务：{exc}", "retry_allowed": True}, 500
    if claimed_previous:
        if (claimed_previous.get("script_id") != script_id
                or claimed_previous.get("expected_version") != expected_version
                or claimed_previous.get("selected_task_indexes") != selected_task_indexes
                or claimed_previous.get("enable_task") != enable_task):
            return {"ok": False, "request_id": request_id, "msg": "导入请求已绑定其他脚本、版本或任务选择，请重新选择。", "retry_allowed": False}, 409
        if claimed_previous.get("state") == "completed":
            return {"ok": True, "request_id": request_id, "msg": claimed_previous.get("message") or "任务导入已完成，未重复写入", "replayed": True}, 200
        if claimed_previous.get("state") == "scheduler_pending":
            return {"ok": False, "request_id": request_id, "msg": claimed_previous.get("message") or "任务已保存，但调度器尚未恢复", "scheduler_reload_required": True, "reload_url": f"/api/online-scripts/import-tasks-reload/{request_id}"}, 503
        return {"ok": False, "request_id": request_id, "msg": "原导入请求仍在处理中或结果未知，请先查询原请求。", "retry_allowed": False}, 409

    try:
        latest_item = get_online_script(script_id)
    except Exception as exc:
        return _import_failure(record, f"脚本源版本无法确认，未写入任务：{exc}", 500)
    if not latest_item or online_task_import_version(latest_item) != expected_version:
        return _import_failure(record, "脚本源已更新或删除，未写入旧页面选择的任务。请重新选择后重试。", 409)

    return _run_online_task_import(record, item, enable_task, selected_task_indexes, request_id)


def _render_import_result(script_id, payload, status_code, data):
    if payload.get("ok"):
        return redirect(url_for("online_scripts.online_scripts_page", msg=payload.get("msg") or "任务导入已完成"))
    selected = _data_values(data, "task_indexes")
    if str(data.get("select_mode", "") or "").strip() == "all":
        try:
            selected = _selected_indexes_from_data(get_online_script(script_id) or {}, data)
        except Exception:
            selected = []
    return _task_import_error(
        script_id,
        payload.get("msg") or "任务导入未完成",
        status_code,
        persisted=bool(payload.get("scheduler_reload_required")),
        request_id=payload.get("request_id", ""),
        selected_task_indexes=selected,
        enable_task=data.get("enable_task") in (True, 1, "1", "true", "True"),
    )


@bp.route("/online-scripts/import-tasks/<script_id>", methods=["POST"])
def online_scripts_import_tasks_only(script_id):
    payload, status_code = _submit_online_task_import(script_id, request.form)
    return _render_import_result(script_id, payload, status_code, request.form)


@bp.route("/api/online-scripts/import-tasks/<script_id>", methods=["POST"])
def api_online_scripts_import_tasks_only(script_id):
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    payload, status_code = _submit_online_task_import(script_id, data)
    return jsonify(payload), status_code


@bp.route("/api/online-scripts/import-tasks-status/<request_id>")
def api_online_scripts_import_tasks_status(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return jsonify(ok=False, msg="在线任务导入请求标识无效"), 400
    try:
        record = get_online_task_import_request(request_id)
    except Exception as exc:
        return jsonify(ok=False, msg=f"导入回执无法读取：{exc}"), 500
    if not record:
        return jsonify(ok=False, msg="导入请求不存在"), 404
    return jsonify(ok=True, **record)


@bp.route("/api/online-scripts/import-tasks-history")
def api_online_scripts_import_tasks_history():
    try:
        items = list_online_task_import_requests(
            request.args.get("limit", 50),
            request.args.get("script_id", ""),
        )
    except ValueError as exc:
        return jsonify(ok=False, msg=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, msg=f"导入历史无法读取：{exc}"), 500
    return jsonify(ok=True, items=items)


@bp.route("/api/online-scripts/import-tasks-reload/<request_id>", methods=["POST"])
def api_online_scripts_import_tasks_reload(request_id):
    payload, status_code = _reload_import_scheduler(request_id)
    return jsonify(payload), status_code


def _reload_import_scheduler(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return {"ok": False, "msg": "在线任务导入请求标识无效"}, 400
    with ONLINE_TASK_IMPORT_LOCK:
        try:
            record = get_online_task_import_request(request_id)
        except Exception as exc:
            return {"ok": False, "msg": f"导入回执无法读取：{exc}"}, 500
        if not record:
            return {"ok": False, "msg": "导入请求不存在"}, 404
        if record.get("state") == "completed":
            return {"ok": True, "request_id": request_id, "script_id": record.get("script_id", ""), "msg": "调度器已恢复，未重复导入", "replayed": True}, 200
        if record.get("state") != "scheduler_pending":
            return {"ok": False, "request_id": request_id, "script_id": record.get("script_id", ""), "msg": "当前导入没有待恢复的调度器状态"}, 409
        try:
            reload_scheduler()
        except Exception as exc:
            return {"ok": False, "request_id": request_id, "script_id": record.get("script_id", ""), "msg": f"调度器重载失败：{exc}，任务文件保持已保存状态"}, 503
        _import_record_update(record, "completed", "任务已保存，调度器已恢复")
        try:
            persist_online_task_import_request(record)
        except Exception as exc:
            return {"ok": False, "request_id": request_id, "script_id": record.get("script_id", ""), "msg": f"调度器已恢复，但导入回执无法保存：{exc}"}, 503
    return {"ok": True, "request_id": request_id, "script_id": record.get("script_id", ""), "msg": "任务已保存，调度器已恢复"}, 200


@bp.route("/online-scripts/import-tasks-reload/<request_id>", methods=["POST"])
def online_scripts_import_tasks_reload(request_id):
    payload, status_code = _reload_import_scheduler(request_id)
    if payload.get("ok"):
        return redirect(url_for("online_scripts.online_scripts_page", msg=payload.get("msg")))
    return _task_import_error(payload.get("script_id", ""), payload.get("msg", "导入恢复失败"), status_code, persisted=True, request_id=request_id)


@bp.route("/online-scripts/import-tasks-status/<request_id>")
def online_scripts_import_tasks_status(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return _task_import_error("", "在线任务导入请求标识无效", 400)
    try:
        record = get_online_task_import_request(request_id)
    except Exception as exc:
        return _task_import_error("", f"导入回执无法读取：{exc}", 500)
    if not record:
        return _task_import_error("", "导入请求不存在", 404)
    state = record.get("state") or "unknown"
    labels = {
        "prepared": "已准备，尚未确认写入",
        "running": "正在导入任务",
        "failed": "导入失败，可继续原请求",
        "scheduler_pending": "任务已保存，调度器待恢复",
        "unknown": "导入结果未知，请勿重复写入",
        "completed": "任务导入已完成",
    }
    actions = '<a class="btn btn-gray" href="/online-scripts/import-tasks-history">导入历史</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
    if state == "failed":
        fields = f'<input type="hidden" name="request_id" value="{h(request_id)}">'
        fields += "".join(f'<input type="hidden" name="task_indexes" value="{h(index)}">' for index in record.get("selected_task_indexes", []))
        if record.get("enable_task"):
            fields += '<input type="hidden" name="enable_task" value="1">'
        actions = f'<form class="inline-form" method="post" action="/online-scripts/import-tasks/{h(record.get("script_id", ""))}">{fields}<button class="btn btn-primary" type="submit">继续原导入请求</button></form>' + actions
    elif state == "scheduler_pending":
        actions = f'<form class="inline-form" method="post" action="/online-scripts/import-tasks-reload/{h(request_id)}"><button class="btn btn-primary" type="submit">仅重新加载调度器</button></form>' + actions
    elif state in ("prepared", "running", "unknown"):
        actions = f'<a class="btn btn-blue" href="/online-scripts/import-tasks-status/{h(request_id)}">刷新查询</a>' + actions
    body = page_header_card(
        "在线任务导入状态",
        help_html="该页面只读取导入回执，不会重复写入任务。调度器待恢复时仅执行重载，不会再次导入。",
        actions_html=actions,
    )
    body += f'''<section class="section"><div class="card"><h2 class="section-title">{h(labels.get(state, state))}</h2><p>请求标识：<code>{h(request_id)}</code></p><p>脚本：{h(record.get("script_id"))}</p><p>更新时间：{h(record.get("updated_at"))}</p><p>{h(record.get("message") or record.get("scheduler_reload_error") or "-")}</p></div></section>'''
    return layout("在线任务导入状态", "online_scripts", body)


@bp.route("/online-scripts/import-tasks-history")
def online_scripts_import_tasks_history():
    script_id = str(request.args.get("script_id") or "").strip()
    try:
        items = list_online_task_import_requests(request.args.get("limit", 50), script_id)
    except ValueError as exc:
        body = page_header_card(
            "在线任务导入历史",
            help_html=h(exc),
            actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        )
        return layout("在线任务导入历史", "online_scripts", body), 400
    except Exception as exc:
        body = page_header_card(
            "在线任务导入历史",
            help_html=f"导入历史无法读取：{h(exc)}。请修复回执存储后重试。",
            actions_html='<a class="btn btn-blue" href="/online-scripts/import-tasks-history">重新读取</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        )
        return layout("在线任务导入历史", "online_scripts", body), 500

    labels = {
        "prepared": "已准备",
        "running": "运行中",
        "failed": "失败，可重试",
        "scheduler_pending": "调度器待恢复",
        "unknown": "结果未知",
        "completed": "已完成",
    }
    rows = []
    for record in items:
        request_id = str(record.get("request_id") or "")
        state = str(record.get("state") or "unknown")
        actions = f'<a class="btn btn-blue" href="/online-scripts/import-tasks-status/{h(request_id)}">查看状态</a>'
        if state == "scheduler_pending":
            actions += f'<form class="inline-form" method="post" action="/online-scripts/import-tasks-reload/{h(request_id)}"><button class="btn btn-primary" type="submit">仅重载调度器</button></form>'
        elif state == "failed":
            fields = f'<input type="hidden" name="request_id" value="{h(request_id)}">'
            fields += "".join(f'<input type="hidden" name="task_indexes" value="{h(index)}">' for index in record.get("selected_task_indexes", []))
            if record.get("enable_task"):
                fields += '<input type="hidden" name="enable_task" value="1">'
            actions += f'<form class="inline-form" method="post" action="/online-scripts/import-tasks/{h(record.get("script_id") or "")}">{fields}<button class="btn btn-orange" type="submit">继续原导入请求</button></form>'
        rows.append(
            "<tr>"
            f"<td><code>{h(request_id)}</code></td>"
            f"<td>{h(record.get('script_id') or '-')}</td>"
            f"<td>{h(labels.get(state, state))}</td>"
            f"<td>{h(','.join(record.get('selected_task_indexes') or []) or '-')}</td>"
            f"<td>{h(record.get('updated_at') or '-')}</td>"
            f"<td>{actions}</td>"
            "</tr>"
        )
    table = (
        '<div class="table-wrap"><table><thead><tr><th>请求标识</th><th>脚本</th><th>状态</th>'
        '<th>任务序号</th><th>更新时间</th><th>操作</th></tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="6">暂无导入历史</td></tr>')
        + "</tbody></table></div>"
    )
    body = page_header_card(
        "在线任务导入历史",
        help_html="历史只读取已保存回执，不会重复写入任务；调度器待恢复时只执行重载。",
        actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
    ) + f'<section class="section"><div class="card">{table}</div></section>'
    return layout("在线任务导入历史", "online_scripts", body)


@bp.route("/online-scripts/install-select/<script_id>")
def online_scripts_install_select(script_id):
    try:
        item = get_online_script(script_id)
    except Exception as exc:
        return online_script_cache_error("在线任务选择无法打开", exc)

    if not item:
        abort(404)

    task_link_request_id = str(request.args.get("task_link_request_id") or "").strip()
    task_link_record = None
    if task_link_request_id:
        if not re.fullmatch(r"[a-f0-9]{32}", task_link_request_id):
            return online_script_cache_error(
                "在线任务选择无法打开",
                "外部任务源同步请求标识无效，请从同步历史重新选择任务",
                back_url="/online-scripts/task-link-history",
            )[0], 400
        try:
            task_link_record = get_task_link_request(task_link_request_id)
        except Exception as exc:
            return online_script_cache_error(
                "在线任务选择无法打开",
                f"外部任务源同步回执无法读取：{exc}",
                back_url=f"/online-scripts/task-link-status/{h(task_link_request_id)}",
            )
        if not task_link_record:
            return online_script_cache_error(
                "在线任务选择无法打开",
                "外部任务源同步请求不存在或已被清理，请从同步历史重新选择",
                back_url="/online-scripts/task-link-history",
            )[0], 404
        if (
            task_link_record.get("script_id") != script_id
            or task_link_record.get("state") != "completed"
            or task_link_record.get("confirmation") not in ("", "merged")
            or online_task_link_version(item) != task_link_record.get("expected_version")
        ):
            return online_script_cache_error(
                "在线任务选择无法打开",
                "外部任务源同步尚未确认完成，或脚本源已变化；请先查询原同步请求后重新选择",
                back_url=f"/online-scripts/task-link-status/{h(task_link_request_id)}",
            )[0], 409

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
        task_rows = '<div class="empty-state" style="grid-column:1 / -1;"><p>暂无匹配任务</p></div>'
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
            validation_html = ""
            try:
                validate_online_task_import(item, [idx])
            except OnlineTaskValidationError as e:
                validation_html = f'<div class="notice red" role="status">{h(e)}</div>'

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
                    <b>任务配置文件：</b>{h(config_path)}
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

        {validation_html}

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

    task_link_notice = ""
    if task_link_record:
        task_link_notice = (
            f'外部任务源同步已确认完成，请求 <code>{h(task_link_request_id)}</code>；'
            f'<a href="/online-scripts/task-link-status/{h(task_link_request_id)}">查看同步回执</a>。<br>'
        )
    header_card = page_header_card(
        f'选择任务并安装：{item.get("name") or script_id}',
        help_html=f"""
                {task_link_notice}
                脚本 ID：{h(item.get("id"))}<br>
                脚本类型：{h(item.get("type"))}<br>
                保存名：{h(item.get("link_name"))}<br>
                共检测到 <b>{total_tasks}</b> 个可导入任务。默认全选全部任务。<br>
选择约 <b id="selectedTaskCount">{selected_count}</b> 个任务。
""",
        actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
    )

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

@media(max-width:767px) {{
    .fls-install-task-list {{
        grid-template-columns:1fr!important;
    }}
}}
</style>

<form method="post" action="/online-scripts/install/{h(script_id)}" id="onlineInstallSelectForm">
<input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
<input type="hidden" name="task_link_request_id" value="{h(task_link_request_id)}">
<input type="hidden" name="select_mode" value="all">
<input type="hidden" name="excluded_task_indexes" id="excludedTaskIndexes" value="{h(excluded_text)}">

{header_card}

<section class="section fls-form-section">
    <h2 class="section-title">安装选项</h2>

    <div class="form-grid">
        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id" aria-label="在线脚本安装使用的代理">{proxy_options}</select>
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
                不勾选“导入后启用任务”时，导入后的任务默认为禁用，需要到任务页面手动启用。
            </div>
        </div>
    </div>
</section>

<section class="section fls-form-section">
    <h2 class="section-title">搜索任务</h2>
    <div class="form-grid">
        <div class="form-item">
            <label>关键词</label>
            <input id="installTaskSearchInput" value="{h(task_q)}" placeholder="任务名 / Cron / 命令 / 备注 / 变量 / 序号" aria-label="搜索任务关键词">
        </div>

        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="button" onclick="flsInstallSearchTasks()">查询导入任务</button>
            <button class="btn btn-gray" type="button" onclick="flsInstallClearSearch()">重置任务筛选</button>
        </div>
    </div>
</section>

<section class="section fls-form-section">
    <div class="fls-install-select-head">
        <div>
            <h2 class="section-title">选择要导入的任务</h2>
            <div class="help">
                当前页显示 {display_start} - {display_end} / {filtered_total} 个匹配任务，每页 10 个。<br>
                默认全选全部任务，可以取消不需要导入的任务。
            </div>
        </div>

        <div class="fls-install-task-tools">
            <button class="btn btn-blue" type="button" onclick="flsInstallSelectCurrentPage(true)">当前页全选</button>
            <button class="btn btn-gray" type="button" onclick="flsInstallSelectCurrentPage(false)">当前页取消</button>
            <button class="btn btn-gray" type="button" onclick="flsInstallCancelAllGlobal()">全部取消选择</button>
            <button class="btn btn-primary" type="button" onclick="flsInstallSelectAllGlobal()">全部任务全选</button>
        </div>
    </div>

    <br>

    <div class="fls-install-task-list">
        {task_rows}
    </div>
</section>

{page_links_html}

<div class="notice red" id="onlineInstallApiError" role="status" hidden></div>
<a class="btn btn-primary" id="onlineInstallApiLogin" href="/login" hidden>重新登录</a>
<div class="notice blue" id="onlineInstallRecoveryStatus" role="status" hidden></div>
<a class="btn btn-orange" id="onlineInstallRecoveryLog" href="#" hidden>查看原安装日志</a>
<button class="btn btn-gray" id="onlineInstallRecoveryRetry" type="button" hidden>重新读取安装状态</button>

<section class="section fls-form-section fls-save-section">
    <div class="row-actions" aria-label="在线脚本导入操作">
        <div class="row-actions-primary">
            <button class="btn btn-primary" type="submit">开始下载安装</button>
            <button class="btn btn-orange" type="submit" formaction="/online-scripts/import-tasks/{h(script_id)}">立即导入所选任务</button>
        </div>
        <div class="row-actions-secondary">
            <a class="btn btn-gray" href="/online-scripts">取消导入</a>
        </div>
    </div>
</section>
</form>

<script>
window.FLS_TOTAL_TASKS = {total_tasks};
window.FLS_INSTALL_SCRIPT_ID = "{h(script_id)}";

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
        countEl.textContent = Math.max(0, window.FLS_TOTAL_TASKS - arr.length);
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

    for(let i = 1; i <= window.FLS_TOTAL_TASKS; i++){{
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

    const url = new URL(
        "/online-scripts/install-select/" + encodeURIComponent(window.FLS_INSTALL_SCRIPT_ID),
        window.location.origin
    );
    url.searchParams.set("task_page", page || "1");

    if(taskQ){{
        url.searchParams.set("task_q", taskQ);
    }}

    if(excluded){{
        url.searchParams.set("excluded", excluded);
    }}

    const taskLinkInput = document.querySelector('input[name="task_link_request_id"]');
    if(taskLinkInput && taskLinkInput.value){{
        url.searchParams.set("task_link_request_id", taskLinkInput.value);
    }}

    return url.toString();
}}

function flsInstallSearchTasks(){{
    window.location.href = flsInstallBuildUrl("1");
}}

function flsInstallClearSearch(){{
    flsSyncVisibleCheckboxesToExcluded();

    const excluded = document.getElementById("excludedTaskIndexes").value || "";
    const url = new URL(
        "/online-scripts/install-select/" + encodeURIComponent(window.FLS_INSTALL_SCRIPT_ID),
        window.location.origin
    );
    url.searchParams.set("task_page", "1");

    if(excluded){{
        url.searchParams.set("excluded", excluded);
    }}

    const taskLinkInput = document.querySelector('input[name="task_link_request_id"]');
    if(taskLinkInput && taskLinkInput.value){{
        url.searchParams.set("task_link_request_id", taskLinkInput.value);
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

    const submitter = e.submitter;
    const action = (submitter && submitter.getAttribute("formaction")) || this.getAttribute("action") || "";
    if(action.indexOf("/online-scripts/install/") < 0 || action.indexOf("/import-tasks/") >= 0) return;

    var importTask = document.querySelector('#onlineInstallSelectForm input[name="import_task"]');
    var excluded = flsParseExcluded();

    if(importTask && importTask.checked && excluded.size >= window.FLS_TOTAL_TASKS){{
        e.preventDefault();
        alert("已勾选导入任务，但没有选择任何任务");
        return false;
    }}

    e.preventDefault();
    flsInstallSubmitApi(action, this, submitter);
}});

async function flsInstallSubmitApi(action, form, submitter){{
    const errorEl = document.getElementById("onlineInstallApiError");
    const loginEl = document.getElementById("onlineInstallApiLogin");
    const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
    const button = submitter || form.querySelector('button[type="submit"]');
    if(button) button.disabled = true;
    if(errorEl){{ errorEl.hidden = true; errorEl.textContent = "正在提交安装请求..."; }}
    if(loginEl) loginEl.hidden = true;

    const requestIdInput = form.querySelector('input[name="request_id"]');
    const requestId = requestIdInput ? requestIdInput.value : "";
    if(requestId) sessionStorage.setItem("fls-online-install-request", JSON.stringify({{
        request_id: requestId, script_id: window.FLS_INSTALL_SCRIPT_ID, action: action
    }}));
    const data = new FormData(form);
    data.delete("visible_task_indexes");
    const apiAction = action.replace("/online-scripts/install/", "/api/online-scripts/install/");
    const headers = {{"X-Requested-With": "XMLHttpRequest"}};
    if(csrf) headers["X-CSRF-Token"] = csrf;
    try{{
        let response = await fetch(apiAction, {{method:"POST", body:data, headers, credentials:"same-origin"}});
        let json;
        try{{
            json = await flsReadJsonResponse(response);
        }}catch(readError){{
            if(response.status === 409 && readError && readError.payload) json = readError.payload;
            else throw readError;
        }}
        if(response.status === 409 && json && json.confirm_required){{
            if(!window.confirm("目标文件已存在，确定覆盖并继续安装吗？")){{
                if(button) button.disabled = false;
                if(errorEl){{ errorEl.hidden = false; errorEl.textContent = "已取消覆盖，未启动安装。"; }}
                return;
            }}
            data.set("force", "1");
            response = await fetch(apiAction, {{method:"POST", body:data, headers, credentials:"same-origin"}});
            json = await flsReadJsonResponse(response);
        }}
        if(!json || json.ok !== true) throw Object.assign(new Error(json && json.msg || "安装请求未完成"), {{status:response.status, payload:json}});
        if(json.install_id) sessionStorage.setItem("fls-online-install-request", JSON.stringify({{
            request_id: json.request_id || requestId, install_id: json.install_id,
            script_id: window.FLS_INSTALL_SCRIPT_ID
        }}));
        window.location.href = "/online-scripts/log/" + encodeURIComponent(json.install_id || json.request_id);
    }}catch(error){{
        const denied = error && (error.status === 401 || error.status === 403);
        const message = error && error.message ? error.message : String(error || "安装请求失败");
        if(errorEl){{
            errorEl.textContent = "安装请求失败：" + message + (denied ? "。请重新登录后查询原安装请求。" : "。请保留当前请求标识后重试或查看安装日志。");
            errorEl.hidden = false;
        }}
        if(loginEl){{
            loginEl.hidden = !denied;
            if(denied) loginEl.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        }}
        if(button) button.disabled = false;
    }}
}}

async function flsInstallRecoverPending(){{
    const statusEl = document.getElementById("onlineInstallRecoveryStatus");
    const loginEl = document.getElementById("onlineInstallApiLogin");
    const logEl = document.getElementById("onlineInstallRecoveryLog");
    const retryEl = document.getElementById("onlineInstallRecoveryRetry");
    let pending;
    try{{ pending = JSON.parse(sessionStorage.getItem("fls-online-install-request") || "null"); }}catch(_error){{ pending = null; }}
    const pendingId = pending && (pending.install_id || pending.request_id);
    const installId = /^[a-f0-9]{{32}}$/.test(String(pendingId || "")) ? String(pendingId) : "";
    if(!installId) return;

    async function read(){{
        if(retryEl) retryEl.disabled = true;
        if(statusEl){{ statusEl.hidden = false; statusEl.textContent = "正在读取原安装状态..."; }}
        if(loginEl) loginEl.hidden = true;
        try{{
            const response = await fetch("/api/online-scripts/log/" + encodeURIComponent(installId) + "?lines=80", {{cache:"no-store", credentials:"same-origin"}});
            const json = await flsReadJsonResponse(response);
            if(!json || typeof json.running !== "boolean") throw new Error("安装状态响应无效");
            if(statusEl) statusEl.textContent = "原安装请求：" + (json.status || "未知") + (json.phase ? "（" + json.phase + "）" : "");
            if(logEl){{ logEl.hidden = false; logEl.href = "/online-scripts/log/" + encodeURIComponent(installId); }}
            if(retryEl) retryEl.hidden = false;
            if(json.running || json.recovery){{
                if(statusEl) statusEl.textContent += "，请查看原安装日志继续恢复。";
            }}
        }}catch(error){{
            const denied = error && (error.status === 401 || error.status === 403);
            const message = error && error.message ? error.message : String(error || "查询失败");
            if(statusEl){{ statusEl.hidden = false; statusEl.textContent = "原安装状态读取失败：" + message; }}
            if(loginEl){{
                loginEl.hidden = !denied;
                if(denied) loginEl.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
            }}
            if(retryEl) retryEl.hidden = false;
        }}finally{{ if(retryEl) retryEl.disabled = false; }}
    }}
    if(retryEl){{ retryEl.hidden = false; retryEl.onclick = read; }}
    await read();
}}

flsApplyExcludedToVisible();
flsInstallRecoverPending();
</script>
"""

    return layout("选择任务并安装", "online_scripts", body)
