from ._common import *
from ...ui.components import page_header_card, table_card


@bp.route("/env")
def env_page():
    header = page_header_card(
        "全局变量列表",
        """
                全局变量对所有任务生效；任务变量中同名变量会覆盖全局变量。
            """,
        """
            <a class="btn btn-blue" href="/env/view">查看全部</a>
            <a class="btn btn-orange" href="/env/import">从任务导入</a>
            <a class="btn btn-primary" href="/env/new">新增变量</a>
        """,
    )

    table = table_card(
        "变量列表",
        ("变量名", "值", "操作"),
        env_rows(),
    )

    body = f"""
{header}
{table}
"""
    return layout("全局变量", "env", body)


@bp.route("/env/view", methods=["GET", "POST"])
def env_view_all():
    if request.method == "POST":
        env = parse_env_text(request.form.get("env_text", ""))
        save_global_env(env)
        return redirect(url_for("env.env_page"))

    env_text = env_to_text(load_global_env())
    header = page_header_card(
        "查看全部全局变量",
        "可一次性查看和编辑全部全局变量，保存后会整体覆盖。",
    )

    body = f"""
<form method="post">
{header}
<div class="card">
    <textarea name="env_text" placeholder='变量名="变量值"'>{h(env_text)}</textarea>
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

    header = page_header_card(
        "新增全局变量",
        "全局变量会对所有任务生效；任务变量中同名变量仍会优先覆盖。",
    )

    body = f"""
<form method="post">
{header}
<div class="card">
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

    header = page_header_card(
        "编辑全局变量",
        "修改变量名会先移除原变量，再保存为新的变量名。",
    )

    body = f"""
<form method="post">
{header}
<div class="card">
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
