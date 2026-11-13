import sys
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint

from ..ui.layout import layout
from ..ui.components import table_card, page_header
from ..utils import h
from ..command import PYTHON_BIN, BASH_BIN, NODE_BIN

bp = Blueprint("status", __name__)
RUNTIME_PROBE_TIMEOUT = 3
RUNTIME_PROBE_WORKERS = 4


def probe_runtime(cmd):
    if not cmd:
        return {"status": "missing", "version": "", "detail": "未找到 tsx / ts-node"}

    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=RUNTIME_PROBE_TIMEOUT,
        )
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            return {
                "status": "error", "version": "",
                "detail": f"版本检测退出码 {r.returncode}：{out[:240] or '无输出'}",
            }
        if not out:
            return {"status": "error", "version": "", "detail": "版本检测未返回版本信息"}
        return {"status": "available", "version": out.splitlines()[0].strip(), "detail": ""}
    except FileNotFoundError as e:
        if shutil.which(cmd[0]) is not None:
            return {"status": "error", "version": "", "detail": f"运行器文件存在，但启动失败：{str(e)[:500]}"}
        return {"status": "missing", "version": "", "detail": f"未找到可执行文件：{cmd[0]}"}
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout", "version": "",
            "detail": f"版本检测超过 {RUNTIME_PROBE_TIMEOUT} 秒，请检查运行器后重新检测",
        }
    except OSError as e:
        return {"status": "error", "version": "", "detail": f"无法执行版本检测：{str(e)[:240]}"}


def runtime_items():
    ts_runner = shutil.which("tsx") or shutil.which("ts-node")
    ts_cmd = ts_runner or "tsx / ts-node"

    items = [
        {
            "key": "python",
            "name": "Python",
            "suffix": ".py / .pyw",
            "command": PYTHON_BIN,
            "status": "available",
            "version": sys.version.split()[0],
            "install_url": "/install/runtime/python",
        },
        {
            "key": "bash",
            "name": "Bash",
            "suffix": ".sh / .bash",
            "command": BASH_BIN,
            "probe": [BASH_BIN, "--version"],
            "install_url": "/install/runtime/bash",
        },
        {
            "key": "node",
            "name": "Node.js",
            "suffix": ".js / .mjs / .cjs",
            "command": NODE_BIN,
            "probe": [NODE_BIN, "--version"],
            "install_url": "/install/runtime/node",
        },
        {
            "key": "typescript",
            "name": "TypeScript",
            "suffix": ".ts / .mts / .cts",
            "command": ts_cmd,
            "probe": [ts_runner, "--version"] if ts_runner else None,
            "install_url": "/install/runtime/typescript",
        },
        {
            "key": "php",
            "name": "PHP",
            "suffix": ".php",
            "command": "php",
            "probe": ["php", "-v"],
            "install_url": "/install/runtime/php",
        },
        {
            "key": "ruby",
            "name": "Ruby",
            "suffix": ".rb",
            "command": "ruby",
            "probe": ["ruby", "-v"],
            "install_url": "/install/runtime/ruby",
        },
        {
            "key": "perl",
            "name": "Perl",
            "suffix": ".pl",
            "command": "perl",
            "probe": ["perl", "-v"],
            "install_url": "/install/runtime/perl",
        },
        {
            "key": "lua",
            "name": "Lua",
            "suffix": ".lua",
            "command": "lua",
            "probe": ["lua", "-v"],
            "install_url": "/install/runtime/lua",
        },
        {
            "key": "java",
            "name": "Java",
            "suffix": ".jar",
            "command": "java",
            "probe": ["java", "-version"],
            "install_url": "/install/runtime/java",
        },
    ]

    if PYTHON_BIN != sys.executable:
        items[0]["probe"] = [PYTHON_BIN, "--version"]

    pending = [item for item in items if "probe" in item]
    # Limit simultaneous interpreter startups while avoiding serial timeout waits.
    with ThreadPoolExecutor(max_workers=RUNTIME_PROBE_WORKERS) as executor:
        results = executor.map(probe_runtime, [item["probe"] for item in pending])
        for item, result in zip(pending, results):
            item.pop("probe")
            item.update(result)

    return items


@bp.route("/panel/status")
def panel_status():
    runtime_rows = ""

    for item in runtime_items():
        status = item["status"]
        version = item.get("version") or item.get("detail") or "未安装"

        if status == "available":
            action = '<span class="badge green status-badge status-success" role="status">已安装</span>'
        elif status == "missing":
            action = f'''
<form class="inline-form" method="post" action="{h(item.get("install_url"))}">
    <button class="btn btn-primary" type="submit">安装{h(item.get("name"))}</button>
</form>
'''
        else:
            label = "检测超时" if status == "timeout" else "检测失败"
            action = f'<span class="badge orange status-badge status-warning" role="status">{label}</span>'

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
        help_html="查看任务使用的脚本运行器。检测失败或超时不代表未安装，请检查原因后重新检测。",
        actions_html='<a class="btn btn-blue" href="/panel/status">重新检测</a>',
    ) + table_card(
        "运行器状态",
        ["环境", "脚本类型", "检测命令", "版本 / 检测结果", "操作"],
        runtime_rows,
        help_html="""
        版本检测失败时，请根据检测结果检查运行器后重试。<br>
        Linux / Termux 点击安装会调用系统包管理器安装。<br>
        Windows 会跳转官方下载页面。
        """,
        table_id="runtimeTable",
    )

    return layout("面板状态", "status", body)
