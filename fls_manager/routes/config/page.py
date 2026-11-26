import binascii
import hashlib
import json

from flask import jsonify

from ._common import *
from ...config import DEFAULT_CONFIG, normalize_config_data
from ...models import TASKS_MUTATION_LOCK
from ...paths import CONFIG_FILE, CONFIG_APPLY_STATE_FILE
from ...storage import storage_transaction, read_json, write_json
from ...utils import now_str
from ...ui.components import table_card, page_header


def _config_snapshot():
    with storage_transaction():
        try:
            raw = CONFIG_FILE.read_bytes()
        except FileNotFoundError:
            raw = b""
            data = {}
        else:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("面板配置不是 JSON 对象，未覆盖原文件")
        cfg = normalize_config_data(data)
        return cfg, _apply_revision(cfg)


def _apply_revision(cfg):
    settings = {key: cfg.get(key) for key in DEFAULT_CONFIG}
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()


def _config_apply_state(cfg):
    state = read_json(CONFIG_APPLY_STATE_FILE, {})
    if (
        not isinstance(state, dict) or not state
        or state.get("status") not in ("running", "succeeded", "failed", "unknown")
        or not isinstance(state.get("errors"), list)
    ):
        return {"status": "unknown", "errors": ["尚未记录当前已保存配置的应用结果，请重新应用已保存配置。"]} if CONFIG_FILE.exists() else {}
    state["errors"] = [str(error) for error in state["errors"]]
    if state.get("status") == "running" or state.get("revision") != _apply_revision(cfg):
        state = {**state, "status": "unknown", "errors": ["无法确认当前已保存配置的应用结果，请重新应用已保存配置。"]}
        try:
            write_json(CONFIG_APPLY_STATE_FILE, state)
        except Exception as e:
            state["errors"].append(f"未知状态保存失败：{e}")
    return state


def _apply_saved_config(cfg, *, cleanup_operation=None, scheduler_operation=None):
    state = {"status": "running", "revision": _apply_revision(cfg), "errors": [], "updated_at": now_str()}
    try:
        write_json(CONFIG_APPLY_STATE_FILE, state)
    except Exception as e:
        return {**state, "status": "unknown", "errors": [f"配置已保存，但应用状态记录失败，未开始应用：{e}"]}
    operations = (
        ("日志清理", cleanup_operation or cleanup_logs),
        ("调度器重载", scheduler_operation or reload_scheduler),
    )
    for label, operation in operations:
        try:
            operation()
        except Exception as e:
            state["errors"].append(f"{label}失败：{e}")
    state["status"] = "failed" if state["errors"] else "succeeded"
    state["updated_at"] = now_str()
    try:
        write_json(CONFIG_APPLY_STATE_FILE, state)
    except Exception as e:
        state = {**state, "status": "unknown", "errors": [*state["errors"], f"配置应用结果保存失败：{e}"]}
    return state


def _config_apply_error(state, logged_out=False):
    actions = (
        '<form class="inline-form" method="post" action="/config/apply">'
        '<button class="btn btn-primary" type="submit">重新应用已保存配置</button></form>'
        '<a class="btn btn-gray" href="/config">查看已保存配置</a>'
    )
    if logged_out:
        actions = '<a class="btn btn-primary" href="/login?next=/config">重新登录</a>'
    body = page_header("配置已保存，应用待恢复", help_html="认证设置已更新，请重新登录后恢复应用。" if logged_out else "不会再次保存配置。", actions_html=actions)
    body += _config_message("配置已保存；" + "；".join(state.get("errors", [])))
    return layout("面板配置应用待恢复", "config", body), 503


def _config_message(message):
    if not message:
        return ""
    return f'<section class="section fls-form-section" data-config-error role="alert"><h2 class="section-title">配置处理结果</h2><p class="help">{h(message)}</p></section>'


def _config_read_error(error):
    draft = json.dumps(request.form.to_dict(flat=False), ensure_ascii=False, indent=2) if request.method == "POST" else ""
    body = page_header("配置读取失败", help_html=f"{h(error)}。未保存，请修复配置文件或恢复备份。", actions_html='<a class="btn btn-gray" href="/config">重新读取配置</a>')
    if draft:
        body += f'<section class="section"><h2 class="section-title">未保存的草稿</h2><textarea readonly aria-label="未保存的草稿">{h(draft)}</textarea></section>'
    return layout("配置读取失败", "config", body), 500


def _config_draft(old_cfg):
    draft = {**old_cfg, **request.form.to_dict()}
    draft["security_verify_enabled"] = request.form.get("security_verify_enabled") == "1"
    draft["task_types"] = {key: request.form.get(f"type_{key}") == "1" for key in DEFAULT_CONFIG["task_types"]}
    return draft


@bp.route("/config/apply", methods=["POST"])
@bp.route("/api/config/apply", methods=["POST"])
def config_apply():
    if request.path.startswith("/api/") and request.is_json:
        from .native import apply_json_config
        return apply_json_config()
    try:
        with TASKS_MUTATION_LOCK:
            cfg, _revision = _config_snapshot()
            state = _apply_saved_config(cfg)
    except Exception as e:
        if request.path.startswith("/api/"):
            return jsonify(ok=False, saved=None, status="unknown", msg=str(e)), 500
        return _config_read_error(e)
    if request.path.startswith("/api/"):
        return jsonify(ok=state["status"] == "succeeded", saved=True, state=state), 200 if state["status"] == "succeeded" else 503
    if state["status"] != "succeeded":
        return _config_apply_error(state)
    return redirect(url_for("config.config_page"))


@bp.route("/config", methods=["GET", "POST"])
def config_page():
    try:
        with TASKS_MUTATION_LOCK:
            old_cfg, revision = _config_snapshot()
            state = _config_apply_state(old_cfg) if request.method == "GET" else {}
    except Exception as e:
        return _config_read_error(e)
    if request.method == "POST":
        draft = _config_draft(old_cfg)
        if request.form.get("config_revision") != revision:
            return _render_config_form(draft, revision, "配置已变更或编辑状态失效，未保存。请对照当前配置合并后重试。", 409, current_cfg=old_cfg)
        theme = str(request.form.get("theme", DEFAULT_CONFIG["theme"]) or DEFAULT_CONFIG["theme"]).strip().lower()
        if theme not in THEME_OPTIONS:
            return _render_config_form(draft, revision, "主题选择无效，未保存。", 400)
        if request.form.get("security_verify_type", "code").strip() not in ("code", "totp"):
            return _render_config_form(draft, revision, "安全验证方式无效，未保存。", 400)
        for key in ("security_verify_enabled", *(f"type_{key}" for key in DEFAULT_CONFIG["task_types"])):
            if any(value not in ("", "1") for value in request.form.getlist(key)):
                return _render_config_form(draft, revision, f"{key} 选择无效，未保存。", 400)
        if any("\x00" in value for value in request.form.values()):
            return _render_config_form(draft, revision, "配置包含 NUL 字符，未保存。", 400)
        limits = {"port": (1, 65535), "log_cleanup_minutes": (1, 1440), "log_max_size_mb": (1, None),
                  "log_keep_per_task": (1, None), "task_timeout_seconds": (0, None), "random_delay_seconds": (0, 120)}
        for key, (minimum, maximum) in limits.items():
            try:
                value = int(request.form.get(key, old_cfg[key]))
                if value < minimum or (maximum is not None and value > maximum):
                    raise ValueError(key)
            except ValueError:
                return _render_config_form(draft, revision, f"{key} 必须为允许范围内的整数，未保存。", 400)

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
        new_admin_token = request.form.get("admin_token", old_admin_token).strip()

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
                try:
                    totp_valid = verify_totp(totp_secret, totp_code)
                except (ValueError, binascii.Error):
                    totp_valid = False
                if not totp_valid:
                    draft["totp_secret"] = totp_secret
                    return _render_config_form(draft, revision, "2FA 验证码错误，未开启 2FA。请用认证器生成当前验证码后重新保存。", 400)

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
            "theme": theme,

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

        with TASKS_MUTATION_LOCK:
            try:
                with storage_transaction():
                    current_cfg, current_revision = _config_snapshot()
                    if current_revision != revision:
                        return _render_config_form(draft, current_revision, "配置在保存期间已变更，未覆盖新配置。请合并后重试。", 409, current_cfg=current_cfg)
                    save_config(cfg)
            except Exception as e:
                return _render_config_form(draft, revision, f"配置保存失败：{e}。草稿已保留。", 500)
            # Invalidate the old authenticated session as soon as credentials are published.
            if token_changed or security_changed:
                auth_clear_session()
            try:
                saved_cfg, _saved_revision = _config_snapshot()
                state = _apply_saved_config(saved_cfg)
            except Exception as e:
                state = {"status": "unknown", "errors": [f"配置已保存，但重新读取失败：{e}"]}
        if state["status"] != "succeeded":
            return _config_apply_error(state, logged_out=token_changed or security_changed)
        if token_changed or security_changed:
            return redirect(url_for("auth.login"))

        return redirect(url_for("config.config_page"))

    if state.get("status") in ("failed", "unknown"):
        message = "已保存配置的应用待恢复：" + "；".join(state.get("errors", []))
    else:
        message = ""
    return _render_config_form(old_cfg, revision, message)


def _render_config_form(cfg, revision, message="", status=200, current_cfg=None):
    types = cfg.get("task_types", {})

    def checked(k):
        return "checked" if types.get(k) else ""

    current_theme = str(cfg.get("theme", DEFAULT_CONFIG["theme"]) or DEFAULT_CONFIG["theme"])
    if current_theme not in THEME_OPTIONS:
        current_theme = DEFAULT_CONFIG["theme"]

    theme_options = "".join(
        f'<option value="{h(key)}"{" selected" if key == current_theme else ""}>'
        f'{h(label)}</option>'
        for key, label in THEME_OPTIONS.items()
    )

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
    <td><input type="checkbox" name="type_{h(k)}" value="1" {checked(k)} style="width:auto;" aria-label="启用 {h(name)}"></td>
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
    totp_qr_html = (
        f'<img src="{h(totp_qr)}" alt="2FA TOTP 二维码" '
        'style="width:220px;height:220px;border:1px solid #e5e7eb;border-radius:12px;background:#fff;">'
        if totp_qr else
        '<div class="help" role="status">本地二维码组件不可用；请使用下方 otpauth 链接或密钥手动添加。</div>'
    )

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

    header = page_header(
        "面板配置",
        help_html="按主题管理登录、安全、在线脚本、日志和任务运行设置；保存后按提示重新加载面板。",
        actions_html='<form class="inline-form" method="post" action="/config/apply"><button class="btn btn-primary" type="submit">重新应用已保存配置</button></form>',
    )
    current_html = ""
    if current_cfg is not None:
        visible_cfg = {key: current_cfg[key] for key in DEFAULT_CONFIG}
        for key in ("admin_token", "totp_secret"):
            visible_cfg[key] = mask_secret_value(visible_cfg[key])
        current_html = f'<section class="section"><h2 class="section-title">当前配置</h2><textarea readonly aria-label="当前配置">{h(json.dumps(visible_cfg, ensure_ascii=False, indent=2))}</textarea></section>'
    body = f"""
{header}
{_config_message(message)}
{current_html}
<form method="post" action="/config">
<input type="hidden" name="config_revision" value="{h(revision)}">
<nav class="fls-section-nav" aria-label="配置区块导航">
    <span class="fls-section-nav-label">配置区块</span>
    <a href="#config-login">登录</a>
    <a href="#config-theme">主题</a>
    <a href="#config-security">安全验证</a>
    <a href="#config-online">在线脚本</a>
    <a href="#config-logs">日志清理</a>
    <a href="#config-runtime">任务运行</a>
    <a href="#config-types">脚本类型</a>
    <a href="#config-save">保存</a>
</nav>

<section class="section fls-section fls-form-section" id="config-login">
    <h2 class="section-title">登录设置</h2>
    <div class="form-item">
        <label>登录 Token</label>
        <input name="admin_token" type="password" value="{h(cfg.get('admin_token', ''))}" autocomplete="new-password" aria-label="登录 Token">
        <div class="help">
            Token 为空时，面板会进入首次设置引导 /setup。<br>
            不建议在公网环境关闭或清空 Token。<br>
            修改 Token 后会自动退出登录。
        </div>
    </div>
    <br>
    <div class="form-item">
        <label>面板端口，保存后重启生效</label>
        <input name="port" type="number" min="1" max="65535" value="{h(cfg.get('port', 5700))}" aria-label="面板端口">
        <div class="help">当前进程实际监听端口：{h(get_port())}</div>
    </div>
</section>

<section class="section fls-section fls-form-section" id="config-theme">
    <h2 class="section-title">主题选择</h2>
    <div class="form-item">
        <label for="panelTheme">面板主题</label>
        <select name="theme" id="panelTheme" aria-label="面板主题">
            {theme_options}
        </select>
        <div class="help">
            deepseek 使用蓝色强调与清晰的中性表面，并跟随系统暗色偏好；新拟物派保留柔和浮雕层次。编辑杂志风采用纸感排版，玻璃拟态使用深色半透明层次。保存后刷新页面生效。
        </div>
    </div>
</section>

<section class="section fls-section fls-form-section" id="config-security">
    <h2 class="section-title">安全验证</h2>

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
            aria-label="登录后启用二次安全验证"
        >
        登录后启用二次安全验证
    </label>

    <br><br>

    <div class="form-item">
        <label>验证方式</label>
        <select name="security_verify_type" id="securityVerifyType" aria-label="安全验证方式" onchange="flsToggleSecurityBox()">
            <option value="code" {code_selected}>随机验证码</option>
            <option value="totp" {totp_selected}>2FA / TOTP 验证</option>
        </select>
    </div>

    <div id="securityCodeBox" style="margin-top:14px;">
        <div class="fls-subsection">
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

        <div class="fls-subsection">
            <div class="card-title">2FA / TOTP 验证</div>

            <div class="help">
                选择 2FA 后，请使用认证器 App 扫描二维码。<br>
                首次开启 2FA 或更换密钥时，需要输入一次正确验证码才会成功保存。<br>
                推荐 App：Google Authenticator、Microsoft Authenticator、1Password、Bitwarden 等。
            </div>

            <br>

            <div style="display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start;">
                <div>
                    {totp_qr_html}
                    <div class="help" style="text-align:center;margin-top:6px;">
                        扫码添加到认证器
                    </div>
                </div>

                <div style="min-width:260px;flex:1;">
                    <div class="form-item">
                        <label>2FA 密钥</label>
                    <input value="{h(mask_secret_value(totp_secret))}" readonly aria-label="2FA 密钥">
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
                        <input name="totp_code" placeholder="首次开启 2FA 时必填，例如：123456" aria-label="输入认证器中的 6 位验证码">
                        <div class="help">
                            如果当前已经开启 2FA，且没有更换密钥，保存其它配置时可不填写。
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</section>

<section class="section fls-section fls-form-section" id="config-online">
    <h2 class="section-title">在线脚本源</h2>
    <div class="form-item">
        <label>脚本源 index.json 地址</label>
        <input name="online_script_source" value="{h(cfg.get('online_script_source', 'https://raw.githubusercontent.com/liyw0205/fls-scripts/main/index.json'))}" aria-label="在线脚本源 index.json 地址">
        <div class="help">
            在线脚本页面会从该 JSON 地址读取脚本列表。<br>
            默认：<code>https://raw.githubusercontent.com/liyw0205/fls-scripts/main/index.json</code>
        </div>
    </div>
</section>

<section class="section fls-section fls-form-section" id="config-logs">
    <h2 class="section-title">日志清理</h2>
    <div class="form-grid">
        <div class="form-item">
            <label>清理间隔，分钟</label>
            <input name="log_cleanup_minutes" type="number" value="{h(cfg.get('log_cleanup_minutes', 30))}" aria-label="日志清理间隔，分钟">
        </div>
        <div class="form-item">
            <label>单个日志最大 MB</label>
            <input name="log_max_size_mb" type="number" value="{h(cfg.get('log_max_size_mb', 10))}" aria-label="单个日志最大 MB">
        </div>
        <div class="form-item">
            <label>每个任务保留日志数量</label>
            <input name="log_keep_per_task" type="number" value="{h(cfg.get('log_keep_per_task', 10))}" aria-label="每个任务保留日志数量">
        </div>
    </div>
</section>

<section class="section fls-section fls-form-section" id="config-runtime">
    <h2 class="section-title">任务运行控制</h2>

    <div class="form-grid">
        <div class="form-item">
            <label>任务超时时间，秒</label>
            <input name="task_timeout_seconds" type="number" min="0" value="{h(cfg.get('task_timeout_seconds', 1800))}" aria-label="任务超时时间，秒">
            <div class="help">
                默认 1800 秒。设置为 0 表示关闭超时控制。<br>
                任务运行超过该时间会被强制结束，避免卡死。
            </div>
        </div>

        <div class="form-item">
            <label>全局随机延迟，秒</label>
            <input name="random_delay_seconds" type="number" min="0" max="120" value="{h(cfg.get('random_delay_seconds', 0))}" aria-label="全局随机延迟，秒">
            <div class="help">
                范围 1-120 秒。设置为 0 表示不启用。<br>
                任务选择“使用全局随机延迟”时，会在 1 到该秒数之间随机等待。
            </div>
        </div>
    </div>
</section>

<section class="section fls-section" id="config-types">
{task_type_table}
</section>

<section class="section fls-section fls-form-section fls-save-section" id="config-save">
    <button class="btn btn-primary" type="submit">保存面板配置</button>
</section>
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

    return layout("面板配置", "config", body), status
