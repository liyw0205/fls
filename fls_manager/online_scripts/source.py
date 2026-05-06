import json
import threading
import uuid
from pathlib import Path

import requests

from ..config import load_config
from ..utils import now_str
from ..proxy import requests_proxy_dict, github_proxy_url
from .constants import (
    DEFAULT_ONLINE_SCRIPT_SOURCE,
    ONLINE_SCRIPT_CACHE_FILE,
    ONLINE_REFRESH_STATE,
)
from .logs import append_log, online_refresh_log_file
from .tasks import normalize_online_task_crons


def get_online_script_source():
    cfg = load_config()
    return str(
        cfg.get("online_script_source")
        or DEFAULT_ONLINE_SCRIPT_SOURCE
    ).strip()


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
    ONLINE_SCRIPT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

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


def start_refresh_thread(proxy_id=""):
    th = threading.Thread(
        target=refresh_worker,
        args=(proxy_id,),
        daemon=True,
        name="fls-online-scripts-refresh",
    )
    th.start()