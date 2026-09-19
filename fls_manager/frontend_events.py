import threading

from flask import request

from .notify import send_all_enabled
from .utils import now_str


def _client_ip():
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def _frontend_open_worker(ip, user_agent):
    try:
        send_all_enabled(
            "FLS 面板登录通知",
            f"时间：{now_str()}\nIP：{ip}\nUser-Agent：{user_agent}",
        )
    except Exception as exc:
        print(f"[Frontend] 登录通知发送失败: {exc}")


def _start_update_log_refresh():
    try:
        # Import lazily so normal authentication startup has no route dependency.
        from .routes.about.helpers import start_refresh_log_job

        start_refresh_log_job(title="首次访问自动刷新更新日志")
    except Exception as exc:
        print(f"[Frontend] 更新日志刷新启动失败: {exc}")


def dispatch_frontend_open_event():
    """Notify and refresh remote version metadata without delaying the first page."""
    ip = _client_ip()
    user_agent = request.headers.get("User-Agent", "")

    # Set the shared status to checking before HTML reaches the browser. This
    # prevents the first status request from caching the initial empty state.
    _start_update_log_refresh()

    worker = threading.Thread(
        target=_frontend_open_worker,
        args=(ip, user_agent),
        daemon=True,
        name="fls-frontend-open",
    )
    worker.start()
