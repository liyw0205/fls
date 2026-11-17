from .bp import bp
import uuid

from flask import request, redirect, url_for, abort

from ...ui.layout import layout
from ...utils import h, now_str
from ...ui.components import page_header
from ...sensitive import is_sensitive_key
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
    normalize_notify_log_mode,
    notify_log_mode_name,
    send_one,
    create_notify_delivery,
    finish_notify_delivery,
    load_notify_history,
    retry_notify_delivery,
    NOTIFY_HISTORY_STATUSES,
)


def send_notify_with_history(item, title, content, source="manual", sender=None):
    try:
        delivery_id = create_notify_delivery(item, title, content, source=source)
    except Exception as exc:
        return False, f"投递记录保存失败：{exc}", None

    try:
        ok, msg = (sender or send_one)(item, title, content)
    except Exception as exc:
        ok, msg = False, str(exc)

    try:
        finish_notify_delivery(delivery_id, ok, msg)
    except Exception as exc:
        msg = f"{msg}；投递结果保存失败：{exc}"

    return ok, msg, delivery_id

def channel_options(selected):
    options = f'<option value="{h(selected)}" selected>无效渠道，请重新选择</option>' if selected not in NOTIFY_CHANNELS else ""

    for key, meta in NOTIFY_CHANNELS.items():
        s = "selected" if key == selected else ""
        options += f'<option value="{h(key)}" {s}>{h(meta.get("name", key))}</option>'

    return options


def notify_retry_form(row, label="重试发送"):
    from ...notify_retry_actions import retry_request
    fields = "".join(f'<input type="hidden" name="{key}" value="{h(value)}">' for key, value in retry_request(row).items())
    return f'''<form class="inline-form" method="post" action="/notify/retry/{h(row["id"])}">
        {fields}
        <button class="btn btn-orange" type="submit">{h(label)}</button>
    </form>'''


def default_options(selected_ids, items=None):
    selected_ids = set(selected_ids or [])
    options = ""

    for item in enabled_notify_items() if items is None else items:
        if not item.get("enabled", True):
            continue
        s = "selected" if item.get("id") in selected_ids else ""
        options += f'<option value="{h(item.get("id"))}" {s}>{h(item.get("name"))} [{h(channel_name(item.get("channel")))}]</option>'

    if not options:
        options = '<option value="" disabled>暂无已启用通知</option>'

    return options


def notify_form(item=None, notice="", *, mode=None, recovery_html="", merge=False):
    if item is None:
        item = {
            "id": "",
            "name": "",
            "channel": request.args.get("channel", "bark") or "bark",
            "enabled": True,
            "config": {},
        }

    channel = item.get("channel", "bark")
    config = item.get("config", {}) or {}
    checked = "checked" if item.get("enabled", True) else ""
    log_mode = item.get("log_mode", "full")
    full_log_checked = "selected" if log_mode == "full" else ""
    trimmed_log_checked = "selected" if log_mode == "trimmed" else ""

    fields_html = ""
    for key, meta in NOTIFY_CHANNELS.items():
        hidden = "" if key == channel else "hidden"
        disabled = "" if key == channel else "disabled"
        fields_html += f'<div data-notify-channel="{h(key)}" {hidden}><h2 class="section-title">{h(meta["name"])} 通知渠道设置</h2><div class="form-grid">'
        for field, label, placeholder in meta.get("fields", []):
            value = config.get(field, "")
            if field in ("WEBHOOK_HEADERS", "WEBHOOK_BODY"):
                fields_html += f"""
<div class="form-item">
    <label>{h(label)}</label>
    <textarea name="{h(field)}" {disabled} style="min-height:110px;" placeholder="{h(placeholder)}" aria-label="{h(label)}">{h(value)}</textarea>
</div>
"""
            else:
                input_type = "password" if is_sensitive_key(field) else "text"
                autocomplete = ' autocomplete="new-password"' if input_type == "password" else ""
                fields_html += f"""
<div class="form-item">
    <label>{h(label)}</label>
            <input name="{h(field)}" {disabled} type="{input_type}" value="{h(value)}" placeholder="{h(placeholder)}" aria-label="{h(label)}"{autocomplete}>
</div>
"""
        fields_html += '</div></div>'

    mode = mode or ("edit" if item.get("id") else "new")
    title = "编辑通知" if mode == "edit" else "新增通知"
    header = page_header(
        "通知渠道设置",
        help_html="设置通知渠道、启停状态和测试动作。",
        actions_html='<a class="btn btn-gray" href="/notify">返回通知管理</a>',
    )
    if notice:
        header += f'<section class="section" role="alert"><p>{h(notice)}</p><a class="btn btn-gray" href="/notify">查看当前通知设置</a></section>'
    header += recovery_html
    version_field = f'<input type="hidden" name="request_id" value="{h(item.get("_request_id", uuid.uuid4().hex))}">'
    if mode == "edit":
        from ...notify_actions import notification_config, notification_version
        version = item.get("_version")
        if version is None:
            version = notification_version(notification_config(), item["id"])
        version_field += f'<input type="hidden" name="version" value="{h(version)}">'
    if item.get("_confirm_retry"):
        version_field += '<input type="hidden" name="confirm_retry" value="1">'
    submit_label = "确认合并后保存通知" if merge else "确认后重试保存" if item.get("_confirm_retry") else "保存通知设置"
    action_url = "/notify/new" if mode == "new" else "/notify/edit/" + item["id"]

    body = f"""
{header}
<form method="post" action="{h(action_url)}" data-no-float-actions="{1 if recovery_html else 0}">
{version_field}
<section class="section fls-form-section">
    <h2 class="section-title">{h(title)}</h2>
    <div class="help">通知名称留空时会自动使用渠道名；若重名会自动追加序号。</div>
</section>

<section class="section fls-form-section">
    <div class="form-grid">
        <div class="form-item">
            <label>通知名称</label>
            <input name="name" value="{h(item.get("name", ""))}" placeholder="例如：微信机器人" aria-label="通知名称">
        </div>
        <div class="form-item">
            <label>通知渠道</label>
            <select name="channel" aria-label="通知渠道" onchange="flsNotifyChannelChanged(this)">{channel_options(channel)}</select>
        </div>
        <div class="form-item">
            <label>通知模式</label>
            <select name="log_mode" aria-label="通知模式">
                {f'<option value="{h(log_mode)}" selected>无效模式，请重新选择</option>' if log_mode not in ('full', 'trimmed') else ''}
                <option value="full" {full_log_checked}>完整日志</option>
                <option value="trimmed" {trimmed_log_checked}>裁剪日志</option>
            </select>
            <div class="help">完整日志会每 2000 字符继续发送；裁剪日志超过 4000 字符时只发送开头 2000 字符和末尾 1500 字符。</div>
        </div>
    </div>
    <br>
    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用此通知
    </label>
</section>

<section class="section fls-form-section">
    {fields_html}
</section>

<section class="section fls-form-section fls-save-section">
    <button class="btn btn-primary" type="submit" name="action" value="save">{submit_label}</button>
    <button class="btn btn-orange" type="submit" name="action" value="test">保存并发送测试通知</button>
    <a class="btn btn-gray" href="/notify">返回通知管理</a>
</section>
</form>
<script>
function flsNotifyChannelChanged(select){{
    select.form.querySelectorAll('[data-notify-channel]').forEach(group=>{{
        const active=group.dataset.notifyChannel===select.value;
        group.hidden=!active;
        group.querySelectorAll('input,textarea').forEach(field=>field.disabled=!active);
    }});
}}
</script>
"""
    return layout("通知渠道设置", "notify", body)
