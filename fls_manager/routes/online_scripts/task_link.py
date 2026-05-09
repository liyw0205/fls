from ._common import *


@bp.route("/online-scripts/task-link/<script_id>", methods=["POST"])
def online_scripts_pull_task_link(script_id):
    items = load_online_script_cache()

    item = None
    item_index = -1

    for idx, one in enumerate(items):
        if one.get("id") == script_id:
            item = one
            item_index = idx
            break

    if not item:
        abort(404)

    task_link = str(item.get("task_link") or "").strip()

    if not task_link:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="该脚本没有配置外部任务源 task_link",
            )
        )

    proxy_id = request.form.get("proxy_id", "").strip()

    try:
        linked_tasks = fetch_task_link_tasks(
            item,
            proxy_id=proxy_id,
            timeout=20,
        )

        if not linked_tasks:
            return redirect(
                url_for(
                    "online_scripts.online_scripts_page",
                    err="外部任务源已拉取，但没有可用任务",
                )
            )

        old_tasks = online_script_task_crons(item)

        exists_keys = set()
        merged = []

        for task in old_tasks:
            key = (
                str(task.get("name") or ""),
                str(task.get("cron") or ""),
                str(task.get("command") or ""),
            )
            exists_keys.add(key)
            merged.append(task)

        added = 0

        for task in linked_tasks:
            key = (
                str(task.get("name") or ""),
                str(task.get("cron") or ""),
                str(task.get("command") or ""),
            )

            if key in exists_keys:
                continue

            exists_keys.add(key)
            merged.append(task)
            added += 1

        item["task_cron"] = merged
        items[item_index] = item
        save_online_script_cache(items)

        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg=f"外部任务源拉取完成，新增 {added} 个任务，当前共 {len(merged)} 个任务",
            )
        )

    except Exception as e:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err=f"外部任务源拉取失败：{e}",
            )
        )

