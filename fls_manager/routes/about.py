import os
import re
import time
import shutil
import subprocess

from flask import Blueprint, request

from ..ui.layout import layout
from ..utils import h
from ..paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ..constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX

bp = Blueprint("about", __name__)


def git_available():
    return bool(shutil.which("git"))


def run_git(args, timeout=30):
    """
    在 BASE_DIR 下执行 git 命令。
    返回：
        ok, output
    """
    if not git_available():
        return False, "未安装 git"

    try:
        r = subprocess.run(
            ["git"] + list(args),
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )

        return r.returncode == 0, (r.stdout or "").strip()

    except Exception as e:
        return False, str(e)


def is_git_repo():
    ok, out = run_git(["rev-parse", "--is-inside-work-tree"], timeout=8)
    return ok and out.strip().lower() == "true"


def git_text(args, default="-", timeout=15):
    ok, out = run_git(args, timeout=timeout)

    if not ok or not out:
        return default

    return out.strip()


def git_fetch_remote():
    ok, out = run_git(["fetch", "--all", "--prune"], timeout=60)

    if ok:
        return True, "更新日志刷新成功"

    return False, "更新日志刷新失败：" + out


def get_version_info(fetch=False):
    """
    获取当前版本和更新日志。
    """
    info = {
        "git_available": git_available(),
        "is_repo": False,
        "fetch_ok": None,
        "fetch_msg": "",
        "current_full": "-",
        "current_short": "-",
        "current_subject": "-",
        "current_time": "-",
        "remote": "-",
        "logs": [],
        "error": "",
    }

    if not info["git_available"]:
        info["error"] = "系统未安装 git，无法获取版本信息"
        return info

    if not is_git_repo():
        info["error"] = f"当前目录不是 Git 仓库：{BASE_DIR}"
        return info

    info["is_repo"] = True

    if fetch:
        ok, msg = git_fetch_remote()
        info["fetch_ok"] = ok
        info["fetch_msg"] = msg

    info["current_full"] = git_text(["rev-parse", "HEAD"])
    info["current_short"] = git_text(["rev-parse", "--short", "HEAD"])
    info["current_subject"] = git_text(["show", "-s", "--format=%s", "HEAD"])
    info["current_time"] = git_text(["show", "-s", "--format=%ci", "HEAD"])
    info["remote"] = git_text(["remote", "get-url", "origin"])

    ok, log_text = run_git(
        [
            "log",
            "--date=iso",
            "--pretty=format:%H%x09%h%x09%ci%x09%s",
            "-n",
            "20",
            "--all",
        ],
        timeout=30,
    )

    if ok and log_text:
        seen = set()

        for line in log_text.splitlines():
            parts = line.split("\t", 3)

            if len(parts) < 4:
                continue

            full_hash, short_hash, date_text, subject = parts

            if full_hash in seen:
                continue

            seen.add(full_hash)

            info["logs"].append({
                "full": full_hash,
                "short": short_hash,
                "date": date_text,
                "subject": subject,
                "current": full_hash == info["current_full"],
            })

    return info


def render_update_log_rows(logs):
    if not logs:
        return '<tr><td colspan="4">暂无更新日志</td></tr>'

    rows = ""

    for item in logs:
        if item.get("current"):
            version_badge = '<span class="badge green">当前版本</span>'
            action = '<span class="badge green">正在使用</span>'
        else:
            version_badge = '<span class="badge gray">可更新</span>'
            action = f"""
<form method="post" action="/about/update-version" style="display:inline;">
    <input type="hidden" name="version" value="{h(item.get("full"))}">
    <button class="btn btn-orange" type="submit" onclick="return confirm('确定更新到该版本吗？更新成功后面板会自动重启。')">
        更新到此版本
    </button>
</form>
"""

        rows += f"""
<tr>
    <td>
        <code>{h(item.get("short"))}</code>
        <div class="help">{h(item.get("full"))}</div>
    </td>
    <td>{h(item.get("date"))}</td>
    <td>
        <b>{h(item.get("subject"))}</b>
        <div style="margin-top:6px;">{version_badge}</div>
    </td>
    <td>{action}</td>
</tr>
"""

    return rows


def schedule_restart():
    """
    更新成功后后台自动重启。

    使用独立 session 启动，避免被当前 Flask 进程退出影响。
    """
    script = BASE_DIR / "fls.sh"

    if not script.exists():
        return False, f"未找到重启脚本：{script}"

    cmd = f"sleep 1; cd {sh_quote(str(BASE_DIR))} && sh fls.sh restart"

    try:
        kwargs = {
            "cwd": str(BASE_DIR),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }

        if os.name != "nt":
            kwargs["preexec_fn"] = os.setsid

        subprocess.Popen(
            ["sh", "-lc", cmd],
            **kwargs,
        )

        return True, "已提交自动重启"

    except Exception as e:
        return False, str(e)


def sh_quote(value):
    """
    简单 shell quote。
    """
    s = str(value)
    return "'" + s.replace("'", "'\"'\"'") + "'"


@bp.route("/about")
def about():
    fetch = request.args.get("fetch") == "1"
    version = get_version_info(fetch=fetch)

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
<style>
.fls-update-log-fold {
    overflow:hidden;
}

.fls-update-log-fold summary {
    cursor:pointer;
    list-style:none;
}

.fls-update-log-fold summary::-webkit-details-marker {
    display:none;
}

.fls-update-log-fold summary::after {
    content:"点击展开";
    display:block;
    margin-top:8px;
    color:#6b7280;
    font-size:12px;
    font-weight:800;
}

.fls-update-log-fold[open] summary::after {
    content:"点击收起";
}
</style>
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
        fetch_msg_html = ""

        if version.get("fetch_msg"):
            color = "#18a058" if version.get("fetch_ok") else "#dc2626"

            fetch_msg_html = f"""
<div class="card">
    <div class="help" style="color:{color};font-weight:800;">
        {h(version.get("fetch_msg"))}
    </div>
</div>
"""

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
            <a class="btn btn-primary" href="/about?fetch=1">刷新更新日志</a>
            {daemon_log_btn}
        </div>
    </div>
</div>

{fetch_msg_html}

<details class="card fls-update-log-fold">
    <summary>
        <div>
            <div class="card-title">更新日志，最近 20 条</div>
            <div class="help">
                默认折叠，点击展开查看版本更新内容并选择更新版本。
            </div>
        </div>
    </summary>

    <br>

    <div class="help">
        这里显示项目 Git 提交时填写的更新内容。<br>
        可以选择某个版本进行更新，更新成功后面板会自动重启。
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

    body = f"""
<div class="card">
    <div class="card-title">关于 FLS 面板</div>
    <div class="help">
        <p><b>FLS 面板</b> 是一个轻量级脚本任务管理面板，可用于管理 Python、Shell、Node.js 等脚本任务。</p>
        <p>支持任务管理、Cron 定时、脚本导入/拉取、日志查看、依赖管理、代理配置、通知管理、备份恢复和面板配置。</p>
    </div>
</div>

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


@bp.route("/about/update-version", methods=["POST"])
def about_update_version():
    version = request.form.get("version", "").strip()

    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", version):
        body = f"""
<div class="card">
    <div class="card-title">更新失败</div>
    <div class="help" style="color:#dc2626;">
        版本号非法：{h(version)}
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("更新失败", "about", body), 400

    if not git_available():
        body = """
<div class="card">
    <div class="card-title">更新失败</div>
    <div class="help" style="color:#dc2626;">
        系统未安装 git。
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("更新失败", "about", body), 400

    if not is_git_repo():
        body = f"""
<div class="card">
    <div class="card-title">更新失败</div>
    <div class="help" style="color:#dc2626;">
        当前目录不是 Git 仓库：{h(BASE_DIR)}
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("更新失败", "about", body), 400

    before_version = git_text(["rev-parse", "--short", "HEAD"], default="-")

    fetch_ok, fetch_out = run_git(["fetch", "--all", "--prune"], timeout=60)
    checkout_ok, checkout_out = run_git(["checkout", version], timeout=60)

    after_version = git_text(["rev-parse", "--short", "HEAD"], default="-")

    restart_ok = False
    restart_msg = ""

    if checkout_ok:
        restart_ok, restart_msg = schedule_restart()

    ok = checkout_ok and restart_ok

    if checkout_ok:
        next_html = f"""
<div class="card">
    <div class="card-title">更新成功，正在自动重启</div>
    <div class="help" style="color:#18a058;font-weight:800;">
        版本已更新：{h(before_version)} → {h(after_version)}<br>
        {h(restart_msg)}<br>
        面板会在几秒后自动重启，请稍后刷新页面。
    </div>
    <br>
    <a class="btn btn-primary" href="/about">稍后刷新</a>
    <a class="btn btn-blue" href="/logfile/fls-manager-daemon.log?back=/about">查看面板日志</a>
</div>

<script>
setTimeout(function(){{
    location.href = "/about";
}}, 8000);
</script>
"""
    else:
        next_html = f"""
<div class="card">
    <div class="card-title">更新失败</div>
    <div class="help" style="color:#dc2626;font-weight:800;">
        更新失败，请查看下方输出。
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""

    body = f"""
<div class="card">
    <div class="card-title">版本更新结果</div>
    <div class="help">
        目标版本：<code>{h(version)}</code><br>
        更新前：<code>{h(before_version)}</code><br>
        更新后：<code>{h(after_version)}</code><br>
        代码更新：<b style="color:{'#18a058' if checkout_ok else '#dc2626'};">{"成功" if checkout_ok else "失败"}</b><br>
        自动重启：<b style="color:{'#18a058' if restart_ok else '#dc2626'};">{"已提交" if restart_ok else (h(restart_msg or "未执行"))}</b>
    </div>
</div>

{next_html}

<div class="card">
    <div class="card-title">刷新更新日志输出</div>
    <pre class="log" style="min-height:180px;">{h(fetch_out or "无输出")}</pre>
</div>

<div class="card">
    <div class="card-title">更新输出</div>
    <pre class="log" style="min-height:220px;">{h(checkout_out or "无输出")}</pre>
</div>
"""

    return layout("版本更新结果", "about", body), 200 if ok else 500