from flask import Blueprint, request, redirect, url_for, current_app
from ..ui.layout import layout
from ..utils import h, now_str
from ..config import fls_get_admin_token, save_config
from ..auth import auth_set_session, auth_clear_session, auth_session_valid
from ..notify import send_all_enabled

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    token = fls_get_admin_token()

    if not token:
        return redirect(url_for("auth.setup"))

    if auth_session_valid(token):
        return redirect(url_for("dashboard.dashboard"))

    msg = ""

    if request.method == "POST":
        input_token = request.form.get("token", "").strip()
        remember = request.form.get("remember") == "1"

        if input_token == token:
            auth_set_session(current_app, token, remember=remember)

            try:
                ip = request.headers.get("X-Forwarded-For", request.remote_addr or "")
                ua = request.headers.get("User-Agent", "")
                send_all_enabled(
                    "FLS 面板登录通知",
                    f"时间：{now_str()}\nIP：{ip}\nUser-Agent：{ua}"
                )
            except Exception as e:
                print(f"[Auth] 登录通知发送失败: {e}")

            return redirect(request.args.get("next") or url_for("dashboard.dashboard"))

        msg = "Token 错误"

    body = f"""
<div class="card" style="max-width:520px;margin:8vh auto;">
    <div class="card-title">登录 FLS 面板</div>
    <form method="post">
        <div class="form-item">
            <label>Token</label>
            <input name="token" type="password" placeholder="请输入登录 Token" autofocus>
        </div>
        <br>
        <label>
            <input type="checkbox" name="remember" value="1" style="width:auto;">
            保持登录 7 天
        </label>
        <div class="help">不勾选则登录有效期为 1 小时。</div>
        <br>
        <button class="btn btn-primary" type="submit">登录</button>
    </form>
    <br>
    <div class="help" style="color:#dc2626;">{h(msg)}</div>
</div>
"""
    return layout("登录", "login", body)


@bp.route("/logout")
def logout():
    auth_clear_session()
    return redirect(url_for("auth.login"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if fls_get_admin_token():
        return redirect(url_for("auth.login"))

    msg = ""

    if request.method == "POST":
        token = request.form.get("token", "").strip()
        confirm = request.form.get("confirm_token", "").strip()

        if not token:
            msg = "Token 不能为空"
        elif len(token) < 6:
            msg = "Token 建议至少 6 位"
        elif token != confirm:
            msg = "两次输入不一致"
        else:
            save_config({"admin_token": token})
            return redirect(url_for("auth.login"))

    body = f"""
<div class="card" style="max-width:620px;margin:8vh auto;">
    <div class="card-title">首次设置登录 Token</div>
    <div class="help">当前面板尚未设置登录 Token，请先设置。</div>
    <br>
    <form method="post">
        <div class="form-item">
            <label>登录 Token</label>
            <input name="token" type="password" autofocus>
        </div>
        <br>
        <div class="form-item">
            <label>确认 Token</label>
            <input name="confirm_token" type="password">
        </div>
        <br>
        <button class="btn btn-primary" type="submit">保存 Token</button>
    </form>
    <br>
    <div class="help" style="color:#dc2626;">{h(msg)}</div>
</div>
"""
    return layout("首次设置 Token", "config", body)
