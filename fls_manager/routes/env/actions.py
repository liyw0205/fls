from ._common import *


@bp.route("/env/delete/<key>", methods=["POST"])
def env_delete(key):
    return env_submit("delete", key)


@bp.route("/env/import", methods=["GET", "POST"])
def env_import_from_tasks():
    if request.method == "POST":
        return env_submit("import")
    tasks = load_tasks()
    env = load_global_env()
    sources = {f"{task['id']}::{key}": env_version({key: value})
               for task in tasks for key, value in task.get("env", {}).items()}
    return layout("导入全局变量", "env", env_form("import", {
        "version": env_version(env), "sources": sources,
    }, env=env, tasks=tasks))
