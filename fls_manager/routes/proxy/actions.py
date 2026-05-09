from ._common import *


@bp.route("/proxy/toggle/<proxy_id>")
def proxy_toggle(proxy_id):
    proxies = load_proxies()

    for p in proxies:
        if p.get("id") == proxy_id:
            p["enabled"] = not p.get("enabled", True)
            p["updated_at"] = now_str()
            break

    save_proxies(proxies)
    return redirect(url_for("proxy.proxy_page"))


@bp.route("/proxy/delete/<proxy_id>")
def proxy_delete(proxy_id):
    proxies = [p for p in load_proxies() if p.get("id") != proxy_id]
    save_proxies(proxies)
    return redirect(url_for("proxy.proxy_page"))

