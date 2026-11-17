from .bp import bp
import json
import uuid
from urllib.parse import quote
from flask import request, redirect, url_for, abort

from ...models import load_global_env, save_global_env, load_tasks
from ...utils import h, parse_env_text, env_to_text
from ...ui.layout import layout
from ...ui.components import empty_table_row, status_badge
from ...sensitive import mask_if_sensitive
from ...global_env import env_version, mutate_global_env
from ...ui.components import page_header_card, table_card

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
        return empty_table_row(3, "暂无全局变量", '<a class="btn btn-primary" href="/env/new">新增变量</a>')

    rows = ""

    for k in sorted(env.keys()):
        v = env.get(k, "")
        display_value = mask_if_sensitive(k, v)

        rows += f"""
<tr>
    <td><b>{h(k)}</b></td>
    <td>{collapsible_text(display_value, 50)}</td>
    <td>
        <div class="row-actions" aria-label="变量 {h(k)} 行操作">
            <div class="row-actions-primary">
                <a class="btn btn-blue" href="/env/edit/{h(quote(k, safe=''))}">编辑变量</a>
            </div>
            <div class="row-actions-danger">
                <form class="inline-form" method="post" action="/env/delete/{h(quote(k, safe=''))}">
                    <input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
                    <input type="hidden" name="version" value="{env_version(env, [k])}">
                    <button class="btn btn-red" type="submit" onclick="return confirm('确定删除变量 {h(k)} 吗？')">删除变量</button>
                </form>
            </div>
        </div>
    </td>
</tr>
"""

    return rows


def collect_task_env_rows(tasks=None, global_env=None, selected=None):
    tasks = load_tasks() if tasks is None else tasks
    global_env = load_global_env() if global_env is None else global_env
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
            exists_badge = status_badge("warning" if exists else "success", "将覆盖" if exists else "新增")
            checked = "checked" if selected is None or f"{task['id']}::{k}" in selected else ""

            rows += f"""
<tr>
    <td><input type="checkbox" name="items" value="{h(task.get('id'))}::{h(k)}" {checked} aria-label="选择 {h(task_name)} 的 {h(k)}" style="width:auto;"></td>
    <td>{h(task_name)}</td>
    <td><b>{h(k)}</b></td>
            <td>{collapsible_text(display_value, 50)}</td>
    <td>{exists_badge}</td>
</tr>
"""

    if not rows:
        rows = empty_table_row(5, "所有任务都没有单独设置变量")

    return rows


def env_form(action, data, *, env=None, tasks=None, conflict=False):
    titles = {"new": "新增全局变量", "edit": "编辑全局变量", "delete": "删除变量恢复",
              "replace": "查看全部全局变量", "import": "从任务变量导入到全局变量"}
    labels = {"new": "保存全局变量", "edit": "保存变量", "delete": "确认删除变量",
              "replace": "保存全部全局变量", "import": "导入所选变量"}
    url = "/env/" + {"replace": "view"}.get(action, action)
    if action in ("edit", "delete"):
        url += "/" + quote(data["original_key"], safe="")
    hidden = f'<input type="hidden" name="request_id" value="{h(data.get("request_id") or uuid.uuid4().hex)}">'
    for key in ("version", "confirm_retry", "sources"):
        if key in data:
            value = json.dumps(data[key], ensure_ascii=False) if isinstance(data[key], dict) else data[key]
            hidden += f'<input type="hidden" name="{key}" value="{h(value)}">'
    if action in ("new", "edit"):
        fields = f'''<div class="form-grid"><div class="form-item"><label>变量名</label>
<input name="key" value="{h(data.get('key', ''))}" aria-label="全局变量名"></div>
<div class="form-item"><label>变量值</label><textarea name="value" aria-label="全局变量值">{h(data.get('value', ''))}</textarea></div></div>'''
    elif action == "replace":
        fields = f'<textarea name="env_text" aria-label="全部全局变量文本">{h(data.get("env_text", ""))}</textarea>'
    elif action == "delete":
        fields = f'<p>{h(data["original_key"])}</p>'
    else:
        selected = data.get("items")
        if tasks is not None:
            fields = table_card("可导入变量", ("选择", "任务", "变量名", "值", "导入状态"),
                                collect_task_env_rows(tasks, env, selected))
        else:
            fields = "".join(f'<input type="hidden" name="items" value="{h(item)}"><p>{h(item)}</p>' for item in selected or [])
        checked = "checked" if data.get("overwrite") in (True, "1") else ""
        fields += f'<label><input type="checkbox" name="overwrite" value="1" {checked}>允许覆盖已有全局变量</label>'
    header = page_header_card(titles[action])
    label = "确认后重试" if data.get("confirm_retry") else "合并后保存变量" if conflict else labels[action]
    confirmation = ' onclick="return confirm(\'确认当前变量内容后继续？\')"' if conflict or data.get("confirm_retry") or action == "delete" else ""
    return f'''{header}<form method="post" action="{h(url)}">{hidden}
<section class="section fls-form-section">{fields}</section>
<section class="section fls-save-section"><button class="btn btn-primary" type="submit"{confirmation}>{label}</button>
<a class="btn btn-gray" href="/env">返回变量列表</a></section></form>'''


def env_submit(action, original_key=""):
    data = dict(request.form.to_dict(), action=action)
    for key in ("value", "env_text"):
        if key in data:
            data[key] = data[key].replace("\r\n", "\n")
    if action in ("edit", "delete"):
        data["original_key"] = original_key
    if action == "import":
        data["items"] = request.form.getlist("items")
    data.setdefault("request_id", uuid.uuid4().hex)
    payload, status = mutate_global_env(data, load_global_env=load_global_env,
        save_global_env=save_global_env, load_tasks=load_tasks)
    if status == 200:
        return redirect(url_for("env.env_page"))
    response_state = {key: value for key, value in payload.items() if key != "current"}
    body = f'<section class="section" role="alert" data-env-result="{h(json.dumps(response_state, ensure_ascii=False))}"><p>{h(payload["msg"])}</p></section>'
    if payload.get("saved"):
        body += '<a class="btn btn-primary" href="/env">查看已保存变量</a>'
    else:
        current = payload.get("current")
        if payload.get("invalid_field") == "version":
            try:
                current = load_global_env()
                payload["conflict"] = True
            except Exception:
                body += '<p>当前变量无法读取，请修复存储后重新核对版本。</p>'
        conflict = bool(payload.get("conflict") and isinstance(current, dict))
        if payload.get("saved") is False and not payload.get("confirm_required"):
            data["request_id"] = uuid.uuid4().hex
        if action == "import":
            body += '<a class="btn btn-primary" href="/env/import">重新选择任务变量</a>'
        if conflict:
            shown = current if action == "replace" else {key: current[key] for key in (original_key, data.get("key")) if key in current}
            if action == "import":
                shown = {key: mask_if_sensitive(key, value) for key, value in current.items()}
            body += f'<section class="section"><h3>当前全局变量</h3><pre>{h(env_to_text(shown))}</pre></section>'
            data["request_id"] = uuid.uuid4().hex
            data["version"] = env_version(current, [original_key] if action in ("edit", "delete") else None)
        if status == 404 and action == "edit":
            action = "new"
            data.pop("version", None)
            body += '<p>原变量已删除，可将草稿保存为新变量。</p>'
        if payload.get("confirm_required"):
            data["confirm_retry"] = "1"
        body += env_form(action, data, conflict=conflict)
    body += '<a class="btn btn-gray" href="/env">查看全局变量</a>'
    return layout("变量操作恢复", "env", body), status
