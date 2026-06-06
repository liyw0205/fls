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

if(!window.__FLS_ALERT_PATCHED__){
    window.__FLS_ORIGINAL_ALERT__ = window.alert;
    window.alert = function(message){
        flsToast(message, "info");
    };
    window.__FLS_ALERT_PATCHED__ = true;
}

function detectFlsMobile(){
    var w = window.innerWidth || document.documentElement.clientWidth || 0;
    var sw = window.screen ? window.screen.width : 0;
    var ua = navigator.userAgent || "";

    if (/Android|iPhone|iPad|Mobile/i.test(ua)) return true;
    if (sw && sw <= 900) return true;
    if (w && w <= 900) return true;

    return false;
}

function applyFlsMobileClass(){
    if (detectFlsMobile()) {
        document.body.classList.add("fls-mobile");
    } else {
        document.body.classList.remove("fls-mobile");
    }

    if (!document.body.classList.contains("fls-mobile")) {
        var sidebar = document.getElementById("sidebar");
        var mask = document.getElementById("mask");
        var btn = document.getElementById("flsFloatMenuBtn");

        if (sidebar) sidebar.classList.remove("open");
        if (mask) mask.classList.remove("show");
        if (btn) btn.classList.remove("menu-open");
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
        if (btn) btn.classList.add("menu-open");
    } else {
        sidebar.classList.remove("open");
        mask.classList.remove("show");
        if (btn) btn.classList.remove("menu-open");
    }

    if (typeof flsUpdateFloatingFormVisibility === "function") {
        flsUpdateFloatingFormVisibility();
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", applyFlsMobileClass);
} else {
    applyFlsMobileClass();
}

window.addEventListener("resize", applyFlsMobileClass);
window.addEventListener("orientationchange", function(){
    setTimeout(applyFlsMobileClass, 200);
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
        [/保存配置/, "保存"],
        [/保存文件/, "保存"],
        [/保存全部/, "保存"],
        [/保存新建/, "保存"],
        [/保存改名/, "保存"],
        [/保存代理/, "保存"],
        [/保存.*通知/, "保存"],
        [/保存/, "保存"],

        [/开始下载安装/, "安装"],
        [/下载安装/, "安装"],

        [/立即导入所选任务/, "导入"],
        [/导入所选变量/, "导入"],
        [/开始导入/, "导入"],

        [/开始拉取/, "拉取"],
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

function flsOriginalButtonsInViewport(){
    const buttons = window.__FLS_FLOAT_FORM_ORIGINAL_BUTTONS__ || [];

    for(const btn of buttons){
        if(!btn || !document.body.contains(btn)) continue;
        if(!flsIsElementVisible(btn)) continue;

        const rect = btn.getBoundingClientRect();

        // 原按钮只要进入视口附近，就隐藏悬浮按钮
        if(
            rect.top < window.innerHeight - 20 &&
            rect.bottom > 0
        ){
            return true;
        }
    }

    return false;
}

function flsSidebarIsOpen(){
    const sidebar = document.getElementById("sidebar");
    const mask = document.getElementById("mask");

    return (
        sidebar && sidebar.classList.contains("open")
    ) || (
        mask && mask.classList.contains("show")
    );
}

function flsUpdateFloatingFormVisibility(){
    const box = document.getElementById("flsFormFloatActions");
    if(!box) return;

    if(!box.classList.contains("show")) return;

    if(flsSidebarIsOpen() || flsOriginalButtonsInViewport()){
        box.classList.add("hide-near-original");
    }else{
        box.classList.remove("hide-near-original");
    }
}

function flsInitFloatingFormActions(root){
    root = root || document;

    const box = flsEnsureFloatActionBox();

    box.innerHTML = "";
    box.classList.remove("show");
    box.classList.remove("hide-near-original");

    window.__FLS_FLOAT_FORM_ORIGINAL_BUTTONS__ = [];

    const form = flsPickFloatingForm(root);

    if(!form){
        return;
    }

    // 最多悬浮 2 个，避免太长
    const buttons = flsGetSubmitButtons(form).slice(0, 2);

    if(!buttons.length){
        return;
    }

    window.__FLS_FLOAT_FORM_ORIGINAL_BUTTONS__ = buttons;

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
    flsUpdateFloatingFormVisibility();
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
    flsUpdateFloatingFormVisibility();
}, {passive:true});

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
        if (document.body && document.body.classList.contains("fls-mobile")) {
            return false;
        }

        var w = window.innerWidth || document.documentElement.clientWidth || 0;
        var ua = navigator.userAgent || "";

        if (w && w <= 900) return false;
        if (/Android|iPhone|iPad|iPod|Mobile/i.test(ua)) return false;

        return true;
    } catch(e) {
        return false;
    }
}

window.__FLS_ASSET_PROMISES__ = window.__FLS_ASSET_PROMISES__ || {};

function flsLoadStyleOnce(href){
    href = String(href || "");
    if(!href) return;

    if(document.querySelector('link[data-fls-href="' + href + '"]')) {
        return;
    }

    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.setAttribute("data-fls-href", href);
    document.head.appendChild(link);
}

function flsLoadScriptOnce(src){
    src = String(src || "");
    if(!src) return Promise.resolve();

    const key = "script:" + src;

    if(window.__FLS_ASSET_PROMISES__[key]){
        return window.__FLS_ASSET_PROMISES__[key];
    }

    window.__FLS_ASSET_PROMISES__[key] = new Promise(function(resolve, reject){
        const existing = document.querySelector('script[data-fls-src="' + src + '"]');

        if(existing && existing.dataset.loaded === "1"){
            resolve();
            return;
        }

        const script = existing || document.createElement("script");

        script.src = src;
        script.async = false;
        script.setAttribute("data-fls-src", src);

        script.addEventListener("load", function(){
            script.dataset.loaded = "1";
            resolve();
        }, {once:true});

        script.addEventListener("error", function(){
            delete window.__FLS_ASSET_PROMISES__[key];
            reject(new Error("资源加载失败：" + src));
        }, {once:true});

        if(!existing){
            document.head.appendChild(script);
        }
    });

    return window.__FLS_ASSET_PROMISES__[key];
}

async function flsEnsureCodeMirrorLoaded(){
    if(typeof CodeMirror !== "undefined") {
        return true;
    }

    const base = "https://cdn.jsdelivr.net/npm/codemirror@5.65.16/";

    flsLoadStyleOnce(base + "lib/codemirror.min.css");
    flsLoadStyleOnce(base + "theme/material-darker.min.css");

    try {
        await flsLoadScriptOnce(base + "lib/codemirror.min.js");

        const modes = [
            "mode/python/python.min.js",
            "mode/shell/shell.min.js",
            "mode/javascript/javascript.min.js",
            "mode/yaml/yaml.min.js",
            "mode/xml/xml.min.js",
            "mode/css/css.min.js",
            "mode/htmlmixed/htmlmixed.min.js",
            "mode/php/php.min.js",
            "mode/ruby/ruby.min.js",
            "mode/perl/perl.min.js",
            "mode/lua/lua.min.js",
            "mode/clike/clike.min.js",
            "mode/markdown/markdown.min.js"
        ];

        for(const mode of modes){
            await flsLoadScriptOnce(base + mode);
        }

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

    textareas.forEach(function(textarea){
        if(!document.body.contains(textarea)) return;
        if(textarea.dataset.cmInited === "1") return;

        textarea.dataset.cmInited = "1";

        var filename = textarea.getAttribute("data-filename") || "";
        var mode = flsCodeModeFromFilename(filename);

        var cm = CodeMirror.fromTextArea(textarea, {
            lineNumbers: true,
            mode: mode,
            theme: "material-darker",
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

    window.__FLS_PAGE_CACHE__ = window.__FLS_PAGE_CACHE__ || new Map();

    function sameOrigin(url){
        try {
            return new URL(url, location.href).origin === location.origin;
        } catch(e) {
            return false;
        }
    }

    function skipPath(path){
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

        if(!prefix){
            cache.clear();
            return;
        }

        for(const key of Array.from(cache.keys())){
            if(key.indexOf(prefix) >= 0){
                cache.delete(key);
            }
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

        if(!item) return "";

        if(Date.now() - item.time > FLS_PAGE_CACHE_TTL){
            cache.delete(key);
            return "";
        }

        return item.html || "";
    }

    function flsPutCachedPage(url, html){
        if(!flsIsCacheablePage(url)) return;

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
            return;
        }

        if (window.__FLS_ACTIVE_LOG_INTERVAL__) {
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }

        if (window.__FLS_DASHBOARD_RUNTIME_INTERVAL__) {
            clearInterval(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__);
            window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = null;
        }

        oldContent.innerHTML = newContent.innerHTML;

        if (newTitle && oldTitle) oldTitle.innerHTML = newTitle.innerHTML;
        if (newNav && oldNav) oldNav.innerHTML = newNav.innerHTML;

        if (newBody) {
            document.body.className = newBody.className;
            applyFlsMobileClass();
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
    }

    async function replaceHtml(res, push){
        const text = await res.text();
        await replaceHtmlText(text, res.url || location.href, push);
    }

    async function ajaxLoad(url, push){
        const canCache = flsIsCacheablePage(url);

        /*
           缓存命中：
           - 不显示 loading
           - 不让页面变灰
           - 直接渲染缓存页面
        */
        if(canCache){
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
                setLoading(false);
                location.href = url;
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
            location.href = url;
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

    document.addEventListener("submit", async function(e){
        const form = e.target;

        if (!form || !form.closest(".content")) return;

        const method = (form.getAttribute("method") || "GET").toUpperCase();
        const submitter = e.submitter || null;
        const action = (
            submitter && submitter.getAttribute("formaction")
        ) || form.getAttribute("action") || location.href;

        const url = new URL(action, location.href);

        if (!sameOrigin(url.href)) return;

        e.preventDefault();
        e.stopImmediatePropagation();

        if(method !== "GET"){
            flsClearPageCache();
        }

        setLoading(true);

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

                if (submitter && submitter.name && !fd.has(submitter.name)) {
                    fd.append(submitter.name, submitter.value || "");
                }

                opts.body = fd;
            }

            const res = await fetch(fetchUrl, opts);

            if (!res.ok) {
                location.href = fetchUrl;
                return;
            }

            await replaceHtml(res, true);
            setLoading(false);
        } catch(err) {
            form.submit();
        }
    }, true);

    window.addEventListener("popstate", function(){
        ajaxLoad(location.href, false);
    });
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
