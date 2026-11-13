from ._common import *
from ...online_scripts.constants import (
    reserve_online_refresh,
    update_online_refresh_state,
)
from ...online_scripts.logs import online_refresh_log_file
from ...utils import now_str


@bp.route("/online-scripts/refresh", methods=["POST"])
def online_scripts_refresh():
    try:
        info = reserve_online_refresh(request.form.get("proxy_id", "").strip())
    except Exception as e:
        return redirect(url_for("online_scripts.online_scripts_page", err=f"提交同步失败：{e}"))
    if not info:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg="在线脚本源正在后台同步中，请稍候",
            )
        )

    refresh_id = info["id"]
    log_file = online_refresh_log_file(refresh_id)
    try:
        start_refresh_thread(refresh_id, info.get("proxy_id", ""), str(log_file))
    except Exception as e:
        update_online_refresh_state(
            running=False,
            status="启动失败",
            message="",
            error=f"启动在线脚本源同步线程失败：{e}",
            updated_at=now_str(),
            log_file=str(log_file),
        )
        return redirect(url_for("online_scripts.online_scripts_page", err=f"启动同步失败：{e}"))

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
                msg="已提交在线脚本源同步，正在处理中",
        )
    )


@bp.route("/api/online-scripts/refresh-status")
def api_online_refresh_status():
    return jsonify(get_online_refresh_state())


@bp.route("/api/online-scripts/refresh", methods=["POST"])
def api_online_scripts_refresh():
    try:
        info = reserve_online_refresh(request.form.get("proxy_id", "").strip())
    except Exception as e:
        return jsonify({"ok": False, "status": "启动失败", "error": str(e)}), 500
    if not info:
        return jsonify({"ok": False, "status": "冲突", "error": "在线脚本源正在同步中，请等待当前任务结束"}), 409
    log_file = online_refresh_log_file(info["id"])
    try:
        start_refresh_thread(info["id"], info.get("proxy_id", ""), str(log_file))
    except Exception as e:
        update_online_refresh_state(
            running=False, status="启动失败", message="",
            error=f"启动在线脚本源同步线程失败：{e}", updated_at=now_str(), log_file=str(log_file),
        )
        return jsonify({"ok": False, "status": "启动失败", "error": str(e)}), 500
    return jsonify({"ok": True, "id": info["id"], "status": "运行中"}), 202
