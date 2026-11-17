import os
import shutil
import tempfile
import zipfile
import tarfile
import subprocess
import stat
from pathlib import Path
from urllib.parse import urlparse

from flask import request, redirect

from . import bp
from .helpers import (
    script_safe_path,
    script_url,
    guess_filename_from_url,
    safe_extract_zip,
    safe_extract_tar,
    build_repo_pull_command,
    fetch_file_bytes,
)

from ...paths import SCRIPT_DIR
from ...utils import h
from ...ui.components import message_card, page_header_card
from ...ui.layout import layout
from ...proxy import proxy_select_options, apply_proxy_env


def pull_result_card(msg, kind="info", strong=False):
    return message_card(msg, kind, strong=strong, title="结果") if msg else ""


def _archive_tree_entries(root):
    """Return regular staged entries while rejecting links and special files."""
    entries = []
    root = Path(root)
    for item in root.rglob("*"):
        mode = item.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise RuntimeError("压缩包包含链接或特殊文件")
        entries.append(item)
    return entries


def _publish_archive_tree(staged, target):
    """Publish an extracted tree without overwriting existing live files."""
    staged = Path(staged)
    target = Path(target)
    entries = _archive_tree_entries(staged)

    def reject_live_links(destination):
        cursor = destination.parent
        while True:
            if os.path.lexists(cursor) and cursor.is_symlink():
                raise RuntimeError("目标目录包含链接，未导入压缩包")
            if cursor == target:
                break
            cursor = cursor.parent

    for item in entries:
        relative = item.relative_to(staged)
        destination = target / relative
        reject_live_links(destination)
        if os.path.lexists(destination):
            if item.is_dir() and destination.is_dir() and not destination.is_symlink():
                continue
            raise FileExistsError(f"目标已存在：{destination}")

    if not os.path.lexists(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        return

    if not target.is_dir() or target.is_symlink():
        raise FileExistsError(f"目标不是目录：{target}")

    moved = []
    created_dirs = []

    def ensure_parent(path):
        missing = []
        parent = path.parent
        while not os.path.lexists(parent) and parent != target:
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir()
            created_dirs.append(directory)

    try:
        for item in sorted(entries, key=lambda path: (len(path.relative_to(staged).parts), str(path))):
            relative = item.relative_to(staged)
            destination = target / relative
            if item.is_dir():
                if destination.is_dir() and not destination.is_symlink():
                    continue
                ensure_parent(destination)
                os.replace(item, destination)
                created_dirs.append(destination)
            else:
                ensure_parent(destination)
                os.replace(item, destination)
                moved.append(destination)
    except Exception:
        for destination in reversed(moved):
            try:
                destination.unlink()
            except OSError:
                pass
        for destination in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
            try:
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            except OSError:
                pass
        raise


def _import_archive_atomic(source, target, mode):
    target = Path(target)
    missing_parents = []
    parent = target.parent
    while not os.path.lexists(parent):
        missing_parents.append(parent)
        parent = parent.parent
    for directory in reversed(missing_parents):
        directory.mkdir()
    staging_parent = Path(tempfile.mkdtemp(prefix=".fls-import-", dir=str(target.parent)))
    staged = staging_parent / "payload"
    staged.mkdir()
    published = False
    try:
        if mode == "zip":
            with zipfile.ZipFile(source, "r") as archive:
                safe_extract_zip(archive, staged)
        else:
            with tarfile.open(source, mode="r:gz" if mode == "tar.gz" else "r:") as archive:
                safe_extract_tar(archive, staged)
        _publish_archive_tree(staged, target)
        published = True
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
        if not published:
            for directory in missing_parents:
                try:
                    directory.rmdir()
                except OSError:
                    pass


def _atomic_write_script(target, content):
    target = Path(target)
    missing_parents = []
    parent = target.parent
    while not os.path.lexists(parent):
        missing_parents.append(parent)
        parent = parent.parent
    for directory in reversed(missing_parents):
        directory.mkdir()
    staging_parent = Path(tempfile.mkdtemp(prefix=".fls-fetch-", dir=str(target.parent)))
    staged = staging_parent / target.name
    published = False
    try:
        staged.write_bytes(content)
        os.replace(staged, target)
        published = True
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
        if not published:
            for directory in missing_parents:
                try:
                    directory.rmdir()
                except OSError:
                    pass


def _clone_repo_atomic(url, target, proxy_id, env):
    target = Path(target)
    if os.path.lexists(target):
        raise FileExistsError(f"目标目录已存在：{target}")
    missing_parents = []
    parent = target.parent
    while not os.path.lexists(parent):
        missing_parents.append(parent)
        parent = parent.parent
    for directory in reversed(missing_parents):
        directory.mkdir()
    staging_parent = Path(tempfile.mkdtemp(prefix=".fls-repo-", dir=str(target.parent)))
    staged = staging_parent / "repo"
    published = False
    try:
        subprocess.check_call(
            build_repo_pull_command(url, staged, proxy_id),
            cwd=str(SCRIPT_DIR),
            env=env,
        )
        if os.path.lexists(target):
            raise FileExistsError(f"目标目录已存在：{target}")
        os.replace(staged, target)
        published = True
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
        if not published:
            for directory in missing_parents:
                try:
                    directory.rmdir()
                except OSError:
                    pass


@bp.route("/pull/fetch", methods=["GET", "POST"])
def pull_fetch():
    current_rel = request.args.get("p", "").strip().strip("/")
    msg = ""
    msg_kind = "info"
    msg_strong = False
    proxy_options = proxy_select_options("")

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        filename = request.form.get("filename", "").strip()
        pull_type = request.form.get("pull_type", "file").strip()
        proxy_id = request.form.get("proxy_id", "").strip()
        current_rel = request.form.get("current_rel", "").strip().strip("/")

        if not url:
            msg = "URL 不能为空"
            msg_kind = "error"
            msg_strong = True
        else:
            try:
                if pull_type == "repo":
                    repo_name = filename.strip().strip("/")

                    if not repo_name:
                        repo_name = os.path.basename(urlparse(url).path).replace(".git", "") or "repo"

                    target_rel = f"{current_rel}/{repo_name}" if current_rel else repo_name
                    target = script_safe_path(target_rel)

                    env = os.environ.copy()
                    env = apply_proxy_env(env, proxy_id)
                    _clone_repo_atomic(url, target, proxy_id, env)

                    msg = f"仓库导入成功：{target}"
                    msg_kind = "success"
                    msg_strong = True
                else:
                    if not filename:
                        filename = guess_filename_from_url(url)

                    target_rel = f"{current_rel}/{filename}" if current_rel else filename
                    target = script_safe_path(target_rel)
                    _atomic_write_script(target, fetch_file_bytes(url, proxy_id))

                    msg = f"远程文件导入成功：{target}"
                    msg_kind = "success"
                    msg_strong = True

            except Exception as e:
                msg = f"脚本导入失败：{e}。下一步：检查 URL、代理和目标路径后重试"
                msg_kind = "error"
                msg_strong = True

    header = page_header_card(
        "从远程导入脚本",
        f"当前目录：{h(current_rel or 'scripts 根目录')}",
    )

    body = f"""
{header}
<form method="post">
<section class="section fls-form-section">
    <h2 class="section-title">远程脚本导入设置</h2>
        <input type="hidden" name="current_rel" value="{h(current_rel)}">

        <div class="form-item">
            <label>导入类型</label>
            <select name="pull_type" aria-label="远程导入类型">
                <option value="file">单文件</option>
                <option value="repo">Git 仓库</option>
            </select>
        </div>

        <br>

        <div class="form-item">
            <label>URL</label>
            <input name="url" placeholder="https://example.com/test.py 或 https://github.com/user/repo.git" aria-label="远程脚本 URL">
        </div>

        <br>

        <div class="form-item">
            <label>保存为，相对当前目录</label>
            <input name="filename" placeholder="文件：1.py；仓库：repo-name。不填则自动识别" aria-label="远程脚本保存名称">
        </div>

        <br>

        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id" aria-label="远程导入代理">{proxy_options}</select>
        </div>

        <br>
        <button class="btn btn-primary" type="submit">开始导入脚本</button>
        <a class="btn btn-gray" href="{h(script_url(current_rel))}">返回脚本</a>
</section>
</form>

{pull_result_card(msg, msg_kind, msg_strong)}
"""
    return layout("从远程导入脚本", "pull", body)


@bp.route("/pull/import", methods=["GET", "POST"])
def pull_import():
    current_rel = request.args.get("p", "").strip().strip("/")
    msg = ""
    msg_kind = "info"
    msg_strong = False

    if request.method == "POST":
        current_rel = request.form.get("current_rel", "").strip().strip("/")
        upload = request.files.get("file")
        save_as = request.form.get("save_as", "").strip()

        if not upload or not upload.filename:
            msg = "请选择要导入的文件"
            msg_kind = "error"
            msg_strong = True
        else:
            tmp_dir = tempfile.mkdtemp()

            try:
                original_name = os.path.basename(upload.filename)
                filename = save_as or original_name

                tmp_file = Path(tmp_dir) / original_name
                upload.save(str(tmp_file))

                lower = original_name.lower()

                if lower.endswith((".tar.gz", ".tgz", ".tar")):
                    target_rel = f"{current_rel}/{save_as}" if current_rel and save_as else (save_as or current_rel)
                    target_dir = script_safe_path(target_rel) if target_rel else SCRIPT_DIR
                    mode = "tar.gz" if lower.endswith((".tar.gz", ".tgz")) else "tar"
                    _import_archive_atomic(tmp_file, target_dir, mode)

                    msg = f"压缩包导入成功：{target_dir}"
                    msg_kind = "success"
                    msg_strong = True

                elif lower.endswith(".zip"):
                    target_rel = f"{current_rel}/{save_as}" if current_rel and save_as else (save_as or current_rel)
                    target_dir = script_safe_path(target_rel) if target_rel else SCRIPT_DIR
                    _import_archive_atomic(tmp_file, target_dir, "zip")

                    msg = f"ZIP 导入成功：{target_dir}"
                    msg_kind = "success"
                    msg_strong = True

                else:
                    target_rel = f"{current_rel}/{filename}" if current_rel else filename
                    target = script_safe_path(target_rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(tmp_file, target)
                    msg = f"文件导入成功：{target}"
                    msg_kind = "success"
                    msg_strong = True

            except Exception as e:
                msg = f"导入失败：{e}。下一步：检查文件格式、解压路径和权限后重试"
                msg_kind = "error"
                msg_strong = True
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    header = page_header_card(
        "导入脚本 / 压缩包",
        f"当前目录：{h(current_rel or 'scripts 根目录')}",
    )

    body = f"""
{header}
<form method="post" enctype="multipart/form-data">
<section class="section fls-form-section">
    <h2 class="section-title">脚本文件导入设置</h2>
        <input type="hidden" name="current_rel" value="{h(current_rel)}">

        <div class="form-item">
            <label>选择文件</label>
            <input type="file" name="file" aria-label="要导入的脚本文件">
            <div class="help">支持普通脚本文件、.zip / .tar / .tar.gz / .tgz。</div>
        </div>

        <br>

        <div class="form-item">
            <label>保存为 / 解压到，相对当前目录，可空</label>
            <input name="save_as" placeholder="普通文件：1.py；压缩包：folder-name；为空则使用原文件名或解压到当前目录" aria-label="脚本保存名称或解压目录">
        </div>

        <br>

        <button class="btn btn-primary" type="submit">开始导入脚本文件</button>
        <a class="btn btn-gray" href="{h(script_url(current_rel))}">返回脚本</a>
</section>
</form>

{pull_result_card(msg, msg_kind, msg_strong)}
"""
    return layout("导入脚本", "pull", body)
