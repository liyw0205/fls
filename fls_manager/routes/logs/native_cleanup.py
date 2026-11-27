import json

from flask import jsonify, request

from .bp import bp
from ._common import log_file_group_name
from ... import log_cleanup
from ...directory_jobs import directory_job_operation


MAX_BODY_BYTES = 384 * 1024


def _response(payload, status=200):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


def _body():
    if not request.is_json:
        raise log_cleanup.CleanupError("请求必须使用 JSON", "json_required", 415)
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise log_cleanup.CleanupError("请求超过大小限制", "request_too_large", 413)
    raw = request.stream.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise log_cleanup.CleanupError("请求超过大小限制", "request_too_large", 413)
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=log_cleanup._unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise log_cleanup.CleanupError("JSON 格式无效或字段重复", "invalid_json", 400) from exc
    if not isinstance(body, dict):
        raise log_cleanup.CleanupError("请求必须是 JSON 对象", "invalid_json", 400)
    return body


def _failure(error):
    return _response({"ok": False, "code": error.code, "msg": str(error)}, error.status)


@bp.route("/api/logs/cleanup/capabilities")
def api_log_cleanup_capabilities():
    return _response({"ok": True, "capabilities": {
        "supported": True, "max_files": log_cleanup.MAX_FILES,
        "max_groups": log_cleanup.MAX_GROUPS, "preview_ttl_seconds": log_cleanup.PREVIEW_TTL,
    }})


@bp.route("/api/logs/cleanup/preview", methods=["POST"])
@directory_job_operation
def api_log_cleanup_preview():
    try:
        preview = log_cleanup.preview_cleanup(_body(), lambda path: log_file_group_name(path, strict=True))
        return _response({"ok": True, "preview": preview})
    except log_cleanup.CleanupError as exc:
        return _failure(exc)


@bp.route("/api/logs/cleanup", methods=["POST"])
@directory_job_operation
def api_log_cleanup():
    try:
        body = _body()
        if set(body) != {"request_id", "preview_id", "digest"}:
            raise log_cleanup.CleanupError("清理请求字段无效", "invalid_cleanup", 400)
        result = log_cleanup.execute_cleanup(body["request_id"], body["preview_id"], body["digest"])
        return _response({"ok": True, "request": result})
    except log_cleanup.CleanupError as exc:
        return _failure(exc)


@bp.route("/api/logs/cleanup/requests/<request_id>")
def api_log_cleanup_request(request_id):
    try:
        return _response({"ok": True, "request": log_cleanup.get_cleanup_request(request_id)})
    except log_cleanup.CleanupError as exc:
        return _failure(exc)


@bp.route("/api/logs/cleanup/requests/<request_id>/reconcile", methods=["POST"])
@directory_job_operation
def api_log_cleanup_reconcile(request_id):
    try:
        if _body():
            raise log_cleanup.CleanupError("核对请求字段无效", "invalid_cleanup", 400)
        return _response({"ok": True, "request": log_cleanup.reconcile_cleanup(request_id)})
    except log_cleanup.CleanupError as exc:
        return _failure(exc)
