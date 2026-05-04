import uuid

from flask import Blueprint, request, redirect, url_for, abort

from ..ui.layout import layout
from ..utils import h, now_str
from ..notify import (
    NOTIFY_CHANNELS,
    notify_items,
    save_notify_items,
    enabled_notify_items,
    channel_name,
    get_notify_item,
    default_notify_ids,
    save_default_notify_ids,
    unique_notify_name,
    send_one,
)

bp = Blueprint("notify", __name__)


def notify_item_from_form(old=None):
    old = old or {}

    channel = request.form.get("channel", old.get("channel", "bark")).strip()
    if channel not in NOTIFY_CHANNELS:
        channel = "bark"

    meta = NOTIFY_CHANNELS[channel]

    config = {}
    for field, label, placeholder in meta.get("fields", []):
        config[field] = request.form.get(field, "")

    return {
        "id": old.get("id") or uuid.uuid4().hex,
        "name": unique_notify_name(channel, request.form.get("name", ""), old.get("id", "")),
        "channel": channel,
        "enabled": request.form.get("enabled", "1") == "1",
        "config": config,
        "created_at": old.get("created_at") or now_str(),
        "updated_at": now_str(),
    }


def channel_options(selected):
    options = ""

    for key, meta in NOTIFY_CHANNELS.items():
        s = "selected" if key == selected else ""
        options += f'<option value="{h(key)}" {s}>{h(meta.get("name", key))}</option>'

    return options


def default_options(selected_ids):
    selected_ids = set(selected_ids or [])
    options = ""

    for item in enabled_notify_items():
        s = "selected" if item.get("id") in selected_ids else ""
        options += f'<option value="{h(item.get("id"))}" {s}>{h(item.get("name"))} [{h(channel_name(item.get("channel")))}]</option>'

    if not options:
        options = '<option value="" disabled>暂无已启用通知</option>'

    return options


def notify_form(item=None):
    if item is None:
        item = {
            "id": "",
            "name": "",
            "channel": request.args.get("channel", "bark") or "bark",
            "enabled": True,
            "config": {},
        }

    channel = item.get("channel", "bark")
    if channel not in NOTIFY_CHANNELS:
        channel = "bark"

    meta = NOTIFY_CHANNELS[channel]
    config = item.get("config", {}) or {}
    checked = "checked" if item.get("enabled", True) else ""

    quick_links = ""
    for key, m in NOTIFY_CHANNELS.items():
        if item.get("id"):
            url = f"/notify/edit/{item.get('id')}?channel={key}"
        else:
            url = f"/notify/new?channel={key}"

        cls = "btn-primary" if key == channel else "btn-gray"
        quick_links += f'<a class="btn {cls}" href="{h(url)}">{h(m.get("name", key))}</a>'

    fields_html = ""

    for field, label, placeholder in meta.get("fields", []):
        value = config.get(field, "")
        if field in ("WEBHOOK_HEADERS", "WEBHOOK_BODY"):
            fields_html += f"""
<div class="form-item">
    <label>{h(label)}</label>
    <textarea name="{h(field)}" style="min-height:110px;" placeholder="{h(placeholder)}">{h(value)}</textarea>
</div>
"""
        else:
            fields_html += f"""
<div class="form-item">
    <label>{h(label)}</label>
    <input name="{h(field)}" value="{h(value)}" placeholder="{h(placeholder)}">
</div>
"""

    if not fields_html:
        fields_html = '<div class="help">该渠道无需额外配置。</div>'

    title = "编辑通知" if item.get("id") else "新增通知"

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">{h(title)}</div>
    <div class="help">通知名称留空时会自动使用渠道名；若重名会自动追加序号。</div>
</div>

<div class="card">
    <div class="card-title">选择通知渠道</div>
    <div class="action-row">{quick_links}</div>
</div>

<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>通知名称</label>
            <input name="name" value="{h(item.get("name", ""))}" placeholder="例如：微信机器人">
        </div>
        <div class="form-item">
            <label>通知渠道</label>
            <select name="channel">{channel_options(channel)}</select>
        </div>
    </div>
    <br>
    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用此通知
    </label>
</div>

<div class="card">
    <div class="card-title">{h(meta.get("name", channel))} 配置</div>
    <div class="form-grid">{fields_html}</div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit" name="action" value="save">保存</button>
    <button class="btn btn-orange" type="submit" name="action" value="test">保存并测试</button>
    <a class="btn btn-gray" href="/notify">返回通知管理</a>
</div>
</form>
"""
    return layout("通知配置", "notify", body)


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


@bp.route("/notify/default", methods=["POST"])
def notify_default_save():
    save_default_notify_ids(request.form.getlist("default_ids"))
    return redirect(url_for("notify.notify_page"))


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


@bp.route("/notify/delete/<item_id>")
def notify_delete(item_id):
    items = [x for x in notify_items() if x.get("id") != item_id]
    save_notify_items(items)
    save_default_notify_ids(default_notify_ids())
    return redirect(url_for("notify.notify_page"))


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
