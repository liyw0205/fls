import re

from ..csrf import csrf_token
from ..utils import h


def inject_csrf_inputs(body, token):
    hidden = f'<input type="hidden" name="csrf_token" value="{h(token)}">'

    def repl(match):
        return match.group(1) + hidden

    return re.sub(
        r'(<form\b(?=[^>]*\bmethod=["\']?post["\']?)[^>]*>)',
        repl,
        str(body or ""),
        flags=re.IGNORECASE,
    )


def layout(title, active, body):
    token = csrf_token()
    body = inject_csrf_inputs(body, token)

    nav = [
        ("dashboard", "/", "仪表盘"),
        ("tasks", "/tasks", "任务"),
        ("collections", "/collections", "任务合集"),
        ("history", "/history", "运行历史"),
        ("env", "/env", "变量"),
        ("proxy", "/proxy", "代理"),
        ("pull", "/pull", "脚本"),
        ("online_scripts", "/online-scripts", "在线脚本"),
        ("backup", "/backup", "备份"),
        ("deps", "/deps", "依赖"),
        ("logs", "/logs", "日志"),
        ("notify", "/notify", "通知"),
        ("status", "/panel/status", "面板状态"),
        ("config", "/config", "面板配置"),
        ("about", "/about", "关于"),
    ]

    nav_html = ""
    for key, url, text in nav:
        cls = "active" if active == key else ""
        current = ' aria-current="page"' if cls else ""
        nav_html += '<a class="{}" href="{}" data-nav-key="{}"{}>{}</a>'.format(
            cls, h(url), h(key), current, h(text)
        )

    nav_html += '<a href="/logout">退出登录</a>'

    html = r'''
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="csrf-token" content="__CSRF_TOKEN__">

<link rel="icon" type="image/png" href="/static/favicon-generated.png?v=20260920-1">
<link rel="apple-touch-icon" href="/static/favicon-generated.png?v=20260920-1">
<link rel="stylesheet" href="/static/fls.css?v=20260920-3&tree=1">
<link rel="stylesheet" href="/static/fls_responsive.css?v=20260920-3&tree=1">
<link rel="stylesheet" href="/static/fls_theme.css?v=20260920-3&tree=1">

</head>

<body class="__BODY_CLASSES__">
<a class="skip-link" href="#main-content">跳转到主要内容</a>
<div class="mask" id="mask" onclick="toggleMenu(false)"></div>
<button class="fls-float-menu-btn" id="flsFloatMenuBtn" type="button" onclick="toggleMenu()" aria-expanded="false" aria-controls="sidebar" aria-label="打开导航菜单" title="打开导航菜单">☰</button>

<div class="app">
    <aside class="sidebar" id="sidebar">
        <div class="brand"><span></span>FLS 面板</div>
        <div class="fls-update-notice-slot" id="flsUpdateNoticeSlot" aria-live="polite"></div>
        <nav class="nav" aria-label="主导航">__NAV__</nav>
    </aside>

    <main class="main">
        <div class="topbar">
            <div class="title">__TITLE__</div>
            <div class="topbar-tools">
                <button class="topbar-refresh" type="button" onclick="flsRefreshCurrentPage()" aria-label="刷新当前页面" title="刷新当前页面">↻</button>
            </div>
        </div>

        <div class="content page-shell" id="main-content">__BODY__</div>
    </main>
</div>

<script src="/static/fls.js?v=20260920-3&tree=1"></script>
</body>
</html>
'''

    body_classes = f"page-{active}"
    # Collections share task-specific table rules while keeping their own
    # navigation identity and body hook.
    if active == "collections":
        body_classes += " page-tasks"

    return (
        html
        .replace("__TITLE__", h(title))
        .replace("__NAV__", nav_html)
        .replace("__BODY__", body)
        .replace("__BODY_CLASSES__", h(body_classes))
        .replace("__CSRF_TOKEN__", h(token))
    )
