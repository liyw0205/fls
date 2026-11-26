import json

from flask import current_app, request

from .bp import bp
from .native import MAX_BODY, _query, _response, _unique_object
from ...proxy_checks import CheckError, get_check, list_checks, start_check


def _execute(callback):
    try:
        payload = callback()
        return _response(dict(ok=True, **payload))
    except CheckError as exc:
        return _response({"ok": False, "msg": str(exc), **({"accepted": False} if request.method == "POST" else {})}, exc.status)
    except (ValueError, TypeError):
        return _response({"ok": False, "msg": "检测参数或记录格式无效", **({"accepted": False} if request.method == "POST" else {})}, 400)
    except Exception:
        return _response({"ok": False, "msg": "检测结果暂无法确认，请查询原提交，勿重复发起检测"}, 503)


def _body():
    _query(())
    if not request.is_json:
        raise CheckError("请求必须使用 JSON", 415)
    if request.content_length is not None and request.content_length > MAX_BODY:
        raise CheckError("检测请求超过 64 KiB 限制", 413)
    raw = request.stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise CheckError("检测请求超过 64 KiB 限制", 413)
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(data, dict):
        raise CheckError("检测请求必须是对象")
    return data


@bp.route("/api/proxy/checks", methods=["POST"])
def native_proxy_check_start():
    return _execute(lambda: {"check": start_check(_body(), current_app.secret_key)})


@bp.route("/api/proxy/checks/<check_id>/retry", methods=["POST"])
def native_proxy_check_retry(check_id):
    return _execute(lambda: {"check": start_check(_body(), current_app.secret_key, retry_of=check_id)})


@bp.route("/api/proxy/checks/<check_id>")
def native_proxy_check_status(check_id):
    def read():
        _query(())
        return {"check": get_check(check_id, current_app.secret_key)}
    return _execute(read)


@bp.route("/api/proxy/checks")
def native_proxy_check_history():
    def read():
        _query(("page", "limit", "proxy_id", "status", "kind"))
        return list_checks(current_app.secret_key, page=int(request.args.get("page", "1")),
                           limit=int(request.args.get("limit", "25")), proxy_id=request.args.get("proxy_id", ""),
                           status=request.args.get("status", ""), kind=request.args.get("kind", ""))
    return _execute(read)
