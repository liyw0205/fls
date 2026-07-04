from ._common import *


@bp.route("/logs")
def logs_page():
    q = request.args.get("q", "").strip().lower()
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = 10

    groups = load_log_groups()

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
        支持搜索任务名 / 日志文件名，也可以按分组批量删除日志。
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
        content += """
<div class="card log-bulk-toolbar">
    <div class="log-bulk-left">
        <label class="log-bulk-select-all">
            <input id="logsSelectAllGroups" type="checkbox" onchange="flsLogsToggleAllGroups(this.checked)">
            全选当前页分组
        </label>
        <span id="logsSelectedGroupCount" class="log-selected-count">已选择 0 个</span>
    </div>
    <div class="log-bulk-actions">
        <button id="logsDeleteSelectedGroupsBtn" class="btn btn-red" type="button" onclick="flsLogsDeleteSelectedGroups()" disabled>删除选中分组日志</button>
    </div>
</div>

<div id="logsGroupGrid">
"""

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
        <form class="inline-form" method="post" action="/logfile/delete/{h(f.name)}?back=/logs">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定删除日志 {h(f.name)} 吗？')">删除</button>
        </form>
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
<details class="log-group-card" data-log-group="{h(task_name)}">
    <summary>
        <div class="log-group-head">
            <div class="log-group-select-wrap">
                <input
                    class="log-group-select"
                    type="checkbox"
                    value="{h(task_name)}"
                    onclick="event.stopPropagation()"
                    onchange="flsLogsUpdateBulkState()"
                    aria-label="选择日志分组 {h(title)}"
                >
            </div>
            <div>
                <div class="log-group-title">{h(title)}</div>
                <div class="log-group-sub">
                    最新日志：{h(latest_file)}<br>
                    最新时间：{h(latest_time)}
                </div>
            </div>
            <div class="log-group-meta">
                <span class="badge blue">{len(log_files)} 条</span>
                <button
                    class="btn btn-red"
                    type="button"
                    data-group="{h(task_name)}"
                    onclick="event.preventDefault();event.stopPropagation();flsLogsDeleteGroups([this.dataset.group]);"
                >删除本组日志</button>
            </div>
        </div>
    </summary>

    <div class="log-group-body">
        {table}
    </div>
</details>
"""

        content += "</div>"

        content += r"""
<style>
.log-bulk-toolbar {
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:12px;
    flex-wrap:wrap;
}

.log-bulk-left,
.log-bulk-actions {
    display:flex;
    align-items:center;
    gap:8px;
    flex-wrap:wrap;
}

.log-bulk-select-all {
    display:inline-flex;
    align-items:center;
    gap:6px;
    color:#374151;
    font-size:13px;
    font-weight:700;
    white-space:nowrap;
}

.log-selected-count {
    color:#6b7280;
    font-size:13px;
    font-weight:700;
}

#logsSelectAllGroups,
.log-group-select {
    width:16px!important;
    height:16px!important;
    min-height:16px;
    margin:0;
    padding:0;
    cursor:pointer;
}

.log-group-select-wrap {
    flex:0 0 auto;
    padding-top:2px;
}

.log-group-card.log-group-selected {
    border-color:#86efac;
}

.log-group-meta {
    display:flex;
    align-items:center;
    justify-content:flex-end;
    gap:8px;
    flex-wrap:wrap;
}

#logsDeleteSelectedGroupsBtn:disabled {
    opacity:.45;
    cursor:not-allowed;
}

@media(max-width:520px) {
    .log-bulk-toolbar {
        align-items:stretch;
    }

    .log-bulk-left,
    .log-bulk-actions {
        width:100%;
    }

    .log-bulk-actions .btn {
        width:100%;
        margin:0;
    }
}
</style>

<script>
function flsLogsGroupBoxes(){
    return Array.prototype.slice.call(document.querySelectorAll(".log-group-select"));
}

function flsLogsSelectedGroups(){
    var result = [];
    var seen = {};

    flsLogsGroupBoxes().forEach(function(box){
        var name = box.value || "";

        if(!box.checked || !name || seen[name]) return;

        seen[name] = true;
        result.push(name);
    });

    return result;
}

function flsLogsUpdateBulkState(){
    var boxes = flsLogsGroupBoxes();
    var selected = flsLogsSelectedGroups();
    var selectedSet = {};

    selected.forEach(function(name){
        selectedSet[name] = true;
    });

    var selectAll = document.getElementById("logsSelectAllGroups");
    if(selectAll){
        selectAll.disabled = boxes.length === 0;
        selectAll.checked = boxes.length > 0 && selected.length === boxes.length;
        selectAll.indeterminate = selected.length > 0 && selected.length < boxes.length;
    }

    var countEl = document.getElementById("logsSelectedGroupCount");
    if(countEl){
        countEl.textContent = "已选择 " + selected.length + " 个";
    }

    var deleteBtn = document.getElementById("logsDeleteSelectedGroupsBtn");
    if(deleteBtn){
        deleteBtn.disabled = selected.length === 0;
    }

    document.querySelectorAll(".log-group-card").forEach(function(card){
        var name = card.dataset.logGroup || "";
        card.classList.toggle("log-group-selected", !!selectedSet[name]);
    });
}

function flsLogsToggleAllGroups(checked){
    flsLogsGroupBoxes().forEach(function(box){
        box.checked = checked;
    });

    flsLogsUpdateBulkState();
}

function flsLogsDeleteSelectedGroups(){
    flsLogsDeleteGroups(flsLogsSelectedGroups());
}

async function flsLogsDeleteGroups(groups){
    groups = groups || [];

    if(!groups.length){
        alert("请选择日志分组");
        return;
    }

    if(!confirm("确定删除选中的 " + groups.length + " 个分组下的所有日志吗？")){
        return;
    }

    try {
        var res = await fetch("/api/logs/groups/delete", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest"
            },
            credentials: "same-origin",
            body: JSON.stringify({groups: groups})
        });

        var json = await res.json();

        if(!json.ok){
            alert(json.msg || "删除失败");
            return;
        }

        if(json.msg){
            alert(json.msg);
        }

        if(typeof flsClearPageCache === "function"){
            flsClearPageCache();
        }

        location.reload();

    } catch(e) {
        alert("删除请求失败：" + e);
    }
}

flsLogsUpdateBulkState();
</script>
"""

    content += page_links("/logs", q, page, pages)

    return layout("日志管理", "logs", content)
