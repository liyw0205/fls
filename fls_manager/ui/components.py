from ..utils import h


def page_header(title, help_html="", actions_html="", eyebrow="", back_html=""):
    """Render the semantic page heading shared by full-page workflows."""
    eyebrow_html = f'<div class="page-eyebrow">{h(eyebrow)}</div>' if str(eyebrow or "").strip() else ""
    help_block = f'<p class="page-description">{help_html}</p>' if str(help_html or "").strip() else ""
    actions_block = f'<div class="page-header-actions">{actions_html}</div>' if str(actions_html or "").strip() else ""
    back_block = f'<div class="page-header-back">{back_html}</div>' if str(back_html or "").strip() else ""
    return f"""
<header class="page-header">
    <div class="page-header-copy">
        {back_block}
        {eyebrow_html}
        <h1 class="page-title">{h(title)}</h1>
        {help_block}
    </div>
    {actions_block}
</header>
"""


def section(title="", content_html="", actions_html="", section_id="", class_name=""):
    """Render a flat content section; repeated entities belong inside content_html."""
    id_attr = f' id="{h(section_id)}"' if str(section_id or "").strip() else ""
    cls = "section" + (f" {h(class_name)}" if str(class_name or "").strip() else "")
    title_html = f'<h2 class="section-title">{h(title)}</h2>' if str(title or "").strip() else ""
    toolbar = f'<div class="section-toolbar">{actions_html}</div>' if str(actions_html or "").strip() else ""
    heading = f'<div class="section-header">{title_html}{toolbar}</div>' if title_html or toolbar else ""
    return f'<section class="{cls}"{id_attr}>{heading}{content_html}</section>'


def data_toolbar(content_html="", toolbar_id=""):
    id_attr = f' id="{h(toolbar_id)}"' if str(toolbar_id or "").strip() else ""
    return f'<div class="data-toolbar"{id_attr}>{content_html}</div>'


def row_actions(primary_html="", secondary_html="", danger_html="", label="行操作"):
    primary = f'<div class="row-actions-primary">{primary_html}</div>' if str(primary_html or "").strip() else ""
    secondary = f'<div class="row-actions-secondary">{secondary_html}</div>' if str(secondary_html or "").strip() else ""
    danger = f'<div class="row-actions-danger">{danger_html}</div>' if str(danger_html or "").strip() else ""
    return f'<div class="row-actions" aria-label="{h(label)}">{primary}{secondary}{danger}</div>'


def detail_disclosure(summary, content_html, open=False, class_name=""):
    cls = "detail-disclosure" + (f" {h(class_name)}" if str(class_name or "").strip() else "")
    open_attr = " open" if open else ""
    return f'<details class="{cls}"{open_attr}><summary>{h(summary)}</summary><div class="detail-disclosure-content">{content_html}</div></details>'


def status_badge(status, label=None, tone=None):
    text = str(label if label is not None else status or "-")
    tone = str(tone or status or "neutral").lower().replace(" ", "-")
    return f'<span class="status-badge status-{h(tone)}" role="status">{h(text)}</span>'


def empty_state(message, action_html="", title=""):
    title_html = f'<h2 class="empty-state-title">{h(title)}</h2>' if str(title or "").strip() else ""
    action_block = f'<div class="empty-state-actions">{action_html}</div>' if str(action_html or "").strip() else ""
    return f'<div class="empty-state">{title_html}<p>{h(message)}</p>{action_block}</div>'


def empty_table_row(colspan, message, action_html="", title="", row_id="", style=""):
    """Keep an empty table accessible while reusing the shared empty-state UI."""
    try:
        span = max(1, int(colspan))
    except (TypeError, ValueError):
        span = 1
    id_attr = f' id="{h(row_id)}"' if str(row_id or "").strip() else ""
    style_attr = f' style="{h(style)}"' if str(style or "").strip() else ""
    return f'<tr class="empty-state-row"{id_attr}{style_attr}><td colspan="{span}">{empty_state(message, action_html, title)}</td></tr>'


def danger_confirm(action_html, description="", confirm_label="确认危险操作"):
    description_html = f'<p class="danger-confirm-description">{h(description)}</p>' if str(description or "").strip() else ""
    return f'<div class="danger-confirm" data-confirm-label="{h(confirm_label)}">{description_html}{action_html}</div>'


def page_header_card(title, help_html="", actions_html="", content_style=""):
    actions_block = ""
    if str(actions_html or "").strip():
        actions_block = f'<div class="action-row">{actions_html}</div>'

    content_style_attr = (
        f' style="{h(content_style)}"' if str(content_style or "").strip() else ""
    )

    help_block = ""
    if str(help_html or "").strip():
        help_block = f"""
    <div class="help">{help_html}</div>
    <br>"""

    return f"""
<header class="page-header page-header-card">
        <div class="page-header-copy"{content_style_attr}>
            <div class="card-title">{h(title)}</div>
            <h1 class="page-title">{h(title)}</h1>
            {help_block}
        </div>
        <div class="page-header-actions">{actions_block}</div>
    </header>
"""


def table_card(
    title,
    headers,
    rows_html,
    help_html="",
    actions_html="",
    table_id="",
    section_id="",
    class_name="",
):
    head_html = "".join(f"<th>{h(item)}</th>" for item in headers)
    table_id_attr = f' id="{h(table_id)}"' if str(table_id or "").strip() else ""

    help_block = ""
    if str(help_html or "").strip():
        help_block = f'<div class="help">{help_html}</div>'

    actions_block = ""
    if str(actions_html or "").strip():
        actions_block = f"""
    <br>
    <div class="action-row">{actions_html}</div>"""

    section_class = "section data-table-section"
    if str(class_name or "").strip():
        section_class += " " + h(class_name)
    section_id_attr = f' id="{h(section_id)}"' if str(section_id or "").strip() else ""

    return f"""
<section class="{section_class}"{section_id_attr}>
    <h2 class="section-title">{h(title)}</h2>
    {help_block}
    <div class="table-wrap data-table">
        <table{table_id_attr}{' class="data-table"' if not table_id_attr else ''}>
            <thead>
                <tr>{head_html}</tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>
    </div>
    {actions_block}
    </section>
"""


def message_card(message, kind="info", strong=False, title=""):
    text = str(message or "").strip()

    if not text:
        return ""

    if kind == "error" and "下一步" not in text:
        text = text.rstrip("。") + "。下一步：返回上一页检查输入、权限和服务状态后重试"

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
    <div class="feedback feedback-{h(kind if kind in colors else 'info')}" role="status">
        <div class="help" style="color:{color};{weight}">{h(text)}</div>
    </div>
</div>
"""


def code_card(title, code_html, help_html="", actions_html=""):
    help_block = ""
    if str(help_html or "").strip():
        help_block = f"""
    <div class="help">{help_html}</div>
    <br>"""

    actions_block = ""
    if str(actions_html or "").strip():
        actions_block = f"""
    <br>
    <div class="action-row">{actions_html}</div>"""

    return f"""
<div class="card">
    <div class="card-title">{h(title)}</div>
    {help_block}
    <div class="code">
{code_html}
    </div>
    {actions_block}
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
    <nav class="pagination" aria-label="分页导航">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            {h(page_label)} <b>{page}</b> / <b>{pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
    </nav>
</div>
"""
