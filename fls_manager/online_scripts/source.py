import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import closing
from pathlib import Path

from ..config import load_config
from ..storage import mutate_json, storage_transaction
from ..utils import now_str, LazyModule
from ..proxy import requests_proxy_dict, github_proxy_url, validate_proxy_id
from .constants import (
    DEFAULT_ONLINE_SCRIPT_SOURCE,
    ONLINE_SCRIPT_CACHE_FILE,
    ONLINE_TASK_LINK_REQUESTS_FILE,
    ONLINE_REFRESH_LOCK,
    ONLINE_REFRESH_STATE,
    update_online_refresh_state,
)
from .logs import append_log, online_refresh_log_file
from .tasks import normalize_online_task_crons, online_script_task_crons

requests = LazyModule("requests")


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


def validate_online_script_source_items(data):
    """Validate a remote source before allowing it to replace the cache."""
    if not isinstance(data, list):
        raise RuntimeError("脚本源格式错误：根节点必须是数组")

    items = normalize_online_scripts(data)
    if len(items) != len(data):
        raise RuntimeError("脚本源包含无效条目，未发布不完整缓存")

    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        raise RuntimeError("脚本源包含重复 ID，未发布不确定缓存")

    return items


def load_online_script_cache():
    if not ONLINE_SCRIPT_CACHE_FILE.exists():
        return []

    if ONLINE_SCRIPT_CACHE_FILE.is_symlink():
        raise RuntimeError("在线脚本源缓存不能是符号链接，未覆盖原文件")

    try:
        data = json.loads(ONLINE_SCRIPT_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"在线脚本源缓存损坏或无法读取，未覆盖原文件：{exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("在线脚本源缓存损坏或无法读取，未覆盖原文件：根节点必须是数组")
    try:
        return normalize_online_scripts(data)
    except Exception as exc:
        raise RuntimeError(f"在线脚本源缓存损坏或无法读取，未覆盖原文件：{exc}") from exc


def online_script_cache_fingerprint():
    """Return the exact current cache bytes used to bind an explicit retry."""
    if not ONLINE_SCRIPT_CACHE_FILE.exists():
        return hashlib.sha256(b"").hexdigest()
    if ONLINE_SCRIPT_CACHE_FILE.is_symlink():
        raise RuntimeError("在线脚本源缓存不能是符号链接，无法确认重试指纹")
    return hashlib.sha256(ONLINE_SCRIPT_CACHE_FILE.read_bytes()).hexdigest()


def online_script_cache_fingerprint_for_items(items):
    payload = json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def online_script_source_fingerprint():
    return hashlib.sha256(get_online_script_source().encode("utf-8")).hexdigest()


def save_online_script_cache(items):
    with storage_transaction():
        if ONLINE_SCRIPT_CACHE_FILE.is_symlink():
            raise RuntimeError("在线脚本源缓存不能是符号链接，未覆盖原文件")
        ONLINE_SCRIPT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_file = ONLINE_SCRIPT_CACHE_FILE.with_name(
            f".{ONLINE_SCRIPT_CACHE_FILE.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_file.write_text(
                json.dumps(items, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_file, ONLINE_SCRIPT_CACHE_FILE)
        finally:
            temp_file.unlink(missing_ok=True)


class OnlineScriptCacheConflict(RuntimeError):
    pass


def online_task_link_version(item):
    metadata = {key: value for key, value in (item or {}).items() if key != "task_cron"}
    payload = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_task_link_requests(data):
    if not isinstance(data, dict):
        raise RuntimeError("在线任务源同步回执损坏，未覆盖原文件")
    for request_id, value in data.items():
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise RuntimeError("在线任务源同步回执标识损坏，未覆盖原文件")
        if not isinstance(value, dict) or value.get("request_id") != request_id:
            raise RuntimeError("在线任务源同步回执标识不一致，未覆盖原文件")
        if value.get("state") not in ("prepared", "running", "failed", "unknown", "completed"):
            raise RuntimeError("在线任务源同步回执状态损坏，未覆盖原文件")
        for field in ("script_id", "proxy_id", "expected_version", "error", "updated_at"):
            if not isinstance(value.get(field, ""), str):
                raise RuntimeError("在线任务源同步回执字段损坏，未覆盖原文件")
    return data


def _task_link_request_snapshot(info):
    fields = (
        "request_id", "script_id", "proxy_id", "expected_version", "state",
        "added", "total", "error", "updated_at",
    )
    snapshot = {field: info.get(field, 0 if field in ("added", "total") else "") for field in fields}
    if type(snapshot["added"]) is not int or type(snapshot["total"]) is not int:
        raise RuntimeError("在线任务源同步回执计数损坏，未覆盖原文件")
    return snapshot


def get_task_link_request(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return None
    with storage_transaction():
        try:
            raw = ONLINE_TASK_LINK_REQUESTS_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            data = _validate_task_link_requests(json.loads(raw))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"在线任务源同步回执损坏，未覆盖原文件：{exc}") from exc
        value = data.get(request_id)
        return dict(value) if isinstance(value, dict) else None


def persist_task_link_request(info):
    snapshot = _task_link_request_snapshot(info)
    request_id = snapshot["request_id"]
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("在线任务源同步请求标识无效")
    with storage_transaction():
        if ONLINE_TASK_LINK_REQUESTS_FILE.is_symlink():
            raise RuntimeError("在线任务源同步回执不能是符号链接，未覆盖原文件")

        def update(data):
            records = _validate_task_link_requests(data)
            records[request_id] = snapshot
            ordered = sorted(
                records.items(),
                key=lambda pair: str(pair[1].get("updated_at") or ""),
                reverse=True,
            )
            return dict(ordered[:200])

        mutate_json(ONLINE_TASK_LINK_REQUESTS_FILE, {}, update, strict=True)


def claim_task_link_request(info):
    """Atomically reserve a request id before making the external request."""
    snapshot = _task_link_request_snapshot(info)
    request_id = snapshot["request_id"]
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("在线任务源同步请求标识无效")

    observed = {}
    with storage_transaction():
        if ONLINE_TASK_LINK_REQUESTS_FILE.is_symlink():
            raise RuntimeError("在线任务源同步回执不能是符号链接，未覆盖原文件")

        def update(data):
            records = _validate_task_link_requests(data)
            current = records.get(request_id)
            if current and current.get("state") != "failed":
                observed["value"] = dict(current)
                return records
            if current and any(current.get(field) != snapshot.get(field) for field in (
                "script_id", "proxy_id", "expected_version",
            )):
                observed["value"] = dict(current)
                return records
            records[request_id] = snapshot
            observed["value"] = None
            ordered = sorted(
                records.items(),
                key=lambda pair: str(pair[1].get("updated_at") or ""),
                reverse=True,
            )
            return dict(ordered[:200])

        mutate_json(ONLINE_TASK_LINK_REQUESTS_FILE, {}, update, strict=True)
    return observed.get("value")


def merge_task_link_tasks(expected_item, linked_tasks):
    # Fetch outside the storage lock, then merge into the latest cache snapshot.
    with storage_transaction():
        items = load_online_script_cache()
        item = next((one for one in items if one.get("id") == expected_item.get("id")), None)
        expected_metadata = {key: value for key, value in expected_item.items() if key != "task_cron"}
        if item is None or {key: value for key, value in item.items() if key != "task_cron"} != expected_metadata:
            raise OnlineScriptCacheConflict("脚本源已更新或删除，请刷新列表后重新同步外部任务源")

        merged = online_script_task_crons(item)
        existing_keys = {(task["name"], task["cron"], task["command"]) for task in merged}
        added = 0
        for task in normalize_online_task_crons(linked_tasks):
            key = (task["name"], task["cron"], task["command"])
            if key not in existing_keys:
                existing_keys.add(key)
                merged.append(task)
                added += 1
        if added:
            item["task_cron"] = merged
            save_online_script_cache(items)
        return added, len(merged)


def read_cache_text(strict=False):
    try:
        if ONLINE_SCRIPT_CACHE_FILE.is_symlink():
            raise RuntimeError("在线脚本源缓存不能是符号链接")
        return ONLINE_SCRIPT_CACHE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception:
        if strict:
            raise

    return ""


def fetch_task_link_tasks(item, proxy_id="", timeout=12):
    """
    同步 item.task_link 指向的任务 JSON。

    支持 task_link 返回：
    1. [...]
    2. {"tasks": [...]}
    3. {"task_cron": [...]}
    4. 单个任务 dict
    """
    task_link = str((item or {}).get("task_link") or "").strip()

    if not task_link:
        return []

    validate_proxy_id(proxy_id)

    real_url = github_proxy_url(task_link, proxy_id)

    with closing(requests.get(
        real_url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
        proxies=requests_proxy_dict(proxy_id),
    )) as r:
        status_code = getattr(r, "status_code", 0)
        text = r.text or ""
        headers = getattr(r, "headers", {})

    if status_code < 200 or status_code >= 300:
        preview = text[:500].replace("\n", "\\n")
        raise RuntimeError(
            f"任务源请求失败，HTTP {status_code}。下一步：检查任务源地址和代理配置后重试。"
            f"请求地址：{real_url}。"
            f"返回内容预览：{preview}"
        )

    if not text.strip():
        raise RuntimeError(f"任务源返回空内容。请求地址：{real_url}。下一步：确认任务源返回 JSON 后重试")

    try:
        data = json.loads(text)
    except Exception as e:
        preview = text[:800].replace("\n", "\\n")
        ctype = headers.get("Content-Type", "")
        raise RuntimeError(
            f"任务源不是合法 JSON：{e}。下一步：检查任务源格式并确认返回 JSON 后重试。"
            f"请求地址：{real_url}。"
            f"Content-Type：{ctype or '-'}。"
            f"返回内容预览：{preview}"
        )

    return normalize_online_task_crons(data)


def _task_link_failure(item, error, proxy_id=""):
    def clean(value, limit):
        text = str(value or "")
        text = "".join(char if char in "\n\t" or ord(char) >= 32 else " " for char in text)
        return text[:limit]

    return {
        "script_id": clean(item.get("id"), 200),
        "name": clean(item.get("name"), 300),
        "task_link": clean(item.get("task_link"), 1000),
        "error": clean(error, 1000),
        "proxy_id": clean(proxy_id, 200),
    }


def fetch_online_scripts(proxy_id="", timeout=12, publish_cache=True, task_link_failures=None):
    validate_proxy_id(proxy_id)
    url = get_online_script_source()
    real_url = github_proxy_url(url, proxy_id)

    with closing(requests.get(
        real_url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
        proxies=requests_proxy_dict(proxy_id),
    )) as r:
        status_code = getattr(r, "status_code", 0)
        text = r.text or ""
        headers = getattr(r, "headers", {})

    if status_code < 200 or status_code >= 300:
        preview = text[:500].replace("\n", "\\n")
        raise RuntimeError(
            f"脚本源请求失败，HTTP {status_code}。下一步：检查在线脚本源地址和代理配置后重试。"
            f"请求地址：{real_url}。"
            f"返回内容预览：{preview}"
        )

    if not text.strip():
        raise RuntimeError(f"脚本源返回空内容。请求地址：{real_url}。下一步：确认脚本源返回 JSON 后重试")

    try:
        data = json.loads(text)
    except Exception as e:
        preview = text[:800].replace("\n", "\\n")
        ctype = headers.get("Content-Type", "")
        raise RuntimeError(
            f"脚本源不是合法 JSON：{e}。下一步：检查在线脚本源格式并确认返回 JSON 后重试。"
            f"请求地址：{real_url}。"
            f"Content-Type：{ctype or '-'}。"
            f"返回内容预览：{preview}"
        )

    items = validate_online_script_source_items(data)

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
                if task_link_failures is not None and len(task_link_failures) < 100:
                    task_link_failures.append(
                        _task_link_failure(item, "外部任务源没有可用任务，请检查任务字段后重新同步", proxy_id)
                    )
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
            if task_link_failures is not None and len(task_link_failures) < 100:
                task_link_failures.append(_task_link_failure(item, e, proxy_id))
            print(
                f"[OnlineScripts] 任务源加载失败: "
                f"{item.get('name')} task_link={task_link} error={e}"
            )

    if publish_cache:
        save_online_script_cache(items)
    return items


def refresh_worker(refresh_id, proxy_id="", log_file=""):
    log_file = log_file or str(online_refresh_log_file(refresh_id))
    request_id = ""
    retry_of = ""
    expected_cache_fingerprint = ""
    source_fingerprint = ""
    with ONLINE_REFRESH_LOCK:
        if ONLINE_REFRESH_STATE.get("id") == refresh_id:
            request_id = str(ONLINE_REFRESH_STATE.get("request_id") or "")
            retry_of = str(ONLINE_REFRESH_STATE.get("retry_of") or "")
            expected_cache_fingerprint = str(ONLINE_REFRESH_STATE.get("expected_cache_fingerprint") or "")
            source_fingerprint = str(ONLINE_REFRESH_STATE.get("source_fingerprint") or "")
    try:
        if source_fingerprint and online_script_source_fingerprint() != source_fingerprint:
            raise RuntimeError("在线脚本源地址在刷新提交后已变化，未发送外部请求")
        if retry_of and expected_cache_fingerprint and online_script_cache_fingerprint() != expected_cache_fingerprint:
            raise RuntimeError("在线脚本源缓存在重试提交后已变化，未发送外部请求")
        update_online_refresh_state(
            id=refresh_id,
            request_id=request_id,
            proxy_id=str(proxy_id or ""),
            running=True,
            status="运行中",
            message="正在后台同步在线脚本源，请稍候...",
            error="",
            cache_published=False,
            task_link_failures=[],
            updated_at=now_str(),
            log_file=str(log_file),
        )
        append_log(log_file, "===== 在线脚本源刷新 =====")
        append_log(log_file, f"时间: {now_str()}")
        append_log(log_file, f"脚本源: {get_online_script_source()}")
        append_log(log_file, f"代理ID: {proxy_id or '不使用代理'}")
        append_log(log_file, "============================================================")

        task_link_failures = []
        items = fetch_online_scripts(
            proxy_id=proxy_id,
            timeout=20,
            publish_cache=False,
            task_link_failures=task_link_failures,
        )
        if source_fingerprint and online_script_source_fingerprint() != source_fingerprint:
            raise RuntimeError("在线脚本源地址在抓取期间已变化，未发布缓存")
        with storage_transaction():
            current_cache_fingerprint = online_script_cache_fingerprint()
            if retry_of and expected_cache_fingerprint and current_cache_fingerprint != expected_cache_fingerprint:
                raise RuntimeError("在线脚本源缓存在抓取期间已变化，未覆盖后来缓存")
            update_online_refresh_state(
                status="发布中",
                message="已获取脚本源，正在原子发布本地缓存",
                updated_at=now_str(),
            )
            save_online_script_cache(items)
            published_cache_fingerprint = online_script_cache_fingerprint_for_items(items)
            if online_script_cache_fingerprint() != published_cache_fingerprint:
                raise RuntimeError("缓存发布后指纹与预期不一致，结果未知")
        # Cache replacement is a separate durable side effect; retain that
        # fact when the later status-file write cannot be confirmed.
        with ONLINE_REFRESH_LOCK:
            ONLINE_REFRESH_STATE["cache_published"] = True
            ONLINE_REFRESH_STATE["task_link_failures"] = list(task_link_failures)
            ONLINE_REFRESH_STATE["published_cache_fingerprint"] = published_cache_fingerprint
        if task_link_failures:
            msg = (
                f"同步在线脚本源部分完成，共 {len(items)} 条；"
                f"{len(task_link_failures)} 个外部任务源加载失败，请逐项重试"
            )
            status = "部分完成"
        else:
            msg = f"同步在线脚本源成功，共 {len(items)} 条"
            status = "已完成"
        append_log(log_file, msg)

        update_online_refresh_state(
            id=refresh_id,
            running=False,
            status=status,
            message=msg,
            error="",
            cache_published=True,
            updated_at=now_str(),
            log_file=str(log_file),
        )

    except Exception as e:
        cache_published = bool(ONLINE_REFRESH_STATE.get("cache_published"))
        if cache_published:
            err = (
                f"在线脚本源缓存已发布，但同步结果无法确认：{e}。"
                "请先查看当前缓存和刷新日志，再决定是否重试"
            )
            status = "结果未知"
        else:
            err = f"远程脚本源刷新失败：{e}。下一步：检查脚本源地址、代理和网络后重试"
            status = "失败"
        try:
            append_log(log_file, err)
        except Exception:
            pass
        try:
            update_online_refresh_state(
                id=refresh_id,
                running=False,
                status=status,
                message="",
                error=err,
                cache_published=cache_published,
                task_link_failures=ONLINE_REFRESH_STATE.get("task_link_failures", []),
                updated_at=now_str(),
                log_file=str(log_file),
            )
        except Exception:
            # A state-file failure must not leave an uncaught worker exception.
            pass


def start_refresh_thread(refresh_id, proxy_id="", log_file=""):
    th = threading.Thread(
        target=refresh_worker,
        args=(refresh_id, proxy_id, log_file),
        daemon=True,
        name="fls-online-scripts-refresh",
    )
    th.start()
    return th
