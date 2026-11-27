import os
import time
import uuid
import signal
from urllib.parse import quote
import subprocess
import json
import tempfile

from flask import redirect, abort, jsonify, request

from . import bp
from .helpers import (
    script_safe_path,
    script_rel_path,
    debug_task_for_script,
    debug_command_info,
)

from ...paths import BASE_DIR, LOG_DIR, SCRIPT_DEBUG_RUNS_FILE
from ...storage import write_json
from ...directory_jobs import DirectoryJobHistoryError, directory_job_operation, start_directory_job
from ...directory_process import DirectoryProcess
from ...script_debug_coordination import DEBUG_RECEIPTS, DEBUG_IDENTITIES, debug_active, debug_active_ids
from ...utils import h, now_str, safe_name, get_back_url
from ...ui.layout import layout
from ...ui.components import page_header_card
from ...ui.log_controls import log_controls
from ...logs import tail_file
from ...models import load_global_env, TASKS_MUTATION_LOCK
from ...constants import TASK_PROCESS_PREFIX

SCRIPT_DEBUG_RUNNING = {}
SCRIPT_DEBUG_LOCK = DEBUG_RECEIPTS
SCRIPT_DEBUG_MANUAL_STOPS = set()
SCRIPT_DEBUG_LIMIT = 200
SCRIPT_DEBUG_TEXT_LIMIT = 12000
SCRIPT_DEBUG_STATUSES = ("starting", "running", "stopping", "succeeded", "failed", "stopped", "unknown")
SCRIPT_DEBUG_IN_FLIGHT = ("starting", "running", "stopping")


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

        if row.get("managed") and (row.get("request_id") != debug_id or not row.get("rel")
                                   or row.get("status") not in SCRIPT_DEBUG_STATUSES):
            raise DirectoryJobHistoryError("managed debug receipt is invalid")
        local = SCRIPT_DEBUG_RUNNING.get(debug_id)
        active = debug_active(debug_id) if row.get("managed") else bool(local and _debug_status(local) in SCRIPT_DEBUG_IN_FLIGHT)
        if status in SCRIPT_DEBUG_IN_FLIGHT and not active:
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
            "managed": row.get("managed") is True,
            "stop_request_id": _debug_text(row.get("stop_request_id")),
            "stop_consumed_id": _debug_text(row.get("stop_consumed_id")),
            "active": active,
        }
        if normalized_row != row:
            changed = True
        normalized.append(normalized_row)

    return normalized, changed


def _read_debug_rows():
    try:
        with SCRIPT_DEBUG_RUNS_FILE.open("rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("debug history is too large")
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise ValueError("debug history is not a list")
        return rows
    except FileNotFoundError:
        return []
    except (ValueError, OSError) as exc:
        raise DirectoryJobHistoryError("debug history cannot be read") from exc


def load_script_debug_runs(limit=None):
    with SCRIPT_DEBUG_LOCK:
        rows, _changed = _debug_rows(_read_debug_rows())
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
        "managed": info.get("managed") is True,
        "stop_request_id": _debug_text(info.get("stop_request_id")),
        "stop_consumed_id": _debug_text(info.get("stop_consumed_id")),
    }


def persist_script_debug(info):
    if not isinstance(info, dict) or not _debug_text(info.get("id")).strip():
        return False
    snapshot = _debug_snapshot(info)
    with SCRIPT_DEBUG_LOCK:
        rows = _read_debug_rows()
        for index, row in enumerate(rows):
            if isinstance(row, dict) and row.get("id") == snapshot["id"]:
                # A late owner diagnostic cannot overwrite a newer persisted result.
                if row.get("managed") and row.get("status") not in SCRIPT_DEBUG_IN_FLIGHT:
                    return False
                snapshot["stop_request_id"] = row.get("stop_request_id", snapshot["stop_request_id"])
                if (snapshot["status"] in ("starting", "running") and row.get("status") == "stopping"
                        and row.get("stop_request_id") != snapshot["stop_consumed_id"]):
                    snapshot["status"] = "stopping"
                rows[index] = snapshot
                break
        else:
            rows.insert(0, snapshot)
        protected = [row for row in rows if isinstance(row, dict) and (
            row.get("status") in (*SCRIPT_DEBUG_IN_FLIGHT, "unknown") or debug_active(row.get("id"))
        )]
        completed = [row for row in rows if isinstance(row, dict) and row not in protected]
        write_json(SCRIPT_DEBUG_RUNS_FILE, protected + completed[:max(0, SCRIPT_DEBUG_LIMIT - len(protected))])
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
        if info is not None and not info.get("managed"):
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
@directory_job_operation
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


def _start_script_debug_run(rel_path, request_id=""):
    from ...script_rename import pending_script_rename

    debug_id = request_id or uuid.uuid4().hex
    # Model readers already use model -> storage; no command waits under either lock.
    with TASKS_MUTATION_LOCK, SCRIPT_DEBUG_LOCK:
        previous = next((row for row in load_script_debug_runs() if row["request_id"] == debug_id), None)
        if previous:
            if previous["rel"] != rel_path:
                raise ValueError("request_id 已用于不同脚本")
            return dict(previous, _replayed=True)
        rows = _read_debug_rows()
        known = {row.get("id") for row in rows if isinstance(row, dict)}
        if debug_id in known or debug_active(debug_id) or debug_active_ids() - known:
            raise DirectoryJobHistoryError("active debug receipt is missing")
        if len([row for row in load_script_debug_runs() if row["active"] or row["status"] == "unknown"]) >= SCRIPT_DEBUG_LIMIT:
            raise DirectoryJobHistoryError("debug recovery history is full")
        target = script_safe_path(rel_path)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError
        if pending_script_rename(path=target):
            raise ValueError("脚本存在待恢复重命名，请先完成恢复")
        rel = script_rel_path(target)
        log_file = script_debug_log_file(debug_id, target.name)
        task = debug_task_for_script(rel, target, debug_id)
        cmd_info = debug_command_info(task)
        env = os.environ.copy()
        env.update(load_global_env())
        env["PYTHONUNBUFFERED"] = "1"
        env["FLS_SCRIPT_DEBUG_ID"] = debug_id
        env["FLS_SCRIPT_DEBUG_FILE"] = str(target)
        env["FLS_TASK_PROCESS_NAME"] = (TASK_PROCESS_PREFIX + safe_name(f"script-debug-{target.name}"))[:120]
        info = {
            "id": debug_id, "request_id": debug_id, "script": str(target), "rel": rel,
            "log_file": str(log_file), "running": True, "process": None, "pid": "-",
            "start_time": time.time(), "returncode": None, "error": "", "status": "starting",
            "created_at": now_str(), "managed": True,
        }
        owner = DEBUG_IDENTITIES.claim(debug_id)
        SCRIPT_DEBUG_RUNNING[debug_id] = info
        try:
            persist_script_debug(info)
        except BaseException:
            SCRIPT_DEBUG_RUNNING.pop(debug_id, None)
            owner.close()
            raise
    log_fp = None
    proc = None
    workspace = None
    try:
        log_fp = open(log_file, "ab", buffering=0)
        header = (
            f"===== 脚本调试运行 =====\n"
            f"时间: {now_str()}\n"
            f"调试ID: {debug_id}\n"
            f"脚本: {rel}\n"
            f"============================================================\n"
        )

        log_fp.write(header.encode("utf-8"))

        workspace = tempfile.TemporaryDirectory(prefix="fls-script-debug-")
        proc = DirectoryProcess(
            cmd_info["cmd"], workspace=workspace.name, cwd=cmd_info["cwd"], env=env,
            shell=cmd_info.get("shell", False), stdout=log_fp, stderr=subprocess.STDOUT,
            lease_path=BASE_DIR / ".backup_native_restore.lock",
            job_lease_path=DEBUG_IDENTITIES.lease_path(debug_id), stop_grace_seconds=1,
        )
        proc.start()
        with SCRIPT_DEBUG_LOCK:
            info.update({
                "running": True,
                "process": proc,
                "pid": proc.pid,
                "status": "running",
                "error": "",
            })
            persist_script_debug(info)
        start_directory_job(
            _managed_debug_watch, args=(info, proc, log_fp, workspace), owner=owner,
            name=f"fls-script-debug-{debug_id[:8]}",
        )
    except Exception as e:
        # Handoff failed: keep the admission and request leases until cleanup is certain.
        _finish_managed_debug(info, proc, log_fp, workspace, error=str(e))
        owner.close()
    return _debug_current_record(debug_id)


def _finish_managed_debug(info, proc, log_fp, workspace, *, error="", returncode=None, stopped=False):
    if proc is not None:
        while True:
            try:
                proc.stop(timeout=10)
                if returncode is None:
                    returncode = proc.wait(timeout=0)
                break
            except subprocess.TimeoutExpired:
                time.sleep(0.05)
            except Exception as exc:
                # A supervisor failure is unknown, but its exit still proves cleanup.
                if proc._process.poll() is not None:
                    error = error or str(exc)
                    break
                time.sleep(0.05)
    if log_fp is not None:
        try:
            log_fp.write(f"\n===== 调试运行结束: {now_str()}，退出码: {returncode} =====\n".encode("utf-8"))
        except Exception as exc:
            error = error or str(exc)
        while not log_fp.closed:
            try:
                log_fp.close()
            except Exception as exc:
                error = error or str(exc)
                time.sleep(0.05)
    if workspace is not None:
        try:
            workspace.cleanup()
        except OSError as exc:
            error = error or str(exc)
    status = ("stopped" if stopped else "succeeded" if returncode == 0 else "failed")
    if proc is not None and returncode is None:
        status = "unknown"
    if error and returncode == 0:
        status = "unknown"
    with SCRIPT_DEBUG_LOCK:
        info.update(running=False, process=None, returncode=returncode if returncode is not None else -1,
                    status=status, error=error or ("用户手动停止" if stopped else
                    f"调试进程退出码：{returncode}" if returncode else ""))
        _persist_debug_safely(info)


def _managed_debug_watch(info, proc, log_fp, workspace):
    stopped = False
    error = ""
    code = None
    try:
        while True:
            code = proc.poll()
            if code is not None:
                break
            with SCRIPT_DEBUG_LOCK:
                row = next((row for row in _read_debug_rows() if isinstance(row, dict) and row.get("id") == info["id"]), None)
                if row is None:
                    raise DirectoryJobHistoryError("active debug receipt is missing")
                intent = row.get("stop_request_id", "")
                consumed = info.get("stop_consumed_id", "")
            if intent and intent != consumed:
                stopped = True
                try:
                    proc.stop(timeout=10)
                    code = proc.wait(timeout=0)
                    break
                except subprocess.TimeoutExpired:
                    with SCRIPT_DEBUG_LOCK:
                        info.update(stop_consumed_id=intent, status="running", error="停止失败：尚未确认进程树退出，可重试")
                        persist_script_debug(info)
            time.sleep(0.05)
    except Exception as exc:
        error = str(exc)
    finally:
        _finish_managed_debug(info, proc, log_fp, workspace, error=error, returncode=code, stopped=stopped)


def request_debug_stop(debug_id):
    with SCRIPT_DEBUG_LOCK:
        info = get_script_debug_run(debug_id)
        if not info:
            return None
        if not info["active"] or info["status"] not in SCRIPT_DEBUG_IN_FLIGHT:
            return info
        rows = _read_debug_rows()
        for row in rows:
            if isinstance(row, dict) and row.get("id") == debug_id:
                # Only an explicit new POST creates a new attempt after a failed stop.
                if row.get("status") != "stopping":
                    row.update(status="stopping", running=True, stop_request_id=uuid.uuid4().hex, updated_at=now_str())
                write_json(SCRIPT_DEBUG_RUNS_FILE, rows)
                break
        return get_script_debug_run(debug_id)


@bp.route("/scripts/debug-stop/<debug_id>", methods=["POST"])
@directory_job_operation
def scripts_debug_stop(debug_id):
    info = _debug_current_record(debug_id)
    if info and info.get("managed"):
        try:
            info = request_debug_stop(debug_id)
        except OSError:
            return "停止请求保存失败，未发送停止信号", 503
        if info["status"] == "unknown" and not info["active"]:
            return "无法确认调试进程结果，未发送停止信号", 409
        return redirect(get_back_url("/pull"))
    return _legacy_scripts_debug_stop(debug_id)


def _legacy_scripts_debug_stop(debug_id):
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
            persist_script_debug(dict(info, id=debug_id, stop_request_id=uuid.uuid4().hex))
        except OSError:
            return "停止请求保存失败，未发送停止信号", 503
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
@directory_job_operation
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

    if status in SCRIPT_DEBUG_IN_FLIGHT and (info.get("active") or info.get("process") is not None):
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
        状态：<b id="debugStatus">{h({"starting": "启动中", "running": "运行中", "stopping": "停止中", "succeeded": "已成功结束", "failed": "已失败", "stopped": "已手动停止", "unknown": "结果未知"}.get(status, status))}</b><br>
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

        if(!json.running && !json.active){{
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
@directory_job_operation
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
        "starting": "启动中", "running": "运行中", "stopping": "停止中", "succeeded": "已成功结束",
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
        "active": info.get("active", status in SCRIPT_DEBUG_IN_FLIGHT),
        "created_at": info.get("created_at", ""),
        "updated_at": info.get("updated_at", ""),
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "log_file": info.get("log_file", ""),
        "log": tail_file(info.get("log_file", ""), lines),
    })
