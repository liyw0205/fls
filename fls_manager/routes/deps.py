import json
import uuid
import time
import subprocess
import importlib
import importlib.metadata

from flask import Blueprint, request, redirect, url_for, jsonify

from ..paths import BASE_DIR, LOG_DIR
from ..state import DEPS_RUNNING
from ..utils import h, now_str, safe_name
from ..logs import tail_file
from ..ui.layout import layout

bp = Blueprint("deps", __name__)


def pip_cmd(args, timeout=600):
    import sys

    return subprocess.run(
        [sys.executable, "-m", "pip"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout
    )


def deps_log_file(install_id, package_name):
    safe_pkg = safe_name(package_name or "package")
    return LOG_DIR / f"deps-install-{safe_pkg}-{install_id}.log"


def is_deps_install_running(install_id):
    info = DEPS_RUNNING.get(install_id)

    if not info:
        return False

    if info.get("finished"):
        return False

    proc = info.get("process")

    if not proc:
        info["finished"] = True
        info["process"] = None
        info["log_fp"] = None
        return False

    if proc.poll() is None:
        return True

    fp = info.get("log_fp")

    if fp:
        try:
            fp.write(
                f"\n===== 安装结束: {now_str()}，退出码: {proc.returncode} =====\n".encode("utf-8")
            )
            fp.close()
        except Exception:
            pass

    info["finished"] = True
    info["process"] = None
    info["log_fp"] = None
    info["returncode"] = proc.returncode

    return False


def get_package_version(name):
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def refresh_dependency_cache():
    importlib.invalidate_caches()

    for name in [
        "socks",
        "sockshandler",
        "urllib3.contrib.socks",
        "setproctitle",
    ]:
        import sys
        sys.modules.pop(name, None)

    result = {
        "time": now_str(),
        "packages": {},
    }

    checks = [
        ("flask", "flask"),
        ("requests", "requests"),
        ("apscheduler", "apscheduler"),
        ("PySocks", "socks"),
        ("setproctitle", "setproctitle"),
    ]

    for pkg, module in checks:
        try:
            importlib.import_module(module)
            result["packages"][pkg] = get_package_version(pkg) or "已安装"
        except Exception as e:
            result["packages"][pkg] = f"不可用：{e}"

    return result


@bp.route("/deps")
def deps_page():
    package = request.args.get("package", "")

    try:
        result = pip_cmd(["list", "--format=json"])
        packages = json.loads(result.stdout)
    except Exception:
        packages = []

    rows = ""

    for p in packages:
        name = p.get("name", "")
        version = p.get("version", "")

        rows += f"""
<tr>
    <td>{h(name)}</td>
    <td>{h(version)}</td>
    <td>
        <a class="btn btn-red" href="/deps/uninstall?name={h(name)}" onclick="return confirm('确定卸载 {h(name)} 吗？')">卸载</a>
    </td>
</tr>
"""

    body = f"""
<div class="card">
    <div class="card-title">安装依赖</div>
    <form method="post" action="/deps/install">
        <input name="name" value="{h(package)}" placeholder="例如：requests 或 PySocks">
        <br><br>
        <button class="btn btn-primary" type="submit">安装并查看日志</button>
        <a class="btn btn-blue" href="/deps/refresh">刷新依赖检测</a>
    </form>
    <div class="help">
        安装依赖会进入实时日志页面。<br>
        如果安装失败，可以到日志管理查看 deps-install-*.log。
    </div>
</div>

<div class="card">
    <div class="card-title">已安装依赖</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>包名</th>
                    <th>版本</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows or '<tr><td colspan="3">暂无依赖</td></tr>'}</tbody>
        </table>
    </div>
</div>
"""
    return layout("依赖管理", "deps", body)


@bp.route("/deps/install", methods=["POST"])
def deps_install():
    import sys

    name = request.form.get("name", "").strip()

    if not name:
        return "依赖名不能为空", 400

    install_id = uuid.uuid4().hex
    log_file = deps_log_file(install_id, name)
    log_fp = open(log_file, "ab", buffering=0)

    header = (
        f"===== 安装依赖: {name} =====\n"
        f"时间: {now_str()}\n"
        f"Python: {sys.executable}\n"
        f"命令: {sys.executable} -m pip install {name}\n"
        f"日志文件: {log_file}\n"
        f"============================================================\n"
    )
    log_fp.write(header.encode("utf-8"))

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", name],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR)
        )

    except Exception as e:
        log_fp.write(f"启动安装失败: {e}\n".encode("utf-8"))
        log_fp.close()
        return f"启动安装失败：{h(e)}", 500

    DEPS_RUNNING[install_id] = {
        "process": proc,
        "package": name,
        "log_file": str(log_file),
        "log_fp": log_fp,
        "start_time": time.time(),
        "finished": False,
        "returncode": None,
    }

    return redirect(url_for("deps.deps_install_log", install_id=install_id))


@bp.route("/deps/install-log/<install_id>")
def deps_install_log(install_id):
    info = DEPS_RUNNING.get(install_id)
    package = info.get("package") if info else "未知"
    log_file = info.get("log_file") if info else ""
    running = is_deps_install_running(install_id)

    body = f"""
<div class="card">
    <div class="card-title">安装日志：{h(package)}</div>
    <div class="help">
        状态：<b id="installStatus">{"安装中" if running else "已结束"}</b><br>
        日志文件：{h(log_file or "当前进程已结束，无法定位日志")}
    </div>
    <br>
    <a class="btn btn-gray" href="/deps">返回依赖管理</a>
    <a class="btn btn-blue" href="/deps/refresh">刷新依赖</a>
</div>

<pre class="log" id="log">加载中...</pre>

<script>
async function loadLog(){{
    try {{
        const res = await fetch("/api/deps/install-log/{h(install_id)}?lines=1200", {{cache:"no-store"}});
        const json = await res.json();

        document.getElementById("log").textContent = json.log || "暂无日志";
        var logEl = document.getElementById("log");
        var logText = json.log || "暂无日志";

        if(typeof flsRenderLogText === "function"){{
            flsRenderLogText(logEl, logText);
        }}else{{
            logEl.textContent = logText;
        }}
        document.getElementById("installStatus").textContent = json.running ? "安装中" : "已结束";

        if(json.running){{
            window.scrollTo(0, document.documentElement.scrollHeight);
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) {{
    clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
    window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
}}

loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""
    return layout("安装日志", "deps", body)


@bp.route("/api/deps/install-log/<install_id>")
def api_deps_install_log(install_id):
    info = DEPS_RUNNING.get(install_id)

    if not info:
        return jsonify({
            "running": False,
            "log": "安装进程已结束或面板已重启，无法通过该 ID 继续追踪。请到日志管理查看 deps-install-*.log / system-install-*.log。",
        })

    running = is_deps_install_running(install_id)
    log_file = info.get("log_file", "")
    lines = int(request.args.get("lines", "800"))

    return jsonify({
        "running": running,
        "log": tail_file(log_file, lines),
    })


@bp.route("/deps/refresh")
def deps_refresh():
    result = refresh_dependency_cache()

    rows = ""

    for name, status in result["packages"].items():
        ok = not str(status).startswith("不可用")
        badge = '<span class="badge green">可用</span>' if ok else '<span class="badge red">异常</span>'

        rows += f"""
<tr>
    <td>{h(name)}</td>
    <td>{badge}</td>
    <td>{h(status)}</td>
</tr>
"""

    body = f"""
<div class="card">
    <div class="card-title">刷新依赖完成</div>
    <div class="help">刷新时间：{h(result["time"])}</div>
</div>

<div class="card">
    <div class="card-title">核心依赖检测</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>依赖</th>
                    <th>状态</th>
                    <th>版本 / 错误</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    <br>
    <a class="btn btn-gray" href="/deps">返回依赖管理</a>
</div>
"""
    return layout("刷新依赖", "deps", body)


@bp.route("/deps/uninstall")
def deps_uninstall():
    name = request.args.get("name", "").strip()

    if not name:
        return "依赖名不能为空", 400

    try:
        result = pip_cmd(["uninstall", "-y", name])
        output = result.stdout
    except Exception as e:
        output = str(e)

    body = f"""
<div class="card">
    <div class="card-title">卸载结果</div>
    <pre class="log">{h(output)}</pre>
    <a class="btn btn-gray" href="/deps">返回</a>
</div>
"""
    return layout("卸载依赖", "deps", body)
