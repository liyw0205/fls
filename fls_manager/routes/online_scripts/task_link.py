from ._common import *
from ...online_scripts.source import merge_task_link_tasks, OnlineScriptCacheConflict


def _task_link_error(script_id, message, status_code, proxy_id="", retry=False):
    retry_form = ""
    if retry:
        retry_form = f'''
<form class="inline-form" method="post" action="/online-scripts/task-link/{h(quote(script_id, safe=''))}">
    <select name="proxy_id" aria-label="外部任务源代理">{proxy_select_options(proxy_id)}</select>
    <button class="btn btn-primary" type="submit">重新同步外部任务源</button>
</form>'''
    body = page_header_card(
        "外部任务源同步未完成",
        help_html=h(message),
        actions_html=(
            retry_form
            + '<a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>'
            + '<a class="btn btn-blue" href="/online-scripts/source">检查脚本源数据</a>'
        ),
    )
    return layout("外部任务源同步", "online_scripts", body), status_code


@bp.route("/online-scripts/task-link/<script_id>", methods=["POST"])
def online_scripts_pull_task_link(script_id):
    item = get_online_script(script_id)
    if not item:
        return _task_link_error(script_id, "脚本已不在本地缓存中，请刷新在线脚本列表。", 404)

    task_link = str(item.get("task_link") or "").strip()

    if not task_link:
        return _task_link_error(script_id, "该脚本没有配置外部任务源 task_link，请检查脚本源数据。", 400)

    proxy_id = request.form.get("proxy_id", "").strip()

    try:
        linked_tasks = fetch_task_link_tasks(
            item,
            proxy_id=proxy_id,
            timeout=20,
        )

    except Exception as e:
        return _task_link_error(
            script_id, f"外部任务源请求失败：{e}。请检查任务源地址、代理和网络后重试。",
            502, proxy_id=proxy_id, retry=True,
        )

    if not linked_tasks:
        return _task_link_error(
            script_id, "外部任务源没有可用任务，请检查任务字段后重新同步。",
            422, proxy_id=proxy_id, retry=True,
        )

    try:
        added, total = merge_task_link_tasks(item, linked_tasks)
    except OnlineScriptCacheConflict as e:
        return _task_link_error(script_id, str(e), 409)
    except Exception as e:
        return _task_link_error(
            script_id, f"外部任务源未保存：{e}。请检查缓存文件权限和磁盘空间后重试。",
            500, proxy_id=proxy_id, retry=True,
        )

    return redirect(url_for(
        "online_scripts.online_scripts_page",
        msg=f"外部任务源同步完成，新增 {added} 个任务，当前共 {total} 个任务",
    ))
