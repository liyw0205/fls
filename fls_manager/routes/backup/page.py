from ._common import *


@bp.route("/backup/delete", methods=["POST"])
def backup_delete():
    from .actions import submit_backup_deletion
    if any(len(request.form.getlist(key)) != 1 for key in ("filename", "version")):
        payload, status = {"msg": "请选择唯一备份文件及版本"}, 400
    else:
        payload, status = submit_backup_deletion(dict(request.form))
    body = f'<section class="section" role="alert"><p>{h(payload["msg"])}</p></section>'
    body += '<a class="btn btn-primary" href="/backup#backup-list">查询实际备份列表</a>'
    return layout("备份删除结果", "backup", body), status


@bp.route("/backup/cancel/<job_id>", methods=["POST"])
def backup_cancel(job_id):
    from .actions import submit_backup_cancellation
    payload, status = submit_backup_cancellation(job_id)
    body = f'<section class="section" role="alert"><p>{h(payload["msg"])}</p></section>'
    if payload.get("retry_allowed"):
        body += f'<form method="post" action="/backup/cancel/{h(job_id)}"><button class="btn btn-primary" type="submit">重试取消原备份</button></form>'
    body += f'<a class="btn btn-primary" href="/backup?job_id={h(job_id)}">查询原备份进度</a> <a class="btn btn-gray" href="/backup">返回备份列表</a>'
    return layout("备份取消结果", "backup", body), status


@bp.route("/backup/create", methods=["POST"])
def backup_create():
    from .actions import submit_backup_creation
    data = dict(request.form, items=request.form.getlist("items"))
    payload, status = submit_backup_creation(data)
    body = f'<section class="section" role="alert"><p>{h(payload["msg"])}</p></section>'
    job_id = payload.get("job_id")
    if job_id:
        body += f'<a class="btn btn-primary" href="/backup?job_id={h(job_id)}">查看原备份进度</a>'
    elif not payload.get("resolved"):
        fields = f'<input type="hidden" name="request_id" value="{h(data.get("request_id", ""))}">'
        fields += "".join(f'<input type="hidden" name="items" value="{h(item)}">' for item in data["items"])
        if payload.get("confirm_required"):
            fields += '<input type="hidden" name="confirm_retry" value="1">'
        body += f'<form method="post" action="/backup/create">{fields}<section class="section"><button class="btn btn-primary" type="submit">{"确认后继续原备份" if payload.get("confirm_required") else "查询原备份请求"}</button></section></form>'
    body += '<a class="btn btn-gray" href="/backup">返回备份列表</a>'
    return layout("备份创建结果", "backup", body), status


@bp.route("/backup")
def backup_page():
    from ...scheduler import get_scheduler_state
    from ...paths import SCHEDULER_STATE_FILE
    scheduler_recovery = ""
    fallback_progress = ""
    job_id = request.args.get("job_id", request.args.get("job", ""))
    if job_id:
        try:
            info = get_backup_job(job_id)
            message = ("状态：" + str(info.get("status", "-"))) if info else "任务不存在，请检查备份列表"
        except Exception:
            info = None
            message = "备份状态无法读取，请修复存储后重新查询"
        fallback_progress = f'<noscript><section class="section"><p>{h(message)}</p>'
        if info and info.get("running") and not info.get("cancel_requested"):
            fallback_progress += f'<form method="post" action="/backup/cancel/{h(job_id)}"><button class="btn btn-red" type="submit">取消备份</button></form>'
        fallback_progress += f'<a class="btn btn-gray" href="/backup?job_id={h(job_id)}">查询原备份进度</a></section></noscript>'
    if SCHEDULER_STATE_FILE.exists():
        state = get_scheduler_state()
        if state["status"] in ("failed", "unknown"):
            scheduler_recovery = f'<section class="section" role="alert"><p>调度器状态：{h(state["status_label"])}。{h(state["error"])}</p><form method="post" action="/scheduler/reload?back=/backup"><button class="btn btn-primary" type="submit">仅重新加载已保存任务</button></form></section>'
    header = page_header(
        "备份恢复",
        help_html="创建配置或脚本备份，查看进度，并在确认后恢复已有备份。",
    )
    body = f"""
{header}
{scheduler_recovery}
{fallback_progress}
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

    <form id="backupCreateForm" aria-label="创建备份" method="post" action="/backup/create">
        <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
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
        <noscript><button class="btn btn-primary" type="submit">提交备份</button></noscript>
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
    <div id="backupDeleteRecovery" role="status" hidden>
        <p id="backupDeleteMessage"></p>
        <button class="btn btn-gray" type="button" onclick="flsRefreshBackupList(this)">查询实际备份列表</button>
    </div>

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
    <div id="backupRestoreRecovery" role="alert"></div>
    <form method="post" action="/backup/import" enctype="multipart/form-data">
        <input type="hidden" name="restore_request_id" value="{uuid.uuid4().hex}">
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
    if(typeof flsShowFloatingPanel === "function"){{
        flsShowFloatingPanel("backupJobCard", text);
        return;
    }}
    var card = document.getElementById("backupJobCard");
    var el = document.getElementById("backupJobText");

    if(card) card.style.display = "block";
    if(el) el.innerHTML = text;
}}

function flsBackupRequest(){{
    let raw;
    try {{
        raw = sessionStorage.getItem("fls-backup-request");
    }} catch(error) {{
        return flsInvalidBackupRequestRecovery("本地备份请求状态无法读取，请先核对实际备份列表。");
    }}
    if(raw === null) return null;

    let pending;
    try {{
        pending = JSON.parse(raw);
    }} catch(error) {{
        return flsInvalidBackupRequestRecovery("本地备份请求状态损坏，原记录已保留；请先核对实际备份列表。");
    }}
    const validId = value => typeof value === "string" && /^[a-f0-9]{{32}}$/.test(value);
    if(!pending || typeof pending !== "object" || Array.isArray(pending)
        || pending.request_id !== undefined && !validId(pending.request_id)
        || pending.job_id !== undefined && !validId(pending.job_id)
        || !validId(pending.request_id) && !validId(pending.job_id)
        || pending.items !== undefined && (!Array.isArray(pending.items) || !pending.items.length
            || pending.items.some(item => item !== "data" && item !== "scripts")
            || new Set(pending.items).size !== pending.items.length)
        || !validId(pending.job_id) && (!Array.isArray(pending.items) || !pending.items.length)
        || pending.result_unknown !== undefined && typeof pending.result_unknown !== "boolean"
        || pending.archive_list_verified !== undefined && typeof pending.archive_list_verified !== "boolean") {{
        return flsInvalidBackupRequestRecovery("本地备份请求状态无效，原记录已保留；请先核对实际备份列表。");
    }}
    return pending;
}}

function flsInvalidBackupRequestRecovery(message){{
    flsSetBackupCreateLocked(true);
    flsShowBackupJob("<p role='alert'>" + flsEscapeHtml(message) + "</p><button class='btn btn-primary' type='button' onclick='flsRecoverInvalidBackupRequest(this)'>刷新备份列表并清理无效请求</button> <a class='btn btn-gray' href='#backup-list'>查看备份列表</a>");
    return {{invalid:true}};
}}

async function flsRecoverInvalidBackupRequest(source){{
    if(!confirm("将先刷新实际备份列表。只有列表读取成功后，才清理损坏的本地请求记录；确认继续吗？")) return;
    if(!flsMarkButtonBusy(source, "核对中...")) return;
    try {{
        if(!await flsRefreshBackupList()) throw new Error("备份列表读取失败");
        sessionStorage.removeItem("fls-backup-request");
        const form = document.getElementById("backupCreateForm");
        if(form) form.elements.request_id.value = flsNewBackupRequest();
        flsSetBackupCreateLocked(false);
        flsShowBackupJob("已刷新实际备份列表并清理无效的本地请求记录。请核对归档后再创建备份。");
    }} catch(error) {{
        flsInvalidBackupRequestRecovery("无法确认或清理本地请求状态，原记录仍保留；请重试。" + String(error));
    }} finally {{
        flsRestoreButton(source);
    }}
}}

function flsNewBackupRequest(){{
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), value=>value.toString(16).padStart(2, "0")).join("");
}}

function flsSetBackupCreateLocked(locked, items){{
    const form = document.getElementById("backupCreateForm");
    if(!form) return;
    if(Array.isArray(items)){{
        const selected = new Set(items);
        form.querySelectorAll('input[name="items"]').forEach(input=>{{input.checked=selected.has(input.value);}});
    }}
    form.querySelectorAll('input[name="items"], button').forEach(control=>{{control.disabled=!!locked;}});
}}

function flsConfirmBackupRecreate(jobId){{
    const pending = flsBackupRequest();
    if(!pending || pending.job_id !== jobId || pending.result_unknown !== true || pending.archive_list_verified !== true) return;
    if(!confirm("已查询当前备份列表。仍确认重新创建备份吗？")) return;
    sessionStorage.removeItem("fls-backup-request");
    const form = document.getElementById("backupCreateForm");
    form.elements.request_id.value = flsNewBackupRequest();
    flsSetBackupCreateLocked(false, pending.items);
    return flsCreateBackup(null);
}}

function flsResetBackupRequest(source){{
    if(!confirm("已核对备份列表，并确认需要重新选择创建备份吗？")) return;
    sessionStorage.removeItem("fls-backup-request");
    document.getElementById("backupCreateForm").elements.request_id.value = flsNewBackupRequest();
    document.querySelectorAll("#backupCreateForm button").forEach(flsRestoreButton);
    flsRestoreButton(source);
    flsShowBackupJob("请选择备份内容后创建新备份。");
}}

function flsBackupRecovery(message, confirmRequired){{
    flsShowBackupJob("<p role='alert'>" + flsEscapeHtml(message) + "</p><button class='btn btn-primary' type='button' onclick='flsResumeBackup(" + !!confirmRequired + ", this)'>" + (confirmRequired ? "确认后继续原备份" : "查询原备份请求") + "</button> <a class='btn btn-gray' href='/login?next=%2Fbackup'>重新登录</a>");
}}

function flsBackupJobRecovery(jobId, message, retryAllowed){{
    const argument = flsEscapeHtml(JSON.stringify(jobId));
    let html = "<p role='alert'>" + flsEscapeHtml(message) + "</p><button class='btn btn-primary' type='button' onclick='flsPollBackupJob(" + argument + ", this)'>查询原备份请求</button>";
    if(retryAllowed) html += " <button class='btn btn-red' type='button' onclick='flsCancelBackup(" + argument + ", this)'>重试取消原备份</button>";
    html += " <a class='btn btn-gray' href='/login?next=" + encodeURIComponent("/backup?job_id=" + jobId) + "'>重新登录</a>";
    flsShowBackupJob(html);
}}

function flsResumeBackup(confirmRetry, source){{
    const pending = flsBackupRequest();
    if(!pending || pending.invalid) return;
    if(pending.job_id) return flsPollBackupJob(pending.job_id, source);
    return flsCreateBackup(source, confirmRetry);
}}

async function flsCreateBackup(source, confirmRetry){{
    const form = document.getElementById("backupCreateForm");
    const pending = flsBackupRequest();
    if(pending && pending.invalid) return;
    if(pending && pending.job_id){{
        await flsPollBackupJob(pending.job_id, source);
        return;
    }}
    const fd = new FormData();
    const selected = pending || {{request_id: form.elements.request_id.value, items: new FormData(form).getAll("items")}};
    fd.append("request_id", selected.request_id);
    selected.items.forEach(item=>fd.append("items", item));
    if(confirmRetry) fd.append("confirm_retry", "1");

    if(!fd.getAll("items").length){{
        alert(flsActionFailure("请至少选择一个备份内容", "勾选配置或脚本后重试"));
        return;
    }}

    if(!flsMarkButtonBusy(source, "创建中...")) return;
    sessionStorage.setItem("fls-backup-request", JSON.stringify(selected));
    flsShowBackupJob("已提交备份任务，正在准备...");

    try {{
        const res = await fetch("/api/backup/create", {{
            method: "POST",
            body: fd,
            headers: {{"X-Requested-With":"XMLHttpRequest"}},
            credentials: "same-origin"
        }});

        const json = await flsReadJsonResponse(res);

        if(!json.ok){{
            if(json.job_id){{
                await flsPollBackupJob(json.job_id, source);
                return;
            }}
            flsShowBackupJob("<span style='color:#dc2626;font-weight:800;'>" + flsEscapeHtml(flsActionFailure("创建失败：" + (json.msg || "未知错误"), "检查备份内容后重试")) + "</span>");
            flsRestoreButton(source);
            return;
        }}

        sessionStorage.setItem("fls-backup-request", JSON.stringify({{...selected, job_id:json.job_id}}));
        await flsPollBackupJob(json.job_id, source);

    }} catch(e) {{
        const payload = e && e.payload;
        if(payload && payload.job_id) {{
            sessionStorage.setItem("fls-backup-request", JSON.stringify({{...selected, job_id:payload.job_id}}));
            await flsPollBackupJob(payload.job_id, source);
            return;
        }}
        if(payload && payload.resolved){{
            sessionStorage.removeItem("fls-backup-request");
            form.elements.request_id.value = flsNewBackupRequest();
            flsShowBackupJob("<p role='alert'>" + flsEscapeHtml(payload.msg) + "</p><a class='btn btn-gray' href='/backup'>返回备份列表</a>");
        }}else{{
            flsBackupRecovery("请求结果未确认：" + String(e), payload && payload.confirm_required);
        }}
        flsRestoreButton(source);
    }}
}}

async function flsPollBackupJob(jobId, source){{
    clearTimeout(window.flsBackupPollTimer);
    if(!document.getElementById("backupCreateForm")) return;
    try {{
        const res = await fetch("/api/backup/job/" + encodeURIComponent(jobId), {{
            cache: "no-store",
            credentials: "same-origin"
        }});

        const json = await flsReadJsonResponse(res);
        const pending = flsBackupRequest();
        if(pending && pending.job_id === jobId && !Array.isArray(pending.items)){{
            pending.items = Array.isArray(json.items) ? json.items : [];
            if(json.status === "结果未知") pending.result_unknown = true;
            sessionStorage.setItem("fls-backup-request", JSON.stringify(pending));
        }}

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

        if(json.cancel_error) html += "<p role='alert'>" + flsEscapeHtml(json.cancel_error) + "</p>";

        if(json.running && !json.cancel_requested){{
            html += "<br><button class='btn btn-red' type='button' onclick='flsCancelBackup(" + JSON.stringify(jobId) + ", this)'>取消备份</button>";
        }}

        if(!json.running && json.filename){{
            html += "<br><a class='btn btn-primary' href='/backup/download/" + encodeURIComponent(json.filename) + "'>下载备份</a>";
        }}

        if(!json.running && !json.filename){{
            if(json.status === "结果未知"){{
                const pending = flsBackupRequest();
                if(pending && pending.job_id === jobId && pending.archive_list_verified === true){{
                    html += "<p>备份列表已刷新，请核对归档后再确认是否重建。</p><button class='btn btn-orange' type='button' onclick='flsConfirmBackupRecreate(" + JSON.stringify(jobId) + ")'>确认后重新创建备份</button> ";
                }}else{{
                    html += "<p>请先核对已发布归档，再决定是否重新创建。</p><button class='btn btn-gray' type='button' onclick='flsRefreshBackupList(this, " + JSON.stringify(jobId) + ")'>查询已发布归档</button> ";
                }}
                flsSetBackupCreateLocked(true, pending && pending.job_id === jobId ? pending.items : null);
            }}else{{
                html += "<br><button class='btn btn-orange' type='button' onclick='flsCreateBackup(this)'>重新创建备份</button>";
            }}
        }}

        flsShowBackupJob(html);

        if(json.running){{
            window.flsBackupPollTimer = setTimeout(function(){{
                flsPollBackupJob(jobId, source);
            }}, 1500);
        }}else{{
            const pending = flsBackupRequest();
            if(pending && pending.job_id === jobId){{
                if(json.status === "结果未知"){{
                    pending.result_unknown = true;
                    sessionStorage.setItem("fls-backup-request", JSON.stringify(pending));
                    flsSetBackupCreateLocked(true, pending.items);
                }}else{{
                    sessionStorage.removeItem("fls-backup-request");
                    document.getElementById("backupCreateForm").elements.request_id.value = flsNewBackupRequest();
                    flsSetBackupCreateLocked(false);
                }}
            }}
            document.querySelectorAll("#backupCreateForm button").forEach(flsRestoreButton);
            flsRestoreButton(source);
            if(json.status === "结果未知"){{
                const current = flsBackupRequest();
                flsSetBackupCreateLocked(true, current && current.job_id === jobId ? current.items : null);
            }}
            flsRefreshBackupList();
        }}

    }} catch(e) {{
        if(e.status === 404){{
            flsShowBackupJob("<p role='alert'>原备份作业记录不存在，请先核对备份列表。</p><button class='btn btn-blue' type='button' onclick='flsRefreshBackupList(this)'>刷新备份列表</button> <button class='btn btn-orange' type='button' onclick='flsResetBackupRequest(this)'>核对后重新选择备份</button>");
        }}else{{
            flsBackupJobRecovery(jobId, "状态读取失败：" + String(e));
        }}
        flsRestoreButton(source);
    }}
}}

async function flsCancelBackup(jobId, source){{
    if(!confirm("确认取消正在执行的备份吗？")) return;
    if(!flsMarkButtonBusy(source, "取消中...")) return;
    clearTimeout(window.flsBackupPollTimer);

    try {{
        const res = await fetch("/api/backup/cancel/" + encodeURIComponent(jobId), {{
            method: "POST",
            credentials: "same-origin",
            headers: {{"X-Requested-With":"XMLHttpRequest"}}
        }});
        const json = await flsReadJsonResponse(res);
        if(!res.ok || !json.ok){{
            flsBackupJobRecovery(jobId, json.msg || "取消失败，请查询原备份状态", json.retry_allowed);
            flsRestoreButton(source);
            return;
        }}
        flsShowBackupJob(flsEscapeHtml(json.msg || "已请求取消"));
        await flsPollBackupJob(jobId, source);
    }} catch(e) {{
        const payload = e && e.payload;
        flsBackupJobRecovery(jobId, payload && payload.msg || "取消结果未确认，请查询原备份状态：" + String(e), payload && payload.retry_allowed);
        flsRestoreButton(source);
    }}
}}

function flsBackupArchiveListVerified(jobId){{
    const pending = flsBackupRequest();
    if(!pending || pending.job_id !== jobId || pending.result_unknown !== true) return;
    pending.archive_list_verified = true;
    sessionStorage.setItem("fls-backup-request", JSON.stringify(pending));
    flsPollBackupJob(jobId);
}}

async function flsRefreshBackupList(source, unknownJobId){{
    if(source && !flsMarkButtonBusy(source, "刷新中...")) return;
    try {{
        const res = await fetch("/api/backup/list", {{
            cache: "no-store",
            credentials: "same-origin"
        }});

        const json = await flsReadJsonResponse(res);
        if(!res.ok || !json.ok || !Array.isArray(json.items)){{
            throw new Error(flsActionFailure(json.msg || ("HTTP " + res.status), "检查服务状态后重试"));
        }}

        const tbody = document.getElementById("backupListTbody");
        if(!tbody) return false;
        const pending = JSON.parse(sessionStorage.getItem("fls-backup-delete") || "null");
        if(pending){{
            const current = (json.items || []).find(item => item.name === pending.filename);
            flsBackupDeleteRecovery(!current ? "原备份已不存在，请核对当前列表。" :
                current.version === pending.version ? "原备份仍存在，可核对后重新确认删除。" :
                "同名备份已变化，未继续删除，请核对当前列表后重新选择。");
            sessionStorage.removeItem("fls-backup-delete");
        }}

        if(!json.items || !json.items.length){{
            tbody.innerHTML = "<tr class='empty-state-row'><td colspan='4'><div class='empty-state'><p>暂无备份</p><div class='empty-state-actions'><a class='btn btn-primary' href='#backup-create'>创建备份</a></div></div></td></tr>";
            if(unknownJobId) flsBackupArchiveListVerified(unknownJobId);
            return true;
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
                        "<div class='row-actions-danger'><button class='btn btn-red' type='button' data-filename='" + flsEscapeHtml(item.name) + "' data-version='" + flsEscapeHtml(item.version) + "' onclick='flsDeleteBackup(this.dataset.filename, this.dataset.version, this)'>删除备份</button></div>" +
                    "</div>" +
                "</td>" +
            "</tr>";
        }}

        tbody.innerHTML = html;

        if(typeof flsEnhanceMobileTables === "function"){{
            flsEnhanceMobileTables(document);
        }}
        if(unknownJobId) flsBackupArchiveListVerified(unknownJobId);
        return true;

    }} catch(e) {{
        alert(flsActionFailure("刷新备份列表失败：" + e, "检查服务状态后重试"));
        return false;
    }} finally {{
        flsRestoreButton(source);
    }}
}}

function flsBackupDeleteRecovery(message){{
    document.getElementById("backupDeleteRecovery").hidden = false;
    document.getElementById("backupDeleteMessage").textContent = message;
}}

async function flsDeleteBackup(filename, version, source){{
    if(!confirm("确定删除备份 " + filename + " 吗？")) return;
    if(!flsMarkButtonBusy(source, "删除中...")) return;

    try {{
        sessionStorage.setItem("fls-backup-delete", JSON.stringify({{filename, version}}));
        const fd = new FormData();
        fd.append("filename", filename);
        fd.append("version", version);

        const res = await fetch("/api/backup/delete", {{
            method: "POST",
            body: fd,
            headers: {{"X-Requested-With":"XMLHttpRequest"}},
            credentials: "same-origin"
        }});

        const json = await flsReadJsonResponse(res);

        if(!json.ok){{
            flsBackupDeleteRecovery(json.msg || "删除失败，请查询实际备份列表");
            return;
        }}

        sessionStorage.removeItem("fls-backup-delete");
        flsBackupDeleteRecovery(json.msg || "已删除");
        await flsRefreshBackupList();

    }} catch(e) {{
        const payload = e && e.payload;
        flsBackupDeleteRecovery(payload && payload.msg || "删除结果未确认，请查询实际备份列表：" + String(e));
    }} finally {{
        flsRestoreButton(source);
    }}
}}

setTimeout(function(){{
    if(sessionStorage.getItem("fls-backup-delete")){{
        flsBackupDeleteRecovery("上次删除结果未确认，请先查询实际备份列表。");
    }}
    const pending = flsBackupRequest();
    const jobId = new URLSearchParams(location.search).get("job_id");
    if(pending && pending.invalid) return;
    if(pending){{
        if(pending.result_unknown) flsSetBackupCreateLocked(true, pending.items);
        if(pending.job_id) flsPollBackupJob(pending.job_id);
        else flsBackupRecovery("上次备份请求结果未确认，请先查询原请求。");
    }}else if(jobId && /^[a-f0-9]{{32}}$/.test(jobId)){{
        sessionStorage.setItem("fls-backup-request", JSON.stringify({{job_id:jobId}}));
        flsPollBackupJob(jobId);
    }}
}}, 0);
</script>
<script>
function flsRestorePending(){{
    const raw=sessionStorage.getItem("fls-backup-restore");
    return raw ? JSON.parse(raw) : null;
}}

function flsRestoreRecovery(message, payload){{
    const box=document.getElementById("backupRestoreRecovery");
    if(!box) return;
    let html="<p>"+flsEscapeHtml(message)+"</p><button class='btn btn-primary' type='button' onclick='flsQueryRestoreRequest(this)'>查询原恢复请求</button> ";
    if(payload && payload.error) html+="<pre class='section backup-restore-diagnostic' style='white-space:pre-wrap;overflow-wrap:anywhere;'>"+flsEscapeHtml(payload.error)+"</pre> ";
    if(payload && payload.scheduler_error) html+="<pre class='section backup-restore-diagnostic' style='white-space:pre-wrap;overflow-wrap:anywhere;'>"+flsEscapeHtml(payload.scheduler_error)+"</pre> ";
    if(payload && payload.scheduler_reload_required){{
        html+="<button class='btn btn-blue' type='button' onclick='flsReloadRestoredTasks(this)'>仅重新加载调度器</button> ";
    }}
    if(payload && payload.dependencies_failed) html+="<a class='btn btn-blue' href='/deps'>查看并修复依赖</a> ";
    if(payload && (payload.state==='received' || payload.state==='failed')){{
        html+="<button class='btn btn-orange' type='button' onclick='flsContinueRestoreRequest(this)'>选择原文件继续恢复</button> ";
    }}
    html+="<button class='btn btn-gray' type='button' onclick='flsResetRestoreRequest()'>核对后开始新的恢复</button> <a class='btn btn-gray' href='/login?next=%2Fbackup'>重新登录</a>";
    box.innerHTML=html;
}}

async function flsQueryRestoreRequest(source){{
    const pending=flsRestorePending();
    if(!pending) return;
    if(!flsMarkButtonBusy(source, "查询中...")) return;
    try{{
        const res=await fetch("/api/backup/restore/"+encodeURIComponent(pending.restore_request_id),{{cache:"no-store",credentials:"same-origin"}});
        let payload;
        try{{payload=await flsReadJsonResponse(res);}}catch(e){{
            payload=e.payload;
            if(!payload || !payload.state && e.status!==404) throw e;
            if(e.status===404) payload.state="received";
        }}
        if(payload.resolved){{
            sessionStorage.removeItem("fls-backup-restore");
            document.querySelector('form[action="/backup/import"] [name="restore_request_id"]').value=flsNewBackupRequest();
            document.getElementById("backupRestoreRecovery").textContent=payload.msg;
        }}else{{
            flsRestoreRecovery(payload.msg,payload);
        }}
    }}catch(e){{
        flsRestoreRecovery("原恢复状态读取失败："+String(e));
    }}finally{{flsRestoreButton(source);}}
}}

function flsContinueRestoreRequest(source){{
    const pending=flsRestorePending();
    if(!pending) return;
    pending.continue_upload=true;
    sessionStorage.setItem("fls-backup-restore",JSON.stringify(pending));
    const form=document.querySelector('form[action="/backup/import"]');
    form.elements.restore_request_id.value=pending.restore_request_id;
    Array.from(form.querySelectorAll('[name="restore_items"]')).forEach(input=>input.checked=pending.restore_items.includes(input.value));
    form.elements.restore_deps.checked=pending.restore_deps;
    document.getElementById("backupRestoreRecovery").textContent="请选择原备份文件，再提交原恢复请求。";
    form.elements.file.focus();
}}

function flsResetRestoreRequest(){{
    if(!confirm("已检查任务、脚本和调度器状态，确认开始新的恢复请求吗？")) return;
    sessionStorage.removeItem("fls-backup-restore");
    const form=document.querySelector('form[action="/backup/import"]');
    form.elements.restore_request_id.value=flsNewBackupRequest();
    document.getElementById("backupRestoreRecovery").textContent="";
}}

async function flsReloadRestoredTasks(source){{
    if(!flsMarkButtonBusy(source,"重载中...")) return;
    try{{
        const pending=flsRestorePending();
        await flsReadJsonResponse(await fetch("/api/backup/restore/"+encodeURIComponent(pending.restore_request_id)+"/reload",{{method:"POST",credentials:"same-origin"}}));
        flsRestoreRecovery("已重新加载当前任务，目录保持不变。可查询原恢复记录或确认开始新请求。");
    }}catch(e){{
        flsRestoreRecovery("调度器重载未确认："+String(e),{{scheduler_reload_required:true}});
    }}finally{{flsRestoreButton(source);}}
}}

setTimeout(function(){{
    if(flsRestorePending()) flsRestoreRecovery("原恢复请求结果未确认，请先查询，避免再次覆盖目录。");
}},0);
</script>
"""
    return layout("备份恢复", "backup", body)
