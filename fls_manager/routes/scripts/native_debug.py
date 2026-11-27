import json
import re
import uuid

from flask import jsonify, request

from . import bp
from .debug import (
    SCRIPT_DEBUG_IN_FLIGHT,
    SCRIPT_DEBUG_LOCK,
    SCRIPT_DEBUG_RUNNING,
    _debug_current_record,
    _debug_status,
    _start_script_debug_run,
    get_script_debug_run,
    load_script_debug_runs,
    persist_script_debug,
    terminate_debug_process,
    request_debug_stop,
)
from ...directory_jobs import directory_job_operation
from .helpers import script_rel_path
from ...models import TASKS_MUTATION_LOCK
from ...paths import SCRIPT_DIR
from ...script_files import script_file_target
from ...script_rename import pending_script_rename
from ...sensitive import mask_sensitive_text
from ...utils import now_str
from ...logs import tail_file

SCRIPT_DEBUG_REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SCRIPT_DEBUG_HISTORY_LIMIT = 100
SCRIPT_DEBUG_LOG_DEFAULT_LINES = 1200
SCRIPT_DEBUG_LOG_MAX_LINES = 1200
SCRIPT_DEBUG_BODY_MAX_BYTES = 16 * 1024
_UNIX_ABSOLUTE_PATH_RE = re.compile(r'''(?<![A-Za-z0-9_:/])/(?!/)[^\s<>"']+''')
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r'''(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s<>"']+''')


def _response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(message, status, code, **extra):
    return _response({"ok": False, "code": code, "msg": message, **extra}, status)


def _json_body():
    if not request.is_json:
        return None, _error("请求必须使用 JSON", 415, "json_required")
    if request.content_length is not None and request.content_length > SCRIPT_DEBUG_BODY_MAX_BYTES:
        return None, _error("请求内容超过限制", 413, "request_too_large")
    raw = request.stream.read(SCRIPT_DEBUG_BODY_MAX_BYTES + 1)
    if len(raw) > SCRIPT_DEBUG_BODY_MAX_BYTES:
        return None, _error("请求内容超过限制", 413, "request_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _error("JSON 格式无效", 400, "invalid_json")
    if not isinstance(value, dict):
        return None, _error("请求字段无效", 400, "invalid_body")
    return value, None


def _relative_path(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise ValueError("path 必须是有效的脚本相对路径")
    target = script_file_target(raw)
    relative = script_rel_path(target).replace("\\", "/")
    if relative in ("", "."):
        raise ValueError("path 必须指向脚本文件")
    return target, relative


def _scrub_text(value, limit=4000):
    text = mask_sensitive_text(str(value or ""))
    for root in (str(SCRIPT_DIR.resolve()),):
        text = text.replace(root, "<scripts>")
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("<path>", text)
    text = _UNIX_ABSOLUTE_PATH_RE.sub("<path>", text)
    return text[:limit]


def _safe_log(raw, info):
    relative = str(info.get("rel") or "")
    lines = []
    for line in str(raw or "").splitlines():
        if line.startswith(("工作目录:", "日志文件:", "命令:", "实际启动命令:")):
            continue
        if line.startswith("脚本:"):
            line = f"脚本: {relative}"
        lines.append(_scrub_text(line, limit=8192))
    return "\n".join(lines)


def _run_payload(info):
    status = _debug_status(info)
    return {
        "id": str(info.get("id") or ""),
        **({"request_id": info["request_id"]} if SCRIPT_DEBUG_REQUEST_ID_RE.fullmatch(str(info.get("request_id") or "")) else {}),
        "path": str(info.get("rel") or ""),
        "status": status,
        "running": status in SCRIPT_DEBUG_IN_FLIGHT,
        "active": info.get("active", status in SCRIPT_DEBUG_IN_FLIGHT),
        "returncode": info.get("returncode"),
        "error": _scrub_text(info.get("error"), limit=1600),
        "created_at": str(info.get("created_at") or ""),
        "updated_at": str(info.get("updated_at") or ""),
    }


def _find_request(request_id):
    for info in load_script_debug_runs():
        if info.get("request_id") == request_id:
            return info
    return None


@bp.route("/api/scripts/debug-runs", methods=["GET"])
@directory_job_operation
def api_script_debug_runs():
    try:
        limit = int(request.args.get("limit", "30"))
    except (TypeError, ValueError):
        return _error("limit 必须为整数", 400, "invalid_limit")
    if not 1 <= limit <= SCRIPT_DEBUG_HISTORY_LIMIT:
        return _error("limit 超出范围", 400, "invalid_limit")
    rows = load_script_debug_runs(limit=limit)
    return _response({"ok": True, "runs": [_run_payload(row) for row in rows]})


@bp.route("/api/scripts/debug-runs", methods=["POST"])
@directory_job_operation
def api_script_debug_start():
    body, error = _json_body()
    if error:
        return error
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not SCRIPT_DEBUG_REQUEST_ID_RE.fullmatch(request_id):
        return _error("request_id 必须为 32 位小写十六进制字符串", 400, "invalid_request_id")
    try:
        target, relative = _relative_path(body.get("path"))
    except ValueError as exc:
        return _error(str(exc), 400, "invalid_path")

    with TASKS_MUTATION_LOCK:
        previous = _find_request(request_id)
        if previous is not None:
            if previous.get("rel") != relative:
                return _error("request_id 已用于不同脚本", 409, "request_id_conflict")
            return _response({"ok": True, "replayed": True, "run": _run_payload(previous)})

        if not target.exists() or not target.is_file():
            return _error("脚本文件不存在", 404, "script_not_found")
        try:
            pending = pending_script_rename(path=target)
        except Exception:
            return _error("无法确认脚本重命名恢复状态", 503, "recovery_state_unavailable")
        if pending:
            return _error("脚本存在待恢复重命名，请先完成恢复", 409, "rename_recovery_pending")

    try:
        info = _start_script_debug_run(relative, request_id=request_id)
    except FileNotFoundError:
        return _error("脚本文件不存在", 404, "script_not_found")
    except OSError:
        return _error("调试作业无法持久化，未启动脚本", 503, "run_persistence_failed")
    except ValueError as exc:
        return _error(str(exc), 409, "request_or_path_conflict")

    replayed = info.get("_replayed", False)
    return _response({"ok": True, "replayed": replayed, "run": _run_payload(info)}, 200 if replayed else 202)


@bp.route("/api/scripts/debug-requests/<request_id>", methods=["GET"])
@directory_job_operation
def api_script_debug_request(request_id):
    if not SCRIPT_DEBUG_REQUEST_ID_RE.fullmatch(request_id):
        return _error("request_id 无效", 400, "invalid_request_id")
    info = _find_request(request_id)
    if not info:
        return _error("未找到原提交记录，请勿自动重新启动", 404, "request_not_found")
    return _response({"ok": True, "run": _run_payload(info)})


@bp.route("/api/scripts/debug-runs/<debug_id>", methods=["GET"])
@directory_job_operation
def api_script_debug_run(debug_id):
    info = _debug_current_record(debug_id)
    if not info:
        return _error("调试记录不存在", 404, "run_not_found")
    try:
        lines = int(request.args.get("lines", str(SCRIPT_DEBUG_LOG_DEFAULT_LINES)))
    except (TypeError, ValueError):
        return _error("lines 必须为整数", 400, "invalid_lines")
    if not 1 <= lines <= SCRIPT_DEBUG_LOG_MAX_LINES:
        return _error("lines 超出范围", 400, "invalid_lines")
    log = tail_file(info.get("log_file", ""), lines)
    return _response({"ok": True, "run": _run_payload(info), "log": _safe_log(log, info)})


@bp.route("/api/scripts/debug-runs/<debug_id>/stop", methods=["POST"])
@directory_job_operation
def api_script_debug_stop(debug_id):
    current = _debug_current_record(debug_id)
    if current and current.get("managed"):
        try:
            current = request_debug_stop(debug_id)
        except OSError:
            return _error("停止请求保存失败，未发送停止信号", 503, "stop_state_persistence_failed")
        if current["status"] == "unknown" and not current["active"]:
            return _error("无法确认调试进程结果，未发送停止信号", 409, "process_unavailable", run=_run_payload(current))
        return _response({"ok": True, "run": _run_payload(current)}, 202 if current["active"] else 200)
    with SCRIPT_DEBUG_LOCK:
        info = SCRIPT_DEBUG_RUNNING.get(debug_id)
        if info is None:
            info = get_script_debug_run(debug_id)
            if not info:
                return _error("调试记录不存在", 404, "run_not_found")
            status = _debug_status(info)
            if status in (*SCRIPT_DEBUG_IN_FLIGHT, "unknown"):
                return _error(
                    "无法确认调试进程仍由当前面板管理，未发送停止信号",
                    409,
                    "process_unavailable",
                    run=_run_payload(info),
                )
            return _response({"ok": True, "run": _run_payload(info)})

        status = _debug_status(info)
        if status not in SCRIPT_DEBUG_IN_FLIGHT:
            return _response({"ok": True, "run": _run_payload(info)})
        proc = info.get("process")
        if proc is None:
            return _error(
                "调试进程尚不可确认，未发送停止信号",
                409,
                "process_unavailable",
                run=_run_payload(info),
            )

        stopped = False
        try:
            persist_script_debug(dict(info, stop_request_id=uuid.uuid4().hex))
        except OSError:
            return _error("停止请求保存失败，未发送停止信号", 503, "stop_state_persistence_failed")
        try:
            return_code = proc.poll()
            if return_code is None:
                stopped = terminate_debug_process(proc)
                return_code = proc.poll()
        except Exception as exc:
            info["error"] = f"停止失败：{str(exc)[:800]}"
            info["updated_at"] = now_str()
            try:
                persist_script_debug(info)
            except Exception:
                pass
            return _error(
                "停止调试运行失败，进程状态尚未确认",
                409,
                "stop_unconfirmed",
                run=_run_payload(info),
            )

        if return_code is None:
            info["running"] = True
            info["status"] = "running"
            info["error"] = "停止失败：无法确认调试进程已退出"
            info["updated_at"] = now_str()
            try:
                persist_script_debug(info)
            except Exception:
                pass
            return _error(
                "停止调试运行失败，进程状态尚未确认",
                409,
                "stop_unconfirmed",
                run=_run_payload(info),
            )

        info["running"] = False
        info["returncode"] = return_code
        info["process"] = None
        if stopped:
            from .debug import SCRIPT_DEBUG_MANUAL_STOPS

            SCRIPT_DEBUG_MANUAL_STOPS.add(debug_id)
            info["status"] = "stopped"
            info["error"] = "用户手动停止"
            try:
                with open(info.get("log_file", ""), "ab") as log_fp:
                    log_fp.write(f"\n===== 用户手动停止调试: {now_str()} =====\n".encode("utf-8"))
            except OSError:
                pass
        else:
            info["status"] = "succeeded" if return_code == 0 else "failed"
        info["updated_at"] = now_str()
        try:
            persist_script_debug(info)
        except Exception:
            return _error(
                "调试进程已停止，但终态未能持久化；请刷新后确认",
                503,
                "stop_state_persistence_failed",
                run=_run_payload(info),
            )
        return _response({"ok": True, "run": _run_payload(info)})
