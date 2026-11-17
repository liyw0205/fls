from ._common import *
import json
from ...models import load_tasks
from ...config import save_config
from ...notify_actions import mutate_notification, notification_config, notification_version


def notify_submit(action, item_id=""):
    data = dict(request.form.to_dict(), action=action, item_id=item_id)
    if action == "default":
        data["default_ids"] = request.form.getlist("default_ids")
    payload, status = mutate_notification(data, load_tasks=load_tasks, save_config=save_config)
    if status == 200:
        return redirect(url_for("notify.notify_page"))
    body = f'<section class="section" role="alert" data-notify-result="{h(json.dumps(payload, ensure_ascii=False))}"><p>{h(payload["msg"])}</p></section>'
    for task in payload.get("references", []):
        body += f'<p><a class="btn btn-blue" href="/task/edit/{h(task["id"])}">设置任务：{h(task["name"])}</a></p>'
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
