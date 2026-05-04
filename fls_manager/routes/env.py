from flask import Blueprint, request, redirect, url_for, abort

from ..models import load_global_env, save_global_env, load_tasks
from ..utils import h, parse_env_text, env_to_text
from ..ui.layout import layout

bp = Blueprint("env", __name__)


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

        rows += f"""
<tr>
    <td><b>{h(k)}</b></td>
    <td>{collapsible_text(v, 50)}</td>
    <td>
        <a class="btn btn-blue" href="/env/edit/{h(k)}">编辑</a>
        <a class="btn btn-red" href="/env/delete/{h(k)}" onclick="return confirm('确定删除变量 {h(k)} 吗？')">删除</a>
    </td>
</tr>
"""

    return rows


@bp.route("/env")
def env_page():
    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">全局变量列表</div>
            <div class="help">
                全局变量对所有任务生效；任务变量中同名变量会覆盖全局变量。
            </div>
        </div>
        <div class="action-row">
            <a class="btn btn-blue" href="/env/view">查看全部</a>
            <a class="btn btn-orange" href="/env/import">从任务导入</a>
            <a class="btn btn-primary" href="/env/new">新增变量</a>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">变量列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>变量名</th>
                    <th>值</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>{env_rows()}</tbody>
        </table>
    </div>
</div>
"""
    return layout("全局变量", "env", body)


@bp.route("/env/view", methods=["GET", "POST"])
def env_view_all():
    if request.method == "POST":
        env = parse_env_text(request.form.get("env_text", ""))
        save_global_env(env)
        return redirect(url_for("env.env_page"))

    env_text = env_to_text(load_global_env())

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">查看全部全局变量</div>
    <textarea name="env_text" placeholder='变量名="变量值"'>{h(env_text)}</textarea>
    <div class="help">
        可一次性查看和编辑全部全局变量，保存后会整体覆盖。
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存全部</button>
    <a class="btn btn-gray" href="/env">返回列表</a>
</div>
</form>
"""
    return layout("查看全部全局变量", "env", body)


@bp.route("/env/new", methods=["GET", "POST"])
def env_new():
    if request.method == "POST":
        key = request.form.get("key", "").strip()
        value = request.form.get("value", "")

        if not key:
            return "变量名不能为空", 400

        env = load_global_env()
        env[key] = value
        save_global_env(env)

        return redirect(url_for("env.env_page"))

    body = """
<form method="post">
<div class="card">
    <div class="card-title">新增全局变量</div>
    <div class="form-grid">
        <div class="form-item">
            <label>变量名</label>
            <input name="key" placeholder="例如：TOKEN">
        </div>
        <div class="form-item">
            <label>变量值</label>
            <input name="value" placeholder="变量值">
        </div>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/env">返回</a>
</div>
</form>
"""
    return layout("新增全局变量", "env", body)


@bp.route("/env/edit/<key>", methods=["GET", "POST"])
def env_edit(key):
    env = load_global_env()

    if key not in env:
        abort(404)

    if request.method == "POST":
        new_key = request.form.get("key", "").strip()
        value = request.form.get("value", "")

        if not new_key:
            return "变量名不能为空", 400

        if new_key != key:
            env.pop(key, None)

        env[new_key] = value
        save_global_env(env)

        return redirect(url_for("env.env_page"))

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">编辑全局变量</div>
    <div class="form-grid">
        <div class="form-item">
            <label>变量名</label>
            <input name="key" value="{h(key)}">
        </div>
        <div class="form-item">
            <label>变量值</label>
            <input name="value" value="{h(env.get(key, ''))}">
        </div>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/env">返回</a>
</div>
</form>
"""
    return layout("编辑全局变量", "env", body)


@bp.route("/env/delete/<key>")
def env_delete(key):
    env = load_global_env()

    if key in env:
        env.pop(key, None)
        save_global_env(env)

    return redirect(url_for("env.env_page"))


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
            exists = k in global_env
            exists_badge = '<span class="badge orange">将覆盖</span>' if exists else '<span class="badge green">新增</span>'

            rows += f"""
<tr>
    <td><input type="checkbox" name="items" value="{h(task.get('id'))}::{h(k)}" checked style="width:auto;"></td>
    <td>{h(task_name)}</td>
    <td><b>{h(k)}</b></td>
    <td><code>{h(v)}</code></td>
    <td>{exists_badge}</td>
</tr>
"""

    if not rows:
        rows = '<tr><td colspan="5">所有任务都没有单独设置变量</td></tr>'

    return rows


@bp.route("/env/import", methods=["GET", "POST"])
def env_import_from_tasks():
    if request.method == "POST":
        selected = request.form.getlist("items")
        overwrite = request.form.get("overwrite") == "1"

        tasks = load_tasks()
        task_map = {t.get("id"): t for t in tasks}

        env = load_global_env()

        for item in selected:
            if "::" not in item:
                continue

            task_id, key = item.split("::", 1)
            task = task_map.get(task_id)

            if not task:
                continue

            task_env = task.get("env", {}) or {}

            if key not in task_env:
                continue

            if key in env and not overwrite:
                continue

            env[key] = task_env[key]

        save_global_env(env)

        return redirect(url_for("env.env_page"))

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">从任务变量导入到全局变量</div>
    <div class="help">
        选择要导入的任务变量。默认勾选全部。<br>
        如果变量名已存在，勾选“允许覆盖”才会覆盖全局变量。
    </div>
    <br>
    <label>
        <input type="checkbox" name="overwrite" value="1" style="width:auto;">
        允许覆盖已有全局变量
    </label>
</div>

<div class="card">
    <div class="card-title">可导入变量</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>选择</th>
                    <th>任务</th>
                    <th>变量名</th>
                    <th>值</th>
                    <th>导入状态</th>
                </tr>
            </thead>
            <tbody>{collect_task_env_rows()}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">导入所选变量</button>
    <a class="btn btn-gray" href="/env">返回</a>
</div>
</form>
"""
    return layout("导入全局变量", "env", body)
