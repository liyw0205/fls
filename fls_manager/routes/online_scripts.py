import os
import re
import json
import uuid
import time
import signal
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
from ..utils import h, now_str, safe_name, get_back_url
from ..ui.layout import layout
from ..ui.log_controls import log_controls
from ..logs import tail_file
from ..proxy import (
    proxy_select_options,
    requests_proxy_dict,
    apply_proxy_env,
    github_proxy_url,
    build_git_command_with_github_proxy,
)

bp = Blueprint("online_scripts", __name__)

DEFAULT_ONLINE_SCRIPT_SOURCE = "https://cdn.jsdelivr.net/gh/liyw0205/fls-scripts@main/index.json"
ONLINE_SCRIPT_CACHE_FILE = DATA_DIR / "online_scripts_cache.json"

ONLINE_INSTALL_RUNNING = {}
ONLINE_INSTALL_STOPPING = set()

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

def normalize_online_task_crons(data):
    """
    标准化 task_cron / task_link 返回的任务列表。

    支持：
    1. 单个 dict
    2. list[dict]
    3. {"tasks": list[dict]}
    4. {"task_cron": list[dict] 或 dict}
    """
    raw = data

    if isinstance(data, dict):
        if isinstance(data.get("tasks"), list):
            raw = data.get("tasks")
        elif "task_cron" in data:
            raw = data.get("task_cron")

    if isinstance(raw, dict):
        raw = [raw]

    if not isinstance(raw, list):
        return []

    result = []

    for item in raw:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or "").strip()
        cron = str(item.get("cron") or "").strip()
        command = str(item.get("command") or "").strip()

        if not name or not command:
            continue

        task = {
            "name": name,
            "cron": cron,
            "command": command,
        }

        for key in ("var", "vars", "env"):
            if key in item:
                task["var"] = item.get(key)
                break

        if "remark" in item:
            task["remark"] = str(item.get("remark") or "").strip()
        
        if "config_path" in item:
            task["config_path"] = str(item.get("config_path") or "").strip()

        if "enabled" in item:
            task["enabled"] = bool(item.get("enabled"))

        result.append(task)

    return result

def online_script_task_crons(item):
    return normalize_online_task_crons(item.get("task_cron"))
    
def online_task_cron_vars(task_cron):
    raw = None

    if isinstance(task_cron, dict):
        if "var" in task_cron:
            raw = task_cron.get("var")
        elif "vars" in task_cron:
            raw = task_cron.get("vars")
        elif "env" in task_cron:
            raw = task_cron.get("env")

    env = {}

    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k or "").strip()
            if key:
                env[key] = "" if v is None else str(v)
        return env

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                for k, v in item.items():
                    key = str(k or "").strip()
                    if key:
                        env[key] = "" if v is None else str(v)
                continue

            text = str(item or "").strip()
            if not text:
                continue

            if ":" in text:
                key, value = text.split(":", 1)
            elif "=" in text:
                key, value = text.split("=", 1)
            else:
                continue

            key = key.strip()
            value = value.strip()

            if key:
                env[key] = value

    return env


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
        item["doc_link"] = str(item.get("doc_link", "") or "").strip()
        item["task_link"] = str(item.get("task_link", "") or "").strip()

        task_crons = normalize_online_task_crons(item.get("task_cron"))
        if task_crons:
            item["task_cron"] = task_crons
        else:
            item.pop("task_cron", None)
            
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

def fetch_task_link_tasks(item, proxy_id="", timeout=12):
    """
    拉取 item.task_link 指向的任务 JSON。

    支持 task_link 返回：
    1. [...]
    2. {"tasks": [...]}
    3. {"task_cron": [...]}
    4. 单个任务 dict
    """
    task_link = str((item or {}).get("task_link") or "").strip()

    if not task_link:
        return []

    real_url = github_proxy_url(task_link, proxy_id)

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
            f"任务源请求失败，HTTP {status_code}。"
            f"请求地址：{real_url}。"
            f"返回内容预览：{preview}"
        )

    if not text.strip():
        raise RuntimeError(f"任务源返回空内容。请求地址：{real_url}")

    try:
        data = json.loads(text)
    except Exception as e:
        preview = text[:800].replace("\n", "\\n")
        ctype = r.headers.get("Content-Type", "")
        raise RuntimeError(
            f"任务源不是合法 JSON：{e}。"
            f"请求地址：{real_url}。"
            f"Content-Type：{ctype or '-'}。"
            f"返回内容预览：{preview}"
        )

    return normalize_online_task_crons(data)

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

    for item in items:
        task_link = str(item.get("task_link") or "").strip()

        if not task_link:
            continue

        try:
            linked_tasks = fetch_task_link_tasks(
                item,
                proxy_id=proxy_id,
                timeout=timeout,
            )

            if not linked_tasks:
                print(
                    f"[OnlineScripts] 任务源为空: "
                    f"{item.get('name')} task_link={task_link}"
                )
                continue

            old_tasks = normalize_online_task_crons(item.get("task_cron"))

            # 合并内置 task_cron 和外部 task_link 任务
            item["task_cron"] = old_tasks + linked_tasks

            print(
                f"[OnlineScripts] 已加载任务源: "
                f"{item.get('name')} task_link={task_link} tasks={len(linked_tasks)}"
            )

        except Exception as e:
            print(
                f"[OnlineScripts] 任务源加载失败: "
                f"{item.get('name')} task_link={task_link} error={e}"
            )

    save_online_script_cache(items)
    return items


def online_refresh_log_file(refresh_id):
    return LOG_DIR / f"online-script-source-refresh-{refresh_id}.log"


def online_install_log_file(install_id, script_name):
    safe_pkg = safe_name(script_name or "online-script")
    return LOG_DIR / f"online-script-install-{safe_pkg}-{install_id}.log"


def append_log(log_file, text):
    try:
        with open(log_file, "ab") as f:
            f.write(str(text).encode("utf-8", errors="replace"))
            if not str(text).endswith("\n"):
                f.write(b"\n")
    except Exception:
        pass


def get_running_install_by_script_id(script_id):
    script_id = str(script_id or "")

    for install_id, info in ONLINE_INSTALL_RUNNING.items():
        if info.get("script_id") == script_id and info.get("running"):
            return install_id, info

    return "", None


def online_install_should_stop(install_id):
    return install_id in ONLINE_INSTALL_STOPPING


def terminate_install_process(proc):
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


def request_stop_online_install(install_id):
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        return False, "安装记录不存在或面板已重启"

    if not info.get("running"):
        return False, "安装任务已结束"

    ONLINE_INSTALL_STOPPING.add(install_id)
    info["status"] = "停止中"
    info["error"] = "用户请求停止安装"

    proc = info.get("process")
    if proc:
        terminate_install_process(proc)

    log_file = info.get("log_file")
    if log_file:
        append_log(log_file, "")
        append_log(log_file, f"===== 用户请求停止安装: {now_str()} =====")

    return True, "已请求停止安装"


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


def guess_task_command(item):
    task_crons = online_script_task_crons(item)

    if task_crons:
        command = str(task_crons[0].get("command", "") or "").strip()
        if command:
            return command

    script_type = item.get("type")
    link_name = str(item.get("link_name") or "").strip().strip("/")

    if script_type == "raw":
        return f"task {link_name}"

    if script_type == "repo":
        return f"cd {link_name} && npm start"

    return ""


def import_task_if_needed(item, log_file=None, enable_task=False):
    task_crons = online_script_task_crons(item)

    if not task_crons:
        msg = "脚本源未提供 task_cron / task_link 任务，跳过任务导入"
        if log_file:
            append_log(log_file, msg)
        return False, msg

    tasks = load_tasks()
    online_id = item.get("id")

    imported = 0
    skipped = 0
    messages = []

    for idx, task_cron in enumerate(task_crons, 1):
        name = str(task_cron.get("name") or item.get("name") or "").strip()
        cron_expr = str(task_cron.get("cron") or "").strip()
        command = str(task_cron.get("command") or "").strip() or guess_task_command(item)
        task_env = online_task_cron_vars(task_cron)

        config_path = str(task_cron.get("config_path") or "").strip().strip("/")

        remark = str(
            task_cron.get("remark")
            or f"从在线脚本导入：{item.get('name')}"
        ).strip()

        if not name:
            msg = f"第 {idx} 个任务 task_cron.name 为空，跳过"
            skipped += 1
            messages.append(msg)
            if log_file:
                append_log(log_file, msg)
            continue

        if not command:
            msg = f"第 {idx} 个任务无法推导任务命令，请在 task_cron.command 中显式指定"
            skipped += 1
            messages.append(msg)
            if log_file:
                append_log(log_file, msg)
            continue

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                msg = f"第 {idx} 个任务 Cron 不合法：{e}"
                skipped += 1
                messages.append(msg)
                if log_file:
                    append_log(log_file, msg)
                continue

        online_task_key = f"{online_id}:{idx}:{name}"

        exists = False

        for task in tasks:
            if task.get("online_script_task_key") == online_task_key:
                exists = True
                break

            if len(task_crons) == 1 and task.get("online_script_id") == online_id:
                exists = True
                break

        if exists:
            msg = f"任务已导入过，跳过：{name}"
            skipped += 1
            messages.append(msg)
            if log_file:
                append_log(log_file, msg)
            continue

        task = {
            "id": uuid.uuid4().hex,
            "name": name,
            "remark": remark,
            "command": command,
            "cron": cron_expr,
            "enabled": bool(enable_task),
            "env": task_env,
            "proxy_id": "",
            "notify": {"mode": "default", "ids": []},
            "random_delay": {"mode": "none", "seconds": 0},
            "run_count": 0,
            "online_script_id": online_id,
            "online_script_task_key": online_task_key,
            "created_at": now_str(),
            "updated_at": now_str(),
        }

        if config_path:
            task["config_path"] = config_path

        tasks.append(task)
        imported += 1

        env_msg = f" / 变量 {len(task_env)} 个" if task_env else ""
        config_msg = f" / 配置 {config_path}" if config_path else ""
        status_msg = "启用" if enable_task else "禁用"

        msg = (
            f"任务已导入：{name} / "
            f"{cron_expr or '手动'} / "
            f"{command} / "
            f"{status_msg}"
            f"{env_msg}"
            f"{config_msg}"
        )

        messages.append(msg)

        if log_file:
            append_log(log_file, msg)

    if imported > 0:
        save_tasks(tasks)
        reload_scheduler()

    summary = f"任务导入完成：新增 {imported} 个，跳过 {skipped} 个"

    if log_file:
        append_log(log_file, summary)

    return imported > 0, summary + "\n" + "\n".join(messages)

def command_list_to_text(cmd):
    return " ".join(str(x) for x in cmd)


def run_logged_command(cmd, cwd, log_file, env=None, shell=False, install_id=""):
    append_log(log_file, "")
    append_log(log_file, f"$ {cmd if isinstance(cmd, str) else command_list_to_text(cmd)}")
    append_log(log_file, f"cwd: {cwd}")
    append_log(log_file, "------------------------------------------------------------")

    with open(log_file, "ab", buffering=0) as log_fp:
        popen_kwargs = {
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "cwd": str(cwd),
            "env": env or os.environ.copy(),
            "shell": shell,
        }

        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            cmd,
            **popen_kwargs,
        )

        if install_id and install_id in ONLINE_INSTALL_RUNNING:
            ONLINE_INSTALL_RUNNING[install_id]["process"] = proc

        while proc.poll() is None:
            if install_id and online_install_should_stop(install_id):
                append_log(log_file, "")
                append_log(log_file, "===== 检测到停止请求，正在结束当前安装命令 =====")
                terminate_install_process(proc)

                if install_id in ONLINE_INSTALL_RUNNING:
                    ONLINE_INSTALL_RUNNING[install_id]["process"] = None

                raise RuntimeError("安装已停止")

            time.sleep(0.5)

        return_code = proc.returncode

        if install_id and install_id in ONLINE_INSTALL_RUNNING:
            ONLINE_INSTALL_RUNNING[install_id]["process"] = None

    append_log(log_file, "------------------------------------------------------------")
    append_log(log_file, f"命令结束，退出码：{return_code}")

    if return_code != 0:
        raise RuntimeError(f"命令执行失败，退出码：{return_code}")


def download_online_script_logged(item, proxy_id, log_file, force=False, install_id=""):
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

    if install_id and online_install_should_stop(install_id):
        raise RuntimeError("安装已停止")

    if target.exists() and not force:
        raise FileExistsError(f"目标已存在，为避免意外覆盖已停止：{target}")

    if script_type == "raw":
        if target.exists() and target.is_dir():
            raise RuntimeError(f"目标已存在且是文件夹，无法覆盖为文件：{target}")

        target.parent.mkdir(parents=True, exist_ok=True)

        real_url = github_proxy_url(link, proxy_id, verify=True)

        if real_url == link:
            append_log(log_file, "GitHub 代理不可用或未启用，使用原始下载地址")
        else:
            append_log(log_file, f"使用 GitHub 代理下载地址：{real_url}")

        append_log(log_file, f"开始下载文件：{real_url}")

        r = requests.get(
            real_url,
            timeout=60,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
            proxies=requests_proxy_dict(proxy_id),
        )
        r.raise_for_status()

        with open(target, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if install_id and online_install_should_stop(install_id):
                    append_log(log_file, "")
                    append_log(log_file, "===== 检测到停止请求，已中断文件下载 =====")
                    raise RuntimeError("安装已停止")

                if chunk:
                    f.write(chunk)

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

        if target.exists():
            if not (target / ".git").exists():
                raise RuntimeError(f"目标目录已存在且不是 git 仓库，请手动处理后重试：{target}")

            append_log(log_file, "目标 Git 仓库已存在，执行 git pull 更新")

            git_cmd = build_git_command_with_github_proxy(
                git_bin,
                proxy_id,
                ["pull"],
                verify=True,
            )

            if len(git_cmd) > 1:
                append_log(log_file, "GitHub 代理可用，使用 git 临时配置方式更新仓库")
            else:
                append_log(log_file, "GitHub 代理不可用或未启用，直接执行原始 git pull")

            run_logged_command(
                git_cmd,
                cwd=target,
                log_file=log_file,
                env=env,
                shell=False,
                install_id=install_id,
            )
        else:
            append_log(log_file, "目标目录不存在，执行 git clone")

            git_cmd = build_git_command_with_github_proxy(
                git_bin,
                proxy_id,
                ["clone", link, str(target)],
                verify=True,
            )

            if len(git_cmd) > 1:
                append_log(log_file, "GitHub 代理可用，使用 git 临时配置方式 clone 仓库")
            else:
                append_log(log_file, "GitHub 代理不可用或未启用，直接执行原始 git clone")

            run_logged_command(
                git_cmd,
                cwd=SCRIPT_DIR,
                log_file=log_file,
                env=env,
                shell=False,
                install_id=install_id,
            )

        append_log(log_file, f"仓库拉取/更新完成：{target}")
        return target

    raise RuntimeError("未知脚本类型")


def install_worker(install_id, item, proxy_id="", import_task=False, force=False, enable_task=False):
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
    append_log(log_file, f"导入后启用任务: {'是' if enable_task else '否'}")
    append_log(log_file, f"允许覆盖/更新: {'是' if force else '否'}")
    append_log(log_file, "============================================================")

    try:
        download_online_script_logged(
            item=item,
            proxy_id=proxy_id,
            log_file=log_file,
            force=force,
            install_id=install_id,
        )

        if online_install_should_stop(install_id):
            raise RuntimeError("安装已停止")

        if import_task:
            append_log(log_file, "")
            append_log(log_file, "===== 导入任务 =====")
            import_task_if_needed(item, log_file=log_file, enable_task=enable_task)

        if online_install_should_stop(install_id):
            raise RuntimeError("安装已停止")

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
                install_id=install_id,
            )
        else:
            append_log(log_file, "")
            append_log(log_file, "该脚本未提供 install 命令，跳过安装步骤")

        ONLINE_INSTALL_RUNNING[install_id]["running"] = False
        ONLINE_INSTALL_RUNNING[install_id]["status"] = "已完成"
        ONLINE_INSTALL_RUNNING[install_id]["returncode"] = 0
        ONLINE_INSTALL_RUNNING[install_id]["process"] = None

        append_log(log_file, "")
        append_log(log_file, f"===== 全部完成: {now_str()} =====")

    except Exception as e:
        stopped = online_install_should_stop(install_id) or str(e) == "安装已停止"

        ONLINE_INSTALL_RUNNING[install_id]["running"] = False
        ONLINE_INSTALL_RUNNING[install_id]["status"] = "已停止" if stopped else "失败"
        ONLINE_INSTALL_RUNNING[install_id]["returncode"] = -1 if stopped else 1
        ONLINE_INSTALL_RUNNING[install_id]["error"] = str(e)
        ONLINE_INSTALL_RUNNING[install_id]["process"] = None

        append_log(log_file, "")

        if stopped:
            append_log(log_file, f"===== 已停止: {now_str()} =====")
        else:
            append_log(log_file, f"===== 失败: {now_str()} =====")
            append_log(log_file, f"错误: {e}")

    finally:
        ONLINE_INSTALL_STOPPING.discard(install_id)

        if install_id in ONLINE_INSTALL_RUNNING:
            ONLINE_INSTALL_RUNNING[install_id]["process"] = None


def script_has_task(item):
    return len(online_script_task_crons(item)) > 0


def script_has_install(item):
    return bool(str(item.get("install") or "").strip())


def script_has_doc(item):
    return bool(str(item.get("doc_link") or "").strip())


def task_vars_summary(task_crons):
    total = 0

    for tc in task_crons:
        total += len(online_task_cron_vars(tc))

    return total


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
        item_id = item.get("id")
        task_crons = online_script_task_crons(item)
        has_task = script_has_task(item)
        has_install = script_has_install(item)
        has_doc = script_has_doc(item)
        vars_count = task_vars_summary(task_crons)

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
        task_badge = f'<span class="badge blue">可导入 {len(task_crons)} 个任务</span>' if has_task else '<span class="badge gray">无任务</span>'
        install_badge = '<span class="badge orange">有安装命令</span>' if has_install else '<span class="badge gray">无安装命令</span>'
        doc_badge = '<span class="badge blue">有文档</span>' if has_doc else '<span class="badge gray">无文档</span>'
        var_badge = f'<span class="badge orange">预设变量 {vars_count} 个</span>' if vars_count else '<span class="badge gray">无预设变量</span>'

        cron_text = "-"
        command_text = "-"
        vars_text = "-"

        if task_crons:
            cron_parts = []
            command_parts = []
            var_parts = []

            for idx, tc in enumerate(task_crons, 1):
                tname = str(tc.get("name") or f"任务{idx}").strip()
                tcron = str(tc.get("cron") or "手动").strip()
                tcmd = str(tc.get("command") or guess_task_command(item) or "-").strip()
                tenv = online_task_cron_vars(tc)

                cron_parts.append(f"{tname}：{tcron}")
                command_parts.append(f"{tname}：{tcmd}")

                if tenv:
                    kv = "，".join([f"{k}={v}" for k, v in tenv.items()])
                    var_parts.append(f"{tname}：{kv}")

            cron_text = "\n".join(cron_parts)
            command_text = "\n".join(command_parts)
            vars_text = "\n".join(var_parts) if var_parts else "-"

        proxy_options = proxy_select_options("")

        doc_btn = ""
        if has_doc:
            doc_btn = f'<a class="btn btn-orange" href="/online-scripts/doc/{h(item_id)}">查看文档</a>'

        running_install_id, running_install_info = get_running_install_by_script_id(item_id)

        if running_install_id:
            install_action_html = f"""
<form method="post" action="/online-scripts/install-stop/{h(running_install_id)}">
    <div class="help" style="margin-bottom:10px;color:#f59e0b;font-weight:800;">
        当前脚本正在安装，状态：{h((running_install_info or {}).get("status") or "运行中")}
    </div>

    <div class="fls-btn-line">
        <a class="btn btn-blue" href="{h(item.get("link"))}" target="_blank">查看源</a>
        {doc_btn}
        <a class="btn btn-orange" href="/online-scripts/log/{h(running_install_id)}?back=/online-scripts">查看日志</a>
        <button class="btn btn-red" type="submit" onclick="return confirm('确定停止该安装任务吗？')">停止安装</button>
    </div>
</form>
"""
        else:
            install_action_html = f"""
<form method="post" action="/online-scripts/install/{h(item_id)}">
    <div class="fls-action-line">
        <select name="proxy_id">{proxy_options}</select>

        <div class="fls-check-group">
            <label class="fls-inline-check">
                <input type="checkbox" name="import_task" value="1" {"checked" if has_task else ""} {"disabled" if not has_task else ""} style="width:auto;">
                导入任务
            </label>

            <label class="fls-inline-check">
                <input type="checkbox" name="enable_task" value="1" {"disabled" if not has_task else ""} style="width:auto;">
                启用任务
            </label>
        </div>
    </div>

    <div class="help" style="margin:6px 0 10px;">
        提示：不勾选“启用任务”时，导入后的任务默认为禁用，需要到任务管理中手动启用。
    </div>

    <div class="fls-btn-line">
        <a class="btn btn-blue" href="{h(item.get("link"))}" target="_blank">查看源</a>
        {doc_btn}
        <button class="btn btn-primary" type="submit">下载安装</button>
    </div>
</form>
"""

        doc_section = ""
        if has_doc:
            doc_section = (
                "<div class='fls-card-section'>"
                "<div class='fls-info-label'>文档地址</div>"
                "<div class='fls-info-value'>"
                f"<a href='{h(item.get('doc_link'))}' target='_blank'>{h(item.get('doc_link'))}</a>"
                "</div></div>"
            )

        cards += f"""
<details class="fls-fold-card">
    <summary>
        <div class="fls-card-head">
            <div class="fls-card-main">
                <div class="fls-card-title-main">{h(item.get("name"))}</div>
                <div class="fls-card-sub">
                    ID：{h(item_id)}<br>
                    保存名：{h(item.get("link_name"))}
                </div>
            </div>

            <div class="fls-card-badges">
                {type_badge}
                {exists_badge}
                {doc_badge}
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
                <div class="fls-info-label">安装 / 文档</div>
                <div class="fls-info-value">
                    {install_badge}
                    {doc_badge}
                </div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">任务</div>
                <div class="fls-info-value">
                    {task_badge}
                    <pre style="margin:6px 0 0;white-space:pre-wrap;word-break:break-word;background:transparent;padding:0;font-family:inherit;font-size:12px;color:#6b7280;">{h(cron_text)}</pre>
                </div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">预设任务变量</div>
                <div class="fls-info-value">
                    {var_badge}
                    <pre style="margin:6px 0 0;white-space:pre-wrap;word-break:break-word;background:transparent;padding:0;font-family:inherit;font-size:12px;color:#6b7280;">{h(vars_text)}</pre>
                </div>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">任务命令</div>
                <pre class="fls-info-value code-like" style="margin:0;white-space:pre-wrap;">{h(command_text)}</pre>
            </div>

            <div class="fls-info-item">
                <div class="fls-info-label">创建 / 更新</div>
                <div class="fls-info-value">
                    {h(item.get("created_at", "-"))}<br>
                    {h(item.get("updated_at", "-"))}
                </div>
            </div>
        </div>

        <div class="fls-card-section">
            <div class="fls-info-label">源地址</div>
            <div class="fls-info-value">
                <a href="{h(item.get("link"))}" target="_blank">{h(item.get("link"))}</a>
            </div>
        </div>

        {doc_section}

        <div class="fls-card-actions">
            {install_action_html}
        </div>
    </div>
</details>
"""

    return cards


def markdown_inline(text):
    text = h(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"!\[([^\]]*)\]\((https?://[^)]+)\)", r'<img alt="\1" src="\2" style="max-width:100%;border-radius:10px;margin:8px 0;">', text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2" target="_blank">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return text


def render_markdown_to_html(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    out = []
    in_code = False
    code_lines = []
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol

        if in_ul:
            out.append("</ul>")
            in_ul = False

        if in_ol:
            out.append("</ol>")
            in_ol = False

    def flush_code():
        nonlocal code_lines
        out.append('<pre class="fls-md-code"><code>{}</code></pre>'.format(h("\n".join(code_lines))))
        code_lines = []

    for line in lines:
        raw = line.rstrip("\n")

        if raw.strip().startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                close_lists()
                in_code = True
                code_lines = []
            continue

        if in_code:
            code_lines.append(raw)
            continue

        if not raw.strip():
            close_lists()
            out.append("")
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", raw)
        if m:
            close_lists()
            level = len(m.group(1))
            out.append(f"<h{level}>{markdown_inline(m.group(2).strip())}</h{level}>")
            continue

        if re.match(r"^\s*[-*_]{3,}\s*$", raw):
            close_lists()
            out.append("<hr>")
            continue

        m = re.match(r"^\s*[-*+]\s+(.+)$", raw)
        if m:
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{markdown_inline(m.group(1).strip())}</li>")
            continue

        m = re.match(r"^\s*\d+\.\s+(.+)$", raw)
        if m:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{markdown_inline(m.group(1).strip())}</li>")
            continue

        m = re.match(r"^\s*>\s*(.*)$", raw)
        if m:
            close_lists()
            out.append(f"<blockquote>{markdown_inline(m.group(1).strip())}</blockquote>")
            continue

        close_lists()
        out.append(f"<p>{markdown_inline(raw.strip())}</p>")

    if in_code:
        flush_code()

    close_lists()

    return "\n".join(out)


def doc_url_looks_markdown(url):
    u = str(url or "").lower().split("?", 1)[0]
    return u.endswith((".md", ".markdown", ".mdown", ".mkd", ".txt"))


def doc_content_looks_markdown(text):
    text = str(text or "")
    sample = text[:4000]

    patterns = [
        r"^#\s+",
        r"^##\s+",
        r"```",
        r"^\s*[-*+]\s+",
        r"^\s*\d+\.\s+",
        r"\[[^\]]+\]\(https?://",
    ]

    for p in patterns:
        if re.search(p, sample, re.M):
            return True

    return False


def doc_response_is_html(resp, text):
    ctype = str(resp.headers.get("Content-Type", "") or "").lower()

    if "text/html" in ctype:
        return True

    s = str(text or "").lstrip().lower()

    return s.startswith("<!doctype html") or s.startswith("<html") or "<body" in s[:1000]


def doc_response_is_text(resp):
    ctype = str(resp.headers.get("Content-Type", "") or "").lower()

    if not ctype:
        return True

    return (
        "text/" in ctype
        or "json" in ctype
        or "xml" in ctype
        or "markdown" in ctype
        or "javascript" in ctype
    )


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
                点击“刷新远程脚本源”后会后台拉取，页面不会变白，也不影响其它操作。<br>
                脚本源支持 <code>doc_link</code> 字段，可在面板内查看 Markdown 文档或网页文档。<br>
                <code>task_cron.var</code> 可预设任务变量，导入任务时会自动写入任务变量。
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
        <div class="fls-summary-num">{sum(len(online_script_task_crons(x)) for x in items)}</div>
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


@bp.route("/online-scripts/doc/<script_id>")
def online_script_doc(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    doc_link = str(item.get("doc_link") or "").strip()

    if not doc_link:
        body = """
<div class="card">
    <div class="card-title">脚本文档</div>
    <div class="help">该脚本未提供 doc_link。</div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
</div>
"""
        return layout("脚本文档", "online_scripts", body)

    proxy_id = request.args.get("proxy_id", "").strip()
    mode = request.args.get("mode", "auto").strip().lower()

    if mode not in ("auto", "render", "web", "raw"):
        mode = "auto"

    proxy_options = proxy_select_options(proxy_id)
    real_url = github_proxy_url(doc_link, proxy_id, verify=True)

    doc_text = ""
    doc_html = ""
    content_type = "-"
    detected = "-"
    err = ""

    if mode == "web":
        detected = "网页窗口"
        doc_html = f"""
<div class="fls-doc-window">
    <iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe>
</div>
<div class="help" style="margin-top:10px;">
    如果网页无法嵌入显示，可能是对方网站禁止 iframe。请点击“打开原文”。
</div>
"""
    else:
        try:
            r = requests.get(
                real_url,
                timeout=25,
                headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
                proxies=requests_proxy_dict(proxy_id),
            )
            r.raise_for_status()

            content_type = r.headers.get("Content-Type", "-")
            doc_text = r.text or ""

            is_html = doc_response_is_html(r, doc_text)
            is_text = doc_response_is_text(r)
            is_md = doc_url_looks_markdown(doc_link) or "markdown" in str(content_type).lower() or doc_content_looks_markdown(doc_text)

            if mode == "raw":
                detected = "原文源码"
                doc_html = f'<pre class="fls-doc-raw">{h(doc_text or "暂无内容")}</pre>'

            elif mode == "render":
                if is_md:
                    detected = "Markdown 渲染"
                    doc_html = f'<div class="fls-doc-md">{render_markdown_to_html(doc_text)}</div>'
                elif is_text:
                    detected = "文本渲染"
                    doc_html = f'<pre class="fls-doc-raw">{h(doc_text or "暂无内容")}</pre>'
                else:
                    detected = "网页窗口"
                    doc_html = f'<div class="fls-doc-window"><iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe></div>'

            else:
                if is_html and not doc_url_looks_markdown(doc_link):
                    detected = "网页窗口"
                    doc_html = f"""
<div class="fls-doc-window">
    <iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe>
</div>
<div class="help" style="margin-top:10px;">
    已自动识别为网页。如果无法显示，请点击“打开原文”。
</div>
"""
                elif is_md:
                    detected = "Markdown 渲染"
                    doc_html = f'<div class="fls-doc-md">{render_markdown_to_html(doc_text)}</div>'
                elif is_text:
                    detected = "文本渲染"
                    doc_html = f'<pre class="fls-doc-raw">{h(doc_text or "暂无内容")}</pre>'
                else:
                    detected = "网页窗口"
                    doc_html = f"""
<div class="fls-doc-window">
    <iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe>
</div>
<div class="help" style="margin-top:10px;">
    已自动识别为网页或非文本内容。如果无法显示，请点击“打开原文”。
</div>
"""

        except Exception as e:
            err = str(e)

            if mode == "auto":
                detected = "请求失败，尝试网页窗口"
                doc_html = f"""
<div class="fls-doc-window">
    <iframe src="{h(real_url)}" class="fls-doc-iframe"></iframe>
</div>
<div class="help" style="margin-top:10px;">
    文档内容拉取失败，已尝试用网页窗口打开。若仍无法显示，请点击“打开原文”。
</div>
"""

    body = f"""
<style>
.fls-doc-toolbar {{
    display:flex;
    flex-wrap:wrap;
    gap:8px;
    align-items:center;
}}

.fls-doc-toolbar select {{
    width:auto;
    min-width:180px;
}}

.fls-doc-toolbar .btn {{
    margin:0;
}}

.fls-doc-window {{
    width:100%;
    height:calc(100vh - 220px);
    min-height:620px;
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:14px;
    overflow:hidden;
}}

.fls-doc-iframe {{
    width:100%;
    height:100%;
    border:0;
    background:#fff;
}}

.fls-doc-md {{
    background:#fff;
    border:1px solid #e5e7eb;
    border-radius:14px;
    padding:18px;
    line-height:1.75;
    color:#111827;
    overflow:auto;
}}

.fls-doc-md h1,
.fls-doc-md h2,
.fls-doc-md h3,
.fls-doc-md h4,
.fls-doc-md h5,
.fls-doc-md h6 {{
    margin:18px 0 10px;
    line-height:1.35;
    color:#111827;
}}

.fls-doc-md h1 {{
    font-size:28px;
    border-bottom:1px solid #e5e7eb;
    padding-bottom:10px;
}}

.fls-doc-md h2 {{
    font-size:23px;
    border-bottom:1px solid #f1f5f9;
    padding-bottom:8px;
}}

.fls-doc-md h3 {{
    font-size:19px;
}}

.fls-doc-md p {{
    margin:10px 0;
}}

.fls-doc-md ul,
.fls-doc-md ol {{
    padding-left:24px;
}}

.fls-doc-md li {{
    margin:5px 0;
}}

.fls-doc-md blockquote {{
    margin:12px 0;
    padding:8px 12px;
    border-left:4px solid #18a058;
    background:#f0fdf4;
    color:#374151;
    border-radius:8px;
}}

.fls-doc-md code {{
    background:#f3f4f6;
    color:#dc2626;
    padding:2px 5px;
    border-radius:6px;
    font-family:Consolas,Menlo,monospace;
}}

.fls-md-code {{
    background:#0b1020;
    color:#d1d5db;
    border-radius:12px;
    padding:14px;
    overflow:auto;
    white-space:pre;
}}

.fls-md-code code {{
    background:transparent;
    color:inherit;
    padding:0;
}}

.fls-doc-raw {{
    background:#0b1020;
    color:#d1d5db;
    border-radius:14px;
    padding:16px;
    min-height:620px;
    white-space:pre-wrap;
    word-break:break-word;
    overflow:auto;
    font-family:Consolas,Menlo,monospace;
    font-size:13px;
    line-height:1.55;
}}

body.fls-mobile .fls-doc-window {{
    height:calc(100vh - 190px);
    min-height:520px;
    border-radius:12px;
}}

body.fls-mobile .fls-doc-md {{
    padding:13px;
    border-radius:12px;
}}

body.fls-mobile .fls-doc-md h1 {{
    font-size:23px;
}}

body.fls-mobile .fls-doc-md h2 {{
    font-size:20px;
}}

body.fls-mobile .fls-doc-raw {{
    min-height:520px;
    font-size:12px;
}}
</style>

<div class="card">
    <div class="card-title">脚本文档：{h(item.get("name") or script_id)}</div>
    <div class="help">
        脚本 ID：{h(item.get("id"))}<br>
        识别结果：<b>{h(detected)}</b><br>
        Content-Type：{h(content_type)}<br>
        文档地址：<a href="{h(doc_link)}" target="_blank">{h(doc_link)}</a><br>
        实际地址：<a href="{h(real_url)}" target="_blank">{h(real_url)}</a>
    </div>
    <br>

    <form method="get" class="fls-doc-toolbar">
        <select name="proxy_id">{proxy_options}</select>
        <select name="mode">
            <option value="auto" {"selected" if mode == "auto" else ""}>自动识别</option>
            <option value="render" {"selected" if mode == "render" else ""}>渲染 Markdown / 文本</option>
            <option value="web" {"selected" if mode == "web" else ""}>网页窗口</option>
            <option value="raw" {"selected" if mode == "raw" else ""}>原文源码</option>
        </select>
        <button class="btn btn-primary" type="submit">重新加载</button>
        <a class="btn btn-blue" href="{h(real_url)}" target="_blank">打开原文</a>
        <a class="btn btn-gray" href="/online-scripts">返回在线脚本</a>
    </form>
</div>

{"<div class='card'><div class='help' style='color:#dc2626;font-weight:800;'>文档加载失败：" + h(err) + "</div></div>" if err else ""}

<div class="card">
    {doc_html or '<div class="help">暂无文档内容</div>'}
</div>
"""
    return layout("脚本文档", "online_scripts", body)


@bp.route("/online-scripts/refresh", methods=["POST"])
def online_scripts_refresh():
    if ONLINE_REFRESH_STATE.get("running"):
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg="脚本源正在后台拉取中，请稍候",
            )
        )

    proxy_id = request.form.get("proxy_id", "").strip()

    th = threading.Thread(
        target=refresh_worker,
        args=(proxy_id,),
        daemon=True,
        name="fls-online-scripts-refresh",
    )
    th.start()

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
            msg="已提交后台刷新，正在拉取中",
        )
    )


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

    cache_text = read_cache_text() or "[]"

    body = f"""
<div class="card">
    <div class="card-title">脚本源 JSON</div>
    <div class="help">
        这里显示当前本地缓存的脚本源 JSON。<br>
        如果服务器无法访问远程源，可以手动复制远程 index.json 内容，粘贴到这里保存。<br>
        保存后“在线脚本”列表会直接使用这份缓存。<br>
        支持字段：<code>doc_link</code>，可用于在线脚本页面查看文档。<br>
        支持字段：<code>task_cron.var</code>，可预设任务变量。
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

    running_install_id, _ = get_running_install_by_script_id(script_id)
    if running_install_id:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                msg="该脚本正在安装中",
            )
        )

    proxy_id = request.form.get("proxy_id", "").strip()
    import_task = request.form.get("import_task") == "1"
    enable_task = request.form.get("enable_task") == "1"
    force = request.form.get("force") == "1"

    if not import_task:
        enable_task = False

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

        <div class="fls-check-group">
            <label class="fls-inline-check">
                <input type="checkbox" name="import_task" value="1" {"checked" if import_task and has_task else ""} {"disabled" if not has_task else ""} style="width:auto;">
                导入任务
            </label>

            <label class="fls-inline-check">
                <input type="checkbox" name="enable_task" value="1" {"checked" if enable_task and has_task else ""} {"disabled" if not has_task else ""} style="width:auto;">
                启用任务
            </label>
        </div>

        <div class="help" style="margin-top:8px;">
            提示：不勾选“启用任务”时，导入后的任务默认为禁用。
        </div>

        <br>

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
        "process": None,
    }

    th = threading.Thread(
        target=install_worker,
        args=(install_id, dict(item), proxy_id, import_task, force, enable_task),
        daemon=True,
        name=f"fls-online-install-{install_id[:8]}",
    )
    th.start()

    return redirect(
        url_for(
            "online_scripts.online_install_log",
            install_id=install_id,
            back="/online-scripts",
        )
    )


@bp.route("/online-scripts/install-stop/<install_id>", methods=["POST"])
def online_scripts_install_stop(install_id):
    ok, msg = request_stop_online_install(install_id)

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
            msg=msg if ok else "",
            err="" if ok else msg,
        )
    )


@bp.route("/online-scripts/log/<install_id>")
def online_install_log(install_id):
    back_url = get_back_url("/online-scripts")
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        body = """
<div class="card">
    <div class="card-title">在线脚本日志</div>
    <div class="help">
        安装记录不存在或面板已重启。<br>
        可以到日志管理中查找 online-script-install-*.log。
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/logs?back={h(back_url)}">查看日志管理</a>
</div>
"""
        return layout("在线脚本日志", "online_scripts", body)

    stop_install_button = ""

    if info.get("running"):
        stop_install_button = f"""
<form method="post" action="/online-scripts/install-stop/{h(install_id)}" style="display:inline;">
    <button class="btn btn-red" type="submit" onclick="return confirm('确定停止该安装任务吗？')">停止安装</button>
</form>
"""

    body = f"""
<div class="card">
    <div class="card-title">在线脚本下载安装日志：{h(info.get("script_name") or install_id)}</div>
    <div class="help">
        状态：<b id="installStatus">{h(info.get("status") or "-")}</b><br>
        日志文件：{h(info.get("log_file") or "-")}
    </div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/pull">脚本管理</a>
    <a class="btn btn-orange" href="/tasks">任务管理</a>
    {stop_install_button}
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