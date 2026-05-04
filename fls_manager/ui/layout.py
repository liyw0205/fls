from ..utils import h


def layout(title, active, body):
    nav = [
        ("dashboard", "/", "📊 仪表盘"),
        ("tasks", "/tasks", "📜 任务管理"),
        ("env", "/env", "🌐 全局变量"),
        ("proxy", "/proxy", "🧩 代理管理"),
        ("pull", "/pull", "📂 脚本管理"),
        ("backup", "/backup", "💾 备份恢复"),
        ("deps", "/deps", "📦 依赖管理"),
        ("logs", "/logs", "📁 日志管理"),
        ("notify", "/notify", "🔔 通知管理"),
        ("status", "/panel/status", "🖥️ 运行环境"),
        ("config", "/config", "🔧 配置"),
        ("about", "/about", "⚙️ 关于"),
    ]

    nav_html = ""
    for key, url, text in nav:
        cls = "active" if active == key else ""
        nav_html += '<a class="{}" href="{}">{}</a>'.format(cls, h(url), h(text))

    nav_html += '<a href="/logout">🚪 退出登录</a>'

    html = r'''
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">

<style>
:root {
    --bg:#f4f6fb;
    --card:#fff;
    --text:#1f2937;
    --muted:#6b7280;
    --border:#e5e7eb;
    --primary:#18a058;
    --blue:#2563eb;
    --red:#dc2626;
    --orange:#f59e0b;
    --sidebar:#111827;
    --side-text:#d1d5db;
}

* {
    box-sizing:border-box;
}

html, body {
    margin:0;
    padding:0;
    width:100%;
    min-height:100%;
    background:var(--bg);
    color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Microsoft YaHei",sans-serif;
    overflow-x:hidden;
}

a {
    text-decoration:none;
    color:#2563eb;
}

.app {
    display:flex;
    min-height:100vh;
    width:100%;
}

/* ============================================================
   桌面端：侧边栏一直显示
   ============================================================ */
.sidebar {
    position:fixed;
    left:0;
    top:0;
    bottom:0;
    width:240px;
    background:var(--sidebar);
    color:var(--side-text);
    z-index:30;
    transition:.25s;
    overflow-y:auto;
    transform:translateX(0);
}

.main {
    margin-left:240px;
    width:calc(100% - 240px);
    min-height:100vh;
}

.brand {
    height:64px;
    display:flex;
    align-items:center;
    padding:0 20px;
    color:#fff;
    font-size:20px;
    font-weight:800;
    border-bottom:1px solid rgba(255,255,255,.08);
}

.brand span {
    width:10px;
    height:10px;
    border-radius:50%;
    background:var(--primary);
    margin-right:10px;
}

.nav {
    padding:14px 10px;
}

.nav a {
    display:block;
    padding:12px 14px;
    color:var(--side-text);
    border-radius:10px;
    margin-bottom:6px;
    font-size:15px;
}

.nav a.active {
    background:var(--primary);
    color:#fff;
}

.nav a:hover {
    background:rgba(255,255,255,.08);
}

.topbar {
    height:64px;
    background:var(--card);
    border-bottom:1px solid var(--border);
    display:flex;
    align-items:center;
    padding:0 22px;
    position:sticky;
    top:0;
    z-index:10;
}

.menu-btn {
    display:none;
    border:0;
    background:transparent;
    font-size:24px;
    margin-right:12px;
    cursor:pointer;
}

.title {
    font-weight:800;
    font-size:18px;
}

.content {
    padding:22px;
    width:100%;
}

.card {
    background:var(--card);
    border-radius:14px;
    padding:18px;
    margin-bottom:18px;
    box-shadow:0 4px 16px rgba(0,0,0,.04);
    width:100%;
}

.card-title {
    font-size:17px;
    font-weight:800;
    margin-bottom:14px;
}

.grid {
    display:grid;
    grid-template-columns:repeat(5,minmax(0,1fr));
    gap:16px;
    margin-bottom:18px;
}

.stat {
    background:var(--card);
    border-radius:14px;
    padding:18px;
    box-shadow:0 4px 16px rgba(0,0,0,.04);
    min-width:0;
}

.stat .label {
    color:var(--muted);
    font-size:14px;
}

.stat .num {
    margin-top:8px;
    font-size:28px;
    font-weight:900;
}

.table-wrap {
    width:100%;
    overflow-x:auto;
    -webkit-overflow-scrolling:touch;
}

table {
    width:100%;
    border-collapse:collapse;
    min-width:980px;
}

th, td {
    border-bottom:1px solid var(--border);
    padding:12px 10px;
    text-align:left;
    font-size:14px;
    vertical-align:middle;
    word-break:break-word;
}

th {
    color:var(--muted);
    background:#fafafa;
}

.badge {
    display:inline-block;
    padding:4px 9px;
    border-radius:999px;
    font-size:12px;
    font-weight:700;
}

.green {
    background:#dcfce7;
    color:#166534;
}

.red {
    background:#fee2e2;
    color:#991b1b;
}

.gray {
    background:#f3f4f6;
    color:#4b5563;
}

.blue {
    background:#dbeafe;
    color:#1d4ed8;
}

.orange {
    background:#fff7ed;
    color:#9a3412;
}

.btn {
    display:inline-flex;
    align-items:center;
    justify-content:center;
    min-height:34px;
    padding:7px 11px;
    border-radius:8px;
    border:0;
    color:#fff!important;
    cursor:pointer;
    font-size:13px;
    margin:2px;
    white-space:nowrap;
}

.btn-primary {
    background:var(--primary);
}

.btn-blue {
    background:var(--blue);
}

.btn-red {
    background:var(--red);
}

.btn-orange {
    background:var(--orange);
}

.btn-gray {
    background:#6b7280;
}

input, textarea, select {
    width:100%;
    border:1px solid var(--border);
    border-radius:10px;
    padding:10px 12px;
    font-size:14px;
    outline:none;
    background:#fff;
}

textarea {
    min-height:220px;
    font-family:Consolas,Menlo,monospace;
    resize:vertical;
}

.form-grid {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:14px;
}

.form-item label {
    display:block;
    font-size:13px;
    color:var(--muted);
    margin-bottom:6px;
}

.help {
    color:var(--muted);
    font-size:13px;
    line-height:1.7;
}

.code {
    background:#f3f4f6;
    border-radius:10px;
    padding:12px;
    font-family:Consolas,Menlo,monospace;
    overflow-x:auto;
    line-height:1.7;
}

pre.log {
    background:#0b1020;
    color:#d1d5db;
    border-radius:12px;
    padding:16px;
    min-height:560px;
    white-space:pre-wrap;
    word-break:break-word;
    font-family:Consolas,Menlo,monospace;
    font-size:13px;
    line-height:1.55;
    overflow:auto;
}

.action-row {
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    align-items:center;
}

.mask {
    display:none;
}

/* ============================================================
   手机端悬浮菜单按钮：已调小，避免超出
   ============================================================ */
.fls-float-menu-btn {
    display:none;
}

body.fls-mobile .fls-float-menu-btn {
    display:flex!important;
    position:fixed;
    left:8px;
    top:calc(env(safe-area-inset-top, 0px) + 8px);
    width:38px;
    height:38px;
    border:1px solid rgba(255,255,255,.45);
    border-radius:999px;
    background:rgba(255,255,255,.58);
    color:#111827;
    font-size:18px;
    font-weight:900;
    align-items:center;
    justify-content:center;
    z-index:10001;
    box-shadow:0 6px 18px rgba(0,0,0,.16);
    backdrop-filter:blur(14px) saturate(180%);
    -webkit-backdrop-filter:blur(14px) saturate(180%);
    cursor:pointer;
    transition:opacity .18s ease, transform .18s ease, background .18s ease;
}

body.fls-mobile .fls-float-menu-btn.menu-open {
    opacity:0;
    pointer-events:none;
    transform:scale(.92);
}

body.fls-mobile .fls-float-menu-btn:active {
    transform:scale(.94);
    background:rgba(255,255,255,.72);
}

/* ============================================================
   手机端：侧边栏默认关闭
   ============================================================ */
body.fls-mobile .sidebar {
    transform:translateX(-100%)!important;
    width:240px!important;
}

body.fls-mobile .sidebar.open {
    transform:translateX(0)!important;
}

body.fls-mobile .main {
    margin-left:0!important;
    width:100%!important;
    max-width:100%!important;
}

body.fls-mobile .menu-btn {
    display:none!important;
}

body.fls-mobile .topbar {
    height:52px!important;
    padding:0 10px 0 52px!important;
}

body.fls-mobile .title {
    font-size:16px!important;
}

body.fls-mobile .content {
    padding:12px!important;
    width:100%!important;
    max-width:100%!important;
}

body.fls-mobile .card {
    padding:12px!important;
    border-radius:12px!important;
    width:100%!important;
    max-width:100%!important;
}

body.fls-mobile .grid {
    grid-template-columns:repeat(2,minmax(0,1fr))!important;
    gap:10px!important;
}

body.fls-mobile .stat {
    padding:12px!important;
}

body.fls-mobile .stat .label {
    font-size:12px!important;
}

body.fls-mobile .stat .num {
    font-size:22px!important;
}

body.fls-mobile .form-grid {
    grid-template-columns:1fr!important;
}

body.fls-mobile .mask.show {
    display:block!important;
    position:fixed!important;
    inset:0!important;
    background:rgba(0,0,0,.45)!important;
    z-index:20!important;
}

body.fls-mobile .content > .card[style*="max-width"] {
    max-width:none!important;
    width:100%!important;
    margin:16px 0!important;
}

/* ============================================================
   文件管理 / 代理管理：保持横向表格
   ============================================================ */
body.fls-mobile.page-pull .table-wrap table,
body.fls-mobile.page-proxy .table-wrap table {
    display:table!important;
    min-width:980px!important;
    width:100%!important;
    border-collapse:collapse!important;
}

body.fls-mobile.page-pull .table-wrap thead,
body.fls-mobile.page-proxy .table-wrap thead {
    display:table-header-group!important;
}

body.fls-mobile.page-pull .table-wrap tbody,
body.fls-mobile.page-proxy .table-wrap tbody {
    display:table-row-group!important;
}

body.fls-mobile.page-pull .table-wrap tr,
body.fls-mobile.page-proxy .table-wrap tr {
    display:table-row!important;
    width:auto!important;
    background:transparent!important;
    border:0!important;
    border-radius:0!important;
    padding:0!important;
    margin:0!important;
    box-shadow:none!important;
}

body.fls-mobile.page-pull .table-wrap th,
body.fls-mobile.page-proxy .table-wrap th,
body.fls-mobile.page-pull .table-wrap td,
body.fls-mobile.page-proxy .table-wrap td {
    display:table-cell!important;
    width:auto!important;
    border-bottom:1px solid #e5e7eb!important;
    padding:10px 8px!important;
    font-size:13px!important;
    vertical-align:middle!important;
}

body.fls-mobile.page-pull .table-wrap td::before,
body.fls-mobile.page-proxy .table-wrap td::before {
    display:none!important;
    content:""!important;
}

/* ============================================================
   依赖管理：双列卡片
   ============================================================ */
body.fls-mobile.page-deps .table-wrap table {
    display:block!important;
    min-width:0!important;
    width:100%!important;
}

body.fls-mobile.page-deps .table-wrap thead {
    display:none!important;
}

body.fls-mobile.page-deps .table-wrap tbody {
    display:grid!important;
    grid-template-columns:repeat(2,minmax(0,1fr))!important;
    gap:12px!important;
}

body.fls-mobile.page-deps .table-wrap tr {
    display:block!important;
    margin:0!important;
    padding:12px!important;
    border:1px solid #e5e7eb!important;
    border-radius:14px!important;
    background:#fff!important;
    box-shadow:0 4px 16px rgba(0,0,0,.04)!important;
}

body.fls-mobile.page-deps .table-wrap td {
    display:block!important;
    border-bottom:1px solid #f1f5f9!important;
    padding:7px 0!important;
    width:100%!important;
}

body.fls-mobile.page-deps .table-wrap td::before {
    display:block!important;
    content:attr(data-label);
    color:#6b7280;
    font-size:12px;
    font-weight:700;
    margin-bottom:3px;
}

body.fls-mobile.page-deps .table-wrap td:last-child {
    border-bottom:0!important;
}

body.fls-mobile.page-deps .table-wrap td:last-child .btn {
    width:100%!important;
    margin:4px 0!important;
}

/* ============================================================
   运行环境：运行环境表格保持横向滚动
   ============================================================ */
body.fls-mobile.page-status #runtimeTable {
    display:table!important;
    min-width:760px!important;
    width:100%!important;
    border-collapse:collapse!important;
}

body.fls-mobile.page-status #runtimeTable thead {
    display:table-header-group!important;
}

body.fls-mobile.page-status #runtimeTable tbody {
    display:table-row-group!important;
}

body.fls-mobile.page-status #runtimeTable tr {
    display:table-row!important;
    width:auto!important;
    background:transparent!important;
    border:0!important;
    border-radius:0!important;
    padding:0!important;
    margin:0!important;
    box-shadow:none!important;
}

body.fls-mobile.page-status #runtimeTable th,
body.fls-mobile.page-status #runtimeTable td {
    display:table-cell!important;
    width:auto!important;
    border-bottom:1px solid #e5e7eb!important;
    padding:10px 8px!important;
    font-size:13px!important;
    vertical-align:middle!important;
}

body.fls-mobile.page-status #runtimeTable td::before {
    display:none!important;
    content:""!important;
}

/* ============================================================
   日志管理：卡片折叠样式
   ============================================================ */
#logsGroupGrid {
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:14px;
}

.log-group-card {
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:14px;
    box-shadow:0 4px 16px rgba(0,0,0,.04);
    overflow:hidden;
    margin:0;
}

.log-group-card summary {
    cursor:pointer;
    list-style:none;
    padding:14px;
}

.log-group-card summary::-webkit-details-marker {
    display:none;
}

.log-group-card summary::after {
    content:"点击展开";
    display:block;
    margin-top:8px;
    color:#6b7280;
    font-size:12px;
    font-weight:700;
}

.log-group-card[open] summary::after {
    content:"点击收起";
}

.log-group-head {
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    gap:10px;
}

.log-group-title {
    font-size:15px;
    font-weight:900;
    color:#111827;
    line-height:1.4;
    word-break:break-word;
}

.log-group-meta {
    display:flex;
    gap:6px;
    flex-wrap:wrap;
    justify-content:flex-end;
    min-width:76px;
}

.log-group-sub {
    margin-top:6px;
    color:#6b7280;
    font-size:12px;
    line-height:1.5;
    word-break:break-word;
}

.log-group-body {
    padding:0 14px 14px;
}

.log-group-body .table-wrap table {
    min-width:560px;
}

.log-empty-card {
    padding:16px;
    color:#6b7280;
    background:#fff;
    border-radius:12px;
}

/* 移动端日志管理仍然双列，极窄屏单列 */
body.fls-mobile.page-logs #logsGroupGrid {
    display:grid!important;
    grid-template-columns:repeat(2,minmax(0,1fr))!important;
    gap:12px!important;
}

body.fls-mobile.page-logs .log-group-card summary {
    padding:12px!important;
}

body.fls-mobile.page-logs .log-group-body {
    padding:0 12px 12px!important;
}

body.fls-mobile.page-logs .log-group-title {
    font-size:14px!important;
}

/* 日志卡片内部日志文件表格保持横向滚动 */
body.fls-mobile.page-logs .log-group-body .table-wrap table {
    display:table!important;
    min-width:560px!important;
    width:100%!important;
    border-collapse:collapse!important;
}

body.fls-mobile.page-logs .log-group-body .table-wrap thead {
    display:table-header-group!important;
}

body.fls-mobile.page-logs .log-group-body .table-wrap tbody {
    display:table-row-group!important;
}

body.fls-mobile.page-logs .log-group-body .table-wrap tr {
    display:table-row!important;
    box-shadow:none!important;
    border:0!important;
    padding:0!important;
    margin:0!important;
}

body.fls-mobile.page-logs .log-group-body .table-wrap th,
body.fls-mobile.page-logs .log-group-body .table-wrap td {
    display:table-cell!important;
    padding:8px 6px!important;
    border-bottom:1px solid #e5e7eb!important;
    font-size:12px!important;
}

body.fls-mobile.page-logs .log-group-body .table-wrap td::before {
    display:none!important;
    content:""!important;
}

/* ============================================================
   其他普通表格：移动端卡片化
   ============================================================ */
body.fls-mobile:not(.page-pull):not(.page-proxy):not(.page-deps):not(.page-status):not(.page-logs) .table-wrap table:not(#tasksTable) {
    min-width:0!important;
    width:100%!important;
    display:block!important;
    border-collapse:separate!important;
}

body.fls-mobile:not(.page-pull):not(.page-proxy):not(.page-deps):not(.page-status):not(.page-logs) .table-wrap table:not(#tasksTable) thead {
    display:none!important;
}

body.fls-mobile:not(.page-pull):not(.page-proxy):not(.page-deps):not(.page-status):not(.page-logs) .table-wrap table:not(#tasksTable) tbody {
    display:block!important;
    width:100%!important;
}

body.fls-mobile:not(.page-pull):not(.page-proxy):not(.page-deps):not(.page-status):not(.page-logs) .table-wrap table:not(#tasksTable) tr {
    display:block!important;
    width:100%!important;
    background:#fff!important;
    border:1px solid #e5e7eb!important;
    border-radius:14px!important;
    padding:12px!important;
    margin-bottom:12px!important;
    box-shadow:0 4px 16px rgba(0,0,0,.04)!important;
}

body.fls-mobile:not(.page-pull):not(.page-proxy):not(.page-deps):not(.page-status):not(.page-logs) .table-wrap table:not(#tasksTable) td {
    display:flex!important;
    align-items:flex-start!important;
    justify-content:space-between!important;
    gap:12px!important;
    width:100%!important;
    border-bottom:1px solid #f1f5f9!important;
    padding:9px 0!important;
    font-size:14px!important;
    word-break:break-word!important;
}

body.fls-mobile:not(.page-pull):not(.page-proxy):not(.page-deps):not(.page-status):not(.page-logs) .table-wrap table:not(#tasksTable) td:last-child {
    border-bottom:0!important;
    display:block!important;
}

body.fls-mobile:not(.page-pull):not(.page-proxy):not(.page-deps):not(.page-status):not(.page-logs) .table-wrap table:not(#tasksTable) td::before {
    content:attr(data-label);
    flex:0 0 86px;
    max-width:86px;
    color:#6b7280;
    font-size:12px;
    font-weight:700;
    line-height:1.5;
}

/* 变量长值折叠 */
.fls-collapsible-value[open] summary .fls-value-preview {
    display:none!important;
}

.fls-collapsible-value[open] summary::after {
    content:"点击收起";
    color:#6b7280;
    font-size:12px;
}

.fls-collapsible-value summary {
    cursor:pointer;
    list-style:none;
}

.fls-collapsible-value summary::-webkit-details-marker {
    display:none;
}

/* 日志悬浮按钮 */
.fls-log-float {
    position:fixed;
    right:14px;
    bottom:90px;
    z-index:9999;
    display:flex;
    flex-direction:column;
    gap:8px;
}

.fls-log-float button,
.fls-log-new-tip {
    border:0;
    border-radius:999px;
    box-shadow:0 6px 20px rgba(0,0,0,.18);
    cursor:pointer;
}

.fls-log-float button {
    width:42px;
    height:42px;
    background:#111827;
    color:#fff;
    font-size:18px;
    font-weight:900;
}

.fls-log-float button:hover {
    background:#18a058;
}

.fls-log-new-tip {
    position:fixed;
    right:14px;
    bottom:22px;
    z-index:10000;
    display:none;
    background:#18a058;
    color:#fff;
    padding:10px 14px;
    font-size:14px;
    font-weight:800;
}

/* 响应式 */
@media(max-width:1280px) {
    .grid {
        grid-template-columns:repeat(3,minmax(0,1fr));
    }
}

@media(max-width:900px) {
    .sidebar {
        transform:translateX(-100%);
        width:240px;
    }

    .sidebar.open {
        transform:translateX(0);
    }

    .mask.show {
        display:block;
        position:fixed;
        inset:0;
        background:rgba(0,0,0,.45);
        z-index:20;
    }

    .main {
        margin-left:0;
        width:100%;
    }

    .content {
        padding:14px;
    }

    .form-grid {
        grid-template-columns:1fr;
    }

    .grid {
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:12px;
    }
}

@media(max-width:520px) {
    .content {
        padding:12px;
    }

    .card {
        border-radius:12px;
        padding:12px;
    }

    .grid {
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:10px;
    }

    .stat {
        padding:12px;
    }

    .stat .label {
        font-size:12px;
    }

    .stat .num {
        font-size:22px;
    }

    input, textarea, select {
        font-size:16px;
    }

    .btn {
        min-height:36px;
        padding:7px 10px;
        font-size:12px;
    }

    pre.log {
        min-height:480px;
        font-size:12px;
    }
}

@media(max-width:390px) {
    body.fls-mobile.page-deps .table-wrap tbody,
    body.fls-mobile.page-logs #logsGroupGrid {
        grid-template-columns:1fr!important;
    }
}
</style>

</head>

<body class="page-__ACTIVE__">
<div class="mask" id="mask" onclick="toggleMenu(false)"></div>
<button class="fls-float-menu-btn" id="flsFloatMenuBtn" type="button" onclick="toggleMenu()">☰</button>

<div class="app">
    <aside class="sidebar" id="sidebar">
        <div class="brand"><span></span>FLS 面板</div>
        <div class="nav">__NAV__</div>
    </aside>

    <main class="main">
        <div class="topbar">
            <div class="title">__TITLE__</div>
        </div>

        <div class="content">__BODY__</div>
    </main>
</div>

<script>
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

/* AJAX 页面切换 */
(function(){
    if (window.__FLS_AJAX_LAYOUT__) return;
    window.__FLS_AJAX_LAYOUT__ = true;

    function sameOrigin(url){
        try {
            return new URL(url, location.href).origin === location.origin;
        } catch(e) {
            return false;
        }
    }

    function skipPath(path){
        if (path.indexOf("/scripts/download/") === 0) return true;
        if (path === "/backup/export") return true;
        if (path.indexOf("/api/") === 0) return true;
        return false;
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

    function setLoading(on){
        const content = document.querySelector(".content");
        if (!content) return;

        content.style.opacity = on ? "0.55" : "1";
        content.style.transition = "opacity .12s ease";
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

    async function replaceHtml(res, push){
        const text = await res.text();
        const doc = new DOMParser().parseFromString(text, "text/html");

        const newContent = doc.querySelector(".content");
        const newTitle = doc.querySelector(".title");
        const newNav = doc.querySelector(".nav");
        const newBody = doc.body;

        const oldContent = document.querySelector(".content");
        const oldTitle = document.querySelector(".title");
        const oldNav = document.querySelector(".nav");

        if (!newContent || !oldContent) {
            location.href = res.url || location.href;
            return;
        }

        if (window.__FLS_ACTIVE_LOG_INTERVAL__) {
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
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

        if (push) {
            history.pushState({url: res.url || location.href}, "", res.url || location.href);
        }

        window.scrollTo(0, 0);
        toggleMenu(false);
    }

    async function ajaxLoad(url, push){
        setLoading(true);

        try {
            const res = await fetch(url, {
                headers: {"X-Requested-With":"FLS-Ajax"},
                credentials: "same-origin"
            });

            if (!res.ok) {
                location.href = url;
                return;
            }

            await replaceHtml(res, push);
            setLoading(false);
        } catch(e) {
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

        ajaxLoad(a.href, true);
    }, true);

    document.addEventListener("submit", async function(e){
        const form = e.target;

        if (!form || !form.closest(".content")) return;

        const method = (form.getAttribute("method") || "GET").toUpperCase();
        const action = form.getAttribute("action") || location.href;
        const url = new URL(action, location.href);

        if (!sameOrigin(url.href)) return;

        e.preventDefault();
        e.stopImmediatePropagation();

        setLoading(true);

        try {
            let fetchUrl = url.href;

            const opts = {
                method: method,
                headers: {"X-Requested-With":"FLS-Ajax"},
                credentials: "same-origin"
            };

            if (method === "GET") {
                const fd = new FormData(form);
                fd.forEach(function(v,k){
                    url.searchParams.set(k,v);
                });
                fetchUrl = url.href;
            } else {
                const fd = new FormData(form);

                if (e.submitter && e.submitter.name && !fd.has(e.submitter.name)) {
                    fd.append(e.submitter.name, e.submitter.value || "");
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

function flsLogCopyAll(){
    const el = document.getElementById("log");

    if (!el) {
        alert("未找到日志内容");
        return;
    }

    const text = el.textContent || "";

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
</script>
</body>
</html>
'''

    return (
        html
        .replace("__TITLE__", h(title))
        .replace("__NAV__", nav_html)
        .replace("__BODY__", body)
        .replace("__ACTIVE__", h(active))
    )