import shutil
from urllib.parse import unquote

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
from ...utils import h
from ...ui.components import message_card, page_header_card
from ...ui.layout import layout


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

    header = page_header_card(
        "新建文件 / 文件夹",
        f"当前目录：{h(current_rel or 'scripts 根目录')}",
    )

    body = f"""
<form method="post">
<input type="hidden" name="current_rel" value="{h(current_rel)}">
{header}
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
{message_card(msg or "暂无操作")}
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
    parent_rel = (
        str(target.parent.relative_to(SCRIPT_DIR))
        if target.parent != SCRIPT_DIR
        else ""
    )
    header = page_header_card(
        f"查看 / 编辑文件：{target.name}",
        f"路径：{h(target)}",
        f"""
    <button class="btn btn-primary" type="submit">保存文件</button>
    <a class="btn btn-primary" href="{h(script_debug_url(rel))}">调试运行</a>
    <a class="btn btn-orange" href="{h(rename_url(rel))}">改名</a>
    <a class="btn btn-gray" href="{h(script_url(parent_rel))}">返回</a>
        """,
    )

    body = f"""
<form method="post">
{header}
<div class="card">
    <textarea
        name="content"
        class="fls-code-editor"
        data-filename="{h(target.name)}"
        style="min-height:680px;"
    >{h(content)}</textarea>
</div>
{message_card(msg or "暂无保存操作")}
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

    header = page_header_card(
        "改名",
        f"当前路径：{h(target)}",
    )

    body = f"""
<form method="post">
{header}
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
{message_card(msg or "暂无操作")}
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
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
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
