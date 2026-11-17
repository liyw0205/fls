import json
from urllib.parse import quote

from ._common import *
from ...notify_test_actions import submit_notification_test


@bp.route("/notify/test/<item_id>", methods=["GET", "POST"])
def notify_test(item_id):
    if request.method == "GET":
        return redirect(url_for("notify.notify_page"))
    data = dict(request.form.to_dict(), item_id=item_id)
    payload, status = submit_notification_test(data, sender=send_one)
    return notify_test_result(data, payload, status)


def notify_test_result(data, payload, status, prefix=""):
    item_id = data["item_id"]
    body = f'<section class="section" role="alert" data-notify-result="{h(json.dumps(payload, ensure_ascii=False))}"><p>{h(payload["msg"])}</p><p>{h(payload.get("detail", ""))}</p></section>'
    delivery_id = payload.get("delivery_id")
    if delivery_id:
        if payload.get("delivery_status") in ("failed", "unknown") and payload.get("history_available", True):
            try:
                row = next((row for row in load_notify_history() if row["id"] == delivery_id), None)
                if row and row["status"] in ("failed", "unknown"):
                    body += '<section class="section">' + notify_retry_form(row, "确认后重试此投递") + '</section>'
            except Exception:
                body += '<p>投递记录暂不可读取，请修复后查看记录再重试。</p>'
        body += '<a class="btn btn-blue" href="/notify/history">查看投递记录</a>'
    if not payload.get("resolved"):
        if payload.get("confirm_required"):
            data["confirm_retry"] = "1"
        fields = "".join(f'<input type="hidden" name="{key}" value="{h(data[key])}">'
                         for key in ("request_id", "version", "confirm_retry") if key in data)
        body += f'<form method="post" action="/notify/test/{h(quote(item_id, safe=""))}">{fields}<section class="section"><button class="btn btn-primary" type="submit">{"确认后继续测试" if payload.get("confirm_required") else "查询原测试结果"}</button></section></form>'
    body += '<a class="btn btn-gray" href="/notify">返回通知管理</a>'
    return layout("通知测试结果", "notify", prefix + body), status
