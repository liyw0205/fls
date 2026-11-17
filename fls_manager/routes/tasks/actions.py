import json
import uuid
from urllib.parse import quote

from flask import redirect, url_for, abort, request

from . import bp

from ...models import load_tasks, save_tasks, mutate_tasks
from ...utils import h, now_str, get_back_url
from ...scheduler import reload_scheduler
from ...task_runner import run_task_now, stop_task_now, cancel_task_retry
from ...ui.components import message_card
from ...ui.layout import layout


@bp.route("/task/delete/<task_id>", methods=["POST"])
def task_delete(task_id):
    from ...task_bulk import execute_single
    back_url = get_back_url("/tasks")
    data = request.form.to_dict()
    data.setdefault("request_id", uuid.uuid4().hex)
    try:
        payload, status = execute_single(
            "delete", task_id, data, load_tasks=load_tasks, mutate_tasks=mutate_tasks,
            run_task=run_task_now, stop_task=stop_task_now,
            cancel_retry=cancel_task_retry, reload_scheduler=reload_scheduler,
        )
    except Exception as exc:
        payload, status = {"msg": str(exc)}, 500
    if status == 200:
        return redirect(back_url)
    recovery = ""
    if payload.get("reload_required"):
        recovery = f'''<form method="post" action="/scheduler/reload?back={h(quote(back_url, safe=''))}">
<button class="btn btn-primary" type="submit">重新加载调度器</button></form>'''
    if status != 404 and (not payload.get("reload_required") or payload.get("retry_ids") or payload.get("unknown_ids")):
        retry_allowed = payload.get("retry_ids") or payload.get("unknown_ids")
        request_id = uuid.uuid4().hex if retry_allowed else data["request_id"]
        if isinstance(request_id, str) and len(request_id) == 32:
            recovery += f'''<form method="post" action="/task/delete/{h(task_id)}?back={h(quote(back_url, safe=''))}">
<input type="hidden" name="request_id" value="{h(request_id)}">
<button class="btn btn-primary" type="submit" onclick="return confirm('确认任务状态后继续删除？')">{'重试删除' if retry_allowed else '查询本次操作结果'}</button></form>'''
    body = message_card(payload["msg"], "error", strong=True, title="删除失败") + f'''
<section class="section" role="alert" data-task-delete-recovery><p>{h(payload['msg'])}</p>
{recovery}<a class="btn btn-gray" href="{h(back_url)}">返回任务列表</a></section>'''
    return layout("删除任务", "tasks", body), status


@bp.route("/task/toggle/<task_id>", methods=["POST"])
def task_toggle(task_id):
    from ...task_bulk import execute_toggle
    back_url = get_back_url("/tasks")
    try:
        payload, status = execute_toggle(
            task_id, request.form.to_dict(), load_tasks=load_tasks, mutate_tasks=mutate_tasks,
            run_task=run_task_now, stop_task=stop_task_now,
            cancel_retry=cancel_task_retry, reload_scheduler=reload_scheduler,
        )
    except Exception as exc:
        payload, status = {"msg": str(exc)}, 500
    if status == 200:
        return redirect(back_url)
    recovery = ""
    if payload.get("reload_required"):
        recovery = f'''<form method="post" action="/scheduler/reload?back={h(quote(back_url, safe=''))}">
<button class="btn btn-primary" type="submit">重新加载调度器</button></form>'''
    elif payload.get("retry_ids"):
        enabled = "1" if payload["action"] == "enable" else "0"
        recovery = f'''<form method="post" action="/task/toggle/{h(task_id)}?back={h(quote(back_url, safe=''))}">
<input type="hidden" name="enabled" value="{enabled}">
<input type="hidden" name="request_id" value="{uuid.uuid4().hex}">
<button class="btn btn-primary" type="submit">重试保存状态</button></form>'''
    body = f'''<section class="section" role="alert" data-task-toggle-recovery>
<p>{h(payload['msg'])}</p>{recovery}<a class="btn btn-gray" href="{h(back_url)}">返回任务列表</a></section>'''
    return layout("任务状态恢复", "tasks", body), status


@bp.route("/task/pin/<task_id>", methods=["POST"])
def task_pin(task_id):
    result = {}

    def pin(current_tasks):
        target = next((item for item in current_tasks if item.get("id") == task_id), None)
        if target is None:
            return False
        result["found"] = True

        pinned_count = sum(1 for task in current_tasks if task.get("pinned"))
        if not target.get("pinned") and pinned_count >= 5:
            result["limit_exceeded"] = True
            return False

        target["pinned"] = not target.get("pinned", False)
        target["updated_at"] = now_str()
        result["pinned"] = target["pinned"]
        return True

    mutate_tasks(pin)

    if not result.get("found"):
        abort(404)

    if result.get("limit_exceeded"):
        back_url = get_back_url("/tasks")
        body = f"""
{message_card("最多只能置顶 5 个任务，请先取消一个置顶任务", "error", strong=True, title="置顶失败")}
<div class="card">
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
"""
        return layout("置顶任务", "tasks", body), 400

    return redirect(get_back_url("/tasks"))


@bp.route("/task/collection/clear/<task_id>", methods=["POST"])
def task_collection_clear(task_id):
    from ...task_bulk import execute_clear_collection
    back_url = get_back_url("/collections")
    data = request.form.to_dict()
    data.setdefault("request_id", uuid.uuid4().hex)
    try:
        payload, status = execute_clear_collection(
            task_id, data, load_tasks=load_tasks, mutate_tasks=mutate_tasks,
            run_task=run_task_now, stop_task=stop_task_now,
            cancel_retry=cancel_task_retry, reload_scheduler=reload_scheduler,
        )
    except Exception as exc:
        payload, status = {"ok": False, "msg": str(exc)}, 500
    if status == 200:
        return redirect(back_url)
    retry_allowed = bool(payload.get("retry_ids") or payload.get("unknown_ids"))
    request_id = uuid.uuid4().hex if retry_allowed else data["request_id"]
    recovery = ""
    if status != 404 and isinstance(request_id, str) and len(request_id) == 32:
        expected_collection = payload.get("expected_collection", data.get("expected_collection"))
        expected = f'<input type="hidden" name="expected_collection" value="{h(expected_collection)}">' if isinstance(expected_collection, str) else ""
        recovery = f'''<form method="post" action="/task/collection/clear/{h(task_id)}?back={h(quote(back_url, safe=''))}">
<input type="hidden" name="request_id" value="{h(request_id)}">{expected}
<button class="btn btn-primary" type="submit" onclick="return confirm('确认任务合集归属后继续？')">{'重试移出合集' if retry_allowed else '查询本次操作结果'}</button></form>'''
    body = message_card(payload["msg"], "error", strong=True, title="移出失败") + f'''
<section class="section" role="alert" data-task-execution-result="{h(json.dumps(payload, ensure_ascii=False))}">
<p>{h(payload['msg'])}</p>{recovery}<a class="btn btn-gray" href="{h(back_url)}">返回并刷新后重试</a></section>'''
    return layout("移出任务", "collections", body), status


def _task_execution_response(action, task_id):
    from ...task_bulk import execute_single
    back_url = get_back_url("/tasks")
    data = request.form.to_dict()
    try:
        payload, status = execute_single(
            action, task_id, data, load_tasks=load_tasks, mutate_tasks=mutate_tasks,
            run_task=run_task_now, stop_task=stop_task_now,
            cancel_retry=cancel_task_retry, reload_scheduler=reload_scheduler,
        )
    except Exception as exc:
        payload, status = {"ok": False, "msg": str(exc)}, 500
    if status == 200:
        return redirect(url_for("tasks.log_view", task_id=task_id, back=back_url) if action == "run" else back_url)
    retry_allowed = bool(payload.get("retry_ids") or payload.get("unknown_ids"))
    request_id = uuid.uuid4().hex if retry_allowed else data.get("request_id", "")
    recovery = ""
    if status != 404 and isinstance(request_id, str) and len(request_id) == 32:
        label = ("重试运行" if action == "run" else "重试停止") if retry_allowed else "查询本次操作结果"
        recovery = f'''<form class="inline-form" method="post" action="/{action}/{h(task_id)}?back={h(quote(back_url, safe=''))}">
<input type="hidden" name="request_id" value="{h(request_id)}">
<button class="btn btn-primary" type="submit" onclick="return confirm('确认任务状态后继续？')">{label}</button></form>'''
    recovery_url = payload.get("recovery_url", "")
    if recovery_url.startswith("/scripts/rename-recovery/"):
        recovery += f'<a class="btn btn-orange" href="{h(recovery_url)}">恢复重命名</a>'
    title = "运行失败" if action == "run" else "停止失败"
    body = message_card(payload["msg"], "error", strong=True, title=title) + f'''
<section class="section" role="alert" data-task-execution-result="{h(json.dumps(payload, ensure_ascii=False))}">
<p>{h(payload['msg'])}</p>{recovery}
<a class="btn btn-gray" href="/log/{h(task_id)}">查看任务日志</a>
<a class="btn btn-gray" href="/history">查看任务历史</a>
<a class="btn btn-gray" href="{h(back_url)}">返回任务列表</a></section>'''
    return layout("任务执行恢复", "tasks", body), status


@bp.route("/run/<task_id>", methods=["POST"])
def run_task_route(task_id):
    if "request_id" in request.form:
        return _task_execution_response("run", task_id)
    ok, msg = run_task_now(task_id, source="manual")
    back_url = get_back_url("/tasks")

    if not ok:
        if msg.startswith("脚本重命名待恢复"):
            from ...script_rename import pending_script_rename
            from ...ui.components import page_header
            pending = pending_script_rename(task_id=task_id)
            if pending:
                body = page_header(
                    "任务未启动", help_html=h(msg),
                    actions_html=f'<a class="btn btn-orange" href="/scripts/rename-recovery/{h(pending["id"])}">恢复重命名</a>',
                )
                return layout("运行任务", "tasks", body), 409
        if msg == "任务不存在":
            status_code = 404
        elif msg.startswith("任务启动失败"):
            status_code = 500
        else:
            status_code = 400
        body = f"""
{message_card(msg, "error", strong=True, title="运行失败")}
<div class="card">
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
"""
        return layout("运行任务", "tasks", body), status_code

    return redirect(url_for("tasks.log_view", task_id=task_id, back=back_url))


@bp.route("/stop/<task_id>", methods=["POST"])
def stop_task_route(task_id):
    if "request_id" in request.form:
        return _task_execution_response("stop", task_id)
    if not any(task.get("id") == task_id for task in load_tasks()):
        abort(404)

    ok, msg = stop_task_now(task_id)
    back_url = get_back_url("/tasks")

    if not ok and msg != "任务未运行":
        title = "任务已结束" if msg.startswith("任务已结束") else "停止失败"
        body = f"""
{message_card(msg, "error", strong=True, title=title)}
<div class="card">
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
"""
        return layout("停止任务", "tasks", body), 409

    return redirect(back_url)
