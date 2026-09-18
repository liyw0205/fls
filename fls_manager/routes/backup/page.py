from ._common import *


@bp.route("/backup")
def backup_page():
    header = page_header(
        "备份恢复",
        help_html="创建配置或脚本备份，查看进度，并在确认后恢复已有备份。",
    )
    body = f"""
{header}
<nav class="fls-section-nav" aria-label="备份恢复区块导航">
    <span class="fls-section-nav-label">备份恢复</span>
    <a href="#backup-create">创建备份</a>
    <a href="#backup-list">备份列表</a>
    <a href="#backup-restore">导入恢复</a>
</nav>
<section class="section" id="backup-create">
<div class="card">
    <h2 class="section-title">创建备份</h2>
    <div class="help">
        可以选择仅备份配置、仅备份脚本，或同时备份配置和脚本。<br>
        备份会在后台压缩，完成后出现在下方备份列表。
    </div>

    <br>

    <form id="backupCreateForm" aria-label="创建备份">
        <label style="display:block;margin:8px 0;">
            <input type="checkbox" name="items" value="data" checked style="width:auto;">
            配置数据，data：任务、配置、变量、代理、通知等
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="checkbox" name="items" value="scripts" checked style="width:auto;">
            脚本目录，scripts
        </label>

        <br>

        <button class="btn btn-primary" type="button" onclick="flsCreateBackup(this)">创建备份</button>
    </form>
</div>
</section>

<section class="section" id="backup-progress">
<div class="card" id="backupJobCard" style="display:none;">
    <h2 class="section-title">备份进度</h2>
    <div class="help" id="backupJobText">等待开始</div>
</div>
</section>

<section class="section" id="backup-list">
<div class="card">
    <h2 class="section-title">备份列表</h2>
    <div class="help">
        备份目录：<code>{h(BACKUP_DIR)}</code>
    </div>

    <br>

    <button class="btn btn-blue" type="button" onclick="flsRefreshBackupList(this)">刷新备份列表</button>

    <br><br>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>文件名</th>
                    <th>大小</th>
                    <th>创建时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody id="backupListTbody">
                {backup_rows_html()}
            </tbody>
        </table>
    </div>
</div>
</section>

<section class="section" id="backup-restore">
<div class="card">
    <h2 class="section-title">导入恢复</h2>
    <form method="post" action="/backup/import" enctype="multipart/form-data">
        <div class="form-item">
            <label>选择备份文件</label>
            <input type="file" name="file" accept=".tar.gz,.tgz,.tar,.zip" aria-label="选择备份文件">
        </div>

        <br>

        <div class="help">选择要恢复的内容：</div>

        <label style="display:block;margin:8px 0;">
            <input type="checkbox" name="restore_items" value="data" checked style="width:auto;">
            恢复配置数据，data
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="checkbox" name="restore_items" value="scripts" checked style="width:auto;">
            恢复脚本目录，scripts
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="checkbox" name="restore_deps" value="1" style="width:auto;">
            如果备份中包含依赖列表，同时恢复 Python 依赖
        </label>

        <div class="help">
            导入会覆盖已选择恢复的目录。<br>
            支持 .tar.gz / .tgz / .tar / .zip。
        </div>

        <br>

        <div class="danger-confirm">
            <div class="danger-confirm-description">恢复会覆盖所选 data / scripts。确认备份文件和恢复范围后再继续。</div>
            <button class="btn btn-orange" type="submit" onclick="return confirm('导入会覆盖已选择的 data / scripts，确定继续吗？')">
                覆盖恢复备份
            </button>
        </div>
    </form>
</div>
</section>

<script>
function flsEscapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c];
    }});
}}

function flsShowBackupJob(text){{
    var card = document.getElementById("backupJobCard");
    var el = document.getElementById("backupJobText");

    if(card) card.style.display = "block";
    if(el) el.innerHTML = text;
}}

async function flsCreateBackup(source){{
    const form = document.getElementById("backupCreateForm");
    const fd = new FormData(form);

    if(!fd.getAll("items").length){{
        alert(flsActionFailure("请至少选择一个备份内容", "勾选配置或脚本后重试"));
        return;
    }}

    if(!flsMarkButtonBusy(source, "创建中...")) return;
    flsShowBackupJob("已提交备份任务，正在准备...");

    try {{
        const res = await fetch("/api/backup/create", {{
            method: "POST",
            body: fd,
            headers: {{"X-Requested-With":"XMLHttpRequest"}},
            credentials: "same-origin"
        }});

        const json = await res.json();

        if(!json.ok){{
            flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>" + flsEscapeHtml(flsActionFailure("创建失败：" + (json.msg || "未知错误"), "检查备份内容后重试")) + "</span>");
            flsRestoreButton(source);
            return;
        }}

        await flsPollBackupJob(json.job_id, source);

    }} catch(e) {{
        flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>" + flsEscapeHtml(flsActionFailure("请求失败：" + String(e), "检查服务状态后重试")) + "</span>");
        flsRestoreButton(source);
    }}
}}

async function flsPollBackupJob(jobId, source){{
    try {{
        const res = await fetch("/api/backup/job/" + encodeURIComponent(jobId), {{
            cache: "no-store",
            credentials: "same-origin"
        }});

        const json = await res.json();

        if(!json.ok){{
            flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>" + flsEscapeHtml(flsActionFailure("任务不存在", "刷新页面后重试")) + "</span>");
            flsRestoreButton(source);
            return;
        }}

        let html =
            "状态：<b>" + flsEscapeHtml(json.status || "-") + "</b><br>" +
            "类型：" + flsEscapeHtml(json.type_text || "-") + "<br>" +
            "更新时间：" + flsEscapeHtml(json.updated_at || "-") + "<br>";

        if(json.filename){{
            html += "文件：" + flsEscapeHtml(json.filename) + "<br>";
        }}

        if(json.size_text){{
            html += "大小：" + flsEscapeHtml(json.size_text) + "<br>";
        }}

        if(json.error){{
            html += "<span style='color:#dc2626;font-weight:800;'>" + flsEscapeHtml(flsActionFailure("错误：" + json.error, "查看备份日志并检查备份内容后重试")) + "</span><br>";
        }}

        if(!json.running && json.filename){{
            html += "<br><a class='btn btn-primary' href='/backup/download/" + encodeURIComponent(json.filename) + "'>下载备份</a>";
        }}

        flsShowBackupJob(html);

        if(json.running){{
            setTimeout(function(){{
                flsPollBackupJob(jobId, source);
            }}, 1500);
        }}else{{
            flsRestoreButton(source);
            flsRefreshBackupList();
        }}

    }} catch(e) {{
        flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>" + flsEscapeHtml(flsActionFailure("状态读取失败：" + String(e), "刷新页面后重试")) + "</span>");
        flsRestoreButton(source);
    }}
}}

async function flsRefreshBackupList(source){{
    if(source && !flsMarkButtonBusy(source, "刷新中...")) return;
    try {{
        const res = await fetch("/api/backup/list", {{
            cache: "no-store",
            credentials: "same-origin"
        }});

        const json = await res.json();
        if(!res.ok || !json.ok){{
            throw new Error(flsActionFailure(json.msg || ("HTTP " + res.status), "检查服务状态后重试"));
        }}

        const tbody = document.getElementById("backupListTbody");
        if(!tbody) return;

        if(!json.items || !json.items.length){{
            tbody.innerHTML = "<tr class='empty-state-row'><td colspan='4'><div class='empty-state'><p>暂无备份</p><div class='empty-state-actions'><a class='btn btn-primary' href='#backup-create'>创建备份</a></div></div></td></tr>";
            return;
        }}

        let html = "";

        for(const item of json.items){{
            html += "<tr>" +
                "<td><b>" + flsEscapeHtml(item.name) + "</b></td>" +
                "<td>" + flsEscapeHtml(item.size_text || "-") + "</td>" +
                "<td>" + flsEscapeHtml(item.mtime_text || "-") + "</td>" +
                    "<td>" +
                    "<div class='row-actions' aria-label='备份 " + flsEscapeHtml(item.name) + " 行操作'>" +
                        "<div class='row-actions-primary'><a class='btn btn-primary' href='/backup/download/" + encodeURIComponent(item.name) + "'>下载备份</a></div>" +
                        "<div class='row-actions-danger'><button class='btn btn-red' type='button' onclick='flsDeleteBackup(" + JSON.stringify(item.name) + ", this)'>删除备份</button></div>" +
                    "</div>" +
                "</td>" +
            "</tr>";
        }}

        tbody.innerHTML = html;

        if(typeof flsEnhanceMobileTables === "function"){{
            flsEnhanceMobileTables(document);
        }}

    }} catch(e) {{
        alert(flsActionFailure("刷新备份列表失败：" + e, "检查服务状态后重试"));
    }} finally {{
        flsRestoreButton(source);
    }}
}}

async function flsDeleteBackup(filename, source){{
    if(!confirm("确定删除备份 " + filename + " 吗？")) return;
    if(!flsMarkButtonBusy(source, "删除中...")) return;

    try {{
        const fd = new FormData();
        fd.append("filename", filename);

        const res = await fetch("/api/backup/delete", {{
            method: "POST",
            body: fd,
            headers: {{"X-Requested-With":"XMLHttpRequest"}},
            credentials: "same-origin"
        }});

        const json = await res.json();

        if(!json.ok){{
            alert(flsActionFailure(json.msg || "删除失败", "检查备份文件后重试"));
            return;
        }}

        await flsRefreshBackupList();

    }} catch(e) {{
        alert(flsActionFailure("删除请求失败：" + e, "检查服务状态后重试"));
    }} finally {{
        flsRestoreButton(source);
    }}
}}
</script>
"""
    return layout("备份恢复", "backup", body)
