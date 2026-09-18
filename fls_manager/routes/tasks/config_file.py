from urllib.parse import quote

from flask import request, abort

from . import bp
from .helpers import task_config_safe_path

from ...models import get_task
from ...utils import h, get_back_url
from ...ui.layout import layout
from ...ui.components import message_card, page_header


@bp.route("/task/config/<task_id>", methods=["GET", "POST"])
def task_config_edit(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    back_url = get_back_url("/tasks")
    back_param = h(quote(back_url, safe="/"))
    config_path = str(task.get("config_path") or "").strip()

    if not config_path:
        body = f"""
<section class="section fls-form-section">
    <h2 class="section-title">任务配置文件</h2>
    <div class="help">该任务没有配置 config_path。</div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}?back={back_param}">编辑任务</a>
</section>
"""
        return layout("任务配置文件", "tasks", body)

    try:
        target = task_config_safe_path(config_path)
    except Exception as e:
        body = f"""
<section class="section fls-form-section">
    <h2 class="section-title">配置文件路径非法</h2>
    <div class="help" style="color:#dc2626;">{h(e)}</div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}?back={back_param}">编辑任务</a>
</section>
"""
        return layout("配置文件路径非法", "tasks", body), 400

    msg = ""

    if request.method == "POST":
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                request.form.get("content", ""),
                encoding="utf-8",
            )
            msg = "保存成功"
        except Exception as e:
            msg = f"保存失败：{e}。下一步：检查文件权限后重试"

    if target.exists():
        try:
            content = target.read_text(
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            content = f"# 读取失败：{e}\n"
    else:
        content = (
            "# 配置文件不存在，可在这里新建。\n"
            "# 示例：\n"
            "# key: value\n"
        )

    header = page_header(
        "编辑任务配置",
        help_html=f"编辑任务“{h(task.get('name') or task_id)}”的配置文件内容。",
        actions_html=(
            f'<a class="btn btn-gray" href="{h(back_url)}">返回任务</a>'
            f'<a class="btn btn-orange" href="/task/edit/{h(task_id)}?back={back_param}">编辑任务</a>'
        ),
    )

    body = f"""
{header}
<form method="post">
<section class="section fls-form-section">
    <h2 class="section-title">编辑任务配置：{h(task.get('name') or task_id)}</h2>
    <div class="help">
        配置路径：<code>{h(config_path)}</code><br>
        实际路径：<code>{h(target)}</code><br>
        状态：{"已存在" if target.exists() else "文件不存在，保存后会自动创建"}
    </div>
    <br>
    <button class="btn btn-blue" type="submit" formaction="/run/{h(task_id)}?back={back_param}" formmethod="post">立即运行任务</button>
    <a class="btn btn-orange" href="/task/edit/{h(task_id)}?back={back_param}">编辑任务</a>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</section>

{message_card(msg, "success" if msg.startswith("保存成功") else "error", strong=True)}

<section class="section fls-form-section">
    <h2 class="section-title">配置内容</h2>
    <textarea name="content" style="min-height:680px;" aria-label="任务配置文件内容">{h(content)}</textarea>
</section>

<section class="section fls-form-section fls-save-section">
    <button class="btn btn-primary" type="submit">保存任务配置</button>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</section>
</form>
"""
    return layout("编辑任务配置", "tasks", body)
