import os
import threading

from flask import request, redirect, url_for

from . import bp
from .helpers import fls_control_script, delayed_restart_panel, delayed_stop_panel

from ...ui.layout import layout
from ...utils import h
from ...paths import BASE_DIR


@bp.route("/about/restart-panel", methods=["GET", "POST"])
def about_restart_panel():
    if request.method == "GET":
        return redirect(url_for("about.about"))

    script = fls_control_script()

    if not script.exists():
        body = f"""
<div class="card">
    <div class="card-title">重启失败</div>
    <div class="help" style="color:#dc2626;font-weight:800;">
        未找到 FLS 控制脚本：{h(script)}
    </div>
    <br>
    <div class="help">
        Windows 请确认存在：<code>{h(BASE_DIR / "fls.bat")}</code> 或 <code>{h(BASE_DIR / "fls.ps1")}</code><br>
        Linux / Termux 请确认存在：<code>{h(BASE_DIR / "fls.sh")}</code>
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("重启失败", "about", body), 400

    th = threading.Thread(
        target=delayed_restart_panel,
        daemon=True,
        name="fls-panel-restart",
    )
    th.start()

    body = f"""
<div class="card">
    <div class="card-title">正在重启面板</div>
    <div class="help">
        面板将在 1 秒后执行自重启。<br>
        系统类型：<code>{h(os.name)}</code><br>
        当前面板 PID：<code>{h(os.getpid())}</code><br>
        控制脚本：<code>{h(script)}</code>
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
    <a class="btn btn-primary" href="/">返回仪表盘</a>
    <a class="btn btn-blue" href="/logfile/fls-manager-daemon.log?back=/about">查看面板日志</a>
</div>

<script>
setTimeout(function(){{
    location.href = "/";
}}, 10000);
</script>
"""
    return layout("正在重启面板", "about", body)


@bp.route("/about/stop-panel", methods=["GET", "POST"])
def about_stop_panel():
    if request.method == "GET":
        return redirect(url_for("about.about"))

    script = fls_control_script()

    if not script.exists():
        body = f"""
<div class="card">
    <div class="card-title">停止失败</div>
    <div class="help" style="color:#dc2626;font-weight:800;">
        未找到 FLS 控制脚本：{h(script)}
    </div>
    <br>
    <div class="help">
        Windows 请确认存在：<code>{h(BASE_DIR / "fls.bat")}</code> 或 <code>{h(BASE_DIR / "fls.ps1")}</code><br>
        Linux / Termux 请确认存在：<code>{h(BASE_DIR / "fls.sh")}</code>
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("停止失败", "about", body), 400

    th = threading.Thread(
        target=delayed_stop_panel,
        daemon=True,
        name="fls-panel-stop",
    )
    th.start()

    body = f"""
<div class="card">
    <div class="card-title">正在停止面板</div>
    <div class="help">
        面板将在 1 秒后停止。<br>
        系统类型：<code>{h(os.name)}</code><br>
        当前面板 PID：<code>{h(os.getpid())}</code><br>
        控制脚本：<code>{h(script)}</code><br>
        停止后需要你手动重新启动面板，或等待系统自启服务拉起。
    </div>
    <br>
    <a class="btn btn-blue" href="/logfile/fls-manager-daemon.log?back=/about">查看面板日志</a>
</div>
"""
    return layout("正在停止面板", "about", body)