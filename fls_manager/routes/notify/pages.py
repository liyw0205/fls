from ._common import *


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
        <form class="inline-form" method="post" action="/notify/delete/{h(item_id)}">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定删除该通知吗？')">删除</button>
        </form>
    </td>
</tr>
"""

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">通知管理</div>
            <div class="help">
                可新增多个通知实例。任务里选择“使用全局默认通知”时，会使用下面保存的全局默认通知。
            </div>
        </div>
        <a class="btn btn-primary" href="/notify/new">新增通知</a>
    </div>
</div>

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

<div class="card">
    <div class="card-title">通知列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>名称</th>
                    <th>渠道</th>
                    <th>状态</th>
                    <th>全局默认</th>
                    <th>更新时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
</div>
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
