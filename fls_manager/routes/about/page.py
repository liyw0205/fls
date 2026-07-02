from . import bp
from .helpers import (
    get_version_info,
    render_update_log_rows,
    about_panel_time_text,
    utc_offset_options,
    fls_control_script,
)
from ...ui.layout import layout
from ...utils import h
from ...paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ...constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX
from ...config import get_timezone_offset_hours, get_panel_timezone_text


@bp.route("/about")
def about():
    version = get_version_info()

    daemon_log = LOG_DIR / "fls-manager-daemon.log"

    daemon_log_btn = ""
    if daemon_log.exists():
        daemon_log_btn = f"""
<a class="btn btn-blue" href="/logfile/{h(daemon_log.name)}?back=/about">
    查看面板运行日志
</a>
"""

    version_card = ""

    if not version["git_available"] or not version["is_repo"]:
        version_card = f"""
<div class="card">
    <div class="card-title">当前版本 / 更新日志</div>
    <div class="help" style="color:#dc2626;">
        {h(version.get("error") or "版本信息不可用")}
    </div>
    <br>
    <div class="help">
        如果你是通过压缩包或手动复制方式安装的，可能无法获取 Git 更新日志。<br>
        如果需要使用版本更新功能，请使用 git clone 方式安装项目。
    </div>
</div>
"""
    else:
        rows = render_update_log_rows(version.get("logs") or [])

        version_card = f"""
<div class="card">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">当前版本 / 更新日志</div>
            <div class="help">
                当前版本：<code>{h(version.get("current_short"))}</code><br>
                版本说明：{h(version.get("current_subject"))}<br>
                更新时间：{h(version.get("current_time"))}<br>
                远程仓库：<code>{h(version.get("remote"))}</code>
            </div>
        </div>

        <div class="action-row">
            <form method="post" action="/about/refresh-log" style="display:inline;">
                <button class="btn btn-primary" type="submit">
                    后台刷新更新日志
                </button>
            </form>
            {daemon_log_btn}
        </div>
    </div>
</div>

<details class="card fls-update-log-fold">
    <summary>
        <div>
            <div class="card-title">更新日志，最近 20 条</div>
            <div class="help">
                默认折叠，点击展开查看版本更新内容并选择更新版本。<br>
                “后台刷新更新日志”会进入实时日志页，不会卡住当前页面。
            </div>
        </div>
    </summary>

    <br>

    <div class="help">
        这里显示项目 Git 提交时填写的更新内容。<br>
        可以选择某个版本进行后台更新。更新完成后需要手动重启面板。
    </div>
    <br>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>版本</th>
                    <th>时间</th>
                    <th>更新内容</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</details>
"""

    current_offset = get_timezone_offset_hours()
    script = fls_control_script()

    body = f"""
<style>
.fls-update-log-fold {{
    overflow:hidden;
}}

.fls-update-log-fold summary {{
    cursor:pointer;
    list-style:none;
}}

.fls-update-log-fold summary::-webkit-details-marker {{
    display:none;
}}

.fls-update-log-fold summary::after {{
    content:"点击展开";
    display:block;
    margin-top:8px;
    color:#6b7280;
    font-size:12px;
    font-weight:800;
}}

.fls-update-log-fold[open] summary::after {{
    content:"点击收起";
}}

.fls-time-mode-box {{
    display:none;
    margin-top:14px;
}}

.fls-time-mode-box.active {{
    display:block;
}}
</style>

<div class="card">
    <div class="card-title">关于 FLS 面板</div>
    <div class="help">
        <p><b>FLS 面板</b> 是一个轻量级脚本任务管理面板，可用于管理 Python、Shell、Node.js 等脚本任务。</p>
        <p>支持任务管理、Cron 定时、脚本导入/拉取、日志查看、依赖管理、代理配置、通知管理、备份恢复和面板配置。</p>
    </div>

    <br>

    <div class="action-row">
        <form method="post" action="/about/restart-panel" style="display:inline;">
            <button class="btn btn-orange" type="submit" onclick="return confirm('确定重启面板吗？重启期间页面会短暂无法访问。')">
                重启面板
            </button>
        </form>

        <form method="post" action="/about/stop-panel" style="display:inline;">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定停止面板吗？停止后需要手动重新启动。')">
                停止面板
            </button>
        </form>
    </div>
</div>

<div class="card">
    <div class="card-title">面板时间校准</div>
    <div class="help">
        当前面板时间：<b>{h(about_panel_time_text())}</b><br>
        当前面板时区：<b>{h(get_panel_timezone_text())}</b><br>
        时间校准主要用于修正系统时间或时区错误导致的 Cron 定时任务触发不准。<br>
        本功能不会修改系统时间。<br>
        自定义当前时间格式必须为：<code>yyyyMMddHHmmss</code>，例如：<code>20260508121200</code>。
    </div>

    <br>

    <div class="form-item">
        <label>选择校准方式</label>
        <select id="flsTimeSyncMode" onchange="flsToggleTimeSyncMode()">
            <option value="beijing">自动校准北京时间</option>
            <option value="utc_offset">选择 UTC 偏移自动校准</option>
            <option value="custom">自定义当前时间</option>
            <option value="reset">重置时间偏移</option>
        </select>
    </div>

    <div id="flsTimeBoxBeijing" class="fls-time-mode-box active">
        <form method="post" action="/about/time-sync">
            <input type="hidden" name="mode" value="beijing">

            <div class="card" style="box-shadow:none;border:1px solid #e5e7eb;margin-top:14px;">
                <div class="card-title">自动校准北京时间</div>
                <div class="help">
                    会从网络 HTTP Date 头获取标准 UTC 时间，并设置 FLS 面板虚拟时间为北京时间。<br>
                    不修改系统时间。<br>
                    Cron 会按北京时间重新计算。
                </div>
                <br>
                <button class="btn btn-primary" type="submit" onclick="return confirm('确定自动校准为北京时间吗？不会修改系统时间。')">
                    自动校准北京时间
                </button>
            </div>
        </form>
    </div>

    <div id="flsTimeBoxUtcOffset" class="fls-time-mode-box">
        <form method="post" action="/about/time-sync">
            <input type="hidden" name="mode" value="utc_offset">

            <div class="card" style="box-shadow:none;border:1px solid #e5e7eb;margin-top:14px;">
                <div class="card-title">选择 UTC 偏移自动校准</div>
                <div class="help">
                    可选择 <code>UTC-23</code> 到 <code>UTC+23</code>。<br>
                    例如选择 <code>UTC+7</code>，面板当前时间和 Cron 都会按 UTC+7 计算。<br>
                    不修改系统时间。
                </div>

                <br>

                <div class="form-item">
                    <label>UTC 偏移</label>
                    <select name="utc_offset">
                        {utc_offset_options(current_offset)}
                    </select>
                </div>

                <br>

                <button class="btn btn-blue" type="submit" onclick="return confirm('确定按选择的 UTC 偏移校准面板时间吗？不会修改系统时间。')">
                    按选择 UTC 校准
                </button>
            </div>
        </form>
    </div>

    <div id="flsTimeBoxCustom" class="fls-time-mode-box">
        <form method="post" action="/about/time-sync">
            <input type="hidden" name="mode" value="custom">

            <div class="card" style="box-shadow:none;border:1px solid #e5e7eb;margin-top:14px;">
                <div class="card-title">自定义当前时间</div>
                <div class="help">
                    输入格式必须是：<code>yyyyMMddHHmmss</code>。<br>
                    例如：<code>20260508121200</code>。<br>
                    下方选择 <code>UTC+8</code> 时，表示该时间是 <code>UTC+8</code> 的当前时间。<br>
                    下方选择 <code>UTC+7</code> 时，表示该时间是 <code>UTC+7</code> 的当前时间。<br>
                    不修改系统时间。
                </div>

                <br>

                <div class="form-grid">
                    <div class="form-item">
                        <label>自定义当前时间</label>
                        <input name="custom_time" placeholder="例如：20260508121200">
                    </div>

                    <div class="form-item">
                        <label>该时间属于哪个 UTC 偏移</label>
                        <select name="utc_offset">
                            {utc_offset_options(current_offset)}
                        </select>
                    </div>
                </div>

                <br>

                <button class="btn btn-orange" type="submit" onclick="return confirm('确定应用自定义面板时间吗？不会修改系统时间。')">
                    应用自定义时间
                </button>
            </div>
        </form>
    </div>

    <div id="flsTimeBoxReset" class="fls-time-mode-box">
        <form method="post" action="/about/time-sync">
            <input type="hidden" name="mode" value="reset">

            <div class="card" style="box-shadow:none;border:1px solid #e5e7eb;margin-top:14px;">
                <div class="card-title">重置时间偏移</div>
                <div class="help">
                    会清除面板虚拟时间偏移，仅保留当前 UTC 时区设置。<br>
                    如果你的系统时间本身已经正确，可以使用此项。
                </div>

                <br>

                <button class="btn btn-gray" type="submit" onclick="return confirm('确定重置面板时间偏移吗？')">
                    重置时间偏移
                </button>
            </div>
        </form>
    </div>

    <br>

    <div class="help" style="color:#18a058;">
        校准成功后会自动重载调度器，让 Cron 任务的下次执行时间重新计算。
    </div>
</div>

<script>
function flsToggleTimeSyncMode(){{
    var modeEl = document.getElementById("flsTimeSyncMode");
    if(!modeEl) return;

    var mode = modeEl.value || "beijing";

    var boxes = {{
        "beijing": document.getElementById("flsTimeBoxBeijing"),
        "utc_offset": document.getElementById("flsTimeBoxUtcOffset"),
        "custom": document.getElementById("flsTimeBoxCustom"),
        "reset": document.getElementById("flsTimeBoxReset")
    }};

    Object.keys(boxes).forEach(function(key){{
        if(!boxes[key]) return;

        if(key === mode){{
            boxes[key].classList.add("active");
        }}else{{
            boxes[key].classList.remove("active");
        }}
    }});
}}

flsToggleTimeSyncMode();
</script>

{version_card}

<div class="card">
    <div class="card-title">面板信息</div>
    <div class="table-wrap">
        <table>
            <tbody>
                <tr>
                    <td><b>作者</b></td>
                    <td>{h("余生只有凄渺")}</td>
                </tr>
                <tr>
                    <td><b>QQ群</b></td>
                    <td>{h("923184177")}</td>
                </tr>
                <tr>
                    <td><b>项目仓库</b></td>
                    <td>
                        <a href="https://github.com/liyw0205/fls" target="_blank">
                            https://github.com/liyw0205/fls
                        </a>
                    </td>
                </tr>
                <tr>
                    <td><b>主进程名</b></td>
                    <td>{h(MAIN_PROCESS_NAME)}</td>
                </tr>
                <tr>
                    <td><b>任务进程标识前缀</b></td>
                    <td>{h(TASK_PROCESS_PREFIX)}</td>
                </tr>
                <tr>
                    <td><b>工作目录</b></td>
                    <td>{h(BASE_DIR)}</td>
                </tr>
                <tr>
                    <td><b>数据目录</b></td>
                    <td>{h(DATA_DIR)}</td>
                </tr>
                <tr>
                    <td><b>日志目录</b></td>
                    <td>{h(LOG_DIR)}</td>
                </tr>
                <tr>
                    <td><b>脚本目录</b></td>
                    <td>{h(SCRIPT_DIR)}</td>
                </tr>
                <tr>
                    <td><b>控制脚本</b></td>
                    <td>{h(script)}</td>
                </tr>
            </tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-title">任务命令规则</div>
    <div class="help">
        使用 <b>task</b> 开头时，会从脚本目录运行对应文件；不使用 <b>task</b> 开头时，会作为系统命令执行。
    </div>
    <br>
    <div class="code">
task 1.py<br>
task a/test.sh<br>
task demo.js<br>
task demo.ts<br>
task script.ps1<br>
task run.bat<br>
task demo.php<br>
task demo.rb<br>
task demo.pl<br>
task demo.lua<br>
task app.jar<br><br>
不加 task 则作为系统命令执行，例如：<br>
python3 /root/test.py
    </div>
</div>

<div class="card">
    <div class="card-title">Cron 说明</div>
    <div class="code">
留空：手动任务<br><br>
5 位：分 时 日 月 周<br>
0 8 * * *     每天 08:00<br>
*/10 * * * *  每 10 分钟<br><br>
6 位：秒 分 时 日 月 周<br>
0 0 8 * * *   每天 08:00:00
    </div>
</div>

<div class="card">
    <div class="card-title">进程查看示例</div>
    <div class="code">
ps -ef | grep fls<br>
ps -eo pid,ppid,comm,args | grep fls
    </div>
</div>
"""

    return layout("关于", "about", body)
