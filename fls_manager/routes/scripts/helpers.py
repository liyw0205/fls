import os
import shlex
import shutil
import tempfile
import zipfile
import tarfile
import subprocess
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ...paths import SCRIPT_DIR
from ...utils import h, now_str
from ...command import build_command
from ...ui.components import empty_state, empty_table_row
from ...proxy import (
    github_proxy_url,
    requests_proxy_dict,
    build_git_command_with_github_proxy,
)


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


def _resolve_script_listing(current_rel=""):
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

    return current_rel, current_dir


def render_rows(current_rel=""):
    current_rel, current_dir = _resolve_script_listing(current_rel)

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
        return empty_table_row(6, "暂无脚本", '<a class="btn btn-primary" href="/pull/new">新建脚本</a>')

    for item in items:
        rel = script_rel_path(item)
        is_dir = item.is_dir()
        badge = '<span class="badge green">文件夹</span>' if is_dir else '<span class="badge blue">文件</span>'
        mtime = now_str()

        try:
            mtime = __import__("datetime").datetime.fromtimestamp(
                item.stat().st_mtime
            ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        if is_dir:
            size_text = "-"
            name_html = f'<a href="{h(script_url(rel))}" style="font-weight:800;">📁 {h(item.name)}</a>'
            buttons = f"""
<div class="row-actions" aria-label="脚本目录 {h(item.name)} 行操作">
    <div class="row-actions-primary"><a class="btn btn-primary" href="{h(script_url(rel))}">打开目录</a></div>
    <div class="row-actions-secondary"><a class="btn btn-orange" href="{h(rename_url(rel))}">重命名目录</a></div>
    <div class="row-actions-danger">
        <form class="inline-form" method="post" action="{h(delete_url(rel))}">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除目录</button>
        </form>
    </div>
</div>
"""
        else:
            size_text = f"{item.stat().st_size / 1024:.1f} KB"
            name_html = f'<a href="{h(view_url(rel))}" style="font-weight:800;">📄 {h(item.name)}</a>'
            buttons = f"""
<div class="row-actions" aria-label="脚本 {h(item.name)} 行操作">
    <div class="row-actions-primary"><a class="btn btn-primary" href="{h(view_url(rel))}">查看脚本</a></div>
    <div class="row-actions-secondary">
        <a class="btn btn-blue" href="{h(script_debug_url(rel))}">调试脚本</a>
        <a class="btn btn-blue" href="{h(download_url(rel))}">下载脚本</a>
        <a class="btn btn-orange" href="{h(rename_url(rel))}">重命名脚本</a>
    </div>
    <div class="row-actions-danger">
        <form class="inline-form" method="post" action="{h(delete_url(rel))}">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定删除 {h(rel)} 吗？')">删除脚本</button>
        </form>
    </div>
</div>
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


def render_mobile_rows(current_rel=""):
    """Render script entities as a single-column mobile list."""
    current_rel, current_dir = _resolve_script_listing(current_rel)
    cards = ""

    if current_dir.resolve() != SCRIPT_DIR.resolve():
        parent = current_dir.parent
        parent_rel = "" if parent.resolve() == SCRIPT_DIR.resolve() else script_rel_path(parent)
        cards += f"""
<article class="fls-fold-card mobile-list-item script-mobile-item">
    <div class="fls-card-head">
        <div class="fls-card-main">
            <div class="fls-card-title-main">返回上级目录</div>
            <div class="fls-card-sub">{h(str(parent))}</div>
        </div>
        <span class="badge gray status-badge status-info" role="status">导航</span>
    </div>
    <div class="fls-card-actions fls-card-primary-action">
        <a class="btn btn-primary" href="{h(script_url(parent_rel))}">返回上级目录</a>
    </div>
</article>
"""

    items = list(current_dir.iterdir()) if current_dir.exists() else []
    items.sort(key=lambda x: (x.is_file(), x.name.lower()))

    for item in items:
        rel = script_rel_path(item)
        is_dir = item.is_dir()
        item_type = "文件夹" if is_dir else "文件"
        badge_class = "green" if is_dir else "blue"
        badge = f'<span class="badge {badge_class} status-badge status-info" role="status">{item_type}</span>'

        try:
            mtime = __import__("datetime").datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = "-"

        if is_dir:
            size_text = "-"
            primary_url = script_url(rel)
            primary_label = "打开脚本目录"
            secondary = f'<a class="btn btn-orange" href="{h(rename_url(rel))}">重命名目录</a>'
            danger_label = "删除目录"
        else:
            size_text = f"{item.stat().st_size / 1024:.1f} KB"
            primary_url = view_url(rel)
            primary_label = "查看脚本"
            secondary = (
                f'<a class="btn btn-blue" href="{h(script_debug_url(rel))}">调试脚本</a>'
                f'<a class="btn btn-blue" href="{h(download_url(rel))}">下载脚本</a>'
                f'<a class="btn btn-orange" href="{h(rename_url(rel))}">重命名脚本</a>'
            )
            danger_label = "删除脚本"

        cards += f"""
<article class="fls-fold-card mobile-list-item script-mobile-item">
    <div class="fls-card-head">
        <div class="fls-card-main">
            <div class="fls-card-title-main">{h(item.name)}</div>
            <div class="fls-card-sub">{h(rel)}</div>
        </div>
        <div class="fls-card-badges">{badge}</div>
    </div>
    <div class="fls-card-actions fls-card-primary-action">
        <a class="btn btn-primary" href="{h(primary_url)}">{primary_label}</a>
    </div>
    <details class="detail-disclosure">
        <summary>查看脚本详情</summary>
        <div class="fls-card-body">
            <div class="fls-info-grid">
                <div class="fls-info-item"><div class="fls-info-label">类型</div><div class="fls-info-value">{h(item_type)}</div></div>
                <div class="fls-info-item"><div class="fls-info-label">大小</div><div class="fls-info-value">{h(size_text)}</div></div>
                <div class="fls-info-item"><div class="fls-info-label">修改时间</div><div class="fls-info-value">{h(mtime)}</div></div>
                <div class="fls-info-item"><div class="fls-info-label">绝对路径</div><div class="fls-info-value">{h(str(item))}</div></div>
            </div>
        </div>
    </details>
    <div class="fls-card-actions">
        <div class="row-actions" aria-label="脚本 {h(item.name)} 行操作">
            <div class="row-actions-secondary">{secondary}</div>
            <div class="row-actions-danger">
                <form class="inline-form" method="post" action="{h(delete_url(rel))}">
                    <button class="btn btn-red" type="submit" onclick="return confirm('确定删除 {h(rel)} 吗？')">{danger_label}</button>
                </form>
            </div>
        </div>
    </div>
</article>
"""

    if not cards:
        return empty_state(
            "暂无脚本，请点击“新建脚本”添加。",
            '<a class="btn btn-primary" href="/pull/new">新建脚本</a>',
        )
    return cards


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


def build_repo_pull_command(url, target, proxy_id):
    git_bin = shutil.which("git")

    if not git_bin:
        raise RuntimeError("未安装 git，请先安装 git")

    return build_git_command_with_github_proxy(
        git_bin,
        proxy_id,
        ["clone", url, str(target)],
        verify=True,
    )


def fetch_file_bytes(url, proxy_id):
    real_url = github_proxy_url(url, proxy_id)

    r = requests.get(
        real_url,
        headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
        timeout=60,
        proxies=requests_proxy_dict(proxy_id),
    )
    r.raise_for_status()

    return r.content


def debug_task_for_script(rel, target, debug_id):
    return {
        "id": debug_id,
        "name": f"脚本调试：{target.name}",
        "command": "task " + shlex.quote(rel),
        "env": {},
        "proxy_id": "",
    }


def debug_command_info(task):
    return build_command(task)
