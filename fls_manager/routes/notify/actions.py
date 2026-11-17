from ._common import *
import json
from ...models import load_tasks
from ...config import save_config
from ...notify_actions import apply_notification, notification_config, notification_version


def notify_submit(action, item_id=""):
    data = dict(request.form.to_dict(), action=action, item_id=item_id)
    if action == "default":
        data["default_ids"] = request.form.getlist("default_ids")
    if action in ("new", "edit"):
        data.setdefault("enabled", "0")
        data["test"] = request.form.get("action", "save") == "test"
        if request.form.get("action", "save") not in ("save", "test"):
            data["test"] = "invalid"
    payload, status = apply_notification(data, load_tasks=load_tasks, save_config=save_config, sender=send_one)
    if status == 200 and not payload.get("test_result"):
        return redirect(url_for("notify.notify_page"))
    state = {key: value for key, value in payload.items() if key != "current"}
    body = f'<section class="section" role="alert" data-notify-result="{h(json.dumps(state, ensure_ascii=False))}"><p>{h(payload["msg"])}</p></section>'
    for task in payload.get("references", []):
        body += f'<p><a class="btn btn-blue" href="/task/edit/{h(task["id"])}">设置任务：{h(task["name"])}</a></p>'
    if action in ("new", "edit"):
        if payload.get("saved"):
            body += f'<a class="btn btn-blue" href="/notify/edit/{h(payload["item_id"])}">查看已保存通知</a>'
            if payload.get("test_result"):
                from .test import notify_test_result
                return notify_test_result(payload["test_request"], payload["test_result"], status, prefix=body)
            if payload.get("test_request"):
                test = payload["test_request"]
                fields = "".join(f'<input type="hidden" name="{key}" value="{h(test[key])}">' for key in ("request_id", "version"))
                body += f'<form method="post" action="/notify/test/{h(test["item_id"])}">{fields}<section class="section"><button class="btn btn-primary" type="submit">继续已保存通知的测试</button></section></form>'
            return layout("通知保存恢复", "notify", body), status
        current, conflict = payload.get("current"), payload.get("conflict", False)
        current_version = payload.get("current_version", "")
        if payload.get("invalid_field") == "version":
            try:
                cfg = notification_config()
                current = next((row for row in cfg.get("notify_items", []) if row["id"] == item_id), None)
                current_version, conflict = notification_version(cfg, item_id), True
            except Exception:
                pass
        fields = {field for meta in NOTIFY_CHANNELS.values() for field, *_ in meta["fields"]}
        draft = {"id": item_id, "name": data.get("name", ""), "channel": data.get("channel", "bark"),
                 "log_mode": data.get("log_mode", "full"), "enabled": data["enabled"] == "1",
                 "config": {field: data.get(field, "") for field in fields},
                 "_version": data.get("version", ""), "_request_id": data.get("request_id", ""),
                 "_confirm_retry": payload.get("confirm_required", False)}
        if conflict:
            if current:
                body += f'<section class="section"><p>当前设置：{h(current.get("name"))}，{h(channel_name(current.get("channel")))}，{"启用" if current.get("enabled", True) else "停用"}</p><a class="btn btn-blue" href="/notify/edit/{h(current["id"])}">查看当前完整通知设置</a></section>'
            draft.update(_version=current_version, _request_id=uuid.uuid4().hex, _confirm_retry=False)
        elif payload.get("saved") is False and status < 500 and not payload.get("confirm_required"):
            draft["_request_id"] = uuid.uuid4().hex
        if (status == 404 or conflict and current is None) and action == "edit":
            action = "new"
            draft.update(id="", _request_id=uuid.uuid4().hex, _confirm_retry=False)
            body += '<p>原通知已删除，可明确将草稿保存为新通知。</p>'
        return notify_form(draft, mode=action, recovery_html=body, merge=conflict), status
    if action == "default" and status in (400, 409) and payload.get("resolved"):
        try:
            cfg = notification_config()
            selected = set(data["default_ids"])
            options = "".join(f'<option value="{h(item["id"])}" {"selected" if item["id"] in selected else ""}>{h(item.get("name") or item["id"])}</option>'
                              for item in cfg["notify_items"] if item.get("enabled", True))
            body += f'<form method="post" action="/notify/default"><input type="hidden" name="request_id" value="{uuid.uuid4().hex}"><input type="hidden" name="version" value="{notification_version(cfg)}"><section class="section"><label>重新核对默认通知<select name="default_ids" multiple size="6" aria-label="重新核对默认通知">{options}</select></label><button class="btn btn-primary" type="submit">确认后保存默认选择</button></section></form>'
        except Exception:
            pass
    if not payload.get("saved") and (status >= 500 or payload.get("confirm_required")):
        if payload.get("confirm_required"):
            data["confirm_retry"] = "1"
        fields = "".join(f'<input type="hidden" name="{key}" value="{h(data[key])}">'
                         for key in ("request_id", "version", "enabled", "confirm_retry") if key in data)
        fields += "".join(f'<input type="hidden" name="default_ids" value="{h(value)}">' for value in data.get("default_ids", []))
        url = "/notify/default" if action == "default" else f"/notify/{action}/{item_id}"
        body += f'<form method="post" action="{h(url)}">{fields}<section class="section"><button class="btn btn-primary" type="submit">{"确认后重试" if payload.get("confirm_required") else "查询或重试原操作"}</button></section></form>'
    body += '<a class="btn btn-gray" href="/notify">查看当前通知并重新选择</a>'
    return layout("通知设置恢复", "notify", body), status


@bp.route("/notify/default", methods=["POST"])
def notify_default_save():
    return notify_submit("default")


@bp.route("/notify/toggle/<item_id>", methods=["GET", "POST"])
def notify_toggle(item_id):
    if request.method == "GET":
        return redirect(url_for("notify.notify_page"))
    return notify_submit("toggle", item_id)


@bp.route("/notify/delete/<item_id>", methods=["POST"])
def notify_delete(item_id):
    return notify_submit("delete", item_id)
