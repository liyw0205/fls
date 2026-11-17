from ._common import *
import json
from urllib.parse import quote
from ...models import load_tasks
from ...proxy_actions import mutate_proxy, proxy_version


def proxy_submit(action, proxy_id):
    data = dict(request.form.to_dict(), action=action, proxy_id=proxy_id)
    if action in ("new", "edit"):
        data.setdefault("enabled", "0")
    payload, status = mutate_proxy(data, load_proxies=load_proxies,
        save_proxies=save_proxies, load_tasks=load_tasks)
    if status == 200:
        return redirect(url_for("proxy.proxy_page"))
    state = {key: value for key, value in payload.items() if key != "current"}
    body = f'<section class="section" role="alert" data-proxy-result="{h(json.dumps(state, ensure_ascii=False))}"><p>{h(payload["msg"])}</p></section>'
    if action in ("new", "edit"):
        if payload.get("saved"):
            body += f'<a class="btn btn-primary" href="/proxy/edit/{h(payload.get("proxy_id", proxy_id))}">查看已保存代理</a>'
        else:
            draft = {key: data.get(key, "") for key in ("name", "type", "host", "port", "username", "password", "url")}
            draft.update(id=proxy_id, enabled=data["enabled"] == "1", _version=data.get("version", ""),
                         _request_id=data.get("request_id", ""), _confirm_retry=payload.get("confirm_required"))
            current = payload.get("current")
            conflict = payload.get("conflict", False)
            if payload.get("invalid_field") == "version":
                try:
                    current = next((p for p in load_proxies() if p["id"] == proxy_id), None)
                    conflict = current is not None
                except Exception:
                    pass
            if conflict and current is not None:
                body += f'<section class="section"><p>当前设置：{h(current.get("name"))}，{h(current.get("type"))}，{h(current.get("host"))}:{h(current.get("port"))}，{"启用" if current.get("enabled") else "停用"}</p><a class="btn btn-blue" href="/proxy/edit/{h(current["id"])}">查看当前完整设置</a></section>'
            if conflict:
                draft.update(_version=proxy_version(current), _request_id=uuid.uuid4().hex, _confirm_retry=False)
                if action == "new":
                    body += '<p>请先核对原提交对应的代理；确认仍需单独创建时再保存此草稿。</p>'
            elif payload.get("saved") is False and status < 500 and not payload.get("confirm_required"):
                draft["_request_id"] = uuid.uuid4().hex
            if (status == 404 or conflict and current is None) and action == "edit":
                action = "new"
                draft.update(id="", _request_id=uuid.uuid4().hex, _confirm_retry=False)
                body += '<p>原代理已删除，可明确将草稿保存为新代理。</p>'
            return proxy_form(draft, mode=action, merge=bool(conflict), recovery_html=body), status
        return layout("代理保存恢复", "proxy", body), status
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
