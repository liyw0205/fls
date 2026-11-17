from ._common import *


@bp.route("/env")
def env_page():
    header = page_header_card("全局变量列表", "全局变量对所有任务生效；任务变量中同名变量会覆盖全局变量。",
        '<a class="btn btn-blue" href="/env/view">查看全部</a>'
        '<a class="btn btn-orange" href="/env/import">从任务导入</a>'
        '<a class="btn btn-primary" href="/env/new">新增变量</a>')
    return layout("全局变量", "env", header + table_card("变量列表", ("变量名", "值", "操作"), env_rows()))


@bp.route("/env/view", methods=["GET", "POST"])
def env_view_all():
    if request.method == "POST":
        return env_submit("replace")
    env = load_global_env()
    return layout("查看全部全局变量", "env", env_form("replace", {"env_text": env_to_text(env), "version": env_version(env)}))


@bp.route("/env/new", methods=["GET", "POST"])
def env_new():
    if request.method == "POST":
        return env_submit("new")
    return layout("新增全局变量", "env", env_form("new", {}))


@bp.route("/env/edit/<key>", methods=["GET", "POST"])
def env_edit(key):
    if request.method == "POST":
        return env_submit("edit", key)
    env = load_global_env()
    if key not in env:
        abort(404)
    return layout("编辑全局变量", "env", env_form("edit", {
        "original_key": key, "key": key, "value": env[key], "version": env_version(env, [key]),
    }))
