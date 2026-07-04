from ._common import *
from ...ui.components import table_card


@bp.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "POST":
        old_cfg = load_config()

        # ============================================================
        # 脚本类型
        # ============================================================
        task_types = {}

        for k in ["py", "sh", "js", "ts", "ps1", "bat", "php", "rb", "pl", "lua", "jar"]:
            task_types[k] = request.form.get(f"type_{k}") == "1"

        # ============================================================
        # 登录 Token
        # ============================================================
        old_admin_token = str(old_cfg.get("admin_token", "") or "").strip()
        new_admin_token = request.form.get("admin_token", "").strip()

        # ============================================================
        # 安全验证配置
        # ============================================================
        old_security_enabled = bool(old_cfg.get("security_verify_enabled", False))
        old_security_type = str(old_cfg.get("security_verify_type", "code") or "code").strip()

        if old_security_type not in ("code", "totp"):
            old_security_type = "code"

        old_totp_secret = str(old_cfg.get("totp_secret", "") or "").strip()

        security_verify_enabled = request.form.get("security_verify_enabled") == "1"
        security_verify_type = request.form.get("security_verify_type", "code").strip()

        if security_verify_type not in ("code", "totp"):
            security_verify_type = "code"

        submitted_totp_secret = str(
            request.form.get("totp_secret")
            or old_totp_secret
            or ""
        ).strip()

        totp_secret = submitted_totp_secret

        if security_verify_enabled and security_verify_type == "totp":
            if not totp_secret:
                totp_secret = generate_totp_secret()

            # 只有在首次开启 2FA / 切换到 2FA / 密钥变化时，强制要求输入一次验证码。
            need_totp_confirm = (
                not old_security_enabled
                or old_security_type != "totp"
                or old_totp_secret != totp_secret
            )

            if need_totp_confirm:
                totp_code = request.form.get("totp_code", "").strip()

                if not verify_totp(totp_secret, totp_code):
                    return (
                        "2FA 验证码错误，未开启 2FA。"
                        "请用认证器扫码后输入正确验证码再保存。"
                    ), 400

        elif security_verify_enabled and security_verify_type == "code":
            # 随机验证码模式不需要 TOTP 密钥。
            totp_secret = ""

        else:
            # 关闭安全验证时清空安全验证类型和 TOTP 密钥。
            security_verify_enabled = False
            security_verify_type = "code"
            totp_secret = ""

        token_changed = new_admin_token != old_admin_token

        security_changed = (
            security_verify_enabled != old_security_enabled
            or security_verify_type != old_security_type
            or totp_secret != old_totp_secret
        )

        # ============================================================
        # 保存配置
        # ============================================================
        cfg = {
            "admin_token": new_admin_token,

            "security_verify_enabled": security_verify_enabled,
            "security_verify_type": security_verify_type,
            "totp_secret": totp_secret,

            "port": safe_int(
                request.form.get("port", "5700"),
                default=5700,
                min_value=1,
                max_value=65535,
            ),
            "online_script_source": (
                request.form.get("online_script_source", "").strip()
                or "https://raw.githubusercontent.com/liyw0205/fls-scripts/main/index.json"
            ),
            "log_cleanup_minutes": safe_int(
                request.form.get("log_cleanup_minutes", "30"),
                default=30,
                min_value=1,
                max_value=1440,
            ),
            "log_max_size_mb": safe_int(
                request.form.get("log_max_size_mb", "10"),
                default=10,
                min_value=1,
            ),
            "log_keep_per_task": safe_int(
                request.form.get("log_keep_per_task", "10"),
                default=10,
                min_value=1,
            ),
            "task_timeout_seconds": safe_int(
                request.form.get("task_timeout_seconds", "1800"),
                default=1800,
                min_value=0,
            ),
            "random_delay_seconds": safe_int(
                request.form.get("random_delay_seconds", "0"),
                default=0,
                min_value=0,
                max_value=120,
            ),
            "task_types": task_types,
        }

        save_config(cfg)
        cleanup_logs()
        reload_scheduler()

        # 修改 Token / 开启安全验证 / 关闭安全验证 / 切换安全验证方式后自动登出。
        if token_changed or security_changed:
            auth_clear_session()
            return redirect(url_for("auth.login"))

        return redirect(url_for("config.config_page"))

    cfg = load_config()
    types = cfg.get("task_types", {})

    def checked(k):
        return "checked" if types.get(k) else ""

    rows = ""

    for k, name in [
        ("py", "Python .py / .pyw"),
        ("sh", "Shell .sh / .bash"),
        ("js", "Node .js / .mjs / .cjs"),
        ("ts", "TypeScript .ts / .mts / .cts"),
        ("ps1", "PowerShell .ps1"),
        ("bat", "Windows Batch .bat / .cmd"),
        ("php", "PHP .php"),
        ("rb", "Ruby .rb"),
        ("pl", "Perl .pl / .pm"),
        ("lua", "Lua .lua"),
        ("jar", "Java Jar .jar"),
    ]:
        rows += f"""
<tr>
    <td><b>{h(name)}</b></td>
    <td><input type="checkbox" name="type_{h(k)}" value="1" {checked(k)} style="width:auto;"></td>
</tr>
"""

    # ============================================================
    # 安全验证 GET 展示数据
    # ============================================================
    security_verify_enabled = bool(cfg.get("security_verify_enabled", False))
    security_enabled_checked = "checked" if security_verify_enabled else ""

    security_type = str(cfg.get("security_verify_type", "code") or "code").strip()

    if security_type not in ("code", "totp"):
        security_type = "code"

    code_selected = "selected" if security_type == "code" else ""
    totp_selected = "selected" if security_type == "totp" else ""

    totp_secret = str(cfg.get("totp_secret") or "").strip()

    if not totp_secret:
        totp_secret = generate_totp_secret()

    totp_uri = totp_otpauth_uri(totp_secret)
    totp_qr = totp_qr_url(totp_secret)

    security_status_text = "已开启" if security_verify_enabled else "未开启"

    if security_verify_enabled:
        if security_type == "totp":
            security_status_text += "，当前方式：2FA / TOTP"
        else:
            security_status_text += "，当前方式：随机验证码"

    task_type_table = table_card(
        "task 可执行脚本类型",
        ("类型", "启用"),
        rows,
    )

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">登录配置</div>
    <div class="form-item">
        <label>登录 Token</label>
        <input name="admin_token" value="{h(cfg.get('admin_token', ''))}">
        <div class="help">
            Token 为空时，面板会进入首次设置引导 /setup。<br>
            不建议在公网环境关闭或清空 Token。<br>
            修改 Token 后会自动退出登录。
        </div>
    </div>
    <br>
    <div class="form-item">
        <label>面板端口，保存后重启生效</label>
        <input name="port" type="number" min="1" max="65535" value="{h(cfg.get('port', 5700))}">
        <div class="help">当前进程实际监听端口：{h(get_port())}</div>
    </div>
</div>

<div class="card">
    <div class="card-title">安全验证</div>

    <div class="help">
        当前状态：<b>{h(security_status_text)}</b><br>
        开启后，Token 登录成功后，还需要进行安全验证。<br>
        修改 Token、开启安全验证、关闭安全验证、切换验证方式后会自动退出登录。
    </div>

    <br>

    <label>
        <input
            type="checkbox"
            name="security_verify_enabled"
            value="1"
            {security_enabled_checked}
            style="width:auto;"
            onchange="flsToggleSecurityBox()"
        >
        登录后启用二次安全验证
    </label>

    <br><br>

    <div class="form-item">
        <label>验证方式</label>
        <select name="security_verify_type" id="securityVerifyType" onchange="flsToggleSecurityBox()">
            <option value="code" {code_selected}>随机验证码</option>
            <option value="totp" {totp_selected}>2FA / TOTP 验证</option>
        </select>
    </div>

    <div id="securityCodeBox" style="margin-top:14px;">
        <div class="card" style="box-shadow:none;border:1px solid #e5e7eb;margin:0;">
            <div class="card-title">随机验证码</div>
            <div class="help">
                Token 登录成功后，系统会生成 6 位随机验证码。<br>
                验证码保存到：<code>{h(random_code_file())}</code><br>
                有效期：<b>300 秒</b>。<br>
                系统会尝试通过通知渠道发送验证码。<br>
                如果未设置通知渠道或通知失败，可在终端手动查看：<br>
                <code>cat {h(random_code_file())}</code>
            </div>
        </div>
    </div>

    <div id="securityTotpBox" style="margin-top:14px;display:none;">
        <input type="hidden" name="totp_secret" value="{h(totp_secret)}">

        <div class="card" style="box-shadow:none;border:1px solid #e5e7eb;margin:0;">
            <div class="card-title">2FA / TOTP 验证</div>

            <div class="help">
                选择 2FA 后，请使用认证器 App 扫描二维码。<br>
                首次开启 2FA 或更换密钥时，需要输入一次正确验证码才会成功保存。<br>
                推荐 App：Google Authenticator、Microsoft Authenticator、1Password、Bitwarden 等。
            </div>

            <br>

            <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start;">
                <div>
                    <img
                        src="{h(totp_qr)}"
                        style="width:220px;height:220px;border:1px solid #e5e7eb;border-radius:12px;background:#fff;"
                    >
                    <div class="help" style="text-align:center;margin-top:6px;">
                        扫码添加到认证器
                    </div>
                </div>

                <div style="min-width:260px;flex:1;">
                    <div class="form-item">
                        <label>2FA 密钥</label>
                        <input value="{h(totp_secret)}" readonly>
                    </div>

                    <br>

                    <div class="form-item">
                        <label>otpauth 链接</label>
                        <div class="help">
                            <a href="{h(totp_uri)}" style="word-break:break-all;" target="_blank">
                                {h(totp_uri)}
                            </a>
                        </div>
                    </div>

                    <br>

                    <div class="form-item">
                        <label>输入认证器中的 6 位验证码</label>
                        <input name="totp_code" placeholder="首次开启 2FA 时必填，例如：123456">
                        <div class="help">
                            如果当前已经开启 2FA，且没有更换密钥，保存其它配置时可不填写。
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">在线脚本源</div>
    <div class="form-item">
        <label>脚本源 index.json 地址</label>
        <input name="online_script_source" value="{h(cfg.get('online_script_source', 'https://raw.githubusercontent.com/liyw0205/fls-scripts/main/index.json'))}">
        <div class="help">
            在线脚本页面会从该 JSON 地址读取脚本列表。<br>
            默认：<code>https://raw.githubusercontent.com/liyw0205/fls-scripts/main/index.json</code>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">日志清理</div>
    <div class="form-grid">
        <div class="form-item">
            <label>清理间隔，分钟</label>
            <input name="log_cleanup_minutes" type="number" value="{h(cfg.get('log_cleanup_minutes', 30))}">
        </div>
        <div class="form-item">
            <label>单个日志最大 MB</label>
            <input name="log_max_size_mb" type="number" value="{h(cfg.get('log_max_size_mb', 10))}">
        </div>
        <div class="form-item">
            <label>每个任务保留日志数量</label>
            <input name="log_keep_per_task" type="number" value="{h(cfg.get('log_keep_per_task', 10))}">
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">任务运行控制</div>

    <div class="form-grid">
        <div class="form-item">
            <label>任务超时时间，秒</label>
            <input name="task_timeout_seconds" type="number" min="0" value="{h(cfg.get('task_timeout_seconds', 1800))}">
            <div class="help">
                默认 1800 秒。设置为 0 表示关闭超时控制。<br>
                任务运行超过该时间会被强制结束，避免卡死。
            </div>
        </div>

        <div class="form-item">
            <label>全局随机延迟，秒</label>
            <input name="random_delay_seconds" type="number" min="0" max="120" value="{h(cfg.get('random_delay_seconds', 0))}">
            <div class="help">
                范围 1-120 秒。设置为 0 表示不启用。<br>
                任务选择“使用全局随机延迟”时，会在 1 到该秒数之间随机等待。
            </div>
        </div>
    </div>
</div>

{task_type_table}

<div class="card">
    <button class="btn btn-primary" type="submit">保存配置</button>
</div>
</form>

<script>
function flsToggleSecurityBox(){{
    var enabled = document.querySelector('input[name="security_verify_enabled"]');
    var typeEl = document.getElementById("securityVerifyType");
    var codeBox = document.getElementById("securityCodeBox");
    var totpBox = document.getElementById("securityTotpBox");

    var on = enabled && enabled.checked;
    var type = typeEl ? typeEl.value : "code";

    if(codeBox){{
        codeBox.style.display = on && type === "code" ? "block" : "none";
    }}

    if(totpBox){{
        totpBox.style.display = on && type === "totp" ? "block" : "none";
    }}
}}

flsToggleSecurityBox();
</script>
"""

    return layout("配置", "config", body)
