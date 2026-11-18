import re
import uuid

from ._common import *
from ...online_scripts.constants import (
    ONLINE_REFRESH_LOCK,
    ONLINE_REFRESH_STATE,
    reserve_online_refresh,
    update_online_refresh_state,
)
from ...online_scripts.logs import online_refresh_log_file
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
        validate_proxy_id(proxy_id)
    except ValueError as e:
        return {"ok": False, "status": "参数错误", "request_id": request_id, "error": str(e)}, 400
    except Exception as e:
        return {"ok": False, "status": "代理不可读", "request_id": request_id,
                "error": f"读取代理配置失败：{e}"}, 500
    try:
        info = reserve_online_refresh(proxy_id, request_id)
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
    if info.get("_replay"):
        return {"ok": True, "replayed": True, "request_id": request_id,
                "id": info.get("id", ""), "status": info.get("status", "已完成"),
                "cache_published": bool(info.get("cache_published")),
                "message": info.get("message", "在线脚本源刷新已完成，未重复抓取")}, 200
    if info.get("_unknown"):
        return {"ok": False, "request_id": request_id, "id": info.get("id", ""),
                "status": info.get("status", "结果未知"),
                "cache_published": bool(info.get("cache_published")),
                "error": info.get("error") or "刷新结果未知，请查询当前缓存和日志后再决定是否创建新的刷新请求"}, 409

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


@bp.route("/online-scripts/refresh", methods=["POST"])
def online_scripts_refresh():
    proxy_id = request.form.get("proxy_id", "").strip()
    request_id = request.form.get("request_id", "").strip()
    payload, status_code = _submit_online_refresh(proxy_id, request_id)
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
