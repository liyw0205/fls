import sys
import shutil
import subprocess
from pathlib import Path

from flask import Blueprint

from ..ui.layout import layout
from ..ui.components import table_card, page_header
from ..utils import h

bp = Blueprint("status", __name__)


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
            "suffix": ".py / .pyw",
            "command": Path(sys.executable).name or "python",
            "version": sys.version.split()[0],
            "install_url": "/install/runtime/python",
        },
        {
            "key": "bash",
            "name": "Bash",
            "suffix": ".sh / .bash",
            "command": "bash",
            "version": cmd_output(["bash", "--version"]),
            "install_url": "/install/runtime/bash",
        },
        {
            "key": "node",
            "name": "Node.js",
            "suffix": ".js / .mjs / .cjs",
            "command": "node",
            "version": cmd_output(["node", "--version"]),
            "install_url": "/install/runtime/node",
        },
        {
            "key": "typescript",
            "name": "TypeScript",
            "suffix": ".ts / .mts / .cts",
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
    runtime_rows = ""

    for item in runtime_items():
        version = item.get("version") or "未安装"

        if item.get("version"):
            action = '<span class="badge green status-badge status-success" role="status">已安装</span>'
        else:
            action = f'''
<form class="inline-form" method="post" action="{h(item.get("install_url"))}">
    <button class="btn btn-primary" type="submit">安装{h(item.get("name"))}</button>
</form>
'''

        runtime_rows += f"""
<tr>
    <td><b>{h(item.get("name"))}</b></td>
    <td>{h(item.get("suffix"))}</td>
    <td>{h(item.get("command"))}</td>
    <td>{h(version)}</td>
    <td>{action}</td>
</tr>
"""

    body = page_header(
        "面板状态",
        help_html="查看 task 可用的脚本运行器状态；安装操作会提交到对应的安装接口。",
    ) + table_card(
        "运行器状态",
        ["环境", "脚本类型", "检测命令", "版本", "操作"],
        runtime_rows,
        help_html="""
        这里显示 task 可用的脚本运行器状态。<br>
        Linux / Termux 点击安装会调用系统包管理器安装。<br>
        Windows 会跳转官方下载页面。
        """,
        table_id="runtimeTable",
    )

    return layout("面板状态", "status", body)
