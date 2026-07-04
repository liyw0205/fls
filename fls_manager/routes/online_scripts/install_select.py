from ._common import *


@bp.route("/online-scripts/import-tasks/<script_id>", methods=["POST"])
def online_scripts_import_tasks_only(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    if not script_has_task(item):
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="该脚本没有可导入的任务",
            )
        )

    enable_task = request.form.get("enable_task") == "1"
    selected_task_indexes = selected_task_indexes_from_form(item)

    if not selected_task_indexes:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="没有选择任何任务，未导入",
            )
        )

    try:
        ok, msg = import_task_if_needed(
            item,
            log_file=None,
            enable_task=enable_task,
            selected_task_indexes=selected_task_indexes,
        )

        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg=msg if ok else "",
                err="" if ok else msg,
            )
        )

    except Exception as e:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err=f"任务导入失败：{e}",
            )
        )


@bp.route("/online-scripts/install-select/<script_id>")
def online_scripts_install_select(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    task_crons = online_script_task_crons(item)
    has_task = script_has_task(item)

    if not has_task:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="该脚本没有可选择的任务",
            )
        )

    proxy_options = proxy_select_options("")

    task_q = request.args.get("task_q", "").strip()
    task_q_lower = task_q.lower()

    task_page = max(1, int(request.args.get("task_page", "1") or 1))
    task_per_page = 10

    excluded_raw = request.args.get("excluded", "").strip()
    excluded_set = parse_excluded_task_indexes(excluded_raw)

    total_tasks = len(task_crons)

    indexed_tasks = []

    for idx, task_cron in enumerate(task_crons, 1):
        env = online_task_cron_vars(task_cron)
        env_text = "\n".join([f"{k}={v}" for k, v in env.items()]) if env else ""

        fields = [
            task_cron.get("name", ""),
            task_cron.get("cron", ""),
            task_cron.get("command", ""),
            task_cron.get("remark", ""),
            task_cron.get("config_path", ""),
            env_text,
            str(idx),
        ]

        search_text = "\n".join(str(x or "") for x in fields).lower()

        if not task_q_lower or task_q_lower in search_text:
            indexed_tasks.append((idx, task_cron))

    filtered_total = len(indexed_tasks)
    task_pages = max(1, ceil(filtered_total / task_per_page))
    task_page = min(task_page, task_pages)

    start = (task_page - 1) * task_per_page
    end = task_page * task_per_page

    show_indexed_tasks = indexed_tasks[start:end]

    task_rows = ""

    if not show_indexed_tasks:
        task_rows = """
<div class="fls-empty-card" style="grid-column:1 / -1;">
    暂无匹配任务
</div>
"""
    else:
        for idx, task_cron in show_indexed_tasks:
            name = str(task_cron.get("name") or f"任务{idx}").strip()
            cron = str(task_cron.get("cron") or "手动").strip()
            command = str(task_cron.get("command") or guess_task_command(item) or "-").strip()
            remark = str(task_cron.get("remark") or "").strip()
            config_path = str(task_cron.get("config_path") or "").strip()
            env = online_task_cron_vars(task_cron)

            env_text = "-"

            if env:
                env_text = "\n".join([f"{k}={v}" for k, v in env.items()])

            checked = "" if idx in excluded_set else "checked"

            remark_html = ""
            if remark:
                remark_html = f"""
                <div class="fls-install-task-meta">
                    <b>备注：</b>{h(remark)}
                </div>
"""

            config_html = ""
            if config_path:
                config_html = f"""
                <div class="fls-install-task-meta">
                    <b>配置：</b>{h(config_path)}
                </div>
"""

            task_rows += f"""
<label class="fls-install-task-row">
    <div class="fls-install-task-check">
        <input
            type="checkbox"
            class="fls-install-task-checkbox"
            data-index="{idx}"
            name="visible_task_indexes"
            value="{idx}"
            {checked}
            style="width:auto;"
        >
    </div>

    <div class="fls-install-task-main">
        <div class="fls-install-task-title">
            {h(name)}
            <span class="badge blue">#{idx}</span>
        </div>

        <div class="fls-install-task-meta">
            <b>Cron：</b>{h(cron)}
        </div>

        {remark_html}
        {config_html}

        <div class="fls-install-task-meta">
            <b>命令：</b>
        </div>
        <pre class="fls-install-task-code">{h(command)}</pre>

        <div class="fls-install-task-meta">
            <b>变量：</b>
        </div>
        <pre class="fls-install-task-code">{h(env_text)}</pre>
    </div>
</label>
"""

    excluded_text = ",".join(str(x) for x in sorted(excluded_set))

    page_links_html = install_task_page_links(
        script_id=script_id,
        task_page=task_page,
        task_pages=task_pages,
        excluded=excluded_text,
        task_q=task_q,
    )

    selected_count = total_tasks - len(excluded_set)
    if selected_count < 0:
        selected_count = 0

    display_start = start + 1 if filtered_total else 0
    display_end = min(end, filtered_total)

    header_card = page_header_card(
        f'选择任务并安装：{item.get("name") or script_id}',
        help_html=f"""
                脚本 ID：{h(item.get("id"))}<br>
                脚本类型：{h(item.get("type"))}<br>
                保存名：{h(item.get("link_name"))}<br>
                共检测到 <b>{total_tasks}</b> 个可导入任务。默认全选全部任务。<br>
选择约 <b id="selectedTaskCount">{selected_count}</b> 个任务。
""",
        actions_html='<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>',
    )

    body = f"""
<style>
.fls-install-select-head {{
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    gap:12px;
    flex-wrap:wrap;
}}

.fls-install-task-tools {{
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    align-items:center;
}}

.fls-install-task-list {{
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:12px;
}}

.fls-install-task-row {{
    display:flex;
    gap:10px;
    align-items:flex-start;
    border:1px solid #e5e7eb;
    border-radius:14px;
    background:#fff;
    padding:12px;
    cursor:pointer;
    min-width:0;
    box-shadow:0 4px 16px rgba(0,0,0,.03);
}}

.fls-install-task-row:hover {{
    border-color:#93c5fd;
    background:#f8fafc;
}}

.fls-install-task-check {{
    padding-top:3px;
}}

.fls-install-task-main {{
    min-width:0;
    flex:1;
}}

.fls-install-task-title {{
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    align-items:center;
    color:#111827;
    font-size:15px;
    font-weight:900;
    line-height:1.35;
    word-break:break-word;
}}

.fls-install-task-meta {{
    margin-top:7px;
    color:#6b7280;
    font-size:12px;
    line-height:1.5;
    word-break:break-word;
}}

.fls-install-task-code {{
    margin:5px 0 0;
    padding:8px;
    border-radius:8px;
    background:#f3f4f6;
    color:#374151;
    font-family:Consolas,Menlo,monospace;
    font-size:12px;
    line-height:1.45;
    white-space:pre-wrap;
    word-break:break-all;
}}

body.fls-mobile .fls-install-task-list {{
    grid-template-columns:1fr!important;
}}
</style>

<form method="post" action="/online-scripts/install/{h(script_id)}" id="onlineInstallSelectForm">
<input type="hidden" name="select_mode" value="all">
<input type="hidden" name="excluded_task_indexes" id="excludedTaskIndexes" value="{h(excluded_text)}">

{header_card}

<div class="card">
    <div class="card-title">安装选项</div>

    <div class="form-grid">
        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id">{proxy_options}</select>
        </div>

        <div class="form-item">
            <label>任务导入</label>
            <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;padding-top:8px;">
                <label class="fls-inline-check">
                    <input type="checkbox" name="import_task" value="1" checked style="width:auto;">
                    导入所选任务
                </label>

                <label class="fls-inline-check">
                    <input type="checkbox" name="enable_task" value="1" style="width:auto;">
                    导入后启用任务
                </label>
            </div>
            <div class="help">
                不勾选“导入后启用任务”时，导入后的任务默认为禁用，需要到任务管理手动启用。
            </div>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">搜索任务</div>
    <div class="form-grid">
        <div class="form-item">
            <label>关键词</label>
            <input id="installTaskSearchInput" value="{h(task_q)}" placeholder="任务名 / Cron / 命令 / 备注 / 变量 / 序号">
        </div>

        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="button" onclick="flsInstallSearchTasks()">搜索</button>
            <button class="btn btn-gray" type="button" onclick="flsInstallClearSearch()">重置搜索</button>
        </div>
    </div>
</div>

<div class="card">
    <div class="fls-install-select-head">
        <div>
            <div class="card-title">选择要导入的任务</div>
            <div class="help">
                当前页显示 {display_start} - {display_end} / {filtered_total} 个匹配任务，每页 10 个。<br>
                默认全选全部任务，可以取消不需要导入的任务。
            </div>
        </div>

        <div class="fls-install-task-tools">
            <button class="btn btn-blue" type="button" onclick="flsInstallSelectCurrentPage(true)">当前页全选</button>
            <button class="btn btn-gray" type="button" onclick="flsInstallSelectCurrentPage(false)">当前页取消</button>
            <button class="btn btn-red" type="button" onclick="flsInstallCancelAllGlobal()">全部取消</button>
            <button class="btn btn-primary" type="button" onclick="flsInstallSelectAllGlobal()">全部任务全选</button>
        </div>
    </div>

    <br>

    <div class="fls-install-task-list">
        {task_rows}
    </div>
</div>

{page_links_html}

<div class="card">
    <button class="btn btn-primary" type="submit">开始下载安装</button>
    <button class="btn btn-orange" type="submit" formaction="/online-scripts/import-tasks/{h(script_id)}">立即导入所选任务</button>
    <a class="btn btn-gray" href="/online-scripts">取消</a>
</div>
</form>

<script>
window.FLS_TOTAL_TASKS = {total_tasks};
window.FLS_INSTALL_SCRIPT_ID = "{h(script_id)}";

function flsParseExcluded(){{
    const el = document.getElementById("excludedTaskIndexes");
    if(!el) return new Set();

    const text = el.value || "";
    const set = new Set();

    text.split(",").forEach(function(x){{
        x = String(x || "").trim();
        if(!x) return;

        const n = parseInt(x, 10);
        if(n > 0) set.add(n);
    }});

    return set;
}}

function flsSaveExcluded(set){{
    const el = document.getElementById("excludedTaskIndexes");
    if(!el) return;

    const arr = Array.from(set)
        .filter(function(n){{ return n > 0; }})
        .sort(function(a,b){{ return a-b; }});

    el.value = arr.join(",");

    const countEl = document.getElementById("selectedTaskCount");
    if(countEl){{
        countEl.textContent = Math.max(0, window.FLS_TOTAL_TASKS - arr.length);
    }}
}}

function flsSyncVisibleCheckboxesToExcluded(){{
    const set = flsParseExcluded();

    document.querySelectorAll(".fls-install-task-checkbox").forEach(function(input){{
        const idx = parseInt(input.getAttribute("data-index") || "0", 10);
        if(idx <= 0) return;

        if(input.checked){{
            set.delete(idx);
        }}else{{
            set.add(idx);
        }}
    }});

    flsSaveExcluded(set);
}}

function flsApplyExcludedToVisible(){{
    const set = flsParseExcluded();

    document.querySelectorAll(".fls-install-task-checkbox").forEach(function(input){{
        const idx = parseInt(input.getAttribute("data-index") || "0", 10);
        input.checked = !set.has(idx);
    }});

    flsSaveExcluded(set);
}}

function flsInstallSelectCurrentPage(checked){{
    document.querySelectorAll(".fls-install-task-checkbox").forEach(function(input){{
        input.checked = !!checked;
    }});

    flsSyncVisibleCheckboxesToExcluded();
}}

function flsInstallSelectAllGlobal(){{
    flsSaveExcluded(new Set());
    flsApplyExcludedToVisible();
}}

function flsInstallCancelAllGlobal(){{
    const set = new Set();

    for(let i = 1; i <= window.FLS_TOTAL_TASKS; i++){{
        set.add(i);
    }}

    flsSaveExcluded(set);
    flsApplyExcludedToVisible();
}}

function flsInstallBuildUrl(page){{
    flsSyncVisibleCheckboxesToExcluded();

    const excluded = document.getElementById("excludedTaskIndexes").value || "";
    const qEl = document.getElementById("installTaskSearchInput");
    const taskQ = qEl ? qEl.value.trim() : "";

    const url = new URL(
        "/online-scripts/install-select/" + encodeURIComponent(window.FLS_INSTALL_SCRIPT_ID),
        window.location.origin
    );
    url.searchParams.set("task_page", page || "1");

    if(taskQ){{
        url.searchParams.set("task_q", taskQ);
    }}

    if(excluded){{
        url.searchParams.set("excluded", excluded);
    }}

    return url.toString();
}}

function flsInstallSearchTasks(){{
    window.location.href = flsInstallBuildUrl("1");
}}

function flsInstallClearSearch(){{
    flsSyncVisibleCheckboxesToExcluded();

    const excluded = document.getElementById("excludedTaskIndexes").value || "";
    const url = new URL(
        "/online-scripts/install-select/" + encodeURIComponent(window.FLS_INSTALL_SCRIPT_ID),
        window.location.origin
    );
    url.searchParams.set("task_page", "1");

    if(excluded){{
        url.searchParams.set("excluded", excluded);
    }}

    window.location.href = url.toString();
}}

document.querySelectorAll(".fls-install-task-checkbox").forEach(function(input){{
    input.addEventListener("change", flsSyncVisibleCheckboxesToExcluded);
}});

function flsInstallGoTaskPage(page){{
    window.location.href = flsInstallBuildUrl(String(page || 1));
}}

document.getElementById("onlineInstallSelectForm").addEventListener("submit", function(e){{
    flsSyncVisibleCheckboxesToExcluded();

    var importTask = document.querySelector('#onlineInstallSelectForm input[name="import_task"]');
    var excluded = flsParseExcluded();

    if(importTask && importTask.checked && excluded.size >= window.FLS_TOTAL_TASKS){{
        e.preventDefault();
        alert("已勾选导入任务，但没有选择任何任务");
        return false;
    }}
}});

flsApplyExcludedToVisible();
</script>
"""

    return layout("选择任务并安装", "online_scripts", body)
