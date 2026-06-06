from ._common import *


@bp.route("/notify/test/<item_id>")
def notify_test(item_id):
    item = get_notify_item(item_id)

    if not item:
        abort(404)

    ok, msg = send_one(
        item,
        "FLS 通知测试",
        f"这是一条测试通知。\n时间：{now_str()}",
    )

    body = f"""
<div class="card">
    <div class="card-title">通知测试结果</div>
    <div class="help">
        通知：<b>{h(item.get("name"))}</b><br>
        渠道：<b>{h(channel_name(item.get("channel")))}</b><br>
        结果：<b>{"成功" if ok else "失败"}</b><br>
        返回：{h(msg)}
    </div>
    <br>
    <a class="btn btn-gray" href="/notify">返回通知管理</a>
</div>
"""
    return layout("通知测试", "notify", body)
