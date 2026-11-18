import json
import re
import threading
import os
import uuid

from ..paths import DATA_DIR
from ..storage import mutate_json, storage_transaction
from ..utils import now_str

DEFAULT_ONLINE_SCRIPT_SOURCE = "https://cdn.jsdelivr.net/gh/liyw0205/fls-scripts@main/index.json"
ONLINE_SCRIPT_CACHE_FILE = DATA_DIR / "online_scripts_cache.json"
ONLINE_TASK_LINK_REQUESTS_FILE = DATA_DIR / "online_task_link_requests.json"
ONLINE_TASK_IMPORT_REQUESTS_FILE = DATA_DIR / "online_task_import_requests.json"

ONLINE_INSTALL_RUNNING = {}
ONLINE_INSTALL_STOPPING = set()
ONLINE_INSTALL_RETRYING = set()
ONLINE_INSTALL_JOBS_FILE = DATA_DIR / "online_install_jobs.json"
ONLINE_INSTALL_LOCK = threading.RLock()
ONLINE_INSTALL_LIMIT = 100
ONLINE_INSTALL_TIMEOUT_SECONDS = 1800
ONLINE_REFRESH_STATE_FILE = DATA_DIR / "online_script_refresh.json"
ONLINE_REFRESH_HISTORY_FILE = DATA_DIR / "online_script_refresh_requests.json"
ONLINE_REFRESH_LOCK = threading.RLock()
ONLINE_TASK_IMPORT_LOCK = threading.RLock()


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


def get_online_task_import_request(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return None
    with ONLINE_TASK_IMPORT_LOCK, storage_transaction():
        return dict(_read_task_import_requests().get(request_id) or {}) or None


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
            temp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, ONLINE_REFRESH_STATE_FILE)
            for candidate in (previous_snapshot, snapshot):
                if candidate.get("request_id"):
                    history[candidate["request_id"]] = candidate
            if snapshot["request_id"]:
                history = dict(sorted(
                    history.items(),
                    key=lambda pair: str(pair[1].get("updated_at") or ""),
                    reverse=True,
                )[:200])
            history_temp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(history_temp, ONLINE_REFRESH_HISTORY_FILE)
        finally:
            temp.unlink(missing_ok=True)
            history_temp.unlink(missing_ok=True)


def update_online_refresh_state(**updates):
    with ONLINE_REFRESH_LOCK:
        previous = dict(ONLINE_REFRESH_STATE)
        ONLINE_REFRESH_STATE.update(updates)
        try:
            persist_online_refresh(ONLINE_REFRESH_STATE)
        except Exception:
            ONLINE_REFRESH_STATE.clear()
            ONLINE_REFRESH_STATE.update(previous)
            raise
        return dict(ONLINE_REFRESH_STATE)


def get_online_refresh_state():
    with ONLINE_REFRESH_LOCK:
        current = dict(ONLINE_REFRESH_STATE)
        loaded_from_file = False
        if ONLINE_REFRESH_STATE_FILE.exists():
            data = _read_online_refresh_file()
            if data and not current.get("id"):
                current.update(data)
                loaded_from_file = True
        if loaded_from_file and current.get("running"):
            current["running"] = False
            current["status"] = "结果未知"
            current["error"] = "面板重启后无法确认在线脚本源同步是否完成；保留旧缓存，请查看刷新日志后重试。"
            current["message"] = ""
            current["updated_at"] = now_str()
            ONLINE_REFRESH_STATE.update(current)
            persist_online_refresh(current)
        return current


def get_online_refresh_request(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return None
    with ONLINE_REFRESH_LOCK, storage_transaction():
        value = _read_online_refresh_history_file().get(request_id)
        return dict(value) if isinstance(value, dict) else None


def get_online_refresh_history(limit=50):
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise ValueError("刷新历史条数必须为整数")
    with ONLINE_REFRESH_LOCK, storage_transaction():
        history = _read_online_refresh_history_file()
        current = _read_online_refresh_file()
        if current.get("request_id") and current["request_id"] not in history:
            history[current["request_id"]] = current
        ordered = sorted(
            history.values(),
            key=lambda value: str(value.get("updated_at") or ""),
            reverse=True,
        )
        return [dict(value) for value in ordered[:limit]]


def confirm_online_refresh_result(request_id, cache_fingerprint, source_fingerprint):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("刷新请求标识无效")
    if not re.fullmatch(r"[a-f0-9]{64}", str(cache_fingerprint or "")):
        raise ValueError("当前缓存指纹无效")
    if not re.fullmatch(r"[a-f0-9]{64}", str(source_fingerprint or "")):
        raise ValueError("在线脚本源版本指纹无效")
    with ONLINE_REFRESH_LOCK, storage_transaction():
        history = _read_online_refresh_history_file()
        record = dict(history.get(request_id) or {})
        current_matches = ONLINE_REFRESH_STATE.get("request_id") == request_id
        if not record and current_matches:
            record = _validate_refresh_snapshot(ONLINE_REFRESH_STATE)
        if not record:
            return {"_missing": True}
        if record.get("confirmation"):
            if record.get("confirmation_fingerprint") != cache_fingerprint:
                return {"_conflict": True, **record}
            return {"_replayed": True, **record}
        if record.get("running") or record.get("status") != "结果未知":
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
            previous = dict(ONLINE_REFRESH_STATE)
            ONLINE_REFRESH_STATE.clear()
            ONLINE_REFRESH_STATE.update(snapshot)
            try:
                persist_online_refresh(snapshot)
            except Exception:
                ONLINE_REFRESH_STATE.clear()
                ONLINE_REFRESH_STATE.update(previous)
                raise
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
        original = history.get(original_request_id)
        if not original:
            return {"_missing": True}
        if original.get("status") not in ("失败", "部分完成", "结果未知", "启动失败"):
            return {"_not_retryable": True, **dict(original)}
        if str(original.get("proxy_id") or "") != str(proxy_id or ""):
            return {"_binding_conflict": True, **dict(original)}
        expected_cache = str(original.get("published_cache_fingerprint") or original.get("expected_cache_fingerprint") or "")
        if not expected_cache or cache_fingerprint != expected_cache:
            return {"_stale": True, **dict(original)}
        if not original.get("source_fingerprint") or source_fingerprint != original.get("source_fingerprint"):
            return {"_stale": True, **dict(original)}
        existing = history.get(request_id)
        if existing:
            if existing.get("retry_of") != original_request_id:
                return {"_binding_conflict": True, **dict(existing)}
            return {"_replay": True, **dict(existing)}
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
        persist_online_refresh(info)
        ONLINE_REFRESH_STATE.clear()
        ONLINE_REFRESH_STATE.update(info)
        return dict(info)


def reserve_online_refresh(proxy_id="", request_id="", cache_fingerprint="", source_fingerprint=""):
    with ONLINE_REFRESH_LOCK:
        current = get_online_refresh_state()
        if current.get("running"):
            return None
        import uuid
        request_id = str(request_id or uuid.uuid4().hex)
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("在线脚本源刷新请求标识无效，请刷新页面后重试")
        if current.get("request_id") == request_id:
            if str(current.get("proxy_id") or "") != str(proxy_id or ""):
                return {"_binding_conflict": True, **dict(current)}
            if current.get("status") in ("已完成", "部分完成"):
                return {"_replay": True, **dict(current)}
            if current.get("status") == "结果未知" or current.get("cache_published"):
                return {"_unknown": True, **dict(current)}
        else:
            history = _read_online_refresh_history_file()
            previous_request = history.get(request_id)
            if previous_request:
                if str(previous_request.get("proxy_id") or "") != str(proxy_id or ""):
                    return {"_binding_conflict": True, **dict(previous_request)}
                if previous_request.get("status") in ("已完成", "部分完成"):
                    return {"_replay": True, **dict(previous_request)}
                return {
                    "_unknown": True,
                    **dict(previous_request),
                    "error": previous_request.get("error") or "原刷新请求已有持久化记录，请先查询原日志后再创建新的刷新请求",
                }
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
        except Exception:
            ONLINE_REFRESH_STATE.clear()
            ONLINE_REFRESH_STATE.update(previous)
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


def _read_install_jobs():
    """Read install history without treating corruption as an empty history."""
    with storage_transaction():
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

        def update(data):
            if not isinstance(data, dict):
                raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：根节点必须是对象")
            jobs = {}
            for key, value in data.items():
                if not isinstance(key, str) or not isinstance(value, dict) or value.get("id") != key:
                    raise RuntimeError("在线脚本安装记录损坏，未覆盖原文件：记录标识不一致")
                jobs[key] = value
            retry_of = str(snapshot.get("retry_of") or "")
            previous = jobs.get(retry_of)
            if retry_of and isinstance(previous, dict):
                previous = dict(previous)
                previous["retried_by"] = install_id
                jobs[retry_of] = previous
            jobs.pop(install_id, None)
            jobs = {install_id: snapshot, **jobs}
            ordered = sorted(
                jobs.items(),
                key=lambda pair: str(pair[1].get("updated_at") or ""),
                reverse=True,
            )
            return dict(ordered[:ONLINE_INSTALL_LIMIT])

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
        return install_id, dict(info)
