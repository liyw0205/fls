from ._common import *
from ...ui.components import page_header_card, table_card


@bp.route("/notify")
def notify_page():
    items = notify_items()
    defaults = set(default_notify_ids())

    rows = ""

    if not items:
        rows = '<tr><td colspan="6">暂无通知，请点击新增通知</td></tr>'
    else:
        for item in items:
            item_id = item.get("id")
            cname = channel_name(item.get("channel"))
            enabled_badge = '<span class="badge green">启用</span>' if item.get("enabled", True) else '<span class="badge gray">禁用</span>'
            default_badge = '<span class="badge blue">全局默认</span>' if item_id in defaults else '<span class="badge gray">-</span>'
            toggle_text = "禁用" if item.get("enabled", True) else "启用"
            toggle_class = "btn-gray" if item.get("enabled", True) else "btn-primary"

            rows += f"""
<tr>
    <td><b>{h(item.get("name", ""))}</b></td>
    <td>{h(cname)}</td>
    <td>{enabled_badge}</td>
    <td>{default_badge}</td>
    <td>{h(item.get("updated_at", "-"))}</td>
    <td>
        <a class="btn btn-orange" href="/notify/test/{h(item_id)}">测试</a>
        <a class="btn btn-blue" href="/notify/edit/{h(item_id)}">编辑</a>
        <a class="btn {toggle_class}" href="/notify/toggle/{h(item_id)}">{toggle_text}</a>
        <a class="btn btn-red" href="/notify/delete/{h(item_id)}" onclick="return confirm('确定删除该通知吗？')">删除</a>
    </td>
</tr>
"""

    header = page_header_card(
        "通知管理",
        """
                可新增多个通知实例。任务里选择“使用全局默认通知”时，会使用下面保存的全局默认通知。
            """,
        '<a class="btn btn-primary" href="/notify/new">新增通知</a>',
    )

    table = table_card(
        "通知列表",
        ("名称", "渠道", "状态", "全局默认", "更新时间", "操作"),
        rows,
    )

    body = f"""
{header}

<form method="post" action="/notify/default">
<div class="card">
    <div class="card-title">全局默认通知</div>
    <select name="default_ids" multiple size="6">{default_options(defaults)}</select>
    <div class="help">
        这里设置全局默认通知。任务选择“使用全局默认通知”时会使用这里的配置。
    </div>
    <br>
    <button class="btn btn-primary" type="submit">保存全局默认通知</button>
</div>
</form>

{table}
"""
    return layout("通知管理", "notify", body)


@bp.route("/notify/new", methods=["GET", "POST"])
def notify_new():
    if request.method == "POST":
        item = notify_item_from_form()
        items = notify_items()
        items.append(item)
        save_notify_items(items)

        if request.form.get("action") == "test":
            send_one(item, "FLS 通知测试", f"这是一条测试通知。\n时间：{now_str()}")

        return redirect(url_for("notify.notify_page"))

    return notify_form()


@bp.route("/notify/edit/<item_id>", methods=["GET", "POST"])
def notify_edit(item_id):
    items = notify_items()
    item = None

    for one in items:
        if one.get("id") == item_id:
            item = one
            break

    if not item:
        abort(404)

    if request.method == "POST":
        new_item = notify_item_from_form(item)

        for idx, one in enumerate(items):
            if one.get("id") == item_id:
                items[idx] = new_item
                break

        save_notify_items(items)

        if request.form.get("action") == "test":
            send_one(new_item, "FLS 通知测试", f"这是一条测试通知。\n时间：{now_str()}")

        return redirect(url_for("notify.notify_page"))

    url_channel = request.args.get("channel", "").strip()
    if url_channel in NOTIFY_CHANNELS:
        item = dict(item)
        item["channel"] = url_channel

    return notify_form(item)
