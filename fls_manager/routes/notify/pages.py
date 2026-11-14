from ._common import *
from ...ui.components import page_header_card, table_card, empty_table_row


def _notify_history_status_badge(status):
    labels = {
        "running": ("发送中", "blue"),
        "succeeded": ("成功", "green"),
        "failed": ("失败", "red"),
        "unknown": ("结果未知", "orange"),
    }
    label, color = labels.get(status, ("结果未知", "orange"))
    return f'<span class="badge {color} status-badge status-{h(status)}" role="status">{label}</span>'


def notify_history_page(notice="", notice_kind="info"):
    status = request.args.get("status", "").strip()
    if status not in NOTIFY_HISTORY_STATUSES:
        status = ""

    raw_limit = request.args.get("limit", "50")
    try:
        limit = max(1, min(100, int(raw_limit)))
    except (TypeError, ValueError):
        limit = 50

    rows = ""
    history = load_notify_history(limit=limit, status=status)
    for item in history:
        item_id = item.get("id", "")
        message = item.get("message", "")
        if item.get("content_truncated"):
            message = f"{message}；正文已截断，重试使用保存的副本"
        if item.get("status") in ("failed", "unknown"):
            action = f'''<form class="inline-form" method="post" action="/notify/retry/{h(item_id)}">
                <button class="btn btn-orange" type="submit">重试发送</button>
            </form>'''
        else:
            action = '<span class="muted">-</span>'

        rows += f"""
<tr>
    <td>{h(item.get("updated_at") or item.get("created_at") or "-")}</td>
    <td>{h(item.get("source") or "manual")}</td>
    <td><b>{h(item.get("item_name") or item.get("item_id") or "-")}</b><br><span class="muted">{h(item.get("channel") or "-")}</span></td>
    <td>{h(item.get("title") or "-")}</td>
    <td>{_notify_history_status_badge(item.get("status"))}</td>
    <td>{h(item.get("attempt", 1))}</td>
    <td>{h(message or "-")}</td>
    <td>{action}</td>
</tr>
"""

    if not rows:
        rows = empty_table_row(8, "暂无通知投递记录", '<a class="btn btn-gray" href="/notify">返回通知管理</a>')

    notice_html = ""
    if notice:
        color = "red" if notice_kind == "error" else "green"
        notice_html = f'<div class="notice {color}" role="status">{h(notice)}</div>'

    options = "".join(
        f'<option value="{h(value)}" {"selected" if value == status else ""}>{h(label)}</option>'
        for value, label in (("", "全部状态"), ("succeeded", "成功"), ("failed", "失败"), ("unknown", "结果未知"), ("running", "发送中"))
    )
    header = page_header_card(
        "通知投递记录",
        "查看通知发送结果；失败或结果未知的记录可以使用当前渠道配置重试。",
        '<a class="btn btn-gray" href="/notify">返回通知管理</a>',
    )
    filter_html = f"""
<form method="get" class="section fls-form-section">
    <div class="form-grid">
        <div class="form-item">
            <label>状态</label>
            <select name="status">{options}</select>
        </div>
        <div class="form-item">
            <label>显示条数</label>
            <input name="limit" type="number" min="1" max="100" value="{limit}">
        </div>
    </div>
    <br><button class="btn btn-blue" type="submit">筛选记录</button>
</form>
"""
    table = table_card(
        "最近投递",
        ("时间", "来源", "通知渠道", "标题", "状态", "尝试", "结果", "操作"),
        rows,
    )
    return layout("通知投递记录", "notify", f"{header}{notice_html}{filter_html}{table}")


@bp.route("/notify/history")
def notify_history():
    return notify_history_page()


@bp.route("/notify/retry/<delivery_id>", methods=["POST"])
def notify_retry(delivery_id):
    try:
        ok, msg, _row = retry_notify_delivery(delivery_id)
    except Exception as exc:
        return notify_history_page(f"重试失败：{exc}", "error"), 500

    if ok:
        return notify_history_page(f"重试发送成功：{msg}", "success")
    if msg == "通知投递不存在":
        return notify_history_page(msg, "error"), 404
    if msg in ("通知投递正在发送", "通知投递已成功，无需重试"):
        return notify_history_page(msg, "error"), 409
    return notify_history_page(f"重试失败：{msg}", "error"), 500


@bp.route("/notify")
def notify_page():
    items = notify_items()
    defaults = set(default_notify_ids())

    rows = ""

    if not items:
        rows = empty_table_row(7, "暂无通知", '<a class="btn btn-primary" href="/notify/new">新增通知</a>')
    else:
        for item in items:
            item_id = item.get("id")
            cname = channel_name(item.get("channel"))
            log_mode = notify_log_mode_name(item)
            enabled_badge = (
                '<span class="badge green status-badge status-enabled" role="status">启用</span>'
                if item.get("enabled", True) else
                '<span class="badge gray status-badge status-disabled" role="status">禁用</span>'
            )
            default_badge = (
                '<span class="badge blue status-badge status-info" role="status">全局默认</span>'
                if item_id in defaults else
                '<span class="badge gray status-badge status-info" role="status">-</span>'
            )
            toggle_text = "停用通知" if item.get("enabled", True) else "启用通知"
            toggle_class = "btn-gray" if item.get("enabled", True) else "btn-primary"

            rows += f"""
<tr>
    <td><b>{h(item.get("name", ""))}</b></td>
    <td>{h(cname)}</td>
    <td>{h(log_mode)}</td>
    <td>{enabled_badge}</td>
    <td>{default_badge}</td>
    <td>{h(item.get("updated_at", "-"))}</td>
    <td>
        <div class="row-actions" aria-label="通知 {h(item.get('name', ''))} 行操作">
            <div class="row-actions-primary">
                <form class="inline-form" method="post" action="/notify/test/{h(item_id)}">
                    <button class="btn btn-orange" type="submit">发送测试通知</button>
                </form>
            </div>
            <div class="row-actions-secondary">
                <a class="btn btn-blue" href="/notify/edit/{h(item_id)}">编辑通知设置</a>
                <form class="inline-form" method="post" action="/notify/toggle/{h(item_id)}">
                    <button class="btn {toggle_class}" type="submit">{toggle_text}</button>
                </form>
            </div>
            <div class="row-actions-danger">
                <form class="inline-form" method="post" action="/notify/delete/{h(item_id)}">
                    <button class="btn btn-red" type="submit" onclick="return confirm('确定删除该通知吗？')">删除通知</button>
                </form>
            </div>
        </div>
    </td>
</tr>
"""

    header = page_header_card(
        "通知管理",
        """
                可新增多个通知实例。任务里选择“使用全局默认通知”时，会使用下面保存的全局默认通知。
            """,
        '<a class="btn btn-primary" href="/notify/new">新增通知</a><a class="btn btn-gray" href="/notify/history">投递记录</a>',
    )

    table = table_card(
        "通知列表",
        ("名称", "渠道", "通知模式", "状态", "全局默认", "更新时间", "操作"),
        rows,
    )

    body = f"""
{header}

<form method="post" action="/notify/default">
<section class="section fls-form-section" id="notify-defaults">
    <h2 class="section-title">全局默认通知</h2>
    <select name="default_ids" multiple size="6">{default_options(defaults)}</select>
    <div class="help">
        这里设置全局默认通知。任务选择“使用全局默认通知”时会使用这里的配置。
    </div>
    <br>
    <button class="btn btn-primary" type="submit">保存全局默认通知</button>
</section>
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
            send_notify_with_history(
                item,
                "FLS 通知测试",
                f"这是一条测试通知。\n时间：{now_str()}",
                source="test",
                sender=send_one,
            )

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
            send_notify_with_history(
                new_item,
                "FLS 通知测试",
                f"这是一条测试通知。\n时间：{now_str()}",
                source="test",
                sender=send_one,
            )

        return redirect(url_for("notify.notify_page"))

    url_channel = request.args.get("channel", "").strip()
    if url_channel in NOTIFY_CHANNELS:
        item = dict(item)
        item["channel"] = url_channel

    return notify_form(item)
