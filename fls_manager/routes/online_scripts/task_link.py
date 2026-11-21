import re
import uuid

from ._common import *
from ...online_scripts.source import (
    OnlineScriptCacheConflict,
    TaskLinkHistoryFull,
    TaskLinkReceiptMissing,
    get_task_link_request,
    claim_task_link_request,
    merge_task_link_tasks,
    online_task_link_version,
    online_script_task_fingerprint,
    load_online_script_cache,
    list_task_link_requests,
    persist_task_link_request,
)
from ...online_scripts.constants import (
    ONLINE_TASK_LINK_ACTIVE,
    get_online_refresh_task_link_failure,
    get_online_refresh_task_link_recovery,
    online_refresh_task_link_request_id,
    persist_online_refresh_task_link_recovery,
)
from ...utils import now_str
from ...storage import storage_transaction


_STALE_TASK_LINK_CONFIRMATION = (
    "原未合并确认已因任务缓存变化失效，不能继续原同步请求；请核对当前任务后再创建新请求。"
)


def _task_link_error(
    script_id, message, status_code, proxy_id="", request_id="", retry=False,
    history_full=False, origin_refresh_request_id="",
):
    retry_form = ""
    if retry:
        hidden = f'<input type="hidden" name="request_id" value="{h(request_id)}">' if request_id else ""
        origin = (
            f"/refresh-task-link/{h(origin_refresh_request_id)}"
            if re.fullmatch(r"[a-f0-9]{32}", origin_refresh_request_id or "") else ""
        )
        action = f"/online-scripts{origin}/task-link/{h(quote(script_id, safe=''))}"
        api_action = f"/api{action[len('/online-scripts'):]}"
        origin_input = (
            f'<input type="hidden" name="origin_refresh_request_id" value="{h(origin_refresh_request_id)}">'
            if origin else ""
        )
        retry_form = f'''
<form class="inline-form" method="post" action="{action}" data-api-action="{api_action}">
    {hidden}{origin_input}
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
            + ('<a class="btn btn-primary" href="/online-scripts/task-link-history?recovery=1">处理待恢复同步</a>' if history_full else "")
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


def _refresh_task_link_recovery_result(request_id, script_id, origin_refresh_request_id, recovery):
    if (request_id != recovery.get("request_id")
            or request_id != online_refresh_task_link_request_id(origin_refresh_request_id, script_id)):
        return None
    return {
        "ok": True,
        "request_id": request_id,
        "origin_refresh_request_id": origin_refresh_request_id,
        "state": "completed",
        "added": recovery.get("added", 0),
        "total": recovery.get("total", 0),
        "msg": f"原刷新失败项已恢复，新增 {recovery.get('added', 0)} 个任务，未重复请求外部任务源",
        "retry_allowed": False,
        "replayed": True,
        "status_url": f"/online-scripts?refresh_request_id={origin_refresh_request_id}#online-status",
    }


def _existing_task_link_result(record, script_id, proxy_id, expected_version="", origin_refresh_request_id=""):
    if not record:
        return None
    if (record.get("script_id") != script_id or record.get("proxy_id") != proxy_id
            or str(record.get("origin_refresh_request_id") or "") != origin_refresh_request_id
            or (record.get("state") == "failed" and expected_version
                and record.get("expected_version") != expected_version)):
        return {"ok": False, "request_id": record["request_id"],
                "msg": "同步请求已绑定其他脚本、代理或旧版本，请刷新页面后重新选择。",
                "retry_allowed": False}, 409
    if record.get("state") == "failed" and not record.get("active") and not record.get("archived"):
        return None
    request_id = record["request_id"]
    state = record["state"]
    messages = {
        "completed": "外部任务源同步已完成，未重复请求",
        "unknown": "该同步请求的发布结果未知，请先查询原同步请求和当前缓存，确认后再创建新的同步请求。",
    }
    message = ("原同步请求已归档，不能继续或重新确认；请核对当前任务。" if record.get("archived")
               else messages.get(state, "该同步请求仍在处理中，请查询原请求后再重试。"))
    diagnostic = record.get("error") or ""
    return {
        "ok": state == "completed", "request_id": request_id, "state": state,
        "added": record.get("added", 0), "total": record.get("total", 0),
        "msg": message + ("；" + diagnostic if diagnostic else ""), "diagnostic": diagnostic,
        "retry_allowed": False, "replayed": True,
        "archived": bool(record.get("archived")),
        "state_url": f"/api/online-scripts/task-link-status/{request_id}",
        "status_url": f"/online-scripts/task-link-status/{request_id}",
    }, 200 if state == "completed" else 409


def _stale_task_link_confirmation(record):
    request_id = record.get("request_id", "")
    return {
        "ok": False,
        "request_id": request_id,
        "state": record.get("state", "unknown"),
        "confirmation_stale": True,
        "confirmation_error": _STALE_TASK_LINK_CONFIRMATION,
        "msg": _STALE_TASK_LINK_CONFIRMATION + " 未发起外部请求。",
        "retry_allowed": False,
        "status_url": f"/online-scripts/task-link-status/{request_id}",
    }, 409


def _task_link_confirmation_matches_current_cache(record):
    current_items = load_online_script_cache()
    current_item = next(
        (item for item in current_items if item.get("id") == record.get("script_id")),
        None,
    )
    return (
        current_item is not None
        and online_task_link_version(current_item) == record.get("expected_version")
        and online_script_task_fingerprint(current_item) == record.get("confirmation_fingerprint")
    )


def _task_link_status_record(request_id):
    with storage_transaction():
        record = get_task_link_request(request_id)
        if record:
            record["retry_allowed"] = record.get("state") == "failed" and not record.get("active") and not record.get("archived")
        if record and record.get("archived"):
            return record
        if not record or record.get("confirmation") != "not_merged":
            return record
        try:
            matches = _task_link_confirmation_matches_current_cache(record)
        except Exception as exc:
            record["confirmation_check_error"] = str(exc)
            record["retry_allowed"] = False
            return record
        record["confirmation_stale"] = not matches
        record["retry_allowed"] = matches and not record.get("active")
        if not matches:
            record["confirmation_error"] = _STALE_TASK_LINK_CONFIRMATION
        return record


def _missing_task_link_receipt(request_id, exc):
    return {"ok": False, "request_id": request_id, "msg": str(exc), "active": True,
            "receipt_missing": True, "retry_allowed": False,
            "status_url": f"/online-scripts/task-link-status/{request_id}"}, 503


def _submit_task_link(script_id, proxy_id, raw_request_id, origin_refresh_request_id=""):
    try:
        request_id = _request_id(raw_request_id)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc), "request_id": "", "retry_allowed": False}, 400
    if origin_refresh_request_id and not re.fullmatch(r"[a-f0-9]{32}", origin_refresh_request_id):
        return {"ok": False, "msg": "原刷新请求标识无效", "request_id": request_id, "retry_allowed": False}, 400

    if origin_refresh_request_id:
        try:
            recovery = get_online_refresh_task_link_recovery(origin_refresh_request_id, script_id)
        except Exception as exc:
            return {"ok": False, "request_id": request_id,
                    "msg": f"原刷新失败项恢复回执无法读取：{exc}；未发起外部请求。",
                    "retry_allowed": False}, 503
        if recovery:
            replay = _refresh_task_link_recovery_result(
                request_id, script_id, origin_refresh_request_id, recovery,
            )
            if replay:
                if proxy_id != recovery.get("proxy_id"):
                    return {"ok": False, "request_id": request_id,
                            "msg": "原刷新失败项已按其他代理完成恢复；未发起外部请求。",
                            "retry_allowed": False}, 409
                return replay, 200

    try:
        previous = get_task_link_request(request_id, script_id=script_id, proxy_id=proxy_id)
    except TaskLinkReceiptMissing as exc:
        return _missing_task_link_receipt(request_id, exc)
    except Exception as exc:
        return {"ok": False, "request_id": request_id,
                "msg": f"同步回执无法读取，未发起外部请求：{exc}", "retry_allowed": False}, 500
    existing = _existing_task_link_result(
        previous, script_id, proxy_id, origin_refresh_request_id=origin_refresh_request_id,
    )
    if existing:
        return existing

    try:
        item = get_online_script(script_id)
    except Exception as exc:
        return {"ok": False, "request_id": request_id,
                "msg": f"在线脚本源缓存无法读取，未覆盖原文件：{exc}。请检查源数据后重试。",
                "retry_allowed": False}, 500
    if not item:
        return {"ok": False, "request_id": request_id,
                "msg": "脚本已不在本地缓存中，请刷新在线脚本列表。", "retry_allowed": False}, 404

    if origin_refresh_request_id:
        try:
            origin_failure = get_online_refresh_task_link_failure(origin_refresh_request_id, script_id)
        except LookupError as exc:
            return {"ok": False, "request_id": request_id, "msg": str(exc), "retry_allowed": False}, 404
        except Exception as exc:
            return {"ok": False, "request_id": request_id,
                    "msg": f"原刷新失败源记录无法读取，未发起外部请求：{exc}", "retry_allowed": False}, 503
        if (
            not origin_failure
            or (request_id != origin_failure.get("task_link_request_id")
                and not (previous and previous.get("origin_refresh_request_id") == origin_refresh_request_id))
            or str(origin_failure.get("proxy_id") or "") != proxy_id
            or str(origin_failure.get("task_link") or "") != str(item.get("task_link") or "").strip()
        ):
            return {"ok": False, "request_id": request_id,
                    "msg": "当前脚本或代理已不同于原刷新失败记录，未发起外部请求；请刷新后重新选择。",
                    "retry_allowed": False}, 409

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
    existing = _existing_task_link_result(
        previous, script_id, proxy_id, expected_version, origin_refresh_request_id,
    )
    if existing:
        return existing

    expected_tasks_fingerprint = online_script_task_fingerprint(item)
    if (previous and previous.get("confirmation") == "not_merged"
            and previous.get("confirmation_fingerprint") != expected_tasks_fingerprint):
        return _stale_task_link_confirmation(previous)
    record = {
        "request_id": request_id,
        "script_id": script_id,
        "proxy_id": proxy_id,
        "origin_refresh_request_id": origin_refresh_request_id,
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
    except TaskLinkHistoryFull as exc:
        return {"ok": False, "request_id": request_id, "msg": str(exc), "history_full": True,
                "accepted": False, "retry_allowed": False,
                "history_url": "/online-scripts/task-link-history?recovery=1"}, 409
    except TaskLinkReceiptMissing as exc:
        return _missing_task_link_receipt(request_id, exc)
    except Exception as exc:
        return {"ok": False, "request_id": request_id,
                "msg": f"同步请求无法保存，未发起外部请求：{exc}", "retry_allowed": True}, 500
    if claimed_previous:
        if "_confirmation_error" in claimed_previous:
            return {"ok": False, "request_id": request_id,
                    "msg": f"任务缓存无法复核原确认：{claimed_previous['_confirmation_error']}；未发起外部请求。",
                    "retry_allowed": False}, 503
        if claimed_previous.get("_confirmation_stale"):
            return _stale_task_link_confirmation(claimed_previous)
        return _existing_task_link_result(claimed_previous, script_id, proxy_id, expected_version)

    try:
        try:
            _record_update(record, "running")
            persist_task_link_request(record)
        except Exception as exc:
            return _persist_failure(record, f"同步请求无法保存，未发起外部请求：{exc}", 500)
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
        if origin_refresh_request_id:
            try:
                persist_online_refresh_task_link_recovery(
                    origin_refresh_request_id, script_id, proxy_id=proxy_id,
                    added=added, total=total,
                )
            except Exception as exc:
                _record_update(
                    record, "unknown", error=f"任务已合并但刷新失败项恢复回执无法保存：{exc}",
                    added=added, total=total,
                )
                record["published_tasks_fingerprint"] = published_tasks_fingerprint
                try:
                    persist_task_link_request(record)
                except Exception:
                    pass
                return {
                    "ok": False, "request_id": request_id,
                    "msg": f"任务已可能合并，但恢复结果无法持久确认：{exc}；请查询原同步请求。",
                    "retry_allowed": False, "resolved": False,
                }, 503
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


def _render_task_link_result(script_id, payload, status_code, proxy_id, origin_refresh_request_id=""):
    if payload.get("ok"):
        if payload.get("replayed"):
            body = page_header_card(
                "外部任务源原同步结果",
                help_html=h(payload.get("msg")),
                actions_html=(
                    f'<a class="btn btn-blue" href="{h(payload["status_url"])}">查询原同步请求</a>'
                    '<a class="btn btn-gray" href="/online-scripts/task-link-history">同步历史</a>'
                    '<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
                ),
            )
            return layout("外部任务源同步", "online_scripts", body), status_code
        return redirect(url_for(
            "online_scripts.online_scripts_page",
            msg=payload.get("msg") or "外部任务源同步完成",
        ))
    return _task_link_error(
        script_id,
        payload.get("msg") or "外部任务源同步未完成",
        status_code,
        proxy_id=proxy_id,
        request_id="" if payload.get("history_full") else payload.get("request_id", ""),
        retry=bool(payload.get("retry_allowed")),
        history_full=bool(payload.get("history_full")),
        origin_refresh_request_id=origin_refresh_request_id,
    )


def _confirm_task_link(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return {"ok": False, "msg": "在线任务源同步请求标识无效"}, 400
    with storage_transaction():
        return _confirm_task_link_locked(request_id)


def _confirm_task_link_locked(request_id):
    """Resolve an interrupted merge by comparing the bound cache fingerprints."""
    try:
        record = get_task_link_request(request_id)
    except TaskLinkReceiptMissing as exc:
        return _missing_task_link_receipt(request_id, exc)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "msg": f"同步回执无法读取：{exc}"}, 500
    if not record:
        return {"ok": False, "request_id": request_id, "msg": "同步请求不存在"}, 404
    if record.get("archived") or record.get("active"):
        return {"ok": False, "request_id": request_id, "archived": bool(record.get("archived")),
                "msg": "原同步请求已归档或仍在处理中，不能确认。", "retry_allowed": False}, 409

    confirmation = record.get("confirmation")
    if confirmation in ("merged", "not_merged"):
        if confirmation == "not_merged":
            try:
                current_matches = _task_link_confirmation_matches_current_cache(record)
            except Exception as exc:
                return {"ok": False, "request_id": request_id,
                        "msg": f"任务缓存无法复核原确认：{exc}；未修改回执。"}, 503
            if not current_matches:
                return _stale_task_link_confirmation(record)
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
        _record_update(
            record, "completed", added=record.get("added", 0), total=record.get("total", 0),
        )
        record["confirmation"] = "merged"
        record["confirmation_fingerprint"] = current_fingerprint
        record["error"] = ""
        if record.get("origin_refresh_request_id"):
            try:
                persist_online_refresh_task_link_recovery(
                    record["origin_refresh_request_id"], record["script_id"],
                    proxy_id=record.get("proxy_id", ""), added=record.get("added", 0),
                    total=record.get("total", 0),
                )
            except Exception as exc:
                return {
                    "ok": False, "request_id": request_id,
                    "msg": f"任务已确认合并，但刷新失败项恢复回执无法保存：{exc}；请查询原同步请求。",
                    "retry_allowed": False,
                }, 503
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


def _archive_task_link(request_id, choice):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or "") or choice != "archive":
        return {"ok": False, "msg": "请明确确认归档原同步请求；未修改回执或任务。"}, 400
    with storage_transaction():
        try:
            record = get_task_link_request(request_id)
        except TaskLinkReceiptMissing as exc:
            return _missing_task_link_receipt(request_id, exc)
        except Exception as exc:
            return {"ok": False, "request_id": request_id, "msg": f"同步回执无法读取：{exc}"}, 500
        if not record:
            return {"ok": False, "request_id": request_id, "msg": "同步请求不存在"}, 404
        if record.get("active") or record.get("state") not in ("failed", "unknown"):
            return {"ok": False, "request_id": request_id,
                    "msg": "只能归档已停止的失败或未知同步请求。", "retry_allowed": False}, 409
        replayed = bool(record.get("archived"))
        if not replayed:
            record.update(archived=True, archived_at=now_str())
            try:
                persist_task_link_request(record)
            except Exception as exc:
                return {"ok": False, "request_id": request_id, "retry_allowed": False,
                        "msg": f"归档结果无法确认，请查询原同步请求后重试：{exc}"}, 503
        return {"ok": True, "request_id": request_id, "state": record["state"],
                "archived": True, "replayed": replayed, "retry_allowed": False,
                "msg": "原同步请求已归档，当前任务未修改。"}, 200


@bp.route("/api/online-scripts/task-link-archive/<request_id>", methods=["POST"])
def api_online_scripts_task_link_archive(request_id):
    data = (request.get_json(silent=True) if request.is_json else request.form) or {}
    choice = data.get("acknowledge") if hasattr(data, "get") else None
    payload, status_code = _archive_task_link(request_id, choice)
    return jsonify(payload), status_code


@bp.route("/online-scripts/task-link-archive/<request_id>", methods=["POST"])
def online_scripts_task_link_archive(request_id):
    payload, status_code = _archive_task_link(request_id, request.form.get("acknowledge"))
    if payload.get("ok"):
        return redirect(url_for("online_scripts.online_scripts_task_link_status", request_id=request_id))
    body = page_header_card(
        "外部任务源归档未完成", help_html=h(payload.get("msg")),
        actions_html=(f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">查询原同步请求</a>'
                      '<a class="btn btn-gray" href="/online-scripts/task-link-history?recovery=1">待恢复同步</a>'),
    )
    return layout("外部任务源归档", "online_scripts", body), status_code


@bp.route("/online-scripts/task-link/<script_id>", methods=["POST"])
def online_scripts_pull_task_link(script_id):
    proxy_id = request.form.get("proxy_id", "").strip()
    payload, status_code = _submit_task_link(script_id, proxy_id, request.form.get("request_id"))
    return _render_task_link_result(script_id, payload, status_code, proxy_id)


@bp.route("/online-scripts/refresh-task-link/<refresh_request_id>/task-link/<script_id>", methods=["POST"])
def online_scripts_pull_refresh_task_link(refresh_request_id, script_id):
    proxy_id = request.form.get("proxy_id", "").strip()
    payload, status_code = _submit_task_link(
        script_id, proxy_id, request.form.get("request_id"), refresh_request_id,
    )
    return _render_task_link_result(script_id, payload, status_code, proxy_id, refresh_request_id)


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


@bp.route("/api/online-scripts/refresh-task-link/<refresh_request_id>/task-link/<script_id>", methods=["POST"])
def api_online_scripts_pull_refresh_task_link(refresh_request_id, script_id):
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    submitted_origin = str(data.get("origin_refresh_request_id", "") or "").strip()
    if submitted_origin and submitted_origin != refresh_request_id:
        return jsonify(ok=False, msg="原刷新请求绑定不一致，未发起外部请求", retry_allowed=False), 409
    payload, status_code = _submit_task_link(
        script_id,
        str(data.get("proxy_id", "") or "").strip(),
        data.get("request_id"),
        refresh_request_id,
    )
    return jsonify(payload), status_code


@bp.route("/api/online-scripts/task-link-status/<request_id>")
def api_online_scripts_task_link_status(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id or ""):
        return jsonify(ok=False, msg="在线任务源同步请求标识无效"), 400
    try:
        record = _task_link_status_record(request_id)
    except TaskLinkReceiptMissing as exc:
        payload, status_code = _missing_task_link_receipt(request_id, exc)
        return jsonify(payload), status_code
    except Exception as exc:
        return jsonify(ok=False, msg=f"同步回执无法读取：{exc}"), 500
    if not record:
        return jsonify(ok=False, msg="同步请求不存在"), 404
    return jsonify(ok=True, **record)


@bp.route("/api/online-scripts/task-link-history")
def api_online_scripts_task_link_history():
    try:
        page = _task_link_history_page()
    except ValueError as exc:
        return jsonify(ok=False, msg=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, msg=f"同步历史无法读取：{exc}"), 500
    return jsonify(ok=True, **page)


def _task_link_history_page():
    recovery = request.args.get("recovery", "0")
    if recovery not in ("0", "1"):
        raise ValueError("待恢复同步筛选无效")
    return list_task_link_requests(request.args.get("limit", 50), request.args.get("script_id", ""),
                                   recovery=recovery == "1", offset=request.args.get("offset", 0),
                                   with_pagination=True)


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
        record = _task_link_status_record(request_id)
    except Exception as exc:
        body = page_header_card(
            "外部任务源同步状态",
            help_html=f"同步回执无法读取：{h(exc)}。请修复存储后重新查询。",
            actions_html=(
                f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">重新查询</a>'
                '<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
            ),
        )
        return layout("外部任务源同步状态", "online_scripts", body), 503 if isinstance(exc, TaskLinkReceiptMissing) else 500
    if not record:
        body = page_header_card(
            "外部任务源同步状态",
            help_html="同步请求不存在，可能已被清理或请求标识有误。",
            actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
        )
        return layout("外部任务源同步状态", "online_scripts", body), 404

    state = str(record.get("state") or "unknown")
    confirmation_stale = record.get("confirmation_stale") is True
    confirmation_check_error = str(record.get("confirmation_check_error") or "")
    labels = {
        "prepared": "已准备，尚未确认外部请求",
        "running": "正在请求外部任务源",
        "failed": "请求失败，可继续原同步请求",
        "unknown": "发布结果未知，请勿直接重发",
        "completed": "同步已完成",
    }
    state_label = labels.get(state, state)
    if record.get("archived"):
        state_label = "已归档（原结果：" + ("请求失败" if state == "failed" else "结果未知") + "）"
    elif confirmation_stale:
        state_label = "原未合并确认已失效，任务缓存已变化"
    elif confirmation_check_error:
        state_label = "原未合并确认暂时无法复核"
    action_html = ""
    if record.get("archived"):
        action_html = '<a class="btn btn-primary" href="/online-scripts">核对当前任务</a>'
    elif confirmation_stale or confirmation_check_error:
        action_html = (
            f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">重新查询</a>'
            '<a class="btn btn-primary" href="/online-scripts">核对当前任务</a>'
        )
    elif state == "failed" and not record.get("active") and (
        record.get("confirmation") != "not_merged" or record.get("retry_allowed") is True
    ):
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
    elif state in ("prepared", "running") or record.get("active"):
        action_html = f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">刷新查询</a>'
    archive_html = ""
    if not record.get("archived") and not record.get("active") and state in ("failed", "unknown"):
        archive_html = f'''
<form class="task-link-archive-form" method="post" action="/online-scripts/task-link-archive/{h(request_id)}" style="display:grid;gap:12px;margin-top:16px">
    <label style="display:flex;align-items:flex-start;gap:8px"><input type="checkbox" name="acknowledge" value="archive" required style="width:auto;flex:none"><span>保留当前任务并放弃此请求，允许清理其回执</span></label>
    <button class="btn btn-gray" type="submit" style="justify-self:start">归档原同步请求</button>
</form>'''
    action_html += '<a class="btn btn-gray" href="/online-scripts/task-link-history">同步历史</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
    details = f'''
<section class="section">
    <div class="card">
        <h2 class="section-title">{h(state_label)}</h2>
        <dl class="detail-list">
            <dt>请求标识</dt><dd><code>{h(request_id)}</code></dd>
            <dt>脚本</dt><dd>{h(record.get("script_id", "-"))}</dd>
            <dt>代理</dt><dd>{h(record.get("proxy_id") or "直连")}</dd>
            <dt>任务数</dt><dd>{h(record.get("total", 0))}（新增 {h(record.get("added", 0))}）</dd>
            <dt>更新时间</dt><dd>{h(record.get("updated_at") or "-")}</dd>
            {f'<dt>归档时间</dt><dd>{h(record.get("archived_at") or "-")}</dd>' if record.get("archived") else ''}
        </dl>
        {f'<p role="alert">{h(record.get("error"))}</p>' if record.get("error") else ''}
        {f'<p role="alert">{h(record.get("confirmation_error"))}</p>' if record.get("confirmation_error") else ''}
        {f'<p role="alert">原确认状态无法复核：{h(confirmation_check_error)}</p>' if confirmation_check_error else ''}
        {archive_html}
    </div>
</section>'''
    body = page_header_card(
        "外部任务源同步状态",
        help_html=(
            "原同步请求已归档，保留原结果；不会继续请求、确认合并或修改当前任务。"
            if record.get("archived") else _STALE_TASK_LINK_CONFIRMATION
            if confirmation_stale
            else "任务缓存暂时无法验证原确认；修复缓存后重新查询，期间不会继续原请求。"
            if confirmation_check_error
            else "该页面只查询已保存的同步回执，不会重复发起外部请求。未知状态请先检查当前任务合并；确认未合并后再执行重试。"
        ),
        actions_html=action_html,
    ) + details
    return layout("外部任务源同步状态", "online_scripts", body)


@bp.route("/online-scripts/task-link-history")
def online_scripts_task_link_history():
    script_id = str(request.args.get("script_id") or "").strip()
    try:
        page = _task_link_history_page()
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
            actions_html=f'<a class="btn btn-blue" href="{h(request.full_path)}">重新读取</a><a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
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
    for record in page["items"]:
        request_id = str(record.get("request_id") or "")
        state = str(record.get("state") or "unknown")
        actions = f'<a class="btn btn-blue" href="/online-scripts/task-link-status/{h(request_id)}">查看状态</a>'
        if state == "completed" or record.get("confirmation") == "merged":
            actions += f'<a class="btn btn-gray" href="/online-scripts/install-select/{h(quote(str(record.get("script_id") or ""), safe=""))}?task_link_request_id={h(request_id)}">选择已同步任务</a>'
        rows.append(
            "<tr>"
            f"<td><code>{h(request_id)}</code></td>"
            f"<td>{h(record.get('script_id') or '-')}</td>"
            f"<td>{h(labels.get(state, state))}{'（已归档）' if record.get('archived') else ''}</td>"
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
    recovery = request.args.get("recovery", "0")
    links = '<a class="btn btn-blue" href="/online-scripts/task-link-history?recovery=1">待恢复同步</a><a class="btn btn-gray" href="/online-scripts/task-link-history">全部同步</a>'
    for label, offset in (("上一页", max(0, page["offset"] - page["limit"])), ("下一页", page["offset"] + page["limit"])):
        if (label == "上一页" and page["offset"]) or (label == "下一页" and page["has_more"]):
            href = url_for("online_scripts.online_scripts_task_link_history", script_id=script_id,
                           recovery=recovery, limit=page["limit"], offset=offset)
            links += f'<a class="btn btn-blue" href="{h(href)}">{label}</a>'
    body = page_header_card(
        "外部任务源同步历史",
        help_html="历史只读取已保存回执，不会发起外部任务源请求；未知状态请进入详情检查当前任务合并。",
        actions_html=links + '<a class="btn btn-blue" href="/online-scripts">返回在线脚本</a>',
    ) + f'<section class="section"><div class="card">{table}</div></section>'
    return layout("外部任务源同步历史", "online_scripts", body)
