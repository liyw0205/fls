import os
import threading

from flask import request, redirect, url_for

from . import bp
from .helpers import fls_control_script, delayed_restart_panel, delayed_stop_panel

from ...ui.layout import layout
from ...ui.components import page_header_card
from ...utils import h
from ...paths import BASE_DIR


@bp.route("/about/restart-panel", methods=["GET", "POST"])
def about_restart_panel():
    if request.method == "GET":
        return redirect(url_for("about.about"))

    script = fls_control_script()

    if not script.exists():
        body = page_header_card(
            "重启失败",
            help_html=f"""
        <span style="color:#dc2626;font-weight:800;">未找到 FLS 控制脚本：{h(script)}</span><br>
        Windows 请确认存在：<code>{h(BASE_DIR / "fls.bat")}</code> 或 <code>{h(BASE_DIR / "fls.ps1")}</code><br>
        Linux / Termux 请确认存在：<code>{h(BASE_DIR / "fls.sh")}</code>
""",
            actions_html='<a class="btn btn-gray" href="/about">返回关于页</a>',
        )
        return layout("重启失败", "about", body), 400

    th = threading.Thread(
        target=delayed_restart_panel,
        daemon=True,
        name="fls-panel-restart",
    )
    th.start()

    body = page_header_card(
        "正在重启面板",
        help_html=f"""
        面板将在 1 秒后执行自重启。<br>
        系统类型：<code>{h(os.name)}</code><br>
        当前面板 PID：<code>{h(os.getpid())}</code><br>
        控制脚本：<code>{h(script)}</code>
""",
        actions_html="""
<a class="btn btn-gray" href="/about">返回关于页</a>
<a class="btn btn-primary" href="/">返回仪表盘</a>
<a class="btn btn-blue" href="/logfile/fls-manager-daemon.log?back=/about">查看面板日志</a>
""",
    ) + """

<script>
setTimeout(function(){
    location.href = "/";
}, 10000);
</script>
"""
    return layout("正在重启面板", "about", body)


@bp.route("/about/stop-panel", methods=["GET", "POST"])
def about_stop_panel():
    if request.method == "GET":
        return redirect(url_for("about.about"))

    script = fls_control_script()

    if not script.exists():
        body = page_header_card(
            "停止失败",
            help_html=f"""
        <span style="color:#dc2626;font-weight:800;">未找到 FLS 控制脚本：{h(script)}</span><br>
        Windows 请确认存在：<code>{h(BASE_DIR / "fls.bat")}</code> 或 <code>{h(BASE_DIR / "fls.ps1")}</code><br>
        Linux / Termux 请确认存在：<code>{h(BASE_DIR / "fls.sh")}</code>
""",
            actions_html='<a class="btn btn-gray" href="/about">返回关于页</a>',
        )
        return layout("停止失败", "about", body), 400

    th = threading.Thread(
        target=delayed_stop_panel,
        daemon=True,
        name="fls-panel-stop",
    )
    th.start()

    body = page_header_card(
        "正在停止面板",
        help_html=f"""
        面板将在 1 秒后停止。<br>
        系统类型：<code>{h(os.name)}</code><br>
        当前面板 PID：<code>{h(os.getpid())}</code><br>
        控制脚本：<code>{h(script)}</code><br>
        停止后需要你手动重新启动面板，或等待系统自启服务拉起。
""",
        actions_html='<a class="btn btn-blue" href="/logfile/fls-manager-daemon.log?back=/about">查看面板日志</a>',
    )
    return layout("正在停止面板", "about", body)
