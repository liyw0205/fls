from ._common import *
from contextlib import nullcontext
from ... import models
from ...storage import storage_transaction
from ...ui.components import page_header_card


class BackupRestoreWriteError(RuntimeError):
    def __init__(self, message, live_state_unchanged=True):
        super().__init__(message)
        self.live_state_unchanged = live_state_unchanged


def _remove_restore_path(path):
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _stage_restore_directory(source, target, keep_backups=False):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(
        prefix=".{}-restore-".format(target.name),
        dir=str(target.parent),
    ))
    staged = workspace / "replacement"

    try:
        shutil.copytree(source, staged)
        if keep_backups:
            current_backup_dir = DATA_DIR / "backups"
            if current_backup_dir.exists():
                shutil.copytree(
                    current_backup_dir,
                    staged / "backups",
                    dirs_exist_ok=True,
                )
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise

    return {
        "target": target,
        "workspace": workspace,
        "staged": staged,
        "previous": None,
        "installed": False,
        "preserve_workspace": False,
    }


def _commit_restore_directories(entries):
    try:
        for entry in entries:
            target = entry["target"]
            if target.exists() or target.is_symlink():
                previous = entry["workspace"] / "previous"
                os.replace(target, previous)
                entry["previous"] = previous

            os.replace(entry["staged"], target)
            entry["installed"] = True
    except Exception as commit_error:
        rollback_errors = []
        for entry in reversed(entries):
            target = entry["target"]
            previous = entry.get("previous")
            try:
                if previous is not None and (previous.exists() or previous.is_symlink()):
                    if target.exists() or target.is_symlink():
                        _remove_restore_path(target)
                    os.replace(previous, target)
                elif entry.get("installed"):
                    _remove_restore_path(target)
            except Exception as rollback_error:
                entry["preserve_workspace"] = True
                rollback_errors.append(
                    "{}: {} (恢复副本保留在 {})".format(
                        target,
                        rollback_error,
                        entry["workspace"],
                    )
                )

        detail = str(commit_error)
        if rollback_errors:
            detail += "；回滚失败：" + "；".join(rollback_errors)
        raise BackupRestoreWriteError(
            detail,
            live_state_unchanged=not rollback_errors,
        ) from commit_error


@bp.route("/backup/download/<path:filename>")
def backup_download(filename):
    target = backup_safe_file(filename)

    if not target.exists() or not target.is_file():
        abort(404)

    def generate():
        with open(target, "rb") as f:
            while True:
                data = f.read(1024 * 1024)

                if not data:
                    break

                yield data

    return Response(
        generate(),
        mimetype="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )

@bp.route("/backup/export")
def backup_export():
    """
    兼容旧入口：
    默认创建 data + scripts 备份并跳转回备份页。
    """
    job_id = start_backup_job(["data", "scripts"])
    return redirect(url_for("backup.backup_page", job=job_id))

@bp.route("/backup/import", methods=["POST"])
def backup_import():
    if (
        request.content_length is not None
        and request.content_length > MAX_BACKUP_UPLOAD_BYTES + MIB
    ):
        return "备份导入失败：请求超过上传上限。请调低文件大小或调整服务端备份限制。", 413

    f = request.files.get("file")

    if not f:
        return "未上传备份文件。下一步：选择 .tar.gz、.tgz、.tar 或 .zip 文件后重试", 400

    restore_items = parse_items_from_form("restore_items")

    if not restore_items:
        return "请至少选择一个恢复内容。下一步：勾选配置或脚本后重试", 400

    restore_deps = request.form.get("restore_deps") == "1"
    tmp_dir = tempfile.mkdtemp()
    restore_reserved = False

    try:
        original = os.path.basename(f.filename or "backup")
        backup_file = Path(tmp_dir) / original
        save_backup_upload(f, backup_file)

        extract_dir = Path(tmp_dir) / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        extract_archive(backup_file, extract_dir)

        root = find_backup_root(extract_dir)

        if not root:
            raise RuntimeError("未在备份中找到 data 或 scripts 目录")

        dep_file = find_dependency_file(extract_dir, root)

        data_src = root / "data"
        scripts_src = root / "scripts"

        restore_sources = []

        if "data" in restore_items:
            if not data_src.exists():
                raise RuntimeError("备份中没有 data 目录，无法恢复配置")
            restore_sources.append((data_src, DATA_DIR, True, "配置 data"))

        if "scripts" in restore_items:
            if not scripts_src.exists():
                raise RuntimeError("备份中没有 scripts 目录，无法恢复脚本")
            restore_sources.append((scripts_src, SCRIPT_DIR, False, "脚本 scripts"))

        if not try_begin_restore_activity():
            return "备份正在创建或恢复，请等待当前操作完成后重试。", 409
        restore_reserved = True

        staged = []
        try:
            for source, target, keep_backups, _label in restore_sources:
                staged.append(
                    _stage_restore_directory(source, target, keep_backups)
                )
        except Exception as e:
            for entry in staged:
                shutil.rmtree(entry["workspace"], ignore_errors=True)
            raise BackupRestoreWriteError(str(e)) from e

        scheduler_error = ""
        restore_lock = (
            models.TASKS_MUTATION_LOCK
            if "data" in restore_items
            else nullcontext()
        )
        storage_guard = (
            storage_transaction()
            if "data" in restore_items
            else nullcontext()
        )
        try:
            with restore_lock:
                with storage_guard:
                    _commit_restore_directories(staged)
                    try:
                        reload_scheduler()
                    except Exception as e:
                        scheduler_error = str(e)
        finally:
            for entry in staged:
                if not entry["preserve_workspace"]:
                    shutil.rmtree(entry["workspace"], ignore_errors=True)

        end_restore_activity()
        restore_reserved = False

        restored = [label for _source, _target, _keep, label in restore_sources]

        deps_msg = ""
        deps_log = ""

        if restore_deps:
            if dep_file:
                ok, deps_log = install_dependencies(dep_file)
                deps_msg = "依赖恢复成功" if ok else "依赖恢复失败，请查看日志"
            else:
                deps_msg = "已勾选恢复依赖，但备份中没有 dependencies.txt / requirements.txt"

        if scheduler_error:
            return (
                "备份已恢复，但调度器重载失败：{}。请检查任务列表和运行状态后再操作。".format(
                    h(scheduler_error)
                ),
                500,
            )

        body = page_header_card(
            "备份导入完成",
            help_html=f"""
        已恢复：{h("、".join(restored) or "-")}<br>
        依赖恢复：{h(deps_msg or "未恢复依赖")}<br>
        日志：{h(deps_log or "-")}
""",
            actions_html="""
<a class="btn btn-primary" href="/backup">返回备份恢复</a>
<a class="btn btn-gray" href="/logs">查看日志</a>
""",
        )
        return layout("备份导入完成", "backup", body)

    except BackupLimitExceeded as e:
        return f"备份导入失败：{h(e)}。请调低文件大小或调整服务端备份限制。", 413
    except BackupRestoreWriteError as e:
        state = (
            "现有目录已恢复到原状态"
            if e.live_state_unchanged
            else "现有目录状态可能不完整"
        )
        return f"备份恢复失败，{state}：{h(e)}。请检查磁盘空间和目录权限后再操作。", 500
    except Exception as e:
        return f"备份导入失败：{h(e)}。下一步：检查备份内容和恢复范围后重试", 400

    finally:
        if restore_reserved:
            end_restore_activity()
        shutil.rmtree(tmp_dir, ignore_errors=True)
