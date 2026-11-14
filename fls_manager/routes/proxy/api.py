from ._common import *


def _diagnostic_item_from_test(url, result):
    return {
        "url": url,
        "ok": True,
        "status_code": result.get("status_code", "-"),
        "elapsed": f"{result.get('elapsed_ms', '-')} ms",
    }


def _finish_saved_diagnostic(diagnostic_id, items, error=""):
    ok = bool(items) and all(bool(item.get("ok")) for item in items)
    status = "succeeded" if ok and not error else "failed"
    message = "检测完成" if status == "succeeded" else (error or "部分检测失败")
    finish_proxy_diagnostic(diagnostic_id, status, items, error or ("" if ok else message), message)
    return status == "succeeded"


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


@bp.route("/api/proxy/test/<proxy_id>", methods=["GET", "POST"])
def api_proxy_test_saved(proxy_id):
    proxy = get_proxy_for_test(proxy_id)

    if not proxy:
        return jsonify({
            "ok": False,
            "name": "",
            "error": "代理不存在",
        }), 404

    diagnostic_id = ""
    test_url = "https://www.baidu.com"
    try:
        diagnostic_id = create_proxy_diagnostic(proxy, "test", [test_url])
        result = test_proxy_object(proxy)
        _finish_saved_diagnostic(diagnostic_id, [_diagnostic_item_from_test(test_url, result)])
        return jsonify({
            "ok": True,
            "name": proxy.get("name", ""),
            "status_code": result["status_code"],
            "elapsed_ms": result["elapsed_ms"],
        })
    except Exception as e:
        if diagnostic_id:
            try:
                finish_proxy_diagnostic(diagnostic_id, "failed", [], str(e), str(e))
            except Exception:
                pass
        return jsonify({
            "ok": False,
            "name": proxy.get("name", ""),
            "error": str(e),
        })


@bp.route("/api/proxy/quality/<proxy_id>", methods=["GET", "POST"])
def api_proxy_quality_saved(proxy_id):
    proxy = get_proxy_for_test(proxy_id)

    if not proxy:
        return jsonify({
            "ok": False,
            "name": "",
            "error": "代理不存在",
        }), 404

    diagnostic_id = ""
    urls = parse_quality_urls(request.values.get("urls"))
    try:
        diagnostic_id = create_proxy_diagnostic(proxy, "quality", urls)
        items = quality_proxy_object(
            proxy,
            urls
        )
        _finish_saved_diagnostic(diagnostic_id, items)
        return jsonify({
            "ok": True,
            "name": proxy.get("name", ""),
            "items": items,
        })
    except Exception as e:
        if diagnostic_id:
            try:
                finish_proxy_diagnostic(diagnostic_id, "failed", [], str(e), str(e))
            except Exception:
                pass
        return jsonify({
            "ok": False,
            "name": proxy.get("name", ""),
            "error": str(e),
        })


@bp.route("/api/proxy/diagnostics")
def api_proxy_diagnostics():
    try:
        limit = int(request.args.get("limit", "50"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit 必须是整数", "items": []}), 400

    status = str(request.args.get("status") or "").strip()
    proxy_id = str(request.args.get("proxy_id") or "").strip()
    if status and status not in PROXY_DIAGNOSTIC_STATUSES:
        return jsonify({"ok": False, "error": "未知诊断状态", "items": []}), 400
    return jsonify({
        "ok": True,
        "items": load_proxy_diagnostics(limit=max(1, min(100, limit)), status=status, proxy_id=proxy_id),
        "limit": max(1, min(100, limit)),
        "status": status,
        "proxy_id": proxy_id,
    })


@bp.route("/api/proxy/diagnostics/retry/<diagnostic_id>", methods=["POST"])
def api_proxy_diagnostic_retry(diagnostic_id):
    try:
        ok, message, row = retry_proxy_diagnostic(diagnostic_id)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    payload = {"ok": ok, "error": message}
    if row is not None:
        payload["diagnostic"] = row
    if ok:
        return jsonify(payload)
    if message == "代理诊断不存在":
        return jsonify(payload), 404
    if message in ("代理诊断正在检测", "代理诊断已成功，无需重试"):
        return jsonify(payload), 409
    return jsonify(payload), 500
