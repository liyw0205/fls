from .bp import bp
import uuid

from flask import request, redirect, url_for, abort

from ...ui.layout import layout
from ...utils import h, now_str
from ...notify import (
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
