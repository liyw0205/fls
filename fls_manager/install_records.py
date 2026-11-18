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
INSTALL_STATES = {"prepared", "running", "succeeded", "failed", "unknown"}


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
            data[snapshot["id"]] = snapshot
            ordered = sorted(
                data.items(),
                key=lambda pair: str(pair[1].get("updated_at") or ""),
                reverse=True,
            )
            return dict(ordered[:INSTALL_JOB_LIMIT])

        mutate_json(INSTALL_JOBS_FILE, {}, update, strict=True)
    return dict(snapshot)


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
            info["updated_at"] = now_str()
            try:
                persist_install_job(info)
            except Exception as exc:
                info["error"] += f"；保存恢复状态失败：{exc}"
        return _snapshot(info)


def list_install_jobs(limit=50, kind=""):
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
                value = get_install_job(install_id) or value
            items.append(_snapshot(value))
        items.sort(key=lambda value: (str(value.get("updated_at") or ""), str(value.get("id") or "")), reverse=True)
        return items[:limit]
