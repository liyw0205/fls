import os
import re
import time
import uuid
import shutil
import threading
import subprocess
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests

from ...ui.layout import layout
from ...utils import h, now_str, safe_name
from ...logs import tail_file
from ...paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ...constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX
from ...scheduler import reload_scheduler
from ...config import (
    panel_now,
    get_panel_timezone_text,
    get_timezone_offset_hours,
    set_panel_time_calibration,
    reset_panel_time_calibration,
)
from .state import ABOUT_JOBS


# ============================================================
# Git / 版本信息
# ============================================================

def git_available():
    return bool(shutil.which("git"))


def run_git(args, timeout=30):
    """
    在 BASE_DIR 下执行 git 命令。
    返回：ok, output
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
    注意：这里不主动 fetch。
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
    action = str(action or "").strip().lower()

    if action not in ("restart", "stop"):
        raise ValueError(f"不支持的控制动作：{action}")

    script = fls_control_script()

    if not script.exists():
        raise FileNotFoundError(f"控制脚本不存在：{script}")

    current_pid = os.getpid()
    script_text = str(script)
    base_text = str(BASE_DIR)

    if os.name == "nt":
        suffix = script.suffix.lower()

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
    run_fls_control_later("restart")


def delayed_stop_panel():
    run_fls_control_later("stop")


# ============================================================
# 面板时间校准
# ============================================================

def about_panel_time_text():
    return panel_now().strftime("%Y %m-%d %H:%M:%S")


def utc_offset_options(selected=8):
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