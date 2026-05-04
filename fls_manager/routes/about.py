from flask import Blueprint

from ..ui.layout import layout
from ..utils import h
from ..paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ..constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX

bp = Blueprint("about", __name__)


@bp.route("/about")
def about():
    body = f"""
<div class="card">
    <div class="card-title">关于 FLS 面板</div>
    <div class="help">
        <p><b>FLS 面板</b> 是一个轻量级脚本任务管理面板，可用于管理 Python、Shell、Node.js 等脚本任务。</p>
        <p>支持任务管理、Cron 定时、脚本导入/拉取、日志查看、依赖管理、代理配置、通知管理、备份恢复和面板配置。</p>
    </div>
</div>

<div class="card">
    <div class="card-title">项目信息</div>
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
            </tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-title">常用入口</div>
    <div class="action-row">
        <a class="btn btn-primary" href="/tasks">任务管理</a>
        <a class="btn btn-blue" href="/pull">脚本管理</a>
        <a class="btn btn-orange" href="/logs">日志管理</a>
        <a class="btn btn-primary" href="/notify">通知管理</a>
        <a class="btn btn-gray" href="/panel/status">环境状态</a>
        <a class="btn btn-gray" href="/config">配置</a>
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
