/* ============================================================
   FLS layout extracted scripts
   ============================================================ */

function flsGetCsrfToken(){
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? (meta.getAttribute("content") || "") : "";
}

function flsFetchNeedsCsrf(input, init){
    try {
        const method = String(
            (init && init.method) ||
            (input && input.method) ||
            "GET"
        ).toUpperCase();

        if(["GET", "HEAD", "OPTIONS", "TRACE"].indexOf(method) >= 0) {
            return false;
        }

        const rawUrl = typeof input === "string" ? input : (input && input.url);
        const url = new URL(rawUrl || location.href, location.href);

        return url.origin === location.origin;
    } catch(e) {
        return false;
    }
}

function flsInitCsrfFetchPatch(){
    if(window.__FLS_CSRF_FETCH_PATCHED__) return;
    if(typeof window.fetch !== "function") return;

    const originalFetch = window.fetch.bind(window);

    window.fetch = function(input, init){
        init = init || {};

        if(flsFetchNeedsCsrf(input, init)){
            const token = flsGetCsrfToken();

            if(token){
                const headers = new Headers(init.headers || (input && input.headers) || {});
                if(!headers.has("X-CSRF-Token")){
                    headers.set("X-CSRF-Token", token);
                }
                init.headers = headers;
            }
        }

        return originalFetch(input, init);
    };

    window.__FLS_CSRF_FETCH_PATCHED__ = true;
}

flsInitCsrfFetchPatch();

function flsMarkFormSubmitting(form, submitter){
    if(!form || form.dataset.submitting === "true") return;
    form.dataset.submitting = "true";
    const button = submitter || form.querySelector('button[type="submit"], input[type="submit"]');
    if(!button || button.dataset.noBusyState === "true") return;
    button.dataset.originalLabel = button.innerText || button.value || "";
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    if(button.tagName === "INPUT") button.value = "处理中...";
    else button.innerText = "处理中...";
}

function flsMarkButtonBusy(button, label){
    if(!button) return true;
    if(button.dataset.busy === "true") return false;

    button.dataset.busy = "true";
    button.dataset.originalLabel = button.innerText || button.value || "";
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    label = String(label || "处理中...");

    if(button.tagName === "INPUT") button.value = label;
    else button.innerText = label;

    return true;
}

function flsRestoreButton(button){
    if(!button) return;

    const original = button.dataset.originalLabel;
    if(original !== undefined){
        if(button.tagName === "INPUT") button.value = original;
        else button.innerText = original;
    }

    button.disabled = false;
    button.removeAttribute("aria-busy");
    delete button.dataset.busy;
}

function flsActionFailure(message, nextStep){
    const text = String(message || "操作未完成");
    const next = String(nextStep || "刷新页面后重试");
    return text + "。下一步：" + next;
}

function flsResponseErrorDiagnostic(response, body){
    let payload = null;
    try { payload = JSON.parse(body); } catch(e) {}
    const message = payload && (payload.msg || payload.error || payload.message);
    if(message) return {payload:payload, message:String(message)};

    let detail = String(body || "");
    const contentType = response.headers && response.headers.get
        ? String(response.headers.get("content-type") || "").toLowerCase() : "";
    if(contentType.includes("text/html") || /^\s*<!doctype\s+html|^\s*<html\b/i.test(detail)){
        if(typeof DOMParser !== "undefined"){
            const doc = new DOMParser().parseFromString(detail, "text/html");
            detail = doc.body ? (doc.body.innerText || doc.body.textContent || "") : "";
        }else{
            detail = detail.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, " ")
                .replace(/<[^>]*>/g, " ");
        }
    }
    detail = detail.replace(/\s+/g, " ").trim().slice(0, 500);
    return {payload:payload, message:detail};
}

async function flsReadJsonResponse(response){
    if(!response || typeof response.ok !== "boolean"){
        throw new Error("响应对象不可用");
    }

    const status = response.status;
    // Check status before parsing and consume each native response body once.
    if(typeof response.text === "function"){
        if(!response.ok){
            let body = "";
            try { body = await response.text(); } catch(e) {}
            const diagnostic = flsResponseErrorDiagnostic(response, body);
            const error = new Error("HTTP " + status + (diagnostic.message ? ": " + diagnostic.message : ""));
            error.status = status;
            error.payload = diagnostic.payload;
            throw error;
        }
        let body = "";
        try { body = await response.text(); } catch(e) {}
        try {
            return JSON.parse(body);
        } catch(e) {
            const error = new Error("响应解析失败：" + status);
            error.status = status;
            throw error;
        }
    }

    // Keep lightweight response doubles used by legacy callers/tests compatible.
    if(!response.ok){
        let payload = null;
        try {
            payload = await response.json();
        } catch(e) {}

        const message = payload && (payload.msg || payload.error || payload.message);
        const error = new Error(String(message || ("HTTP " + status)));
        error.status = status;
        error.payload = payload;
        throw error;
    }

    try {
        return await response.json();
    } catch(e) {
        const error = new Error("响应解析失败：" + status);
        error.status = status;
        throw error;
    }
}

async function flsReadTextResponse(response){
    if(!response || typeof response.ok !== "boolean"){
        throw new Error("响应对象不可用");
    }
    if(!response.ok){
        let body = "";
        try { body = await response.text(); } catch(e) {}
        const diagnostic = flsResponseErrorDiagnostic(response, body);
        const error = new Error("HTTP " + response.status + (diagnostic.message ? ": " + diagnostic.message : ""));
        error.status = response.status;
        error.payload = diagnostic.payload;
        throw error;
    }
    return response.text();
}

function flsLogPollRecovered(loader){
    const recovery = document.getElementById("logRecovery");
    if(recovery){
        recovery.hidden = true;
        recovery.replaceChildren();
    }
    window.__FLS_LOG_AUTH_BLOCKED__ = false;
    if(typeof loader === "function" && !window.__FLS_ACTIVE_LOG_INTERVAL__){
        window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loader, 2000);
    }
}

function flsLogPollFailed(error, loader, label){
    const recovery = document.getElementById("logRecovery");
    if(!recovery) return;
    recovery.replaceChildren();
    const status = Number(error && error.status || 0);
    const message = document.createElement("p");
    if(status === 401 || status === 403){
        message.textContent = status === 401
            ? "会话已失效，现有日志已保留；重新登录后可继续读取。"
            : "当前会话无权读取日志；现有日志已保留，请核对权限或重新登录。";
        const login = document.createElement("a");
        login.className = "btn btn-primary";
        login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        login.textContent = "重新登录";
        recovery.append(message, login);
        window.__FLS_LOG_AUTH_BLOCKED__ = true;
    }else{
        message.textContent = String(label || "日志") + "读取失败：" + String(error || "未知错误") + "。现有日志已保留，请重试。";
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn btn-primary";
        retry.textContent = "立即重试";
        retry.addEventListener("click", function(){
            if(typeof loader === "function") loader();
        });
        recovery.append(message, retry);
    }
    recovery.hidden = false;
    if(window.__FLS_ACTIVE_LOG_INTERVAL__){
        clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
        window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
    }
}

document.addEventListener("submit", function(event){
    const form = event.target;
    if(!form || String(form.dataset.noBusyState || "") === "true") return;
    flsMarkFormSubmitting(form, event.submitter || null);
});

function flsToast(message, type, timeout){
    message = String(message || "");
    type = type || "info";
    timeout = timeout || 3200;

    if(!message) return;

    let box = document.getElementById("flsToastBox");

    if(!box){
        box = document.createElement("div");
        box.id = "flsToastBox";
        box.className = "fls-toast-box";
        document.body.appendChild(box);
    }

    const item = document.createElement("div");
    item.className = "fls-toast " + type;
    item.textContent = message;
    item.addEventListener("click", function(){
        item.remove();
    });

    box.appendChild(item);

    setTimeout(function(){
        item.remove();
    }, timeout);
}

function flsConvertFeedbackCards(root){
    root = root || document;

    root.querySelectorAll(".feedback").forEach(function(feedback){
        const card = feedback.closest(".card");
        if(!card || card.dataset.flsFeedbackHandled === "1") return;

        const kindClass = Array.from(feedback.classList).find(function(name){
            return name.indexOf("feedback-") === 0;
        }) || "feedback-info";
        const type = kindClass.replace("feedback-", "") || "info";
        const text = (feedback.textContent || "").trim();

        card.dataset.flsFeedbackHandled = "1";
        if(text) flsToast(text, type, type === "error" ? 6200 : 4200);
        card.remove();
    });
}

function flsShowFloatingPanel(id, html){
    const card = document.getElementById(String(id || ""));
    if(!card) return;

    card.classList.add("fls-floating-panel");
    card.style.display = "block";
    const text = card.querySelector(".help") || card.querySelector("[id$='Text']");
    if(text && html !== undefined) text.innerHTML = String(html);

    if(!card.querySelector(".fls-floating-panel-close")){
        const close = document.createElement("button");
        close.type = "button";
        close.className = "fls-floating-panel-close";
        close.setAttribute("aria-label", "关闭浮窗");
        close.title = "关闭浮窗";
        close.textContent = "×";
        close.addEventListener("click", function(){ card.style.display = "none"; });
        card.appendChild(close);
    }
}

function flsDisclosureDetails(details){
    return details && details.matches(
        "details.detail-disclosure, details.fls-collapsible-value, " +
        "details.fls-fold-card, details.log-group-card, details.fls-update-log-fold"
    );
}

function flsCloseDisclosureModal(){
    const modal = document.querySelector(".fls-disclosure-modal");
    if(!modal) return false;

    const trigger = modal.__flsTrigger;
    modal.remove();
    document.body.classList.remove("fls-modal-open");
    if(trigger && document.body.contains(trigger)) trigger.focus({preventScroll:true});
    return true;
}

function flsOpenModalContent(titleText, content, trigger){
    flsCloseDisclosureModal();

    const modal = document.createElement("div");
    modal.className = "fls-disclosure-modal";
    modal.setAttribute("role", "presentation");
    modal.__flsTrigger = trigger || null;

    const dialog = document.createElement("section");
    dialog.className = "fls-disclosure-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const head = document.createElement("div");
    head.className = "fls-disclosure-dialog-head";
    const title = document.createElement("div");
    title.className = "fls-disclosure-dialog-title";
    title.textContent = String(titleText || "查看详情").trim();
    const close = document.createElement("button");
    close.type = "button";
    close.className = "fls-disclosure-dialog-close";
    close.setAttribute("aria-label", "关闭详情");
    close.title = "关闭详情";
    close.textContent = "×";
    close.addEventListener("click", flsCloseDisclosureModal);
    head.appendChild(title);
    head.appendChild(close);

    const body = document.createElement("div");
    body.className = "fls-disclosure-dialog-body";
    body.appendChild(content);

    dialog.appendChild(head);
    dialog.appendChild(body);
    modal.appendChild(dialog);
    modal.addEventListener("click", function(event){
        if(event.target === modal) flsCloseDisclosureModal();
    });
    document.body.appendChild(modal);
    document.body.classList.add("fls-modal-open");
    close.focus({preventScroll:true});
    return modal;
}

function flsConfirmPanelControl(button, action){
    const form = button && button.closest ? button.closest("form") : null;
    if(!form) return true;

    const isStop = String(action || "") === "stop";
    const title = isStop ? "确认停止面板" : "确认重启面板";
    const message = isStop
        ? "停止后面板将无法继续提供服务，需要手动重新启动。"
        : "重启期间页面会短暂无法访问，当前任务和操作可能受到影响。";

    const content = document.createElement("div");
    content.className = "fls-panel-confirm-content";

    const text = document.createElement("p");
    text.className = "help";
    text.textContent = message;
    content.appendChild(text);

    const actions = document.createElement("div");
    actions.className = "action-row fls-panel-confirm-actions";

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn btn-gray";
    cancel.textContent = "取消";
    cancel.addEventListener("click", flsCloseDisclosureModal);

    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = isStop ? "btn btn-red" : "btn btn-orange";
    confirm.textContent = isStop ? "确认停止" : "确认重启";
    confirm.addEventListener("click", function(){
        flsCloseDisclosureModal();
        if(typeof form.requestSubmit === "function"){
            form.requestSubmit(button);
        }else{
            form.submit();
        }
    });

    actions.appendChild(cancel);
    actions.appendChild(confirm);
    content.appendChild(actions);

    const modal = flsOpenModalContent(title, content, button);
    modal.classList.add("fls-panel-confirm-modal");
    const dialog = modal.querySelector(".fls-disclosure-dialog");
    if(dialog) dialog.classList.add("fls-panel-confirm-dialog");
    return false;
}

function flsOpenDisclosureModal(details){
    const content = document.createDocumentFragment();
    Array.from(details.children).forEach(function(child){
        if(child.tagName !== "SUMMARY") content.appendChild(child.cloneNode(true));
    });
    return flsOpenModalContent(
        (details.querySelector("summary")?.textContent || "查看详情").trim(),
        content,
        details.querySelector("summary")
    );
}

async function flsFetchUpdateInfo(){
    const response = await fetch("/api/about/update-info?wait=1", {
        cache:"no-store",
        headers:{"X-Requested-With":"XMLHttpRequest"},
        credentials:"same-origin"
    });

    return flsReadJsonResponse(response);
}

function flsRenderUpdateNotice(info){
    info = info || {};
    const slot = document.getElementById("flsUpdateNoticeSlot");
    if(!slot) return;

    slot.replaceChildren();

    const current = document.createElement("div");
    current.className = "fls-version-current";
    const label = document.createElement("span");
    label.textContent = "当前版本";
    const value = document.createElement("code");
    value.textContent = String((info && info.current_version) || "--");
    current.appendChild(label);
    current.appendChild(value);
    slot.appendChild(current);

    const errorText = String(info.error || "").trim().slice(0, 500);
    if(errorText){
        const error = document.createElement("div");
        error.className = "help fls-update-notice-error";
        error.textContent = "更新检查失败：" + errorText;

        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn btn-gray";
        retry.textContent = "重试检查";
        retry.addEventListener("click", function(){
            if(!flsMarkButtonBusy(retry)) return;
            window.__FLS_UPDATE_NOTICE_RESOLVED__ = false;
            flsCheckUpdateNoticeCache().finally(function(){
                flsRestoreButton(retry);
            });
        });
        error.appendChild(retry);
        slot.appendChild(error);
    }

    if(!info || !info.available || !info.version) return;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "fls-update-notice";
    button.setAttribute("aria-label", "查看版本 " + info.version + " 的更新日志");
    button.title = "查看更新日志";
    button.append(document.createTextNode(info.version));

    const badge = document.createElement("span");
    badge.className = "fls-update-notice-new";
    badge.textContent = "new";
    button.appendChild(badge);
    button.addEventListener("click", flsOpenUpdateLogModal);
    slot.appendChild(button);
}

async function flsOpenUpdateLogModal(event){
    const trigger = event && event.currentTarget;
    let response;
    try {
        response = await fetch("/about", {
            cache:"no-store",
            headers:{"X-Requested-With":"XMLHttpRequest"},
            credentials:"same-origin"
        });
        const aboutHtml = await flsReadTextResponse(response);
        const about = new DOMParser().parseFromString(aboutHtml, "text/html");
        const versionCard = about.querySelector("#about-version");
        const updates = about.querySelector("#about-updates");
        if(!versionCard || !updates){
            const error = new Error("更新日志内容不可用");
            flsRenderUpdateLogFailure(trigger, error);
            return;
        }

        const content = document.createElement("div");
        content.className = "fls-update-page-modal-content";
        const versionCopy = versionCard.cloneNode(true);
        versionCopy.removeAttribute("id");
        content.appendChild(versionCopy);

        const updateCard = document.createElement("section");
        updateCard.className = updates.className;
        updateCard.classList.remove("fls-update-log-fold");
        const summary = updates.querySelector("summary");
        if(summary){
            const heading = document.createElement("div");
            heading.className = "fls-update-modal-heading";
            while(summary.firstChild) heading.appendChild(summary.firstChild);
            updateCard.appendChild(heading);
        }
        Array.from(updates.children).forEach(function(child){
            if(child !== summary) updateCard.appendChild(child.cloneNode(true));
        });
        content.appendChild(updateCard);

        flsOpenModalContent("当前版本 / 更新日志", content, trigger);
        return;
    } catch(e) {
        flsRenderUpdateLogFailure(trigger, e);
        return;
    }
}

function flsRenderUpdateLogFailure(trigger, error){
    error = error || {};
    const status = Number(error.status || 0);
    const detail = String(error.message || "无法读取更新日志").slice(0, 600);
    const content = document.createElement("div");
    content.className = "help fls-update-log-error";
    content.setAttribute("role", "alert");
    content.textContent = "更新日志读取失败" + (status ? "（HTTP " + status + "）" : "") + "：" + detail;

    const actions = document.createElement("div");
    actions.className = "action-row";
    if(status === 401 || status === 403){
        const login = document.createElement("a");
        login.className = "btn btn-gray";
        login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        login.textContent = "重新登录";
        actions.appendChild(login);
    } else {
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn btn-primary";
        retry.textContent = "重新读取更新日志";
        retry.addEventListener("click", function(){
            if(!flsMarkButtonBusy(retry)) return;
            flsOpenUpdateLogModal({currentTarget:trigger}).finally(function(){
                flsRestoreButton(retry);
            });
        });
        actions.appendChild(retry);
    }
    content.appendChild(document.createElement("br"));
    content.appendChild(actions);
    flsOpenModalContent("更新日志不可用", content, trigger);
}

const FLS_UPDATE_NOTICE_CACHE_KEY = "fls-update-notice-20260919-5";

function flsReadUpdateNoticeCache(){
    try {
        const raw = localStorage.getItem(FLS_UPDATE_NOTICE_CACHE_KEY);
        const value = raw ? JSON.parse(raw) : null;
        return value && typeof value === "object" ? value : null;
    } catch(e) {
        return null;
    }
}

function flsWriteUpdateNoticeCache(info){
    if(!info || info.checking) return;

    try {
        localStorage.setItem(FLS_UPDATE_NOTICE_CACHE_KEY, JSON.stringify({
            available:!!info.available,
            version:String(info.version || ""),
            current_version:String(info.current_version || ""),
            checked_at:String(info.checked_at || ""),
            saved_at:Date.now()
        }));
    } catch(e) {
        // Storage can be unavailable in private browsing contexts.
    }
}

function flsMergeUpdateNoticeInfo(primary, fallback){
    primary = primary || {};
    fallback = fallback || {};
    return {
        available:!!primary.available,
        version:String(primary.version || ""),
        current_version:String(primary.current_version || fallback.current_version || ""),
        checked_at:String(primary.checked_at || fallback.checked_at || "")
    };
}

async function flsCheckUpdateNoticeCache(){
    if(window.__FLS_UPDATE_NOTICE_CHECKING__ || window.__FLS_UPDATE_NOTICE_RESOLVED__) return;
    window.__FLS_UPDATE_NOTICE_CHECKING__ = true;

    try {
        const info = await flsFetchUpdateInfo();
        const cached = flsReadUpdateNoticeCache();
        const rendered = flsMergeUpdateNoticeInfo(info, cached);
        flsRenderUpdateNotice(rendered);

        if(!info.checking && info.ok){
            flsWriteUpdateNoticeCache(info);
            window.__FLS_UPDATE_NOTICE_RESOLVED__ = true;
        }
    } catch(e) {
        const cached = flsReadUpdateNoticeCache();
        const diagnostic = e && e.payload && (e.payload.error || e.payload.msg)
            ? (e.payload.error || e.payload.msg)
            : (e && e.message) || "无法读取更新状态";
        const rendered = flsMergeUpdateNoticeInfo(cached, cached);
        rendered.error = String(diagnostic);
        flsRenderUpdateNotice(rendered);
    } finally {
        window.__FLS_UPDATE_NOTICE_CHECKING__ = false;
    }
}

function flsInitUpdateNotice(){
    if(window.__FLS_UPDATE_NOTICE_INITIALIZED__) return;
    window.__FLS_UPDATE_NOTICE_INITIALIZED__ = true;

    const cached = flsReadUpdateNoticeCache();
    flsRenderUpdateNotice(cached);
}

document.addEventListener("click", function(event){
    const summary = event.target.closest && event.target.closest("summary");
    const details = summary && summary.parentElement;
    if(!flsDisclosureDetails(details)) return;

    event.preventDefault();
    event.stopPropagation();
    flsOpenDisclosureModal(details);
}, true);

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function(){
        flsConvertFeedbackCards(document);
        flsInitUpdateNotice();
    });
} else {
    flsConvertFeedbackCards(document);
    flsInitUpdateNotice();
}

if(!window.__FLS_ALERT_PATCHED__){
    window.__FLS_ORIGINAL_ALERT__ = window.alert;
    window.alert = function(message){
        flsToast(message, "info");
    };
    window.__FLS_ALERT_PATCHED__ = true;
}

function flsNumberValue(value){
    const numberValue = Number(value || 0);

    if(!Number.isFinite(numberValue) || numberValue < 0) return 0;

    return Math.floor(numberValue);
}

function flsFailureSummary(failures, failedCount, prefix){
    if(!Array.isArray(failures) || !failures.length || !failedCount) return "";

    const first = failures.slice(0, 3).map(function(item){
        return String(item || "");
    }).filter(Boolean);

    if(!first.length) return "";

    let text = failedCount + " 个" + prefix + "：" + first.join("；");

    if(failedCount > first.length){
        text += " 等 " + failedCount + " 个";
    }

    return text;
}

function flsBulkActionMessage(json, fallback){
    json = json || {};

    const action = String(json.action || "");
    const fallbackText = String(fallback || json.msg || "");

    if(!json.ok){
        return String(json.msg || fallbackText || "操作未完成，请检查输入后重试");
    }

    const count = flsNumberValue(json.count);
    const updatedCount = flsNumberValue(json.updated_count);
    const deletedCount = flsNumberValue(json.deleted_count);
    const submittedCount = flsNumberValue(json.submitted_count);
    const stoppedCount = flsNumberValue(json.stopped_count);
    const skippedCount = flsNumberValue(json.skipped_count);
    const failedCount = flsNumberValue(json.failed_count);
    const parts = [];

    if(action === "enable"){
        parts.push("已启用 " + updatedCount + " 个任务");
    } else if(action === "disable"){
        parts.push("已禁用 " + updatedCount + " 个任务");
    } else if(action === "clear_collection"){
        parts.push("已取出 " + updatedCount + " 个任务");
    } else if(action === "delete"){
        parts.push("已删除 " + deletedCount + " 个任务");
        const failureText = flsFailureSummary(json.failures, failedCount, "删除失败");
        if(failureText) parts.push(failureText);
    } else if(action === "run"){
        parts.push("已提交运行 " + submittedCount + " 个任务");
        const failureText = flsFailureSummary(json.failures, failedCount, "未运行");
        if(failureText) parts.push(failureText);
    } else if(action === "stop"){
        parts.push("已结束 " + stoppedCount + " 个任务");
        if(skippedCount) parts.push("跳过 " + skippedCount + " 个未运行任务");
        const failureText = flsFailureSummary(json.failures, failedCount, "结束失败");
        if(failureText) parts.push(failureText);
    }

    if(parts.length) return parts.join("；");

    return fallbackText;
}

function flsBulkStorageKey(scope){
    return "fls-task-bulk:" + scope;
}

function flsBulkState(scope){
    const saved = sessionStorage.getItem(flsBulkStorageKey(scope));
    return saved ? JSON.parse(saved) : {};
}

function flsBulkSave(scope, state){
    sessionStorage.setItem(flsBulkStorageKey(scope), JSON.stringify(state));
}

function flsBulkNotice(options, state, error){
    const block = document.querySelector(options.block);
    if(!block) return;
    let notice = Array.from(block.querySelectorAll("[data-bulk-recovery]")).find(el => el.dataset.bulkRecovery === options.scope);
    if(!notice){
        notice = document.createElement("section");
        notice.className = "section";
        notice.dataset.bulkRecovery = options.scope;
        notice.setAttribute("role", "status");
        block.prepend(notice);
    }
    notice.replaceChildren();
    const payload = state.payload || {};
    if(payload.action === "clear_collection" && typeof payload.expected_collection === "string"){
        options.expectedCollection = payload.expected_collection;
    }
    const message = document.createElement("p");
    message.textContent = error || payload.msg || flsBulkActionMessage(payload, "操作结果尚未确认");
    notice.append(message);
    if(options.single && ["run", "stop"].includes(payload.action)){
        const log = document.createElement("a");
        log.className = "btn btn-gray";
        log.href = "/log/" + encodeURIComponent(options.ids()[0]);
        log.textContent = "查看任务日志";
        notice.append(log);
    }
    (payload.created_ids || []).forEach(id => {
        if(!/^[a-f0-9]{32}$/.test(id)) return;
        const link = document.createElement("a");
        link.className = "btn btn-blue";
        link.href = "/task/edit/" + id + "?back=" + encodeURIComponent(location.pathname + location.search);
        link.textContent = "查看已保存副本";
        notice.append(link);
    });
    function button(label, callback){
        const control = document.createElement("button");
        control.type = "button";
        control.className = "btn btn-primary";
        control.textContent = label;
        control.onclick = () => callback(control);
        notice.append(control);
    }
    if(state.pending){
        button("查询本次操作结果", control => flsSubmitTaskBulk(options, state.pending.action, control, true));
        const login = document.createElement("a");
        login.className = "btn btn-gray";
        login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
        login.textContent = "重新登录";
        notice.append(login);
    } else if(payload.reselection_required){
        const link = document.createElement("a");
        link.className = "btn btn-primary";
        link.href = "/collections";
        link.textContent = "重新选择合集和任务";
        notice.append(link);
    } else if(payload.resume_request && !payload.saved && state.retry_request){
        button("继续删除合集", control => {
            if(!confirm("确认继续删除该合集？")) return;
            state.pending = state.retry_request;
            flsBulkSave(options.scope, state);
            return flsSubmitTaskBulk(options, payload.action, control, true);
        });
    } else if((payload.retry_ids || []).length){
        button("重试未完成项", control => {
            options.select(payload.retry_ids);
            return flsSubmitTaskBulk(options, payload.action, control);
        });
    }
    if(payload.reload_required){
        button("重新加载调度器", async control => {
            if(!flsMarkButtonBusy(control)) return;
            let applied = false;
            try {
                const response = await fetch("/api/scheduler/reload", {
                    method:"POST", credentials:"same-origin",
                    headers:{"X-CSRF-Token":flsGetCsrfToken(), "X-Requested-With":"XMLHttpRequest"}
                });
                const result = await flsReadJsonResponse(response);
                if(!result.ok) throw new Error(result.msg);
                applied = true;
                payload.reload_required = false;
                payload.msg = "已重新加载调度器";
                flsBulkSave(options.scope, state);
                await options.refresh();
                options.select(state.selection || []);
                flsBulkNotice(options, state);
            } catch(e){
                flsBulkNotice(options, state, (applied ? "调度器已重新加载，页面恢复失败：" : "调度器重载失败：") + e);
            } finally {
                flsRestoreButton(control);
            }
        });
    }
    if(/^\/scripts\/rename-recovery\/[a-f0-9]{32}$/.test(payload.recovery_url || "")){
        const link = document.createElement("a");
        link.className = "btn btn-orange";
        link.href = payload.recovery_url;
        link.textContent = "恢复重命名";
        notice.append(link);
    }
    if(payload.recovery_url === "/proxy"){
        const link = document.createElement("a");
        link.className = "btn btn-orange";
        link.href = "/proxy";
        link.textContent = "修复所选代理";
        notice.append(link);
    }
    if((payload.unknown_ids || []).length && payload.action !== "delete_collection"){
        const link = document.createElement("a");
        link.href = "/history";
        link.className = "btn btn-gray";
        link.textContent = "查看任务历史";
        notice.append(link);
        button(options.single || options.confirmRetry ? "确认后重试本项" : "重新选择结果未知项", control => {
            if(!confirm("结果未知的任务可能已执行。确认查看任务历史后重新选择？")) return;
            state.selection = Array.from(new Set([...(state.selection || []), ...payload.unknown_ids]));
            flsBulkSave(options.scope, state);
            options.select(state.selection);
            if(options.single || options.confirmRetry) return flsSubmitTaskBulk(options, payload.action, control);
        });
    }
    button("刷新列表", async control => {
        if(!flsMarkButtonBusy(control)) return;
        try {
            await options.refresh();
            options.select(state.selection || []);
            flsBulkNotice(options, state);
        } catch(e){
            message.textContent = "列表刷新失败：" + e;
        } finally { flsRestoreButton(control); }
    });
}

function flsRestoreTaskBulk(options){
    try {
        const state = flsBulkState(options.scope);
        if(state.selection) options.select(state.selection);
        if(state.pending || state.payload) flsBulkNotice(options, state);
    } catch(e){
        // A storage error must not erase the current checkbox selection.
    }
}

async function flsSubmitTaskBulk(options, action, source, replay){
    if(window.__flsTaskBulkBusy) return;
    const ids = options.ids();
    let state;
    try {
        state = flsBulkState(options.scope);
        if(state.pending && !replay){
            flsBulkNotice(options, state, "上次请求尚未确认，请先查询本次操作结果");
            return;
        }
        if(!state.pending){
            if(!ids.length){ alert("请选择任务"); return; }
            const bytes = crypto.getRandomValues(new Uint8Array(16));
            const requestId = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
            state = {selection:ids, pending:{action:action, task_ids:ids, request_id:requestId}};
            if(action === "clear_collection" && options.expectedCollection !== undefined){
                state.pending.expected_collection = options.expectedCollection;
            }
            if(action === "assign_collection" && options.expectedCollections !== undefined){
                state.pending.expected_collections = options.expectedCollections;
            }
            flsBulkSave(options.scope, state);
        }
    } catch(e){
        alert("无法保存提交标识，未执行操作：" + e);
        return;
    }
    window.__flsTaskBulkBusy = true;
    flsMarkButtonBusy(source, "处理中...");
    try {
        let payload;
        try {
            const res = await fetch(options.endpoint ? options.endpoint(state.pending) : "/api/task/bulk-action", {
                method:"POST", credentials:"same-origin",
                headers:{"Content-Type":"application/json", "X-Requested-With":"XMLHttpRequest", "X-CSRF-Token":flsGetCsrfToken()},
                body:JSON.stringify(state.pending)
            });
            payload = await flsReadJsonResponse(res);
        } catch(e){
            if(!e.payload || !Array.isArray(e.payload.results)) throw e;
            payload = e.payload;
        }
        if(!Array.isArray(payload.results)) throw new Error(payload.msg || "操作结果不完整");
        state = {payload:payload, selection:payload.retry_ids || [],
            retry_request:payload.resume_request ? state.pending || state.retry_request : undefined};
        flsBulkSave(options.scope, state);
        // Apply confirmed results before refreshing, so a failed GET cannot rerun successes.
        options.select(state.selection);
        flsClearPageCache();
        try {
            await options.refresh();
            options.select(state.selection);
            flsBulkNotice(options, state);
        } catch(e){
            flsBulkNotice(options, state, payload.msg + "；列表刷新失败：" + e);
        }
    } catch(e){
        flsBulkNotice(options, state, "请求结果未确认：" + e);
    } finally {
        window.__flsTaskBulkBusy = false;
        flsRestoreButton(source);
        options.select(state.selection || ids);
    }
}

function flsMediaQuery(query){
    try {
        return !!(window.matchMedia && window.matchMedia(query).matches);
    } catch(e) {
        return false;
    }
}

function flsIsMobileViewport(){
    return flsMediaQuery("(max-width: 767px)");
}

function flsIsDrawerViewport(){
    return flsMediaQuery("(max-width: 1199px)");
}

function flsSyncMenuViewport(){
    if (!flsIsDrawerViewport()) {
        toggleMenu(false);
        flsCheckUpdateNoticeCache();
    }
}

function toggleMenu(show){
    const sidebar = document.getElementById("sidebar");
    const mask = document.getElementById("mask");
    const btn = document.getElementById("flsFloatMenuBtn");

    if (!sidebar || !mask) return;

    if (typeof show === "undefined" || show === null) {
        show = !sidebar.classList.contains("open");
    }

    if (show) {
        sidebar.classList.add("open");
        mask.classList.add("show");
        document.body.classList.add("fls-menu-open");
        if (btn) {
            btn.classList.add("menu-open");
            btn.setAttribute("aria-expanded", "true");
        }
        if (sidebar) {
            const firstLink = sidebar.querySelector("a");
            if(firstLink) firstLink.focus({preventScroll:true});
        }
        flsCheckUpdateNoticeCache();
    } else {
        sidebar.classList.remove("open");
        mask.classList.remove("show");
        document.body.classList.remove("fls-menu-open");
        if (btn) {
            btn.classList.remove("menu-open");
            btn.setAttribute("aria-expanded", "false");
        }
    }

}

document.addEventListener("keydown", function(e){
    if(e.key === "Escape") {
        if(flsCloseDisclosureModal()) {
            e.preventDefault();
            return;
        }
        const sidebar = document.getElementById("sidebar");
        const wasOpen = !!sidebar && sidebar.classList.contains("open");
        toggleMenu(false);
        if(wasOpen) {
            const btn = document.getElementById("flsFloatMenuBtn");
            if(btn) btn.focus({preventScroll:true});
        }
    }
});

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", flsSyncMenuViewport);
} else {
    flsSyncMenuViewport();
}

window.addEventListener("resize", flsSyncMenuViewport);
window.addEventListener("orientationchange", function(){
    setTimeout(flsSyncMenuViewport, 200);
});

/* 表格字段名补全 */
function flsEnhanceMobileTables(root){
    root = root || document;
    var tables = root.querySelectorAll(".table-wrap table:not(#tasksTable)");

    tables.forEach(function(table){
        var headers = [];

        table.querySelectorAll("thead th").forEach(function(th){
            headers.push((th.textContent || "").trim());
        });

        if (!headers.length) return;

        table.querySelectorAll("tbody tr").forEach(function(tr){
            var tds = tr.querySelectorAll("td");

            tds.forEach(function(td, index){
                if (td.hasAttribute("colspan")) {
                    td.setAttribute("data-label", "");
                    return;
                }

                var label = headers[index] || "";
                td.setAttribute("data-label", label);
            });
        });
    });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function(){
        flsEnhanceMobileTables(document);
    });
} else {
    flsEnhanceMobileTables(document);
}

/* ============================================================
   长表单悬浮提交按钮
   - 自动扫描 .content 内较长 POST 表单
   - 克隆 submit 按钮为悬浮按钮
   - 点击悬浮按钮时触发原按钮 click，兼容 formaction/name/value/AJAX
   - 打开侧边栏时自动隐藏
   - 原始按钮进入视口时自动隐藏，避免到底部遮挡原按钮
   ============================================================ */
window.__FLS_FLOAT_FORM_ORIGINAL_BUTTONS__ = [];

function flsIsElementVisible(el){
    if(!el) return false;

    const style = window.getComputedStyle(el);
    if(style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
        return false;
    }

    if(el.offsetParent === null && style.position !== "fixed") {
        return false;
    }

    return true;
}

function flsGetSubmitButtons(form){
    if(!form) return [];

    const buttons = Array.from(
        form.querySelectorAll('button[type="submit"], input[type="submit"]')
    );

    return buttons.filter(function(btn){
        if(btn.disabled) return false;
        if(!flsIsElementVisible(btn)) return false;

        const text = (btn.innerText || btn.value || "").trim();

        // 避免危险按钮悬浮，防误触
        if(/删除|停止|结束|退出|重启|清空|卸载/.test(text)) return false;

        return true;
    });
}

function flsFormLooksLong(form){
    if(!form) return false;

    const rect = form.getBoundingClientRect();
    const height = Math.max(form.offsetHeight || 0, rect.height || 0);
    const pageLong = document.documentElement.scrollHeight > window.innerHeight * 1.25;

    return pageLong && height > window.innerHeight * 0.7;
}

function flsPickFloatingForm(root){
    root = root || document;

    const forms = Array.from(root.querySelectorAll(".content form"));

    let best = null;
    let bestScore = 0;

    forms.forEach(function(form){
        const method = (form.getAttribute("method") || "GET").toUpperCase();

        // 搜索表单不需要悬浮
        if(method === "GET") return;

        if(form.dataset.noFloatActions === "1") return;
        // Code editors already occupy the user's working surface; a cloned
        // submit bar would cover the editor and is especially awkward on mobile.
        if(form.querySelector("textarea.fls-code-editor, .CodeMirror")) return;
        if(!flsIsElementVisible(form)) return;

        const buttons = flsGetSubmitButtons(form);
        if(!buttons.length) return;

        if(!flsFormLooksLong(form)) return;

        const score = form.offsetHeight || form.getBoundingClientRect().height || 0;

        if(score > bestScore){
            bestScore = score;
            best = form;
        }
    });

    return best;
}

function flsEnsureFloatActionBox(){
    let box = document.getElementById("flsFormFloatActions");

    if(!box){
        box = document.createElement("div");
        box.id = "flsFormFloatActions";
        box.className = "fls-form-float-actions";
        document.body.appendChild(box);
    }

    return box;
}

function flsShortFloatButtonText(text){
    text = String(text || "提交").trim();

    const map = [
        [/测试通知|发送测试/, "测试"],
        [/保存配置/, "保存"],
        [/保存文件/, "保存"],
        [/保存全部/, "保存"],
        [/保存新建/, "保存"],
        [/保存改名/, "保存"],
        [/保存代理/, "保存"],
        [/保存/, "保存"],

        [/开始下载安装/, "安装"],
        [/下载安装/, "安装"],

        [/立即导入所选任务/, "导入"],
        [/导入所选变量/, "导入"],
        [/开始导入/, "导入"],
        [/安装并查看日志/, "安装"],
        [/提交/, "提交"],
    ];

    for(const item of map){
        if(item[0].test(text)) return item[1];
    }

    if(text.length > 4){
        return text.slice(0, 4);
    }

    return text || "提交";
}

function flsFixedActionOverlapsFields(box){
    if(!box || getComputedStyle(box).display === "none") return false;
    const boxRect = box.getBoundingClientRect();
    return Array.from(document.querySelectorAll(
        "input,select,textarea,button[type=submit],.form-item:last-child,.pagination,p,li,pre,h1,h2,h3,.help,.page-description"
    )).some(function(el){
        if(box.contains(el)) return false;
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 &&
            rect.bottom > boxRect.top && rect.top < boxRect.bottom;
    });
}

function flsUpdateLogFloatVisibility(){
    const box = document.querySelector(".fls-log-float");
    if(!box || getComputedStyle(box).display === "none") return;

    const overlaps = flsFixedActionOverlapsFields(box);

    box.classList.toggle("fls-log-float-hidden", overlaps);
}

function flsInitFloatingFormActions(root){
    root = root || document;

    const box = flsEnsureFloatActionBox();

    document.body.classList.remove("fls-has-floating-actions");

    box.innerHTML = "";
    box.classList.remove("show");

    const form = flsPickFloatingForm(root);

    if(!form){
        return;
    }

    // 最多悬浮 2 个，避免太长
    const buttons = flsGetSubmitButtons(form).slice(0, 2);

    if(!buttons.length){
        return;
    }

    const title = document.createElement("span");
    title.className = "fls-form-float-title";
    title.textContent = "快捷";
    box.appendChild(title);

    buttons.forEach(function(originalBtn){
        const rawText = (originalBtn.innerText || originalBtn.value || "提交").trim();

        const clone = document.createElement("button");
        clone.type = "button";
        clone.className = originalBtn.className || "btn btn-primary";
        clone.textContent = flsShortFloatButtonText(rawText);
        clone.title = rawText;

        clone.addEventListener("click", function(){
            try {
                originalBtn.click();
            } catch(e) {
                try {
                    form.requestSubmit(originalBtn);
                } catch(err) {
                    form.submit();
                }
            }
        });

        box.appendChild(clone);
    });

    box.classList.add("show");
    document.body.classList.add("fls-has-floating-actions");
}

function flsRefreshFloatingFormActionsSoon(root){
    setTimeout(function(){
        flsInitFloatingFormActions(root || document);
    }, 80);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function(){
        flsInitFloatingFormActions(document);
    });
} else {
    flsInitFloatingFormActions(document);
}

window.addEventListener("resize", function(){
    flsRefreshFloatingFormActionsSoon(document);
});

window.addEventListener("orientationchange", function(){
    flsRefreshFloatingFormActionsSoon(document);
});

window.addEventListener("scroll", function(){
    flsUpdateLogFloatVisibility();
}, {passive:true});

window.addEventListener("resize", function(){
    flsUpdateLogFloatVisibility();
});

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", flsUpdateLogFloatVisibility);
} else {
    flsUpdateLogFloatVisibility();
}

/* ============================================================
   脚本编辑器语法高亮：CodeMirror
   ============================================================ */
window.__FLS_CODE_MIRRORS__ = window.__FLS_CODE_MIRRORS__ || [];

function flsCodeModeFromFilename(name){
    name = String(name || "").toLowerCase();

    if(name.endsWith(".py") || name.endsWith(".pyw")) return "python";
    if(name.endsWith(".sh") || name.endsWith(".bash")) return "shell";
    if(name.endsWith(".js") || name.endsWith(".mjs") || name.endsWith(".cjs")) return "javascript";
    if(name.endsWith(".ts") || name.endsWith(".mts") || name.endsWith(".cts")) return "text/typescript";
    if(name.endsWith(".json")) return {name:"javascript", json:true};
    if(name.endsWith(".yml") || name.endsWith(".yaml")) return "yaml";
    if(name.endsWith(".html") || name.endsWith(".htm")) return "htmlmixed";
    if(name.endsWith(".css")) return "css";
    if(name.endsWith(".php")) return "application/x-httpd-php";
    if(name.endsWith(".rb")) return "ruby";
    if(name.endsWith(".pl") || name.endsWith(".pm")) return "perl";
    if(name.endsWith(".lua")) return "lua";
    if(name.endsWith(".java")) return "text/x-java";
    if(name.endsWith(".md") || name.endsWith(".markdown")) return "markdown";

    return "text/plain";
}

function flsShouldUseCodeMirror(){
    /*
       手机端禁用 CodeMirror。

       原因：
       CodeMirror 5 在 Android / iOS / WebView 上容易出现：
       - 全选无效
       - 输入法组合输入异常
       - 删除时自动多删 / 少删一位
       - 光标位置错乱

       桌面端继续启用语法高亮。
    */
    try {
        var ua = navigator.userAgent || "";

        if (flsIsMobileViewport()) return false;
        if (/Android|iPhone|iPad|iPod|Mobile/i.test(ua)) return false;

        return true;
    } catch(e) {
        return false;
    }
}

window.__FLS_ASSET_PROMISES__ = window.__FLS_ASSET_PROMISES__ || {};

function flsLoadStyleOnce(href){
    href = String(href || "");
    if(!href) return Promise.resolve(false);
    const key = "style:" + href;
    if(window.__FLS_ASSET_PROMISES__[key]) return window.__FLS_ASSET_PROMISES__[key];
    window.__FLS_ASSET_PROMISES__[key] = new Promise(function(resolve){
        const link = document.createElement("link");
        let timer;
        const finish = function(ok){
            clearTimeout(timer);
            link.onload = link.onerror = null;
            if(!ok){
                link.remove();
                delete window.__FLS_ASSET_PROMISES__[key];
            }
            resolve(ok);
        };
        link.rel = "stylesheet";
        link.href = href;
        link.setAttribute("data-fls-href", href);
        link.onload = function(){ finish(true); };
        link.onerror = function(){ finish(false); };
        timer = setTimeout(function(){ finish(false); }, 8000);
        document.head.appendChild(link);
    });
    return window.__FLS_ASSET_PROMISES__[key];
}

function flsLoadScriptOnce(src){
    src = String(src || "");
    if(!src) return Promise.resolve();

    const key = "script:" + src;

    if(window.__FLS_ASSET_PROMISES__[key]){
        return window.__FLS_ASSET_PROMISES__[key];
    }

    window.__FLS_ASSET_PROMISES__[key] = new Promise(function(resolve, reject){
        const script = document.createElement("script");
        let timer;
        const finish = function(error){
            clearTimeout(timer);
            script.onload = script.onerror = null;
            if(error){
                script.remove();
                delete window.__FLS_ASSET_PROMISES__[key];
                reject(error);
            }else{
                script.dataset.loaded = "1";
                resolve();
            }
        };

        script.src = src;
        script.async = false;
        script.setAttribute("data-fls-src", src);

        script.onload = function(){ finish(); };
        script.onerror = function(){ finish(new Error("资源加载失败：" + src)); };
        timer = setTimeout(function(){ finish(new Error("资源加载超时：" + src)); }, 8000);
        document.head.appendChild(script);
    });

    return window.__FLS_ASSET_PROMISES__[key];
}

async function flsEnsureCodeMirrorLoaded(){
    const base = "/static/vendor/codemirror-5.65.16/";
    try {
        const styled = await flsLoadStyleOnce(base + "codemirror.css");
        if(!styled) return false;
        if(typeof CodeMirror === "undefined") await flsLoadScriptOnce(base + "codemirror.min.js");
        return typeof CodeMirror !== "undefined";
    } catch(e) {
        return false;
    }
}

async function flsInitCodeEditors(root){
    root = root || document;

    const textareas = Array.from(root.querySelectorAll("textarea.fls-code-editor"));

    if(!textareas.length){
        return;
    }

    /*
       手机端直接使用原生 textarea。
       注意：不要设置 cmInited，否则从手机切到桌面后无法初始化。
    */
    if(!flsShouldUseCodeMirror()){
        textareas.forEach(function(textarea){
            textarea.classList.add("fls-code-editor-native");
            textarea.style.minHeight = textarea.style.minHeight || "520px";
            textarea.style.fontFamily = "Consolas, Menlo, monospace";
            textarea.style.lineHeight = "1.55";
            textarea.style.whiteSpace = "pre";
            textarea.style.overflow = "auto";
            textarea.style.wordBreak = "normal";
            textarea.style.overflowWrap = "normal";
            textarea.setAttribute("autocomplete", "off");
            textarea.setAttribute("autocorrect", "off");
            textarea.setAttribute("autocapitalize", "off");
            textarea.setAttribute("spellcheck", "false");
        });

        return;
    }

    const loaded = await flsEnsureCodeMirrorLoaded();

    if(!loaded || typeof CodeMirror === "undefined") {
        return;
    }
    if(!flsShouldUseCodeMirror()) return;

    textareas.forEach(function(textarea){
        if(!document.body.contains(textarea)) return;
        if(textarea.dataset.cmInited === "1") return;

        var filename = textarea.getAttribute("data-filename") || "";
        var mode = flsCodeModeFromFilename(filename);

        var cm = CodeMirror.fromTextArea(textarea, {
            lineNumbers: true,
            screenReaderLabel: textarea.getAttribute("aria-label") || textarea.name || "代码编辑器",
            mode: mode,
            theme: "default",
            lineWrapping: true,
            indentUnit: 4,
            tabSize: 4,
            indentWithTabs: false,
            viewportMargin: 80,

            /*
               桌面端使用 textarea 输入模型，稳定性更好。
               手机端已经禁用 CodeMirror。
            */
            inputStyle: "textarea"
        });

        cm.setSize("100%", "680px");

        textarea.__flsCodeMirror = cm;
        textarea.dataset.cmInited = "1";
        window.__FLS_CODE_MIRRORS__.push(cm);

        setTimeout(function(){
            try {
                cm.refresh();
            } catch(e) {}
        }, 80);
    });
}

function flsSaveCodeMirrors(root){
    root = root || document;

    root.querySelectorAll("textarea.fls-code-editor").forEach(function(textarea){
        if(textarea.__flsCodeMirror){
            try {
                textarea.__flsCodeMirror.save();
            } catch(e) {}
        }
    });
}

function flsDisposeCodeEditors(root){
    root.querySelectorAll("textarea.fls-code-editor").forEach(function(textarea){
        const cm = textarea.__flsCodeMirror;
        if(!cm) return;
        cm.toTextArea();
        delete textarea.__flsCodeMirror;
        delete textarea.dataset.cmInited;
        window.__FLS_CODE_MIRRORS__ = window.__FLS_CODE_MIRRORS__.filter(function(item){ return item !== cm; });
    });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function(){
        flsInitCodeEditors(document);
    });
} else {
    flsInitCodeEditors(document);
}

/* AJAX 页面切换 */
(function(){
    if (window.__FLS_AJAX_LAYOUT__) return;
    window.__FLS_AJAX_LAYOUT__ = true;

    const FLS_PAGE_CACHE_TTL = 5 * 60 * 1000;
    const FLS_PAGE_CACHE_MAX = 40;
    const FLS_SESSION_CACHE_KEY = "fls-page-cache-v2";
    const FLS_SESSION_CACHE_MAX = 12;
    const FLS_SESSION_CACHE_TTL = 15 * 60 * 1000;

    window.__FLS_PAGE_CACHE__ = window.__FLS_PAGE_CACHE__ || new Map();
    window.__FLS_PAGE_PREFETCH__ = window.__FLS_PAGE_PREFETCH__ || new Map();
    window.__FLS_PAGE_CACHE_EPOCH__ = window.__FLS_PAGE_CACHE_EPOCH__ || 0;

    function sameOrigin(url){
        try {
            return new URL(url, location.href).origin === location.origin;
        } catch(e) {
            return false;
        }
    }

    function skipPath(path){
        // Authentication changes the session and its CSRF token.
        if(path === "/login" || path === "/logout") return true;
        if (path.indexOf("/scripts/download/") === 0) return true;

        // 备份下载必须走浏览器原生下载，不能 AJAX
        if (path.indexOf("/backup/download/") === 0) return true;

        // 旧导出入口也跳过 AJAX
        if (path === "/backup/export") return true;

        if (path.indexOf("/api/") === 0) return true;
        return false;
    }

    function flsPageCacheKey(url){
        const u = new URL(url, location.href);
        u.hash = "";
        return u.href;
    }

    function flsClearPageCache(prefix){
        const cache = window.__FLS_PAGE_CACHE__;
        if(!cache) return;

        // In-flight prefetches may finish after a mutation. Ignore their old
        // response so a write cannot repopulate a cache we just invalidated.
        window.__FLS_PAGE_CACHE_EPOCH__ += 1;

        if(!prefix){
            cache.clear();
        }else{
            for(const key of Array.from(cache.keys())){
                if(key.indexOf(prefix) >= 0){
                    cache.delete(key);
                }
            }
        }

        try {
            const stored = JSON.parse(sessionStorage.getItem(FLS_SESSION_CACHE_KEY) || "{}");
            Object.keys(stored).forEach(function(key){
                if(!prefix || key.indexOf(prefix) >= 0){
                    delete stored[key];
                }
            });
            sessionStorage.setItem(FLS_SESSION_CACHE_KEY, JSON.stringify(stored));
        } catch(e) {
            // sessionStorage may be disabled; memory cache still remains useful.
        }
    }

    function flsHtmlCacheAllowed(html){
        const text = String(html || "");
        return !/<form\b[^>]*\bmethod\s*=\s*["']?post\b/i.test(text) && !/<textarea\b/i.test(text);
    }

    function flsSessionCacheAllowed(url, html){
        try {
            const u = new URL(url, location.href);
            const path = u.pathname;

            if(path !== "/" && path !== "/history") return false;
            if(!flsHtmlCacheAllowed(html)) return false;
            return true;
        } catch(e) {
            return false;
        }
    }

    function flsReadSessionCache(key){
        try {
            const stored = JSON.parse(sessionStorage.getItem(FLS_SESSION_CACHE_KEY) || "{}");
            const item = stored[key];

            if(!item) return "";
            if(Date.now() - Number(item.time || 0) > FLS_SESSION_CACHE_TTL){
                delete stored[key];
                sessionStorage.setItem(FLS_SESSION_CACHE_KEY, JSON.stringify(stored));
                return "";
            }

            return item.html || "";
        } catch(e) {
            return "";
        }
    }

    function flsWriteSessionCache(key, html){
        try {
            const stored = JSON.parse(sessionStorage.getItem(FLS_SESSION_CACHE_KEY) || "{}");
            stored[key] = {time:Date.now(), html:String(html || "")};
            const keys = Object.keys(stored).sort(function(a,b){
                return Number(stored[a].time || 0) - Number(stored[b].time || 0);
            });

            while(keys.length > FLS_SESSION_CACHE_MAX){
                delete stored[keys.shift()];
            }

            sessionStorage.setItem(FLS_SESSION_CACHE_KEY, JSON.stringify(stored));
        } catch(e) {
            // Keep navigation working when browser storage is unavailable/full.
        }
    }

    window.flsClearPageCache = flsClearPageCache;

    function flsIsCacheablePage(url){
        try {
            const u = new URL(url, location.href);
            const path = u.pathname;

            if(!sameOrigin(u.href)) return false;

            // API / 下载类不缓存
            if(skipPath(path)) return false;

            // 登录 / 验证 / 退出 / 设置不缓存
            if(path === "/login") return false;
            if(path === "/logout") return false;
            if(path === "/setup") return false;
            if(path === "/verify") return false;
            if(path === "/verify/resend") return false;

            // 实时日志页不缓存
            if(path.indexOf("/log/") === 0) return false;
            if(path.indexOf("/online-scripts/log/") === 0) return false;
            if(path.indexOf("/deps/install-log/") === 0) return false;

            // 日志管理变化较频繁，不缓存
            if(path === "/logs") return false;

            // 带登录 token 的 URL 不缓存
            if(u.searchParams.has("token")) return false;

            // 消息提示页不缓存，避免旧提示重复出现
            if(u.searchParams.has("msg")) return false;
            if(u.searchParams.has("err")) return false;

            return true;
        } catch(e) {
            return false;
        }
    }

    function flsGetCachedPage(url){
        const cache = window.__FLS_PAGE_CACHE__;
        if(!cache) return "";

        const key = flsPageCacheKey(url);
        const item = cache.get(key);

        if(item){
            if(Date.now() - item.time > FLS_PAGE_CACHE_TTL){
                cache.delete(key);
                return "";
            }

            return item.html || "";
        }

        const sessionHtml = flsReadSessionCache(key);
        if(sessionHtml){
            cache.set(key, {time:Date.now(), html:sessionHtml});
        }
        return sessionHtml;
    }

    function flsPutCachedPage(url, html){
        if(!flsIsCacheablePage(url)) return;
        if(!flsHtmlCacheAllowed(html)) return;

        const cache = window.__FLS_PAGE_CACHE__;
        if(!cache) return;

        const key = flsPageCacheKey(url);

        cache.set(key, {
            time: Date.now(),
            html: String(html || "")
        });

        while(cache.size > FLS_PAGE_CACHE_MAX){
            const firstKey = cache.keys().next().value;
            cache.delete(firstKey);
        }

        if(flsSessionCacheAllowed(url, html)){
            flsWriteSessionCache(key, html);
        }
    }

    function flsPrefetchPage(url){
        if(!flsIsCacheablePage(url)) return;

        try {
            const path = new URL(url, location.href).pathname;
            if(["/", "/tasks", "/history"].indexOf(path) < 0) return;
        } catch(e) {
            return;
        }

        const key = flsPageCacheKey(url);
        if(flsGetCachedPage(url) || window.__FLS_PAGE_PREFETCH__.has(key)) return;

        const epoch = window.__FLS_PAGE_CACHE_EPOCH__;

        const request = fetch(url, {
            headers:{"X-Requested-With":"FLS-Ajax"},
            credentials:"same-origin",
            cache:"no-store"
        }).then(function(res){
            if(!res.ok) return "";
            return res.text().then(function(text){
                if(epoch === window.__FLS_PAGE_CACHE_EPOCH__){
                    flsPutCachedPage(res.url || url, text);
                }
                return text;
            });
        }).catch(function(){
            // Prefetch is optional; the next click performs a regular request.
        });

        window.__FLS_PAGE_PREFETCH__.set(key, request);
        request.finally(function(){
            window.__FLS_PAGE_PREFETCH__.delete(key);
        });
    }

    window.flsRefreshCurrentPage = function(){
        const key = flsPageCacheKey(location.href);
        flsClearPageCache(key);
        ajaxLoad(location.href, false, {force:true});
    };

    function flsSeedCurrentPageCache(){
        if(!flsIsCacheablePage(location.href)) return;
        flsPutCachedPage(location.href, document.documentElement.outerHTML);
    }

    function shouldAjaxLink(a){
        if (!a) return false;

        const href = a.getAttribute("href") || "";
        if (!href) return false;
        if (href.startsWith("#")) return false;
        if (href.startsWith("javascript:")) return false;
        if (a.target && a.target !== "_self") return false;
        if (a.hasAttribute("download")) return false;
        if (!sameOrigin(a.href)) return false;

        const u = new URL(a.href, location.href);
        if (skipPath(u.pathname)) return false;

        return true;
    }

    function confirmIfNeeded(a){
        const onclick = a.getAttribute("onclick") || "";

        if (onclick.indexOf("confirm") < 0) return true;

        let msg = "确定继续吗？";
        const m1 = onclick.match(/confirm\('([^']*)'\)/);
        const m2 = onclick.match(/confirm\("([^"]*)"\)/);

        if (m1 && m1[1]) msg = m1[1];
        if (m2 && m2[1]) msg = m2[1];

        return window.confirm(msg);
    }

    function linkMayChangeData(url){
        try {
            const u = new URL(url, location.href);
            const path = u.pathname;

            return (
                path.indexOf("/deps/uninstall") === 0 ||
                path.indexOf("/install/") === 0 ||
                path.indexOf("/backup/import") === 0 ||
                path.indexOf("/backup/restore") === 0 ||
                path.indexOf("/backup/delete") === 0 ||
                path.indexOf("/logout") === 0 ||
                path.indexOf("/verify/resend") === 0
            );
        } catch(e) {
            return false;
        }
    }

    function setLoading(on){
        let bar = document.getElementById("flsPageLoadingBar");

        if(!bar){
            bar = document.createElement("div");
            bar.id = "flsPageLoadingBar";
            bar.className = "fls-page-loading-bar";
            document.body.appendChild(bar);
        }

        if(on){
            bar.classList.remove("done");
            bar.classList.add("show");
        }else{
            bar.classList.remove("show");
            bar.classList.add("done");

            setTimeout(function(){
                bar.classList.remove("done");
                bar.style.width = "";
            }, 220);
        }
    }

    function runInlineScripts(container){
        const scripts = container.querySelectorAll("script");

        scripts.forEach(function(oldScript){
            const script = document.createElement("script");

            for (let i = 0; i < oldScript.attributes.length; i++) {
                const attr = oldScript.attributes[i];
                script.setAttribute(attr.name, attr.value);
            }

            script.textContent = oldScript.textContent;
            oldScript.parentNode.replaceChild(script, oldScript);
        });
    }

    async function replaceHtmlText(text, finalUrl, push){
        const doc = new DOMParser().parseFromString(text, "text/html");

        const newContent = doc.querySelector(".content");
        const newTitle = doc.querySelector(".title");
        const newNav = doc.querySelector(".nav");
        const newBody = doc.body;

        const oldContent = document.querySelector(".content");
        const oldTitle = document.querySelector(".title");
        const oldNav = document.querySelector(".nav");

        if (!newContent || !oldContent) {
            location.href = finalUrl || location.href;
            return false;
        }

        flsCloseDisclosureModal();

        if (window.__FLS_ACTIVE_LOG_INTERVAL__) {
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }

        if (window.__FLS_DASHBOARD_RUNTIME_INTERVAL__) {
            clearInterval(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__);
            window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = null;
        }

        flsDisposeCodeEditors(oldContent);
        oldContent.innerHTML = newContent.innerHTML;
        flsConvertFeedbackCards(oldContent);

        if (newTitle && oldTitle) oldTitle.innerHTML = newTitle.innerHTML;
        if (newNav && oldNav) oldNav.innerHTML = newNav.innerHTML;

        if (newBody) {
            document.body.className = newBody.className;
        }

        document.title = doc.title || document.title;

        runInlineScripts(oldContent);

        if (typeof flsEnhanceMobileTables === "function") {
            flsEnhanceMobileTables(oldContent);
        }

        if (typeof flsInitCodeEditors === "function") {
            flsInitCodeEditors(oldContent);
        }

        if (typeof flsInitFloatingFormActions === "function") {
            flsInitFloatingFormActions(oldContent);
        }

        if (push) {
            history.pushState({url: finalUrl || location.href}, "", finalUrl || location.href);
        }

        window.scrollTo(0, 0);
        toggleMenu(false);
        restoreTaskExecutions();
        return true;
    }

    async function replaceHtml(res, push){
        const text = await res.text();
        return await replaceHtmlText(text, res.url || location.href, push);
    }

    function restoreFormSubmission(form, submitter){
        if(form) delete form.dataset.submitting;

        const button = submitter || (form && form.querySelector('button[type="submit"], input[type="submit"]'));
        if(button) flsRestoreButton(button);
    }

    function taskExecutionOptions(taskId){
        const payload = flsBulkState("task:" + taskId).payload || {};
        return {
            scope:"task:" + taskId, block:document.querySelector("#tasksBlock") ? "#tasksBlock" : ".content",
            single:true, ids:() => [taskId], select:() => {},
            expectedCollection:payload.expected_collection,
            endpoint:data => "/api/task/action/" + data.action + "/" + encodeURIComponent(taskId),
            refresh:() => ajaxLoad(/^\/(run|stop|task\/collection\/clear)\//.test(location.pathname) ? "/tasks" : location.href, false, {force:true})
        };
    }

    function restoreTaskExecutions(){
        if(!document.querySelector(".content") || /^\/(login|logout)$/.test(location.pathname)) return;
        try {
            restoreCollectionSaves();
            Object.keys(sessionStorage).filter(key => key.startsWith(flsBulkStorageKey("collection-add:"))).forEach(key => {
                const id = key.slice(flsBulkStorageKey("collection-add:").length);
                flsRestoreTaskBulk(collectionAddOptions(id));
            });
            Object.keys(sessionStorage).filter(key => key.startsWith(flsBulkStorageKey("collection-delete:"))).forEach(key => {
                const id = key.slice(flsBulkStorageKey("collection-delete:").length);
                flsRestoreTaskBulk(collectionDeleteOptions(id));
            });
            Object.keys(sessionStorage).filter(key => key.startsWith(flsBulkStorageKey("task:"))).forEach(key => {
                const state = JSON.parse(sessionStorage.getItem(key));
                const action = (state.pending || state.payload || {}).action;
                if(!["run", "stop", "clear_collection"].includes(action)) return;
                const taskId = key.slice(flsBulkStorageKey("task:").length);
                flsRestoreTaskBulk(taskExecutionOptions(taskId));
            });
        } catch(e) {}
    }

    function collectionDeleteOptions(collectionId){
        return {
            scope:"collection-delete:" + collectionId, block:".content",
            ids:() => [collectionId], select:() => {},
            endpoint:() => "/api/collection/delete/" + encodeURIComponent(collectionId),
            refresh:() => ajaxLoad("/collections", false, {force:true})
        };
    }

    function restoreCollectionSaves(){
        Object.keys(sessionStorage).filter(key => ["collection-save:", "env-save:", "proxy-save:", "notify-save:"].some(prefix=>key.startsWith(flsBulkStorageKey(prefix)))).forEach(key => {
            const state = JSON.parse(sessionStorage.getItem(key));
            if(!state || !state.fields || !state.url) return;
            const scope = key.slice(flsBulkStorageKey("").length);
            let notice = Array.from(document.querySelectorAll("[data-collection-save-pending]")).find(el=>el.dataset.collectionSavePending===scope);
            if(!notice){
                notice = document.createElement("section");
                notice.className = "section";
                notice.dataset.collectionSavePending = scope;
                document.querySelector(".content").prepend(notice);
            }
            notice.replaceChildren();
            const message = document.createElement("p");
            message.textContent = scope.startsWith("notify-save:") ? "通知操作结果尚未确认，请先查询原提交。" : scope.startsWith("proxy-save:") ? "代理操作结果尚未确认，请先查询原提交。" : scope.startsWith("env-save:") ? "变量操作结果尚未确认，请先查询原提交。" : "合集保存结果尚未确认，请先查询原提交。";
            const form = document.createElement("form");
            form.method = "post";
            form.action = state.url;
            Object.entries({...state.fields, csrf_token:flsGetCsrfToken()}).forEach(([name,value])=>{
                (Array.isArray(value) ? value : [value]).forEach(value=>{
                    const input = document.createElement("input");
                    input.type = "hidden";
                    input.name = name;
                    input.value = value;
                    form.append(input);
                });
            });
            const button = document.createElement("button");
            button.type = "submit";
            button.className = "btn btn-primary";
            button.textContent = "查询本次保存结果";
            form.append(button);
            const login = document.createElement("a");
            login.className = "btn btn-gray";
            login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
            login.textContent = "重新登录";
            notice.append(message, form, login);
        });
    }

    function clearCollectionSave(scope){
        sessionStorage.removeItem(flsBulkStorageKey(scope));
        Array.from(document.querySelectorAll("[data-collection-save-pending]")).filter(el=>el.dataset.collectionSavePending===scope).forEach(el=>el.remove());
    }

    function collectionAddOptions(collectionId){
        const scope = "collection-add:" + collectionId;
        const state = flsBulkState(scope);
        const options = {
            scope:scope, block:".content", confirmRetry:true,
            selection:state.selection || (state.pending || {}).task_ids || [],
            expectedCollections:(state.pending || state.payload || {}).expected_collections,
            ids:() => options.selection, select:ids => {options.selection = ids;},
            endpoint:() => "/api/collection/add-task/" + encodeURIComponent(collectionId),
            refresh:() => ajaxLoad("/collections", false, {force:true})
        };
        return options;
    }

    function trackTaskExecution(url, data){
        if((/^\/env\/(new|view|import|edit\/[^/]+|delete\/[^/]+)$/.test(url.pathname) || /^\/proxy\/(new|(toggle|delete|edit)\/[^/]+)$/.test(url.pathname) || /^\/notify\/(new|default|(edit|toggle|delete|test|retry)\/[^/]+)$/.test(url.pathname)) && data.has("request_id")){
            const scope = (url.pathname.startsWith("/notify/") ? "notify-save:" : url.pathname.startsWith("/proxy/") ? "proxy-save:" : "env-save:") + url.pathname;
            const fields = Object.fromEntries(data);
            delete fields.csrf_token;
            if(data.has("items")) fields.items = data.getAll("items");
            if(url.pathname === "/notify/default") fields.default_ids = data.getAll("default_ids");
            const previous = flsBulkState(scope);
            if(previous.fields && Object.keys({...previous.fields,...fields}).some(name=>name!=="confirm_retry" && JSON.stringify(previous.fields[name])!==JSON.stringify(fields[name]))){
                restoreCollectionSaves();
                return false;
            }
            flsBulkSave(scope, {url:url.pathname, fields:fields});
            return {kind:"collection-save", scope:scope};
        }
        const save = url.pathname.match(/^\/collection\/(new|edit\/[^/]+)$/);
        if(save && data.has("request_id")){
            const scope = "collection-save:" + save[1];
            const fields = Object.fromEntries(data);
            delete fields.csrf_token;
            const previous = flsBulkState(scope);
            if(previous.fields && ["request_id", "name", "remark", "collection_version"].some(name=>previous.fields[name]!==fields[name])){
                restoreCollectionSaves();
                return false;
            }
            flsBulkSave(scope, {url:url.pathname, fields:fields});
            return {kind:"collection-save", scope:scope};
        }
        const deletion = url.pathname.match(/^\/collection\/delete\/([^/]+)$/);
        if(deletion && data.has("request_id")){
            const id = decodeURIComponent(deletion[1]);
            const options = collectionDeleteOptions(id);
            const state = flsBulkState(options.scope);
            if(state.pending && state.pending.request_id !== data.get("request_id")){
                flsBulkNotice(options, state, "上次请求尚未确认，请先查询本次操作结果");
                return false;
            }
            const pending = {action:"delete_collection", request_id:data.get("request_id")};
            if(data.has("expected_members")) pending.expected_members = JSON.parse(data.get("expected_members"));
            if(data.has("collection_version")) pending.collection_version = data.get("collection_version");
            flsBulkSave(options.scope, {selection:[id], pending:pending});
            return options;
        }
        const collection = url.pathname.match(/^\/collection\/add-task\/([^/]+)$/);
        if(collection && data.has("request_id")){
            const id = decodeURIComponent(collection[1]);
            const options = collectionAddOptions(id);
            const state = flsBulkState(options.scope);
            if(state.pending && state.pending.request_id !== data.get("request_id")){
                flsBulkNotice(options, state, "上次请求尚未确认，请先查询本次操作结果");
                return false;
            }
            const pending = {action:"assign_collection", task_ids:data.getAll("task_ids"), request_id:data.get("request_id")};
            if(data.has("expected_collections")) pending.expected_collections = JSON.parse(data.get("expected_collections"));
            flsBulkSave(options.scope, {selection:pending.task_ids, pending:pending});
            return options;
        }
        const match = url.pathname.match(/^\/(run|stop|task\/collection\/clear)\/([^/]+)$/);
        if(!match || !data.has("request_id")) return null;
        const taskId = decodeURIComponent(match[2]);
        const options = taskExecutionOptions(taskId);
        const state = flsBulkState(options.scope);
        if(state.pending && state.pending.request_id !== data.get("request_id")){
            flsBulkNotice(options, state, "上次请求尚未确认，请先查询本次操作结果");
            return false;
        }
        const pending = {action:match[1] === "task/collection/clear" ? "clear_collection" : match[1], task_ids:[taskId], request_id:data.get("request_id")};
        if(data.has("expected_collection")) pending.expected_collection = data.get("expected_collection");
        flsBulkSave(options.scope, {selection:[taskId], pending:pending});
        return options;
    }

    async function handleFormErrorResponse(res, fetchUrl, form, submitter, execution){
        const contentType = String(res.headers.get("content-type") || "").toLowerCase();
        let body = "";

        try {
            body = await res.text();
        } catch(e) {}

        if(contentType.indexOf("text/html") >= 0 && body.trim()){
            try {
                const errorDoc = new DOMParser().parseFromString(body, "text/html");
                if(execution && execution.kind === "collection-save"){
                    const saved = errorDoc.querySelector("[data-collection-save-result], [data-env-result], [data-proxy-result], [data-notify-result]");
                    if(saved && JSON.parse(saved.dataset.collectionSaveResult || saved.dataset.envResult || saved.dataset.proxyResult || saved.dataset.notifyResult).resolved) clearCollectionSave(execution.scope);
                }
                const result = errorDoc.querySelector("[data-task-execution-result]");
                if(execution && result){
                    const payload = JSON.parse(result.dataset.taskExecutionResult);
                    if(Array.isArray(payload.results)){
                        const state = flsBulkState(execution.scope);
                        flsBulkSave(execution.scope, {payload:payload, selection:payload.retry_ids || [],
                            retry_request:payload.resume_request ? state.pending || state.retry_request : undefined});
                    }
                }
                if(errorDoc.querySelector(".content") && document.querySelector(".content")){
                    await replaceHtmlText(body, res.url || fetchUrl, false);
                    return;
                }
            } catch(e) {}
        }

        let message = "";
        if(contentType.indexOf("application/json") >= 0){
            try {
                const payload = JSON.parse(body);
                message = payload.msg || payload.error || payload.message || "";
            } catch(e) {}
        }

        if(!message) message = body.trim().slice(0, 500);
        if(!message) message = "HTTP " + res.status;

        const nextStep = (res.status === 401 || res.status === 403)
            ? "重新登录后重试"
            : (res.status === 409 ? "刷新状态后重试" : "检查服务状态后重试");
        restoreFormSubmission(form, submitter);
        flsToast(flsActionFailure(message, nextStep), "error", 6000);
    }

    function flsRenderAjaxLoadError(url, error){
        const content = document.querySelector(".content");
        if(!content) return;

        let block = content.querySelector("[data-fls-ajax-load-error]");
        if(!block){
            block = document.createElement("section");
            block.className = "section fls-ajax-load-error";
            block.setAttribute("data-fls-ajax-load-error", "1");
            block.setAttribute("role", "alert");
            content.prepend(block);
        }
        block.replaceChildren();

        const status = Number(error && error.status || 0);
        const detail = String(error && error.message || "页面请求失败").slice(0, 600);
        const message = document.createElement("p");
        message.textContent = "页面加载失败" + (status ? "（HTTP " + status + "）" : "") + "：" + detail;
        block.appendChild(message);

        const actions = document.createElement("div");
        actions.className = "action-row";
        if(status === 401 || status === 403){
            const login = document.createElement("a");
            login.className = "btn btn-gray";
            login.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
            login.textContent = "重新登录";
            actions.appendChild(login);
        } else {
            const retry = document.createElement("button");
            retry.type = "button";
            retry.className = "btn btn-primary";
            retry.textContent = "重新加载页面";
            retry.addEventListener("click", function(){
                if(!flsMarkButtonBusy(retry)) return;
                ajaxLoad(url, true, {force:true}).finally(function(){
                    flsRestoreButton(retry);
                });
            });
            actions.appendChild(retry);
        }
        block.appendChild(actions);
    }

    async function ajaxLoad(url, push, options){
        options = options || {};
        const canCache = flsIsCacheablePage(url);

        if(canCache && !options.force){
            const pending = window.__FLS_PAGE_PREFETCH__.get(flsPageCacheKey(url));
            if(pending){
                await pending;
            }
        }

        /*
           缓存命中：
           - 不显示 loading
           - 不让页面变灰
           - 直接渲染缓存页面
        */
        if(canCache && !options.force){
            const cached = flsGetCachedPage(url);

            if(cached){
                try {
                    await replaceHtmlText(cached, url, push);
                    return;
                } catch(e) {
                    try {
                        window.__FLS_PAGE_CACHE__.delete(flsPageCacheKey(url));
                    } catch(err) {}
                }
            }
        }

        /*
           未命中缓存：
           - 走网络请求
           - 显示顶部加载条
        */
        setLoading(true);

        try {
            const res = await fetch(url, {
                headers: {"X-Requested-With":"FLS-Ajax"},
                credentials: "same-origin"
            });

            if (!res.ok) {
                let error;
                try {
                    await flsReadTextResponse(res);
                } catch(e) {
                    error = e;
                }
                setLoading(false);
                flsRenderAjaxLoadError(url, error || {status:res.status, message:"请求失败"});
                return;
            }

            const text = await res.text();

            if(canCache){
                flsPutCachedPage(res.url || url, text);
            }

            await replaceHtmlText(text, res.url || url, push);
            setLoading(false);
        } catch(e) {
            setLoading(false);
            flsRenderAjaxLoadError(url, e);
        }
    }

    document.addEventListener("click", function(e){
        const a = e.target.closest("a");

        if (!a) return;
        if (!a.closest(".content") && !a.closest(".nav")) return;
        if (!shouldAjaxLink(a)) return;

        e.preventDefault();
        e.stopImmediatePropagation();

        if (!confirmIfNeeded(a)) return;

        if(linkMayChangeData(a.href)){
            flsClearPageCache();
        }

        ajaxLoad(a.href, true);
    }, true);

    document.addEventListener("mouseover", function(e){
        const a = e.target.closest(".nav a");
        if(a && shouldAjaxLink(a)) flsPrefetchPage(a.href);
    }, {passive:true});

    document.addEventListener("focusin", function(e){
        const a = e.target.closest(".nav a");
        if(a && shouldAjaxLink(a)) flsPrefetchPage(a.href);
    });

    document.addEventListener("submit", async function(e){
        const form = e.target;

        if (!form || !form.closest(".content, .fls-disclosure-modal")) return;

        const method = (form.getAttribute("method") || "GET").toUpperCase();
        const submitter = e.submitter || null;
        const action = (
            submitter && submitter.getAttribute("formaction")
        ) || form.getAttribute("action") || location.href;

        const url = new URL(action, location.href);

        if (!sameOrigin(url.href)) return;
        if(url.pathname === "/login" || url.pathname === "/logout") return;

        e.preventDefault();
        e.stopImmediatePropagation();

        if(method !== "GET"){
            flsClearPageCache();
        }

        setLoading(true);

        let execution = null;
        try {
            let fetchUrl = url.href;

            const opts = {
                method: method,
                headers: {"X-Requested-With":"FLS-Ajax"},
                credentials: "same-origin"
            };

            if (typeof flsSaveCodeMirrors === "function") {
                flsSaveCodeMirrors(form);
            }

            if (method === "GET") {
                const fd = new FormData(form);
                fd.forEach(function(v,k){
                    url.searchParams.set(k,v);
                });
                fetchUrl = url.href;
            } else {
                const fd = new FormData(form);

                if(url.pathname === "/backup/import" && typeof flsRestorePending === "function"){
                    const previous=flsRestorePending();
                    if(previous && !previous.continue_upload){
                        flsRestoreRecovery("原恢复请求结果未确认，请先查询原请求。");
                        restoreFormSubmission(form,submitter);setLoading(false);return;
                    }
                    if(previous && (JSON.stringify(fd.getAll("restore_items"))!==JSON.stringify(previous.restore_items)
                        || (fd.get("restore_deps")==="1") !== previous.restore_deps)){
                        flsRestoreRecovery("原恢复范围已变化，请查询原请求或明确开始新请求。");
                        restoreFormSubmission(form,submitter);setLoading(false);return;
                    }
                    if(previous) fd.set("restore_request_id",previous.restore_request_id);
                    sessionStorage.setItem("fls-backup-restore",JSON.stringify({restore_request_id:fd.get("restore_request_id"),
                        restore_items:fd.getAll("restore_items"),restore_deps:fd.get("restore_deps")==="1"}));
                }

                if (submitter && submitter.name && !fd.has(submitter.name)) {
                    fd.append(submitter.name, submitter.value || "");
                }

                opts.body = fd;
                execution = trackTaskExecution(url, fd);
                if(execution === false){
                    restoreFormSubmission(form, submitter);
                    setLoading(false);
                    return;
                }
            }

            if(form.closest(".fls-disclosure-modal")) flsCloseDisclosureModal();
            const res = await fetch(fetchUrl, opts);

            if (!res.ok) {
                await handleFormErrorResponse(res, fetchUrl, form, submitter, execution);
                if(execution){
                    if(execution.kind === "collection-save"){
                        restoreCollectionSaves();
                        setLoading(false);
                        return;
                    }
                    const options = execution.scope.startsWith("collection-add:")
                        ? collectionAddOptions(execution.scope.slice("collection-add:".length))
                        : execution.scope.startsWith("collection-delete:")
                        ? collectionDeleteOptions(execution.scope.slice("collection-delete:".length))
                        : taskExecutionOptions(execution.ids()[0]);
                    flsRestoreTaskBulk(options);
                }
                setLoading(false);
                return;
            }

            const replaced = await replaceHtml(res, true);
            if(execution && replaced && new URL(res.url || fetchUrl).pathname !== "/login"){
                if(execution.kind === "collection-save") clearCollectionSave(execution.scope);
                sessionStorage.removeItem(flsBulkStorageKey(execution.scope));
                Array.from(document.querySelectorAll("[data-bulk-recovery]")).filter(el => el.dataset.bulkRecovery === execution.scope).forEach(el => el.remove());
            }
            setLoading(false);
        } catch(err) {
            setLoading(false);
            restoreFormSubmission(form, submitter);
            if(execution){
                if(execution.kind === "collection-save") restoreCollectionSaves();
                else flsRestoreTaskBulk(execution);
            }
            flsToast(flsActionFailure("请求失败：" + String(err), "检查网络或登录状态后重试"), "error", 6000);
        }
    }, true);

    window.addEventListener("popstate", function(){
        ajaxLoad(location.href, false);
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function(){
            flsSeedCurrentPageCache();
            restoreTaskExecutions();
        });
    } else {
        flsSeedCurrentPageCache();
        restoreTaskExecutions();
    }
})();

/* ============================================================
   日志增强：ANSI 颜色 / Base64 图片预览
   ============================================================ */
function flsEscapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(c){
        return {
            "&":"&amp;",
            "<":"&lt;",
            ">":"&gt;",
            '"':"&quot;",
            "'":"&#39;"
        }[c];
    });
}

function flsEscapeAttr(s){
    return flsEscapeHtml(s).replace(/`/g, "&#96;");
}

function flsAnsiClassFromCodes(codes){
    var classes = [];
    var fg = "";
    var bold = false;
    var dim = false;
    var underline = false;

    codes.forEach(function(code){
        code = String(code || "").trim();

        if(code === "" || code === "0"){
            fg = "";
            bold = false;
            dim = false;
            underline = false;
            return;
        }

        if(code === "1"){
            bold = true;
            return;
        }

        if(code === "2"){
            dim = true;
            return;
        }

        if(code === "4"){
            underline = true;
            return;
        }

        if(code === "22"){
            bold = false;
            dim = false;
            return;
        }

        if(code === "24"){
            underline = false;
            return;
        }

        if(code === "39"){
            fg = "";
            return;
        }

        if(
            ["30","31","32","33","34","35","36","37",
             "90","91","92","93","94","95","96","97"].indexOf(code) >= 0
        ){
            fg = "ansi-fg-" + code;
            return;
        }
    });

    if(fg) classes.push(fg);
    if(bold) classes.push("ansi-bold");
    if(dim) classes.push("ansi-dim");
    if(underline) classes.push("ansi-underline");

    return classes.join(" ");
}

function flsRenderAnsiToHtml(text){
    text = String(text || "");
    var regex = /(?:\x1b\[|\[)([0-9;]*)m/g;

    var html = "";
    var last = 0;
    var match;
    var currentCodes = [];
    var currentClass = "";

    function appendText(part){
        if(!part) return;

        var escaped = flsEscapeHtml(part);

        if(currentClass){
            html += '<span class="' + currentClass + '">' + escaped + '</span>';
        }else{
            html += escaped;
        }
    }

    while((match = regex.exec(text)) !== null){
        appendText(text.slice(last, match.index));

        var rawCodes = match[1] || "0";
        var codes = rawCodes.split(";");

        if(codes.indexOf("0") >= 0 || rawCodes === ""){
            currentCodes = [];
            currentClass = "";
        }else{
            currentCodes = currentCodes.concat(codes);
            currentClass = flsAnsiClassFromCodes(currentCodes);
        }

        last = regex.lastIndex;
    }

    appendText(text.slice(last));

    return html;
}

function flsLooksLikeBase64Image(raw){
    raw = String(raw || "");
    return /^data:image\/(png|jpg|jpeg|gif|webp|bmp|svg\+xml);base64,[A-Za-z0-9+/=\s\r\n]+$/i.test(raw);
}

function flsNormalizeBase64Image(raw){
    raw = String(raw || "");

    var m = raw.match(/^(data:image\/(?:png|jpg|jpeg|gif|webp|bmp|svg\+xml);base64,)([\s\S]+)$/i);
    if(!m) return raw;

    var prefix = m[1];
    var body = m[2].replace(/\s+/g, "");

    return prefix + body;
}

function flsRenderBase64Images(html){
    var regex = /(data:image\/(?:png|jpg|jpeg|gif|webp|bmp|svg\+xml);base64,[A-Za-z0-9+/=\s\r\n]{80,})/gi;

    return html.replace(regex, function(raw){
        var normalized = flsNormalizeBase64Image(raw);

        if(!flsLooksLikeBase64Image(normalized)){
            return raw;
        }

        var safeSrc = flsEscapeAttr(normalized);
        var safeRaw = flsEscapeAttr(normalized);

        return (
            '<span class="fls-log-image-wrap" data-raw="' + safeRaw + '">' +
                '<img class="fls-log-image" src="' + safeSrc + '" title="点击变回 Base64 原文">' +
                '<div class="fls-log-image-tip">Base64 图片已自动预览，点击图片可变回原文</div>' +
            '</span>'
        );
    });
}

function flsRenderLogText(el, text){
    if(!el) return;

    text = String(text || "");

    var html = flsRenderAnsiToHtml(text);
    html = flsRenderBase64Images(html);

    el.innerHTML = html;
    el.dataset.rawLogText = text;
}

document.addEventListener("click", function(e){
    var img = e.target.closest(".fls-log-image");
    if(img){
        var wrap = img.closest(".fls-log-image-wrap");
        if(!wrap) return;

        var raw = wrap.getAttribute("data-raw") || "";
        var code = document.createElement("code");
        code.className = "fls-log-base64-raw";
        code.setAttribute("data-raw", raw);
        code.setAttribute("title", "点击重新显示图片");
        code.textContent = raw;

        wrap.replaceWith(code);
        return;
    }

    var rawEl = e.target.closest(".fls-log-base64-raw");
    if(rawEl){
        var rawText = rawEl.getAttribute("data-raw") || rawEl.textContent || "";
        var src = flsNormalizeBase64Image(rawText);

        var wrap2 = document.createElement("span");
        wrap2.className = "fls-log-image-wrap";
        wrap2.setAttribute("data-raw", src);

        var image = document.createElement("img");
        image.className = "fls-log-image";
        image.src = src;
        image.title = "点击变回 Base64 原文";

        var tip = document.createElement("div");
        tip.className = "fls-log-image-tip";
        tip.textContent = "Base64 图片已自动预览，点击图片可变回原文";

        wrap2.appendChild(image);
        wrap2.appendChild(tip);

        rawEl.replaceWith(wrap2);
    }
}, true);

function flsLogCopyAll(){
    const el = document.getElementById("log");

    if (!el) {
        alert("未找到日志内容");
        return;
    }

    const text = el.dataset.rawLogText || el.textContent || "";

    if (!text) {
        alert("暂无日志可复制");
        return;
    }

    function fallback(){
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();

        try {
            document.execCommand("copy");
            alert("已复制全部日志");
        } catch(e) {
            alert("复制失败，请手动复制");
        }

        document.body.removeChild(ta);
    }

    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function(){
            alert("已复制全部日志");
        }).catch(fallback);
    } else {
        fallback();
    }
}

function flsLogGoTop(){
    window.scrollTo({top:0,behavior:"smooth"});
}

function flsLogGoBottom(){
    const tip = document.getElementById("flsLogNewTip");

    if (tip) tip.style.display = "none";

    window.__FLS_LOG_NEAR_BOTTOM__ = true;
    window.scrollTo({top:document.documentElement.scrollHeight,behavior:"smooth"});
}
