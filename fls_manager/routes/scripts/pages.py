from flask import request

from . import bp
from .helpers import script_safe_path, script_url, breadcrumb, render_rows

from ...paths import SCRIPT_DIR
from ...utils import h
from ...ui.layout import layout


@bp.route("/pull")
def scripts_page():
    current_rel = request.args.get("p", "").strip().strip("/")

    try:
        current_dir = script_safe_path(current_rel)
    except Exception:
        current_rel = ""
        current_dir = SCRIPT_DIR

    if not current_dir.exists():
        current_dir.mkdir(parents=True, exist_ok=True)

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">脚本管理</div>
            <div class="help">
                当前目录：<b>{h(current_dir)}</b><br>
                路径：{breadcrumb(current_rel)}
            </div>
        </div>
        <div class="action-row">
            <a class="btn btn-primary" href="/pull/fetch?p={h(current_rel)}">拉取</a>
            <a class="btn btn-orange" href="/pull/import?p={h(current_rel)}">导入</a>
            <a class="btn btn-blue" href="/pull/new?p={h(current_rel)}">新建</a>
            <a class="btn btn-gray" href="/pull">回到根目录</a>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">文件列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>类型</th><th>名称 / 相对路径</th><th>大小</th><th>修改时间</th><th>绝对路径</th><th>操作</th>
                </tr>
            </thead>
            <tbody>{render_rows(current_rel)}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-title">任务命令示例</div>
    <div class="code">
task 1.py<br>
task folder/main.py<br>
task /root/fls/scripts/demo.sh arg1 arg2
    </div>
</div>
"""
    return layout("脚本管理", "pull", body)