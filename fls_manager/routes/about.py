import os
import re
import sys
import time
import uuid
import shutil
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests
from flask import Blueprint, request, redirect, url_for, jsonify

from ..ui.layout import layout
from ..ui.log_controls import log_controls
from ..utils import h, get_back_url, now_str, safe_name
from ..logs import tail_file
from ..paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ..constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX
from ..scheduler import reload_scheduler
from ..config import (
    panel_now,
    get_panel_timezone_text,
    get_timezone_offset_hours,
    set_panel_time_calibration,
    reset_panel_time_calibration,
)

bp = Blueprint("about", __name__)

ABOUT_JOBS = {}


# ============================================================
# Git / 版本信息
# ============================================================

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
    这里不主动 fetch。
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
    <button class="btn btn-orange" type="submit" onclick="return confirm('确定更新到该版本吗？更新任务将在后台执行。更新完成后需要手动重启面板。')">
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


# ============================================================
# 后台任务日志
# ============================================================

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

        info["running"] = False
        info["status"] = "更新完成"
        info["returncode"] = 0
        info["updated_at"] = now_str()

        append_job_log(log_file, "")
        append_job_log(log_file, f"===== 更新完成: {now_str()} =====")
        append_job_log(log_file, f"版本变化: {before_version} -> {after_version}")
        append_job_log(log_file, "已完成更新，请手动重启面板以生效。")

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


# ============================================================
# 面板控制：重启 / 停止
# ============================================================

def fls_control_script():
    """
    获取当前系统可用的 FLS 控制脚本。

    Windows：
        优先 BASE_DIR/fls.bat
        其次 BASE_DIR/fls.ps1

    Linux / Termux：
        优先 BASE_DIR/fls.sh
    """
    if os.name == "nt":
        candidates = [
            BASE_DIR / "fls.bat",
            BASE_DIR / "fls.ps1",
        ]
    else:
        candidates = [
            BASE_DIR / "fls.sh",
        ]

    for item in candidates:
        try:
            if item.exists() and item.is_file():
                return item
        except Exception:
            pass

    if os.name == "nt":
        return BASE_DIR / "fls.bat"

    return BASE_DIR / "fls.sh"


def build_fls_control_command(action):
    """
    构造面板控制命令。

    重点：
    - restart 不再依赖 fls.sh restart
    - 因为 fls.sh stop 可能识别不到当前面板进程
    - 这里会直接杀掉当前 Flask 进程 PID，再调用控制脚本 start
    - 这样可以保证端口释放

    action:
        restart / stop
    """
    action = str(action or "").strip().lower()

    if action not in ("restart", "stop"):
        raise ValueError(f"不支持的控制动作：{action}")

    script = fls_control_script()

    if not script.exists():
        raise FileNotFoundError(f"控制脚本不存在：{script}")

    current_pid = os.getpid()
    script_text = str(script)
    base_text = str(BASE_DIR)

    # ============================================================
    # Windows
    # ============================================================
    if os.name == "nt":
        suffix = script.suffix.lower()
        lower_name = script.name.lower()

        if suffix == ".bat":
            start_cmd = (
                f'Start-Process -FilePath "cmd.exe" '
                f'-ArgumentList "/c","\\"{script_text}\\" start" '
                f'-WorkingDirectory "{base_text}" '
                f'-WindowStyle Hidden'
            )
        elif suffix == ".ps1":
            start_cmd = (
                f'Start-Process -FilePath "powershell.exe" '
                f'-ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","\\"{script_text}\\"","start" '
                f'-WorkingDirectory "{base_text}" '
                f'-WindowStyle Hidden'
            )
        else:
            raise RuntimeError(f"Windows 不支持的控制脚本类型：{script}")

        if action == "restart":
            ps_cmd = f"""
Start-Sleep -Seconds 1
try {{
    Stop-Process -Id {current_pid} -Force -ErrorAction SilentlyContinue
}} catch {{}}
Start-Sleep -Seconds 4
{start_cmd}
"""
        else:
            ps_cmd = f"""
Start-Sleep -Seconds 1
try {{
    Stop-Process -Id {current_pid} -Force -ErrorAction SilentlyContinue
}} catch {{}}
"""

        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps_cmd,
        ]

    # ============================================================
    # Linux / Termux / macOS
    # ============================================================
    if action == "restart":
        shell_cmd = f"""
(
    echo ""
    echo "===== FLS 面板自重启开始: $(date '+%Y-%m-%d %H:%M:%S') ====="
    echo "当前面板 PID: {current_pid}"
    echo "控制脚本: {script_text}"

    echo "[FLS] 尝试优雅结束当前面板进程..."
    kill -TERM {current_pid} 2>/dev/null || true

    i=0
    while kill -0 {current_pid} 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 20 ]; then
            echo "[FLS] 当前面板进程仍未退出，执行 kill -9"
            kill -KILL {current_pid} 2>/dev/null || true
            break
        fi
        sleep 0.5
    done

    echo "[FLS] 等待端口释放..."
    sleep 3

    echo "[FLS] 调用控制脚本启动面板..."
    sh "{script_text}" start

    echo "===== FLS 面板自重启结束: $(date '+%Y-%m-%d %H:%M:%S') ====="
) >> "{LOG_DIR / 'fls-manager-daemon.log'}" 2>&1
"""
    else:
        shell_cmd = f"""
(
    echo ""
    echo "===== FLS 面板停止开始: $(date '+%Y-%m-%d %H:%M:%S') ====="
    echo "当前面板 PID: {current_pid}"
    echo "控制脚本: {script_text}"

    echo "[FLS] 尝试调用控制脚本 stop..."
    sh "{script_text}" stop || true

    echo "[FLS] 兜底结束当前面板进程..."
    kill -TERM {current_pid} 2>/dev/null || true

    sleep 1

    if kill -0 {current_pid} 2>/dev/null; then
        echo "[FLS] 当前面板进程仍未退出，执行 kill -9"
        kill -KILL {current_pid} 2>/dev/null || true
    fi

    echo "===== FLS 面板停止结束: $(date '+%Y-%m-%d %H:%M:%S') ====="
) >> "{LOG_DIR / 'fls-manager-daemon.log'}" 2>&1
"""

    return [
        "sh",
        "-c",
        shell_cmd,
    ]


def run_fls_control_later(action):
    """
    延迟执行面板控制命令。

    action:
        restart / stop

    restart 逻辑：
        1. 当前请求先返回页面
        2. 后台启动一个独立 shell
        3. shell 杀掉当前 Flask 进程
        4. 等待端口释放
        5. 调用 fls.sh start / fls.bat start / fls.ps1 start

    这样不会再出现 fls.sh stop 识别不到当前进程导致端口占用的问题。
    """
    time.sleep(1.2)

    log_file = LOG_DIR / "fls-manager-daemon.log"

    try:
        script = fls_control_script()
        cmd = build_fls_control_command(action)

        LOG_DIR.mkdir(parents=True, exist_ok=True)

        with open(log_file, "ab", buffering=0) as log_fp:
            log_fp.write(
                (
                    f"\n===== 调用 FLS 面板控制 {action}: {now_str()} =====\n"
                    f"系统类型: {os.name}\n"
                    f"当前面板 PID: {os.getpid()}\n"
                    f"脚本路径: {script}\n"
                    f"工作目录: {BASE_DIR}\n"
                    f"命令: {' '.join(str(x) for x in cmd)}\n"
                    f"说明: restart 会直接结束当前 Flask 进程，再调用控制脚本 start，避免端口未释放\n"
                    f"============================================================\n"
                ).encode("utf-8", errors="replace")
            )

            popen_kwargs = {
                "cwd": str(BASE_DIR),
                "stdout": log_fp,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
            }

            if os.name == "nt":
                creationflags = 0

                try:
                    creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
                except Exception:
                    pass

                try:
                    creationflags |= subprocess.DETACHED_PROCESS
                except Exception:
                    pass

                if creationflags:
                    popen_kwargs["creationflags"] = creationflags
            else:
                popen_kwargs["start_new_session"] = True

            subprocess.Popen(
                cmd,
                **popen_kwargs,
            )

    except Exception as e:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)

            with open(log_file, "ab", buffering=0) as log_fp:
                log_fp.write(
                    (
                        f"\n===== 调用 FLS 面板控制 {action} 失败: {now_str()} =====\n"
                        f"错误: {e}\n"
                        f"============================================================\n"
                    ).encode("utf-8", errors="replace")
                )
        except Exception:
            pass

        print(f"[About] 调用 FLS 面板控制 {action} 失败: {e}")


def delayed_restart_panel():
    """
    延迟重启面板。
    """
    run_fls_control_later("restart")


def delayed_stop_panel():
    """
    延迟停止面板。
    """
    run_fls_control_later("stop")


@bp.route("/about/restart-panel", methods=["GET", "POST"])
def about_restart_panel():
    """
    重启面板。

    GET：
        防止刷新 POST 地址出现 Method Not Allowed，直接返回关于页。

    POST：
        执行重启。
    """
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
    """
    停止面板。

    GET：
        防止刷新 POST 地址出现 Method Not Allowed，直接返回关于页。

    POST：
        执行停止。
    """
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


# ============================================================
# 面板时间校准
# ============================================================

def about_panel_time_text():
    """
    关于页显示用的面板当前时间。

    格式：
        2026 05-08 00:00:00
    """
    return panel_now().strftime("%Y %m-%d %H:%M:%S")


def utc_offset_options(selected=8):
    """
    生成 UTC 偏移选择项。

    范围：
        UTC-24 到 UTC+24
    """
    try:
        selected = int(selected)
    except Exception:
        selected = 8

    selected = max(-24, min(24, selected))

    options = ""

    for offset in range(-24, 25):
        text = f"UTC{offset:+d}"
        s = "selected" if offset == selected else ""
        options += f'<option value="{offset}" {s}>{h(text)}</option>'

    return options


def timezone_from_offset(offset):
    """
    根据 UTC 偏移小时数生成 timezone 对象。

    支持：
        -24 到 +24
    """
    try:
        offset = int(offset)
    except Exception:
        offset = 8

    offset = max(-24, min(24, offset))

    return timezone(
        timedelta(hours=offset),
        name=f"UTC{offset:+d}",
    )


def fetch_network_utc_time():
    """
    从网络 HTTP Date 头获取当前 UTC 时间。

    HTTP Date 标准是 GMT/UTC。
    """
    urls = [
        "https://www.baidu.com",
        "https://www.qq.com",
        "https://www.aliyun.com",
        "https://www.cloudflare.com",
    ]

    last_error = ""

    for url in urls:
        try:
            r = requests.head(
                url,
                timeout=8,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
            )

            date_header = r.headers.get("Date", "")

            if not date_header:
                r = requests.get(
                    url,
                    timeout=8,
                    stream=True,
                    headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
                )
                date_header = r.headers.get("Date", "")

            if not date_header:
                raise RuntimeError("响应头中没有 Date")

            dt = parsedate_to_datetime(date_header)

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.astimezone(timezone.utc)

        except Exception as e:
            last_error = f"{url}: {e}"

    raise RuntimeError(f"无法从网络获取时间：{last_error}")


def parse_custom_time_with_offset(value, offset):
    """
    解析自定义当前时间。

    格式必须是：
        yyyyMMddHHmmss

    示例：
        20260508121200

    该时间会按照用户选择的 UTC 偏移解释：
        UTC+8  => 20260508121200 表示 UTC+8 的 2026-05-08 12:12:00
        UTC+0  => 表示 UTC 的 2026-05-08 12:12:00
        UTC-5  => 表示 UTC-5 的 2026-05-08 12:12:00
    """
    value = str(value or "").strip()

    if not re.fullmatch(r"\d{14}", value):
        raise ValueError("自定义时间格式错误，正确格式例如：20260508121200")

    try:
        dt = datetime.strptime(value, "%Y%m%d%H%M%S")
    except Exception:
        raise ValueError("自定义时间无法解析，正确格式例如：20260508121200")

    tz = timezone_from_offset(offset)

    return dt.replace(tzinfo=tz)


def render_time_sync_result(title, ok, message, detail=""):
    color = "#18a058" if ok else "#dc2626"

    body = f"""
<div class="card">
    <div class="card-title">{h(title)}</div>
    <div class="help" style="color:{color};font-weight:800;">
        {h(message)}
    </div>
    <br>
    <div class="help">
        当前面板时间：<b>{h(about_panel_time_text())}</b><br>
        当前面板时区：<b>{h(get_panel_timezone_text())}</b><br>
        {h(detail or "")}
    </div>
    <br>
    <a class="btn btn-primary" href="/about">返回关于页</a>
    <a class="btn btn-gray" href="/">返回仪表盘</a>
</div>
"""
    return layout(title, "about", body)


@bp.route("/about/time-sync", methods=["POST"])
def about_time_sync():
    """
    面板时间校准

    支持：
    1. 自动校准北京时间
       - 网络标准时间 + UTC+8
       - 不修改系统时间
       - 写入 FLS 面板虚拟时间偏移

    2. 选择 UTC 偏移自动校准
       - 例如 UTC+7
       - 面板当前时间会变成 UTC+7 对应时间
       - Cron 也按 UTC+7 的面板虚拟时间计算

    3. 自定义当前时间
       - 格式 yyyyMMddHHmmss，例如 20260508121200
       - 按选择的 UTC 偏移解释
       - 不修改系统时间

    4. 重置时间偏移
       - 清除虚拟时间偏移，仅保留当前 UTC 时区设置

    校准成功后会 reload_scheduler()，让 Cron 下次执行时间重新计算。
    """
    mode = request.form.get("mode", "").strip()
    custom_time = request.form.get("custom_time", "").strip()
    utc_offset = request.form.get("utc_offset", "8").strip()

    try:
        if mode == "beijing":
            offset = 8
            tz = timezone_from_offset(offset)
            network_utc = fetch_network_utc_time()
            virtual_now = network_utc.astimezone(tz)

            result = set_panel_time_calibration(
                offset_hours=offset,
                virtual_now=virtual_now,
            )

            reload_scheduler()

            return render_time_sync_result(
                "北京时间校准完成",
                True,
                "已自动校准为北京时间，未修改系统时间",
                (
                    f"网络 UTC：{network_utc.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"北京时间：{virtual_now.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"面板时区：{result.get('timezone_text')}；"
                    f"面板时间偏移秒数：{result.get('panel_time_offset_seconds')}"
                ),
            )

        if mode == "utc_offset":
            offset = int(utc_offset)
            offset = max(-24, min(24, offset))
            tz = timezone_from_offset(offset)

            network_utc = fetch_network_utc_time()
            virtual_now = network_utc.astimezone(tz)

            result = set_panel_time_calibration(
                offset_hours=offset,
                virtual_now=virtual_now,
            )

            reload_scheduler()

            return render_time_sync_result(
                f"UTC{offset:+d} 时间校准完成",
                True,
                f"已按 UTC{offset:+d} 校准面板虚拟时间，未修改系统时间",
                (
                    f"网络 UTC：{network_utc.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"UTC{offset:+d}：{virtual_now.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"面板时区：{result.get('timezone_text')}；"
                    f"面板时间偏移秒数：{result.get('panel_time_offset_seconds')}"
                ),
            )

        if mode == "custom":
            offset = int(utc_offset)
            offset = max(-24, min(24, offset))

            virtual_now = parse_custom_time_with_offset(
                custom_time,
                offset,
            )

            result = set_panel_time_calibration(
                offset_hours=offset,
                virtual_now=virtual_now,
            )

            reload_scheduler()

            return render_time_sync_result(
                "自定义时间校准完成",
                True,
                f"已按 UTC{offset:+d} 应用自定义面板时间，未修改系统时间",
                (
                    f"输入时间：{custom_time}；"
                    f"UTC{offset:+d}：{virtual_now.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"换算 UTC：{virtual_now.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"面板时区：{result.get('timezone_text')}；"
                    f"面板时间偏移秒数：{result.get('panel_time_offset_seconds')}"
                ),
            )

        if mode == "reset":
            offset = get_timezone_offset_hours()

            result = reset_panel_time_calibration(offset)

            reload_scheduler()

            return render_time_sync_result(
                "面板时间偏移已重置",
                True,
                "已清除面板虚拟时间偏移，仅保留当前 UTC 时区设置",
                (
                    f"面板时区：{result.get('timezone_text')}；"
                    f"面板时间偏移秒数：{result.get('panel_time_offset_seconds')}"
                ),
            )

        return render_time_sync_result(
            "时间校准失败",
            False,
            "未知的时间校准方式",
        ), 400

    except Exception as e:
        return render_time_sync_result(
            "时间校准失败",
            False,
            str(e),
            "提示：当前方案不修改系统时间。请检查网络或输入格式。",
        ), 400


# ============================================================
# 关于页
# ============================================================

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
                    可选择 <code>UTC-24</code> 到 <code>UTC+24</code>。<br>
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


# ============================================================
# 版本刷新 / 更新
# ============================================================

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
    更新完成后不会自动重启，需要用户手动重启。
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