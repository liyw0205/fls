import json

from flask import current_app, request

from .bp import bp
from .native import query, response, unique_object
from ... import notify_deliveries as service


def _handle(callback):
    try:
        return response({"ok": True, **callback()})
    except service.DeliveryError as exc:
        return response({"ok": False, "msg": str(exc),
                         **({"accepted": False} if request.method == "POST" and exc.accepted is False else {})}, exc.status)
    except (ValueError, TypeError):
        return response({"ok": False, "msg": "通知投递参数或记录无法读取"}, 400 if request.method == "GET" else 503)
    except Exception:
        return response({"ok": False, "msg": "通知记录无法确认，请查询原请求", "status": "unknown"}, 503)


def _body():
    query(())
    if not request.is_json:
        raise service.DeliveryError("请求必须使用 JSON", 415)
    if request.content_length is not None and request.content_length > 4096:
        raise service.DeliveryError("投递请求超过 4 KiB", 413)
    raw = request.stream.read(4097)
    if len(raw) > 4096:
        raise service.DeliveryError("投递请求超过 4 KiB", 413)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        raise service.DeliveryError("投递 JSON 无效") from None


@bp.route("/api/notify/deliveries")
def native_delivery_list():
    def read():
        query(("page", "limit", "status", "channel", "item_id"))
        return service.list_deliveries(page=int(request.args.get("page", "1")), limit=int(request.args.get("limit", "25")),
                                       status=request.args.get("status", ""), channel=request.args.get("channel", ""),
                                       item_id=request.args.get("item_id", ""))
    return _handle(read)


@bp.route("/api/notify/deliveries/<delivery_id>")
def native_delivery_detail(delivery_id):
    def read():
        query(("reveal",))
        if request.args.get("reveal", "0") not in ("0", "1"):
            raise service.DeliveryError("正文查看参数无效")
        return {"item": service.delivery_detail(delivery_id, reveal=request.args.get("reveal") == "1")}
    return _handle(read)


@bp.route("/api/notify/delivery-requests/<request_id>")
def native_delivery_request(request_id):
    def read():
        query(())
        return {"request": service.request_status(request_id, current_app.secret_key)}
    return _handle(read)


@bp.route("/api/notify/deliveries/test", methods=["POST"])
def native_delivery_test():
    return _handle(lambda: {"request": service.start_delivery(_body(), current_app.secret_key)})


@bp.route("/api/notify/deliveries/<delivery_id>/retry", methods=["POST"])
def native_delivery_retry(delivery_id):
    return _handle(lambda: {"request": service.start_delivery(_body(), current_app.secret_key, delivery_id=delivery_id)})
