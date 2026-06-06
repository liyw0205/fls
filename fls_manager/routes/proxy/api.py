from ._common import *


@bp.route("/api/proxy/test-form", methods=["POST"])
def api_proxy_test_form():
    proxy = proxy_from_form(request.form)

    try:
        result = test_proxy_object(proxy)
        return jsonify({
            "ok": True,
            "status_code": result["status_code"],
            "elapsed_ms": result["elapsed_ms"],
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        })


@bp.route("/api/proxy/quality-form", methods=["POST"])
def api_proxy_quality_form():
    proxy = proxy_from_form(request.form)

    try:
        items = quality_proxy_object(
            proxy,
            parse_quality_urls(request.form.get("quality_urls"))
        )
        return jsonify({
            "ok": True,
            "items": items,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        })


@bp.route("/api/proxy/test/<proxy_id>")
def api_proxy_test_saved(proxy_id):
    proxy = get_proxy_for_test(proxy_id)

    if not proxy:
        return jsonify({
            "ok": False,
            "name": "",
            "error": "代理不存在",
        }), 404

    try:
        result = test_proxy_object(proxy)
        return jsonify({
            "ok": True,
            "name": proxy.get("name", ""),
            "status_code": result["status_code"],
            "elapsed_ms": result["elapsed_ms"],
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "name": proxy.get("name", ""),
            "error": str(e),
        })


@bp.route("/api/proxy/quality/<proxy_id>")
def api_proxy_quality_saved(proxy_id):
    proxy = get_proxy_for_test(proxy_id)

    if not proxy:
        return jsonify({
            "ok": False,
            "name": "",
            "error": "代理不存在",
        }), 404

    try:
        items = quality_proxy_object(
            proxy,
            parse_quality_urls(request.args.get("urls"))
        )
        return jsonify({
            "ok": True,
            "name": proxy.get("name", ""),
            "items": items,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "name": proxy.get("name", ""),
            "error": str(e),
        })
