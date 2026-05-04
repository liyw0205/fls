import os
import sys
import shutil
import platform

from flask import Blueprint

from ..models import load_tasks
from ..task_runner import is_running
from ..ui.layout import layout
from ..utils import h
from ..paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ..config import get_host, get_port, fls_get_admin_token
from ..constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX

bp = Blueprint("dashboard", __name__)


def fmt_bytes(n):
    try:
        n = float(n)
    except Exception:
        return "-"

    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0

    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1

    return f"{n:.1f} {units[i]}"


@bp.route("/")
def dashboard():
    tasks = load_tasks()

    total = len(tasks)
    enabled = sum(1 for t in tasks if t.get("enabled", True))
    running = sum(1 for t in tasks if is_running(t["id"]))
    cron_count = sum(1 for t in tasks if str(t.get("cron", "")).strip())
    run_total = sum(int(t.get("run_count", 0)) for t in tasks)

    try:
        disk = shutil.disk_usage(str(BASE_DIR))
        disk_total = fmt_bytes(disk.total)
        disk_used = fmt_bytes(disk.used)
        disk_free = fmt_bytes(disk.free)
    except Exception:
        disk_total = disk_used = disk_free = "-"

    env_data = [
        ("系统", platform.platform()),
        ("Python", sys.version.split()[0]),
        ("工作目录", str(BASE_DIR)),
        ("数据目录", str(DATA_DIR)),
        ("日志目录", str(LOG_DIR)),
        ("脚本目录", str(SCRIPT_DIR)),
        ("主进程 PID", os.getpid()),
        ("主进程名", MAIN_PROCESS_NAME),
        ("任务进程名前缀", TASK_PROCESS_PREFIX),
        ("Host / Port", f"{get_host()}:{get_port()}"),
        ("鉴权", "已开启" if fls_get_admin_token() else "未开启"),
        ("磁盘总量", disk_total),
        ("磁盘已用", disk_used),
        ("磁盘可用", disk_free),
    ]

    env_rows = ""
    for k, v in env_data:
        env_rows += f"""
<tr>
    <td><b>{h(k)}</b></td>
    <td>{h(v)}</td>
</tr>
"""

    body = f"""
<div class="grid">
    <div class="stat">
        <div class="label">任务总数</div>
        <div class="num">{total}</div>
    </div>
    <div class="stat">
        <div class="label">已启用</div>
        <div class="num" style="color:#18a058;">{enabled}</div>
    </div>
    <div class="stat">
        <div class="label">运行中</div>
        <div class="num" style="color:#2563eb;">{running}</div>
    </div>
    <div class="stat">
        <div class="label">定时任务</div>
        <div class="num" style="color:#f59e0b;">{cron_count}</div>
    </div>
    <div class="stat">
        <div class="label">累计运行次数</div>
        <div class="num" style="color:#7c3aed;">{run_total}</div>
    </div>
</div>

<div class="card">
    <div class="card-title">环境状态</div>
    <div class="table-wrap">
        <table>
            <tbody>{env_rows}</tbody>
        </table>
    </div>
</div>
"""
    return layout("仪表盘", "dashboard", body)