import json
import uuid
import time
import subprocess
import importlib
import importlib.metadata
import os
import re
import stat
from pathlib import Path

from flask import Blueprint, request, redirect, url_for, jsonify

from ..paths import BASE_DIR, LOG_DIR
from ..state import DEPS_RUNNING
from ..utils import h, now_str, safe_name, get_back_url, prune_completed_records
from ..logs import tail_file
from ..install_records import get_install_job, list_install_jobs, persist_install_job
from ..ui.layout import layout
from ..ui.components import page_header_card, page_header, table_card, empty_table_row

bp = Blueprint("deps", __name__)


def find_install_log_context(install_id):
    """Recover a lost install's kind/name from its bounded log filename."""
    install_id = str(install_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", install_id):
        return None

    log_dir = Path(LOG_DIR).resolve()
    marker = f"-{install_id}.log"
    candidates = (
        ("dependency", "deps-install-"),
        ("runtime", "system-install-"),
    )

    for kind, prefix in candidates:
        pattern = f"{prefix}*-{install_id}.log"
        try:
            matches = sorted(log_dir.glob(pattern))
        except OSError:
            continue

        for path in matches:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve()
                if resolved != log_dir and not str(resolved).startswith(str(log_dir) + os.sep):
                    continue
                name = path.name
                if not name.startswith(prefix) or not name.endswith(marker):
                    continue
                package = name[len(prefix):-len(marker)].strip("-").strip()
                if package:
                    return {
                        "kind": kind,
                        "package": package,
                        "log_file": str(resolved),
                    }
            except OSError:
                continue

    return None


def install_recovery_action(context):
    if not context:
        return ""

    package = str(context.get("package") or "").strip()
    kind = str(context.get("kind") or "").strip()
    if not package or kind not in ("dependency", "runtime"):
        return ""

    if kind == "runtime":
        return (
            f'<form class="inline-form" method="post" action="/install/runtime/{h(package)}">'
            '<button class="btn btn-orange" type="submit">重新安装运行器</button>'
            '</form>'
        )

    return (
        '<form class="inline-form" method="post" action="/deps/install">'
        f'<input type="hidden" name="name" value="{h(package)}">'
        '<button class="btn btn-orange" type="submit">重新安装依赖</button>'
        '</form>'
    )


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


def safe_install_log_file(info, install_id):
    raw_path = info.get("log_file") if isinstance(info, dict) else ""
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, ("日志缺失", "安装记录没有日志路径，未读取未知文件。", 404)
    candidate = Path(raw_path)
    try:
        log_dir = Path(LOG_DIR).resolve()
        if (
            not candidate.is_absolute()
            or candidate.is_symlink()
            or candidate.parent.resolve(strict=False) != candidate.parent
        ):
            return None, ("日志边界错误", "安装日志路径不在受控日志目录内，未读取该文件。", 500)
        actual = candidate.resolve(strict=False)
        if actual.parent != log_dir or str(install_id) not in actual.name:
            return None, ("日志边界错误", "安装日志路径不在受控日志目录内，未读取该文件。", 500)
        if not actual.exists():
            return None, ("日志不存在", "安装日志尚未生成或已被清理，请返回安装历史后重试。", 404)
        if not stat.S_ISREG(actual.stat().st_mode):
            return None, ("日志类型错误", "安装日志不是普通文件，未读取该路径。", 500)
        with actual.open("rb") as handle:
            handle.read(1)
    except FileNotFoundError:
        return None, ("日志不存在", "安装日志尚未生成或已被清理，请返回安装历史后重试。", 404)
    except OSError as exc:
        return None, ("日志不可读", f"安装日志无法读取：{exc}。请修复日志权限后重试。", 500)
    return actual, None


def install_request_id(raw):
    value = str(raw or "").strip() or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("安装请求标识无效，请刷新页面后重试")
    return value


def _persist_dependency_state(info):
    if not info.get("id") or info.get("kind") not in ("dependency", "runtime"):
        return
    info["updated_at"] = now_str()
    try:
        persist_install_job(info)
    except Exception as exc:
        info["running"] = False
        info["finished"] = True
        info["status"] = "unknown"
        info["error"] = "；".join(filter(None, (
            info.get("error"), f"安装状态保存失败：{exc}",
        )))


def is_deps_install_running(install_id):
    info = DEPS_RUNNING.get(install_id)

    if not info:
        return False

    if info.get("finished"):
        return False

    proc = info.get("process")

    returncode = proc.poll() if proc is not None else None
    if proc is not None and returncode is None:
        return True

    fp = info.get("log_fp")

    if fp:
        try:
            if returncode is not None:
                fp.write(
                    f"\n===== 安装结束: {now_str()}，退出码: {returncode} =====\n".encode("utf-8")
                )
        except Exception:
            pass
        finally:
            try:
                fp.close()
            except Exception:
                pass

    info["finished"] = True
    info["process"] = None
    info["log_fp"] = None
    info["returncode"] = returncode
    info["status"] = "succeeded" if returncode == 0 else "failed" if returncode is not None else "unknown"
    _persist_dependency_state(info)

    return False


def deps_install_result(install_id):
    running = is_deps_install_running(install_id)
    info = DEPS_RUNNING.get(install_id)
    if not info:
        info = get_install_job(install_id)
    returncode = info.get("returncode") if info else None
    error = info.get("error", "") if info else ""

    if not info:
        context = find_install_log_context(install_id)
        if context:
            status, label = "unknown", "结果未知"
            error = (
                "面板重启后无法确认安装进程结果；已保留日志，请检查日志后重新安装。"
            )
        else:
            status, label = "missing", "无法追踪"
            error = "安装记录不存在或面板已重启，请到日志管理查看 deps-install-*.log / system-install-*.log。"
    elif running:
        status, label = "running", "安装中"
    elif info and info.get("status") == "unknown":
        status, label = "unknown", "结果未知"
        error = error or "无法取得安装进程的退出码，请查看安装日志并明确重试。"
    elif info and info.get("status") == "failed" and returncode is not None:
        status, label = "failed", "安装失败"
        error = error or f"安装进程退出码为 {returncode}，请查看日志中的错误信息。"
    elif error:
        status, label = "start_failed", "启动失败"
    elif returncode == 0:
        status, label = "succeeded", "安装成功"
    elif returncode is not None:
        status, label = "failed", "安装失败"
        error = f"安装进程退出码为 {returncode}，请查看日志中的错误信息。"
    else:
        # Older records may have no exit code; completion alone does not prove success.
        status, label = "unknown", "结果未知"
        error = "无法取得安装进程的退出码，请查看安装日志并刷新依赖检测结果。"

    return {
        "running": running,
        "status": status,
        "status_label": label,
        "returncode": returncode,
        "error": error,
    }


def install_api_payload(install_id, log_url):
    result = deps_install_result(install_id)
    return {
        "ok": True,
        "request_id": install_id,
        "install_id": install_id,
        "log_url": log_url,
        **result,
    }


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
        <div class="row-actions" aria-label="依赖 {h(name)} 行操作">
            <div class="row-actions-danger">
                <form class="inline-form" method="post" action="/deps/uninstall">
                    <input type="hidden" name="name" value="{h(name)}">
                    <button class="btn btn-red" type="submit" onclick="return confirm('确定卸载 {h(name)} 吗？')">卸载依赖</button>
                </form>
            </div>
        </div>
    </td>
</tr>
"""

    installed_table = table_card(
        "已安装依赖",
        ["包名", "版本", "操作"],
        rows or empty_table_row(3, "暂无依赖", '<a class="btn btn-primary" href="/deps#deps-install">安装依赖</a>'),
    )

    header = page_header(
        "依赖管理",
        help_html="安装依赖、查看已安装包并刷新运行器检测。",
    )
    body = f"""
{header}
<nav class="fls-section-nav" aria-label="依赖管理区块导航">
    <span class="fls-section-nav-label">依赖管理</span>
    <a href="#deps-install">安装依赖</a>
    <a href="#deps-installed">已安装依赖</a>
</nav>

<section class="section fls-section" id="deps-install">
<div class="card">
    <h2 class="section-title">安装依赖</h2>
    <form method="post" action="/deps/install">
        <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
        <input name="name" value="{h(package)}" placeholder="例如：requests 或 PySocks" aria-label="依赖名">
        <br><br>
        <button class="btn btn-primary" type="submit">安装并查看日志</button>
    </form>
        <form class="inline-form" method="post" action="/deps/refresh">
            <button class="btn btn-blue" type="submit">刷新依赖检测结果</button>
        </form>
    <div class="help">
        安装依赖会进入实时日志页面。<br>
        如果安装失败，可以到日志管理查看 deps-install-*.log。
    </div>
    <a class="btn btn-gray" href="/deps/install-history">安装历史</a>
</div>
</section>

<section class="section fls-section" id="deps-installed">
{installed_table}
</section>
"""
    return layout("依赖管理", "deps", body)


@bp.route("/deps/install", methods=["POST"])
def deps_install():
    import sys

    data = request.get_json(silent=True) if request.is_json else request.form
    data = data if isinstance(data, dict) else request.form
    name = str(data.get("name", "") or "").strip()

    if not name:
        return "依赖名不能为空。下一步：填写包名后重试", 400

    try:
        install_id = install_request_id(data.get("request_id"))
    except ValueError as exc:
        return str(exc), 400

    try:
        previous = get_install_job(install_id, DEPS_RUNNING)
    except Exception as exc:
        return f"安装回执无法读取，未启动安装：{exc}", 500
    if previous:
        if previous.get("kind") != "dependency" or previous.get("package") != name:
            return "安装请求标识已绑定其他依赖，请重新打开依赖页面后重试", 409
        return redirect(url_for("deps.deps_install_log", install_id=install_id, back="/deps"))

    log_file = deps_log_file(install_id, name)
    try:
        log_fp = open(log_file, "ab", buffering=0)
    except Exception as exc:
        info = {
            "id": install_id, "request_id": install_id, "kind": "dependency", "package": name,
            "log_file": str(log_file), "running": False, "finished": True,
            "status": "failed", "returncode": None,
            "error": f"打开安装日志失败：{exc}", "updated_at": now_str(),
        }
        try:
            persist_install_job(info)
        except Exception as persist_error:
            info["error"] += f"；保存失败：{persist_error}"
        DEPS_RUNNING[install_id] = info
        return redirect(url_for("deps.deps_install_log", install_id=install_id, back="/deps"))

    header = (
        f"===== 安装依赖: {name} =====\n"
        f"时间: {now_str()}\n"
        f"Python: {sys.executable}\n"
        f"命令: {sys.executable} -m pip install {name}\n"
        f"日志文件: {log_file}\n"
        f"============================================================\n"
    )
    log_fp.write(header.encode("utf-8"))

    prune_completed_records(DEPS_RUNNING, finished_key="finished")
    info = {
        "id": install_id,
        "request_id": install_id,
        "kind": "dependency",
        "process": None,
        "package": name,
        "log_file": str(log_file),
        "log_fp": log_fp,
        "start_time": time.time(),
        "finished": False,
        "running": True,
        "status": "prepared",
        "returncode": None,
        "updated_at": now_str(),
    }
    DEPS_RUNNING[install_id] = info
    try:
        persist_install_job(info)
    except Exception as exc:
        info.update(
            running=False,
            finished=True,
            status="unknown",
            error=f"安装已准备但状态保存失败：{exc}",
            updated_at=now_str(),
        )
        log_fp.write((info["error"] + "\n").encode("utf-8", errors="replace"))
        log_fp.close()
        return redirect(url_for("deps.deps_install_log", install_id=install_id, back="/deps"))

    try:
        info["process"] = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", name],
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR)
        )

    except Exception as e:
        log_fp.write(f"启动安装失败: {e}\n".encode("utf-8"))
        log_fp.close()
        info.update(
            finished=True,
            log_fp=None,
            running=False,
            status="failed",
            error=f"启动安装失败：{e}。请检查 Python、pip 和日志目录权限后重试。",
            updated_at=now_str(),
        )
        _persist_dependency_state(info)

    if info.get("process") is not None:
        info["status"] = "running"
        try:
            persist_install_job(info)
        except Exception as exc:
            info["error"] = f"安装进程已启动但状态保存失败：{exc}"

    return redirect(url_for("deps.deps_install_log", install_id=install_id, back="/deps"))


@bp.route("/api/deps/install", methods=["POST"])
def api_deps_install():
    request_id = ""
    data = request.get_json(silent=True) if request.is_json else request.form
    if isinstance(data, dict):
        request_id = str(data.get("request_id") or "").strip()
    try:
        result = deps_install()
    except Exception as exc:
        return jsonify(ok=False, request_id=request_id, msg=f"安装提交失败：{exc}"), 500
    if getattr(result, "status_code", 0) in (301, 302, 303, 307, 308):
        location = result.headers.get("Location", "")
        install_id = location.split("/deps/install-log/", 1)[-1].split("?", 1)[0]
        return jsonify(install_api_payload(install_id, location)), 200
    if isinstance(result, tuple):
        body, status = result
        return jsonify(ok=False, request_id=request_id, msg=str(body)), status
    return jsonify(ok=False, request_id=request_id, msg="安装提交响应无效"), 500


@bp.route("/deps/install-log/<install_id>")
def deps_install_log(install_id):
    back_url = get_back_url("/deps")
    info = DEPS_RUNNING.get(install_id)
    if not info:
        try:
            info = get_install_job(install_id)
        except Exception:
            info = None
    context = find_install_log_context(install_id) if not info else None
    package = info.get("package") if info else (context or {}).get("package", "未知")
    log_file = info.get("log_file") if info else (context or {}).get("log_file", "")
    result = deps_install_result(install_id)
    recovery_action = ""
    if not result.get("running") and result.get("status") in (
        "unknown", "missing", "failed", "start_failed"
    ):
        recovery_action = install_recovery_action(
            context
            or {
                "kind": info.get("kind", "dependency") if info else "",
                "package": package,
            }
        )

    log_path, log_failure = safe_install_log_file(info, install_id) if info else (None, None)
    log_notice = ""
    if log_failure:
        _title, message, _status = log_failure
        log_notice = f'<div class="notice red" role="alert">{h(message)}</div>'
        log_file = ""
    elif log_path:
        log_file = str(log_path)

    header_card = page_header_card(
        f"安装日志：{package}",
        help_html=f"""
        状态：<b id="installStatus" role="status" aria-live="polite">{h(result["status_label"])}</b><br>
        退出码：<span id="installReturncode">{h(result["returncode"] if result["returncode"] is not None else "-")}</span><br>
        日志文件：{h(log_file or "无法定位日志")}
        <div id="installError" role="status" aria-live="polite"{'' if result["error"] else ' hidden'}>{h(result["error"])}</div>
""",
        actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<form class="inline-form" method="post" action="/deps/refresh">
    <button class="btn btn-blue" type="submit">刷新依赖检测结果</button>
</form>
{recovery_action}
""",
    )

    body = f"""
{header_card}
{log_notice}

<pre class="log" id="log">加载中...</pre>
<div id="installPollError" class="help" role="status" aria-live="polite" hidden></div>
<button id="installPollRetry" class="btn btn-primary" type="button" hidden>立即重试读取日志</button>
<a id="installPollLogin" class="btn btn-primary" hidden>重新登录</a>

<script>
(function(){{
const installPollError = document.getElementById("installPollError");
const installPollRetry = document.getElementById("installPollRetry");
const installPollLogin = document.getElementById("installPollLogin");
let installLogRequestPending = false;
let installLogHasLoaded = false;

function stopInstallLogPolling(){{
    if(window.__FLS_ACTIVE_LOG_INTERVAL__){{
        clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
        window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
    }}
}}

function startInstallLogPolling(){{
    if(!window.__FLS_ACTIVE_LOG_INTERVAL__){{
        window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
    }}
}}

async function loadLog(){{
    if(installLogRequestPending || document.getElementById("installPollError") !== installPollError) return;
    installLogRequestPending = true;
    try {{
        const res = await fetch("/api/deps/install-log/{h(install_id)}?lines=1200", {{cache:"no-store"}});
        const json = await flsReadJsonResponse(res);
        if(document.getElementById("installPollError") !== installPollError) return;
        if(!json || typeof json.running !== "boolean" || typeof json.log !== "string" || json.ok === false){{
            const error = new Error((json && (json.msg || json.error)) || "安装日志响应无效");
            error.status = res.status;
            throw error;
        }}

        var logEl = document.getElementById("log");
        var logText = json.log || "暂无日志";

        if(typeof flsRenderLogText === "function"){{
            flsRenderLogText(logEl, logText);
        }}else{{
            logEl.textContent = logText;
        }}
        document.getElementById("installStatus").textContent = json.status_label || (json.running ? "安装中" : "结果未知");
        document.getElementById("installReturncode").textContent = json.returncode == null ? "-" : String(json.returncode);
        const errorEl = document.getElementById("installError");
        errorEl.textContent = json.error || "";
        errorEl.hidden = !json.error;
        installLogHasLoaded = true;
        installPollError.textContent = "";
        installPollError.hidden = true;
        installPollRetry.hidden = true;
        installPollLogin.hidden = true;

        if(json.running){{
            window.scrollTo(0, document.documentElement.scrollHeight);
            startInstallLogPolling();
        }}else{{
            stopInstallLogPolling();
        }}
    }} catch(e) {{
        if(document.getElementById("installPollError") !== installPollError) return;
        if(!installLogHasLoaded){{
            document.getElementById("log").textContent = "暂未读取到安装日志";
        }}
        const denied = e && (e.status === 401 || e.status === 403);
        const message = e && e.message ? e.message : String(e);
        installPollError.textContent = denied
            ? "日志读取失败：" + message + "。现有日志和安装状态已保留，请重新登录后继续读取。"
            : "日志读取失败：" + message + "。现有日志和安装状态已保留，可手动重试。";
        installPollError.hidden = false;
        installPollRetry.hidden = denied;
        installPollLogin.hidden = !denied;
        if(denied){{
            installPollLogin.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        }}
        stopInstallLogPolling();
    }}finally{{
        installLogRequestPending = false;
    }}
}}

installPollRetry.addEventListener("click", loadLog);
stopInstallLogPolling();

loadLog();
}})();
</script>
"""
    return layout("安装日志", "deps", body)


@bp.route("/api/deps/install-log/<install_id>")
def api_deps_install_log(install_id):
    info = DEPS_RUNNING.get(install_id)
    if not info:
        try:
            info = get_install_job(install_id)
        except Exception as exc:
            return jsonify(
                ok=False,
                running=False,
                status="unreadable",
                status_label="记录不可读",
                returncode=None,
                error=f"安装回执无法读取，未覆盖原文件：{exc}",
                log="",
            ), 500
    context = find_install_log_context(install_id) if not info else None

    if not info:
        result = deps_install_result(install_id)
        if context:
            result["package"] = context["package"]
            result["recovery"] = {
                "kind": context["kind"],
                "package": context["package"],
            }

            try:
                lines = int(request.args.get("lines", "800"))
            except (TypeError, ValueError):
                return jsonify({
                    "ok": False,
                    "msg": "lines 必须为整数",
                    "running": False,
                    "log": "",
                }), 400

            result["log"] = tail_file(context["log_file"], lines)
            return jsonify(result)

        return jsonify({
            **result,
            "log": "安装进程已结束或面板已重启，无法通过该 ID 继续追踪。请到日志管理查看 deps-install-*.log / system-install-*.log。",
        })

    try:
        lines = int(request.args.get("lines", "800"))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "msg": "lines 必须为整数",
            "running": False,
            "log": "",
        }), 400

    result = deps_install_result(install_id)
    log_file, log_failure = safe_install_log_file(info, install_id)
    if log_failure:
        _title, message, failure_status = log_failure
        payload = {
            **result,
            "ok": False,
            "log": "",
            "error": "；".join(filter(None, (result.get("error"), message))),
            "log_status": failure_status,
            "recovery_url": "/deps/install-history",
        }
        return jsonify(payload), failure_status

    log_file = str(log_file)
    log = tail_file(log_file, lines)
    if str(log).startswith("读取日志失败:"):
        return jsonify({
            **result,
            "ok": False,
            "log": "",
            "error": str(log),
            "recovery_url": "/deps/install-history",
        }), 500

    return jsonify({
        **result,
        "log": log,
    })


@bp.route("/api/deps/install-history")
def api_deps_install_history():
    try:
        items = list_install_jobs(request.args.get("limit", 50), request.args.get("kind", ""))
    except ValueError as exc:
        return jsonify(ok=False, msg=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, msg=f"安装历史无法读取：{exc}"), 500
    return jsonify(ok=True, items=items)


@bp.route("/deps/install-history")
def deps_install_history():
    try:
        items = list_install_jobs(request.args.get("limit", 50), request.args.get("kind", ""))
    except ValueError as exc:
        body = page_header_card(
            "依赖/运行时安装历史",
            help_html=h(exc),
            actions_html='<a class="btn btn-gray" href="/deps">返回依赖管理</a>',
        )
        return layout("安装历史", "deps", body), 400
    except Exception as exc:
        body = page_header_card(
            "依赖/运行时安装历史",
            help_html=f"安装历史无法读取：{h(exc)}。请修复回执文件后重试。",
            actions_html='<a class="btn btn-primary" href="/deps/install-history">重新读取</a><a class="btn btn-gray" href="/deps">返回依赖管理</a>',
        )
        return layout("安装历史", "deps", body), 500
    rows = []
    for info in items:
        install_id = h(info.get("id") or "")
        kind = "依赖" if info.get("kind") == "dependency" else "运行时"
        rows.append(
            "<tr>"
            f"<td><code>{install_id}</code></td>"
            f"<td>{kind}</td>"
            f"<td>{h(info.get('package') or '-')}</td>"
            f"<td>{h(info.get('status') or '-')}</td>"
            f"<td>{h(info.get('updated_at') or '-')}</td>"
            f'<td><a class="btn btn-blue" href="/deps/install-log/{install_id}">查看安装日志</a></td>'
            "</tr>"
        )
    table = (
        '<div class="table-wrap"><table><thead><tr><th>安装 ID</th><th>类型</th><th>包/运行时</th>'
        '<th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="6">暂无安装历史</td></tr>')
        + "</tbody></table></div>"
    )
    body = page_header_card(
        "依赖/运行时安装历史",
        help_html="历史只读取受控安装回执；结果未知或失败请进入原日志确认后再明确重试。",
        actions_html='<a class="btn btn-gray" href="/deps">返回依赖管理</a>',
    ) + f'<section class="section"><div class="card">{table}</div></section>'
    return layout("安装历史", "deps", body)


@bp.route("/deps/refresh", methods=["GET", "POST"])
def deps_refresh():
    result = refresh_dependency_cache()

    rows = ""

    for name, status in result["packages"].items():
        ok = not str(status).startswith("不可用")
        badge = (
            '<span class="badge green status-badge status-success" role="status">可用</span>'
            if ok else
            '<span class="badge red status-badge status-error" role="status">异常</span>'
        )

        rows += f"""
<tr>
    <td>{h(name)}</td>
    <td>{badge}</td>
    <td>{h(status)}</td>
</tr>
"""

    core_table = table_card(
        "核心依赖检测",
        ["依赖", "状态", "版本 / 错误"],
        rows,
        actions_html='<a class="btn btn-gray" href="/deps">返回依赖管理</a>',
    )

    body = f"""
<div class="card">
    <div class="card-title">刷新依赖完成</div>
    <div class="help">刷新时间：{h(result["time"])}</div>
</div>

{core_table}
"""
    return layout("刷新依赖", "deps", body)


@bp.route("/deps/uninstall", methods=["POST"])
def deps_uninstall():
    name = request.form.get("name", "").strip()

    if not name:
        return "依赖名不能为空", 400

    try:
        result = pip_cmd(["uninstall", "-y", name])
        output = result.stdout
    except Exception as e:
        output = str(e)

    header_card = page_header_card(
        "卸载结果",
        actions_html='<a class="btn btn-gray" href="/deps">返回</a>',
    )

    body = f"""
{header_card}
<pre class="log">{h(output)}</pre>
"""
    return layout("卸载依赖", "deps", body)
