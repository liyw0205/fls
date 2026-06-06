from ._common import *


@bp.route("/online-scripts/refresh", methods=["POST"])
def online_scripts_refresh():
    if ONLINE_REFRESH_STATE.get("running"):
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg="脚本源正在后台拉取中，请稍候",
            )
        )

    proxy_id = request.form.get("proxy_id", "").strip()
    start_refresh_thread(proxy_id)

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
            msg="已提交后台刷新，正在拉取中",
        )
    )


@bp.route("/api/online-scripts/refresh-status")
def api_online_refresh_status():
    return jsonify({
        "running": bool(ONLINE_REFRESH_STATE.get("running")),
        "message": ONLINE_REFRESH_STATE.get("message", ""),
        "error": ONLINE_REFRESH_STATE.get("error", ""),
        "updated_at": ONLINE_REFRESH_STATE.get("updated_at", ""),
        "log_file": ONLINE_REFRESH_STATE.get("log_file", ""),
    })
