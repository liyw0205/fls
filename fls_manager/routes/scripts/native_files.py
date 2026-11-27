import json
import stat
import uuid
from datetime import datetime
from pathlib import Path

from flask import jsonify, request
from werkzeug.datastructures import MultiDict

from . import bp
from .files import _guard_file_mutation
from .helpers import script_rel_path
from ...directory_jobs import directory_job_operation
from ...data_access import PanelStorageBusy
from ...models import TASKS_MUTATION_LOCK
from ...task_file_coordination import TASK_FILES_LOCK
from ...paths import SCRIPT_DIR
from ...script_files import (
    ScriptFileConflict,
    ScriptFileTooLarge,
    ScriptFileWriteError,
    publish_script_file,
    script_file_snapshot,
    script_file_target,
)
from ...script_rename import (
    RENAME_TERMINAL,
    begin_script_rename,
    get_script_rename,
    rename_preview,
    resume_script_rename,
    ScriptReferenceLimitError,
    script_delete_preview,
    validate_new_name,
    validate_rename_updates,
)


SCRIPT_FILE_MAX_BYTES = 2 * 1024 * 1024
SCRIPT_FILE_REQUEST_MAX_BYTES = SCRIPT_FILE_MAX_BYTES + 128 * 1024
SCRIPT_TREE_DEFAULT_LIMIT = 50
SCRIPT_TREE_MAX_LIMIT = 100


def _response(payload, status=200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _error(message, status, code):
    return _response({"ok": False, "code": code, "msg": message}, status)


def _json_body():
    if not request.is_json:
        return None, _error("请求必须使用 JSON", 415, "json_required")
    if request.content_length is not None and request.content_length > SCRIPT_FILE_REQUEST_MAX_BYTES:
        return None, _error("请求内容超过限制", 413, "request_too_large")
    raw = request.stream.read(SCRIPT_FILE_REQUEST_MAX_BYTES + 1)
    if len(raw) > SCRIPT_FILE_REQUEST_MAX_BYTES:
        return None, _error("请求内容超过限制", 413, "request_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _error("JSON 格式无效", 400, "invalid_json")
    if not isinstance(value, dict):
        return None, _error("请求字段无效", 400, "invalid_body")
    return value, None


def _path_target(raw, *, allow_root=False):
    if not isinstance(raw, str):
        raise ValueError("path 必须是字符串")
    if allow_root and raw.strip() in ("", "/"):
        return SCRIPT_DIR.resolve(), ""
    target = script_file_target(raw)
    return target, script_rel_path(target)


def _file_payload(path, content, revision, metadata):
    return {
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "content": content,
        "revision": revision,
        "size_bytes": metadata.st_size,
        "modified_at": datetime.fromtimestamp(metadata.st_mtime).astimezone().isoformat(),
    }


def _current_file(path, target):
    try:
        content, revision, metadata = script_file_snapshot(
            target, path, max_bytes=SCRIPT_FILE_MAX_BYTES,
        )
        return _file_payload(path, content, revision, metadata)
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _rename_preview(path, new_name):
    source = script_file_target(path)
    validate_new_name(new_name)
    if not source.exists():
        raise FileNotFoundError
    destination = script_file_target(
        (source.with_name(new_name)).relative_to(SCRIPT_DIR.resolve()).as_posix(),
    )
    if source == destination:
        raise ValueError("新名称与当前名称相同")
    if destination.exists() or destination.is_symlink():
        raise ScriptFileConflict("目标已存在，未覆盖文件或任务")
    _guard_file_mutation(source)
    _guard_file_mutation(destination)
    references, revision = rename_preview(source, destination)
    return source, destination, references, revision


def _rename_operation_payload(record):
    from ...models import load_tasks

    source = script_file_target(record["source"])
    destination = script_file_target(record["destination"])
    tasks = {task["id"]: task for task in load_tasks()}
    states = []
    for update in record.get("updates", []):
        task = tasks.get(update["id"])
        if task is None:
            state = "missing"
        else:
            values = {key: task.get(key, "") for key in ("command", "config_path")}
            state = "updated" if values == update["after"] else (
                "unchanged" if values == update["before"] else "conflict"
            )
        states.append({"task_id": update["id"], "state": state})
    return {
        "id": record["id"],
        "source": record["source"],
        "destination": record["destination"],
        "status": record.get("status", "unknown"),
        "finished": record.get("status") in RENAME_TERMINAL,
        "updated_at": record.get("updated_at", ""),
        "actual": {
            "source_exists": source.exists(),
            "destination_exists": destination.exists(),
            "tasks": states,
        },
    }


def _rename_identifier(value):
    if not isinstance(value, str) or len(value) != 32:
        return False
    try:
        return uuid.UUID(hex=value).hex == value
    except ValueError:
        return False


def _delete_preview(path):
    target, relative = _path_target(path)
    _guard_file_mutation(target)
    references, revision, metadata = script_delete_preview(target)
    return target, {
        "path": relative,
        "kind": "file",
        "revision": revision,
        "size_bytes": metadata.st_size,
        "modified_at": datetime.fromtimestamp(metadata.st_mtime).astimezone().isoformat(),
        "references": references,
    }


def _assert_script_not_running(target, references):
    from ...task_runner import is_running
    from .debug import SCRIPT_DEBUG_LOCK, SCRIPT_DEBUG_RUNNING

    if any(is_running(reference["task_id"]) for reference in references):
        raise ScriptFileConflict("引用该脚本的任务仍在运行，请先停止任务后删除")
    with SCRIPT_DEBUG_LOCK:
        for info in SCRIPT_DEBUG_RUNNING.values():
            running_path = info.get("script") or ""
            if info.get("running") and running_path and Path(running_path).resolve() == target:
                raise ScriptFileConflict("脚本仍在调试，请先停止调试后删除")


@bp.route("/api/scripts/tree")
@directory_job_operation
def api_script_tree():
    raw_path = request.args.get("path", "")
    query = request.args.get("q", "").strip().casefold()
    try:
        page = int(request.args.get("page", "1"))
        limit = int(request.args.get("limit", str(SCRIPT_TREE_DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        return _error("page 和 limit 必须是整数", 400, "invalid_pagination")
    if page < 1 or limit < 1 or limit > SCRIPT_TREE_MAX_LIMIT:
        return _error("page 必须大于 0，limit 必须在 1 到 100 之间", 400, "invalid_pagination")

    try:
        current_dir, current_path = _path_target(raw_path, allow_root=True)
    except ValueError:
        return _error("脚本目录路径无效", 400, "invalid_path")
    if not current_dir.exists():
        return _error("脚本目录不存在", 404, "not_found")
    if not current_dir.is_dir():
        return _error("path 必须是目录", 400, "not_directory")

    try:
        entries = []
        for item in current_dir.iterdir():
            try:
                metadata = item.stat(follow_symlinks=False)
            except OSError:
                continue
            if item.is_symlink() or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                continue
            kind = "directory" if stat.S_ISDIR(metadata.st_mode) else "file"
            relative = item.relative_to(SCRIPT_DIR.resolve()).as_posix()
            if query and query not in item.name.casefold() and query not in relative.casefold():
                continue
            if item.name.startswith(".fls-script-") and item.name.endswith(".tmp"):
                continue
            entries.append({
                "name": item.name,
                "path": relative,
                "kind": kind,
                "size_bytes": metadata.st_size if kind == "file" else None,
                "modified_at": datetime.fromtimestamp(metadata.st_mtime).astimezone().isoformat(),
            })
    except OSError:
        return _error("脚本目录读取失败", 500, "directory_read_failed")

    entries.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold(), item["name"]))
    total = len(entries)
    pages = max(1, (total + limit - 1) // limit)
    start = (page - 1) * limit
    items = entries[start:start + limit]
    parent = current_path.rsplit("/", 1)[0] if "/" in current_path else ("" if current_path else None)
    return _response({
        "ok": True,
        "path": current_path,
        "parent": parent,
        "items": items,
        "page": page,
        "pages": pages,
        "total": total,
        "limit": limit,
    })


@bp.route("/api/scripts/delete/preview")
@directory_job_operation
def api_script_delete_preview():
    raw_path = request.args.get("path", "")
    try:
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            _target, preview = _delete_preview(raw_path)
    except FileNotFoundError:
        return _error("脚本文件不存在", 404, "not_found")
    except ScriptFileConflict:
        return _error("脚本路径存在待恢复操作", 409, "recovery_required")
    except ScriptReferenceLimitError:
        return _error("引用该文件的任务超过原生确认上限", 413, "too_many_references")
    except ValueError:
        return _error("只支持预览普通脚本文件", 400, "not_file")
    except OSError:
        return _error("删除影响预览读取失败", 500, "preview_failed")
    return _response({"ok": True, "preview": preview})


@bp.route("/api/scripts/file", methods=["GET", "PUT", "POST", "DELETE"])
@directory_job_operation
def api_script_file():
    if request.method == "GET":
        raw_path = request.args.get("path")
        try:
            target, path = _path_target(raw_path)
        except ValueError:
            return _error("脚本文件路径无效", 400, "invalid_path")
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            try:
                _guard_file_mutation(target)
                if not target.exists():
                    return _error("脚本文件不存在", 404, "not_found")
                if not target.is_file():
                    return _error("path 必须是普通文件", 400, "not_file")
                content, revision, metadata = script_file_snapshot(
                    target, path, max_bytes=SCRIPT_FILE_MAX_BYTES,
                )
            except ScriptFileConflict:
                return _error("脚本路径存在待恢复操作", 409, "recovery_required")
            except ScriptFileTooLarge:
                return _error("文件超过 2 MiB 原生编辑上限", 413, "file_too_large")
            except UnicodeDecodeError:
                return _error("文件不是 UTF-8 文本，未读取内容", 415, "not_utf8_text")
            except FileNotFoundError:
                return _error("脚本文件不存在", 404, "not_found")
            except OSError:
                return _error("脚本文件读取失败", 500, "file_read_failed")
        return _response({"ok": True, "file": _file_payload(path, content, revision, metadata)})

    data, error = _json_body()
    if error is not None:
        return error

    if request.method == "DELETE":
        if set(data) != {"path", "revision", "confirm_references"}:
            return _error("请求字段无效", 400, "invalid_body")
        raw_path = data.get("path")
        revision = data.get("revision")
        confirm_references = data.get("confirm_references")
        if (
            not isinstance(raw_path, str)
            or not isinstance(revision, str)
            or len(revision) != 64
            or any(char not in "0123456789abcdef" for char in revision)
            or not isinstance(confirm_references, bool)
        ):
            return _error("path、revision 或引用确认无效", 400, "invalid_body")
        try:
            with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
                target, preview = _delete_preview(raw_path)
                if revision != preview["revision"]:
                    return _response({
                        "ok": False,
                        "code": "revision_conflict",
                        "msg": "文件或任务引用已变更，请重新检查删除影响",
                        "preview": preview,
                    }, 409)
                if preview["references"] and not confirm_references:
                    return _response({
                        "ok": False,
                        "code": "references_ack_required",
                        "msg": "该文件仍被任务引用，请确认任务可能无法运行后再删除",
                        "preview": preview,
                    }, 409)
                _assert_script_not_running(target, preview["references"])
                target.unlink()
        except FileNotFoundError:
            return _error("脚本文件不存在", 404, "not_found")
        except ScriptFileConflict:
            return _error("脚本正在运行或存在待恢复操作，未删除", 409, "script_busy_or_recovery_required")
        except ScriptReferenceLimitError:
            return _error("引用该文件的任务超过原生确认上限", 413, "too_many_references")
        except ValueError:
            return _error("只支持删除普通脚本文件", 400, "not_file")
        except OSError:
            return _error("删除失败或结果未知，请刷新目录确认", 503, "delete_result_unknown")
        return _response({"ok": True, "deleted": True, "path": preview["path"]})

    if request.method == "POST":
        if set(data) - {"path", "kind", "content"}:
            return _error("请求字段无效", 400, "invalid_body")
        raw_path = data.get("path")
        kind = data.get("kind")
        content = data.get("content", "")
        if not isinstance(raw_path, str) or kind not in ("file", "directory"):
            return _error("path 或 kind 无效", 400, "invalid_body")
        if not isinstance(content, str):
            return _error("content 必须是字符串", 400, "invalid_content")
        if kind == "directory" and content:
            return _error("目录不能包含文件内容", 400, "invalid_content")
        if len(content.encode("utf-8")) > SCRIPT_FILE_MAX_BYTES:
            return _error("文件内容超过 2 MiB 限制", 413, "file_too_large")
        try:
            target, path = _path_target(raw_path)
        except ValueError:
            return _error("脚本文件路径无效", 400, "invalid_path")
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            try:
                _guard_file_mutation(target)
                if target.exists():
                    return _error("目标已存在，未覆盖", 409, "target_exists")
                if not target.parent.is_dir():
                    return _error("上级目录不存在", 404, "parent_not_found")
                if kind == "directory":
                    target.mkdir()
                else:
                    publish_script_file(target, path, content)
            except ScriptFileConflict:
                return _error("路径存在待恢复重命名", 409, "recovery_required")
            except ScriptFileWriteError:
                return _error("文件创建状态未知，请刷新目录确认后再重试", 503, "write_result_unknown")
            except FileExistsError:
                return _error("目标已存在，未覆盖", 409, "target_exists")
            except FileNotFoundError:
                return _error("上级目录不存在", 404, "parent_not_found")
            except OSError:
                return _error("脚本项目创建失败", 500, "create_failed")
        return _response({"ok": True, "path": path, "kind": kind}, 201)

    if set(data) - {"path", "content", "revision"}:
        return _error("请求字段无效", 400, "invalid_body")
    raw_path = data.get("path")
    content = data.get("content")
    revision = data.get("revision")
    if not isinstance(raw_path, str) or not isinstance(content, str):
        return _error("path 和 content 必须是字符串", 400, "invalid_body")
    if not isinstance(revision, str) or len(revision) != 64 or any(ch not in "0123456789abcdef" for ch in revision):
        return _error("revision 无效，请重新读取文件", 400, "invalid_revision")
    if len(content.encode("utf-8")) > SCRIPT_FILE_MAX_BYTES:
        return _error("文件内容超过 2 MiB 限制", 413, "file_too_large")
    try:
        target, path = _path_target(raw_path)
    except ValueError:
        return _error("脚本文件路径无效", 400, "invalid_path")

    with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
        try:
            _guard_file_mutation(target)
            if not target.exists():
                return _error("脚本文件不存在", 404, "not_found")
            if not target.is_file():
                return _error("path 必须是普通文件", 400, "not_file")
            current_content, current_revision, metadata = script_file_snapshot(
                target, path, max_bytes=SCRIPT_FILE_MAX_BYTES,
            )
            if revision != current_revision:
                if content == current_content:
                    return _response({
                        "ok": True,
                        "changed": False,
                        "file": _file_payload(path, current_content, current_revision, metadata),
                    })
                return _response({
                    "ok": False,
                    "code": "revision_conflict",
                    "msg": "文件已被其他操作修改，未覆盖当前文件",
                    "current": _file_payload(path, current_content, current_revision, metadata),
                }, 409)
            publish_script_file(target, path, content, revision, metadata)
            saved_content, saved_revision, saved_metadata = script_file_snapshot(
                target, path, max_bytes=SCRIPT_FILE_MAX_BYTES,
            )
        except ScriptFileConflict:
            current = _current_file(path, target)
            return _response({
                "ok": False,
                "code": "revision_conflict",
                "msg": "文件已被其他操作修改，未覆盖当前文件",
                **({"current": current} if current is not None else {}),
            }, 409)
        except ScriptFileTooLarge:
            return _error("文件超过 2 MiB 原生编辑上限", 413, "file_too_large")
        except UnicodeDecodeError:
            return _error("文件不是 UTF-8 文本，未读取内容", 415, "not_utf8_text")
        except FileNotFoundError:
            return _error("脚本文件不存在", 404, "not_found")
        except ScriptFileWriteError as exc:
            return _response({
                "ok": False,
                "saved": exc.saved,
                "code": "write_result_unknown" if exc.saved else "write_failed",
                "msg": "文件保存状态异常，请重新读取确认后再编辑",
            }, 503)
        except OSError:
            return _error("脚本文件保存失败", 500, "file_write_failed")

    return _response({
        "ok": True,
        "changed": saved_content != current_content,
        "file": _file_payload(path, saved_content, saved_revision, saved_metadata),
    })


@bp.route("/api/scripts/rename/preview")
@directory_job_operation
def api_script_rename_preview():
    raw_path = request.args.get("path", "")
    new_name = request.args.get("new_name", "")
    try:
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            source, destination, references, revision = _rename_preview(raw_path, new_name)
    except FileNotFoundError:
        return _error("源文件或目录不存在", 404, "not_found")
    except ScriptFileConflict:
        return _error("路径已存在待恢复操作或目标名称已占用", 409, "rename_conflict")
    except ValueError:
        return _error("脚本路径或新名称无效", 400, "invalid_path_or_name")
    except OSError:
        return _error("重命名预览读取失败", 500, "preview_failed")
    return _response({
        "ok": True,
        "preview": {
            "source": script_rel_path(source),
            "destination": script_rel_path(destination),
            "revision": revision,
            "references": references,
        },
    })


@bp.route("/api/scripts/rename", methods=["POST"])
@directory_job_operation
def api_script_rename():
    data, error = _json_body()
    if error is not None:
        return error
    if set(data) != {"path", "new_name", "revision", "operation_id", "references"}:
        return _error("请求字段无效", 400, "invalid_body")
    raw_path = data.get("path")
    new_name = data.get("new_name")
    revision = data.get("revision")
    operation_id = data.get("operation_id")
    raw_references = data.get("references")
    if (
        not isinstance(raw_path, str)
        or not isinstance(new_name, str)
        or not isinstance(revision, str)
        or len(revision) != 64
        or any(char not in "0123456789abcdef" for char in revision)
        or not _rename_identifier(operation_id)
        or not isinstance(raw_references, list)
    ):
        return _error("路径、名称、revision、operation_id 或任务引用无效", 400, "invalid_body")

    form = MultiDict()
    desired = {}
    for reference in raw_references:
        if (
            not isinstance(reference, dict)
            or set(reference) != {"task_id", "command", "config_path"}
            or not all(isinstance(reference.get(key), str) for key in ("task_id", "command", "config_path"))
            or not reference["task_id"]
            or reference["task_id"] in desired
        ):
            return _error("任务引用格式无效", 400, "invalid_references")
        task_id = reference["task_id"]
        command = reference["command"]
        config_path = reference["config_path"]
        desired[task_id] = {"command": command, "config_path": config_path}
        form.add("task_id", task_id)
        form[f"command_{task_id}"] = command
        form[f"config_{task_id}"] = config_path

    try:
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            source = script_file_target(raw_path)
            validate_new_name(new_name)
            destination = script_file_target(
                source.with_name(new_name).relative_to(SCRIPT_DIR.resolve()).as_posix(),
            )
            source_rel = script_rel_path(source)
            destination_rel = script_rel_path(destination)
            existing = get_script_rename(operation_id)
            if existing:
                recorded = {item["id"]: item["after"] for item in existing.get("updates", [])}
                if (
                    existing["source"] != source_rel
                    or existing["destination"] != destination_rel
                    or recorded != desired
                ):
                    return _error("operation_id 已用于其他重命名请求", 409, "operation_conflict")
                return _response({
                    "ok": True,
                    "replayed": True,
                    "operation": _rename_operation_payload(existing),
                }, 200 if existing.get("status") in RENAME_TERMINAL else 202)

            source, destination, references, current_revision = _rename_preview(raw_path, new_name)
            if revision != current_revision:
                return _response({
                    "ok": False,
                    "code": "rename_revision_conflict",
                    "msg": "文件或任务引用已变更，请重新检查改名影响",
                    "preview": {
                        "source": script_rel_path(source),
                        "destination": script_rel_path(destination),
                        "revision": current_revision,
                        "references": references,
                    },
                }, 409)
            updates = validate_rename_updates(references, form, source, destination)
            record = begin_script_rename(source, destination, operation_id, updates)
    except FileNotFoundError:
        return _error("源文件或目录不存在", 404, "not_found")
    except ScriptFileConflict:
        record = get_script_rename(operation_id)
        if record:
            return _response({
                "ok": False,
                "code": "rename_recovery_required",
                "msg": "重命名未完整结束，可检查状态后继续或回滚",
                "operation": _rename_operation_payload(record),
            }, 503)
        return _error("文件、任务或调试状态冲突，未开始重命名", 409, "rename_conflict")
    except ValueError:
        return _error("任务引用无效或仍包含旧路径，未执行重命名", 400, "invalid_references")
    except OSError:
        return _error("重命名操作状态未知，请读取操作状态后再处理", 503, "rename_result_unknown")
    except PanelStorageBusy:
        raise
    except Exception:
        record = get_script_rename(operation_id)
        if record:
            return _response({
                "ok": False,
                "code": "rename_recovery_required",
                "msg": "重命名未完整结束，可检查状态后继续或回滚",
                "operation": _rename_operation_payload(record),
            }, 503)
        return _error("重命名失败，未完成文件或任务变更", 500, "rename_failed")
    return _response({
        "ok": True,
        "operation": _rename_operation_payload(record),
    }, 200 if record.get("status") in RENAME_TERMINAL else 202)


@bp.route("/api/scripts/rename/<operation_id>")
@directory_job_operation
def api_script_rename_status(operation_id):
    if not _rename_identifier(operation_id):
        return _error("operation_id 无效", 400, "invalid_operation_id")
    try:
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            record = get_script_rename(operation_id)
            if not record:
                return _error("重命名记录不存在", 404, "not_found")
            operation = _rename_operation_payload(record)
    except PanelStorageBusy:
        raise
    except (OSError, ValueError, RuntimeError):
        return _error("重命名状态读取失败", 500, "status_read_failed")
    return _response({"ok": True, "operation": operation})


@bp.route("/api/scripts/rename/<operation_id>/recovery", methods=["POST"])
@directory_job_operation
def api_script_rename_recovery(operation_id):
    if not _rename_identifier(operation_id):
        return _error("operation_id 无效", 400, "invalid_operation_id")
    data, error = _json_body()
    if error is not None:
        return error
    if set(data) != {"action"} or data.get("action") not in ("resume", "rollback"):
        return _error("action 必须是 resume 或 rollback", 400, "invalid_action")
    try:
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            record = get_script_rename(operation_id)
            if not record:
                return _error("重命名记录不存在", 404, "not_found")
            record = resume_script_rename(record, rollback=data["action"] == "rollback")
            operation = _rename_operation_payload(record)
    except ScriptFileConflict:
        record = get_script_rename(operation_id)
        return _response({
            "ok": False,
            "code": "rename_conflict",
            "msg": "当前文件或任务状态与恢复记录冲突，请检查后再试",
            **({"operation": _rename_operation_payload(record)} if record else {}),
        }, 409)
    except PanelStorageBusy:
        raise
    except Exception:
        record = get_script_rename(operation_id)
        return _response({
            "ok": False,
            "code": "rename_result_unknown",
            "msg": "恢复结果未知，请重新读取状态确认",
            **({"operation": _rename_operation_payload(record)} if record else {}),
        }, 503)
    return _response({"ok": True, "operation": operation}, 200 if operation["finished"] else 202)


bp.add_url_rule("/api/scripts/files", view_func=api_script_tree, methods=["GET"])
