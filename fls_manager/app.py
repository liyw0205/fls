import os
import uuid

from flask import Flask

from .paths import DATA_DIR

from .auth import auth_before_request
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
        key_file.write_text(key, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except Exception:
            pass
    except Exception:
        # 极端情况下文件无法写入，至少保证程序能启动。
        return key

    return key


def create_app():
    app = Flask(__name__)

    app.secret_key = get_persistent_secret_key()

    # Cookie 基础配置。
    # remember 登录时，auth.py 会设置 session.permanent 和有效期。
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    app.before_request(auth_before_request)

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(env_bp)
    app.register_blueprint(scripts_bp)
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
