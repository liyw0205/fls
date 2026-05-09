from ._common import *


@bp.route("/backup")
def backup_page():
    body = f"""
<div class="card">
    <div class="card-title">创建备份</div>
    <div class="help">
        可以选择仅备份配置、仅备份脚本，或同时备份配置和脚本。<br>
        备份会在后台压缩，完成后出现在下方备份列表。
    </div>

    <br>

    <form id="backupCreateForm">
        <label style="display:block;margin:8px 0;">
            <input type="checkbox" name="items" value="data" checked style="width:auto;">
            配置数据，data：任务、配置、变量、代理、通知等
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="checkbox" name="items" value="scripts" checked style="width:auto;">
            脚本目录，scripts
        </label>

        <br>

        <button class="btn btn-primary" type="button" onclick="flsCreateBackup()">开始备份</button>
    </form>
</div>

<div class="card" id="backupJobCard" style="display:none;">
    <div class="card-title">备份进度</div>
    <div class="help" id="backupJobText">等待开始</div>
</div>

<div class="card">
    <div class="card-title">备份列表</div>
    <div class="help">
        备份目录：<code>{h(BACKUP_DIR)}</code>
    </div>

    <br>

    <button class="btn btn-blue" type="button" onclick="flsRefreshBackupList()">刷新列表</button>

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

<div class="card">
    <div class="card-title">导入恢复</div>
    <form method="post" action="/backup/import" enctype="multipart/form-data">
        <div class="form-item">
            <label>选择备份文件</label>
            <input type="file" name="file" accept=".tar.gz,.tgz,.tar,.zip">
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

        <button class="btn btn-orange" type="submit" onclick="return confirm('导入会覆盖已选择的 data / scripts，确定继续吗？')">
            导入备份
        </button>
    </form>
</div>

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

async function flsCreateBackup(){{
    const form = document.getElementById("backupCreateForm");
    const fd = new FormData(form);

    if(!fd.getAll("items").length){{
        alert("请至少选择一个备份内容");
        return;
    }}

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
            flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>创建失败：" + flsEscapeHtml(json.msg || "未知错误") + "</span>");
            return;
        }}

        flsPollBackupJob(json.job_id);

    }} catch(e) {{
        flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>请求失败：" + flsEscapeHtml(String(e)) + "</span>");
    }}
}}

async function flsPollBackupJob(jobId){{
    try {{
        const res = await fetch("/api/backup/job/" + encodeURIComponent(jobId), {{
            cache: "no-store",
            credentials: "same-origin"
        }});

        const json = await res.json();

        if(!json.ok){{
            flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>任务不存在</span>");
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
            html += "<span style='color:#dc2626;font-weight:800;'>错误：" + flsEscapeHtml(json.error) + "</span><br>";
        }}

        if(!json.running && json.filename){{
            html += "<br><a class='btn btn-primary' href='/backup/download/" + encodeURIComponent(json.filename) + "'>下载备份</a>";
        }}

        flsShowBackupJob(html);

        if(json.running){{
            setTimeout(function(){{
                flsPollBackupJob(jobId);
            }}, 1500);
        }}else{{
            flsRefreshBackupList();
        }}

    }} catch(e) {{
        flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>状态读取失败：" + flsEscapeHtml(String(e)) + "</span>");
    }}
}}

async function flsRefreshBackupList(){{
    try {{
        const res = await fetch("/api/backup/list", {{
            cache: "no-store",
            credentials: "same-origin"
        }});

        const json = await res.json();

        const tbody = document.getElementById("backupListTbody");
        if(!tbody) return;

        if(!json.items || !json.items.length){{
            tbody.innerHTML = "<tr><td colspan='4'>暂无备份</td></tr>";
            return;
        }}

        let html = "";

        for(const item of json.items){{
            html += "<tr>" +
                "<td><b>" + flsEscapeHtml(item.name) + "</b></td>" +
                "<td>" + flsEscapeHtml(item.size_text || "-") + "</td>" +
                "<td>" + flsEscapeHtml(item.mtime_text || "-") + "</td>" +
                "<td>" +
                    "<a class='btn btn-primary' href='/backup/download/" + encodeURIComponent(item.name) + "'>下载</a>" +
                    "<button class='btn btn-red' type='button' onclick='flsDeleteBackup(" + JSON.stringify(item.name) + ")'>删除</button>" +
                "</td>" +
            "</tr>";
        }}

        tbody.innerHTML = html;

        if(typeof flsEnhanceMobileTables === "function"){{
            flsEnhanceMobileTables(document);
        }}

    }} catch(e) {{
        alert("刷新备份列表失败：" + e);
    }}
}}

async function flsDeleteBackup(filename){{
    if(!confirm("确定删除备份 " + filename + " 吗？")) return;

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
            alert(json.msg || "删除失败");
            return;
        }}

        flsRefreshBackupList();

    }} catch(e) {{
        alert("删除请求失败：" + e);
    }}
}}
</script>
"""
    return layout("备份恢复", "backup", body)
