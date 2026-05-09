from ._common import *


@bp.route("/logs")
def logs_page():
    q = request.args.get("q", "").strip().lower()
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = 10

    files = sorted(
        LOG_DIR.glob("*.log"),
        key=lambda x: x.stat().st_mtime,
        reverse=True
    )

    groups = {}

    for f in files:
        if (
            f.name.startswith("deps-install-")
            or f.name.startswith("system-install-")
            or f.name.startswith("backup-restore-deps-")
            or f.name.startswith("fls-manager")
        ):
            key = "其他日志"
        else:
            key = parse_task_name_from_log(f) or "其他日志"

        groups.setdefault(key, []).append(f)

    group_items = []

    for name, fs in groups.items():
        if q:
            matched = q in name.lower() or any(q in x.name.lower() for x in fs)
            if not matched:
                continue

        group_items.append((name, fs))

    group_items.sort(
        key=lambda x: max([f.stat().st_mtime for f in x[1]] or [0]),
        reverse=True
    )

    total = len(group_items)
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)
    show = group_items[(page - 1) * per_page: page * per_page]

    content = f"""
<form method="get">
<div class="card">
    <div class="card-title">日志管理</div>
    <div class="help">
        日志按任务分组显示。每个分组默认折叠，点击卡片可展开查看日志文件。<br>
        支持搜索任务名 / 日志文件名。
    </div>
    <br>
    <div class="form-grid">
        <div class="form-item">
            <label>搜索日志</label>
            <input name="q" value="{h(q)}" placeholder="任务名 / 日志文件名">
        </div>
        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="submit">搜索</button>
            <a class="btn btn-gray" href="/logs">重置</a>
        </div>
    </div>
</div>
</form>
"""

    if not show:
        content += """
<div class="card">
    <div class="help">暂无匹配日志</div>
</div>
"""
    else:
        content += '<div id="logsGroupGrid">'

        for task_name, log_files in show:
            rows = ""

            latest_time = "-"
            latest_file = "-"

            try:
                latest = max(log_files, key=lambda x: x.stat().st_mtime)
                latest_time = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                latest_file = latest.name
            except Exception:
                pass

            for f in log_files:
                try:
                    size = f.stat().st_size / 1024
                    size_text = f"{size:.1f} KB"
                except Exception:
                    size_text = "-"

                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    mtime = "-"

                rows += f"""
<tr>
    <td>{h(f.name)}</td>
    <td>{h(size_text)}</td>
    <td>{h(mtime)}</td>
    <td>
        <a class="btn btn-orange" href="/logfile/{h(f.name)}?back=/logs">查看</a>
        <a class="btn btn-red" href="/logfile/delete/{h(f.name)}" onclick="return confirm('确定删除日志 {h(f.name)} 吗？')">删除</a>
    </td>
</tr>
"""

            table = f"""
<div class="table-wrap">
<table>
<thead>
<tr>
    <th>日志文件</th>
    <th>大小</th>
    <th>修改时间</th>
    <th>操作</th>
</tr>
</thead>
<tbody>{rows}</tbody>
</table>
</div>
"""

            title = log_group_title(task_name, len(log_files))

            content += f"""
<details class="log-group-card">
    <summary>
        <div class="log-group-head">
            <div>
                <div class="log-group-title">{h(title)}</div>
                <div class="log-group-sub">
                    最新日志：{h(latest_file)}<br>
                    最新时间：{h(latest_time)}
                </div>
            </div>
            <div class="log-group-meta">
                <span class="badge blue">{len(log_files)} 条</span>
            </div>
        </div>
    </summary>

    <div class="log-group-body">
        {table}
    </div>
</details>
"""

        content += "</div>"

    content += page_links("/logs", q, page, pages)

    return layout("日志管理", "logs", content)

