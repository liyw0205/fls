from ._common import *
import json
from urllib.parse import quote
from ...models import load_tasks
from ...proxy_actions import mutate_proxy


def proxy_submit(action, proxy_id):
    data = dict(request.form.to_dict(), action=action, proxy_id=proxy_id)
    payload, status = mutate_proxy(data, load_proxies=load_proxies,
        save_proxies=save_proxies, load_tasks=load_tasks)
    if status == 200:
        return redirect(url_for("proxy.proxy_page"))
    body = f'<section class="section" role="alert" data-proxy-result="{h(json.dumps(payload, ensure_ascii=False))}"><p>{h(payload["msg"])}</p></section>'
    for task in payload.get("references", []):
        body += f'<p><a class="btn btn-blue" href="/task/edit/{h(quote(task["id"], safe=""))}">设置任务：{h(task["name"])}</a></p>'
    if not payload.get("saved") and (status >= 500 or payload.get("confirm_required")):
        if payload.get("confirm_required"):
            data["confirm_retry"] = "1"
        fields = "".join(f'<input type="hidden" name="{key}" value="{h(data[key])}">'
                         for key in ("request_id", "version", "enabled", "confirm_retry") if key in data)
        body += f'''<form method="post" action="/proxy/{action}/{h(quote(proxy_id, safe=''))}">{fields}
<section class="section"><button class="btn btn-primary" type="submit">{'确认后重试' if payload.get('confirm_required') else '查询或重试原操作'}</button></section></form>'''
    body += '<a class="btn btn-gray" href="/proxy">查看当前代理并重新选择</a>'
    return layout("代理操作恢复", "proxy", body), status


@bp.route("/proxy/toggle/<proxy_id>", methods=["GET", "POST"])
def proxy_toggle(proxy_id):
    if request.method == "GET":
        return redirect(url_for("proxy.proxy_page"))
    return proxy_submit("toggle", proxy_id)


@bp.route("/proxy/delete/<proxy_id>", methods=["POST"])
def proxy_delete(proxy_id):
    return proxy_submit("delete", proxy_id)
