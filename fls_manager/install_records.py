import json
import re
import threading

from .paths import DATA_DIR
from .storage import mutate_json, storage_transaction
from .utils import now_str


INSTALL_JOBS_FILE = DATA_DIR / "dependency_install_jobs.json"
INSTALL_JOBS_LOCK = threading.RLock()
INSTALL_JOB_LIMIT = 100
INSTALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
INSTALL_STATES = {"prepared", "running", "succeeded", "failed", "unknown", "cancelled"}
CANCEL_STATES = {"", "requested", "cancelled", "completed", "failed", "unknown"}


def _snapshot(info):
    install_id = str(info.get("id") or info.get("request_id") or "")
    return {
        "id": install_id,
        "request_id": str(info.get("request_id") or install_id),
        "kind": str(info.get("kind") or ""),
        "package": str(info.get("package") or ""),
        "log_file": str(info.get("log_file") or ""),
        "start_time": info.get("start_time") or 0,
        "running": bool(info.get("running", not bool(info.get("finished")))),
        "status": str(info.get("status") or "unknown"),
        "returncode": info.get("returncode"),
        "error": str(info.get("error") or ""),
        "updated_at": str(info.get("updated_at") or now_str()),
        "retry_of": str(info.get("retry_of") or ""),
        "retried_by": str(info.get("retried_by") or ""),
        "cancel_request_id": str(info.get("cancel_request_id") or ""),
        "cancel_status": str(info.get("cancel_status") or ""),
        "cancel_error": str(info.get("cancel_error") or ""),
    }


def _validate(info):
    if (
        not isinstance(info, dict)
        or not INSTALL_ID_RE.fullmatch(str(info.get("id") or ""))
        or info.get("request_id") != info.get("id")
        or info.get("kind") not in ("dependency", "runtime")
        or not isinstance(info.get("package"), str)
        or not info.get("package")
        or not isinstance(info.get("log_file"), str)
        or info.get("status") not in INSTALL_STATES
        or type(info.get("running")) is not bool
        or not isinstance(info.get("error"), str)
        or not isinstance(info.get("updated_at"), str)
        or (info.get("cancel_request_id") and not INSTALL_ID_RE.fullmatch(info["cancel_request_id"]))
        or info.get("cancel_status", "") not in CANCEL_STATES
        or not isinstance(info.get("cancel_error", ""), str)
        or (info.get("retry_of") and not INSTALL_ID_RE.fullmatch(info["retry_of"]))
        or (info.get("retried_by") and not INSTALL_ID_RE.fullmatch(info["retried_by"]))
    ):
        raise RuntimeError("依赖安装回执损坏，未覆盖原文件")


def _read_jobs():
    with storage_transaction():
        if INSTALL_JOBS_FILE.is_symlink():
            raise RuntimeError("依赖安装回执不能是符号链接，未读取外部文件")
        try:
            raw = INSTALL_JOBS_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"依赖安装回执损坏，未覆盖原文件：{exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("依赖安装回执损坏，未覆盖原文件")
        for key, value in data.items():
            if key != str(key) or not isinstance(value, dict) or value.get("id") != key:
                raise RuntimeError("依赖安装回执标识损坏，未覆盖原文件")
            _validate(value)
        return data


def persist_install_job(info):
    snapshot = _snapshot(info)
    _validate(snapshot)
    if snapshot["retry_of"] == snapshot["id"]:
        raise RuntimeError("依赖安装重试回执不能引用自身，未覆盖原文件")
    with INSTALL_JOBS_LOCK:
        if INSTALL_JOBS_FILE.is_symlink():
            raise RuntimeError("依赖安装回执不能是符号链接，未覆盖外部文件")

        def update(data):
            if not isinstance(data, dict):
                raise RuntimeError("依赖安装回执损坏，未覆盖原文件")
            for key, value in data.items():
                if key != str(key) or not isinstance(value, dict) or value.get("id") != key:
                    raise RuntimeError("依赖安装回执标识损坏，未覆盖原文件")
                _validate(value)
            existing = data.get(snapshot["id"])
            if isinstance(existing, dict) and any(
                existing.get(field) != snapshot[field]
                for field in ("kind", "package", "retry_of")
            ):
                raise RuntimeError("依赖安装请求标识已绑定其他安装，未覆盖原回执")
            retry_of = snapshot["retry_of"]
            if retry_of:
                previous = data.get(retry_of)
                if not isinstance(previous, dict):
                    raise RuntimeError("原依赖安装回执不存在，未启动重试")
                linked = str(previous.get("retried_by") or "")
                if linked and linked != snapshot["id"]:
                    raise RuntimeError("原依赖安装已经提交过重试，未重复启动")
            data[snapshot["id"]] = snapshot
            if retry_of:
                previous = dict(data[retry_of])
                previous["retried_by"] = snapshot["id"]
                previous["updated_at"] = snapshot["updated_at"]
                data[retry_of] = previous
            ordered = sorted(
                data.items(),
                key=lambda pair: str(pair[1].get("updated_at") or ""),
                reverse=True,
            )
            return dict(ordered[:INSTALL_JOB_LIMIT])

        mutate_json(INSTALL_JOBS_FILE, {}, update, strict=True)
    return dict(snapshot)


def claim_install_retry(info):
    """Atomically bind one retry to its parent before any process is launched."""
    snapshot = _snapshot(info)
    _validate(snapshot)
    if not snapshot["retry_of"] or snapshot["retry_of"] == snapshot["id"]:
        raise ValueError("依赖安装重试回执缺少有效的原安装 ID")
    with INSTALL_JOBS_LOCK:
        if INSTALL_JOBS_FILE.is_symlink():
            raise RuntimeError("依赖安装回执不能是符号链接，未覆盖外部文件")
        result = {"claimed": False, "job": None}

        def update(data):
            if not isinstance(data, dict):
                raise RuntimeError("依赖安装回执损坏，未覆盖原文件")
            for key, value in data.items():
                if not isinstance(key, str) or not isinstance(value, dict) or value.get("id") != key:
                    raise RuntimeError("依赖安装回执标识损坏，未覆盖原文件")
                _validate(value)
            parent = data.get(snapshot["retry_of"])
            if not isinstance(parent, dict):
                raise LookupError("原依赖安装回执不存在，未启动重试")
            existing_child_id = str(parent.get("retried_by") or "")
            if existing_child_id:
                existing_child = data.get(existing_child_id)
                if (
                    isinstance(existing_child, dict)
                    and existing_child.get("retry_of") == snapshot["retry_of"]
                    and existing_child.get("kind") == parent.get("kind")
                    and existing_child.get("package") == parent.get("package")
                ):
                    result["job"] = dict(existing_child)
                    return None
                raise RuntimeError("原安装重试回执损坏或缺失，未再次启动")
            existing = data.get(snapshot["id"])
            if isinstance(existing, dict):
                if any(existing.get(field) != snapshot[field] for field in ("kind", "package", "retry_of")):
                    raise RuntimeError("依赖安装重试 ID 已绑定其他安装，未覆盖原回执")
                result["job"] = dict(existing)
                return None
            if parent.get("running") or parent.get("status") not in ("failed", "unknown", "cancelled"):
                raise RuntimeError("原安装状态已变更，当前不允许重试")
            parent = dict(parent)
            parent["retried_by"] = snapshot["id"]
            parent["updated_at"] = snapshot["updated_at"]
            data[snapshot["retry_of"]] = parent
            data[snapshot["id"]] = snapshot
            ordered = sorted(
                data.items(),
                key=lambda pair: str(pair[1].get("updated_at") or ""),
                reverse=True,
            )
            result["claimed"] = True
            result["job"] = dict(snapshot)
            return dict(ordered[:INSTALL_JOB_LIMIT])

        mutate_json(INSTALL_JOBS_FILE, {}, update, strict=True)
        if result["job"] is None:
            raise RuntimeError("无法确认依赖安装重试回执")
        return result


def claim_install_cancel(install_id, request_id):
    if not INSTALL_ID_RE.fullmatch(str(install_id or "")) or not INSTALL_ID_RE.fullmatch(str(request_id or "")):
        raise ValueError("安装或取消请求标识无效")
    with INSTALL_JOBS_LOCK:
        if INSTALL_JOBS_FILE.is_symlink():
            raise RuntimeError("依赖安装回执不能是符号链接，未覆盖外部文件")
        result = {"claimed": False, "job": None}

        def update(data):
            if not isinstance(data, dict):
                raise RuntimeError("依赖安装回执损坏，未覆盖原文件")
            for key, value in data.items():
                if not isinstance(key, str) or not isinstance(value, dict) or value.get("id") != key:
                    raise RuntimeError("依赖安装回执标识损坏，未覆盖原文件")
                _validate(value)
            info = data.get(install_id)
            if not isinstance(info, dict):
                raise LookupError("安装记录不存在")
            current_id = str(info.get("cancel_request_id") or "")
            current_status = str(info.get("cancel_status") or "")
            if current_id:
                if current_id == request_id:
                    result["job"] = dict(info)
                    return None
                if current_status not in ("failed", "unknown"):
                    result["job"] = dict(info)
                    return None
            if not info.get("running") or info.get("status") != "running":
                raise RuntimeError("安装已结束或状态未知，不能确认取消")
            info = dict(info)
            info.update(
                cancel_request_id=request_id,
                cancel_status="requested",
                cancel_error="",
                updated_at=now_str(),
            )
            data[install_id] = info
            result["claimed"] = True
            result["job"] = dict(info)
            return data

        mutate_json(INSTALL_JOBS_FILE, {}, update, strict=True)
        if result["job"] is None:
            raise RuntimeError("无法确认安装取消回执")
        return result


def get_install_job(install_id, live_jobs=None):
    install_id = str(install_id or "")
    if not INSTALL_ID_RE.fullmatch(install_id):
        return None
    with INSTALL_JOBS_LOCK:
        live = live_jobs.get(install_id) if isinstance(live_jobs, dict) else None
        if isinstance(live, dict):
            return _snapshot(live)
        info = _read_jobs().get(install_id)
        if not isinstance(info, dict):
            return None
        info = dict(info)
        if info.get("running"):
            info["running"] = False
            info["status"] = "unknown"
            info["returncode"] = None
            info["error"] = "；".join(filter(None, (
                info.get("error"),
                "面板重启后无法确认安装进程结果，请查看安装日志后再决定是否重试。",
            )))
            if info.get("cancel_status") == "requested":
                info["cancel_status"] = "unknown"
                info["cancel_error"] = "面板重启后无法确认取消信号是否生效，请检查原安装日志。"
            info["updated_at"] = now_str()
            try:
                persist_install_job(info)
            except Exception as exc:
                info["error"] += f"；保存恢复状态失败：{exc}"
        return _snapshot(info)


def list_install_jobs(limit=50, kind="", live_jobs=None):
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("安装历史数量无效") from exc
    if not 1 <= limit <= INSTALL_JOB_LIMIT:
        raise ValueError(f"安装历史数量必须在 1 到 {INSTALL_JOB_LIMIT} 之间")
    kind = str(kind or "").strip()
    if kind and kind not in ("dependency", "runtime"):
        raise ValueError("安装类型无效")
    with INSTALL_JOBS_LOCK:
        records = _read_jobs()
        items = []
        for install_id, value in records.items():
            if kind and value.get("kind") != kind:
                continue
            if value.get("running"):
                value = get_install_job(install_id, live_jobs) or value
            items.append(_snapshot(value))
        items.sort(key=lambda value: (str(value.get("updated_at") or ""), str(value.get("id") or "")), reverse=True)
        return items[:limit]
