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
