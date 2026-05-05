import os
import json
import uuid
import time
import shutil
import threading
import subprocess
from pathlib import Path

import requests
from flask import Blueprint, request, redirect, url_for, abort, jsonify

from ..paths import DATA_DIR, SCRIPT_DIR, LOG_DIR
from ..config import load_config
from ..models import load_tasks, save_tasks
from ..scheduler import reload_scheduler, cron_to_trigger
from ..utils import h, now_str, safe_name
from ..ui.layout import layout
from ..ui.log_controls import log_controls
from ..logs import tail_file
from ..proxy import (
    proxy_select_options,
    requests_proxy_dict,
    apply_proxy_env,
    github_proxy_url,
)

bp = Blueprint("online_scripts", __name__)

DEFAULT_ONLINE_SCRIPT_SOURCE = "https://cdn.jsdelivr.net/gh/liyw0205/fls-scripts@main/index.json"
ONLINE_SCRIPT_CACHE_FILE = DATA_DIR / "online_scripts_cache.json"

ONLINE_INSTALL_RUNNING = {}
ONLINE_REFRESH_STATE = {
    "running": False,
    "message": "",
    "error": "",
    "updated_at": "",
    "log_file": "",
}


def get_online_script_source():
    cfg = load_config()
    return str(cfg.get("online_script_source") or DEFAULT_ONLINE_SCRIPT_SOURCE).strip()


def normalize_online_scripts(data):
    if not isinstance(data, list):
        raise RuntimeError("脚本源格式错误：根节点必须是数组")

    result = []

    for item in data:
        if not isinstance(item, dict):
            continue

        script_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        script_type = str(item.get("type", "")).strip().lower()
        link = str(item.get("link", "")).strip()
        link_name = str(item.get("link_name", "")).strip()

        if not script_id or not name or script_type not in ("raw", "repo") or not link:
            continue

        if not link_name:
            if script_type == "repo":
                link_name = Path(link.rstrip("/").replace(".git", "")).name or script_id
            else:
                link_name = Path(link.split("?", 1)[0]).name or script_id

        item["id"] = script_id
        item["name"] = name
        item["type"] = script_type
        item["link"] = link
        item["link_name"] = link_name.strip().strip("/")
        item["install"] = str(item.get("install", "") or "").strip()

        result.append(item)

    return result


def load_online_script_cache():
    if not ONLINE_SCRIPT_CACHE_FILE.exists():
        return []

    try:
        data = json.loads(ONLINE_SCRIPT_CACHE_FILE.read_text(encoding="utf-8"))
        return normalize_online_scripts(data)
    except Exception:
        return []


def save_online_script_cache(items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ONLINE_SCRIPT_CACHE_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_cache_text():
    try:
        if ONLINE_SCRIPT_CACHE_FILE.exists():
            return ONLINE_SCRIPT_CACHE_FILE.read_text(encoding="utf-8")
    except Exception:
        pass

    return ""


def fetch_online_scripts(proxy_id="", timeout=12):
    url = get_online_script_source()
    real_url = github_proxy_url(url, proxy_id)

    r = requests.get(
        real_url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
        proxies=requests_proxy_dict(proxy_id),
    )

    status_code = getattr(r, "status_code", 0)
    text = r.text or ""

    if status_code < 200 or status_code >= 300:
        preview = text[:500].replace("\n", "\\n")
        raise RuntimeError(
            f"脚本源请求失败，HTTP {status_code}。"
            f"请求地址：{real_url}。"
            f"返回内容预览：{preview}"
        )

    if not text.strip():
        raise RuntimeError(f"脚本源返回空内容。请求地址：{real_url}")

    try:
        data = json.loads(text)
    except Exception as e:
        preview = text[:800].replace("\n", "\\n")
        ctype = r.headers.get("Content-Type", "")

        raise RuntimeError(
            f"脚本源不是合法 JSON：{e}。"
            f"请求地址：{real_url}。"
            f"Content-Type：{ctype or '-'}。"
            f"返回内容预览：{preview}"
        )

    items = normalize_online_scripts(data)
    save_online_script_cache(items)

    return items


def online_refresh_log_file(refresh_id):
    return LOG_DIR / f"online-script-source-refresh-{refresh_id}.log"


def append_log(log_file, text):
    try:
        with open(log_file, "ab") as f:
            f.write(str(text).encode("utf-8", errors="replace"))
            if not str(text).endswith("\n"):
                f.write(b"\n")
    except Exception:
        pass


def refresh_worker(proxy_id=""):
    refresh_id = uuid.uuid4().hex
    log_file = online_refresh_log_file(refresh_id)

    ONLINE_REFRESH_STATE.update({
        "running": True,
        "message": "正在后台拉取脚本源，请稍候...",
        "error": "",
        "updated_at": now_str(),
        "log_file": str(log_file),
    })

    append_log(log_file, "===== 在线脚本源刷新 =====")
    append_log(log_file, f"时间: {now_str()}")
    append_log(log_file, f"脚本源: {get_online_script_source()}")
    append_log(log_file, f"代理ID: {proxy_id or '不使用代理'}")
    append_log(log_file, "============================================================")

    try:
        items = fetch_online_scripts(proxy_id=proxy_id, timeout=20)
        msg = f"远程脚本源刷新成功，共 {len(items)} 条"

        append_log(log_file, msg)

        ONLINE_REFRESH_STATE.update({
            "running": False,
            "message": msg,
            "error": "",
            "updated_at": now_str(),
            "log_file": str(log_file),
        })

    except Exception as e:
        err = f"远程脚本源刷新失败：{e}"

        append_log(log_file, err)

        ONLINE_REFRESH_STATE.update({
            "running": False,
            "message": "",
            "error": err,
            "updated_at": now_str(),
            "log_file": str(log_file),
        })


def get_online_script(script_id):
    for item in load_online_script_cache():
        if item.get("id") == script_id:
            return item

    return None


def online_script_target(item):
    link_name = str(item.get("link_name") or item.get("id") or "script").strip().strip("/")

    if not link_name or link_name in (".", ".."):
        raise RuntimeError("link_name 非法")

    target = (SCRIPT_DIR / link_name).resolve()
    base = SCRIPT_DIR.resolve()

    if target != base and not str(target).startswith(str(base) + os.sep):
        raise RuntimeError("目标路径非法")

    return target


def online_install_log_file(install_id, script_name):
    safe_pkg = safe_name(script_name or "online-script")
    return LOG_DIR / f"online-script-install-{safe_pkg}-{install_id}.log"


def guess_task_command(item):
    task_cron = item.get("task_cron") or {}

    if isinstance(task_cron, dict):
        command = str(task_cron.get("command", "") or "").strip()
        if command:
            return command

    script_type = item.get("type")
    link_name = str(item.get("link_name") or "").strip().strip("/")

    if script_type == "raw":
        return f"task {link_name}"

    if script_type == "repo":
        return f"cd {link_name} && npm start"

    return ""


def import_task_if_needed(item, log_file=None):
    task_cron = item.get("task_cron")

    if not isinstance(task_cron, dict):
        msg = "脚本源未提供 task_cron，跳过任务导入"
        if log_file:
            append_log(log_file, msg)
        return False, msg

    name = str(task_cron.get("name") or item.get("name") or "").strip()
    cron_expr = str(task_cron.get("cron") or "").strip()
    command = guess_task_command(item)

    if not name:
        msg = "task_cron.name 为空，跳过任务导入"
        if log_file:
            append_log(log_file, msg)
        return False, msg

    if not command:
        msg = "无法推导任务命令，请在 task_cron.command 中显式指定"
        if log_file:
            append_log(log_file, msg)
        return False, msg

    if cron_expr:
        try:
            cron_to_trigger(cron_expr)
        except Exception as e:
            msg = f"Cron 不合法：{e}"
            if log_file:
                append_log(log_file, msg)
            return False, msg

    tasks = load_tasks()
    online_id = item.get("id")

    for task in tasks:
        if task.get("online_script_id") == online_id:
            msg = "任务已导入过，跳过"
            if log_file:
                append_log(log_file, msg)
            return False, msg

    task = {
        "id": uuid.uuid4().hex,
        "name": name,
        "remark": f"从在线脚本导入：{item.get('name')}",
        "command": command,
        "cron": cron_expr,
        "enabled": True,
        "env": {},
        "proxy_id": "",
        "notify": {"mode": "default", "ids": []},
        "random_delay": {"mode": "none", "seconds": 0},
        "run_count": 0,
        "online_script_id": online_id,
        "created_at": now_str(),
        "updated_at": now_str(),
    }

    tasks.append(task)
    save_tasks(tasks)
    reload_scheduler()

    msg = f"任务已导入：{name} / {cron_expr or '手动'} / {command}"
    if log_file:
        append_log(log_file, msg)

    return True, msg


def command_list_to_text(cmd):
    return " ".join(str(x) for x in cmd)


def run_logged_command(cmd, cwd, log_file, env=None, shell=False):
    append_log(log_file, "")
    append_log(log_file, f"$ {cmd if isinstance(cmd, str) else command_list_to_text(cmd)}")
    append_log(log_file, f"cwd: {cwd}")
    append_log(log_file, "------------------------------------------------------------")

    with open(log_file, "ab", buffering=0) as log_fp:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            cwd=str(cwd),
            env=env or os.environ.copy(),
            shell=shell,
        )

        return_code = proc.wait()

    append_log(log_file, "------------------------------------------------------------")
    append_log(log_file, f"命令结束，退出码：{return_code}")

    if return_code != 0:
        raise RuntimeError(f"命令执行失败，退出码：{return_code}")


def download_online_script_logged(item, proxy_id, log_file, force=False):
    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    script_type = item.get("type")
    link = item.get("link")
    target = online_script_target(item)

    append_log(log_file, "")
    append_log(log_file, "===== 下载/拉取脚本 =====")
    append_log(log_file, f"脚本类型: {script_type}")
    append_log(log_file, f"原始链接: {link}")
    append_log(log_file, f"保存目标: {target}")
    append_log(log_file, f"代理ID: {proxy_id or '不使用代理'}")
    append_log(log_file, f"允许覆盖/更新: {'是' if force else '否'}")
    append_log(log_file, "============================================================")

    if target.exists() and not force:
        raise FileExistsError(f"目标已存在，为避免意外覆盖已停止：{target}")

    if script_type == "raw":
        if target.exists() and target.is_dir():
            raise RuntimeError(f"目标已存在且是文件夹，无法覆盖为文件：{target}")

        target.parent.mkdir(parents=True, exist_ok=True)

        real_url = github_proxy_url(link, proxy_id)

        append_log(log_file, f"开始下载文件：{real_url}")

        r = requests.get(
            real_url,
            timeout=60,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
            proxies=requests_proxy_dict(proxy_id),
        )
        r.raise_for_status()

        total = int(r.headers.get("content-length", "0") or 0)
        downloaded = 0
        last_report = 0

        with open(target, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue

                f.write(chunk)
                downloaded += len(chunk)

                now_ts = time.time()
                if now_ts - last_report >= 1:
                    if total > 0:
                        percent = downloaded / total * 100
                        append_log(log_file, f"下载进度：{downloaded}/{total} bytes，{percent:.1f}%")
                    else:
                        append_log(log_file, f"下载进度：{downloaded} bytes")
                    last_report = now_ts

        append_log(log_file, f"文件下载完成：{target}")

        try:
            if target.suffix.lower() in (".sh", ".bash"):
                target.chmod(target.stat().st_mode | 0o755)
                append_log(log_file, "已添加可执行权限")
        except Exception as e:
            append_log(log_file, f"添加可执行权限失败：{e}")

        return target

    if script_type == "repo":
        git_bin = shutil.which("git")
        if not git_bin:
            raise RuntimeError("未安装 git，无法拉取仓库")

        env = os.environ.copy()
        env = apply_proxy_env(env, proxy_id)

        real_link = github_proxy_url(link, proxy_id)

        if target.exists():
            if not (target / ".git").exists():
                raise RuntimeError(f"目标目录已存在且不是 git 仓库，请手动处理后重试：{target}")

            append_log(log_file, "目标 Git 仓库已存在，执行 git pull 更新")
            run_logged_command(
                [git_bin, "pull"],
                cwd=target,
                log_file=log_file,
                env=env,
                shell=False,
            )
        else:
            append_log(log_file, "目标目录不存在，执行 git clone")
            run_logged_command(
                [git_bin, "clone", real_link, str(target)],
                cwd=SCRIPT_DIR,
                log_file=log_file,
                env=env,
                shell=False,
            )

        append_log(log_file, f"仓库拉取/更新完成：{target}")
        return target

    raise RuntimeError("未知脚本类型")


def install_worker(install_id, item, proxy_id="", import_task=False, force=False):
    log_file = ONLINE_INSTALL_RUNNING[install_id]["log_file"]

    ONLINE_INSTALL_RUNNING[install_id]["running"] = True
    ONLINE_INSTALL_RUNNING[install_id]["status"] = "运行中"

    append_log(log_file, "===== 在线脚本下载安装 =====")
    append_log(log_file, f"时间: {now_str()}")
    append_log(log_file, f"安装ID: {install_id}")
    append_log(log_file, f"脚本ID: {item.get('id')}")
    append_log(log_file, f"脚本名称: {item.get('name')}")
    append_log(log_file, f"类型: {item.get('type')}")
    append_log(log_file, f"链接: {item.get('link')}")
    append_log(log_file, f"保存名: {item.get('link_name')}")
    append_log(log_file, f"脚本目录: {SCRIPT_DIR}")
    append_log(log_file, f"代理ID: {proxy_id or '不使用代理'}")
    append_log(log_file, f"导入任务: {'是' if import_task else '否'}")
    append_log(log_file, f"允许覆盖/更新: {'是' if force else '否'}")
    append_log(log_file, "============================================================")

    try:
        download_online_script_logged(
            item=item,
            proxy_id=proxy_id,
            log_file=log_file,
            force=force,
        )

        if import_task:
            append_log(log_file, "")
            append_log(log_file, "===== 导入任务 =====")
            import_task_if_needed(item, log_file=log_file)

        install_cmd = str(item.get("install") or "").strip()

        if install_cmd:
            append_log(log_file, "")
            append_log(log_file, "===== 执行安装命令 =====")
            append_log(log_file, f"安装命令: {install_cmd}")
            append_log(log_file, f"工作目录: {SCRIPT_DIR}")

            env = os.environ.copy()
            env = apply_proxy_env(env, proxy_id)

            run_logged_command(
                ["sh", "-lc", install_cmd],
                cwd=SCRIPT_DIR,
                log_file=log_file,
                env=env,
                shell=False,
            )
        else:
            append_log(log_file, "")
            append_log(log_file, "该脚本未提供 install 命令，跳过安装步骤")

        ONLINE_INSTALL_RUNNING[install_id]["running"] = False
        ONLINE_INSTALL_RUNNING[install_id]["status"] = "已完成"
        ONLINE_INSTALL_RUNNING[install_id]["returncode"] = 0

        append_log(log_file, "")
        append_log(log_file, f"===== 全部完成: {now_str()} =====")

    except Exception as e:
        ONLINE_INSTALL_RUNNING[install_id]["running"] = False
        ONLINE_INSTALL_RUNNING[install_id]["status"] = "失败"
        ONLINE_INSTALL_RUNNING[install_id]["returncode"] = 1
        ONLINE_INSTALL_RUNNING[install_id]["error"] = str(e)

        append_log(log_file, "")
        append_log(log_file, f"===== 失败: {now_str()} =====")
        append_log(log_file, f"错误: {e}")


def script_has_task(item):
    return isinstance(item.get("task_cron"), dict)


def script_has_install(item):
    return bool(str(item.get("install") or "").strip())


def render_online_script_rows(items):
    if not items:
        return """
<div class="fls-empty-card">
    <div style="font-size:34px;margin-bottom:8px;">📭</div>
    <div style="font-weight:900;font-size:16px;">暂无在线脚本</div>
    <div class="help" style="margin-top:6px;">
        请点击“刷新远程脚本源”，或进入“脚本源 JSON”手动粘贴缓存。
    </div>
</div>
"""

    cards = ""

    for item in items:
        task_cron = item.get("task_cron")
        has_task = script_has_task(item)
        has_install = script_has_install(item)

        target = "-"
        exists = False

        try:
            target_path = online_script_target(item)
            target = str(target_path)
            exists = target_path.exists()
        except Exception:
            pass

        type_badge = f'<span class="badge blue">{h(item.get("type"))}</span>'
        exists_badge = '<span class="badge orange">目标已存在</span>' if exists else '<span class="badge green">可安装</span>'
        task_badge = '<span class="badge blue">可导入任务</span>' if has_task else '<span class="badge gray">无任务</span>'
        install_badge = '<span class="badge orange">有安装命令</span>' if has_install else '<span class="badge gray">无安装命令</span>'

        cron_text = "-"
        command_text = "-"

        if has_task and isinstance(task_cron, dict):
            cron_text = task_cron.get("cron") or "手动"
            command_text = task_cron.get("command") or guess_task_command(item) or "-"

        proxy_options = proxy_select_options("")

        cards += f"""
<details class="fls-fold-card">
    <summary>
        <div class="fls-card-head">
            <div class="fls-card-main">
                <div class="fls-card-title-main">{h(item.get("name"))}</div>
                <div class="fls-card-sub">
                    ID：{h(item.get("id"))}<br>
                    保存名：{h(item.get("link_name"))}
                </div>
            </div>

            <div class="fls-card-badges">
                {type_badge}
                {exists_badge}
            </div>
        </div>
    </summary>

    <div class="fls-card-body">
        <div class="fls-info-grid">
            <div class="fls-info-item">
                <div class="fls-info-label">目标路径</div>
                <div class="fls-info-value">{h(target)}</div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">安装</div>
                <div class="fls-info-value">{install_badge}</div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">任务</div>
                <div class="fls-info-value">
                    {task_badge}
                    <div class="help" style="margin-top:4px;">Cron：{h(cron_text)}</div>
                </div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">任务命令</div>
                <div class="fls-info-value code-like">{h(command_text)}</div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">创建时间</div>
                <div class="fls-info-value">{h(item.get("created_at", "-"))}</div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">更新时间</div>
                <div class="fls-info-value">{h(item.get("updated_at", "-"))}</div>
            </div>
        </div>

        <div class="fls-card-section">
            <div class="fls-info-label">源地址</div>
            <div class="fls-info-value">
                <a href="{h(item.get("link"))}" target="_blank">{h(item.get("link"))}</a>
            </div>
        </div>

        <div class="fls-card-actions">
            <form method="post" action="/online-scripts/install/{h(item.get("id"))}">
                <div class="fls-action-line">
                    <select name="proxy_id">{proxy_options}</select>

                    <label class="fls-inline-check">
                        <input type="checkbox" name="import_task" value="1" {"checked" if has_task else ""} {"disabled" if not has_task else ""} style="width:auto;">
                        导入任务
                    </label>
                </div>

                <div class="fls-btn-line">
                    <a class="btn btn-blue" href="{h(item.get("link"))}" target="_blank">查看源</a>
                    <button class="btn btn-primary" type="submit">下载安装</button>
                </div>
            </form>
        </div>
    </div>
</details>
"""

    return cards


@bp.route("/online-scripts")
def online_scripts_page():
    source = get_online_script_source()
    items = load_online_script_cache()

    msg = request.args.get("msg", "").strip()
    err = request.args.get("err", "").strip()

    refresh_running = ONLINE_REFRESH_STATE.get("running")
    refresh_message = ONLINE_REFRESH_STATE.get("message", "")
    refresh_error = ONLINE_REFRESH_STATE.get("error", "")
    refresh_updated_at = ONLINE_REFRESH_STATE.get("updated_at", "")
    refresh_log = ONLINE_REFRESH_STATE.get("log_file", "")

    proxy_options = proxy_select_options("")

    refresh_status_html = f"""
<div class="card" id="onlineRefreshStatusCard" style="{"display:block;" if refresh_running or refresh_message or refresh_error else "display:none;"}">
    <div class="card-title">脚本源刷新状态</div>
    <div class="help" id="onlineRefreshStatusText">
        {"正在后台拉取中，请稍候..." if refresh_running else h(refresh_message or refresh_error or "")}<br>
        更新时间：{h(refresh_updated_at or "-")}<br>
        日志：{h(refresh_log or "-")}
    </div>
</div>
"""

    body = f"""
<div class="card">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap;">
        <div style="min-width:0;flex:1 1 360px;">
            <div class="card-title">在线脚本</div>
            <div class="help">
                默认读取本地缓存，不会因为脚本源网络问题卡住。<br>
                点击“刷新远程脚本源”后会后台拉取，页面不会变白，也不影响其它操作。
            </div>
            <div class="fls-source-code">{h(source)}</div>
        </div>

        <div class="action-row" style="justify-content:flex-end;">
            <form method="post" action="/online-scripts/refresh" class="action-row">
                <select name="proxy_id" style="width:auto;min-width:180px;">{proxy_options}</select>
                <button class="btn btn-primary" type="submit" id="onlineRefreshBtn">刷新远程脚本源</button>
            </form>

            <a class="btn btn-blue" href="{h(source)}" target="_blank">打开源地址</a>
            <a class="btn btn-orange" href="/online-scripts/source">脚本源 JSON</a>
            <a class="btn btn-gray" href="/config">修改源地址</a>
        </div>
    </div>
</div>

<div class="fls-summary-grid">
    <div class="fls-summary-item">
        <div class="fls-summary-label">缓存脚本数</div>
        <div class="fls-summary-num">{len(items)}</div>
    </div>

    <div class="fls-summary-item">
        <div class="fls-summary-label">可导入任务</div>
        <div class="fls-summary-num">{sum(1 for x in items if script_has_task(x))}</div>
    </div>

    <div class="fls-summary-item">
        <div class="fls-summary-label">有安装命令</div>
        <div class="fls-summary-num">{sum(1 for x in items if script_has_install(x))}</div>
    </div>
</div>

{"<div class='card'><div class='help' style='color:#18a058;font-weight:800;'>" + h(msg) + "</div></div>" if msg else ""}
{"<div class='card'><div class='help' style='color:#dc2626;font-weight:800;'>" + h(err) + "</div></div>" if err else ""}

{refresh_status_html}

<div class="card">
    <div class="card-title">脚本列表，本地缓存</div>
    <div class="help">
        缓存文件：{h(ONLINE_SCRIPT_CACHE_FILE)}
    </div>
    <br>

    <div class="fls-card-grid">
        {render_online_script_rows(items)}
    </div>
</div>

<script>
window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ = false;
window.__FLS_ONLINE_REFRESH_RELOADED__ = false;

function escapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c];
    }});
}}

async function updateOnlineRefreshStatus(){{
    try {{
        const res = await fetch("/api/online-scripts/refresh-status", {{cache:"no-store"}});
        const json = await res.json();

        const card = document.getElementById("onlineRefreshStatusCard");
        const text = document.getElementById("onlineRefreshStatusText");
        const btn = document.getElementById("onlineRefreshBtn");

        if(!card || !text) return;

        if(json.running){{
            window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ = true;
        }}

        if(json.running || json.message || json.error){{
            card.style.display = "block";

            let color = json.error ? "#dc2626" : "#18a058";
            let first = json.running ? "正在后台拉取中，请稍候..." : (json.message || json.error || "");

            text.innerHTML =
                "<span style='color:" + color + ";font-weight:900;'>" + escapeHtml(first) + "</span><br>" +
                "更新时间：" + escapeHtml(json.updated_at || "-") + "<br>" +
                "日志：" + escapeHtml(json.log_file || "-");

            if(btn){{
                btn.disabled = !!json.running;
                btn.textContent = json.running ? "正在拉取中..." : "刷新远程脚本源";
            }}
        }}else{{
            if(btn){{
                btn.disabled = false;
                btn.textContent = "刷新远程脚本源";
            }}
        }}

        if(
            window.__FLS_ONLINE_REFRESH_WAS_RUNNING__ &&
            !json.running &&
            !json.error &&
            json.message &&
            !window.__FLS_ONLINE_REFRESH_RELOADED__
        ){{
            window.__FLS_ONLINE_REFRESH_RELOADED__ = true;

            if(btn){{
                btn.textContent = "刷新成功，正在更新列表...";
            }}

            setTimeout(function(){{
                location.href = "/online-scripts";
            }}, 800);

            return;
        }}

        if(json.running){{
            setTimeout(updateOnlineRefreshStatus, 2000);
        }}

    }} catch(e) {{}}
}}

updateOnlineRefreshStatus();
</script>
"""
    return layout("在线脚本", "online_scripts", body)


@bp.route("/online-scripts/refresh", methods=["POST"])
def online_scripts_refresh():
    if ONLINE_REFRESH_STATE.get("running"):
        return redirect(url_for("online_scripts.online_scripts_page", msg="脚本源正在后台拉取中，请稍候"))

    proxy_id = request.form.get("proxy_id", "").strip()

    th = threading.Thread(
        target=refresh_worker,
        args=(proxy_id,),
        daemon=True,
        name="fls-online-scripts-refresh",
    )
    th.start()

    return redirect(url_for("online_scripts.online_scripts_page", msg="已提交后台刷新，正在拉取中"))


@bp.route("/api/online-scripts/refresh-status")
def api_online_refresh_status():
    return jsonify({
        "running": bool(ONLINE_REFRESH_STATE.get("running")),
        "message": ONLINE_REFRESH_STATE.get("message", ""),
        "error": ONLINE_REFRESH_STATE.get("error", ""),
        "updated_at": ONLINE_REFRESH_STATE.get("updated_at", ""),
        "log_file": ONLINE_REFRESH_STATE.get("log_file", ""),
    })


@bp.route("/online-scripts/source", methods=["GET", "POST"])
def online_scripts_source():
    msg = ""
    err = ""

    if request.method == "POST":
        text = request.form.get("json_text", "").strip()

        if not text:
            err = "JSON 内容不能为空"
        else:
            try:
                data = json.loads(text)
                items = normalize_online_scripts(data)
                save_online_script_cache(items)
                msg = f"脚本源 JSON 保存成功，共 {len(items)} 条"
            except Exception as e:
                err = f"脚本源 JSON 保存失败：{e}"

    cache_text = read_cache_text()

    if not cache_text:
        cache_text = "[]"

    body = f"""
<div class="card">
    <div class="card-title">脚本源 JSON</div>
    <div class="help">
        这里显示当前本地缓存的脚本源 JSON。<br>
        如果服务器无法访问远程源，可以手动复制远程 index.json 内容，粘贴到这里保存。<br>
        保存后“在线脚本”列表会直接使用这份缓存。
    </div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
</div>

{"<div class='card'><div class='help' style='color:#18a058;'>" + h(msg) + "</div></div>" if msg else ""}
{"<div class='card'><div class='help' style='color:#dc2626;'>" + h(err) + "</div></div>" if err else ""}

<form method="post">
<div class="card">
    <div class="card-title">查看 / 修改缓存 JSON</div>
    <textarea name="json_text" style="min-height:520px;">{h(cache_text)}</textarea>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存脚本源 JSON</button>
    <a class="btn btn-gray" href="/online-scripts">返回</a>
</div>
</form>
"""
    return layout("脚本源 JSON", "online_scripts", body)


@bp.route("/online-scripts/install/<script_id>", methods=["POST"])
def online_scripts_install(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    proxy_id = request.form.get("proxy_id", "").strip()
    import_task = request.form.get("import_task") == "1"
    force = request.form.get("force") == "1"

    try:
        target = online_script_target(item)
    except Exception as e:
        return layout("在线脚本安装失败", "online_scripts", f"""
<div class="card">
    <div class="card-title">目标路径非法</div>
    <div class="help">{h(e)}</div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回</a>
</div>
"""), 400

    if target.exists() and not force:
        proxy_options = proxy_select_options(proxy_id)
        has_task = script_has_task(item)

        body = f"""
<div class="card">
    <div class="card-title">目标已存在，请确认</div>
    <div class="help" style="color:#dc2626;">
        检测到同名文件或文件夹已经存在。为避免意外覆盖，已暂停操作。<br>
        目标路径：<b>{h(target)}</b>
    </div>
    <br>
    <div class="help">
        如果是 Git 仓库目录，继续后会执行 <code>git pull</code> 更新。<br>
        如果是 raw 文件，继续后会覆盖该文件。<br>
        如果目标是非 Git 文件夹，继续也不会强行覆盖，需要你手动处理。
    </div>
</div>

<div class="card">
    <form method="post" action="/online-scripts/install/{h(script_id)}">
        <input type="hidden" name="force" value="1">
        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id">{proxy_options}</select>
        </div>
        <br>
        <label>
            <input type="checkbox" name="import_task" value="1" {"checked" if import_task and has_task else ""} {"disabled" if not has_task else ""} style="width:auto;">
            导入任务
        </label>
        <br><br>
        <button class="btn btn-orange" type="submit" onclick="return confirm('确定继续吗？可能会覆盖文件或更新仓库。')">确认继续</button>
        <a class="btn btn-gray" href="/online-scripts">取消</a>
    </form>
</div>
"""
        return layout("目标已存在", "online_scripts", body)

    install_id = uuid.uuid4().hex
    log_file = online_install_log_file(install_id, item.get("name") or item.get("id"))

    ONLINE_INSTALL_RUNNING[install_id] = {
        "id": install_id,
        "script_id": script_id,
        "script_name": item.get("name"),
        "log_file": str(log_file),
        "running": True,
        "status": "准备中",
        "start_time": time.time(),
        "returncode": None,
        "error": "",
    }

    th = threading.Thread(
        target=install_worker,
        args=(install_id, dict(item), proxy_id, import_task, force),
        daemon=True,
        name=f"fls-online-install-{install_id[:8]}",
    )
    th.start()

    return redirect(url_for("online_scripts.online_install_log", install_id=install_id))


@bp.route("/online-scripts/log/<install_id>")
def online_install_log(install_id):
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        body = f"""
<div class="card">
    <div class="card-title">在线脚本日志</div>
    <div class="help">
        安装记录不存在或面板已重启。<br>
        可以到日志管理中查找 online-script-install-*.log。
    </div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
    <a class="btn btn-blue" href="/logs">查看日志管理</a>
</div>
"""
        return layout("在线脚本日志", "online_scripts", body)

    body = f"""
<div class="card">
    <div class="card-title">在线脚本下载安装日志：{h(info.get("script_name") or install_id)}</div>
    <div class="help">
        状态：<b id="installStatus">{h(info.get("status") or "-")}</b><br>
        日志文件：{h(info.get("log_file") or "-")}
    </div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
    <a class="btn btn-blue" href="/pull">脚本管理</a>
    <a class="btn btn-orange" href="/tasks">任务管理</a>
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

async function loadLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/online-scripts/log/{h(install_id)}?lines=1600", {{cache:"no-store"}});
        const json = await res.json();

        document.getElementById("installStatus").textContent = json.status || "-";

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
loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""
    return layout("在线脚本日志", "online_scripts", body)


@bp.route("/api/online-scripts/log/<install_id>")
def api_online_install_log(install_id):
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        return jsonify({
            "running": False,
            "status": "记录不存在或面板已重启",
            "log": "安装记录不存在或面板已重启。请到日志管理中查找 online-script-install-*.log。",
        })

    log_file = info.get("log_file", "")
    lines = int(request.args.get("lines", "1200") or 1200)

    return jsonify({
        "running": bool(info.get("running")),
        "status": info.get("status") or "-",
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "log_file": log_file,
        "log": tail_file(log_file, lines),
    })