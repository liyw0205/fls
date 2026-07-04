import os
import shutil
import tempfile
import zipfile
import tarfile
import subprocess
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
    return message_card(msg or "暂无操作", kind, strong=strong, title="结果")


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

                    if target.exists():
                        raise FileExistsError(f"目标目录已存在：{target}")

                    env = os.environ.copy()
                    env = apply_proxy_env(env, proxy_id)

                    git_cmd = build_repo_pull_command(url, target, proxy_id)

                    subprocess.check_call(
                        git_cmd,
                        cwd=str(SCRIPT_DIR),
                        env=env,
                    )

                    msg = f"仓库拉取成功：{target}"
                    msg_kind = "success"
                    msg_strong = True
                else:
                    if not filename:
                        filename = guess_filename_from_url(url)

                    target_rel = f"{current_rel}/{filename}" if current_rel else filename
                    target = script_safe_path(target_rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(fetch_file_bytes(url, proxy_id))

                    msg = f"文件拉取成功：{target}"
                    msg_kind = "success"
                    msg_strong = True

            except Exception as e:
                msg = f"拉取失败：{e}"
                msg_kind = "error"
                msg_strong = True

    header = page_header_card(
        "拉取脚本 / 仓库",
        f"当前目录：{h(current_rel or 'scripts 根目录')}",
    )

    body = f"""
{header}
<div class="card">
    <form method="post">
        <input type="hidden" name="current_rel" value="{h(current_rel)}">

        <div class="form-item">
            <label>拉取类型</label>
            <select name="pull_type">
                <option value="file">单文件</option>
                <option value="repo">Git 仓库</option>
            </select>
        </div>

        <br>

        <div class="form-item">
            <label>URL</label>
            <input name="url" placeholder="https://example.com/test.py 或 https://github.com/user/repo.git">
        </div>

        <br>

        <div class="form-item">
            <label>保存为，相对当前目录</label>
            <input name="filename" placeholder="文件：1.py；仓库：repo-name。不填则自动识别">
        </div>

        <br>

        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id">{proxy_options}</select>
        </div>

        <br>
        <button class="btn btn-primary" type="submit">开始拉取</button>
        <a class="btn btn-gray" href="{h(script_url(current_rel))}">返回脚本管理</a>
    </form>
</div>

{pull_result_card(msg, msg_kind, msg_strong)}
"""
    return layout("拉取脚本", "pull", body)


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
                    target_dir.mkdir(parents=True, exist_ok=True)

                    mode = "r:gz" if lower.endswith((".tar.gz", ".tgz")) else "r:"

                    with tarfile.open(tmp_file, mode) as tar:
                        safe_extract_tar(tar, target_dir)

                    msg = f"压缩包导入成功：{target_dir}"
                    msg_kind = "success"
                    msg_strong = True

                elif lower.endswith(".zip"):
                    target_rel = f"{current_rel}/{save_as}" if current_rel and save_as else (save_as or current_rel)
                    target_dir = script_safe_path(target_rel) if target_rel else SCRIPT_DIR
                    target_dir.mkdir(parents=True, exist_ok=True)

                    with zipfile.ZipFile(tmp_file, "r") as z:
                        safe_extract_zip(z, target_dir)

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
                msg = f"导入失败：{e}"
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
<div class="card">
    <form method="post" enctype="multipart/form-data">
        <input type="hidden" name="current_rel" value="{h(current_rel)}">

        <div class="form-item">
            <label>选择文件</label>
            <input type="file" name="file">
            <div class="help">支持普通脚本文件、.zip / .tar / .tar.gz / .tgz。</div>
        </div>

        <br>

        <div class="form-item">
            <label>保存为 / 解压到，相对当前目录，可空</label>
            <input name="save_as" placeholder="普通文件：1.py；压缩包：folder-name；为空则使用原文件名或解压到当前目录">
        </div>

        <br>

        <button class="btn btn-primary" type="submit">开始导入</button>
        <a class="btn btn-gray" href="{h(script_url(current_rel))}">返回脚本管理</a>
    </form>
</div>

{pull_result_card(msg, msg_kind, msg_strong)}
"""
    return layout("导入脚本", "pull", body)
