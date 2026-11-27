import hashlib
import json
import os
import stat
import uuid
from urllib.parse import quote

from flask import request

from . import bp
from .helpers import task_config_safe_path

from ...models import get_task, TASKS_MUTATION_LOCK
from ...storage import storage_transaction
from ...data_access import PanelStorageBusy
from ...directory_jobs import directory_job_operation
from ...task_file_coordination import TASK_FILES_LOCK
from ...utils import h, get_back_url
from ...ui.layout import layout
from ...ui.components import message_card, page_header
from ...task_runner import is_running


MAX_CONFIG_BYTES = 1024 * 1024


class TaskConfigLimitError(ValueError):
    pass


class TaskConfigConflict(ValueError):
    pass


class TaskConfigSaveError(RuntimeError):
    def __init__(self, message, saved=False):
        super().__init__(message)
        self.saved = saved


def _unsaved_config_draft(content):
    if content is None:
        return ""
    return f'''
<section class="section fls-form-section">
    <h2 class="section-title">未保存的草稿</h2>
    <textarea readonly aria-label="未保存的草稿" style="min-height:220px;">{h(content)}</textarea>
</section>'''


def _read_config_snapshot(target, config_path):
    metadata = None
    data = b""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(target, flags)
    except FileNotFoundError:
        pass
    else:
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("配置路径不是普通文件")
            if metadata.st_size > MAX_CONFIG_BYTES:
                raise TaskConfigLimitError("配置文件超过 1 MiB 编辑限制")
            fp = os.fdopen(fd, "rb")
        except Exception:
            os.close(fd)
            raise
        with fp:
            data = fp.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise TaskConfigLimitError("配置文件超过 1 MiB 编辑限制")
    signature = [config_path, str(target), bool(metadata)]
    if metadata:
        signature.extend([
            metadata.st_ino, metadata.st_mtime_ns, metadata.st_mode, metadata.st_uid, metadata.st_gid,
        ])
    revision = hashlib.sha256(json.dumps(signature).encode("utf-8") + b"\x00" + data).hexdigest()
    return data.decode("utf-8"), revision, metadata


def _save_config_atomic(target, config_path, content, revision, metadata):
    if len(content) > MAX_CONFIG_BYTES or len(content.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise TaskConfigLimitError("配置内容超过 1 MiB 编辑限制")
    if task_config_safe_path(config_path) != target:
        raise TaskConfigConflict("配置文件路径已变更，请重新检查当前配置后保存")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target.with_name(f".fls-config-{uuid.uuid4().hex}.tmp")
    created = False
    published = False
    failure = None
    try:
        fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        created = True
        try:
            fp = os.fdopen(fd, "wb")
        except Exception:
            os.close(fd)
            raise
        with fp:
            fp.write(content.encode("utf-8"))
            fp.flush()
            if metadata:
                current = os.fstat(fp.fileno())
                if hasattr(os, "fchown") and (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
                    os.fchown(fp.fileno(), metadata.st_uid, metadata.st_gid)
                temp_file.chmod(stat.S_IMODE(metadata.st_mode))
            os.fsync(fp.fileno())
        if task_config_safe_path(config_path) != target or _read_config_snapshot(target, config_path)[1] != revision:
            raise TaskConfigConflict("配置文件在保存期间已变更，未覆盖新内容，请合并后重试")
        os.replace(temp_file, target)
        published = True
    except Exception as e:
        failure = e
        raise
    finally:
        if created:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError as e:
                detail = f"清理临时配置失败：{e}"
                if failure:
                    detail = f"{failure}；{detail}"
                raise TaskConfigSaveError(detail, saved=published) from e


@bp.route("/task/config/<task_id>", methods=["GET", "POST"])
@directory_job_operation
def task_config_edit(task_id):
    back_url = get_back_url("/tasks")
    submitted = request.form.get("content", "") if request.method == "POST" else None
    try:
        task = get_task(task_id)
    except PanelStorageBusy:
        raise
    except Exception as e:
        body = page_header(
            "任务读取失败", help_html=f"{h(e)}。配置未保存，请修复任务文件或恢复备份后重试。",
            actions_html=f'<a class="btn btn-gray" href="{h(back_url)}">返回任务</a>',
        ) + _unsaved_config_draft(submitted)
        return layout("任务配置文件", "tasks", body), 500

    if not task:
        body = page_header(
            "任务不存在", help_html="任务已删除或 ID 无效，配置未保存。",
            actions_html=f'<a class="btn btn-gray" href="{h(back_url)}">返回任务</a>',
        ) + _unsaved_config_draft(submitted)
        return layout("任务配置文件", "tasks", body), 404

    back_param = h(quote(back_url, safe="/"))
    config_path = str(task.get("config_path") or "").strip()
    running = is_running(task_id)

    if not config_path:
        body = f"""
<section class="section fls-form-section">
    <h2 class="section-title">任务配置文件</h2>
    <div class="help">该任务没有配置 config_path。</div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}?back={back_param}">编辑任务</a>
</section>
{_unsaved_config_draft(submitted)}
"""
        return layout("任务配置文件", "tasks", body), 400 if submitted is not None else 200

    try:
        target = task_config_safe_path(config_path)
    except PanelStorageBusy:
        raise
    except Exception as e:
        body = f"""
<section class="section fls-form-section">
    <h2 class="section-title">配置文件路径非法</h2>
    <div class="help" style="color:#dc2626;">{h(e)}</div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}?back={back_param}">编辑任务</a>
</section>
{_unsaved_config_draft(submitted)}
"""
        return layout("配置文件路径非法", "tasks", body), 400

    msg = ""
    status_code = 200
    current_content = ""
    revision = ""
    exists = False
    readable = False
    saved = False
    # Match task mutations' lock order so a path edit cannot race publication.
    with TASKS_MUTATION_LOCK, TASK_FILES_LOCK, storage_transaction():
        try:
            latest_task = get_task(task_id)
            if not latest_task:
                body = page_header(
                    "任务不存在", help_html="任务已删除，配置未保存。",
                    actions_html=f'<a class="btn btn-gray" href="{h(back_url)}">返回任务</a>',
                ) + _unsaved_config_draft(submitted)
                return layout("任务配置文件", "tasks", body), 404
            if str(latest_task.get("config_path") or "").strip() != config_path:
                raise TaskConfigConflict("任务配置路径已变更，未保存。请刷新页面后重新检查配置。")
            target = task_config_safe_path(config_path)
            from ...script_rename import pending_script_rename
            pending = pending_script_rename(task_id=task_id, path=target)
            if pending:
                recovery_url = f"/scripts/rename-recovery/{pending['id']}"
                body = page_header(
                    "脚本重命名待恢复", help_html="配置未保存，请先完成或回滚重命名，再重新打开配置文件。",
                    actions_html=f'<a class="btn btn-orange" href="{h(recovery_url)}">恢复重命名</a>',
                ) + _unsaved_config_draft(submitted)
                return layout("任务配置文件", "tasks", body), 409
            current_content, revision, metadata = _read_config_snapshot(target, config_path)
            exists = metadata is not None
            readable = True
            if submitted is not None:
                if request.form.get("config_revision") != revision:
                    raise TaskConfigConflict("配置文件已变更或编辑状态已失效，未保存。请对照当前内容合并草稿后重试。")
                _save_config_atomic(target, config_path, submitted, revision, metadata)
                saved = True
                current_content, revision, metadata = _read_config_snapshot(target, config_path)
                exists = True
                msg = "保存成功"
        except PanelStorageBusy:
            raise
        except TaskConfigConflict as e:
            status_code = 409
            msg = str(e)
            if readable:
                try:
                    current_content, revision, _metadata = _read_config_snapshot(target, config_path)
                except PanelStorageBusy:
                    raise
                except Exception as read_error:
                    status_code = 500
                    revision = ""
                    readable = False
                    msg += f" 当前配置读取失败：{read_error}。草稿已保留，请检查文件状态后重试。"
        except TaskConfigLimitError as e:
            status_code = 413
            msg = f"配置文件不可编辑：{e}。草稿已保留，配置未保存。"
        except ValueError as e:
            status_code = 400
            msg = f"配置文件不可编辑：{e}。草稿已保留，请检查配置路径和编码后重试。"
        except TaskConfigSaveError as e:
            status_code = 500
            saved = e.saved
            action = "配置已保存，但清理失败" if saved else "保存失败"
            if saved:
                revision = ""
                readable = False
            msg = f"{action}：{e}。草稿已保留，请检查临时文件权限后重试。"
        except Exception as e:
            status_code = 500
            action = "配置已保存，但重新读取失败" if saved else ("保存失败" if submitted is not None else "读取失败")
            if saved:
                revision = ""
                readable = False
            msg = f"{action}：{e}。草稿已保留，请检查文件权限和磁盘空间后重试。"

    if saved and status_code == 200:
        content = current_content
    elif submitted is not None:
        content = submitted
    elif exists or not readable:
        content = current_content
    else:
        content = "# 配置文件不存在，可在这里新建。\n# 示例：\n# key: value\n"

    conflict_html = ""
    if status_code == 409 and readable:
        conflict_html = f'''
<section class="section fls-form-section">
    <details open>
        <summary>当前配置内容</summary>
        <textarea readonly aria-label="当前配置内容" style="min-height:220px;">{h(current_content)}</textarea>
    </details>
</section>'''

    header = page_header(
        "编辑任务配置",
        help_html=f"编辑任务“{h(task.get('name') or task_id)}”的配置文件内容。",
        actions_html=(
            f'<a class="btn btn-gray" href="{h(back_url)}">返回任务</a>'
            f'<a class="btn btn-blue" href="/task/config/{h(task_id)}?back={back_param}">重新读取配置</a>'
            f'<a class="btn btn-orange" href="/task/edit/{h(task_id)}?back={back_param}">编辑任务</a>'
        ),
    )

    if running:
        run_action = f'/stop/{h(task_id)}?back={back_param}'
        run_label = "停止任务"
        run_class = "btn-orange"
        run_confirm = " onclick=\"return confirm('确定停止该任务吗？')\""
    else:
        run_action = f'/run/{h(task_id)}?back={back_param}'
        run_label = "立即运行任务"
        run_class = "btn-blue"
        run_confirm = ""

    body = f"""
{header}
<form method="post" action="/task/config/{h(task_id)}?back={back_param}">
<input type="hidden" name="config_revision" value="{h(revision)}">
<section class="section fls-form-section">
    <h2 class="section-title">编辑任务配置：{h(task.get('name') or task_id)}</h2>
    <div class="help">
        配置路径：<code>{h(config_path)}</code><br>
        实际路径：<code>{h(target)}</code><br>
        状态：{"读取失败，未确认文件状态" if not readable else ("已存在" if exists else "文件不存在，保存后会自动创建")}
    </div>
    <br>
    <!-- Legacy action marker: formaction="/run/{h(task_id)}?back={back_param}" -->
    <button class="btn {run_class}" type="submit" name="request_id" value="{uuid.uuid4().hex}" formaction="{run_action}" formmethod="post"{run_confirm}>{run_label}</button>
    <a class="btn btn-orange" href="/task/edit/{h(task_id)}?back={back_param}">编辑任务</a>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</section>

{message_card(msg, "success" if msg.startswith("保存成功") else "error", strong=True)}

{conflict_html}

<section class="section fls-form-section">
    <h2 class="section-title">配置内容</h2>
    <textarea name="content" style="min-height:680px;" aria-label="任务配置文件内容">{h(content)}</textarea>
</section>

<section class="section fls-form-section fls-save-section">
    <button class="btn btn-primary" type="submit">保存任务配置</button>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</section>
</form>
"""
    return layout("编辑任务配置", "tasks", body), status_code
