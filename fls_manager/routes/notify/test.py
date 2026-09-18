from ._common import *
from ...ui.components import table_card


@bp.route("/notify/test/<item_id>", methods=["GET", "POST"])
def notify_test(item_id):
    item = get_notify_item(item_id)

    if not item:
        abort(404)

    ok, msg = send_one(
        item,
        "FLS 通知测试",
        f"这是一条测试通知。\n时间：{now_str()}",
    )

    status_badge = (
        '<span class="badge green status-badge status-success" role="status">成功</span>'
        if ok else
        '<span class="badge red status-badge status-error" role="status">失败</span>'
    )
    result_text = (
        str(msg)
        if ok else
        f"{msg}。下一步：检查通知渠道设置和网络后重试"
    )
    rows = f"""
<tr>
    <td><b>通知</b></td>
    <td>{h(item.get("name"))}</td>
</tr>
<tr>
    <td><b>渠道</b></td>
    <td>{h(channel_name(item.get("channel")))}</td>
</tr>
<tr>
    <td><b>结果</b></td>
    <td>{status_badge}</td>
</tr>
<tr>
    <td><b>返回</b></td>
    <td>{h(result_text)}</td>
</tr>
"""
    body = table_card(
        "通知测试结果",
        ["项目", "值"],
        rows,
        actions_html='<a class="btn btn-gray" href="/notify">返回通知管理</a>',
    )
    return layout("通知测试", "notify", body)
