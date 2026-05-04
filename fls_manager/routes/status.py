import os
import sys
import shutil
import platform
import subprocess
from pathlib import Path

from flask import Blueprint

from ..paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ..config import get_host, get_port, fls_get_admin_token
from ..constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX
from ..ui.layout import layout
from ..utils import h

bp = Blueprint("status", __name__)


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


def cmd_output(cmd):
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=8
        )
        out = (r.stdout or "").strip()
        if not out:
            return ""
        return out.splitlines()[0].strip()
    except Exception:
        return ""


def runtime_items():
    ts_runner = shutil.which("tsx") or shutil.which("ts-node")
    ts_cmd = Path(ts_runner).name if ts_runner else "tsx / ts-node"

    items = [
        {
            "key": "python",
            "name": "Python",
            "suffix": ".py",
            "command": Path(sys.executable).name or "python",
            "version": sys.version.split()[0],
            "install_url": "/install/runtime/python",
        },
        {
            "key": "bash",
            "name": "Bash",
            "suffix": ".sh",
            "command": "bash",
            "version": cmd_output(["bash", "--version"]),
            "install_url": "/install/runtime/bash",
        },
        {
            "key": "node",
            "name": "Node.js",
            "suffix": ".js",
            "command": "node",
            "version": cmd_output(["node", "--version"]),
            "install_url": "/install/runtime/node",
        },
        {
            "key": "typescript",
            "name": "TypeScript",
            "suffix": ".ts",
            "command": ts_cmd,
            "version": cmd_output([ts_runner, "--version"]) if ts_runner else "",
            "install_url": "/install/runtime/typescript",
        },
        {
            "key": "php",
            "name": "PHP",
            "suffix": ".php",
            "command": "php",
            "version": cmd_output(["php", "-v"]),
            "install_url": "/install/runtime/php",
        },
        {
            "key": "ruby",
            "name": "Ruby",
            "suffix": ".rb",
            "command": "ruby",
            "version": cmd_output(["ruby", "-v"]),
            "install_url": "/install/runtime/ruby",
        },
        {
            "key": "perl",
            "name": "Perl",
            "suffix": ".pl",
            "command": "perl",
            "version": cmd_output(["perl", "-v"]),
            "install_url": "/install/runtime/perl",
        },
        {
            "key": "lua",
            "name": "Lua",
            "suffix": ".lua",
            "command": "lua",
            "version": cmd_output(["lua", "-v"]),
            "install_url": "/install/runtime/lua",
        },
        {
            "key": "java",
            "name": "Java",
            "suffix": ".jar",
            "command": "java",
            "version": cmd_output(["java", "-version"]),
            "install_url": "/install/runtime/java",
        },
    ]

    return items


@bp.route("/panel/status")
def panel_status():
    disk = shutil.disk_usage(str(BASE_DIR))

    data = [
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
        ("磁盘总量", fmt_bytes(disk.total)),
        ("磁盘已用", fmt_bytes(disk.used)),
        ("磁盘可用", fmt_bytes(disk.free)),
    ]

    rows = ""

    for k, v in data:
        rows += f"<tr><td><b>{h(k)}</b></td><td>{h(v)}</td></tr>"

    runtime_rows = ""

    for item in runtime_items():
        version = item.get("version") or "未安装"

        if item.get("version"):
            action = '<span class="badge green">已安装</span>'
        else:
            action = f'<a class="btn btn-primary" href="{h(item.get("install_url"))}">安装</a>'

        runtime_rows += f"""
<tr>
    <td><b>{h(item.get("name"))}</b></td>
    <td>{h(item.get("suffix"))}</td>
    <td>{h(item.get("command"))}</td>
    <td>{h(version)}</td>
    <td>{action}</td>
</tr>
"""

    body = f"""
<div class="card">
    <div class="card-title">环境状态</div>
    <div class="table-wrap">
        <table id="statusInfoTable"><tbody>{rows}</tbody></table>
    </div>
</div>

<div class="card">
    <div class="card-title">脚本运行环境</div>
    <div class="help">
        Linux / Termux 点击安装会调用系统包管理器安装。<br>
        Windows 会跳转官方下载页面。
    </div>
    <br>
    <div class="table-wrap">
        <table id="runtimeTable">
            <thead>
                <tr>
                    <th>环境</th>
                    <th>脚本类型</th>
                    <th>检测命令</th>
                    <th>版本</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{runtime_rows}</tbody>
        </table>
    </div>
</div>
"""
    return layout("环境状态", "status", body)
