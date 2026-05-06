from ..utils import h
from ..proxy import proxy_select_options
from .tasks import (
    online_script_task_crons,
    online_task_cron_vars,
    guess_task_command,
    task_vars_summary,
    script_has_task,
)
from .install import (
    online_script_target,
    get_running_install_by_script_id,
    script_has_install,
    script_has_doc,
)


def _task_preview_text(task_crons, item, limit=3):
    if not task_crons:
        return "-"

    parts = []

    for idx, tc in enumerate(task_crons[:limit], 1):
        tname = str(tc.get("name") or f"任务{idx}").strip()
        tcron = str(tc.get("cron") or "手动").strip()
        parts.append(f"{tname}：{tcron}")

    more = len(task_crons) - limit

    if more > 0:
        parts.append(f"... 还有 {more} 个任务，点击“选择任务并安装”后可选择")

    return "\n".join(parts)


def _task_command_preview_text(task_crons, item, limit=3):
    if not task_crons:
        return "-"

    parts = []

    for idx, tc in enumerate(task_crons[:limit], 1):
        tname = str(tc.get("name") or f"任务{idx}").strip()
        tcmd = str(tc.get("command") or guess_task_command(item) or "-").strip()
        parts.append(f"{tname}：{tcmd}")

    more = len(task_crons) - limit

    if more > 0:
        parts.append(f"... 还有 {more} 个任务")

    return "\n".join(parts)


def _task_vars_preview_text(task_crons, limit=3):
    if not task_crons:
        return "-"

    parts = []

    for idx, tc in enumerate(task_crons[:limit], 1):
        tname = str(tc.get("name") or f"任务{idx}").strip()
        tenv = online_task_cron_vars(tc)

        if tenv:
            kv = "，".join([f"{k}={v}" for k, v in tenv.items()])
            parts.append(f"{tname}：{kv}")

    more = len(task_crons) - limit

    if more > 0:
        parts.append(f"... 还有 {more} 个任务")

    return "\n".join(parts) if parts else "-"


def render_online_script_rows(items):
    if not items:
        return """
<div class="fls-empty-card">
    <div style="font-size:34px;margin-bottom:8px;">📭</div>
    <div style="font-weight:900;font-size:16px;">暂无在线脚本</div>
    <div class="help" style="margin-top:6px;">
        请点击“刷新远程脚本源”，或进入“脚本源 JSON”手动粘贴缓存。
    </div>
</div>
"""

    cards = ""

    for item in items:
        item_id = item.get("id")

        task_crons = online_script_task_crons(item)
        has_task = script_has_task(item)
        has_install = script_has_install(item)
        has_doc = script_has_doc(item)

        has_task_link = bool(str(item.get("task_link") or "").strip())
        task_link_unloaded = has_task_link and not has_task

        vars_count = task_vars_summary(task_crons)

        target = "-"
        exists = False

        try:
            target_path = online_script_target(item)
            target = str(target_path)
            exists = target_path.exists()
        except Exception:
            pass

        type_badge = f'<span class="badge blue">{h(item.get("type"))}</span>'
        exists_badge = '<span class="badge orange">目标已存在</span>' if exists else '<span class="badge green">可安装</span>'

        if has_task:
            task_badge = f'<span class="badge blue">可导入 {len(task_crons)} 个任务</span>'
        elif task_link_unloaded:
            task_badge = '<span class="badge orange">外部任务源未加载</span>'
        else:
            task_badge = '<span class="badge gray">无任务</span>'

        install_badge = '<span class="badge orange">有安装命令</span>' if has_install else '<span class="badge gray">无安装命令</span>'
        doc_badge = '<span class="badge blue">有文档</span>' if has_doc else '<span class="badge gray">无文档</span>'
        var_badge = f'<span class="badge orange">预设变量 {vars_count} 个</span>' if vars_count else '<span class="badge gray">无预设变量</span>'

        cron_text = _task_preview_text(task_crons, item, limit=3)
        command_text = _task_command_preview_text(task_crons, item, limit=3)
        vars_text = _task_vars_preview_text(task_crons, limit=3)

        proxy_options = proxy_select_options("")

        doc_btn = ""
        if has_doc:
            doc_btn = f'<a class="btn btn-orange" href="/online-scripts/doc/{h(item_id)}">查看文档</a>'

        task_link_form_html = ""
        task_link_btn_html = ""

        if task_link_unloaded:
            task_link_form_html = f"""
<form method="post" action="/online-scripts/task-link/{h(item_id)}" style="display:inline;">
    <button class="btn btn-orange" type="submit">手动拉取任务源</button>
</form>
"""
            task_link_btn_html = task_link_form_html

        running_install_id, running_install_info = get_running_install_by_script_id(item_id)

        if running_install_id:
            install_action_html = f"""
<form method="post" action="/online-scripts/install-stop/{h(running_install_id)}">
    <div class="help" style="margin-bottom:10px;color:#f59e0b;font-weight:800;">
        当前脚本正在安装，状态：{h((running_install_info or {}).get("status") or "运行中")}
    </div>

    <div class="fls-btn-line">
        <a class="btn btn-blue" href="{h(item.get("link"))}" target="_blank">查看源</a>
        {doc_btn}
        <a class="btn btn-orange" href="/online-scripts/log/{h(running_install_id)}?back=/online-scripts">查看日志</a>
        <button class="btn btn-red" type="submit" onclick="return confirm('确定停止该安装任务吗？')">停止安装</button>
    </div>
</form>
"""
        else:
            if has_task:
                install_action_html = f"""
<div class="help" style="margin:0 0 10px;">
    该脚本包含 <b>{len(task_crons)}</b> 个任务。点击“选择任务并安装”后，可选择要导入的任务，默认全选。
</div>

<div class="fls-btn-line">
    <a class="btn btn-blue" href="{h(item.get("link"))}" target="_blank">查看源</a>
    {doc_btn}
    {task_link_btn_html}
    <a class="btn btn-primary" href="/online-scripts/install-select/{h(item_id)}">选择任务并安装</a>
</div>
"""
            else:
                install_action_html = f"""
<form method="post" action="/online-scripts/install/{h(item_id)}">
    <div class="fls-action-line">
        <select name="proxy_id">{proxy_options}</select>

        <div class="fls-check-group">
            <label class="fls-inline-check">
                <input type="checkbox" name="import_task" value="1" disabled style="width:auto;">
                导入任务
            </label>

            <label class="fls-inline-check">
                <input type="checkbox" name="enable_task" value="1" disabled style="width:auto;">
                启用任务
            </label>
        </div>
    </div>

    <div class="help" style="margin:6px 0 10px;">
        {"该脚本配置了外部任务源，但尚未加载任务。可以先手动拉取任务源。" if task_link_unloaded else "该脚本没有内置任务。下载安装后可到任务管理手动创建任务。"}
    </div>

    <div class="fls-btn-line">
        <a class="btn btn-blue" href="{h(item.get("link"))}" target="_blank">查看源</a>
        {doc_btn}
        {task_link_btn_html}
        <button class="btn btn-primary" type="submit">下载安装</button>
    </div>
</form>
"""

        doc_section = ""
        if has_doc:
            doc_section = (
                "<div class='fls-card-section'>"
                "<div class='fls-info-label'>文档地址</div>"
                "<div class='fls-info-value'>"
                f"<a href='{h(item.get('doc_link'))}' target='_blank'>{h(item.get('doc_link'))}</a>"
                "</div></div>"
            )

        task_link_section = ""
        if has_task_link:
            task_link_section = (
                "<div class='fls-card-section'>"
                "<div class='fls-info-label'>外部任务源</div>"
                "<div class='fls-info-value'>"
                f"<a href='{h(item.get('task_link'))}' target='_blank'>{h(item.get('task_link'))}</a>"
                "</div></div>"
            )

        cards += f"""
<details class="fls-fold-card">
    <summary>
        <div class="fls-card-head">
            <div class="fls-card-main">
                <div class="fls-card-title-main">{h(item.get("name"))}</div>
                <div class="fls-card-sub">
                    ID：{h(item_id)}<br>
                    保存名：{h(item.get("link_name"))}
                </div>
            </div>

            <div class="fls-card-badges">
                {type_badge}
                {exists_badge}
                {doc_badge}
            </div>
        </div>
    </summary>

    <div class="fls-card-body">
        <div class="fls-info-grid">
            <div class="fls-info-item">
                <div class="fls-info-label">目标路径</div>
                <div class="fls-info-value">{h(target)}</div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">安装 / 文档</div>
                <div class="fls-info-value">
                    {install_badge}
                    {doc_badge}
                </div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">任务预览，最多显示 3 个</div>
                <div class="fls-info-value">
                    {task_badge}
                    <pre style="margin:6px 0 0;white-space:pre-wrap;word-break:break-word;background:transparent;padding:0;font-family:inherit;font-size:12px;color:#6b7280;">{h(cron_text)}</pre>
                </div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">预设任务变量</div>
                <div class="fls-info-value">
                    {var_badge}
                    <pre style="margin:6px 0 0;white-space:pre-wrap;word-break:break-word;background:transparent;padding:0;font-family:inherit;font-size:12px;color:#6b7280;">{h(vars_text)}</pre>
                </div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">任务命令预览，最多显示 3 个</div>
                <pre class="fls-info-value code-like" style="margin:0;white-space:pre-wrap;">{h(command_text)}</pre>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">创建 / 更新</div>
                <div class="fls-info-value">
                    {h(item.get("created_at", "-"))}<br>
                    {h(item.get("updated_at", "-"))}
                </div>
            </div>
        </div>

        <div class="fls-card-section">
            <div class="fls-info-label">源地址</div>
            <div class="fls-info-value">
                <a href="{h(item.get("link"))}" target="_blank">{h(item.get("link"))}</a>
            </div>
        </div>

        {task_link_section}
        {doc_section}

        <div class="fls-card-actions">
            {install_action_html}
        </div>
    </div>
</details>
"""

    return cards