from datetime import datetime
from math import ceil
from urllib.parse import quote

from flask import Blueprint, abort, redirect, url_for, request, Response

from ..paths import LOG_DIR
from ..logs import parse_task_name_from_log, tail_file
from ..utils import h, get_back_url
from ..ui.layout import layout
from ..ui.log_controls import log_controls

bp = Blueprint("logs", __name__)


def page_links(base, q, page, pages):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"{base}?page={int(p)}"

        if q:
            url += "&q=" + quote(q)

        return url

    def page_btn(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"
        return f'<a class="btn {cls}" href="{h(build_url(p))}">{h(text)}</a>'

    page = max(1, min(int(page), int(pages)))

    items = []

    # 上一页
    items.append(
        page_btn(page - 1, "上一页", disabled=(page <= 1))
    )

    # 始终显示 1、最后一页、当前页前后 2 页
    show = {1, pages}

    for p in range(page - 2, page + 3):
        if 1 <= p <= pages:
            show.add(p)

    show = sorted(show)

    last = 0

    for p in show:
        if last and p - last > 1:
            items.append('<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>')

        items.append(
            page_btn(p, active=(p == page))
        )

        last = p

    # 下一页
    items.append(
        page_btn(page + 1, "下一页", disabled=(page >= pages))
    )

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            第 <b>{page}</b> / <b>{pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
</div>
"""


def log_group_title(task_name, count):
    if task_name == "其他日志":
        title = "其他日志"
    else:
        title = f"任务：{task_name}"

    if int(count or 0) > 1:
        title += f"（{int(count)}）"

    return title


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


@bp.route("/logfile/<filename>")
def logfile_view(filename):
    back_url = get_back_url("/logs")
    filename = filename.split("/")[-1]
    file_path = LOG_DIR / filename

    if not file_path.exists():
        abort(404)

    body = f"""
<div class="card">
    <div class="card-title">日志文件：{h(filename)}</div>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-red" href="/logfile/delete/{h(filename)}?back={h(back_url)}" onclick="return confirm('确定删除日志吗？')">删除</a>
</div>

<pre class="log" id="log">加载中...</pre>
{log_controls()}

<script>
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function nearBottom(){{
    return document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 90;
}}

window.addEventListener("scroll", function(){{
    window.__FLS_LOG_NEAR_BOTTOM__ = nearBottom();
}}, {{passive:true}});

async function loadLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/logfile/{h(filename)}?lines=1500", {{cache:"no-store"}});
        const text = await res.text();
        const old = window.__FLS_LOG_LAST_TEXT__ || "";
        const changed = text !== old;

        var logEl = document.getElementById("log");
        if(typeof flsRenderLogText === "function"){{
            flsRenderLogText(logEl, text);
        }}else{{
            logEl.textContent = text;
        }}
        window.__FLS_LOG_LAST_TEXT__ = text;

        if(changed){{
            if(wasNearBottom || window.__FLS_LOG_NEAR_BOTTOM__){{
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "none";
                window.scrollTo(0, document.documentElement.scrollHeight);
            }}else{{
                const afterHeight = document.documentElement.scrollHeight;
                window.scrollTo(0, beforeScroll + Math.max(afterHeight - beforeHeight, 0));
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "block";
            }}
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""
    return layout("日志文件", "logs", body)


@bp.route("/api/logfile/<filename>")
def api_logfile(filename):
    filename = filename.split("/")[-1]
    file_path = LOG_DIR / filename

    if not file_path.exists():
        abort(404)

    lines = int(request.args.get("lines", "1500"))

    return Response(
        tail_file(str(file_path), lines),
        mimetype="text/plain; charset=utf-8"
    )


@bp.route("/logfile/delete/<filename>")
def logfile_delete(filename):
    back_url = get_back_url("/logs")
    filename = filename.split("/")[-1]
    file_path = LOG_DIR / filename

    if file_path.exists():
        file_path.unlink()

    return redirect(back_url)