from flask import request

from . import bp
from .helpers import script_safe_path, script_url, breadcrumb, render_rows

from ...paths import SCRIPT_DIR
from ...utils import h
from ...ui.layout import layout
from ...ui.components import code_card, page_header_card, table_card


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

    header = page_header_card(
        "脚本管理",
        f"""
                当前目录：<b>{h(current_dir)}</b><br>
                路径：{breadcrumb(current_rel)}
            """,
        f"""
            <a class="btn btn-primary" href="/pull/fetch?p={h(current_rel)}">拉取</a>
            <a class="btn btn-orange" href="/pull/import?p={h(current_rel)}">导入</a>
            <a class="btn btn-blue" href="/pull/new?p={h(current_rel)}">新建</a>
            <a class="btn btn-gray" href="/pull">回到根目录</a>
        """,
    )

    table = table_card(
        "文件列表",
        ("类型", "名称 / 相对路径", "大小", "修改时间", "绝对路径", "操作"),
        render_rows(current_rel),
    )
    command_example = code_card(
        "任务命令示例",
        """
task 1.py<br>
task folder/main.py<br>
task /root/fls/scripts/demo.sh arg1 arg2
        """,
    )

    body = f"""
{header}
{table}
{command_example}
"""
    return layout("脚本管理", "pull", body)
