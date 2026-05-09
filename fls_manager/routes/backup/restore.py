from ._common import *


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
    f = request.files.get("file")

    if not f:
        return "未上传文件", 400

    restore_items = parse_items_from_form("restore_items")

    if not restore_items:
        return "请至少选择一个恢复内容", 400

    restore_deps = request.form.get("restore_deps") == "1"
    tmp_dir = tempfile.mkdtemp()

    try:
        original = os.path.basename(f.filename or "backup")
        backup_file = Path(tmp_dir) / original
        f.save(str(backup_file))

        extract_dir = Path(tmp_dir) / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        extract_archive(backup_file, extract_dir)

        root = find_backup_root(extract_dir)

        if not root:
            raise RuntimeError("未在备份中找到 data 或 scripts 目录")

        dep_file = find_dependency_file(extract_dir, root)

        data_src = root / "data"
        scripts_src = root / "scripts"

        restored = []

        if "data" in restore_items:
            if not data_src.exists():
                raise RuntimeError("备份中没有 data 目录，无法恢复配置")

            # 保留当前 backups 目录，避免导入配置时把备份列表删掉。
            current_backup_dir = backup_dir()
            keep_tmp = Path(tmp_dir) / "keep_backups"

            if current_backup_dir.exists():
                shutil.copytree(current_backup_dir, keep_tmp, dirs_exist_ok=True)

            if DATA_DIR.exists():
                shutil.rmtree(DATA_DIR)

            shutil.copytree(data_src, DATA_DIR)

            if keep_tmp.exists():
                shutil.copytree(keep_tmp, DATA_DIR / "backups", dirs_exist_ok=True)

            restored.append("配置 data")

        if "scripts" in restore_items:
            if not scripts_src.exists():
                raise RuntimeError("备份中没有 scripts 目录，无法恢复脚本")

            if SCRIPT_DIR.exists():
                shutil.rmtree(SCRIPT_DIR)

            shutil.copytree(scripts_src, SCRIPT_DIR)

            restored.append("脚本 scripts")

        deps_msg = ""
        deps_log = ""

        if restore_deps:
            if dep_file:
                ok, deps_log = install_dependencies(dep_file)
                deps_msg = "依赖恢复成功" if ok else "依赖恢复失败，请查看日志"
            else:
                deps_msg = "已勾选恢复依赖，但备份中没有 dependencies.txt / requirements.txt"

        reload_scheduler()

        body = f"""
<div class="card">
    <div class="card-title">备份导入完成</div>
    <div class="help">
        已恢复：{h("、".join(restored) or "-")}<br>
        依赖恢复：{h(deps_msg or "未恢复依赖")}<br>
        日志：{h(deps_log or "-")}
    </div>
    <br>
    <a class="btn btn-primary" href="/backup">返回备份恢复</a>
    <a class="btn btn-gray" href="/logs">查看日志</a>
</div>
"""
        return layout("备份导入完成", "backup", body)

    except Exception as e:
        return f"备份导入失败：{h(e)}", 400

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
