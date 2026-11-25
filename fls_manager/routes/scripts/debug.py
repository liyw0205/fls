import os
import time
import uuid
import signal
from urllib.parse import quote
import threading
import subprocess

from flask import redirect, abort, jsonify, request

from . import bp
from .helpers import (
    script_safe_path,
    script_rel_path,
    debug_task_for_script,
    debug_command_info,
)

from ...paths import LOG_DIR, SCRIPT_DEBUG_RUNS_FILE
from ...storage import read_json, write_json
from ...utils import h, now_str, safe_name, get_back_url, prune_completed_records
from ...ui.layout import layout
from ...ui.components import page_header_card
from ...ui.log_controls import log_controls
from ...logs import tail_file
from ...models import load_global_env, TASKS_MUTATION_LOCK
from ...constants import TASK_PROCESS_PREFIX

SCRIPT_DEBUG_RUNNING = {}
SCRIPT_DEBUG_LOCK = threading.RLock()
SCRIPT_DEBUG_MANUAL_STOPS = set()
SCRIPT_DEBUG_LIMIT = 200
SCRIPT_DEBUG_TEXT_LIMIT = 12000
SCRIPT_DEBUG_STATUSES = ("starting", "running", "succeeded", "failed", "stopped", "unknown")
SCRIPT_DEBUG_IN_FLIGHT = ("starting", "running")


def _debug_text(value, default=""):
    if value is None:
        return default
    return str(value)


def _debug_status(info):
    status = _debug_text(info.get("status")).strip()
    if status in SCRIPT_DEBUG_STATUSES:
        return status
    if info.get("running"):
        return "running"
    return "succeeded" if info.get("returncode") == 0 else "failed"


def _debug_rows(raw):
    rows = raw if isinstance(raw, list) else []
    normalized = []
    changed = not isinstance(raw, list)

    for row in rows:
        if not isinstance(row, dict):
            changed = True
            continue
        debug_id = _debug_text(row.get("id")).strip()
        if not debug_id:
            changed = True
            continue

        status = _debug_text(row.get("status")).strip()
        if status not in SCRIPT_DEBUG_STATUSES:
            status = "running" if row.get("running") else (
                "succeeded" if row.get("returncode") == 0 else "failed"
            )
            changed = True

        if status in SCRIPT_DEBUG_IN_FLIGHT and debug_id not in SCRIPT_DEBUG_RUNNING:
            status = "unknown"
            previous = _debug_text(row.get("error")).strip()
            recovery = "面板重启后无法确认调试进程结果，请检查日志和系统进程后重试"
            suffix = f"；{recovery}"
            row = dict(row)
            row["error"] = previous[:max(0, SCRIPT_DEBUG_TEXT_LIMIT - len(suffix))] + suffix
            changed = True

        returncode = row.get("returncode")
        if returncode is not None:
            try:
                returncode = int(returncode)
            except (TypeError, ValueError):
                returncode = None
                changed = True

        normalized_row = {
            "id": debug_id,
            "request_id": _debug_text(row.get("request_id")),
            "script": _debug_text(row.get("script")),
            "rel": _debug_text(row.get("rel")),
            "log_file": _debug_text(row.get("log_file")),
            "pid": row.get("pid") if row.get("pid") is not None else "-",
            "status": status,
            "running": status in SCRIPT_DEBUG_IN_FLIGHT,
            "returncode": returncode,
            "error": _debug_text(row.get("error"))[:SCRIPT_DEBUG_TEXT_LIMIT],
            "created_at": _debug_text(row.get("created_at")),
            "updated_at": _debug_text(row.get("updated_at")),
            "start_time": row.get("start_time"),
        }
        if normalized_row != row:
            changed = True
        normalized.append(normalized_row)

    limited = normalized[:SCRIPT_DEBUG_LIMIT]
    if limited != normalized:
        changed = True
    return limited, changed


def load_script_debug_runs(limit=None):
    with SCRIPT_DEBUG_LOCK:
        rows, changed = _debug_rows(read_json(SCRIPT_DEBUG_RUNS_FILE, None))
        if changed:
            try:
                write_json(SCRIPT_DEBUG_RUNS_FILE, rows)
            except Exception:
                # A read-only or unavailable data directory must not prevent
                # the panel from serving the recoverable in-memory state.
                pass
        if limit is None:
            return rows
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        return rows[:max(1, min(100, limit))]


def get_script_debug_run(debug_id):
    debug_id = _debug_text(debug_id).strip()
    if not debug_id:
        return None
    for row in load_script_debug_runs():
        if row.get("id") == debug_id:
            return row
    return None


def _debug_snapshot(info):
    status = _debug_status(info)
    return {
        "id": _debug_text(info.get("id")),
        "request_id": _debug_text(info.get("request_id")),
        "script": _debug_text(info.get("script")),
        "rel": _debug_text(info.get("rel")),
        "log_file": _debug_text(info.get("log_file")),
        "pid": info.get("pid", "-"),
        "status": status,
        "running": status in SCRIPT_DEBUG_IN_FLIGHT,
        "returncode": info.get("returncode"),
        "error": _debug_text(info.get("error"))[:SCRIPT_DEBUG_TEXT_LIMIT],
        "created_at": _debug_text(info.get("created_at")),
        "updated_at": now_str(),
        "start_time": info.get("start_time"),
    }


def persist_script_debug(info):
    if not isinstance(info, dict) or not _debug_text(info.get("id")).strip():
        return False
    snapshot = _debug_snapshot(info)
    with SCRIPT_DEBUG_LOCK:
        rows, _changed = _debug_rows(read_json(SCRIPT_DEBUG_RUNS_FILE, []))
        for index, row in enumerate(rows):
            if row.get("id") == snapshot["id"]:
                rows[index] = snapshot
                break
        else:
            rows.insert(0, snapshot)
        write_json(SCRIPT_DEBUG_RUNS_FILE, rows[:SCRIPT_DEBUG_LIMIT])
    return True


def _persist_debug_safely(info):
    """Persist diagnostics without allowing a storage outage to break a run."""
    try:
        persist_script_debug(info)
        return True
    except Exception as exc:
        message = f"持久化调试结果失败：{str(exc)[:800]}"
        if isinstance(info, dict) and not info.get("error"):
            info["error"] = message
        return False


def _debug_current_record(debug_id):
    with SCRIPT_DEBUG_LOCK:
        info = SCRIPT_DEBUG_RUNNING.get(debug_id)
        if info is not None:
            return dict(info)
        info = get_script_debug_run(debug_id)
        return dict(info) if isinstance(info, dict) else info


def script_debug_log_file(debug_id, script_name):
    safe = safe_name(script_name or "script-debug")
    return LOG_DIR / f"script-debug-{safe}-{debug_id}.log"


def terminate_debug_process(proc):
    if proc is None:
        return False

    try:
        if proc.poll() is not None:
            return True

        if os.name == "nt":
            try:
                proc.terminate()
            except Exception:
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

        try:
            proc.wait(timeout=1)
            return proc.poll() is not None
        except subprocess.TimeoutExpired:
            pass

        if os.name == "nt":
            try:
                proc.kill()
            except Exception:
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return proc.poll() is not None
    except Exception:
        return False


def script_debug_watch(debug_id, proc, log_fp):
    try:
        return_code = proc.wait()

        try:
            log_fp.write(
                f"\n===== 调试运行结束: {now_str()}，退出码: {return_code} =====\n".encode("utf-8")
            )
            log_fp.close()
        except Exception:
            pass

        with SCRIPT_DEBUG_LOCK:
            info = SCRIPT_DEBUG_RUNNING.get(debug_id)
            if info is not None:
                manually_stopped = (
                    debug_id in SCRIPT_DEBUG_MANUAL_STOPS
                    or _debug_status(info) == "stopped"
                )
                info["running"] = False
                info["returncode"] = return_code
                info["process"] = None
                if manually_stopped:
                    info["status"] = "stopped"
                else:
                    info["status"] = "succeeded" if return_code == 0 else "failed"
                    if return_code != 0 and not info.get("error"):
                        info["error"] = f"调试进程退出码：{return_code}"
                _persist_debug_safely(info)

    except Exception as e:
        try:
            log_fp.write(
                f"\n===== 调试监听异常: {e} =====\n".encode("utf-8")
            )
            log_fp.close()
        except Exception:
            pass

        with SCRIPT_DEBUG_LOCK:
            info = SCRIPT_DEBUG_RUNNING.get(debug_id)
            if info is not None:
                manually_stopped = (
                    debug_id in SCRIPT_DEBUG_MANUAL_STOPS
                    or _debug_status(info) == "stopped"
                )
                info["running"] = False
                info["returncode"] = -1
                info["process"] = None
                if manually_stopped:
                    info["status"] = "stopped"
                else:
                    info["status"] = "failed"
                    info["error"] = str(e)
                _persist_debug_safely(info)


@bp.route("/scripts/debug/<path:rel_path>")
def scripts_debug_run(rel_path):
    from ...script_rename import pending_script_rename

    with TASKS_MUTATION_LOCK:
        try:
            target = script_safe_path(rel_path)
            pending = pending_script_rename(path=target)
        except ValueError as e:
            return f"路径非法：{h(e)}", 400
        except Exception as e:
            return f"恢复状态读取失败：{h(e)}", 500
        if pending:
            return redirect(f"/scripts/rename-recovery/{pending['id']}")
        return _scripts_debug_run(rel_path)


def _scripts_debug_run(rel_path):
    try:
        info = _start_script_debug_run(rel_path)
    except ValueError as e:
        return f"路径非法：{h(e)}", 400
    except FileNotFoundError:
        abort(404)
    return redirect(
        f"/scripts/debug-log/{info['id']}?back={quote(get_back_url('/pull'))}"
    )


def _start_script_debug_run(rel_path, request_id="", require_persist=False):
    target = script_safe_path(rel_path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError

    rel = script_rel_path(target)

    debug_id = uuid.uuid4().hex
    log_file = script_debug_log_file(debug_id, target.name)
    prune_completed_records(SCRIPT_DEBUG_RUNNING)

    task = debug_task_for_script(rel, target, debug_id)

    created_at = now_str()
    info = {
        "id": debug_id,
        "request_id": request_id,
        "script": str(target),
        "rel": rel,
        "log_file": str(log_file),
        "running": True,
        "process": None,
        "pid": "-",
        "start_time": time.time(),
        "returncode": None,
        "error": "",
        "status": "starting",
        "created_at": created_at,
    }
    with SCRIPT_DEBUG_LOCK:
        SCRIPT_DEBUG_RUNNING[debug_id] = info
        try:
            if require_persist:
                persist_script_debug(info)
            else:
                _persist_debug_safely(info)
        except Exception:
            SCRIPT_DEBUG_RUNNING.pop(debug_id, None)
            raise

    log_fp = None

    try:
        log_fp = open(log_file, "ab", buffering=0)
        cmd_info = debug_command_info(task)

        env = os.environ.copy()
        env.update(load_global_env())
        env["PYTHONUNBUFFERED"] = "1"
        env["FLS_SCRIPT_DEBUG_ID"] = debug_id
        env["FLS_SCRIPT_DEBUG_FILE"] = str(target)
        env["FLS_TASK_PROCESS_NAME"] = (TASK_PROCESS_PREFIX + safe_name(f"script-debug-{target.name}"))[:120]

        header = (
            f"===== 脚本调试运行 =====\n"
            f"时间: {now_str()}\n"
            f"调试ID: {debug_id}\n"
            f"脚本: {rel}\n"
            f"============================================================\n"
        )

        log_fp.write(header.encode("utf-8"))

        popen_kwargs = {
            "shell": cmd_info.get("shell", False),
            "cwd": cmd_info["cwd"],
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "env": env,
        }

        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            cmd_info["cmd"],
            **popen_kwargs,
        )

        with SCRIPT_DEBUG_LOCK:
            info.update({
                "running": True,
                "process": proc,
                "pid": proc.pid,
                "status": "running",
                "error": "",
            })
            _persist_debug_safely(info)

        th = threading.Thread(
            target=script_debug_watch,
            args=(debug_id, proc, log_fp),
            daemon=True,
            name=f"fls-script-debug-{debug_id[:8]}",
        )
        th.start()

    except Exception as e:
        try:
            if log_fp is not None:
                log_fp.write(f"启动调试失败: {e}\n".encode("utf-8"))
                log_fp.close()
        except Exception:
            pass

        with SCRIPT_DEBUG_LOCK:
            info.update({
                "running": False,
                "process": None,
                "returncode": -1,
                "status": "failed",
                "error": str(e),
            })
            _persist_debug_safely(info)

    return dict(info)


@bp.route("/scripts/debug-stop/<debug_id>", methods=["POST"])
def scripts_debug_stop(debug_id):
    back_url = get_back_url("/pull")
    with SCRIPT_DEBUG_LOCK:
        info = SCRIPT_DEBUG_RUNNING.get(debug_id)
        persisted_info = None if info is not None else get_script_debug_run(debug_id)
        info = info or persisted_info

    if not info:
        body = page_header_card(
            "调试记录不存在",
            help_html="调试记录不存在或面板已重启，无法停止此调试运行。",
            actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/logs?back={h(quote(back_url, safe=''))}">日志管理</a>
""",
        )
        return layout("停止调试", "pull", body), 404

    if info is persisted_info:
        error = "面板重启后无法确认调试进程结果，不能安全停止；请检查日志和系统进程后重试"
        body = page_header_card(
            "停止失败",
            help_html=h(error),
            actions_html=f'<a class="btn btn-gray" href="/scripts/debug-log/{h(quote(debug_id, safe=""))}?back={h(quote(back_url, safe=""))}">返回调试日志</a>',
        )
        return layout("停止调试", "pull", body), 409

    with SCRIPT_DEBUG_LOCK:
        proc = info.get("process")
        if proc is None and not info.get("running"):
            return redirect(back_url)

        return_code = None
        stopped = False
        try:
            if proc is not None:
                # The watcher may lag behind; use the actual process exit code.
                return_code = proc.poll()
                if return_code is None:
                    stopped = terminate_debug_process(proc)
                    return_code = proc.poll()
        except Exception as exc:
            info["error"] = f"停止失败：{str(exc)[:800]}"

        if return_code is None:
            info["running"] = True
            if not info.get("error") or info.get("error") == "用户手动停止":
                info["error"] = "停止失败：无法确认调试进程已退出"
            info["status"] = "running"
            _persist_debug_safely(info)
            body = page_header_card(
                "停止失败",
                help_html=h(info["error"]),
                actions_html=f'<a class="btn btn-gray" href="/scripts/debug-log/{h(quote(debug_id, safe=""))}?back={h(quote(back_url, safe=""))}">返回调试日志</a>',
            )
            return layout("停止调试", "pull", body), 409

        info["running"] = False
        info["returncode"] = return_code
        info["process"] = None
        if stopped:
            SCRIPT_DEBUG_MANUAL_STOPS.add(debug_id)
            info["status"] = "stopped"
            info["error"] = "用户手动停止"
            log_file = info.get("log_file")
            if log_file:
                try:
                    with open(log_file, "ab") as f:
                        f.write(
                            f"\n===== 用户手动停止调试: {now_str()} =====\n".encode("utf-8")
                        )
                except Exception:
                    pass
        else:
            info["status"] = "succeeded" if return_code == 0 else "failed"
        _persist_debug_safely(info)

    return redirect(back_url)


@bp.route("/scripts/debug-log/<debug_id>")
def scripts_debug_log(debug_id):
    back_url = get_back_url("/pull")
    info = _debug_current_record(debug_id)

    if not info:
        body = page_header_card(
            "脚本调试日志",
            help_html="""
        调试记录不存在或面板已重启。<br>
        可以到日志管理里查找 <code>script-debug-*.log</code>。
""",
            actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/logs?back={h(back_url)}">日志管理</a>
""",
        )
        return layout("脚本调试日志", "pull", body), 404

    stop_btn = ""
    status = _debug_status(info)

    if status in ("starting", "running") and info.get("process") is not None:
        stop_btn = f"""
<div class="row-actions-danger">
    <form class="inline-form" method="post" action="/scripts/debug-stop/{h(debug_id)}?back={h(back_url)}">
        <button class="btn btn-red" type="submit" onclick="return confirm('确定停止该调试运行吗？')">
            停止调试运行
        </button>
    </form>
</div>
"""

    header_card = page_header_card(
        "脚本调试日志",
        help_html=f"""
        状态：<b id="debugStatus">{h({"starting": "启动中", "running": "运行中", "succeeded": "已成功结束", "failed": "已失败", "stopped": "已手动停止", "unknown": "结果未知"}.get(status, status))}</b><br>
        PID：{h(info.get("pid") or "-")}<br>
        脚本：{h(info.get("script") or "-")}<br>
        日志文件：{h(info.get("log_file") or "-")}<br>
        退出码：{h(info.get("returncode") if info.get("returncode") is not None else "-")}<br>
        错误：{h(info.get("error") or "-")}
""",
        actions_html=f"""
{stop_btn}
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/pull">脚本</a>
""",
    )

    body = f"""
{header_card}

<pre class="log" id="log">加载中...</pre>
<section class="section" id="logRecovery" role="alert" hidden></section>
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

async function loadScriptDebugLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/scripts/debug-log/{h(debug_id)}?lines=1600", {{cache:"no-store"}});
        const json = await flsReadJsonResponse(res);
        flsLogPollRecovered(loadScriptDebugLog);

        const statusEl = document.getElementById("debugStatus");
        if(statusEl) statusEl.textContent = json.status_label || (json.running ? "运行中" : "已结束");

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
        flsLogPollFailed(e, loadScriptDebugLog, "调试日志");
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadScriptDebugLog();
if(!window.__FLS_ACTIVE_LOG_INTERVAL__) window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadScriptDebugLog, 2000);
</script>
"""
    return layout("脚本调试日志", "pull", body)


@bp.route("/api/scripts/debug-log/<debug_id>")
def api_scripts_debug_log(debug_id):
    info = _debug_current_record(debug_id)

    if not info:
        return jsonify({"ok": False, "msg": "调试记录不存在", "running": False, "log": ""}), 404

    try:
        lines = int(request.args.get("lines", "1200"))
    except (TypeError, ValueError):
        return jsonify({
            "ok": False,
            "msg": "lines 必须为整数",
            "running": False,
            "log": "",
        }), 400

    status = _debug_status(info)
    status_labels = {
        "starting": "启动中", "running": "运行中", "succeeded": "已成功结束",
        "failed": "已失败", "stopped": "已手动停止", "unknown": "结果未知",
    }
    return jsonify({
        "ok": True,
        "id": info.get("id", debug_id),
        "script": info.get("script", ""),
        "rel": info.get("rel", ""),
        "pid": info.get("pid", "-"),
        "status": status,
        "status_label": status_labels.get(status, status),
        "running": status in SCRIPT_DEBUG_IN_FLIGHT,
        "created_at": info.get("created_at", ""),
        "updated_at": info.get("updated_at", ""),
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "log_file": info.get("log_file", ""),
        "log": tail_file(info.get("log_file", ""), lines),
    })
