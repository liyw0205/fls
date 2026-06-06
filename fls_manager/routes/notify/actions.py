from ._common import *


@bp.route("/notify/default", methods=["POST"])
def notify_default_save():
    save_default_notify_ids(request.form.getlist("default_ids"))
    return redirect(url_for("notify.notify_page"))


@bp.route("/notify/toggle/<item_id>")
def notify_toggle(item_id):
    items = notify_items()

    for item in items:
        if item.get("id") == item_id:
            item["enabled"] = not item.get("enabled", True)
            item["updated_at"] = now_str()
            break

    save_notify_items(items)
    save_default_notify_ids(default_notify_ids())
    return redirect(url_for("notify.notify_page"))


@bp.route("/notify/delete/<item_id>", methods=["POST"])
def notify_delete(item_id):
    items = [x for x in notify_items() if x.get("id") != item_id]
    save_notify_items(items)
    save_default_notify_ids(default_notify_ids())
    return redirect(url_for("notify.notify_page"))
