import os
import uuid
from datetime import timedelta

from flask import Flask, request

from .paths import BASE_DIR, DATA_DIR
from . import backup_commit
from .data_access import directory_access, PanelStorageBusy

from .csrf import csrf_before_request
from .auth import (
    auth_before_request,
    auth_security_verified,
    auth_session_valid,
    FLS_AUTH_REMEMBER_SECONDS,
)
from .config import fls_get_admin_token
from .frontend_events import dispatch_frontend_open_event
from .models import reconcile_task_history_after_restart
from .task_runner import running_history_ids
from .routes.auth_routes import bp as auth_bp
from .routes.dashboard import bp as dashboard_bp
from .routes.tasks import bp as tasks_bp
from .routes.env import bp as env_bp
from .routes.scripts import bp as scripts_bp
from .routes.logs import bp as logs_bp
from .routes.config import bp as config_bp
from .routes.proxy import bp as proxy_bp
from .routes.status import bp as status_bp
from .routes.about import bp as about_bp
from .routes.deps import bp as deps_bp
from .routes.backup import bp as backup_bp
from .routes.notify import bp as notify_bp
from .routes.api import bp as api_bp
from .routes.runtime import bp as runtime_bp
from .routes.online_scripts import bp as online_scripts_bp


def get_persistent_secret_key():
    """
    获取稳定的 Flask secret_key。

    优先级：
    1. 环境变量 FLS_SECRET_KEY；
    2. data/secret_key.txt；
    3. 如果文件不存在，则生成并保存。

    注意：
    Flask session 依赖 secret_key 签名。
    如果每次重启都生成随机 secret_key，浏览器里的登录态会全部失效。
    """
    env_key = os.environ.get("FLS_SECRET_KEY", "").strip()
    if env_key:
        return env_key

    with directory_access():
        return _secret_key_from_data()


def _secret_key_from_data():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    key_file = DATA_DIR / "secret_key.txt"

    try:
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
            if key:
                return key
    except Exception:
        pass

    key = uuid.uuid4().hex + uuid.uuid4().hex

    try:
        with directory_access(write=True):
            key_file.write_text(key, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except Exception:
            pass
    except PanelStorageBusy:
        raise
    except Exception:
        # 极端情况下文件无法写入，至少保证程序能启动。
        return key

    return key


def reconcile_startup_state(app):
    with directory_access(write=True):
        # Runtime handles are in memory; reconcile durable snapshots before use.
        reconcile_task_history_after_restart(running_history_ids())
        from .routes.scripts.debug import load_script_debug_runs
        load_script_debug_runs()
        if app.config.get("RESTORE_STARTUP_RUNTIME_PENDING"):
            initialize_startup_runtime(app)
        app.config["RESTORE_STARTUP_RECONCILIATION_PENDING"] = False


def initialize_startup_runtime(app):
    from .config import save_config, load_config
    from .scheduler import reload_scheduler
    from .logs import cleanup_logs

    with directory_access(write=True):
        try:
            save_config(load_config())
        except PanelStorageBusy:
            raise
        except Exception as exc:
            print(f"[Config] 初始化配置失败: {exc}")
        reload_scheduler()
        cleanup_logs()
        app.config["RESTORE_STARTUP_RUNTIME_PENDING"] = False


def create_app():
    app = Flask(__name__)

    app.secret_key = get_persistent_secret_key()
    app.config["RESTORE_STARTUP_REQUEST_IDS"] = frozenset(backup_commit.pending(BASE_DIR))
    app.config["RESTORE_STARTUP_RECONCILIATION_PENDING"] = bool(app.config["RESTORE_STARTUP_REQUEST_IDS"])
    if not app.config["RESTORE_STARTUP_RECONCILIATION_PENDING"]:
        reconcile_startup_state(app)

    @app.errorhandler(PanelStorageBusy)
    def panel_storage_busy(_error):
        from flask import jsonify
        response = jsonify(ok=False, code="panel_storage_busy", msg=str(_error))
        response.status_code = 503
        response.headers["Retry-After"] = "1"
        response.headers["Cache-Control"] = "no-store"
        return response

    # Cookie 基础配置。
    # remember 登录时，auth.py 会设置 session.permanent；
    # 这里固定长期 Cookie 有效期，避免不同登录请求互相改写全局有效期。
    app.config.update(
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=FLS_AUTH_REMEMBER_SECONDS),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    app.before_request(csrf_before_request)
    app.before_request(auth_before_request)

    @app.after_request
    def frontend_open_after_request(response):
        """Run frontend-open side effects for every actual rendered page."""
        requested_with = (request.headers.get("X-Requested-With") or "").lower()

        if (
            request.method != "GET"
            or response.status_code < 200
            or response.status_code >= 300
            or response.mimetype != "text/html"
            or requested_with in ("xmlhttprequest", "fls-ajax")
        ):
            return response

        token = fls_get_admin_token()
        if not token or not auth_session_valid(token) or not auth_security_verified():
            return response

        dispatch_frontend_open_event()
        return response

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(env_bp)
    app.register_blueprint(scripts_bp)
    app.register_blueprint(online_scripts_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(proxy_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(about_bp)
    app.register_blueprint(deps_bp)
    app.register_blueprint(backup_bp)
    app.register_blueprint(notify_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(runtime_bp)

    return app
