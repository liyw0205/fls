import os
import shlex
import time
import uuid
import signal
import shutil
import tempfile
import zipfile
import tarfile
import threading
import subprocess
from pathlib import Path
from urllib.parse import quote, unquote

from flask import Blueprint, request, redirect, abort, Response, jsonify

from ..paths import SCRIPT_DIR, LOG_DIR
from ..utils import h, now_str, safe_name, get_back_url
from ..ui.layout import layout
from ..ui.log_controls import log_controls
from ..logs import tail_file
from ..models import load_global_env
from ..command import build_command

bp = Blueprint("scripts", __name__)
SCRIPT_DEBUG_RUNNING = {}

def script_safe_path(rel_path=""):
    rel_path = str(rel_path or "").strip().lstrip("/")
    target = (SCRIPT_DIR / rel_path).resolve()
    base = SCRIPT_DIR.resolve()
    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("路径非法")
    return target

def script_rel_path(path):
    return str(Path(path).resolve().relative_to(SCRIPT_DIR.resolve()))

def script_url(rel=""):
    rel = str(rel or "").strip().strip("/")
    if not rel:
        return "/pull"
    return "/pull?p=" + quote(rel)

def view_url(rel):
    return "/scripts/view?path=" + quote(str(rel or ""))

def rename_url(rel):
    return "/scripts/rename?path=" + quote(str(rel or ""))

def download_url(rel):
    return "/scripts/download/" + quote(str(rel or ""))

def delete_url(rel):
    return "/scripts/delete/" + quote(str(rel or ""))

def script_debug_url(rel):
    return "/scripts/debug/" + quote(str(rel or ""))


def script_debug_log_url(debug_id):
    return "/scripts/debug-log/" + quote(str(debug_id or ""))
    
def breadcrumb(current_rel):
    parts = [p for p in Path(current_rel).parts if p not in ("", ".")]
    items = [f'<a href="{h(script_url(""))}">scripts</a>']
    acc = []
    for p in parts:
        acc.append(p)
        rel = "/".join(acc)
        items.append(f'<a href="{h(script_url(rel))}">{h(p)}</a>')
    return " / ".join(items)

def render_rows(current_rel=""):
    current_rel = str(current_rel or "").strip().strip("/")
    try:
        current_dir = script_safe_path(current_rel)
    except Exception:
        current_rel = ""
        current_dir = SCRIPT_DIR

    if not current_dir.exists():
        current_dir.mkdir(parents=True, exist_ok=True)

    if not current_dir.is_dir():
        current_dir = current_dir.parent
        current_rel = script_rel_path(current_dir) if current_dir != SCRIPT_DIR else ""

    rows = ""

    if current_dir.resolve() != SCRIPT_DIR.resolve():
        parent = current_dir.parent
        parent_rel = "" if parent.resolve() == SCRIPT_DIR.resolve() else script_rel_path(parent)
        rows += f"""
<tr>
    <td><span class="badge gray">返回</span></td>
    <td><a href="{h(script_url(parent_rel))}" style="font-weight:900;font-size:16px;">..</a></td>
    <td>-</td><td>-</td><td>{h(str(parent))}</td>
    <td><a class="btn btn-gray" href="{h(script_url(parent_rel))}">返回上级</a></td>
</tr>
"""

    items = list(current_dir.iterdir()) if current_dir.exists() else []
    items.sort(key=lambda x: (x.is_file(), x.name.lower()))

    if not items and not rows:
        return '<tr><td colspan="6">暂无脚本，请点击“新建”添加脚本</td></tr>'

    for item in items:
        rel = script_rel_path(item)
        is_dir = item.is_dir()
        badge = '<span class="badge green">文件夹</span>' if is_dir else '<span class="badge blue">文件</span>'
        mtime = now_str()
        try:
            mtime = __import__("datetime").datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        if is_dir:
            size_text = "-"
            name_html = f'<a href="{h(script_url(rel))}" style="font-weight:800;">📁 {h(item.name)}</a>'
            buttons = f"""
<a class="btn btn-primary" href="{h(script_url(rel))}">打开</a>
<a class="btn btn-orange" href="{h(rename_url(rel))}">改名</a>
<a class="btn btn-red" href="{h(delete_url(rel))}" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除</a>
"""
        else:
            size_text = f"{item.stat().st_size / 1024:.1f} KB"
            name_html = f'<a href="{h(view_url(rel))}" style="font-weight:800;">📄 {h(item.name)}</a>'
            buttons = f"""
<a class="btn btn-blue" href="{h(view_url(rel))}">查看</a>
<a class="btn btn-primary" href="{h(script_debug_url(rel))}">调试</a>
<a class="btn btn-primary" href="{h(download_url(rel))}">下载</a>
<a class="btn btn-orange" href="{h(rename_url(rel))}">改名</a>
<a class="btn btn-red" href="{h(delete_url(rel))}" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除</a>
"""

        rows += f"""
<tr>
    <td>{badge}</td>
    <td>{name_html}<div class="help">{h(rel)}</div></td>
    <td>{h(size_text)}</td>
    <td>{h(mtime)}</td>
    <td>{h(str(item))}</td>
    <td>{buttons}</td>
</tr>
"""
    return rows

@bp.route("/pull")
def scripts_page():
    current_rel = request.args.get("p", "").strip().strip("/")
    try:
        current_dir = script_safe_path(current_rel)
    except Exception:
        current_rel = ""
        current_dir = SCRIPT_DIR

    if not current_dir.exists():
        current_dir.mkdir(parents=True, exist_ok=True)

    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">脚本管理</div>
            <div class="help">
                当前目录：<b>{h(current_dir)}</b><br>
                路径：{breadcrumb(current_rel)}
            </div>
        </div>
        <div class="action-row">
            <a class="btn btn-primary" href="/pull/fetch?p={h(current_rel)}">拉取</a>
            <a class="btn btn-orange" href="/pull/import?p={h(current_rel)}">导入</a>
            <a class="btn btn-blue" href="/pull/new?p={h(current_rel)}">新建</a>
            <a class="btn btn-gray" href="/pull">回到根目录</a>
        </div>
    </div>
</div>

<div class="card">
    <div class="card-title">文件列表</div>
    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>类型</th><th>名称 / 相对路径</th><th>大小</th><th>修改时间</th><th>绝对路径</th><th>操作</th>
                </tr>
            </thead>
            <tbody>{render_rows(current_rel)}</tbody>
        </table>
    </div>
</div>

<div class="card">
    <div class="card-title">任务命令示例</div>
    <div class="code">
task 1.py<br>
task folder/main.py<br>
task /root/fls/scripts/demo.sh arg1 arg2
    </div>
</div>
"""
    return layout("脚本管理", "pull", body)

@bp.route("/pull/new", methods=["GET", "POST"])
def scripts_new():
    current_rel = request.args.get("p", "").strip().strip("/")
    msg = ""
    if request.method == "POST":
        current_rel = request.form.get("current_rel", "").strip().strip("/")
        item_type = request.form.get("item_type", "file")
        name = request.form.get("name", "").strip()
        content = request.form.get("content", "")
        try:
            if not name or "/" in name or "\\" in name or name in (".", ".."):
                raise ValueError("名称非法")
            rel = f"{current_rel}/{name}" if current_rel else name
            target = script_safe_path(rel)
            if target.exists():
                raise FileExistsError("目标已存在")
            if item_type == "dir":
                target.mkdir(parents=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            return redirect(script_url(current_rel))
        except Exception as e:
            msg = f"新建失败：{e}"

    body = f"""
<form method="post">
<input type="hidden" name="current_rel" value="{h(current_rel)}">
<div class="card">
    <div class="card-title">新建文件 / 文件夹</div>
    <div class="help">当前目录：{h(current_rel or 'scripts 根目录')}</div>
</div>
<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>类型</label>
            <select name="item_type">
                <option value="file">文件</option>
                <option value="dir">文件夹</option>
            </select>
        </div>
        <div class="form-item">
            <label>名称</label>
            <input name="name" required placeholder="test.py 或 demo">
        </div>
    </div>
</div>
<div class="card">
    <div class="card-title">文件内容</div>
    <textarea
        name="content"
        class="fls-code-editor"
        data-filename=""
    ></textarea>
</div>
<div class="card">
    <button class="btn btn-primary" type="submit">保存新建</button>
    <a class="btn btn-gray" href="{h(script_url(current_rel))}">返回</a>
</div>
<div class="card"><div class="help">{h(msg or '暂无操作')}</div></div>
</form>
"""
    return layout("新建脚本", "pull", body)

@bp.route("/scripts/view", methods=["GET", "POST"])
def scripts_view():
    raw = unquote(request.args.get("path", "").strip())
    target = script_safe_path(raw)
    if not target.exists() or not target.is_file():
        abort(404)

    msg = ""
    if request.method == "POST":
        try:
            target.write_text(request.form.get("content", ""), encoding="utf-8")
            msg = "保存成功"
        except Exception as e:
            msg = f"保存失败：{e}"

    content = target.read_text(encoding="utf-8", errors="replace")
    rel = script_rel_path(target)
    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">查看 / 编辑文件：{h(target.name)}</div>
    <div class="help">路径：{h(target)}</div>
    <br>
    <button class="btn btn-primary" type="submit">保存文件</button>
    <a class="btn btn-primary" href="{h(script_debug_url(rel))}">调试运行</a>
    <a class="btn btn-orange" href="{h(rename_url(rel))}">改名</a>
    <a class="btn btn-gray" href="{h(script_url(str(target.parent.relative_to(SCRIPT_DIR)) if target.parent != SCRIPT_DIR else ''))}">返回</a>
</div>
<div class="card">
    <textarea
        name="content"
        class="fls-code-editor"
        data-filename="{h(target.name)}"
        style="min-height:680px;"
    >{h(content)}</textarea>
</div>
<div class="card"><div class="help">{h(msg or '暂无保存操作')}</div></div>
</form>
"""
    return layout("查看 / 编辑文件", "pull", body)

@bp.route("/scripts/rename", methods=["GET", "POST"])
def scripts_rename():
    raw = unquote(request.args.get("path", "").strip())
    target = script_safe_path(raw)
    if not target.exists() or target.resolve() == SCRIPT_DIR.resolve():
        abort(404)

    msg = ""
    if request.method == "POST":
        try:
            new_name = request.form.get("new_name", "").strip()
            if not new_name or "/" in new_name or "\\" in new_name:
                raise ValueError("新名称非法")
            new_target = target.with_name(new_name).resolve()
            new_target.relative_to(SCRIPT_DIR.resolve())
            if new_target.exists():
                raise FileExistsError("目标已存在")
            target.rename(new_target)
            parent_rel = "" if new_target.parent == SCRIPT_DIR else str(new_target.parent.relative_to(SCRIPT_DIR))
            return redirect(script_url(parent_rel))
        except Exception as e:
            msg = f"改名失败：{e}"

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">改名</div>
    <div class="help">当前路径：{h(target)}</div>
</div>
<div class="card">
    <div class="form-item">
        <label>新名称</label>
        <input name="new_name" required value="{h(target.name)}">
    </div>
</div>
<div class="card">
    <button class="btn btn-primary" type="submit">保存改名</button>
    <a class="btn btn-gray" href="/pull">返回</a>
</div>
<div class="card"><div class="help">{h(msg or '暂无操作')}</div></div>
</form>
"""
    return layout("改名", "pull", body)

@bp.route("/scripts/download/<path:rel_path>")
def scripts_download(rel_path):
    target = script_safe_path(rel_path)
    if not target.exists() or target.is_dir():
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
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'}
    )

@bp.route("/scripts/delete/<path:rel_path>")
def scripts_delete(rel_path):
    target = script_safe_path(rel_path)
    if not target.exists() or target.resolve() == SCRIPT_DIR.resolve():
        abort(404)

    parent = target.parent
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    parent_rel = "" if parent == SCRIPT_DIR else str(parent.relative_to(SCRIPT_DIR))
    return redirect(script_url(parent_rel))

# ============================================================
# 脚本拉取 / 导入
# ============================================================
import requests
import subprocess
from urllib.parse import urlparse
from ..proxy import (
    proxy_select_options,
    github_proxy_url,
    requests_proxy_dict,
    build_git_command_with_github_proxy,
)


def guess_filename_from_url(url):
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    if not name:
        name = f"script_{now_str().replace(':', '').replace(' ', '-')}.py"
    return name


def safe_extract_zip(zip_obj, path):
    base = Path(path).resolve()

    for member in zip_obj.infolist():
        target = (base / member.filename).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise RuntimeError("压缩包包含非法路径")

    zip_obj.extractall(path)


def safe_extract_tar(tar, path):
    base = Path(path).resolve()

    for member in tar.getmembers():
        target = (base / member.name).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise RuntimeError("压缩包包含非法路径")

    tar.extractall(path)


@bp.route("/pull/fetch", methods=["GET", "POST"])
def pull_fetch():
    current_rel = request.args.get("p", "").strip().strip("/")
    msg = ""
    proxy_options = proxy_select_options("")

    if request.method == "POST":
        url = request.form.get("url", "").strip()
        filename = request.form.get("filename", "").strip()
        pull_type = request.form.get("pull_type", "file").strip()
        proxy_id = request.form.get("proxy_id", "").strip()
        current_rel = request.form.get("current_rel", "").strip().strip("/")

        if not url:
            msg = "URL 不能为空"
        else:
            try:
                url_for_pull = github_proxy_url(url, proxy_id)

                if pull_type == "repo":
                    repo_name = filename.strip().strip("/")

                    if not repo_name:
                        repo_name = os.path.basename(urlparse(url).path).replace(".git", "") or "repo"

                    target_rel = f"{current_rel}/{repo_name}" if current_rel else repo_name
                    target = script_safe_path(target_rel)

                    if target.exists():
                        raise FileExistsError(f"目标目录已存在：{target}")

                    git_bin = shutil.which("git")
                    if not git_bin:
                        raise RuntimeError("未安装 git，请先安装 git")

                    env = os.environ.copy()
                    from ..proxy import apply_proxy_env
                    env = apply_proxy_env(env, proxy_id)

                    git_cmd = build_git_command_with_github_proxy(
                        git_bin,
                        proxy_id,
                        ["clone", url, str(target)],
                        verify=True,
                    )

                    subprocess.check_call(
                        git_cmd,
                        cwd=str(SCRIPT_DIR),
                        env=env
                    )

                    msg = f"仓库拉取成功：{target}"

                else:
                    if not filename:
                        filename = guess_filename_from_url(url)

                    target_rel = f"{current_rel}/{filename}" if current_rel else filename
                    target = script_safe_path(target_rel)
                    target.parent.mkdir(parents=True, exist_ok=True)

                    r = requests.get(
                        url_for_pull,
                        headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
                        timeout=60,
                        proxies=requests_proxy_dict(proxy_id)
                    )
                    r.raise_for_status()
                    target.write_bytes(r.content)

                    msg = f"文件拉取成功：{target}"

            except Exception as e:
                msg = f"拉取失败：{e}"

    body = f"""
<div class="card">
    <div class="card-title">拉取脚本 / 仓库</div>
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

<div class="card">
    <div class="card-title">结果</div>
    <div class="help">{h(msg or "暂无操作")}</div>
</div>
"""
    return layout("拉取脚本", "pull", body)


@bp.route("/pull/import", methods=["GET", "POST"])
def pull_import():
    current_rel = request.args.get("p", "").strip().strip("/")
    msg = ""

    if request.method == "POST":
        current_rel = request.form.get("current_rel", "").strip().strip("/")
        upload = request.files.get("file")
        save_as = request.form.get("save_as", "").strip()

        if not upload or not upload.filename:
            msg = "请选择要导入的文件"
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

                elif lower.endswith(".zip"):
                    target_rel = f"{current_rel}/{save_as}" if current_rel and save_as else (save_as or current_rel)
                    target_dir = script_safe_path(target_rel) if target_rel else SCRIPT_DIR
                    target_dir.mkdir(parents=True, exist_ok=True)

                    with zipfile.ZipFile(tmp_file, "r") as z:
                        safe_extract_zip(z, target_dir)

                    msg = f"ZIP 导入成功：{target_dir}"

                else:
                    target_rel = f"{current_rel}/{filename}" if current_rel else filename
                    target = script_safe_path(target_rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(tmp_file, target)
                    msg = f"文件导入成功：{target}"

            except Exception as e:
                msg = f"导入失败：{e}"
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    body = f"""
<div class="card">
    <div class="card-title">导入脚本 / 压缩包</div>
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

<div class="card">
    <div class="card-title">结果</div>
    <div class="help">{h(msg or "暂无操作")}</div>
</div>
"""
    return layout("导入脚本", "pull", body)

# ============================================================
# 脚本调试运行 / 测试运行
# ============================================================

def script_debug_log_file(debug_id, script_name):
    safe = safe_name(script_name or "script-debug")
    return LOG_DIR / f"script-debug-{safe}-{debug_id}.log"


def terminate_debug_process(proc):
    if not proc:
        return

    try:
        if proc.poll() is not None:
            return

        if os.name == "nt":
            try:
                proc.terminate()
            except Exception:
                pass

            time.sleep(1)

            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

            time.sleep(1)

            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    except Exception:
        pass


def script_debug_watch(debug_id, proc, log_fp):
    try:
        return_code = proc.wait()

        try:
            log_fp.write(
                f"\n===== 调试运行结束: {now_str()}，退出码: {return_code} =====\n".encode("utf-8")
            )
            log_fp.close()
        except Exception:
            pass

        if debug_id in SCRIPT_DEBUG_RUNNING:
            SCRIPT_DEBUG_RUNNING[debug_id]["running"] = False
            SCRIPT_DEBUG_RUNNING[debug_id]["returncode"] = return_code
            SCRIPT_DEBUG_RUNNING[debug_id]["process"] = None

    except Exception as e:
        try:
            log_fp.write(
                f"\n===== 调试监听异常: {e} =====\n".encode("utf-8")
            )
            log_fp.close()
        except Exception:
            pass

        if debug_id in SCRIPT_DEBUG_RUNNING:
            SCRIPT_DEBUG_RUNNING[debug_id]["running"] = False
            SCRIPT_DEBUG_RUNNING[debug_id]["returncode"] = -1
            SCRIPT_DEBUG_RUNNING[debug_id]["error"] = str(e)
            SCRIPT_DEBUG_RUNNING[debug_id]["process"] = None


@bp.route("/scripts/debug/<path:rel_path>")
def scripts_debug_run(rel_path):
    back_url = get_back_url("/pull")

    try:
        target = script_safe_path(rel_path)
    except Exception as e:
        return f"路径非法：{h(e)}", 400

    if not target.exists() or not target.is_file():
        abort(404)

    rel = script_rel_path(target)

    debug_id = uuid.uuid4().hex
    log_file = script_debug_log_file(debug_id, target.name)
    log_fp = open(log_file, "ab", buffering=0)

    task = {
        "id": debug_id,
        "name": f"脚本调试：{target.name}",
        "command": "task " + shlex.quote(rel),
        "env": {},
        "proxy_id": "",
    }

    try:
        cmd_info = build_command(task)

        env = os.environ.copy()
        env.update(load_global_env())
        env["PYTHONUNBUFFERED"] = "1"
        env["FLS_SCRIPT_DEBUG_ID"] = debug_id
        env["FLS_SCRIPT_DEBUG_FILE"] = str(target)

        display_cmd = cmd_info.get("display_cmd", cmd_info.get("cmd"))

        header = (
            f"===== 脚本调试运行 =====\n"
            f"时间: {now_str()}\n"
            f"调试ID: {debug_id}\n"
            f"脚本: {target}\n"
            f"相对路径: {rel}\n"
            f"命令: {task['command']}\n"
            f"工作目录: {cmd_info.get('cwd')}\n"
            f"实际启动命令: {display_cmd}\n"
            f"日志文件: {log_file}\n"
            f"============================================================\n"
        )

        log_fp.write(header.encode("utf-8"))

        popen_kwargs = {
            "shell": cmd_info.get("shell", False),
            "cwd": cmd_info["cwd"],
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "env": env,
        }

        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            cmd_info["cmd"],
            **popen_kwargs,
        )

        SCRIPT_DEBUG_RUNNING[debug_id] = {
            "id": debug_id,
            "script": str(target),
            "rel": rel,
            "log_file": str(log_file),
            "running": True,
            "process": proc,
            "pid": proc.pid,
            "start_time": time.time(),
            "returncode": None,
            "error": "",
        }

        th = threading.Thread(
            target=script_debug_watch,
            args=(debug_id, proc, log_fp),
            daemon=True,
            name=f"fls-script-debug-{debug_id[:8]}",
        )
        th.start()

    except Exception as e:
        try:
            log_fp.write(f"启动调试失败: {e}\n".encode("utf-8"))
            log_fp.close()
        except Exception:
            pass

        SCRIPT_DEBUG_RUNNING[debug_id] = {
            "id": debug_id,
            "script": str(target),
            "rel": rel,
            "log_file": str(log_file),
            "running": False,
            "process": None,
            "pid": "-",
            "start_time": time.time(),
            "returncode": -1,
            "error": str(e),
        }

    return redirect(f"/scripts/debug-log/{debug_id}?back={quote(back_url)}")


@bp.route("/scripts/debug-stop/<debug_id>")
def scripts_debug_stop(debug_id):
    info = SCRIPT_DEBUG_RUNNING.get(debug_id)

    if info and info.get("running"):
        proc = info.get("process")

        if proc:
            terminate_debug_process(proc)

        info["running"] = False
        info["process"] = None
        info["error"] = "用户手动停止"

        log_file = info.get("log_file")

        if log_file:
            try:
                with open(log_file, "ab") as f:
                    f.write(
                        f"\n===== 用户手动停止调试: {now_str()} =====\n".encode("utf-8")
                    )
            except Exception:
                pass

    return redirect(get_back_url("/pull"))


@bp.route("/scripts/debug-log/<debug_id>")
def scripts_debug_log(debug_id):
    back_url = get_back_url("/pull")
    info = SCRIPT_DEBUG_RUNNING.get(debug_id)

    if not info:
        body = f"""
<div class="card">
    <div class="card-title">脚本调试日志</div>
    <div class="help">
        调试记录不存在或面板已重启。<br>
        可以到日志管理里查找 <code>script-debug-*.log</code>。
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/logs?back={h(back_url)}">日志管理</a>
</div>
"""
        return layout("脚本调试日志", "pull", body)

    stop_btn = ""

    if info.get("running"):
        stop_btn = f"""
<a class="btn btn-red" href="/scripts/debug-stop/{h(debug_id)}?back={h(back_url)}" onclick="return confirm('确定停止该调试运行吗？')">
    停止调试
</a>
"""

    body = f"""
<div class="card">
    <div class="card-title">脚本调试日志</div>
    <div class="help">
        状态：<b id="debugStatus">{"运行中" if info.get("running") else "已结束"}</b><br>
        PID：{h(info.get("pid") or "-")}<br>
        脚本：{h(info.get("script") or "-")}<br>
        日志文件：{h(info.get("log_file") or "-")}
    </div>
    <br>
    {stop_btn}
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/pull">脚本管理</a>
</div>

<pre class="log" id="log">加载中...</pre>
{log_controls()}

<script>
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function nearBottom(){{
    return document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 90;
}}

window.addEventListener("scroll", function(){{
    window.__FLS_LOG_NEAR_BOTTOM__ = nearBottom();
}}, {{passive:true}});

async function loadScriptDebugLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/scripts/debug-log/{h(debug_id)}?lines=1600", {{cache:"no-store"}});
        const json = await res.json();

        const statusEl = document.getElementById("debugStatus");
        if(statusEl) statusEl.textContent = json.running ? "运行中" : "已结束";

        const text = json.log || "暂无日志";
        const old = window.__FLS_LOG_LAST_TEXT__ || "";
        const changed = text !== old;

        var logEl = document.getElementById("log");

        if(typeof flsRenderLogText === "function"){{
            flsRenderLogText(logEl, text);
        }}else{{
            logEl.textContent = text;
        }}

        window.__FLS_LOG_LAST_TEXT__ = text;

        if(changed){{
            if(wasNearBottom || window.__FLS_LOG_NEAR_BOTTOM__){{
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "none";
                window.scrollTo(0, document.documentElement.scrollHeight);
            }}else{{
                const afterHeight = document.documentElement.scrollHeight;
                window.scrollTo(0, beforeScroll + Math.max(afterHeight - beforeHeight, 0));
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "block";
            }}
        }}

        if(!json.running){{
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadScriptDebugLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadScriptDebugLog, 2000);
</script>
"""

    return layout("脚本调试日志", "pull", body)


@bp.route("/api/scripts/debug-log/<debug_id>")
def api_scripts_debug_log(debug_id):
    info = SCRIPT_DEBUG_RUNNING.get(debug_id)

    if not info:
        return jsonify({
            "running": False,
            "log": "调试记录不存在或面板已重启。请到日志管理中查找 script-debug-*.log。",
        })

    lines = int(request.args.get("lines", "1200") or 1200)

    return jsonify({
        "running": bool(info.get("running")),
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "log_file": info.get("log_file", ""),
        "log": tail_file(info.get("log_file", ""), lines),
    })