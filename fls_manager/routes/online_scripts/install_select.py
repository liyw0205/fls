from ._common import *
import re
import uuid

from ...models import TASKS_MUTATION_LOCK, TaskSchedulerReloadError
from ...scheduler import reload_scheduler
from ...online_scripts.constants import (
    claim_online_task_import_request,
    get_online_task_import_request,
    list_online_task_import_requests,
    persist_online_task_import_request,
    _task_import_snapshot,
    ONLINE_TASK_IMPORT_LOCK,
    ONLINE_TASK_IMPORT_ACTIVE,
    TaskImportHistoryFull,
    TaskImportReceiptMissing,
)
from ...online_scripts.tasks import (
    online_task_import_version, OnlineTaskPreparationError, OnlineTaskConfirmationStale, task_import_publication_state,
    task_import_file_signature,
)
from ...online_scripts.source import get_task_link_request, online_task_link_version
from ...storage import storage_transaction
from ...utils import now_str


def _task_import_error(script_id, message, status_code, persisted=False, request_id="", selected_task_indexes=None, enable_task=False, history_full=False):
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
            + ('<a class="btn btn-primary" href="/online-scripts/import-tasks-history?recovery=1">处理待恢复导入</a>' if history_full else "")
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


def _selected_indexes_from_data(item, data, *, task_count=None):
    select_mode = str(data.get("select_mode", "") or "").strip()
    excluded_raw = str(data.get("excluded_task_indexes", "") or "").strip()
    if select_mode == "all":
        if task_count is None:
            task_count = len(online_script_task_crons(item))
        excluded = set()
        for value in excluded_raw.replace("，", ",").split(","):
            value = value.strip()
            if not value:
                continue
            if not value.isdecimal():
                raise ValueError("任务排除项必须为整数")
            excluded.add(int(value))
        if any(index < 1 or index > task_count for index in excluded):
            raise ValueError("任务排除项已失效")
        return [str(index) for index in range(1, task_count + 1) if index not in excluded]
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
            "retry_allowed": False,
            "scheduler_reload_required": persisted,
        }, 503
    return {
        "ok": False,
        "request_id": record["request_id"],
        "msg": message,
        "resolved": True,
        "scheduler_reload_required": persisted,
        "retry_allowed": state == "failed",
    }, status_code


def _run_online_task_import(record, item, enable_task, selected_task_indexes, request_id):
    ONLINE_TASK_IMPORT_ACTIVE.add(request_id)
    try:
        try:
            with TASKS_MUTATION_LOCK, storage_transaction():
                record["task_baseline"] = task_import_file_signature()
                record["task_baseline_captured"] = True
                persist_online_task_import_request(record)
        except Exception as exc:
            return _import_failure(record, f"任务文件基线无法保存，未写入任务：{exc}", 503)

        try:
            _import_record_update(record, "running")
            persist_online_task_import_request(record)
        except Exception as exc:
            return _import_failure(record, f"导入启动回执无法保存，未写入任务：{exc}", 500)

        def before_publish(publication):
            if record.get("retry_source") and record["retry_source"] != publication["before_source"]:
                raise OnlineTaskConfirmationStale("原未写入确认已因任务文件变化失效，未写入任务；请重新核对当前任务")
            if record.get("task_baseline_captured") and record["task_baseline"] != publication["before_source"]:
                raise OnlineTaskConfirmationStale("任务文件自原请求检查后已变化，未写入任务；请重新核对当前任务")
            record["publication"] = publication
            persist_online_task_import_request(record)

        try:
            ok, message = import_task_if_needed(
                item,
                log_file=None,
                enable_task=enable_task,
                selected_task_indexes=selected_task_indexes,
                raise_on_reload_error=True,
                before_publish=before_publish,
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
        except OnlineTaskConfirmationStale as exc:
            record["publication"] = None
            return _import_failure(record, str(exc), 409, state="unknown")
        except OnlineTaskPreparationError as exc:
            record["publication"] = None
            return _import_failure(record, str(exc), 500)
        except Exception as exc:
            if record.get("publication"):
                return _import_failure(record, f"任务发布结果无法确认：{exc}。请检查原导入结果，勿重复写入。", 503, state="unknown")
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
                return _import_failure(record, f"任务已导入，但完成回执无法确认：{exc}，请检查原导入结果。", 503, state="unknown")
        if not ok:
            return {"ok": False, "request_id": request_id, "msg": message, "retry_allowed": False}, 409
        return {"ok": True, "request_id": request_id, "msg": message}, 200
    finally:
        ONLINE_TASK_IMPORT_ACTIVE.discard(request_id)


def _missing_import_receipt(request_id, exc):
    return {"ok": False, "request_id": request_id, "msg": str(exc), "active": True,
            "receipt_missing": True, "retry_allowed": False,
            "status_url": f"/online-scripts/import-tasks-status/{request_id}"}, 503


def _existing_import_result(record, script_id, data, *, expected_version=""):
    if not record:
        return None
    request_id = record["request_id"]
    identity = {"request_id": request_id, "state": record["state"], "retry_allowed": False,
                "status_url": f"/online-scripts/import-tasks-status/{request_id}",
                "state_url": f"/api/online-scripts/import-tasks-status/{request_id}"}
    enable_task = data.get("enable_task") in (True, 1, "1", "true", "True")
    if record.get("script_id") != script_id or record.get("enable_task") != enable_task:
        return {**identity, "ok": False, "msg": "导入请求已绑定其他脚本或启用选择，请查询原请求。"}, 409
    if not expected_version and record["state"] in ("prepared", "failed") and not record.get("active") and not record.get("archived"):
        return None
    if str(data.get("select_mode", "") or "").strip() == "all" and not record.get("task_count"):
        if record["state"] == "failed" and not record.get("active") and not record.get("archived"):
            return None
        if record["state"] == "prepared" and not record.get("active"):
            return None
        return {**identity, "ok": False, "msg": "原回执未保存任务总数，无法核对全部选择；请查询原导入请求。"}, 409
    try:
        values = _selected_indexes_from_data({}, data, task_count=record.get("task_count", 0))
        selected = []
        for value in values:
            raw = str(value).strip()
            if not raw.isdecimal() or int(raw) < 1:
                raise ValueError("任务序号必须为正整数")
            index = str(int(raw))
            if index not in selected:
                selected.append(index)
        if not selected:
            raise ValueError("任务选择为空或已失效")
    except ValueError as exc:
        return {**identity, "ok": False, "msg": str(exc)}, 400
    if selected != record.get("selected_task_indexes"):
        return {**identity, "ok": False, "msg": "导入请求已绑定其他任务选择，请查询原请求。"}, 409
    if record.get("archived"):
        return {**identity, "ok": False, "archived": True, "replayed": True,
                "msg": "原导入请求已归档，不能继续导入；" + record.get("message", "")}, 409
    if record.get("confirmation_stale") or record.get("confirmation_check_error"):
        return {**identity, "ok": False, "confirmation_stale": True,
                "msg": "原未写入确认已失效或无法复核，请检查原导入结果；未重复写入任务。"}, 409
    if record["state"] == "failed" and not record.get("active"):
        if expected_version and record.get("expected_version") != expected_version:
            return {**identity, "ok": False, "msg": "脚本源版本已变化，请查询原请求并重新选择。"}, 409
        return None
    if record["state"] == "prepared" and not record.get("active"):
        return None
    payload = {**identity, "ok": record["state"] == "completed", "replayed": True,
               "msg": record.get("message") or "原导入仍在处理中或结果未知，请先查询原请求。"}
    if record["state"] == "scheduler_pending":
        payload.update(scheduler_reload_required=True,
                       reload_url=f"/api/online-scripts/import-tasks-reload/{request_id}")
    return payload, 200 if payload["ok"] else 503 if record["state"] == "scheduler_pending" else 409


def _submit_online_task_import(script_id, data):
    try:
        request_id = _import_request_id(data.get("request_id"))
    except ValueError as exc:
        return {"ok": False, "request_id": "", "msg": str(exc), "retry_allowed": False}, 400
    try:
        previous = get_online_task_import_request(request_id, recover=False)
    except TaskImportReceiptMissing as exc:
        return _missing_import_receipt(request_id, exc)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"导入回执无法读取，未写入任务：{exc}", "retry_allowed": False}, 500
    replay = _existing_import_result(previous, script_id, data)
    if replay:
        return replay
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
        previous = get_online_task_import_request(request_id, binding={
            "script_id": script_id, "expected_version": expected_version,
            "selected_task_indexes": selected_task_indexes, "enable_task": enable_task,
        })
    except TaskImportReceiptMissing as exc:
        return _missing_import_receipt(request_id, exc)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"导入回执无法读取，未写入任务：{exc}", "retry_allowed": False}, 500
    if previous:
        replay = _existing_import_result(previous, script_id, data, expected_version=expected_version)
        if replay:
            return replay
        if (previous.get("script_id") != script_id
                or previous.get("expected_version") != expected_version
                or previous.get("selected_task_indexes") != selected_task_indexes
                or previous.get("enable_task") != enable_task):
            return {"ok": False, "request_id": request_id, "msg": "导入请求已绑定其他脚本、版本或任务选择，请重新选择。", "retry_allowed": False}, 409
        if previous.get("state") == "completed":
            return {"ok": True, "request_id": request_id, "msg": previous.get("message") or "任务导入已完成，未重复写入", "replayed": True}, 200
        if previous.get("state") == "scheduler_pending":
            return {"ok": False, "request_id": request_id, "msg": previous.get("message") or "任务已保存，但调度器尚未恢复", "scheduler_reload_required": True, "reload_url": f"/api/online-scripts/import-tasks-reload/{request_id}"}, 503
        if previous.get("active") or previous.get("state") in ("running", "prepared", "unknown"):
            return {"ok": False, "request_id": request_id, "msg": "原导入请求仍在处理中或结果未知，请先查询原请求。", "retry_allowed": False}, 409

    record = {
        "request_id": request_id,
        "script_id": script_id,
        "expected_version": expected_version,
        "selected_task_indexes": list(selected_task_indexes),
        "task_count": len(online_script_task_crons(item)),
        "publication_tracking": True,
        "enable_task": bool(enable_task),
        "state": "prepared",
        "message": "",
        "scheduler_reload_error": "",
        "updated_at": now_str(),
    }
    try:
        claimed_previous = claim_online_task_import_request(record)
    except TaskImportHistoryFull as exc:
        return {"ok": False, "request_id": request_id, "msg": str(exc), "accepted": False,
                "history_full": True, "retry_allowed": False,
                "history_url": "/online-scripts/import-tasks-history?recovery=1"}, 409
    except TaskImportReceiptMissing as exc:
        return _missing_import_receipt(request_id, exc)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"导入请求无法保存，未写入任务：{exc}", "retry_allowed": True}, 500
    if claimed_previous:
        replay = _existing_import_result(claimed_previous, script_id, data, expected_version=expected_version)
        if replay:
            return replay
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
        try:
            latest_item = get_online_script(script_id)
        except Exception as exc:
            return _import_failure(record, f"脚本源版本无法确认，未写入任务：{exc}", 500)
        if not latest_item or online_task_import_version(latest_item) != expected_version:
            return _import_failure(record, "脚本源已更新或删除，未写入旧页面选择的任务。请重新选择后重试。", 409)
        return _run_online_task_import(record, item, enable_task, selected_task_indexes, request_id)
    finally:
        ONLINE_TASK_IMPORT_ACTIVE.discard(request_id)


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
        request_id="" if payload.get("accepted") is False else payload.get("request_id", ""),
        selected_task_indexes=selected if payload.get("retry_allowed") else [],
        enable_task=data.get("enable_task") in (True, 1, "1", "true", "True"),
        history_full=bool(payload.get("history_full")),
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
    except TaskImportReceiptMissing as exc:
        payload, status_code = _missing_import_receipt(request_id, exc)
        return jsonify(payload), status_code
    except Exception as exc:
        return jsonify(ok=False, msg=f"导入回执无法读取：{exc}"), 500
    if not record:
        return jsonify(ok=False, msg="导入请求不存在"), 404
    return jsonify(ok=True, **record)


@bp.route("/api/online-scripts/import-tasks-history")
def api_online_scripts_import_tasks_history():
    try:
        page = _import_history_page()
    except ValueError as exc:
        return jsonify(ok=False, msg=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, msg=f"导入历史无法读取：{exc}"), 500
    return jsonify(ok=True, **page)


def _import_history_page():
    recovery = request.args.get("recovery", "0")
    if recovery not in ("0", "1"):
        raise ValueError("待恢复导入筛选无效")
    return list_online_task_import_requests(request.args.get("limit", 50), request.args.get("script_id", ""),
                                           recovery=recovery == "1", offset=request.args.get("offset", 0),
                                           with_pagination=True)


def _confirm_task_import(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return {"ok": False, "msg": "在线任务导入请求标识无效", "retry_allowed": False}, 400
    with ONLINE_TASK_IMPORT_LOCK, TASKS_MUTATION_LOCK, storage_transaction():
        try:
            record = get_online_task_import_request(request_id)
        except TaskImportReceiptMissing as exc:
            return _missing_import_receipt(request_id, exc)
        except Exception as exc:
            return {"ok": False, "request_id": request_id, "msg": f"导入回执无法读取：{exc}", "retry_allowed": False}, 503
        if not record:
            return {"ok": False, "request_id": request_id, "msg": "导入请求不存在", "retry_allowed": False}, 404
        identity = {"request_id": request_id, "script_id": record["script_id"], "retry_allowed": False,
                    "status_url": f"/online-scripts/import-tasks-status/{request_id}"}
        if record.get("active") or record.get("archived"):
            return {**identity, "ok": False, "msg": "原导入仍在处理中或已归档，不能确认。"}, 409
        if record["state"] == "completed":
            return {**identity, "ok": True, "state": "completed", "replayed": True, "msg": record["message"]}, 200
        if record["state"] not in ("unknown", "scheduler_pending") and not record.get("confirmation"):
            return {**identity, "ok": False, "msg": "当前导入无需检查写入结果，请查询原请求。"}, 409
        try:
            confirmation, signature = task_import_publication_state(record)
        except Exception as exc:
            return {**identity, "ok": False, "state": record["state"], "msg": f"原导入结果无法确认：{exc}"}, 409
        state = "scheduler_pending" if confirmation == "published" else "failed"
        replayed = record.get("confirmation") == confirmation and record["state"] == state
        if not replayed:
            message = ("已核对原选择，任务已写入，仅需恢复调度器。" if confirmation == "published"
                       else "已核对任务文件未变化，原导入未写入，可继续原请求。")
            if record.get("message"):
                message += "原诊断：" + record["message"]
            _import_record_update(record, state, message, record.get("scheduler_reload_error", ""))
            record.update(confirmation=confirmation, confirmation_source=signature)
            try:
                persist_online_task_import_request(record)
            except Exception as exc:
                return {**identity, "ok": False, "msg": f"检查结果回执无法确认，请查询原导入请求后重试：{exc}"}, 503
        return {**identity, "ok": True, "state": state, "confirmation": confirmation, "replayed": replayed,
                "retry_allowed": state == "failed", "scheduler_reload_required": state == "scheduler_pending",
                "msg": record["message"]}, 200


@bp.route("/api/online-scripts/import-tasks-confirm/<request_id>", methods=["POST"])
def api_online_scripts_import_tasks_confirm(request_id):
    payload, status_code = _confirm_task_import(request_id)
    return jsonify(payload), status_code


@bp.route("/online-scripts/import-tasks-confirm/<request_id>", methods=["POST"])
def online_scripts_import_tasks_confirm(request_id):
    payload, status_code = _confirm_task_import(request_id)
    if payload.get("ok"):
        return redirect(url_for("online_scripts.online_scripts_import_tasks_status", request_id=request_id))
    return _task_import_error(payload.get("script_id", ""), payload.get("msg", "导入检查失败"), status_code,
                              request_id=request_id)


@bp.route("/api/online-scripts/import-tasks-reload/<request_id>", methods=["POST"])
def api_online_scripts_import_tasks_reload(request_id):
    payload, status_code = _reload_import_scheduler(request_id)
    return jsonify(payload), status_code


def _task_import_receipt_matches(expected, actual):
    if not isinstance(actual, dict):
        return False
    try:
        return _task_import_snapshot(expected) == _task_import_snapshot(actual)
    except Exception:
        return False


def _reload_import_scheduler(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return {"ok": False, "msg": "在线任务导入请求标识无效"}, 400
    with ONLINE_TASK_IMPORT_LOCK, TASKS_MUTATION_LOCK:
        try:
            record = get_online_task_import_request(request_id)
        except TaskImportReceiptMissing as exc:
            return _missing_import_receipt(request_id, exc)
        except Exception as exc:
            return {"ok": False, "msg": f"导入回执无法读取：{exc}"}, 500
        if not record:
            return {"ok": False, "msg": "导入请求不存在"}, 404
        if record.get("archived") or record.get("active"):
            return {"ok": False, "request_id": request_id, "script_id": record.get("script_id", ""),
                    "msg": "原导入仍在处理中或已归档，不能执行调度器恢复。", "retry_allowed": False}, 409
        if record.get("state") == "completed":
            return {"ok": True, "request_id": request_id, "script_id": record.get("script_id", ""),
                    "state": "completed", "receipt_confirmed": True,
                    "msg": "调度器已恢复，未重复导入", "replayed": True}, 200
        if record.get("state") != "scheduler_pending":
            return {"ok": False, "request_id": request_id, "script_id": record.get("script_id", ""), "msg": "当前导入没有待恢复的调度器状态"}, 409
        if record.get("publication"):
            try:
                with storage_transaction():
                    confirmation, _signature = task_import_publication_state(record)
                if confirmation != "published":
                    raise RuntimeError("原导入任务未写入，请检查原导入结果")
            except Exception as exc:
                return {"ok": False, "request_id": request_id, "script_id": record.get("script_id", ""),
                        "msg": f"原任务状态无法复核，未重载调度器：{exc}", "retry_allowed": False}, 409
        original_receipt = _task_import_snapshot(record)
        try:
            reload_scheduler()
        except Exception as exc:
            return {
                "ok": False,
                "request_id": request_id,
                "script_id": record.get("script_id", ""),
                "state": "scheduler_pending",
                "scheduler_reload_required": True,
                "reload_url": f"/api/online-scripts/import-tasks-reload/{request_id}",
                "status_url": f"/online-scripts/import-tasks-status/{request_id}",
                "state_url": f"/api/online-scripts/import-tasks-status/{request_id}",
                "msg": f"调度器重载失败：{exc}，任务文件保持已保存状态",
            }, 503
        _import_record_update(record, "completed", "任务已保存，调度器已恢复")
        try:
            persist_online_task_import_request(record)
        except Exception as exc:
            try:
                persisted = get_online_task_import_request(request_id, recover=False)
            except Exception as read_exc:
                persisted = None
                exc = f"{exc}；重读原回执失败：{read_exc}"
            if not _task_import_receipt_matches(record, persisted):
                retryable = (
                    _task_import_receipt_matches(original_receipt, persisted)
                    and persisted.get("state") == "scheduler_pending"
                    and not persisted.get("active")
                    and not persisted.get("archived")
                ) if isinstance(persisted, dict) else False
                payload = {
                    "ok": False,
                    "request_id": request_id,
                    "script_id": record.get("script_id", ""),
                    "state": persisted.get("state") if isinstance(persisted, dict) else "unknown",
                    "receipt_confirmation_required": True,
                    "status_url": f"/online-scripts/import-tasks-status/{request_id}",
                    "state_url": f"/api/online-scripts/import-tasks-status/{request_id}",
                    "retry_allowed": False,
                    "msg": f"调度器已重载，但原导入完成回执无法确认：{exc}。请查询原请求。",
                }
                if retryable:
                    payload.update(
                        scheduler_reload_required=True,
                        reload_url=f"/api/online-scripts/import-tasks-reload/{request_id}",
                    )
                return payload, 503
            record = persisted
    return {"ok": True, "request_id": request_id, "script_id": record.get("script_id", ""),
            "state": "completed", "receipt_confirmed": True,
            "msg": "任务已保存，调度器已恢复"}, 200


def _archive_task_import(request_id, choice):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or "") or choice != "archive":
        return {"ok": False, "msg": "请明确确认归档原导入请求；未修改回执或任务。"}, 400
    with ONLINE_TASK_IMPORT_LOCK, storage_transaction():
        try:
            record = get_online_task_import_request(request_id)
        except TaskImportReceiptMissing as exc:
            return _missing_import_receipt(request_id, exc)
        except Exception as exc:
            return {"ok": False, "request_id": request_id, "msg": f"导入回执无法读取：{exc}"}, 500
        if not record:
            return {"ok": False, "request_id": request_id, "msg": "导入请求不存在"}, 404
        if record.get("active") or record.get("state") not in ("failed", "unknown", "scheduler_pending"):
            return {"ok": False, "request_id": request_id, "retry_allowed": False,
                    "msg": "只能归档已停止的失败、未知或调度器待恢复导入。"}, 409
        replayed = bool(record.get("archived"))
        if not replayed:
            record.update(archived=True, archived_at=now_str())
            try:
                persist_online_task_import_request(record)
            except Exception as exc:
                return {"ok": False, "request_id": request_id, "retry_allowed": False,
                        "msg": f"归档结果无法确认，请查询原导入请求后重试：{exc}"}, 503
        return {"ok": True, "request_id": request_id, "state": record["state"], "archived": True,
                "replayed": replayed, "retry_allowed": False, "msg": "原导入请求已归档，任务和调度器未修改。"}, 200


@bp.route("/api/online-scripts/import-tasks-archive/<request_id>", methods=["POST"])
def api_online_scripts_import_tasks_archive(request_id):
    data = (request.get_json(silent=True) if request.is_json else request.form) or {}
    choice = data.get("acknowledge") if hasattr(data, "get") else None
    payload, status_code = _archive_task_import(request_id, choice)
    return jsonify(payload), status_code


@bp.route("/online-scripts/import-tasks-archive/<request_id>", methods=["POST"])
def online_scripts_import_tasks_archive(request_id):
    payload, status_code = _archive_task_import(request_id, request.form.get("acknowledge"))
    if payload.get("ok"):
        return redirect(url_for("online_scripts.online_scripts_import_tasks_status", request_id=request_id))
    return _task_import_error("", payload.get("msg") or "归档失败", status_code, request_id=request_id,
                              history_full=True)


@bp.route("/online-scripts/import-tasks-reload/<request_id>", methods=["POST"])
def online_scripts_import_tasks_reload(request_id):
    payload, status_code = _reload_import_scheduler(request_id)
    if payload.get("ok"):
        return redirect(url_for("online_scripts.online_scripts_page", msg=payload.get("msg")))
    return _task_import_error(payload.get("script_id", ""), payload.get("msg", "导入恢复失败"), status_code,
                              persisted=bool(payload.get("scheduler_reload_required")), request_id=request_id)


@bp.route("/online-scripts/import-tasks-status/<request_id>")
def online_scripts_import_tasks_status(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return _task_import_error("", "在线任务导入请求标识无效", 400)
    try:
        record = get_online_task_import_request(request_id)
    except TaskImportReceiptMissing as exc:
        return _task_import_error("", str(exc), 503, request_id=request_id)
    except Exception as exc:
        return _task_import_error("", f"导入回执无法读取：{exc}", 500, request_id=request_id)
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
    if record.get("archived"):
        labels[state] = {"failed": "导入失败", "unknown": "导入结果未知", "scheduler_pending": "任务已保存，调度器未确认恢复"}[state] + "（已归档）"
    elif record.get("confirmation_stale") or record.get("confirmation_check_error"):
        labels[state] = "原未写入确认已失效或无法复核，请检查当前任务"
        actions = '<a class="btn btn-primary" href="/tasks">核对当前任务</a>' + actions
    elif state == "failed" and record.get("retry_allowed"):
        fields = f'<input type="hidden" name="request_id" value="{h(request_id)}">'
        fields += "".join(f'<input type="hidden" name="task_indexes" value="{h(index)}">' for index in record.get("selected_task_indexes", []))
        if record.get("enable_task"):
            fields += '<input type="hidden" name="enable_task" value="1">'
        actions = f'<form class="inline-form" method="post" action="/online-scripts/import-tasks/{h(record.get("script_id", ""))}">{fields}<button class="btn btn-primary" type="submit">继续原导入请求</button></form>' + actions
    elif state == "scheduler_pending" and not record.get("active") and not record.get("confirmation_stale") and not record.get("confirmation_check_error"):
        actions = f'<form class="inline-form" method="post" action="/online-scripts/import-tasks-reload/{h(request_id)}"><button class="btn btn-primary" type="submit">仅重新加载调度器</button></form>' + actions
    elif state in ("prepared", "running", "unknown"):
        actions = f'<a class="btn btn-blue" href="/online-scripts/import-tasks-status/{h(request_id)}">刷新查询</a>' + actions
    if not record.get("active") and not record.get("archived") and (state == "unknown" or record.get("confirmation_stale") or record.get("confirmation_check_error")):
        actions = f'<form class="inline-form" method="post" action="/online-scripts/import-tasks-confirm/{h(request_id)}"><button class="btn btn-primary" type="submit">检查原导入结果</button></form>' + actions
    body = page_header_card(
        "在线任务导入状态",
        help_html="该页面只读取导入回执，不会重复写入任务。调度器待恢复时仅执行重载，不会再次导入。",
        actions_html=actions,
    )
    body += f'''<section class="section"><div class="card"><h2 class="section-title">{h(labels.get(state, state))}</h2><p>请求标识：<code>{h(request_id)}</code></p><p>脚本：{h(record.get("script_id"))}</p><p>更新时间：{h(record.get("updated_at"))}</p><p>{h(record.get("message") or record.get("scheduler_reload_error") or "-")}</p>{f'<p role="alert">原导入任务复核失败：{h(record.get("confirmation_check_error")) if record.get("confirmation_check_error") else "原任务内容已变化"}</p>' if record.get("confirmation_stale") or record.get("confirmation_check_error") else ''}</div></section>'''
    if not record.get("active") and not record.get("archived") and state in ("failed", "unknown", "scheduler_pending"):
        body += f'''<form method="post" action="/online-scripts/import-tasks-archive/{h(request_id)}" style="display:grid;gap:12px;margin:16px 0">
<label style="display:flex;align-items:flex-start;gap:8px"><input type="checkbox" name="acknowledge" value="archive" required style="width:auto;flex:none"><span>已核对任务，保留当前任务和调度器状态并放弃此请求，允许清理其回执</span></label>
<div><button class="btn btn-gray" type="submit">归档原导入请求</button></div></form>'''
    elif record.get("archived"):
        body += '<p>已放弃此请求，不能继续导入或恢复调度器；原任务和诊断保持不变。</p>'
    return layout("在线任务导入状态", "online_scripts", body)


@bp.route("/online-scripts/import-tasks-history")
def online_scripts_import_tasks_history():
    script_id = str(request.args.get("script_id") or "").strip()
    try:
        page = _import_history_page()
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
            actions_html=f'<a class="btn btn-blue" href="{h(request.full_path)}">重新读取</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
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
    for record in page["items"]:
        request_id = str(record.get("request_id") or "")
        state = str(record.get("state") or "unknown")
        status_label = "失败" if state == "failed" and record.get("archived") else labels.get(state, state)
        actions = f'<a class="btn btn-blue" href="/online-scripts/import-tasks-status/{h(request_id)}">查看状态</a>'
        if (state == "scheduler_pending" and not record.get("active") and not record.get("archived")
                and not record.get("confirmation_stale") and not record.get("confirmation_check_error")):
            actions += f'<form class="inline-form" method="post" action="/online-scripts/import-tasks-reload/{h(request_id)}"><button class="btn btn-primary" type="submit">仅重载调度器</button></form>'
        elif state == "failed" and record.get("retry_allowed"):
            fields = f'<input type="hidden" name="request_id" value="{h(request_id)}">'
            fields += "".join(f'<input type="hidden" name="task_indexes" value="{h(index)}">' for index in record.get("selected_task_indexes", []))
            if record.get("enable_task"):
                fields += '<input type="hidden" name="enable_task" value="1">'
            actions += f'<form class="inline-form" method="post" action="/online-scripts/import-tasks/{h(record.get("script_id") or "")}">{fields}<button class="btn btn-orange" type="submit">继续原导入请求</button></form>'
        elif not record.get("active") and not record.get("archived") and (state == "unknown" or record.get("confirmation_stale") or record.get("confirmation_check_error")):
            actions += f'<form class="inline-form" method="post" action="/online-scripts/import-tasks-confirm/{h(request_id)}"><button class="btn btn-primary" type="submit">检查原导入结果</button></form>'
            if state == "failed":
                status_label = "原未写入确认无法复核"
        rows.append(
            "<tr>"
            f"<td><code>{h(request_id)}</code></td>"
            f"<td>{h(record.get('script_id') or '-')}</td>"
            f"<td>{h(status_label)}{'（已归档）' if record.get('archived') else ''}</td>"
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
    recovery = request.args.get("recovery", "0")
    links = '<a class="btn btn-blue" href="/online-scripts/import-tasks-history?recovery=1">待恢复导入</a><a class="btn btn-gray" href="/online-scripts/import-tasks-history">全部导入</a>'
    for label, offset in (("上一页", max(0, page["offset"] - page["limit"])), ("下一页", page["offset"] + page["limit"])):
        if (label == "上一页" and page["offset"]) or (label == "下一页" and page["has_more"]):
            href = url_for("online_scripts.online_scripts_import_tasks_history", script_id=script_id,
                           recovery=recovery, limit=page["limit"], offset=offset)
            links += f'<a class="btn btn-blue" href="{h(href)}">{label}</a>'
    body = page_header_card(
        "在线任务导入历史",
        help_html="历史只读取已保存回执，不会重复写入任务；调度器待恢复时只执行重载。",
        actions_html=links + '<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
    ) + f'<section class="section"><div class="card">{table}</div></section>'
    return layout("在线任务导入历史", "online_scripts", body)


@bp.route("/online-scripts/install-select/<script_id>")
def online_scripts_install_select(script_id):
    try:
        item = get_online_script(script_id)
    except Exception as exc:
        return _install_selection_unavailable(script_id, f"在线脚本源缓存无法读取，未覆盖原文件：{exc}。请检查源数据后重试。", 500)

    if not item:
        return _install_selection_unavailable(script_id, "脚本已不在当前本地缓存中", 404)

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

<form method="post" action="/online-scripts/install/{h(script_id)}" id="onlineInstallSelectForm" data-api-action="online-task-install-select">
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
<a class="btn btn-blue" id="onlineInstallRecoveryStatusLink" href="#" hidden>查询原导入请求</a>
<a class="btn btn-primary" id="onlineInstallRecoveryHistory" href="/online-scripts/import-tasks-history?recovery=1" hidden>处理待恢复导入</a>
<button class="btn btn-primary" id="onlineInstallRecoveryConfirm" type="button" hidden>检查原导入结果</button>
<button class="btn btn-primary" id="onlineInstallRecoveryReload" type="button" hidden>仅重新加载调度器</button>
<button class="btn btn-gray" id="onlineInstallRecoveryRetry" type="button" hidden>重新读取安装状态</button>
<button class="btn btn-orange" id="onlineInstallRecoveryResume" type="button" hidden>沿原请求重试导入</button>
<a class="btn btn-gray" id="onlineInstallRecoveryInstallHistory" href="/online-scripts/install-history" hidden>查看安装历史</a>
<button class="btn btn-blue" id="onlineInstallRecoveryCheckHistory" type="button" hidden>核对安装历史</button>
<div id="onlineInstallRecoveryHistoryItems" class="help"></div>
<button class="btn btn-orange" id="onlineInstallRecoveryClear" type="button" hidden>确认核对后清理原请求</button>

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

const ONLINE_INSTALL_STORAGE_KEY = "fls-online-install-request";
const ONLINE_INSTALL_TERMINAL_STATUSES = new Set(["已完成", "失败", "已停止", "启动失败", "超时"]);

function flsInstallStorageState(){{
    let raw;
    try{{ raw = sessionStorage.getItem(ONLINE_INSTALL_STORAGE_KEY); }}
    catch(error){{ return {{kind:"unavailable", error:String(error)}}; }}
    if(raw === null || raw === undefined) return {{kind:"empty", raw:null}};
    let value;
    try{{ value = JSON.parse(raw); }}
    catch(error){{ return {{kind:"corrupt", raw, error:String(error)}}; }}
    const requestId = value && typeof value === "object" && !Array.isArray(value)
        ? String(value.install_id || value.request_id || "") : "";
    const action = value && typeof value.action === "string" ? value.action : "";
    const validAction = !action || action.startsWith("/online-scripts/install/")
        || action.startsWith("/online-scripts/import-tasks/");
    if(!value || typeof value !== "object" || Array.isArray(value)
        || !/^[a-f0-9]{{32}}$/.test(requestId)
        || typeof value.script_id !== "string" || !value.script_id || value.script_id.length > 200
        || !validAction
        || (value.excluded_task_indexes !== undefined && typeof value.excluded_task_indexes !== "string")
        || (value.enable_task !== undefined && typeof value.enable_task !== "boolean")
        || (value.import_task !== undefined && typeof value.import_task !== "boolean")
        || (value.proxy_id !== undefined && typeof value.proxy_id !== "string")
        || (value.force !== undefined && typeof value.force !== "boolean")){{
        return {{kind:"corrupt", raw}};
    }}
    return {{kind:"valid", raw, value, request_id:requestId}};
}}

function flsInstallWritePending(value, expectedRaw){{
    const current = flsInstallStorageState();
    if(current.kind === "unavailable") throw new Error("无法读取本地安装请求状态");
    if(expectedRaw === undefined ? current.kind !== "empty" : current.raw !== expectedRaw)
        throw new Error("本地安装请求状态已变化");
    const raw = JSON.stringify(value);
    sessionStorage.setItem(ONLINE_INSTALL_STORAGE_KEY, raw);
    if(sessionStorage.getItem(ONLINE_INSTALL_STORAGE_KEY) !== raw) throw new Error("本地安装请求状态读回校验失败");
    return raw;
}}

function flsInstallClearKnownRejected(expectedRaw){{
    const current = flsInstallStorageState();
    if(current.kind === "unavailable" || current.raw !== expectedRaw) throw new Error("本地安装请求状态已变化");
    sessionStorage.removeItem(ONLINE_INSTALL_STORAGE_KEY);
    if(sessionStorage.getItem(ONLINE_INSTALL_STORAGE_KEY) !== null) throw new Error("本地请求状态未能清理");
}}

function flsInstallLockForm(locked){{
    const form = document.getElementById("onlineInstallSelectForm");
    if(!form) return;
    form.__flsInstallRecoveryLocked = !!locked;
    form.querySelectorAll('button[type="submit"]').forEach(button => {{ button.disabled = !!locked; }});
}}

function flsInstallSetStatus(message){{
    const status = document.getElementById("onlineInstallRecoveryStatus");
    if(status){{ status.hidden = false; status.textContent = message; }}
}}

function flsInstallShowHistory(pending){{
    const link = document.getElementById("onlineInstallRecoveryInstallHistory");
    const check = document.getElementById("onlineInstallRecoveryCheckHistory");
    const clear = document.getElementById("onlineInstallRecoveryClear");
    if(link){{
        link.hidden = false;
        link.href = pending && pending.script_id
            ? "/online-scripts/install-history?script_id=" + encodeURIComponent(pending.script_id)
            : "/online-scripts/install-history";
    }}
    if(check){{ check.hidden = false; check.onclick = flsInstallCheckHistory; }}
    if(clear) clear.hidden = true;
    window.__FLS_INSTALL_VERIFIED_RAW__ = null;
}}

function flsInstallEnableClear(raw){{
    const clear = document.getElementById("onlineInstallRecoveryClear");
    if(clear){{ clear.hidden = false; clear.onclick = () => flsInstallClearPending(raw); }}
    window.__FLS_INSTALL_VERIFIED_RAW__ = raw;
}}

function flsInstallRenderHistory(items, summary){{
    const box = document.getElementById("onlineInstallRecoveryHistoryItems");
    if(!box) return;
    box.replaceChildren();
    const message = document.createElement("div");
    message.textContent = summary;
    box.appendChild(message);
    items.slice(0, 8).forEach(item => {{
        const row = document.createElement("div");
        const link = document.createElement("a");
        link.href = "/online-scripts/log/" + encodeURIComponent(item.id);
        link.textContent = String(item.script_id || "-") + " | " + String(item.status || "-") + " | " + String(item.id || "-");
        row.appendChild(link);
        box.appendChild(row);
    }});
}}

function flsInstallHistoryItemValid(item){{
    return !!item && typeof item === "object" && !Array.isArray(item)
        && typeof item.id === "string" && /^[A-Za-z0-9_-]{{1,80}}$/.test(item.id)
        && typeof item.script_id === "string" && item.script_id.length > 0
        && typeof item.status === "string" && item.status.length > 0
        && typeof item.running === "boolean"
        && (item.scheduler_reload_pending === undefined || item.scheduler_reload_pending === null
            || typeof item.scheduler_reload_pending === "boolean");
}}

function flsInstallHistoryItemSafe(item){{
    return flsInstallHistoryItemValid(item) && !item.running
        && !item.scheduler_reload_pending && ONLINE_INSTALL_TERMINAL_STATUSES.has(item.status);
}}

async function flsInstallCheckHistory(){{
    const state = flsInstallStorageState();
    if(state.kind === "unavailable" || state.kind === "empty"){{
        flsInstallRenderHistory([], "无法读取待处理请求状态；未清理本地记录。请检查浏览器存储后重试。");
        return;
    }}
    const pending = state.kind === "valid" ? state.value : null;
    const scriptQuery = pending && pending.script_id
        ? "&script_id=" + encodeURIComponent(pending.script_id) : "";
    const check = document.getElementById("onlineInstallRecoveryCheckHistory");
    if(check) check.disabled = true;
    flsInstallSetStatus("正在读取完整安装历史；在核对完成前不会启动新请求。");
    try{{
        const history = [], seenIds = new Set();
        let total = null, offset = 0, snapshot = "";
        while(true){{
            const query = "?limit=100" + scriptQuery
                + (offset ? "&offset=" + offset : "")
                + (snapshot ? "&snapshot=" + encodeURIComponent(snapshot) : "");
            const response = await fetch("/api/online-scripts/install-history" + query,
                {{cache:"no-store", credentials:"same-origin"}});
            const json = await flsReadJsonResponse(response);
            if(!json || json.ok !== true || !Array.isArray(json.items)
                || !Number.isInteger(json.total) || json.total < 0
                || json.offset !== offset || json.limit !== 100 || typeof json.has_more !== "boolean"
                || typeof json.snapshot !== "string" || !/^[a-f0-9]{{64}}$/.test(json.snapshot)
                || json.items.some(item => !flsInstallHistoryItemValid(item))){{
                throw new Error("安装历史响应无效或结构不完整");
            }}
            if(total === null){{
                total = json.total;
                snapshot = json.snapshot;
            }}else if(total !== json.total || snapshot !== json.snapshot){{
                throw new Error("安装历史在分页期间发生变化，请重新查询");
            }}
            const expectedCount = Math.min(100, Math.max(0, total - offset));
            if(json.items.length !== expectedCount
                || json.has_more !== (offset + json.items.length < total)){{
                throw new Error("安装历史分页不完整，请重新查询");
            }}
            const pageIds = new Set();
            for(const item of json.items){{
                if(seenIds.has(item.id) || pageIds.has(item.id))
                    throw new Error("安装历史分页包含重复记录，请重新查询");
                pageIds.add(item.id);
            }}
            json.items.forEach(item => seenIds.add(item.id));
            history.push(...json.items);
            if(!json.has_more) break;
            if(!json.items.length) throw new Error("安装历史分页未前进，请重新查询");
            offset += json.items.length;
        }}
        const complete = total !== null && history.length === total;
        const installId = pending && (pending.install_id || pending.request_id);
        const original = installId ? history.find(item => item.id === installId) : null;
        const safe = complete && history.every(item => flsInstallHistoryItemSafe(item))
            && (pending ? (!original || (original.script_id === pending.script_id
                && flsInstallHistoryItemSafe(original))) : true);
        flsInstallRenderHistory(history, complete
            ? "已读取 " + history.length + " 条相关安装记录。"
            : "安装历史未完整读取，当前仅显示已返回记录，不能确认请求可以清理。");
        if(pending && original && !flsInstallHistoryItemSafe(original)){{
            flsInstallSetStatus("原安装仍处于 " + original.status + " 状态或需要恢复；保留原请求，不允许新安装。");
        }}else if(!safe){{
            flsInstallSetStatus(complete
                ? "历史中存在运行中、未知、部分完成或结构未识别的安装；请先处理原记录。"
                : "安装历史不完整，未清理原请求；请稍后重新核对。");
        }}else{{
            flsInstallSetStatus(pending && original
                ? "原安装已确认处于终态，可以确认清理本地请求。"
                : pending ? "完整历史中未发现原请求且无未决安装，可以确认清理本地请求。"
                    : "完整安装历史均为已知终态，可以确认清理损坏状态。");
            const clear = document.getElementById("onlineInstallRecoveryClear");
            if(clear){{ clear.hidden = false; clear.onclick = () => flsInstallClearPending(state.raw); }}
            window.__FLS_INSTALL_VERIFIED_RAW__ = state.raw;
        }}
    }}catch(error){{
        flsInstallRenderHistory([], "安装历史读取失败：" + String(error && error.message || error));
        flsInstallSetStatus("安装历史未能核实；原始本地状态仍保留，不能启动新请求。");
        const login = document.getElementById("onlineInstallApiLogin");
        const denied = error && (error.status === 401 || error.status === 403);
        if(login){{
            login.hidden = !denied;
            if(denied) login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        }}
    }}finally{{ if(check) check.disabled = false; }}
}}

async function flsInstallClearPending(verifiedRaw){{
    if(window.__FLS_INSTALL_VERIFIED_RAW__ !== verifiedRaw){{
        flsInstallSetStatus("本地安装请求在核对后发生变化，请重新查询安装历史。");
        return;
    }}
    if(!confirm("已核对完整安装历史。确认清理原本地请求状态并重新选择吗？")) return;
    try{{
        const current = flsInstallStorageState();
        if(current.kind === "unavailable" || current.raw !== verifiedRaw) throw new Error("本地安装请求状态已变化");
        sessionStorage.removeItem(ONLINE_INSTALL_STORAGE_KEY);
        if(sessionStorage.getItem(ONLINE_INSTALL_STORAGE_KEY) !== null) throw new Error("本地请求状态未能清理");
        window.__FLS_INSTALL_VERIFIED_RAW__ = null;
        flsInstallSetStatus("已清理已核对的本地请求，正在重新载入选择页面。");
        if(window.location && typeof window.location.reload === "function") window.location.reload();
        else flsInstallLockForm(true);
    }}catch(error){{
        flsInstallSetStatus("未能清理原请求，原始状态仍保留：" + String(error && error.message || error));
    }}
}}

function flsInstallRestoreForm(pending){{
    const form = document.getElementById("onlineInstallSelectForm");
    if(!form || pending.script_id !== window.FLS_INSTALL_SCRIPT_ID) return;
    const request = form.querySelector('[name="request_id"]');
    if(request) request.value = pending.request_id || pending.install_id;
    if(typeof pending.excluded_task_indexes === "string")
        document.getElementById("excludedTaskIndexes").value = pending.excluded_task_indexes;
    const enable = form.querySelector('[name="enable_task"]');
    if(enable && typeof pending.enable_task === "boolean") enable.checked = pending.enable_task;
    const importTask = form.querySelector('[name="import_task"]');
    if(importTask && typeof pending.import_task === "boolean") importTask.checked = pending.import_task;
    const proxy = form.querySelector('[name="proxy_id"]');
    if(proxy && typeof pending.proxy_id === "string") proxy.value = pending.proxy_id;
}}

function flsInstallGoTaskPage(page){{
    window.location.href = flsInstallBuildUrl(String(page || 1));
}}

document.getElementById("onlineInstallSelectForm").addEventListener("submit", function(e){{
    if(this.__flsInstallRecoveryLocked){{
        e.preventDefault();
        flsInstallSetStatus("原安装请求尚未核实；请先查询原状态或安装历史，不会启动新请求。");
        return false;
    }}
    flsSyncVisibleCheckboxesToExcluded();

    const submitter = e.submitter;
    const action = (submitter && submitter.getAttribute("formaction")) || this.getAttribute("action") || "";
    if(action.indexOf("/online-scripts/install/") < 0 && action.indexOf("/online-scripts/import-tasks/") < 0) return;

    var importTask = document.querySelector('#onlineInstallSelectForm input[name="import_task"]');
    var excluded = flsParseExcluded();

    if(importTask && importTask.checked && excluded.size >= window.FLS_TOTAL_TASKS){{
        e.preventDefault();
        alert("已勾选导入任务，但没有选择任何任务");
        return false;
    }}

    e.preventDefault();
    e.stopImmediatePropagation();
    flsInstallSubmitApi(action, this, submitter);
}});

async function flsInstallSubmitApi(action, form, submitter, allowOriginalRetry){{
    const errorEl = document.getElementById("onlineInstallApiError");
    const loginEl = document.getElementById("onlineInstallApiLogin");
    const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
    const button = submitter || form.querySelector('button[type="submit"]');
    if(form.__flsInstallSubmitting) return;
    form.__flsInstallSubmitting = true;
    if(button) button.disabled = true;
    if(errorEl){{ errorEl.hidden = true; errorEl.textContent = "正在提交安装请求..."; }}
    if(loginEl) loginEl.hidden = true;

    const requestIdInput = form.querySelector('input[name="request_id"]');
    const requestId = requestIdInput ? requestIdInput.value : "";
    const importMode = action.indexOf("/online-scripts/import-tasks/") >= 0;
    let requestState = null;
    let pendingValue = null;
    try{{
        if(!/^[a-f0-9]{{32}}$/.test(requestId)) throw new Error("安装请求标识无效，未发送请求");
        const current = flsInstallStorageState();
        if(current.kind === "unavailable") throw new Error("无法读取本地安装请求状态，未发送请求");
        if(current.kind === "corrupt"){{
            flsInstallLockForm(true);
            flsInstallShowHistory(null);
            flsInstallSetStatus("本地安装请求状态损坏；原始值已保留，先核对安装历史。");
            throw new Error("本地安装请求状态损坏，未发送请求");
        }}
        if(current.kind === "valid"){{
            const sameRequest = allowOriginalRetry && current.request_id === requestId
                && current.value.script_id === window.FLS_INSTALL_SCRIPT_ID
                && current.value.action === action && importMode;
            if(!sameRequest){{
                flsInstallLockForm(true);
                flsInstallShowHistory(current.value);
                flsInstallSetStatus("已有原安装请求尚未确认；不会用新请求覆盖其身份。");
                throw new Error("请先恢复或核对原安装请求");
            }}
            pendingValue = current.value;
            requestState = current.raw;
        }}else{{
            const enableInput = form.querySelector('[name="enable_task"]');
            const importInput = form.querySelector('[name="import_task"]');
            const proxyInput = form.querySelector('[name="proxy_id"]');
            const excludedInput = document.getElementById("excludedTaskIndexes");
            pendingValue = {{
                request_id:requestId, script_id:window.FLS_INSTALL_SCRIPT_ID, action,
                import_task:!!(importInput && importInput.checked),
                excluded_task_indexes:excludedInput ? excludedInput.value || "" : "",
                enable_task:!!(enableInput && enableInput.checked),
                proxy_id:proxyInput ? proxyInput.value || "" : "", force:false
            }};
            requestState = flsInstallWritePending(pendingValue);
        }}
    }}catch(error){{
        if(errorEl){{ errorEl.hidden = false; errorEl.textContent = error && error.message || "无法保存安装请求状态"; }}
        if(button) button.disabled = !!form.__flsInstallRecoveryLocked;
        form.__flsInstallSubmitting = false;
        return;
    }}
    const data = new FormData(form);
    data.delete("visible_task_indexes");
    const apiAction = action.replace("/online-scripts/install/", "/api/online-scripts/install/")
        .replace("/online-scripts/import-tasks/", "/api/online-scripts/import-tasks/");
    const headers = {{"X-Requested-With": "XMLHttpRequest"}};
    if(csrf) headers["X-CSRF-Token"] = csrf;
    try{{
        let response = await fetch(apiAction, {{method:"POST", body:data, headers, credentials:"same-origin"}});
        let json;
        try{{
            json = await flsReadJsonResponse(response);
        }}catch(readError){{
            if(!importMode && response.status === 409 && readError && readError.payload) json = readError.payload;
            else throw readError;
        }}
        if(response.status === 409 && json && json.confirm_required){{
            if(!window.confirm("目标文件已存在，确定覆盖并继续安装吗？")){{
                flsInstallClearKnownRejected(requestState);
                flsInstallLockForm(false);
                if(button) button.disabled = false;
                if(errorEl){{ errorEl.hidden = false; errorEl.textContent = "已取消覆盖，未启动安装。"; }}
                return;
            }}
            data.set("force", "1");
            pendingValue = Object.assign({{}}, pendingValue, {{force:true}});
            requestState = flsInstallWritePending(pendingValue, requestState);
            response = await fetch(apiAction, {{method:"POST", body:data, headers, credentials:"same-origin"}});
            json = await flsReadJsonResponse(response);
        }}
        if(!json || json.ok !== true) throw Object.assign(new Error(json && json.msg || "安装请求未完成"), {{status:response.status, payload:json}});
        if(json.request_id !== requestId) throw new Error("安装响应身份与原请求不一致，请查询原安装记录");
        if(importMode){{
            window.location.href = "/online-scripts/import-tasks-status/" + encodeURIComponent(requestId);
            return;
        }}
        if(json.install_id !== requestId) throw new Error("安装响应标识无效，请查询原请求");
        window.location.href = "/online-scripts/log/" + encodeURIComponent(requestId);
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
        if(importMode){{
            const rejected = error && error.payload && error.payload.accepted === false;
            const full = rejected && error.payload.history_full === true;
            if(rejected){{
                try{{ flsInstallClearKnownRejected(requestState); flsInstallLockForm(false); }}
                catch(_error){{ flsInstallLockForm(true); }}
            }}else{{
                flsInstallLockForm(true);
                flsInstallShowHistory(pendingValue);
            }}
            const history = document.getElementById("onlineInstallRecoveryHistory");
            if(history) history.hidden = !full;
            const statusLink = document.getElementById("onlineInstallRecoveryStatusLink");
            if(statusLink){{ statusLink.hidden = rejected; statusLink.href = "/online-scripts/import-tasks-status/" + encodeURIComponent(requestId); }}
            const query = document.getElementById("onlineInstallRecoveryRetry");
            if(query){{ query.hidden = rejected; query.onclick = flsInstallRecoverPending; }}
        }}else{{
            const rejected = error && error.payload && error.payload.accepted === false;
            if(rejected){{
                try{{ flsInstallClearKnownRejected(requestState); flsInstallLockForm(false); }}
                catch(_error){{ flsInstallLockForm(true); }}
            }}else{{
                flsInstallLockForm(true);
                flsInstallShowHistory(pendingValue);
                if(error && error.payload && error.payload.log_url){{
                    const link = document.getElementById("onlineInstallRecoveryLog");
                    if(link){{ link.hidden = false; link.href = error.payload.log_url.replace("/api/online-scripts/log/", "/online-scripts/log/"); }}
                }}
            }}
        }}
    }}finally{{ form.__flsInstallSubmitting = false; }}
}}

async function flsInstallRecoverPending(){{
    const statusEl = document.getElementById("onlineInstallRecoveryStatus");
    const loginEl = document.getElementById("onlineInstallApiLogin");
    const logEl = document.getElementById("onlineInstallRecoveryLog");
    const importStatusEl = document.getElementById("onlineInstallRecoveryStatusLink");
    const reloadSchedulerEl = document.getElementById("onlineInstallRecoveryReload");
    const confirmImportEl = document.getElementById("onlineInstallRecoveryConfirm");
    const retryEl = document.getElementById("onlineInstallRecoveryRetry");
    const local = flsInstallStorageState();
    if(local.kind === "empty"){{ flsInstallLockForm(false); return; }}
    flsInstallLockForm(true);
    if(local.kind === "unavailable"){{
        flsInstallShowHistory(null);
        flsInstallSetStatus("无法读取本地安装请求状态；不会启动新请求。请检查浏览器存储权限后重试。");
        return;
    }}
    if(local.kind === "corrupt"){{
        flsInstallShowHistory(null);
        flsInstallSetStatus("本地安装请求状态损坏；原始值已保留，请先核对完整安装历史。");
        return;
    }}
    const pending = local.value;
    flsInstallRestoreForm(pending);
    flsApplyExcludedToVisible();
    const installId = local.request_id;
    const importMode = String(pending.action || "").indexOf("/import-tasks/") >= 0;
    flsInstallShowHistory(pending);
    if(importMode && importStatusEl){{ importStatusEl.hidden = false; importStatusEl.href = "/online-scripts/import-tasks-status/" + encodeURIComponent(installId); }}
    if(logEl && !importMode){{ logEl.hidden = false; logEl.href = "/online-scripts/log/" + encodeURIComponent(installId); }}
    const resumeEl = document.getElementById("onlineInstallRecoveryResume");
    if(resumeEl) resumeEl.hidden = true;

    async function read(){{
        if(retryEl) retryEl.disabled = true;
        if(statusEl){{ statusEl.hidden = false; statusEl.textContent = "正在读取原安装状态..."; }}
        if(loginEl) loginEl.hidden = true;
        if(reloadSchedulerEl) reloadSchedulerEl.hidden = true;
        if(confirmImportEl) confirmImportEl.hidden = true;
        if(resumeEl) resumeEl.hidden = true;
        try{{
            const response = await fetch(
                importMode
                    ? "/api/online-scripts/import-tasks-status/" + encodeURIComponent(installId)
                    : "/api/online-scripts/log/" + encodeURIComponent(installId) + "?state_only=1",
                {{cache:"no-store", credentials:"same-origin"}}
            );
            const json = await flsReadJsonResponse(response);
            if(importMode){{
                if(!json || json.ok !== true || !json.state || json.request_id !== installId) throw new Error("导入状态响应无效");
                if(importStatusEl){{ importStatusEl.hidden = false; importStatusEl.href = "/online-scripts/import-tasks-status/" + encodeURIComponent(installId); }}
                if(json.state === "completed"){{
                    flsInstallClearKnownRejected(local.raw);
                    flsInstallLockForm(false);
                    if(statusEl) statusEl.textContent = json.message || "原任务导入已完成，未重复写入";
                    window.location.href = "/online-scripts?msg=" + encodeURIComponent(json.message || "任务导入已完成");
                    return;
                }}
                if(statusEl) statusEl.textContent = "原任务导入请求：" + (json.archived ? "已归档，不能继续导入或恢复调度器；" : "") + (json.message || json.state || "未知");
                if(json.confirmation_stale || json.confirmation_check_error){{
                    if(statusEl) statusEl.textContent += "。原未写入确认已失效或无法复核，请检查当前任务。";
                }}
                if(json.state === "failed" && json.retry_allowed && !json.archived && resumeEl){{
                    resumeEl.hidden = false;
                    resumeEl.onclick = () => {{
                        const form = document.getElementById("onlineInstallSelectForm");
                        if(!form) return;
                        const retryButton = Array.from(form.querySelectorAll('button[type="submit"]'))
                            .find(button => String(button.getAttribute("formaction") || "").indexOf("/online-scripts/import-tasks/") >= 0);
                        flsInstallRestoreForm(pending);
                        return flsInstallSubmitApi(pending.action, form, retryButton, true);
                    }};
                }}
                async function recoverImport(action, button, label){{
                    if(button.__flsRecovering) return;
                    button.__flsRecovering = true;
                    button.disabled = true;
                    try{{
                        const csrf = typeof flsGetCsrfToken === "function" ? flsGetCsrfToken() : "";
                        const headers = {{"X-Requested-With":"XMLHttpRequest"}};
                        if(csrf) headers["X-CSRF-Token"] = csrf;
                        const response = await fetch("/api/online-scripts/import-tasks-" + action + "/" + encodeURIComponent(installId), {{method:"POST", headers, credentials:"same-origin"}});
                        const result = await flsReadJsonResponse(response);
                        if(!result || result.ok !== true || result.request_id !== installId) throw new Error("原请求恢复响应无效，请查询原导入请求");
                        await read();
                    }}catch(error){{
                        if(statusEl) statusEl.textContent = label + "失败：" + (error && error.message || String(error || "请求失败")) + "。请查询原导入请求。";
                        const denied = error && (error.status === 401 || error.status === 403);
                        if(loginEl){{
                            loginEl.hidden = !denied;
                            if(denied) loginEl.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
                        }}
                    }}finally{{ button.disabled = false; button.__flsRecovering = false; }}
                }}
                if(confirmImportEl && !json.active && !json.archived && (json.state === "unknown" || json.confirmation_stale || json.confirmation_check_error)){{
                    confirmImportEl.hidden = false;
                    confirmImportEl.onclick = () => recoverImport("confirm", confirmImportEl, "原导入结果检查");
                }}
                if(reloadSchedulerEl && json.state === "scheduler_pending" && !json.archived && !json.active && !json.confirmation_stale && !json.confirmation_check_error){{
                    reloadSchedulerEl.hidden = false;
                    reloadSchedulerEl.onclick = () => recoverImport("reload", reloadSchedulerEl, "调度器重载");
                }}
                return;
            }}
            if(!json || json.ok !== true || json.install_id !== installId
                || typeof json.running !== "boolean" || typeof json.status !== "string") throw new Error("安装状态响应无效");
            if(statusEl) statusEl.textContent = "原安装请求：" + (json.status || "未知") + (json.phase ? "（" + json.phase + "）" : "");
            if(logEl){{ logEl.hidden = false; logEl.href = "/online-scripts/log/" + encodeURIComponent(installId); }}
            if(retryEl) retryEl.hidden = false;
            if(!json.running && ONLINE_INSTALL_TERMINAL_STATUSES.has(json.status)
                && !(json.recovery && json.recovery.available) && !json.scheduler_reload_pending){{
                flsInstallSetStatus("原安装已明确结束（" + json.status + "），可确认清理本地请求后重新选择。");
                flsInstallEnableClear(local.raw);
            }}else{{
                if(statusEl) statusEl.textContent += "；请先查看原安装日志或安装历史继续恢复。";
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


def _install_selection_unavailable(script_id, message, status_code):
    body = page_header_card(
        "在线任务选择无法打开", help_html=h(message),
        actions_html='<a class="btn btn-blue" href="/online-scripts/import-tasks-history">任务导入历史</a><a class="btn btn-orange" href="/online-scripts/source">检查在线脚本源数据</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
    )
    body += f'''<div id="originalImportRecovery"></div><script>
(function(){{
    try{{
        const saved=JSON.parse(sessionStorage.getItem("fls-online-install-request") || "null");
        if(!saved || saved.script_id!=={json.dumps(script_id)} || !/^[a-f0-9]{{32}}$/.test(saved.request_id || "") || String(saved.action || "").indexOf("/import-tasks/")<0) return;
        const link=document.createElement("a");link.className="btn btn-primary";
        link.href="/online-scripts/import-tasks-status/"+encodeURIComponent(saved.request_id);
        link.textContent="查询原导入请求";document.getElementById("originalImportRecovery").appendChild(link);
    }}catch(_error){{}}
}})();
</script>'''
    return layout("在线任务选择", "online_scripts", body), status_code
