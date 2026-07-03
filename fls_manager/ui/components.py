from ..utils import h


def page_header_card(title, help_html="", actions_html=""):
    actions_block = ""
    if str(actions_html or "").strip():
        actions_block = f'<div class="action-row">{actions_html}</div>'

    help_block = ""
    if str(help_html or "").strip():
        help_block = f'<div class="help">{help_html}</div>'

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">{h(title)}</div>
            {help_block}
        </div>
        {actions_block}
    </div>
</div>
"""


def table_card(title, headers, rows_html):
    head_html = "".join(f"<th>{h(item)}</th>" for item in headers)

    return f"""
<div class="card">
    <div class="card-title">{h(title)}</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>{head_html}</tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>
    </div>
</div>
"""


def message_card(message, kind="info", strong=False, title=""):
    text = str(message or "").strip()

    if not text:
        return ""

    title_text = str(title or "").strip()
    title_html = f'<div class="card-title">{h(title_text)}</div>' if title_text else ""

    colors = {
        "success": "#18a058",
        "error": "#dc2626",
        "info": "#6b7280",
    }
    color = colors.get(kind, colors["info"])
    weight = "font-weight:800;" if strong else ""

    return f"""
<div class="card">
    {title_html}
    <div class="help" style="color:{color};{weight}">{h(text)}</div>
</div>
"""


def summary_item(label, value):
    return f"""
<div class="fls-summary-item">
    <div class="fls-summary-label">{h(label)}</div>
    <div class="fls-summary-num">{h(value)}</div>
</div>
"""


def pagination_card(
    page,
    pages,
    href_for=None,
    onclick_for=None,
    page_label="第",
):
    if int(pages or 0) <= 1:
        return ""

    page = max(1, min(int(page), int(pages)))
    pages = int(pages)

    def page_btn(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"

        if onclick_for:
            return (
                f'<button class="btn {cls}" type="button" '
                f'onclick="{h(onclick_for(int(p)))}">{h(text)}</button>'
            )

        return f'<a class="btn {cls}" href="{h(href_for(int(p)))}">{h(text)}</a>'

    items = [
        page_btn(page - 1, "上一页", disabled=(page <= 1))
    ]

    show = {1, pages}

    for p in range(page - 2, page + 3):
        if 1 <= p <= pages:
            show.add(p)

    last = 0

    for p in sorted(show):
        if last and p - last > 1:
            items.append(
                '<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>'
            )

        items.append(page_btn(p, active=(p == page)))
        last = p

    items.append(
        page_btn(page + 1, "下一页", disabled=(page >= pages))
    )

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            {h(page_label)} <b>{page}</b> / <b>{pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
</div>
"""
