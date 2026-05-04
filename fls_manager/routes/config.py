from flask import Blueprint, request, redirect, url_for
from ..config import load_config, save_config, get_port
from ..scheduler import reload_scheduler
from ..logs import cleanup_logs
from ..utils import h
from ..ui.layout import layout

bp = Blueprint("config", __name__)

@bp.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "POST":
        task_types = {}

        for k in ["py", "sh", "js", "ts", "ps1", "bat", "php", "rb", "pl", "lua", "jar"]:
            task_types[k] = request.form.get(f"type_{k}") == "1"

        cfg = {
            "admin_token": request.form.get("admin_token", "").strip(),
            "port": max(1, min(65535, int(request.form.get("port", "5700") or 5700))),
            "log_cleanup_minutes": max(1, min(1440, int(request.form.get("log_cleanup_minutes", "30") or 30))),
            "log_max_size_mb": max(1, int(request.form.get("log_max_size_mb", "10") or 10)),
            "log_keep_per_task": max(1, int(request.form.get("log_keep_per_task", "10") or 10)),
            "task_timeout_seconds": max(
                0,
                int(request.form.get("task_timeout_seconds", "1800") or 0)
            ),
            "random_delay_seconds": max(
                0,
                min(120, int(request.form.get("random_delay_seconds", "0") or 0))
            ),
            "task_types": task_types,
        }

        save_config(cfg)
        cleanup_logs()
        reload_scheduler()

        return redirect(url_for("config.config_page"))

    cfg = load_config()
    types = cfg.get("task_types", {})

    def checked(k):
        return "checked" if types.get(k) else ""

    rows = ""

    for k, name in [
        ("py", "Python .py"),
        ("sh", "Shell .sh"),
        ("js", "Node .js"),
        ("ts", "TypeScript .ts"),
        ("ps1", "PowerShell .ps1"),
        ("bat", "Windows Batch .bat"),
        ("php", "PHP .php"),
        ("rb", "Ruby .rb"),
        ("pl", "Perl .pl"),
        ("lua", "Lua .lua"),
        ("jar", "Java Jar .jar"),
    ]:
        rows += f"""
<tr>
    <td><b>{h(name)}</b></td>
    <td><input type="checkbox" name="type_{h(k)}" value="1" {checked(k)} style="width:auto;"></td>
</tr>
"""

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">登录配置</div>
    <div class="form-item">
        <label>登录 Token</label>
        <input name="admin_token" value="{h(cfg.get('admin_token', ''))}">
        <div class="help">
            Token 为空时，面板会进入首次设置引导 /setup。<br>
            不建议在公网环境关闭或清空 Token。
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

<div class="card">
    <div class="card-title">task 可执行脚本类型</div>
    <div class="table-wrap">
        <table>
            <thead><tr><th>类型</th><th>启用</th></tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存配置</button>
</div>
</form>
"""

    return layout("配置", "config", body)
