import shutil
import uuid
from contextlib import ExitStack

from flask import request, redirect, abort, Response

from . import bp
from .helpers import (
    script_safe_path,
    script_rel_path,
    script_url,
    view_url,
    rename_url,
    script_debug_url,
)

from ...paths import SCRIPT_DIR
from ...directory_jobs import directory_job, directory_job_operation
from ...data_access import PanelStorageBusy
from ...models import TASKS_MUTATION_LOCK
from ...task_file_coordination import TASK_FILES_LOCK
from ...script_files import (
    ScriptFileConflict, ScriptFileWriteError, script_file_target,
    script_file_snapshot, publish_script_file,
)
from ...utils import h
from ...ui.components import page_header_card
from ...script_rename import (
    validate_new_name, rename_preview, validate_rename_updates, begin_script_rename,
    get_script_rename, resume_script_rename, pending_script_rename,
)
from ...ui.layout import layout


def _file_message(message, success=False):
    if not message:
        return ""
    color = "#18a058" if success else "#dc2626"
    return f'<section class="section fls-form-section" role="alert"><p class="help" style="color:{color};font-weight:800;">{h(message)}</p></section>'


def _file_error(message, content=None, status=500):
    draft = "" if content is None else f'<section class="section fls-form-section"><h2 class="section-title">未保存的草稿</h2><textarea readonly aria-label="未保存的草稿">{h(content)}</textarea></section>'
    return layout("文件操作失败", "pull", _file_message(message) + draft + '<a class="btn btn-gray" href="/pull">返回脚本</a>'), status


def _guard_file_mutation(target):
    pending = pending_script_rename(path=target)
    if pending:
        raise ScriptFileConflict(f"路径存在待恢复重命名，请先访问 /scripts/rename-recovery/{pending['id']} 完成恢复")


@bp.route("/pull/new", methods=["GET", "POST"])
@directory_job_operation
def scripts_new():
    current_rel = request.args.get("p", "").strip().strip("/")
    msg = ""
    item_type = "file"
    name = ""
    content = ""
    status = 200

    if request.method == "POST":
        current_rel = request.form.get("current_rel", "").strip()
        item_type = request.form.get("item_type", "file")
        name = request.form.get("name", "").strip()
        content = request.form.get("content", "")

        try:
            if item_type not in ("file", "dir"):
                raise ValueError("项目类型无效")
            if not name or "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
                raise ValueError("名称非法")

            rel = f"{current_rel}/{name}" if current_rel else name
            target = script_file_target(rel)
            if not target.parent.is_dir():
                raise ValueError("当前目录不存在，请重新选择目录")
            with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
                _guard_file_mutation(target)
                if target.exists():
                    # Replaying the same create request must never replace another file.
                    if item_type == "file" and script_file_snapshot(target, rel)[0] == content:
                        return redirect(view_url(rel))
                    if item_type == "dir" and target.is_dir():
                        return redirect(script_url(rel))
                    raise FileExistsError("目标已存在，未覆盖，请更换名称或打开已有文件")
                if item_type == "dir":
                    target.mkdir()
                else:
                    publish_script_file(target, rel, content)

            return redirect(script_url(current_rel))
        except PanelStorageBusy:
            raise
        except Exception as e:
            msg = f"新建失败：{e}。下一步：检查名称、目标路径和文件权限后重试"
            status = 409 if isinstance(e, (FileExistsError, ScriptFileConflict)) else (400 if isinstance(e, ValueError) else 500)
            if isinstance(e, ScriptFileWriteError) and e.saved:
                msg = f"文件已创建，但清理临时文件失败：{e}。请打开已有文件确认，重复提交不会覆盖它。"
                status = 500

    file_selected = " selected" if item_type != "dir" else ""
    dir_selected = " selected" if item_type == "dir" else ""

    header = page_header_card(
        "新建文件 / 文件夹",
        f"当前目录：{h(current_rel or 'scripts 根目录')}",
    )

    message_html = _file_message(msg)

    body = f"""
<form method="post" action="/pull/new">
<input type="hidden" name="current_rel" value="{h(current_rel)}">
{header}
<section class="section fls-form-section">
    <div class="form-grid">
        <div class="form-item">
            <label>类型</label>
            <select name="item_type" aria-label="新建项目类型">
                <option value="file"{file_selected}>文件</option>
                <option value="dir"{dir_selected}>文件夹</option>
            </select>
        </div>
        <div class="form-item">
            <label>名称</label>
            <input name="name" required value="{h(name)}" placeholder="test.py 或 demo" aria-label="新建项目名称">
        </div>
    </div>
</section>
<section class="section fls-form-section">
    <h2 class="section-title">文件内容</h2>
    <textarea
        name="content"
        aria-label="新建脚本内容"
        class="fls-code-editor"
        data-filename=""
    >{h(content)}</textarea>
</section>
<section class="section fls-form-section fls-save-section">
    <button class="btn btn-primary" type="submit">保存新建脚本</button>
    <a class="btn btn-gray" href="{h(script_url(current_rel))}">返回</a>
</section>
</form>
{message_html}
"""
    return layout("新建脚本", "pull", body), status


@bp.route("/scripts/view", methods=["GET", "POST"])
@directory_job_operation
def scripts_view():
    raw = request.args.get("path", "").strip()
    posted_content = request.form.get("content", "") if request.method == "POST" else None
    msg = ""
    status = 200
    revision = ""
    current_content = ""
    saved = False
    try:
        target = script_file_target(raw)
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            if posted_content is None:
                pending = pending_script_rename(path=target)
                if pending:
                    return redirect(f"/scripts/rename-recovery/{pending['id']}")
            if posted_content is not None:
                _guard_file_mutation(target)
            current_content, revision, metadata = script_file_snapshot(target, raw)
            if posted_content is not None:
                if request.form.get("file_revision") != revision:
                    if posted_content != current_content:
                        raise ScriptFileConflict("文件已变更或编辑状态已失效，未保存。请对照当前内容合并后重试。")
                else:
                    publish_script_file(target, raw, posted_content, revision, metadata)
                saved = True
                current_content, revision, metadata = script_file_snapshot(target, raw)
                msg = "保存成功"
    except PanelStorageBusy:
        raise
    except ScriptFileConflict as e:
        status = 409
        msg = str(e)
        try:
            current_content, revision, _metadata = script_file_snapshot(target, raw)
        except PanelStorageBusy:
            raise
        except Exception as read_error:
            return _file_error(f"{msg}；当前文件读取失败：{read_error}", posted_content)
    except ScriptFileWriteError as e:
        saved = e.saved
        status = 500
        revision = "" if saved else revision
        msg = f"{'文件已保存，但清理失败' if saved else '保存失败'}：{e}。草稿已保留，请重新读取确认后重试。"
    except FileNotFoundError:
        if saved:
            return _file_error("文件已保存，但重新读取时文件不存在。请检查文件是否被移动或删除，草稿已保留。", posted_content, 500)
        return _file_error("文件不存在或已被重命名，未保存。请从脚本列表重新打开文件后合并草稿。", posted_content, 404)
    except ValueError as e:
        if saved:
            return _file_error(f"文件已保存，但重新读取失败：{e}。请检查当前文件，草稿已保留。", posted_content, 500)
        return _file_error(f"文件不可编辑：{e}。请检查路径和 UTF-8 编码。", posted_content, 400)
    except Exception as e:
        if not revision:
            return _file_error(f"文件读取失败：{e}。未保存，请检查文件权限后重新读取。", posted_content)
        status = 500
        if saved:
            revision = ""
        msg = f"{'文件已保存，但重新读取失败' if saved else '保存失败'}：{e}。草稿已保留，请重新读取确认后重试。"

    content = posted_content if posted_content is not None else current_content
    rel = script_rel_path(target)
    parent_rel = (
        str(target.parent.relative_to(SCRIPT_DIR))
        if target.parent != SCRIPT_DIR
        else ""
    )
    header = page_header_card(
        f"查看 / 编辑文件：{target.name}",
        f"路径：{h(target)}",
        f"""
    <button class="btn btn-primary" type="submit">保存脚本文件</button>
    <a class="btn btn-primary" href="{h(script_debug_url(rel))}">调试运行</a>
    <a class="btn btn-orange" href="{h(rename_url(rel))}">重命名脚本</a>
    <a class="btn btn-blue" href="{h(view_url(rel))}">重新读取文件</a>
    <a class="btn btn-gray" href="{h(script_url(parent_rel))}">返回</a>
        """,
    )

    message_html = _file_message(msg, success=status == 200)
    conflict_html = f'<section class="section fls-form-section"><h2 class="section-title">当前文件内容</h2><textarea readonly aria-label="当前文件内容">{h(current_content)}</textarea></section>' if status == 409 else ""

    body = f"""
<form method="post" action="{h(view_url(raw))}">
<input type="hidden" name="file_revision" value="{h(revision)}">
{header}
{message_html}
{conflict_html}
<section class="section fls-form-section">
    <textarea
        name="content"
        aria-label="脚本文件内容"
        class="fls-code-editor"
        data-filename="{h(target.name)}"
        style="min-height:680px;"
    >{h(content)}</textarea>
</section>
</form>
"""
    return layout("查看 / 编辑文件", "pull", body), status


@bp.route("/scripts/rename", methods=["GET", "POST"])
@directory_job_operation
def scripts_rename():
    raw = request.args.get("path", "").strip()
    msg = ""
    status = 200
    new_name = request.form.get("new_name", "").strip() if request.method == "POST" else ""
    operation_id = request.form.get("operation_id", "") if request.method == "POST" else uuid.uuid4().hex
    references = []
    revision = ""
    confirmed = False
    try:
        target = script_file_target(raw)
        new_name = new_name or target.name
        if len(operation_id) != 32 or uuid.UUID(hex=operation_id).hex != operation_id:
            raise ValueError("重命名提交标识无效，请重新打开页面")
        validate_new_name(new_name)
        destination = script_file_target(str(target.with_name(new_name).relative_to(SCRIPT_DIR.resolve())))
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            existing = get_script_rename(operation_id)
            if existing:
                if existing["source"] != raw or existing["destination"] != str(destination.relative_to(SCRIPT_DIR.resolve())):
                    return _rename_recovery_page(existing, "提交已用于其他重命名，不允许更改重试参数。未执行新的更改。", 409)
                return redirect(f"/scripts/rename-recovery/{operation_id}")
            _guard_file_mutation(target)
            _guard_file_mutation(destination)
            references, revision = rename_preview(target, destination)
            if request.method == "POST":
                if destination == target:
                    raise ValueError("新名称与当前名称相同，未更改文件或任务")
                if destination.exists():
                    raise ScriptFileConflict("目标已存在，未覆盖文件或任务")
                if request.form.get("rename_revision") != revision:
                    raise ScriptFileConflict("文件或任务引用已变更，请检查当前引用后重新确认")
                if request.form.get("confirm") == "1":
                    updates = validate_rename_updates(references, request.form, target, destination)
                    begin_script_rename(target, destination, operation_id, updates)
                    parent_rel = str(destination.parent.relative_to(SCRIPT_DIR.resolve()))
                    return redirect(script_url("" if parent_rel == "." else parent_rel))
                if request.form.get("confirm") not in (None, "", "0"):
                    raise ValueError("确认选择无效，未更改文件或任务")
                confirmed = True
    except PanelStorageBusy:
        raise
    except FileNotFoundError:
        return _file_error("源文件不存在，请从脚本列表重新打开或完成待恢复重命名。", new_name, 404)
    except Exception as e:
        try:
            record = get_script_rename(operation_id)
        except PanelStorageBusy:
            raise
        except Exception:
            record = None
        if record:
            return _rename_recovery_page(record, f"改名未完成：{e}。请检查实际文件与任务状态后继续或回滚。", 503)
        status = 409 if isinstance(e, ScriptFileConflict) else (400 if isinstance(e, ValueError) else 500)
        msg = f"改名失败：{e}。未更改文件或任务，请检查后重试。"
        if not revision:
            return _file_error(msg, new_name, status)
        confirmed = True
    reference_html = ""
    for reference in references:
        task_id = reference["id"]
        command = request.form.get(f"command_{task_id}", reference["after"]["command"]) if request.method == "POST" and request.form.get("confirm") == "1" else reference["after"]["command"]
        config = request.form.get(f"config_{task_id}", reference["after"]["config_path"]) if request.method == "POST" and request.form.get("confirm") == "1" else reference["after"]["config_path"]
        reference_html += f'''
<section class="section fls-form-section">
    <h2 class="section-title">任务：{h(reference['name'])}</h2>
    <input type="hidden" name="task_id" value="{h(task_id)}">
    <label>当前命令</label><pre>{h(reference['before']['command'])}</pre>
    <label for="rename-command-{h(task_id)}">改名后的命令</label>
    <textarea id="rename-command-{h(task_id)}" name="command_{h(task_id)}" aria-label="改名后的任务命令">{h(command)}</textarea>
    <label for="rename-config-{h(task_id)}">改名后的配置路径</label>
    <input id="rename-config-{h(task_id)}" name="config_{h(task_id)}" value="{h(config)}" aria-label="改名后的任务配置路径">
    {'<p class="help">命令仍可能引用旧路径，请确认并修改后提交。</p>' if reference['manual'] else ''}
</section>'''
    header = page_header_card("确认重命名和任务引用" if confirmed else "改名", f"当前路径：{h(target)}")
    body = f"""
<form method="post" action="{h(rename_url(raw))}">
<input type="hidden" name="operation_id" value="{h(operation_id)}">
<input type="hidden" name="rename_revision" value="{h(revision)}">
{header}
{_file_message(msg)}
<section class="section fls-form-section">
    <div class="form-item">
        <label>新名称</label>
        <input name="new_name" required value="{h(new_name)}" aria-label="脚本新名称">
    </div>
</section>
{reference_html if confirmed else ''}
<section class="section fls-form-section fls-save-section">
    <button class="btn btn-primary" type="submit" name="confirm" value="{'1' if confirmed else '0'}">{'确认重命名并更新任务' if confirmed else '检查任务引用'}</button>
    <a class="btn btn-gray" href="/pull">返回</a>
</section>
</form>
"""
    return layout("改名", "pull", body), status


def _rename_recovery_page(record, message="", status=200):
    operation_id = record["id"]
    rows = "".join(f'<li>{h(update["id"])}：<pre>{h(json_text(update))}</pre></li>' for update in record["updates"])
    try:
        from ...models import load_tasks
        source = script_file_target(record["source"])
        destination = script_file_target(record["destination"])
        tasks = {task["id"]: task for task in load_tasks()}
        actual = f"原路径：{'存在' if source.exists() else '不存在'}；目标路径：{'存在' if destination.exists() else '不存在'}"
        for update in record["updates"]:
            task = tasks.get(update["id"])
            values = {key: task.get(key, "") for key in ("command", "config_path")} if task else None
            state = "已更新" if values == update["after"] else ("仍为原引用" if values == update["before"] else "已删除或发生冲突")
            actual += f"；任务 {update['id']}：{state}"
    except PanelStorageBusy:
        raise
    except Exception as e:
        actual = f"实际文件/任务状态读取失败：{e}"
    actions = ""
    if record["status"] not in ("succeeded", "rolled_back"):
        actions = f'''<form method="post" action="/scripts/rename-recovery/{h(operation_id)}">
<section class="section fls-form-section"><div class="action-row">
<button class="btn btn-primary" type="submit" name="action" value="resume">继续完成重命名</button>
<button class="btn btn-orange" type="submit" name="action" value="rollback">回滚文件与任务引用</button>
</div></section></form>'''
    body = page_header_card("重命名恢复", f"{h(record['source'])} → {h(record['destination'])}")
    body += _file_message(message or record.get("error", ""))
    body += f'<section class="section fls-form-section"><p class="help">记录状态：{h(record["status"])}</p><p class="help" data-rename-state>{h(actual)}</p><ul>{rows}</ul></section>'
    body += actions + '<a class="btn btn-gray" href="/pull">返回脚本</a>'
    return layout("重命名恢复", "pull", body), status


def json_text(update):
    import json
    return json.dumps({"before": update["before"], "after": update["after"]}, ensure_ascii=False, indent=2)


@bp.route("/scripts/rename-recovery/<operation_id>", methods=["GET", "POST"])
@directory_job_operation
def scripts_rename_recovery(operation_id):
    try:
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            record = get_script_rename(operation_id)
            if not record:
                abort(404)
            if request.method == "POST":
                action = request.form.get("action")
                if action not in ("resume", "rollback"):
                    return _rename_recovery_page(record, "恢复选择无效，未更改文件或任务。", 400)
                try:
                    resume_script_rename(record, rollback=action == "rollback")
                except PanelStorageBusy:
                    raise
                except Exception as e:
                    return _rename_recovery_page(record, f"恢复未完成：{e}。请检查当前文件和任务后重试。", 409 if isinstance(e, ValueError) else 503)
                return redirect("/pull")
    except PanelStorageBusy:
        raise
    except Exception as e:
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            raise
        return _file_error(f"重命名恢复记录读取失败：{e}")
    return _rename_recovery_page(record)


@bp.route("/scripts/download/<path:rel_path>")
def scripts_download(rel_path):
    resources = ExitStack()
    try:
        resources.enter_context(directory_job())
        target = script_safe_path(rel_path)
        if not target.exists() or target.is_dir():
            abort(404)
        source = resources.enter_context(open(target, "rb"))

        def generate():
            try:
                while True:
                    data = source.read(1024 * 1024)
                    if not data:
                        break
                    yield data
            finally:
                resources.close()

        response = Response(
            generate(),
            mimetype="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
        )
        # WSGI may close the response without ever starting its generator.
        response.call_on_close(resources.close)
        return response
    except BaseException:
        resources.close()
        raise


@bp.route("/scripts/delete/<path:rel_path>", methods=["POST"])
@directory_job_operation
def scripts_delete(rel_path):
    target = script_safe_path(rel_path)

    if not target.exists() or target.resolve() == SCRIPT_DIR.resolve():
        abort(404)

    parent = target.parent

    try:
        with TASKS_MUTATION_LOCK, TASK_FILES_LOCK:
            _guard_file_mutation(target)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
    except PanelStorageBusy:
        raise
    except Exception as e:
        return _file_error(f"删除失败：{e}。请检查文件状态后重试。", status=409 if isinstance(e, ValueError) else 500)

    parent_rel = "" if parent == SCRIPT_DIR else str(parent.relative_to(SCRIPT_DIR))
    return redirect(script_url(parent_rel))
