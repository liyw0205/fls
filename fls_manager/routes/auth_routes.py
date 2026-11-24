import ipaddress
import os
import secrets
import time

from flask import Blueprint, request, redirect, url_for, current_app, jsonify, session

from ..ui.layout import layout
from ..utils import h, now_str
from ..config import fls_get_admin_token, save_config
from ..auth import (
    auth_set_session,
    auth_clear_session,
    auth_session_valid,
    safe_local_next_url,
)
from ..native_sessions import (
    clear_native_session_cookie,
    create_native_session,
    delete_native_session,
    mark_native_session_verified,
    native_session_from_request,
    set_native_session_cookie,
)
from ..notify import send_all_enabled
from ..security import (
    security_enabled,
    security_type,
    security_config,
    generate_random_code,
    save_random_code,
    random_code_valid,
    clear_random_code,
    random_code_file,
    verify_totp,
)

bp = Blueprint("auth", __name__)

LOGIN_FAIL_STATE = {}
LOGIN_LOCK_SECONDS = 300
LOGIN_FAIL_LIMIT = 3
LOGIN_FAIL_MAX_RECORDS = 256


def client_ip():
    remote_addr = request.remote_addr or "unknown"
    configured = os.environ.get("FLS_TRUSTED_PROXIES", "")
    configured = configured.replace(",", " ").split()

    try:
        remote_ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return remote_addr

    trusted_networks = []
    for value in configured:
        try:
            trusted_networks.append(ipaddress.ip_network(str(value), strict=False))
        except ValueError:
            continue

    if not any(remote_ip in network for network in trusted_networks):
        return remote_addr

    forwarded = [
        item.strip()
        for item in request.headers.get("X-Forwarded-For", "").split(",")
        if item.strip()
    ]

    try:
        forwarded_ips = [ipaddress.ip_address(value) for value in forwarded]
    except ValueError:
        return remote_addr

    for candidate in reversed(forwarded_ips):
        if not any(candidate in network for network in trusted_networks):
            return str(candidate)

    return remote_addr


def login_fail_info(ip):
    now = int(time.time())

    for old_ip, old_info in list(LOGIN_FAIL_STATE.items()):
        lock_until = int(old_info.get("lock_until", 0) or 0)
        if lock_until and lock_until <= now:
            LOGIN_FAIL_STATE.pop(old_ip, None)

    if ip not in LOGIN_FAIL_STATE and len(LOGIN_FAIL_STATE) >= LOGIN_FAIL_MAX_RECORDS:
        entries = list(LOGIN_FAIL_STATE.items())
        entries.sort(key=lambda item: int(item[1].get("lock_until", 0) or 0))
        if entries:
            LOGIN_FAIL_STATE.pop(entries[0][0], None)

    info = LOGIN_FAIL_STATE.get(ip) or {
        "count": 0,
        "lock_until": 0,
        "notified": False,
    }

    if int(info.get("lock_until", 0) or 0) <= now and info.get("lock_until"):
        info = {
            "count": 0,
            "lock_until": 0,
            "notified": False,
        }

    LOGIN_FAIL_STATE[ip] = info
    return info


def record_login_fail(ip, ua=""):
    now = int(time.time())
    info = login_fail_info(ip)

    info["count"] = int(info.get("count", 0) or 0) + 1

    if info["count"] >= LOGIN_FAIL_LIMIT:
        info["lock_until"] = now + LOGIN_LOCK_SECONDS

        if not info.get("notified"):
            info["notified"] = True

            try:
                send_all_enabled(
                    "FLS 面板登录失败告警",
                    (
                        f"时间：{now_str()}\n"
                        f"IP：{ip}\n"
                        f"User-Agent：{ua}\n"
                        f"失败次数：{info['count']}\n"
                        f"已禁止登录：{LOGIN_LOCK_SECONDS} 秒"
                    ),
                )
            except Exception as e:
                print(f"[Auth] 登录失败通知发送失败: {e}")

    LOGIN_FAIL_STATE[ip] = info
    return info


def clear_login_fail(ip):
    LOGIN_FAIL_STATE.pop(ip, None)


def create_and_send_security_code():
    ip = client_ip()
    ua = request.headers.get("User-Agent", "")

    code = generate_random_code()
    data = save_random_code(code, ip=ip, ua=ua)

    try:
        send_all_enabled(
            "FLS 面板安全验证码",
            (
                f"验证码：{code}\n"
                f"有效期：300 秒\n"
                f"时间：{now_str()}\n"
                f"IP：{ip}\n"
                f"User-Agent：{ua}\n"
                f"\n如果没有收到通知，可在终端查看：\n"
                f"cat {random_code_file()}"
            ),
        )
    except Exception as e:
        print(f"[Auth] 安全验证码通知发送失败: {e}")

    return data


@bp.route("/login", methods=["GET", "POST"])
def login():
    token = fls_get_admin_token()

    if not token:
        return redirect(url_for("auth.setup"))

    if auth_session_valid(token) and not security_enabled():
        return redirect(url_for("dashboard.dashboard"))

    ip = client_ip()
    ua = request.headers.get("User-Agent", "")
    fail_info = login_fail_info(ip)

    now = int(time.time())
    lock_until = int(fail_info.get("lock_until", 0) or 0)

    msg = ""

    if lock_until > now:
        remain = lock_until - now
        msg = f"登录失败次数过多，请 {remain} 秒后再试"

    elif request.method == "POST":
        input_token = request.form.get("token", "").strip()
        remember = request.form.get("remember") == "1"

        if input_token == token:
            clear_login_fail(ip)

            if security_enabled():
                auth_set_session(
                    current_app,
                    token,
                    remember=remember,
                    security_verified=False,
                )

                if security_type() == "code":
                    create_and_send_security_code()

                return redirect(url_for("auth.verify"))

            auth_set_session(
                current_app,
                token,
                remember=remember,
                security_verified=True,
            )

            return redirect(
                safe_local_next_url(
                    request.args.get("next"),
                    default=url_for("dashboard.dashboard"),
                )
            )

        info = record_login_fail(ip, ua=ua)

        if int(info.get("lock_until", 0) or 0) > int(time.time()):
            msg = f"Token 错误，失败 {info.get('count')} 次，已禁止登录 {LOGIN_LOCK_SECONDS} 秒"
        else:
            msg = f"Token 错误，失败 {info.get('count')} 次"

    body = f"""
<div class="card" style="max-width:520px;margin:8vh auto;">
    <div class="card-title">登录 FLS 面板</div>
    <form method="post">
        <div class="form-item">
            <label>Token</label>
            <input name="token" type="password" placeholder="请输入登录 Token" autofocus aria-label="登录 Token">
        </div>
        <br>
        <label>
            <input type="checkbox" name="remember" value="1" style="width:auto;" aria-label="保持登录 14 天">
            保持登录 14 天
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


@bp.route("/api/auth/session", methods=["POST"])
def api_native_session():
    if not request.is_json:
        return jsonify({"ok": False, "msg": "请求必须使用 JSON"}), 415

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "msg": "请求必须是对象"}), 400

    input_token = data.get("token")
    if not isinstance(input_token, str) or not input_token.strip() or len(input_token) > 4096:
        return jsonify({"ok": False, "msg": "Token 无效"}), 400

    remember = data.get("remember", False)
    if not isinstance(remember, bool):
        return jsonify({"ok": False, "msg": "保持登录选项无效"}), 400

    token = fls_get_admin_token()
    if not token:
        return jsonify({"ok": False, "msg": "面板尚未设置 Token，请访问 /setup"}), 403

    ip = client_ip()
    fail_info = login_fail_info(ip)
    lock_until = int(fail_info.get("lock_until", 0) or 0)
    if lock_until > int(time.time()):
        return jsonify({"ok": False, "msg": "登录失败次数过多，请稍后再试"}), 429

    ua = request.headers.get("User-Agent", "")
    if not secrets.compare_digest(
        input_token.strip().encode("utf-8"),
        token.encode("utf-8"),
    ):
        info = record_login_fail(ip, ua=ua)
        status = 429 if int(info.get("lock_until", 0) or 0) > int(time.time()) else 403
        return jsonify({"ok": False, "msg": "Token 错误，请检查后重试"}), status

    clear_login_fail(ip)
    verified = not security_enabled()
    session_id, csrf, max_age = create_native_session(
        token,
        remember=remember,
        verified=verified,
    )
    requires_verification = not verified
    if requires_verification and security_type() == "code":
        create_and_send_security_code()

    response = jsonify({
        "ok": True,
        "authenticated": verified,
        "requires_verification": requires_verification,
        "verification_type": security_type() if requires_verification else None,
        "csrf_token": csrf,
        "remember": remember,
    })
    set_native_session_cookie(
        response,
        session_id,
        max_age=max_age if remember else None,
    )
    return response, 202 if requires_verification else 200


@bp.route("/api/auth/verify", methods=["POST"])
def api_native_verify():
    token = fls_get_admin_token()
    native = native_session_from_request(token)
    if not token or not native:
        return jsonify({"ok": False, "msg": "登录已过期，请重新登录"}), 401
    if native["verified"]:
        return jsonify({"ok": False, "msg": "登录已完成安全验证"}), 400

    if not security_enabled():
        if not mark_native_session_verified(native, token):
            return jsonify({"ok": False, "msg": "登录已过期，请重新登录"}), 401
        return jsonify({
            "ok": True,
            "authenticated": True,
            "csrf_token": native["csrf_token"],
        })

    ip = client_ip()
    fail_info = login_fail_info(ip)
    lock_until = int(fail_info.get("lock_until", 0) or 0)
    if lock_until > int(time.time()):
        return jsonify({"ok": False, "msg": "验证失败次数过多，请稍后再试"}), 429

    if not request.is_json:
        return jsonify({"ok": False, "msg": "请求必须使用 JSON"}), 415
    data = request.get_json(silent=True)
    raw_code = data.get("code") if isinstance(data, dict) else None
    code = raw_code.strip() if isinstance(raw_code, str) else ""
    if not code or len(code) > 64:
        return jsonify({"ok": False, "msg": "验证码无效"}), 400

    ua = request.headers.get("User-Agent", "")
    if security_type() == "code":
        valid, message = random_code_valid(code)
    elif security_type() == "totp":
        valid = verify_totp(security_config().get("totp_secret", ""), code)
        message = "验证成功" if valid else "2FA 验证码错误"
    else:
        valid, message = False, "未知安全验证方式"

    if not valid:
        info = record_login_fail(ip, ua=ua)
        status = 429 if int(info.get("lock_until", 0) or 0) > int(time.time()) else 403
        return jsonify({"ok": False, "msg": message}), status

    if not mark_native_session_verified(native, token):
        return jsonify({"ok": False, "msg": "登录已过期，请重新登录"}), 401
    if security_type() == "code":
        clear_random_code()
    clear_login_fail(ip)
    return jsonify({
        "ok": True,
        "authenticated": True,
        "csrf_token": native["csrf_token"],
    })


@bp.route("/api/auth/resend-code", methods=["POST"])
def api_native_resend_code():
    token = fls_get_admin_token()
    native = native_session_from_request(token)
    if not token or not native:
        return jsonify({"ok": False, "msg": "登录已过期，请重新登录"}), 401
    if native["verified"]:
        return jsonify({"ok": False, "msg": "登录已完成安全验证"}), 400
    if not security_enabled() or security_type() != "code":
        return jsonify({"ok": False, "msg": "当前没有可重新发送的验证码"}), 400

    fail_info = login_fail_info(client_ip())
    if int(fail_info.get("lock_until", 0) or 0) > int(time.time()):
        return jsonify({"ok": False, "msg": "验证失败次数过多，请稍后再试"}), 429

    create_and_send_security_code()
    return jsonify({"ok": True, "msg": "验证码已重新发送"})


@bp.route("/api/auth/logout", methods=["POST"])
def api_native_logout():
    delete_native_session(request.cookies.get("fls_native_session", ""))
    response = jsonify({"ok": True})
    clear_native_session_cookie(response)
    return response


@bp.route("/verify", methods=["GET", "POST"])
def verify():
    token = fls_get_admin_token()

    if not token:
        return redirect(url_for("auth.setup"))

    if not auth_session_valid(token):
        return redirect(url_for("auth.login"))

    ip = client_ip()
    ua = request.headers.get("User-Agent", "")

    fail_info = login_fail_info(ip)
    now = int(time.time())
    lock_until = int(fail_info.get("lock_until", 0) or 0)

    if lock_until > now:
        remain = lock_until - now

        body = f"""
<div class="card" style="max-width:560px;margin:8vh auto;">
    <div class="card-title">登录已被临时禁止</div>
    <div class="help" style="color:#dc2626;font-weight:800;">
        验证失败次数过多，请 {h(remain)} 秒后再试。
    </div>
    <br>
    <a class="btn btn-gray" href="/logout">退出登录</a>
</div>
"""
        return layout("安全验证", "login", body), 403

    if not security_enabled():
        session["security_verified"] = True
        return redirect(url_for("dashboard.dashboard"))

    stype = security_type()
    msg = ""

    if request.method == "POST":
        code = request.form.get("code", "").strip()

        # ============================================================
        # 验证码为空时不允许验证
        # 不计入失败次数，避免误触。
        # ============================================================
        if not code:
            msg = "验证码不能为空"

        elif stype == "code":
            ok, message = random_code_valid(code)

            if ok:
                session["security_verified"] = True
                clear_random_code()
                clear_login_fail(ip)
                return redirect(url_for("dashboard.dashboard"))

            # 随机验证码错误，计入失败次数
            info = record_login_fail(ip, ua=ua)

            if int(info.get("lock_until", 0) or 0) > int(time.time()):
                msg = (
                    f"{message}。失败 {info.get('count')} 次，"
                    f"已禁止登录 {LOGIN_LOCK_SECONDS} 秒"
                )
            else:
                msg = f"{message}。失败 {info.get('count')} 次"

        elif stype == "totp":
            secret = security_config().get("totp_secret", "")

            if verify_totp(secret, code):
                session["security_verified"] = True
                clear_login_fail(ip)
                return redirect(url_for("dashboard.dashboard"))

            # 2FA 错误，计入失败次数
            info = record_login_fail(ip, ua=ua)

            if int(info.get("lock_until", 0) or 0) > int(time.time()):
                msg = (
                    f"2FA 验证码错误。失败 {info.get('count')} 次，"
                    f"已禁止登录 {LOGIN_LOCK_SECONDS} 秒"
                )
            else:
                msg = f"2FA 验证码错误。失败 {info.get('count')} 次"

        else:
            msg = "未知安全验证方式"

    help_text = ""

    if stype == "code":
        help_text = (
            f"验证码已通过通知渠道发送，有效期 300 秒。<br>"
            f"如果没有设置通知渠道，请在终端查看：<br>"
            f"<code>cat {h(random_code_file())}</code>"
        )
    else:
        help_text = "请输入认证器 App 中显示的 6 位验证码。"

    body = f"""
<div class="card" style="max-width:560px;margin:8vh auto;">
    <div class="card-title">安全验证</div>
    <div class="help">{help_text}</div>
    <br>

    <form method="post">
        <div class="form-item">
            <label>{"随机验证码" if stype == "code" else "2FA 验证码"}</label>
            <input name="code" placeholder="请输入验证码" autofocus aria-label="安全验证码">
        </div>
        <br>
        <button class="btn btn-primary" type="submit">验证</button>
        {"<a class='btn btn-orange' href='/verify/resend'>重新发送验证码</a>" if stype == "code" else ""}
        <a class="btn btn-gray" href="/logout">退出登录</a>
    </form>

    <br>
    <div class="help" style="color:#dc2626;">{h(msg)}</div>
</div>
"""
    return layout("安全验证", "login", body)


@bp.route("/verify/resend")
def resend_code():
    token = fls_get_admin_token()

    if not token or not auth_session_valid(token):
        return redirect(url_for("auth.login"))

    ip = client_ip()
    fail_info = login_fail_info(ip)
    now = int(time.time())
    lock_until = int(fail_info.get("lock_until", 0) or 0)

    if lock_until > now:
        remain = lock_until - now

        body = f"""
<div class="card" style="max-width:560px;margin:8vh auto;">
    <div class="card-title">登录已被临时禁止</div>
    <div class="help" style="color:#dc2626;font-weight:800;">
        验证失败次数过多，请 {h(remain)} 秒后再试。<br>
        禁止期间不能重新发送验证码。
    </div>
    <br>
    <a class="btn btn-gray" href="/logout">退出登录</a>
</div>
"""
        return layout("安全验证", "login", body), 403

    if not security_enabled() or security_type() != "code":
        return redirect(url_for("auth.verify"))

    create_and_send_security_code()
    return redirect(url_for("auth.verify"))

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
            <input name="token" type="password" autofocus aria-label="登录 Token">
        </div>
        <br>
        <div class="form-item">
            <label>确认 Token</label>
            <input name="confirm_token" type="password" aria-label="确认登录 Token">
        </div>
        <br>
        <button class="btn btn-primary" type="submit">保存 Token</button>
    </form>
    <br>
    <div class="help" style="color:#dc2626;">{h(msg)}</div>
</div>
"""
    return layout("首次设置 Token", "config", body)
