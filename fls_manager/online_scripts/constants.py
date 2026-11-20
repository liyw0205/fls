import json
import math
import re
import threading
import os
import uuid

from ..paths import DATA_DIR
from .logs import online_refresh_log_file
from ..storage import mutate_json, storage_transaction
from ..utils import now_str

DEFAULT_ONLINE_SCRIPT_SOURCE = "https://cdn.jsdelivr.net/gh/liyw0205/fls-scripts@main/index.json"
ONLINE_SCRIPT_CACHE_FILE = DATA_DIR / "online_scripts_cache.json"
ONLINE_TASK_LINK_REQUESTS_FILE = DATA_DIR / "online_task_link_requests.json"
ONLINE_TASK_LINK_ACTIVE = set()
ONLINE_TASK_IMPORT_REQUESTS_FILE = DATA_DIR / "online_task_import_requests.json"
ONLINE_TASK_IMPORT_ACTIVE = set()

ONLINE_INSTALL_RUNNING = {}
ONLINE_INSTALL_STOPPING = set()
ONLINE_INSTALL_RETRYING = set()
ONLINE_INSTALL_JOBS_FILE = DATA_DIR / "online_install_jobs.json"
ONLINE_INSTALL_LOCK = threading.RLock()
ONLINE_INSTALL_LIMIT = 100
ONLINE_INSTALL_STATUSES = (
    "准备中", "运行中", "停止中", "停止失败", "超时（停止失败）", "部分完成", "已完成", "失败",
    "超时", "已停止", "启动失败", "结果未知",
)
ONLINE_INSTALL_TIMEOUT_SECONDS = 1800
ONLINE_INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ONLINE_REFRESH_STATE_FILE = DATA_DIR / "online_script_refresh.json"
ONLINE_REFRESH_HISTORY_FILE = DATA_DIR / "online_script_refresh_requests.json"
ONLINE_REFRESH_LOCK = threading.RLock()
ONLINE_TASK_IMPORT_LOCK = threading.RLock()


class InstallHistoryLimitError(RuntimeError):
    pass


def _task_import_snapshot(info):
    fields = (
        "request_id", "script_id", "expected_version", "selected_task_indexes",
        "enable_task", "state", "message", "scheduler_reload_error", "updated_at",
    )
    snapshot = {field: info.get(field, "") for field in fields}
    selected = snapshot["selected_task_indexes"]
    if (
        not isinstance(snapshot["request_id"], str)
        or not re.fullmatch(r"[a-f0-9]{32}", snapshot["request_id"])
        or not isinstance(snapshot["script_id"], str)
        or not isinstance(snapshot["expected_version"], str)
        or not isinstance(selected, list)
        or any(not isinstance(value, str) for value in selected)
        or type(snapshot["enable_task"]) is not bool
        or snapshot["state"] not in ("prepared", "running", "failed", "scheduler_pending", "completed", "unknown")
        or any(not isinstance(snapshot[field], str) for field in ("message", "scheduler_reload_error", "updated_at"))
    ):
        raise RuntimeError("在线任务导入回执损坏，未覆盖原文件")
    return snapshot


def _read_task_import_requests():
    try:
        raw = ONLINE_TASK_IMPORT_REQUESTS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"在线任务导入回执损坏，未覆盖原文件：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("在线任务导入回执损坏，未覆盖原文件")
    for request_id, value in data.items():
        if request_id != str(request_id) or not isinstance(value, dict) or value.get("request_id") != request_id:
            raise RuntimeError("在线任务导入回执标识损坏，未覆盖原文件")
        _task_import_snapshot(value)
    return data


def _recover_stale_task_imports(data):
    changed = False
    for request_id, value in data.items():
        if value.get("state") == "running" and request_id not in ONLINE_TASK_IMPORT_ACTIVE:
            value = dict(value)
            value["state"] = "unknown"
            value["message"] = "面板重启或请求中断后无法确认任务是否已导入，请查询任务列表"
            value["updated_at"] = now_str()
            data[request_id] = _task_import_snapshot(value)
            changed = True
    return changed


def get_online_task_import_request(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return None
    with ONLINE_TASK_IMPORT_LOCK, storage_transaction():
        data = _read_task_import_requests()
        if _recover_stale_task_imports(data):
            mutate_json(ONLINE_TASK_IMPORT_REQUESTS_FILE, {}, lambda _: data, strict=True)
        return dict(data.get(request_id) or {}) or None


def list_online_task_import_requests(limit=50, script_id=""):
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("在线任务导入历史数量无效") from exc
    if not 1 <= limit <= 100:
        raise ValueError("在线任务导入历史数量必须在 1 到 100 之间")
    script_id = str(script_id or "").strip()
    with ONLINE_TASK_IMPORT_LOCK, storage_transaction():
        records = _read_task_import_requests()
        if _recover_stale_task_imports(records):
            mutate_json(ONLINE_TASK_IMPORT_REQUESTS_FILE, {}, lambda _: records, strict=True)
        items = [dict(value) for value in records.values() if not script_id or value.get("script_id") == script_id]
        items.sort(key=lambda value: str(value.get("updated_at") or ""), reverse=True)
        return items[:limit]


def persist_online_task_import_request(info):
    snapshot = _task_import_snapshot(info)
    with ONLINE_TASK_IMPORT_LOCK, storage_transaction():
        if ONLINE_TASK_IMPORT_REQUESTS_FILE.is_symlink():
            raise RuntimeError("在线任务导入回执不能是符号链接，未覆盖原文件")

        def update(data):
            if not isinstance(data, dict):
                raise RuntimeError("在线任务导入回执损坏，未覆盖原文件")
            for request_id, value in data.items():
                if not isinstance(request_id, str) or not isinstance(value, dict) or value.get("request_id") != request_id:
                    raise RuntimeError("在线任务导入回执标识损坏，未覆盖原文件")
                _task_import_snapshot(value)
            data[snapshot["request_id"]] = snapshot
            ordered = sorted(data.items(), key=lambda pair: str(pair[1].get("updated_at") or ""), reverse=True)
            return dict(ordered[:200])

        mutate_json(ONLINE_TASK_IMPORT_REQUESTS_FILE, {}, update, strict=True)


def claim_online_task_import_request(info):
    """Reserve an import request; only failed requests may be explicitly retried."""
    snapshot = _task_import_snapshot(info)
    observed = {}
    with ONLINE_TASK_IMPORT_LOCK, storage_transaction():
        if ONLINE_TASK_IMPORT_REQUESTS_FILE.is_symlink():
            raise RuntimeError("在线任务导入回执不能是符号链接，未覆盖原文件")

        def update(data):
            data = dict(data) if isinstance(data, dict) else data
            if not isinstance(data, dict):
                raise RuntimeError("在线任务导入回执损坏，未覆盖原文件")
            for request_id, value in data.items():
                if not isinstance(request_id, str) or not isinstance(value, dict) or value.get("request_id") != request_id:
                    raise RuntimeError("在线任务导入回执标识损坏，未覆盖原文件")
                _task_import_snapshot(value)
            current = data.get(snapshot["request_id"])
            if current and current.get("state") != "failed":
                observed["value"] = dict(current)
                return data
            if current and any(current.get(field) != snapshot.get(field) for field in ("script_id", "expected_version", "selected_task_indexes", "enable_task")):
                observed["value"] = dict(current)
                return data
            data[snapshot["request_id"]] = snapshot
            observed["value"] = None
            ordered = sorted(data.items(), key=lambda pair: str(pair[1].get("updated_at") or ""), reverse=True)
            return dict(ordered[:200])

        mutate_json(ONLINE_TASK_IMPORT_REQUESTS_FILE, {}, update, strict=True)
    return observed.get("value")

ONLINE_REFRESH_STATE = {
    "id": "",
    "request_id": "",
    "proxy_id": "",
    "running": False,
    "status": "",
    "message": "",
    "error": "",
    "cache_published": False,
    "task_link_failures": [],
    "updated_at": "",
    "log_file": "",
    "retry_of": "",
    "expected_cache_fingerprint": "",
    "published_cache_fingerprint": "",
    "source_fingerprint": "",
    "confirmation": "",
    "confirmation_fingerprint": "",
}


def _refresh_snapshot(info):
    fields = (
        "id", "request_id", "proxy_id", "running", "status", "message", "error", "cache_published",
        "task_link_failures", "updated_at", "log_file", "retry_of", "expected_cache_fingerprint",
        "published_cache_fingerprint", "source_fingerprint", "confirmation", "confirmation_fingerprint",
    )
    defaults = {
        "request_id": "",
        "proxy_id": "",
        "running": False,
        "cache_published": False,
        "task_link_failures": [],
        "retry_of": "",
        "expected_cache_fingerprint": "",
        "published_cache_fingerprint": "",
        "source_fingerprint": "",
        "confirmation": "",
        "confirmation_fingerprint": "",
    }
    return {
        field: info.get(field, defaults.get(field, ""))
        for field in fields
    }


def _validate_refresh_snapshot(info):
    snapshot = _refresh_snapshot(info)
    if (
        type(snapshot["running"]) is not bool
        or type(snapshot["cache_published"]) is not bool
        or any(not isinstance(snapshot[field], str) for field in ("id", "request_id", "proxy_id", "status", "message", "error", "updated_at", "log_file", "retry_of", "expected_cache_fingerprint", "published_cache_fingerprint", "source_fingerprint", "confirmation", "confirmation_fingerprint"))
        or (snapshot["request_id"] and not re.fullmatch(r"[a-f0-9]{32}", snapshot["request_id"]))
        or (snapshot["retry_of"] and not re.fullmatch(r"[a-f0-9]{32}", snapshot["retry_of"]))
        or (snapshot["expected_cache_fingerprint"] and not re.fullmatch(r"[a-f0-9]{64}", snapshot["expected_cache_fingerprint"]))
        or (snapshot["published_cache_fingerprint"] and not re.fullmatch(r"[a-f0-9]{64}", snapshot["published_cache_fingerprint"]))
        or (snapshot["source_fingerprint"] and not re.fullmatch(r"[a-f0-9]{64}", snapshot["source_fingerprint"]))
        or (snapshot["confirmation_fingerprint"] and not re.fullmatch(r"[a-f0-9]{64}", snapshot["confirmation_fingerprint"]))
        or snapshot["confirmation"] not in ("", "published", "not_published")
    ):
        raise RuntimeError("在线脚本源同步状态损坏，未覆盖原文件")
    if len(snapshot["proxy_id"]) > 200:
        raise RuntimeError("在线脚本源同步状态损坏，未覆盖原文件")
    failures = snapshot["task_link_failures"]
    if len(failures) > 100 or any(
        not isinstance(item, dict)
        or not isinstance(item.get("script_id"), str)
        or not isinstance(item.get("name"), str)
        or not isinstance(item.get("task_link"), str)
        or not isinstance(item.get("error"), str)
        or ("proxy_id" in item and not isinstance(item.get("proxy_id"), str))
        for item in failures
    ):
        raise RuntimeError("在线脚本源同步状态损坏，未覆盖原文件")
    for item in failures:
        if any(len(item.get(field, "")) > 1000 for field in ("script_id", "name", "task_link", "error", "proxy_id")):
            raise RuntimeError("在线脚本源同步状态损坏，未覆盖原文件")
    return snapshot


def _read_online_refresh_file():
    with storage_transaction():
        if ONLINE_REFRESH_STATE_FILE.is_symlink() or (ONLINE_REFRESH_STATE_FILE.exists() and not ONLINE_REFRESH_STATE_FILE.is_file()):
            raise RuntimeError("在线脚本源同步状态必须是普通文件，未覆盖原文件")
        try:
            raw = ONLINE_REFRESH_STATE_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"在线脚本源同步状态损坏，未覆盖原文件：{exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("在线脚本源同步状态损坏，未覆盖原文件：根节点必须是对象")
        return _validate_refresh_snapshot(data)


def _read_online_refresh_history_file():
    if ONLINE_REFRESH_HISTORY_FILE.is_symlink() or (ONLINE_REFRESH_HISTORY_FILE.exists() and not ONLINE_REFRESH_HISTORY_FILE.is_file()):
        raise RuntimeError("在线脚本源刷新历史必须是普通文件，未覆盖原文件")
    try:
        raw = ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"在线脚本源刷新历史损坏，未覆盖原文件：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("在线脚本源刷新历史损坏，未覆盖原文件：根节点必须是对象")
    for request_id, value in data.items():
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise RuntimeError("在线脚本源刷新历史标识损坏，未覆盖原文件")
        if not isinstance(value, dict) or value.get("request_id") != request_id:
            raise RuntimeError("在线脚本源刷新历史标识不一致，未覆盖原文件")
        _validate_refresh_snapshot(value)
    return data


class RefreshPublicationError(RuntimeError):
    def __init__(self, error, published_snapshot=None):
        super().__init__(str(error))
        self.published_snapshot = published_snapshot


def _publish_refresh_history(history):
    temp = ONLINE_REFRESH_HISTORY_FILE.with_name(
        f".{ONLINE_REFRESH_HISTORY_FILE.name}.{threading.get_ident()}.tmp"
    )
    try:
        temp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, ONLINE_REFRESH_HISTORY_FILE)
    finally:
        temp.unlink(missing_ok=True)


def persist_online_refresh(info):
    snapshot = _validate_refresh_snapshot(info)
    with storage_transaction():
        if ONLINE_REFRESH_STATE_FILE.is_symlink():
            raise RuntimeError("在线脚本源同步状态文件不能是符号链接")
        if ONLINE_REFRESH_HISTORY_FILE.is_symlink():
            raise RuntimeError("在线脚本源刷新历史不能是符号链接")
        previous_snapshot = _read_online_refresh_file()
        history = _read_online_refresh_history_file()
        ONLINE_REFRESH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = ONLINE_REFRESH_STATE_FILE.with_name(
            f".{ONLINE_REFRESH_STATE_FILE.name}.{threading.get_ident()}.tmp"
        )
        history_temp = ONLINE_REFRESH_HISTORY_FILE.with_name(
            f".{ONLINE_REFRESH_HISTORY_FILE.name}.{threading.get_ident()}.tmp"
        )
        try:
            try:
                previous_id = previous_snapshot.get("request_id")
                if previous_id and history.get(previous_id) != previous_snapshot:
                    # Preserve a receipt left behind by a partial publication before replacing it.
                    history[previous_id] = previous_snapshot
                    _publish_refresh_history(history)
                for candidate in (previous_snapshot, snapshot):
                    if candidate.get("request_id"):
                        history[candidate["request_id"]] = candidate
                if snapshot["request_id"]:
                    history = dict(sorted(
                        history.items(),
                        key=lambda pair: (pair[0] == snapshot["request_id"], str(pair[1].get("updated_at") or "")),
                        reverse=True,
                    )[:200])
                temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
                history_temp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temp, ONLINE_REFRESH_STATE_FILE)
                os.replace(history_temp, ONLINE_REFRESH_HISTORY_FILE)
            finally:
                temp.unlink(missing_ok=True)
                history_temp.unlink(missing_ok=True)
        except Exception as exc:
            try:
                published = snapshot if _read_online_refresh_file() == snapshot else None
            except Exception:
                published = None
            raise RefreshPublicationError(exc, published) from exc


def update_online_refresh_state(**updates):
    with ONLINE_REFRESH_LOCK:
        previous = dict(ONLINE_REFRESH_STATE)
        ONLINE_REFRESH_STATE.update(updates)
        try:
            persist_online_refresh(ONLINE_REFRESH_STATE)
        except Exception as exc:
            ONLINE_REFRESH_STATE.clear()
            ONLINE_REFRESH_STATE.update(getattr(exc, "published_snapshot", None) or previous)
            if getattr(exc, "published_snapshot", None):
                ONLINE_REFRESH_STATE["persistence_error"] = str(exc)
            raise
        return dict(ONLINE_REFRESH_STATE)


def get_online_refresh_state():
    with ONLINE_REFRESH_LOCK:
        current = dict(ONLINE_REFRESH_STATE)
        loaded_from_file = False
        if ONLINE_REFRESH_STATE_FILE.exists() or ONLINE_REFRESH_STATE_FILE.is_symlink():
            data = _read_online_refresh_file()
            if data and (data.get("id") != current.get("id") or (not data.get("running") and current.get("running"))):
                current = dict(data)
                loaded_from_file = True
                ONLINE_REFRESH_STATE.clear()
                ONLINE_REFRESH_STATE.update(current)
        if loaded_from_file and current.get("running"):
            current["running"] = False
            current["status"] = "结果未知"
            current["error"] = "面板重启后无法确认在线脚本源同步是否完成；保留旧缓存，请查看刷新日志后重试。"
            current["message"] = ""
            current["updated_at"] = now_str()
            if not current.get("log_file") and re.fullmatch(r"[a-f0-9]{32}", current.get("id", "")):
                recovered_log = online_refresh_log_file(current["id"])
                if recovered_log.is_file() and not recovered_log.is_symlink():
                    current["log_file"] = str(recovered_log)
            ONLINE_REFRESH_STATE.update(current)
            persist_online_refresh(current)
        if current.get("request_id"):
            disk = _read_online_refresh_file()
            history = _read_online_refresh_history_file()
            if history.get(current["request_id"]) != disk or _refresh_snapshot(current) != disk or current.get("persistence_error"):
                current["persistence_error"] = current.get("persistence_error") or "刷新当前状态与历史回执尚未一致，请修复回执后继续。"
                current["persistence_recovery"] = {
                    "action": f'/online-scripts/refresh-recover/{current["request_id"]}',
                    "api_action": f'/api/online-scripts/refresh-recover/{current["request_id"]}',
                    "label": "修复刷新回执",
                }
        return current


def _recover_online_refresh_history(history, current, request_id=""):
    recovered = dict(history)
    changed = False
    for key, info in history.items():
        if (request_id and key != request_id) or key == current.get("request_id") or not info.get("running"):
            continue
        reason = "原同步已不再是当前活动刷新，无法确认是否完成；请查看原日志并检查当前缓存，再明确重试。"
        recovered[key] = _validate_refresh_snapshot({
            **info, "running": False, "status": "结果未知", "message": "",
            "error": "；".join(filter(None, (info.get("error"), reason))), "updated_at": now_str(),
        })
        changed = True
    if changed:
        _publish_refresh_history(recovered)
    return recovered


def get_online_refresh_request(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return None
    with ONLINE_REFRESH_LOCK, storage_transaction():
        current = get_online_refresh_state()
        if current.get("request_id") == request_id:
            return current
        history = _recover_online_refresh_history(_read_online_refresh_history_file(), current, request_id)
        value = history.get(request_id)
        return dict(value) if isinstance(value, dict) else None


def get_online_refresh_history(limit=50, request_id=""):
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise ValueError("刷新历史条数必须为整数")
    if request_id and (not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id)):
        raise ValueError("刷新请求标识无效")
    with ONLINE_REFRESH_LOCK, storage_transaction():
        if request_id:
            saved_history = _read_online_refresh_history_file()
            saved_current = _read_online_refresh_file()
            if request_id not in saved_history and request_id not in (
                saved_current.get("request_id"), ONLINE_REFRESH_STATE.get("request_id"),
            ):
                raise LookupError("原刷新记录不存在或已清理，请返回刷新状态重新选择")
        current = get_online_refresh_state()
        history = _recover_online_refresh_history(_read_online_refresh_history_file(), current)
        if current.get("request_id"):
            history[current["request_id"]] = current
        ordered = sorted(
            history.values(),
            key=lambda value: (value.get("request_id") == request_id, str(value.get("updated_at") or "")),
            reverse=True,
        )
        return [dict(value) for value in ordered[:limit]]


def recover_online_refresh_receipt(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("刷新请求标识无效")
    with ONLINE_REFRESH_LOCK, storage_transaction():
        info = get_online_refresh_request(request_id)
        if not info:
            return None
        if info.get("request_id") == ONLINE_REFRESH_STATE.get("request_id"):
            persist_online_refresh(info)
            ONLINE_REFRESH_STATE.clear()
            ONLINE_REFRESH_STATE.update(_refresh_snapshot(info))
            return get_online_refresh_state()
        return info


def confirm_online_refresh_result(request_id, cache_fingerprint, source_fingerprint):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("刷新请求标识无效")
    if not re.fullmatch(r"[a-f0-9]{64}", str(cache_fingerprint or "")):
        raise ValueError("当前缓存指纹无效")
    if not re.fullmatch(r"[a-f0-9]{64}", str(source_fingerprint or "")):
        raise ValueError("在线脚本源版本指纹无效")
    with ONLINE_REFRESH_LOCK, storage_transaction():
        history = _read_online_refresh_history_file()
        current = get_online_refresh_state()
        record = dict(current if current.get("request_id") == request_id else history.get(request_id) or {})
        current_matches = ONLINE_REFRESH_STATE.get("request_id") == request_id
        if not record and current_matches:
            record = _validate_refresh_snapshot(ONLINE_REFRESH_STATE)
        if not record:
            return {"_missing": True}
        if record.get("confirmation"):
            if record.get("confirmation_fingerprint") != cache_fingerprint:
                return {"_conflict": True, **record}
            return {"_replayed": True, **record}
        orphaned = record.get("running") and record.get("request_id") != current.get("request_id")
        if not orphaned and (record.get("running") or record.get("status") != "结果未知"):
            return {"_conflict": True, **record}
        if not record.get("source_fingerprint") or record.get("source_fingerprint") != source_fingerprint:
            return {"_conflict": True, **record}
        published_fingerprint = str(record.get("published_cache_fingerprint") or "")
        expected_fingerprint = str(record.get("expected_cache_fingerprint") or "")
        if published_fingerprint and cache_fingerprint == published_fingerprint:
            confirmation = "published"
            status = "已确认发布"
            message = "当前缓存指纹与刷新发布回执一致"
            cache_published = True
        elif not published_fingerprint and expected_fingerprint and cache_fingerprint == expected_fingerprint:
            confirmation = "not_published"
            status = "已确认未发布"
            message = "当前缓存仍与刷新前指纹一致，未发现缓存发布"
            cache_published = False
        else:
            return {"_conflict": True, **record}
        if orphaned:
            record = get_online_refresh_request(request_id)
        snapshot = _validate_refresh_snapshot({
            **record,
            "running": False,
            "status": status,
            "message": message,
            "cache_published": cache_published,
            "confirmation": confirmation,
            "confirmation_fingerprint": cache_fingerprint,
            "updated_at": now_str(),
        })
        if current_matches:
            update_online_refresh_state(**snapshot)
        else:
            if ONLINE_REFRESH_HISTORY_FILE.is_symlink():
                raise RuntimeError("在线脚本源刷新历史不能是符号链接，未覆盖原文件")

            def update(data):
                records = _read_online_refresh_history_file()
                if request_id not in records:
                    raise RuntimeError("刷新记录已被清理，不能确认结果")
                records[request_id] = snapshot
                return records

            mutate_json(ONLINE_REFRESH_HISTORY_FILE, {}, update, strict=True)
        return dict(snapshot)


def _existing_online_refresh_retry(history, original_request_id, proxy_id, request_id):
    existing = history.get(request_id)
    if not existing:
        return None
    if existing.get("retry_of") != original_request_id or str(existing.get("proxy_id") or "") != str(proxy_id or ""):
        return {"_binding_conflict": True, **dict(existing)}
    return {"_replay": True, **dict(existing)}


def get_online_refresh_retry(original_request_id, proxy_id, request_id):
    if not isinstance(original_request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", original_request_id):
        raise ValueError("原刷新请求标识无效")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("新的刷新请求标识无效")
    with ONLINE_REFRESH_LOCK, storage_transaction():
        history = _read_online_refresh_history_file()
        current = get_online_refresh_state()
        if current.get("request_id"):
            history[current["request_id"]] = current
        existing = _existing_online_refresh_retry(history, original_request_id, proxy_id, request_id)
        if existing and existing.get("_replay") and existing.get("running") and request_id != current.get("request_id"):
            return {"_replay": True, **get_online_refresh_request(request_id)}
        return existing


def claim_online_refresh_retry(original_request_id, proxy_id="", request_id="", cache_fingerprint="", source_fingerprint=""):
    """Atomically create one explicit retry for a failed refresh history record."""
    if not isinstance(original_request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", original_request_id):
        raise ValueError("原刷新请求标识无效")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("新的刷新请求标识无效")
    if cache_fingerprint and not re.fullmatch(r"[a-f0-9]{64}", str(cache_fingerprint)):
        raise ValueError("刷新缓存指纹无效")
    with ONLINE_REFRESH_LOCK, storage_transaction():
        history = _read_online_refresh_history_file()
        current = get_online_refresh_state()
        if current.get("request_id"):
            history[current["request_id"]] = current
        existing = _existing_online_refresh_retry(history, original_request_id, proxy_id, request_id)
        if existing:
            if existing.get("_replay") and existing.get("running") and request_id != current.get("request_id"):
                return {"_replay": True, **get_online_refresh_request(request_id)}
            return existing
        if current.get("persistence_error"):
            if current.get("status") == "启动失败":
                current = recover_online_refresh_receipt(current["request_id"])
                history[current["request_id"]] = current
            else:
                raise RuntimeError("刷新当前状态与历史回执尚未一致，请先修复原刷新回执后再提交新的请求")
        original = history.get(original_request_id)
        if not original:
            return {"_missing": True}
        orphaned = original.get("running") and original.get("request_id") != current.get("request_id")
        if not orphaned and original.get("status") not in ("失败", "部分完成", "结果未知", "启动失败"):
            return {"_not_retryable": True, **dict(original)}
        if str(original.get("proxy_id") or "") != str(proxy_id or ""):
            return {"_binding_conflict": True, **dict(original)}
        expected_cache = str(original.get("published_cache_fingerprint") or original.get("expected_cache_fingerprint") or "")
        if not expected_cache or cache_fingerprint != expected_cache:
            return {"_stale": True, **dict(original)}
        if not original.get("source_fingerprint") or source_fingerprint != original.get("source_fingerprint"):
            return {"_stale": True, **dict(original)}
        if orphaned:
            original = get_online_refresh_request(original_request_id)
        current = dict(ONLINE_REFRESH_STATE)
        if current.get("running"):
            return None
        info = {
            "id": uuid.uuid4().hex,
            "request_id": request_id,
            "proxy_id": str(proxy_id or ""),
            "running": True,
            "status": "运行中",
            "message": "正在重试在线脚本源同步，请稍候...",
            "error": "",
            "cache_published": False,
            "task_link_failures": [],
            "updated_at": now_str(),
            "log_file": "",
            "retry_of": original_request_id,
            "expected_cache_fingerprint": str(cache_fingerprint or ""),
            "published_cache_fingerprint": "",
            "source_fingerprint": str(source_fingerprint or ""),
            "confirmation": "",
            "confirmation_fingerprint": "",
        }
        try:
            persist_online_refresh(info)
        except Exception as exc:
            _retain_unlaunched_refresh(exc)
            raise
        ONLINE_REFRESH_STATE.clear()
        ONLINE_REFRESH_STATE.update(info)
        return dict(info)


def _retain_unlaunched_refresh(exc):
    published = getattr(exc, "published_snapshot", None)
    if published:
        error = f"刷新回执发布未确认，未启动抓取：{exc}。请先修复刷新回执。"
        if re.fullmatch(r"[a-f0-9]{32}", published.get("id", "")):
            log_path = online_refresh_log_file(published["id"])
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(log_path, flags, 0o600)
                with os.fdopen(fd, "ab") as stream:
                    stream.write((error + "\n").encode("utf-8", errors="replace"))
                published["log_file"] = str(log_path)
            except Exception as log_error:
                error += f"；刷新诊断日志未能创建：{log_error}"
        ONLINE_REFRESH_STATE.clear()
        ONLINE_REFRESH_STATE.update(published)
        ONLINE_REFRESH_STATE.update(
            running=False, status="结果未知", message="",
            error=error,
            persistence_error=str(exc),
        )


def get_online_refresh_submission(proxy_id, request_id):
    with ONLINE_REFRESH_LOCK, storage_transaction():
        current = _read_online_refresh_file()
        info = current if current.get("request_id") == request_id else _read_online_refresh_history_file().get(request_id)
        if not info and ONLINE_REFRESH_STATE.get("request_id") == request_id:
            info = dict(ONLINE_REFRESH_STATE)
        if not info:
            return None
        if str(info.get("proxy_id") or "") != str(proxy_id or ""):
            return {"_binding_conflict": True, **info}
        info = get_online_refresh_request(request_id)
        if info.get("running"):
            return {"_running": True, **info}
        if info.get("status") in ("已完成", "部分完成"):
            return {"_replay": True, **info}
        if (
            info.get("status") == "启动失败"
            and info.get("request_id") == ONLINE_REFRESH_STATE.get("request_id")
            and not info.get("cache_published")
        ):
            return None
        return {"_unknown": True, **info}


def reserve_online_refresh(proxy_id="", request_id="", cache_fingerprint="", source_fingerprint=""):
    with ONLINE_REFRESH_LOCK:
        current = get_online_refresh_state()
        import uuid
        request_id = str(request_id or uuid.uuid4().hex)
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("在线脚本源刷新请求标识无效，请刷新页面后重试")
        existing = get_online_refresh_submission(proxy_id, request_id)
        if existing:
            return existing
        if current.get("running"):
            return None
        if current.get("persistence_error"):
            if current.get("status") == "启动失败":
                current = recover_online_refresh_receipt(current["request_id"])
            else:
                raise RuntimeError("刷新当前状态与历史回执尚未一致，请先修复原刷新回执后再提交新的请求")
        previous = dict(ONLINE_REFRESH_STATE)
        info = {
            "id": uuid.uuid4().hex,
            "request_id": request_id,
            "proxy_id": str(proxy_id or ""),
            "running": True,
            "status": "运行中",
            "message": "正在后台同步在线脚本源，请稍候...",
            "error": "",
            "cache_published": False,
            "task_link_failures": [],
            "updated_at": now_str(),
            "log_file": "",
            "proxy_id": proxy_id,
            "expected_cache_fingerprint": str(cache_fingerprint or ""),
            "published_cache_fingerprint": "",
            "source_fingerprint": str(source_fingerprint or ""),
            "retry_of": "",
            "confirmation": "",
            "confirmation_fingerprint": "",
        }
        ONLINE_REFRESH_STATE.update(info)
        try:
            persist_online_refresh(info)
        except Exception as exc:
            ONLINE_REFRESH_STATE.clear()
            ONLINE_REFRESH_STATE.update(previous)
            _retain_unlaunched_refresh(exc)
            raise
        return dict(info)


def _install_snapshot(info):
    fields = (
        "id",
        "script_id",
        "script_name",
        "log_file",
        "proxy_id",
        "import_task",
        "enable_task",
        "selected_task_indexes",
        "selected_tasks",
        "force",
        "retry_of",
        "retried_by",
        "running",
        "status",
        "phase",
        "target_path",
        "target_fingerprint",
        "tasks_fingerprint",
        "task_snapshot",
        "start_time",
        "returncode",
        "error",
        "scheduler_reload_pending",
        "scheduler_reload_error",
        "updated_at",
    )
    return {field: info.get(field) for field in fields}


def _bounded_online_install_jobs(data):
    if len(data) <= ONLINE_INSTALL_LIMIT:
        return dict(data)

    roots = {install_id: install_id for install_id in data}

    def find(install_id):
        while roots[install_id] != install_id:
            roots[install_id] = roots[roots[install_id]]
            install_id = roots[install_id]
        return install_id

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            roots[right_root] = left_root

    for install_id, info in data.items():
        for field in ("retry_of", "retried_by"):
            linked_id = info.get(field)
            if isinstance(linked_id, str) and linked_id in data and linked_id != install_id:
                union(install_id, linked_id)

    groups = {}
    for install_id in data:
        groups.setdefault(find(install_id), set()).add(install_id)

    def group_key(group):
        return max(
            (str(data[install_id].get("updated_at") or ""), install_id)
            for install_id in group
        )

    protected = []
    disposable = []
    for group in groups.values():
        jobs = [data[install_id] for install_id in group]
        if any(
            job.get("running")
            or job.get("status") in ("准备中", "运行中", "停止中")
            or bool(job.get("scheduler_reload_pending"))
            for job in jobs
        ):
            protected.append(group)
        else:
            disposable.append(group)

    protected_count = sum(len(group) for group in protected)
    if protected_count > ONLINE_INSTALL_LIMIT:
        raise InstallHistoryLimitError(
            "活动在线安装及其重试链超过历史保留上限，未写入新回执，也未启动下载或安装进程。"
        )

    retained_ids = set().union(*protected) if protected else set()
    for group in sorted(disposable, key=group_key, reverse=True):
        if len(retained_ids) + len(group) <= ONLINE_INSTALL_LIMIT:
            retained_ids.update(group)

    ordered = sorted(
        ((install_id, data[install_id]) for install_id in retained_ids),
        key=lambda pair: (str(pair[1].get("updated_at") or ""), pair[0]),
        reverse=True,
    )
    return dict(ordered)


def _read_install_jobs():
    """Read install history without treating corruption as an empty history."""
    with storage_transaction():
        if ONLINE_INSTALL_JOBS_FILE.is_symlink():
            raise RuntimeError("在线脚本安装记录不能是符号链接，未读取外部文件")
        try:
            raw = ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"在线脚本安装记录损坏，未覆盖原文件：{exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：根节点必须是对象")
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, dict) or value.get("id") != key:
                raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：记录标识不一致")
        return data


def persist_online_install(info):
    with ONLINE_INSTALL_LOCK:
        snapshot = _install_snapshot(info)
        install_id = str(snapshot.get("id") or "")
        if not install_id:
            return
        if ONLINE_INSTALL_JOBS_FILE.is_symlink():
            raise RuntimeError("在线脚本安装记录不能是符号链接，未覆盖外部文件")

        def update(data):
            if not isinstance(data, dict):
                raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：根节点必须是对象")
            jobs = {}
            for key, value in data.items():
                if not isinstance(key, str) or not isinstance(value, dict) or value.get("id") != key:
                    raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：记录标识不一致")
                jobs[key] = value
            retry_of = str(snapshot.get("retry_of") or "")
            if retry_of == install_id:
                raise RuntimeError("在线脚本安装重试记录不能引用自身，未覆盖原文件")
            existing = jobs.get(install_id)
            if existing and any(
                existing.get(field) != snapshot.get(field)
                for field in ("script_id", "retry_of")
            ):
                raise RuntimeError("在线脚本安装标识已绑定其他作业，未覆盖原文件")
            previous = jobs.get(retry_of)
            if retry_of:
                if not isinstance(previous, dict) or previous.get("script_id") != snapshot.get("script_id"):
                    raise RuntimeError("在线脚本原安装记录缺失或不匹配，未覆盖原文件")
                linked_id = str(previous.get("retried_by") or "")
                if linked_id and linked_id != install_id:
                    raise RuntimeError("在线脚本原安装已关联其他重试，未覆盖原文件")
                previous = dict(previous)
                previous["retried_by"] = install_id
                jobs[retry_of] = previous
            retry_child_id = str(snapshot.get("retried_by") or "")
            if retry_child_id:
                child = jobs.get(retry_child_id)
                if (
                    not isinstance(child, dict)
                    or child.get("retry_of") != install_id
                    or child.get("script_id") != snapshot.get("script_id")
                ):
                    raise RuntimeError("在线脚本重试关联损坏，未覆盖原文件")
            jobs.pop(install_id, None)
            jobs = {install_id: snapshot, **jobs}
            return _bounded_online_install_jobs(jobs)

        mutate_json(ONLINE_INSTALL_JOBS_FILE, {}, update, strict=True)
        previous = ONLINE_INSTALL_RUNNING.get(str(snapshot.get("retry_of") or ""))
        if isinstance(previous, dict):
            previous["retried_by"] = install_id


def _load_persisted_online_install(install_id):
    jobs = _read_install_jobs()
    info = jobs.get(str(install_id or ""))
    return dict(info) if isinstance(info, dict) else None


def _load_persisted_online_installs():
    jobs = _read_install_jobs()
    return {
        str(install_id): dict(info)
        for install_id, info in jobs.items()
        if isinstance(info, dict)
    }


def online_install_retry_relation(info, jobs=None):
    install_id = str((info or {}).get("id") or "")
    if not ONLINE_INSTALL_ID_RE.fullmatch(install_id):
        return {"retry_parent_id": "", "retry_child_id": "", "retry_relation_error": "安装标识无效"}
    try:
        with ONLINE_INSTALL_LOCK:
            if jobs is None:
                jobs = _load_persisted_online_installs()
                for live_id, live_info in ONLINE_INSTALL_RUNNING.items():
                    if isinstance(live_info, dict):
                        jobs[live_id] = _install_snapshot(live_info)
            else:
                jobs = dict(jobs)
            jobs[install_id] = dict(info)
    except Exception as exc:
        return {
            "retry_parent_id": "",
            "retry_child_id": "",
            "retry_relation_error": f"安装重试关系无法读取：{exc}",
        }

    def relation_error(message):
        return {
            "retry_parent_id": "",
            "retry_child_id": "",
            "retry_relation_error": message,
        }

    visited = {install_id}
    current_id = install_id
    while True:
        current = jobs.get(current_id)
        if not isinstance(current, dict):
            return relation_error("安装重试关系指向缺失记录")
        parent_id = current.get("retry_of") or ""
        if not parent_id:
            break
        if not isinstance(parent_id, str) or not ONLINE_INSTALL_ID_RE.fullmatch(parent_id):
            return relation_error("安装重试关联标识无效")
        if parent_id in visited:
            return relation_error("安装重试关系存在循环")
        parent = jobs.get(parent_id)
        if (
            not isinstance(parent, dict)
            or parent.get("retried_by") != current_id
            or parent.get("script_id") != current.get("script_id")
        ):
            return relation_error("安装重试父子关联缺失或不匹配，已禁用重试")
        visited.add(parent_id)
        current_id = parent_id

    current_id = install_id
    while True:
        current = jobs.get(current_id)
        if not isinstance(current, dict):
            return relation_error("安装重试关系指向缺失记录")
        child_id = current.get("retried_by") or ""
        if not child_id:
            break
        if not isinstance(child_id, str) or not ONLINE_INSTALL_ID_RE.fullmatch(child_id):
            return relation_error("安装重试关联标识无效")
        if child_id in visited:
            return relation_error("安装重试关系存在循环")
        child = jobs.get(child_id)
        if (
            not isinstance(child, dict)
            or child.get("retry_of") != current_id
            or child.get("script_id") != current.get("script_id")
        ):
            return relation_error("安装重试父子关联缺失或不匹配，已禁用重试")
        visited.add(child_id)
        current_id = child_id

    parent_id = str((info or {}).get("retry_of") or "")
    child_id = str((info or {}).get("retried_by") or "")
    return {"retry_parent_id": parent_id, "retry_child_id": child_id, "retry_relation_error": ""}


def get_online_install_job(install_id):
    """Return a live or persisted install, marking lost processes as unknown."""
    with ONLINE_INSTALL_LOCK:
        info = ONLINE_INSTALL_RUNNING.get(install_id)
        if isinstance(info, dict):
            return dict(info)

        info = _load_persisted_online_install(install_id)
        if not info:
            return None

        if info.get("running"):
            info["running"] = False
            info["status"] = "结果未知"
            info["returncode"] = None
            restart_error = (
                "面板重启后无法确认在线脚本安装进程是否完成，请查看安装日志和脚本目录；"
                "确认状态后再决定是否重试。"
            )
            info["error"] = "；".join(filter(None, (info.get("error"), restart_error)))
            info["updated_at"] = now_str()
            try:
                persist_online_install(info)
            except Exception as e:
                info["error"] += f"；保存恢复状态失败：{e}，请修复文件权限或磁盘空间后重试。"

        return info


def active_online_install_for_script(script_id):
    script_id = str(script_id or "")
    with ONLINE_INSTALL_LOCK:
        for install_id, info in ONLINE_INSTALL_RUNNING.items():
            if info.get("script_id") == script_id and info.get("running"):
                return install_id, dict(info)
    return "", None


def latest_online_install_for_script(script_id):
    """Return the newest install record, including a process lost on restart."""
    script_id = str(script_id or "")
    if not script_id:
        return "", None

    with ONLINE_INSTALL_LOCK:
        jobs = _load_persisted_online_installs()
        for install_id, info in ONLINE_INSTALL_RUNNING.items():
            if isinstance(info, dict):
                jobs[str(install_id)] = dict(info)

        matches = []
        for install_id, info in jobs.items():
            if str(info.get("script_id") or "") != script_id:
                continue
            if install_id not in ONLINE_INSTALL_RUNNING and info.get("running"):
                info = get_online_install_job(install_id) or info
            matches.append((install_id, info))

        if not matches:
            return "", None

        def order(pair):
            try:
                started = float(pair[1].get("start_time") or 0)
            except (ValueError, TypeError):
                started = 0
            return started, str(pair[1].get("updated_at") or "")

        # Recovery changes updated_at, but must not promote an older attempt.
        matches.sort(key=order, reverse=True)
        install_id, info = matches[0]
        result = dict(info)
        result.update(online_install_retry_relation(result, jobs))
        return install_id, result


def _install_history_integer(value, label, minimum, maximum):
    raw = "" if value is None else str(value)
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"在线脚本安装历史{label}无效")
    number = int(raw)
    if not minimum <= number <= maximum:
        raise ValueError(f"在线脚本安装历史{label}必须在 {minimum} 到 {maximum} 之间")
    return number


def list_online_install_jobs(limit=50, script_id="", status="", offset=0):
    limit = _install_history_integer(limit, "数量", 1, ONLINE_INSTALL_LIMIT)
    offset = _install_history_integer(offset, "偏移", 0, ONLINE_INSTALL_LIMIT)
    script_id = str(script_id or "").strip()
    if len(script_id) > 200 or any(ord(char) < 32 for char in script_id):
        raise ValueError("在线脚本安装历史脚本 ID 无效")
    status = str(status or "").strip()
    if status and status not in ONLINE_INSTALL_STATUSES:
        raise ValueError("在线脚本安装历史状态无效")
    with ONLINE_INSTALL_LOCK:
        jobs = _load_persisted_online_installs()
        for install_id, info in ONLINE_INSTALL_RUNNING.items():
            # In-memory terminal records can outlive their retained receipts.
            if install_id in jobs and isinstance(info, dict):
                jobs[str(install_id)] = _install_snapshot(info)
        for install_id, info in list(jobs.items()):
            if info.get("running") and install_id not in ONLINE_INSTALL_RUNNING:
                jobs[install_id] = get_online_install_job(install_id) or info
        items = [
            _install_snapshot(info)
            for info in jobs.values()
            if (not script_id or info.get("script_id") == script_id)
            and (not status or info.get("status") == status)
        ]

        def order(info):
            try:
                started = float(info.get("start_time") or 0)
            except (TypeError, ValueError, OverflowError):
                started = 0
            return started if math.isfinite(started) else 0, str(info.get("id") or "")

        items.sort(key=order, reverse=True)
        total = len(items)
        selected = items[offset:offset + limit]
        for info in selected:
            info.update(online_install_retry_relation(info, jobs))
        return selected, total
