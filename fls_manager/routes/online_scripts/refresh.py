import re
import uuid

from ._common import *
from ...online_scripts.constants import (
    ONLINE_REFRESH_LOCK,
    ONLINE_REFRESH_STATE,
    claim_online_refresh_retry,
    confirm_online_refresh_result,
    get_online_refresh_history,
    get_online_refresh_request,
    get_online_refresh_submission,
    get_online_refresh_retry,
    reserve_online_refresh,
    recover_online_refresh_receipt,
    update_online_refresh_state,
)
from ...online_scripts.logs import online_refresh_log_file
from ...online_scripts.source import online_script_cache_fingerprint, online_script_source_fingerprint
from ...utils import now_str


def _refresh_request_id(raw):
    value = str(raw or "").strip() or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("在线脚本源刷新请求标识无效，请刷新页面后重试")
    return value


def _submit_online_refresh(proxy_id, raw_request_id=""):
    try:
        request_id = _refresh_request_id(raw_request_id)
    except ValueError as exc:
        return {"ok": False, "status": "参数错误", "error": str(exc), "request_id": ""}, 400
    try:
        info = get_online_refresh_submission(proxy_id, request_id)
        if info is None:
            try:
                validate_proxy_id(proxy_id)
            except ValueError as e:
                return {"ok": False, "status": "参数错误", "request_id": request_id, "error": str(e)}, 400
            except Exception as e:
                return {"ok": False, "status": "代理不可读", "request_id": request_id,
                        "error": f"读取代理配置失败：{e}"}, 500
            cache_fingerprint = online_script_cache_fingerprint()
            source_fingerprint = online_script_source_fingerprint()
            info = reserve_online_refresh(proxy_id, request_id, cache_fingerprint, source_fingerprint)
    except Exception as e:
        return {
            "ok": False, "status": "启动失败",
            "request_id": request_id,
            "error": f"提交在线脚本源同步失败：{e}。请检查状态文件权限和磁盘空间后重试。",
        }, 500
    if not info:
        return {"ok": False, "status": "冲突", "request_id": request_id,
                "error": "在线脚本源正在同步中，请等待当前任务结束"}, 409
    if info.get("_binding_conflict"):
        return {"ok": False, "status": "冲突", "request_id": request_id,
                "id": info.get("id", ""),
                "error": "刷新请求已绑定其他代理，请查询原请求或明确创建新的刷新请求"}, 409
    receipt = {
        "replayed": True, "request_id": request_id, "id": info.get("id", ""),
        "status": info.get("status", "结果未知"), "running": bool(info.get("running")),
        "cache_published": bool(info.get("cache_published")),
        "message": info.get("message") or "", "error": info.get("error") or "",
        "state_url": url_for("online_scripts.api_online_refresh_status_for_request", request_id=request_id),
        "log_url": url_for("online_scripts.online_refresh_log", request_id=request_id),
    }
    if info.get("_replay"):
        return {"ok": True, **receipt}, 200
    if info.get("_unknown"):
        return {"ok": False, **receipt,
                "error": info.get("error") or "原刷新请求已有回执，请查询原状态和日志后再明确创建新的刷新请求"}, 409
    if info.get("_running"):
        return {"ok": False, **receipt, "error": "在线脚本源正在同步中，请等待当前任务结束"}, 409

    log_file = ""
    try:
        log_file = str(online_refresh_log_file(info["id"]))
        start_refresh_thread(info["id"], info.get("proxy_id", ""), log_file)
    except Exception as e:
        error = f"启动在线脚本源同步线程失败：{e}"
        try:
            update_online_refresh_state(
                running=False, status="启动失败", message="", error=error,
                updated_at=now_str(), log_file=log_file,
            )
        except Exception as state_error:
            error += f"；同步状态保存失败：{state_error}。请检查状态文件权限和磁盘空间后重试。"
            with ONLINE_REFRESH_LOCK:
                ONLINE_REFRESH_STATE.update(
                    running=False,
                    status="启动失败",
                    message="",
                    error=error,
                    updated_at=now_str(),
                    log_file=log_file,
                )
        return {"ok": False, "id": info["id"], "request_id": request_id,
                "status": "启动失败", "error": error}, 500
    return {"ok": True, "id": info["id"], "request_id": request_id,
            "status": "运行中"}, 202


def _submit_online_refresh_retry(original_request_id, proxy_id, raw_request_id=""):
    try:
        request_id = _refresh_request_id(raw_request_id)
    except ValueError as exc:
        return {"ok": False, "status": "参数错误", "error": str(exc), "request_id": ""}, 400
    try:
        info = get_online_refresh_retry(original_request_id, proxy_id, request_id)
        if info is None:
            validate_proxy_id(proxy_id)
            cache_fingerprint = online_script_cache_fingerprint()
            source_fingerprint = online_script_source_fingerprint()
            info = claim_online_refresh_retry(
                original_request_id, proxy_id, request_id, cache_fingerprint, source_fingerprint,
            )
    except ValueError as exc:
        return {"ok": False, "status": "参数错误", "request_id": request_id, "error": str(exc)}, 400
    except Exception as exc:
        return {"ok": False, "status": "重试准备失败", "request_id": request_id,
                "error": f"准备在线脚本源重试失败：{exc}"}, 500
    if info is None:
        return {"ok": False, "status": "冲突", "request_id": request_id,
                "error": "已有在线脚本源同步正在运行，请等待结束后再明确重试"}, 409
    if info.get("_missing"):
        return {"ok": False, "status": "记录不存在", "request_id": request_id,
                "error": "原刷新记录不存在或已被清理，请从最新历史重新选择"}, 404
    if info.get("_not_retryable"):
        return {"ok": False, "status": "不可重试", "request_id": request_id,
                "error": "该刷新已完成或仍在运行，请查询原记录，不会重复抓取"}, 409
    if info.get("_binding_conflict"):
        return {"ok": False, "status": "冲突", "request_id": request_id,
                "error": "重试请求与原刷新记录或既有请求绑定不一致，请重新读取历史"}, 409
    if info.get("_stale"):
        return {"ok": False, "status": "缓存或源已变化", "request_id": request_id,
                "error": "原刷新后的缓存或在线脚本源地址已变化，请明确创建新的刷新请求，不会覆盖当前内容"}, 409
    if info.get("_replay"):
        return {"ok": True, "replayed": True, "request_id": request_id,
                "retry_of": original_request_id, "id": info.get("id", ""),
                "status": info.get("status", "运行中"), "running": bool(info.get("running")),
                "error": info.get("error") or "", "cache_published": bool(info.get("cache_published")),
                "state_url": url_for("online_scripts.api_online_refresh_status_for_request", request_id=request_id),
                "log_url": url_for("online_scripts.online_refresh_log", request_id=request_id)}, 200
    log_file = ""
    try:
        log_file = str(online_refresh_log_file(info["id"]))
        start_refresh_thread(info["id"], info.get("proxy_id", ""), log_file)
    except Exception as exc:
        error = f"启动在线脚本源重试线程失败：{exc}"
        try:
            update_online_refresh_state(
                running=False, status="启动失败", message="", error=error,
                updated_at=now_str(), log_file=log_file,
            )
        except Exception as state_error:
            error += f"；同步状态保存失败：{state_error}"
            with ONLINE_REFRESH_LOCK:
                ONLINE_REFRESH_STATE.update(
                    running=False, status="启动失败", message="", error=error,
                    persistence_error=str(state_error), updated_at=now_str(), log_file=log_file,
                )
        return {"ok": False, "id": info["id"], "request_id": request_id,
                "retry_of": original_request_id, "status": "启动失败", "error": error}, 500
    return {"ok": True, "id": info["id"], "request_id": request_id,
            "retry_of": original_request_id, "status": "运行中"}, 202


def _confirm_online_refresh_result(request_id):
    try:
        cache_fingerprint = online_script_cache_fingerprint()
        source_fingerprint = online_script_source_fingerprint()
        info = confirm_online_refresh_result(request_id, cache_fingerprint, source_fingerprint)
    except ValueError as exc:
        return {"ok": False, "status": "参数错误", "error": str(exc), "request_id": request_id}, 400
    except Exception as exc:
        return {"ok": False, "status": "确认失败", "error": f"核对刷新结果失败：{exc}", "request_id": request_id}, 500
    if info.get("_missing"):
        return {"ok": False, "status": "记录不存在", "error": "刷新记录不存在或已清理", "request_id": request_id}, 404
    if info.get("_conflict"):
        return {"ok": False, "status": "结果仍未知", "error": "当前缓存或源版本与刷新回执不一致，不能确认发布结果", "request_id": request_id}, 409
    return {"ok": True, "request_id": request_id, "status": info.get("status"),
            "confirmation": info.get("confirmation"), "cache_published": info.get("cache_published"),
            "replayed": bool(info.get("_replayed"))}, 200


def _recover_online_refresh(request_id):
    try:
        info = recover_online_refresh_receipt(request_id)
    except ValueError as exc:
        return {"ok": False, "request_id": request_id, "error": str(exc)}, 400
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "error": f"修复刷新回执失败：{exc}。请查询原请求后重试。"}, 500
    if info is None:
        return {"ok": False, "request_id": request_id, "error": "刷新请求不存在或已清理"}, 404
    return {"ok": True, **info}, 200


@bp.route("/api/online-scripts/refresh-recover/<request_id>", methods=["POST"])
def api_online_refresh_recover(request_id):
    payload, status_code = _recover_online_refresh(request_id)
    return jsonify(payload), status_code


@bp.route("/online-scripts/refresh-recover/<request_id>", methods=["POST"])
def online_refresh_recover(request_id):
    payload, status_code = _recover_online_refresh(request_id)
    actions = '<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>'
    if status_code == 500 and re.fullmatch(r"[a-f0-9]{32}", request_id):
        actions += (
            f'<form class="inline-form" method="post" action="/online-scripts/refresh-recover/{h(request_id)}">'
            '<button class="btn btn-primary" type="submit">修复刷新回执</button></form>'
        )
    return layout(
        "刷新回执恢复", "online_scripts",
        page_header_card(
            "刷新回执已修复" if payload.get("ok") else "刷新回执修复未完成",
            help_html=(
                (h(payload.get("error")) + "<br>" if payload.get("error") else "")
                + f'原请求状态：{h(payload.get("status") or "-")}；仅修复回执，不重新抓取或改动缓存。'
            ),
            actions_html=actions,
        ),
    ), status_code


@bp.route("/online-scripts/refresh", methods=["POST"])
def online_scripts_refresh():
    proxy_id = request.form.get("proxy_id", "").strip()
    request_id = request.form.get("request_id", "").strip()
    payload, status_code = _submit_online_refresh(proxy_id, request_id)
    if payload.get("replayed"):
        return layout(
            "在线脚本源原同步结果", "online_scripts",
            page_header_card(
                "已查询原同步请求",
                help_html=f'状态：{h(payload.get("status") or "-")}<br>{h(payload.get("error") or payload.get("message") or "")}',
                actions_html=(
                    f'<a class="btn btn-primary" href="{h(payload["log_url"])}">查看原刷新日志</a>'
                    '<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>'
                ),
            ),
        ), status_code
    if not payload["ok"]:
        retry_form = ""
        if status_code != 409:
            retry_form = f'''
<form class="inline-form" method="post" action="/online-scripts/refresh">
    <input type="hidden" name="request_id" value="{h(payload.get("request_id", ""))}">
    <select name="proxy_id" aria-label="刷新脚本源使用的代理">{proxy_select_options(proxy_id)}</select>
    <button class="btn btn-primary" type="submit">重新同步在线脚本源</button>
</form>'''
        body = page_header_card(
            "在线脚本源同步未启动",
            help_html=h(payload["error"]),
            actions_html=retry_form + '<a class="btn btn-gray" href="/online-scripts#online-status">查看同步状态</a>',
        )
        return layout("在线脚本源同步", "online_scripts", body), status_code

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
            msg="已提交在线脚本源同步，正在处理中",
        )
    )


@bp.route("/api/online-scripts/refresh-status")
def api_online_refresh_status():
    try:
        return jsonify(get_online_refresh_state())
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取在线脚本源同步状态失败：{e}"}), 500


@bp.route("/api/online-scripts/refresh-history")
def api_online_refresh_history():
    try:
        limit = int(request.args.get("limit", "50"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="limit 必须为整数", items=[]), 400
    try:
        items = get_online_refresh_history(limit, request_id=request.args.get("request_id", "").strip())
    except ValueError as e:
        return jsonify(ok=False, error=str(e), items=[]), 400
    except LookupError as e:
        return jsonify(ok=False, error=str(e), items=[]), 404
    except Exception as e:
        return jsonify(ok=False, error=f"读取在线脚本源刷新历史失败：{e}", items=[]), 500
    return jsonify(ok=True, items=items), 200


@bp.route("/api/online-scripts/refresh-retry/<request_id>", methods=["POST"])
def api_online_refresh_retry(request_id):
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    payload, status_code = _submit_online_refresh_retry(
        request_id,
        str(data.get("proxy_id", "") or "").strip(),
        data.get("retry_request_id") or data.get("request_id", ""),
    )
    return jsonify(payload), status_code


@bp.route("/online-scripts/refresh-retry/<request_id>", methods=["POST"])
def online_refresh_retry(request_id):
    proxy_id = request.form.get("proxy_id", "").strip()
    payload, status_code = _submit_online_refresh_retry(
        request_id, proxy_id, request.form.get("retry_request_id") or request.form.get("request_id", ""),
    )
    if not payload.get("ok"):
        return layout(
            "在线脚本源重试",
            "online_scripts",
            page_header_card(
                "在线脚本源重试未启动",
                help_html=h(payload.get("error") or "重试失败"),
                actions_html=(
                    f'<a class="btn btn-gray" href="/online-scripts?refresh_request_id={h(request_id)}#onlineRefreshHistory">返回原刷新记录</a>'
                    if re.fullmatch(r"[a-f0-9]{32}", request_id) else
                    '<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>'
                ),
            ),
        ), status_code
    if payload.get("replayed"):
        return layout(
            "在线脚本源重试结果", "online_scripts",
            page_header_card(
                "已查询原同步重试",
                help_html=f'状态：{h(payload.get("status") or "-")}<br>{h(payload.get("error") or "")}',
                actions_html=(
                    f'<a class="btn btn-primary" href="{h(payload["log_url"])}">查看原重试日志</a>'
                    '<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>'
                ),
            ),
        ), 200
    return redirect(url_for("online_scripts.online_scripts_page", msg="已提交在线脚本源明确重试，正在处理中"))


@bp.route("/api/online-scripts/refresh-confirm/<request_id>", methods=["POST"])
def api_online_refresh_confirm(request_id):
    payload, status_code = _confirm_online_refresh_result(request_id)
    return jsonify(payload), status_code


@bp.route("/online-scripts/refresh-confirm/<request_id>", methods=["POST"])
def online_refresh_confirm(request_id):
    payload, status_code = _confirm_online_refresh_result(request_id)
    if not payload.get("ok"):
        return layout(
            "在线脚本源结果确认",
            "online_scripts",
            page_header_card(
                "刷新结果仍需核对",
                help_html=h(payload.get("error") or "刷新结果无法确认"),
                actions_html=(
                    f'<a class="btn btn-gray" href="/online-scripts?refresh_request_id={h(request_id)}#onlineRefreshHistory">返回原刷新记录</a>'
                    if re.fullmatch(r"[a-f0-9]{32}", request_id) else
                    '<a class="btn btn-gray" href="/online-scripts#online-status">返回刷新状态</a>'
                ),
            ),
        ), status_code
    return redirect(url_for("online_scripts.online_scripts_page", msg=f"在线脚本源刷新结果已确认：{payload.get('status')}"))


@bp.route("/api/online-scripts/refresh-status/<request_id>")
def api_online_refresh_status_for_request(request_id):
    try:
        expected = _refresh_request_id(request_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    try:
        state = get_online_refresh_state()
    except Exception as e:
        return jsonify({"ok": False, "error": f"读取在线脚本源同步状态失败：{e}"}), 500
    if state.get("request_id") != expected:
        try:
            state = get_online_refresh_request(expected)
        except Exception as e:
            return jsonify({
                "ok": False,
                "error": f"读取在线脚本源刷新历史失败：{e}",
                "request_id": expected,
            }), 500
        if not state:
            return jsonify({"ok": False, "error": "刷新请求不存在或已被明确的新请求替换", "request_id": expected}), 404
    return jsonify(state)


@bp.route("/api/online-scripts/refresh", methods=["POST"])
def api_online_scripts_refresh():
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    payload, status_code = _submit_online_refresh(
        str(data.get("proxy_id", "") or "").strip(),
        data.get("request_id", ""),
    )
    return jsonify(payload), status_code
