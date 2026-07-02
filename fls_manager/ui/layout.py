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
        ("dashboard", "/", "📊 仪表盘"),
        ("tasks", "/tasks", "📜 任务管理"),
        ("history", "/history", "🧾 运行历史"),
        ("env", "/env", "🌐 全局变量"),
        ("proxy", "/proxy", "🧩 代理管理"),
        ("pull", "/pull", "📂 脚本管理"),
        ("online_scripts", "/online-scripts", "🌍 在线脚本"),
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
<meta name="csrf-token" content="__CSRF_TOKEN__">

<link rel="stylesheet" href="/static/fls.css?v=20260703-1">

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

<script src="/static/fls.js?v=20260703-1"></script>
</body>
</html>
'''

    return (
        html
        .replace("__TITLE__", h(title))
        .replace("__NAV__", nav_html)
        .replace("__BODY__", body)
        .replace("__ACTIVE__", h(active))
        .replace("__CSRF_TOKEN__", h(token))
    )
