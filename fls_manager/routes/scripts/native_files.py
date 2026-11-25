import json
import stat
from datetime import datetime

from flask import jsonify, request

from . import bp
from .files import _guard_file_mutation
from .helpers import script_rel_path
from ...models import TASKS_MUTATION_LOCK
from ...paths import SCRIPT_DIR
from ...script_files import (
    ScriptFileConflict,
    ScriptFileTooLarge,
    ScriptFileWriteError,
    publish_script_file,
    script_file_snapshot,
    script_file_target,
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


@bp.route("/api/scripts/tree")
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


@bp.route("/api/scripts/file", methods=["GET", "PUT", "POST"])
def api_script_file():
    if request.method == "GET":
        raw_path = request.args.get("path")
        try:
            target, path = _path_target(raw_path)
        except ValueError:
            return _error("脚本文件路径无效", 400, "invalid_path")
        with TASKS_MUTATION_LOCK:
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
        with TASKS_MUTATION_LOCK:
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

    with TASKS_MUTATION_LOCK:
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


bp.add_url_rule("/api/scripts/files", view_func=api_script_tree, methods=["GET"])
