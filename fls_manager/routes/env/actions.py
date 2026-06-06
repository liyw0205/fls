from ._common import *


@bp.route("/env/delete/<key>", methods=["POST"])
def env_delete(key):
    env = load_global_env()

    if key in env:
        env.pop(key, None)
        save_global_env(env)

    return redirect(url_for("env.env_page"))


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
