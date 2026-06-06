from .bp import bp
from flask import request, redirect, url_for, abort

from ...models import load_global_env, save_global_env, load_tasks
from ...utils import h, parse_env_text, env_to_text
from ...ui.layout import layout
from ...sensitive import mask_if_sensitive

def collapsible_text(value, limit=50):
    raw = str(value if value is not None else "")

    if len(raw) <= int(limit):
        return f"<code>{h(raw)}</code>"

    short = raw[:int(limit)] + "..."

    return (
        '<details class="fls-collapsible-value" style="display:inline-block;max-width:100%;">'
        f'<summary><code class="fls-value-preview">{h(short)}</code></summary>'
        f'<code style="white-space:pre-wrap;word-break:break-all;">{h(raw)}</code>'
        '</details>'
    )


def env_rows():
    env = load_global_env()

    if not env:
        return '<tr><td colspan="3">暂无全局变量</td></tr>'

    rows = ""

    for k in sorted(env.keys()):
        v = env.get(k, "")
        display_value = mask_if_sensitive(k, v)

        rows += f"""
<tr>
    <td><b>{h(k)}</b></td>
    <td>{collapsible_text(display_value, 50)}</td>
    <td>
        <a class="btn btn-blue" href="/env/edit/{h(k)}">编辑</a>
        <form class="inline-form" method="post" action="/env/delete/{h(k)}">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定删除变量 {h(k)} 吗？')">删除</button>
        </form>
    </td>
</tr>
"""

    return rows


def collect_task_env_rows():
    tasks = load_tasks()
    global_env = load_global_env()
    rows = ""

    for task in tasks:
        task_name = task.get("name") or task.get("command") or task.get("id")
        task_env = task.get("env", {}) or {}

        if not task_env:
            continue

        for k in sorted(task_env.keys()):
            v = task_env.get(k, "")
            display_value = mask_if_sensitive(k, v)
            exists = k in global_env
            exists_badge = '<span class="badge orange">将覆盖</span>' if exists else '<span class="badge green">新增</span>'

            rows += f"""
<tr>
    <td><input type="checkbox" name="items" value="{h(task.get('id'))}::{h(k)}" checked style="width:auto;"></td>
    <td>{h(task_name)}</td>
    <td><b>{h(k)}</b></td>
    <td><code>{h(display_value)}</code></td>
    <td>{exists_badge}</td>
</tr>
"""

    if not rows:
        rows = '<tr><td colspan="5">所有任务都没有单独设置变量</td></tr>'

    return rows
