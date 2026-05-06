import os
import re
import time
import uuid
import shutil
import threading
import subprocess

from flask import Blueprint, request, redirect, url_for, jsonify

from ..ui.layout import layout
from ..ui.log_controls import log_controls
from ..utils import h, get_back_url, now_str, safe_name
from ..logs import tail_file
from ..paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ..constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX

bp = Blueprint("about", __name__)

ABOUT_JOBS = {}


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


def get_version_info():
    """
    获取当前版本和更新日志。

    注意：
    这里不再主动 fetch。
    刷新远程更新日志改为后台任务 /about/refresh-log。
    """
    info = {
        "git_available": git_available(),
        "is_repo": False,
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
        return '<tr><td colspan="4">暂无更新日志，请点击“刷新更新日志”</td></tr>'

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
    <button class="btn btn-orange" type="submit" onclick="return confirm('确定更新到该版本吗？更新任务将在后台执行。')">
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


def sh_quote(value):
    """
    简单 shell quote。
    """
    s = str(value)
    return "'" + s.replace("'", "'\"'\"'") + "'"


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


def about_job_log_file(job_id, action):
    safe_action = safe_name(action or "about-job")
    return LOG_DIR / f"about-{safe_action}-{job_id}.log"


def append_job_log(log_file, text=""):
    try:
        with open(log_file, "ab") as f:
            f.write(str(text).encode("utf-8", errors="replace"))

            if not str(text).endswith("\n"):
                f.write(b"\n")

    except Exception:
        pass


def about_job_running(job_id):
    info = ABOUT_JOBS.get(job_id)

    if not info:
        return False

    return bool(info.get("running"))


def run_git_logged(job_id, log_file, args, timeout=120):
    """
    执行 git 命令并写入日志。
    """
    cmd = ["git"] + list(args)

    append_job_log(log_file, "")
    append_job_log(log_file, "$ " + " ".join(cmd))
    append_job_log(log_file, f"cwd: {BASE_DIR}")
    append_job_log(log_file, "------------------------------------------------------------")

    try:
        r = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )

        output = r.stdout or ""

        if output:
            append_job_log(log_file, output.rstrip())
        else:
            append_job_log(log_file, "无输出")

        append_job_log(log_file, "------------------------------------------------------------")
        append_job_log(log_file, f"命令结束，退出码：{r.returncode}")

        return r.returncode == 0, output.strip()

    except subprocess.TimeoutExpired as e:
        msg = f"命令超时：{e}"
        append_job_log(log_file, msg)
        return False, msg

    except Exception as e:
        msg = f"命令异常：{e}"
        append_job_log(log_file, msg)
        return False, msg


def refresh_log_worker(job_id):
    info = ABOUT_JOBS.get(job_id)

    if not info:
        return

    log_file = info.get("log_file")

    info["running"] = True
    info["status"] = "正在刷新更新日志"
    info["error"] = ""
    info["updated_at"] = now_str()

    append_job_log(log_file, "===== FLS 更新日志后台刷新 =====")
    append_job_log(log_file, f"时间: {now_str()}")
    append_job_log(log_file, f"工作目录: {BASE_DIR}")
    append_job_log(log_file, "============================================================")

    try:
        if not git_available():
            raise RuntimeError("系统未安装 git")

        if not is_git_repo():
            raise RuntimeError(f"当前目录不是 Git 仓库：{BASE_DIR}")

        ok, out = run_git_logged(
            job_id,
            log_file,
            ["fetch", "--all", "--prune"],
            timeout=120,
        )

        if not ok:
            raise RuntimeError(out or "git fetch 失败")

        info["running"] = False
        info["status"] = "刷新完成"
        info["returncode"] = 0
        info["updated_at"] = now_str()

        append_job_log(log_file, "")
        append_job_log(log_file, f"===== 刷新完成: {now_str()} =====")
        append_job_log(log_file, "返回关于页即可看到最新更新日志。")

    except Exception as e:
        info["running"] = False
        info["status"] = "刷新失败"
        info["returncode"] = 1
        info["error"] = str(e)
        info["updated_at"] = now_str()

        append_job_log(log_file, "")
        append_job_log(log_file, f"===== 刷新失败: {now_str()} =====")
        append_job_log(log_file, f"错误: {e}")


def update_version_worker(job_id, version):
    info = ABOUT_JOBS.get(job_id)

    if not info:
        return

    log_file = info.get("log_file")

    info["running"] = True
    info["status"] = "正在更新"
    info["error"] = ""
    info["updated_at"] = now_str()

    append_job_log(log_file, "===== FLS 版本后台更新 =====")
    append_job_log(log_file, f"时间: {now_str()}")
    append_job_log(log_file, f"目标版本: {version}")
    append_job_log(log_file, f"工作目录: {BASE_DIR}")
    append_job_log(log_file, "============================================================")

    before_version = "-"
    after_version = "-"

    try:
        if not git_available():
            raise RuntimeError("系统未安装 git")

        if not is_git_repo():
            raise RuntimeError(f"当前目录不是 Git 仓库：{BASE_DIR}")

        before_version = git_text(["rev-parse", "--short", "HEAD"], default="-")
        append_job_log(log_file, f"更新前版本: {before_version}")

        fetch_ok, fetch_out = run_git_logged(
            job_id,
            log_file,
            ["fetch", "--all", "--prune"],
            timeout=120,
        )

        if not fetch_ok:
            raise RuntimeError(fetch_out or "git fetch 失败")

        reset_ok, reset_out = run_git_logged(
            job_id,
            log_file,
            ["reset", "--hard", "HEAD"],
            timeout=120,
        )

        if not reset_ok:
            raise RuntimeError(reset_out or "git reset --hard 失败")

        clean_ok, clean_out = run_git_logged(
            job_id,
            log_file,
            [
                "clean",
                "-fd",
                "-e", "data/",
                "-e", "scripts/",
                "-e", "log/",
                "-e", ".venv/",
            ],
            timeout=120,
        )

        if not clean_ok:
            raise RuntimeError(clean_out or "git clean -fd 失败")

        checkout_ok, checkout_out = run_git_logged(
            job_id,
            log_file,
            ["checkout", version],
            timeout=120,
        )

        if not checkout_ok:
            raise RuntimeError(checkout_out or "git checkout 失败")

        after_version = git_text(["rev-parse", "--short", "HEAD"], default="-")
        append_job_log(log_file, "")
        append_job_log(log_file, f"更新后版本: {after_version}")

        restart_ok, restart_msg = schedule_restart()

        append_job_log(log_file, "")
        append_job_log(log_file, "===== 自动重启 =====")
        append_job_log(log_file, f"结果: {'成功提交' if restart_ok else '失败'}")
        append_job_log(log_file, f"消息: {restart_msg or '-'}")

        if not restart_ok:
            raise RuntimeError(f"代码已更新，但自动重启失败：{restart_msg or '未知错误'}")

        info["running"] = False
        info["status"] = "更新完成，已提交自动重启"
        info["returncode"] = 0
        info["updated_at"] = now_str()

        append_job_log(log_file, "")
        append_job_log(log_file, f"===== 更新完成: {now_str()} =====")
        append_job_log(log_file, f"版本变化: {before_version} -> {after_version}")
        append_job_log(log_file, "面板会在几秒后自动重启，请稍后刷新页面。")

    except Exception as e:
        after_version = git_text(["rev-parse", "--short", "HEAD"], default="-")

        info["running"] = False
        info["status"] = "更新失败"
        info["returncode"] = 1
        info["error"] = str(e)
        info["updated_at"] = now_str()

        append_job_log(log_file, "")
        append_job_log(log_file, f"===== 更新失败: {now_str()} =====")
        append_job_log(log_file, f"目标版本: {version}")
        append_job_log(log_file, f"更新前版本: {before_version}")
        append_job_log(log_file, f"当前版本: {after_version}")
        append_job_log(log_file, f"错误: {e}")


def start_about_job(action, title, target, args=()):
    job_id = uuid.uuid4().hex
    log_file = about_job_log_file(job_id, action)

    ABOUT_JOBS[job_id] = {
        "id": job_id,
        "action": action,
        "title": title,
        "log_file": str(log_file),
        "running": True,
        "status": "准备中",
        "returncode": None,
        "error": "",
        "start_time": time.time(),
        "updated_at": now_str(),
    }

    th = threading.Thread(
        target=target,
        args=(job_id,) + tuple(args),
        daemon=True,
        name=f"fls-about-{action}-{job_id[:8]}",
    )
    th.start()

    return job_id


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
        可以选择某个版本进行后台更新，更新成功后面板会自动重启。
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
</style>

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


@bp.route("/about/refresh-log", methods=["POST"])
def about_refresh_log():
    """
    后台刷新更新日志。
    """
    if not git_available():
        body = """
<div class="card">
    <div class="card-title">刷新失败</div>
    <div class="help" style="color:#dc2626;">
        系统未安装 git。
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("刷新失败", "about", body)

    if not is_git_repo():
        body = f"""
<div class="card">
    <div class="card-title">刷新失败</div>
    <div class="help" style="color:#dc2626;">
        当前目录不是 Git 仓库：{h(BASE_DIR)}
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("刷新失败", "about", body)

    job_id = start_about_job(
        action="refresh-log",
        title="刷新更新日志",
        target=refresh_log_worker,
    )

    return redirect(
        url_for(
            "about.about_job_log",
            job_id=job_id,
            back="/about",
        )
    )


@bp.route("/about/update-version", methods=["POST"])
def about_update_version():
    """
    后台更新版本。

    点击更新后立即跳转日志页，避免页面卡住。
    """
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
        return layout("更新失败", "about", body)

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
        return layout("更新失败", "about", body)

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
        return layout("更新失败", "about", body)

    job_id = start_about_job(
        action="update-version",
        title=f"更新版本 {version[:12]}",
        target=update_version_worker,
        args=(version,),
    )

    return redirect(
        url_for(
            "about.about_job_log",
            job_id=job_id,
            back="/about",
        )
    )


@bp.route("/about/job-log/<job_id>")
def about_job_log(job_id):
    back_url = get_back_url("/about")
    info = ABOUT_JOBS.get(job_id)

    if not info:
        body = f"""
<div class="card">
    <div class="card-title">后台任务日志</div>
    <div class="help">
        任务记录不存在或面板已重启。<br>
        可以到日志管理中查找 about-*.log。
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/logs?back={h(back_url)}">查看日志管理</a>
</div>
"""
        return layout("后台任务日志", "about", body)

    body = f"""
<div class="card">
    <div class="card-title">后台任务日志：{h(info.get("title") or job_id)}</div>
    <div class="help">
        状态：<b id="aboutJobStatus">{h(info.get("status") or "-")}</b><br>
        日志文件：{h(info.get("log_file") or "-")}<br>
        更新时间：<span id="aboutJobUpdatedAt">{h(info.get("updated_at") or "-")}</span>
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回关于页</a>
    <a class="btn btn-blue" href="/logs?back={h(back_url)}">日志管理</a>
    <a class="btn btn-orange" href="/logfile/fls-manager-daemon.log?back={h(back_url)}">面板日志</a>
</div>

<pre class="log" id="log">加载中...</pre>
{log_controls()}

<script>
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function nearBottom(){{
    return document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 90;
}}

window.addEventListener("scroll", function(){{
    window.__FLS_LOG_NEAR_BOTTOM__ = nearBottom();
}}, {{passive:true}});

async function loadAboutJobLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/about/job-log/{h(job_id)}?lines=1600", {{cache:"no-store"}});
        const json = await res.json();

        document.getElementById("aboutJobStatus").textContent = json.status || "-";
        document.getElementById("aboutJobUpdatedAt").textContent = json.updated_at || "-";

        const text = json.log || "暂无日志";
        const old = window.__FLS_LOG_LAST_TEXT__ || "";
        const changed = text !== old;

        var logEl = document.getElementById("log");

        if(typeof flsRenderLogText === "function"){{
            flsRenderLogText(logEl, text);
        }}else{{
            logEl.textContent = text;
        }}

        window.__FLS_LOG_LAST_TEXT__ = text;

        if(changed){{
            if(wasNearBottom || window.__FLS_LOG_NEAR_BOTTOM__){{
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "none";
                window.scrollTo(0, document.documentElement.scrollHeight);
            }}else{{
                const afterHeight = document.documentElement.scrollHeight;
                window.scrollTo(0, beforeScroll + Math.max(afterHeight - beforeHeight, 0));
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "block";
            }}
        }}

        if(!json.running){{
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadAboutJobLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadAboutJobLog, 2000);
</script>
"""

    return layout("后台任务日志", "about", body)


@bp.route("/api/about/job-log/<job_id>")
def api_about_job_log(job_id):
    info = ABOUT_JOBS.get(job_id)

    if not info:
        return jsonify({
            "running": False,
            "status": "记录不存在或面板已重启",
            "updated_at": "-",
            "log": "任务记录不存在或面板已重启。请到日志管理中查找 about-*.log。",
        })

    log_file = info.get("log_file", "")
    lines = int(request.args.get("lines", "1200") or 1200)

    return jsonify({
        "running": bool(info.get("running")),
        "status": info.get("status") or "-",
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "updated_at": info.get("updated_at", ""),
        "log_file": log_file,
        "log": tail_file(log_file, lines),
    })