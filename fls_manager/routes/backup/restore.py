from ._common import *
import hashlib
import json
import re
import threading
import uuid
from flask import redirect, send_file
from contextlib import contextmanager, nullcontext, ExitStack
from ... import models
from ...csrf import csrf_token
from ...storage import storage_transaction
from ...data_access import directory_access
from ...ui.components import page_header_card


class BackupRestoreWriteError(RuntimeError):
    def __init__(self, message, live_state_unchanged=True):
        super().__init__(message)
        self.live_state_unchanged = live_state_unchanged


try:
    RESTORE_RECEIPT_DETAIL_LIMIT = max(1, int(os.environ.get("FLS_BACKUP_RESTORE_RECEIPT_LIMIT", "200")))
except (TypeError, ValueError):
    RESTORE_RECEIPT_DETAIL_LIMIT = 200


def _restore_archive_index_path():
    return BACKUP_RESTORE_REQUESTS_DIR / ".archive"


def _restore_archive_backup_path():
    return BACKUP_RESTORE_REQUESTS_DIR / ".archive.bak"


RESTORE_ARCHIVE_MAINTENANCE_NAME = ".archive-maintenance"
_RESTORE_ARCHIVE_PROCESS_LOCK_STATE = threading.local()


@contextmanager
def _restore_archive_process_lock():
    depth = getattr(_RESTORE_ARCHIVE_PROCESS_LOCK_STATE, "depth", 0)
    if depth:
        _RESTORE_ARCHIVE_PROCESS_LOCK_STATE.depth = depth + 1
        try:
            yield
        finally:
            _RESTORE_ARCHIVE_PROCESS_LOCK_STATE.depth -= 1
        return

    lock_path = BACKUP_RESTORE_REQUESTS_DIR.with_name(BACKUP_RESTORE_REQUESTS_DIR.name + ".lock")
    if lock_path.is_symlink():
        raise RuntimeError("备份恢复跨进程锁不能是符号链接")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise RuntimeError("备份恢复跨进程锁必须是普通文件")
    if os.name == "nt":
        import msvcrt
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
    _RESTORE_ARCHIVE_PROCESS_LOCK_STATE.depth = 1
    try:
        yield
    finally:
        _RESTORE_ARCHIVE_PROCESS_LOCK_STATE.depth = 0
        try:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _restore_archive_maintenance_path():
    return BACKUP_RESTORE_REQUESTS_DIR / RESTORE_ARCHIVE_MAINTENANCE_NAME


def _restore_archive_maintenance_notice(state):
    if state["required"] and state["kind"] == "publication":
        return "恢复归档索引发布尚未完成；请求明细已保留，请核对历史后重试。"
    if state["required"]:
        return "恢复归档索引冗余修复尚未完成；原恢复请求仍保留，请核对后重试。"
    if state["kind"] == "publication":
        return "恢复归档索引发布已确认；原恢复请求未重复提交目录。"
    return "恢复归档索引副本已自动合并并修复；原恢复请求已保留，重放不会再次覆盖目录。"


def _read_restore_archive_maintenance_state():
    directory = BACKUP_RESTORE_REQUESTS_DIR
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise RuntimeError("备份恢复维护状态目录无效")
    path = _restore_archive_maintenance_path()
    if path.is_symlink() or path.exists() and not path.is_file():
        raise RuntimeError("备份恢复维护状态文件无效")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError("备份恢复维护状态损坏") from exc
    if (not isinstance(state, dict) or state.get("version") != 1
        or not isinstance(state.get("event_id"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", state["event_id"])
        or type(state.get("required")) is not bool
        or not isinstance(state.get("fingerprint"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", state["fingerprint"])
        or not isinstance(state.get("target_digest"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", state["target_digest"])
        or state.get("kind") not in ("repair", "publication", "unavailable")
        or "acknowledged" in state and type(state["acknowledged"]) is not bool):
        raise RuntimeError("备份恢复维护状态结构无效")
    state.setdefault("acknowledged", False)
    return state


def _write_restore_archive_maintenance_state(state):
    directory = BACKUP_RESTORE_REQUESTS_DIR
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("备份恢复回执目录无效")
    path = _restore_archive_maintenance_path()
    temp = path.with_name(path.name + ".tmp")
    if path.is_symlink() or path.exists() and not path.is_file():
        raise RuntimeError("备份恢复维护状态文件无效")
    if temp.is_symlink() or temp.exists() and not temp.is_file():
        raise RuntimeError("备份恢复维护状态临时文件无效")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(state, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _sync_restore_archive_directory()
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _clear_restore_archive_maintenance_state(event_id):
    state = _read_restore_archive_maintenance_state()
    if state is None or state["event_id"] != event_id:
        return False
    _restore_archive_maintenance_path().unlink()
    _sync_restore_archive_directory()
    return True


def _restore_archive_observation(parsed, index, errors):
    paths = (_restore_archive_index_path(), _restore_archive_backup_path())
    copies = []
    for path, candidate in zip(paths, parsed):
        if candidate is None:
            if path.is_symlink():
                copies.append("symlink")
            elif path.exists():
                copies.append("invalid")
            else:
                copies.append("missing")
        else:
            copies.append("current" if candidate == index else "stale")
    material = {
        "copies": copies,
        "index": hashlib.sha256(json.dumps(
            index, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest(),
        "errors": [bool(error) for error in errors],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _restore_archive_index_digest(index):
    return hashlib.sha256(json.dumps(
        index, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _persist_restore_archive_maintenance(required, fingerprint, target_digest, kind):
    previous = _read_restore_archive_maintenance_state()
    reusable = previous and previous["target_digest"] == target_digest and (not required or previous["required"])
    event_id = previous["event_id"] if reusable else uuid.uuid4().hex
    state = {
        "version": 1, "event_id": event_id, "required": bool(required),
        "fingerprint": fingerprint, "target_digest": target_digest, "kind": kind,
        "acknowledged": False,
    }
    if previous == state:
        return previous
    _write_restore_archive_maintenance_state(state)
    return state


def _restore_archive_maintenance_payload(state=None, *, unreadable=False):
    if unreadable:
        return {
            "event_id": "",
            "required": True,
            "notice": "恢复归档维护状态无法读取；原恢复请求与目录均未清理，请重试核对。",
        }
    if state is None:
        state = _read_restore_archive_maintenance_state()
    if state is None:
        return None
    if state.get("acknowledged"):
        try:
            _sync_restore_archive_directory()
            return None
        except Exception:
            return {
                "event_id": state["event_id"],
                "required": False,
                "acknowledged": True,
                "notice": "维护提示已确认，但目录同步尚未确认；请重试确认以完成恢复。",
            }
    return {
        "event_id": state["event_id"],
        "required": state["required"],
        "acknowledged": False,
        "notice": _restore_archive_maintenance_notice(state),
    }


def _attach_restore_archive_maintenance(payload, state=None, *, unreadable=False):
    maintenance = _restore_archive_maintenance_payload(state, unreadable=unreadable)
    payload["maintenance"] = maintenance
    notices = [payload.get("maintenance_error", "")]
    if maintenance:
        notices.append(maintenance["notice"])
    payload["maintenance_error"] = "；".join(dict.fromkeys(filter(None, notices)))
    payload["maintenance_required"] = bool(maintenance and maintenance["required"])
    return payload


def _attach_current_restore_archive_maintenance(payload):
    try:
        state = _read_restore_archive_maintenance_state()
        if state is None:
            return _attach_restore_archive_maintenance(payload, unreadable=True)
        return _attach_restore_archive_maintenance(payload, state)
    except Exception:
        return _attach_restore_archive_maintenance(payload, unreadable=True)


def _read_restore_archive_index_file(path):
    if path.is_symlink() or path.exists() and not path.is_file():
        raise RuntimeError("恢复归档索引副本必须是普通文件")
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"恢复归档索引副本无法读取：{exc}") from exc
    if not isinstance(index, dict) or index.get("version") != 1 or not isinstance(index.get("records"), dict):
        raise RuntimeError("恢复归档索引副本损坏")
    for request_id, record in index["records"].items():
        if (not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id)
            or not isinstance(record, dict) or record.get("request_id") != request_id
            or record.get("archived") is not True
            or not isinstance(record.get("archive_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", record["archive_sha256"])
            or record.get("restore_items") not in (["data"], ["scripts"], ["data", "scripts"])
            or type(record.get("restore_deps")) is not bool
            or record.get("state") not in ("restored", "failed")
            or type(record.get("scheduler_reload_required")) is not bool
            or type(record.get("dependencies_failed")) is not bool):
            raise RuntimeError("恢复归档索引副本记录损坏")
    return index


def _merge_restore_archive_indexes(indexes):
    records = {}
    for index in indexes:
        if index is None:
            continue
        for request_id, record in index["records"].items():
            existing = records.get(request_id)
            if existing and not _restore_request_identity_matches(existing, record):
                raise RuntimeError("主备恢复归档索引包含冲突的请求身份")
            if existing is None or str(record.get("archived_at") or "") > str(existing.get("archived_at") or ""):
                records[request_id] = record
    return {"version": 1, "records": records}


def _sync_restore_archive_directory():
    if os.name == "nt":
        return
    directory_fd = os.open(
        BACKUP_RESTORE_REQUESTS_DIR,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_restore_archive_index_file(path, index):
    directory = BACKUP_RESTORE_REQUESTS_DIR
    if directory.is_symlink() or not directory.is_dir():
        raise RuntimeError("备份恢复回执目录无效")
    if path.is_symlink() or path.exists() and not path.is_file():
        raise RuntimeError("恢复归档索引副本必须是普通文件")
    temp = path.with_name(path.name + ".tmp")
    if temp.is_symlink() or temp.exists() and not temp.is_file():
        raise RuntimeError("恢复归档索引临时文件必须是普通文件")

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(index, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _sync_restore_archive_directory()
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def _write_restore_archive_index_pair(index):
    # Publish the redundant copy first; details are never removed until both are durable.
    _write_restore_archive_index_file(_restore_archive_backup_path(), index)
    _write_restore_archive_index_file(_restore_archive_index_path(), index)


def _read_restore_archive_index(*, require_redundancy=False):
    directory = BACKUP_RESTORE_REQUESTS_DIR
    if directory.is_symlink():
        raise RuntimeError("备份恢复回执目录不能是符号链接")
    if not directory.exists():
        return {"version": 1, "records": {}}
    if not directory.is_dir():
        raise RuntimeError("备份恢复回执目录无效")
    primary_path, backup_path = _restore_archive_index_path(), _restore_archive_backup_path()
    parsed, errors = [], []
    for path in (primary_path, backup_path):
        try:
            parsed.append(_read_restore_archive_index_file(path))
        except Exception as exc:
            parsed.append(None)
            errors.append(f"{path.name}: {exc}")
    if all(index is None for index in parsed):
        if errors:
            empty_index = {"version": 1, "records": {}}
            fingerprint = _restore_archive_observation(parsed, empty_index, errors)
            _persist_restore_archive_maintenance(
                True, fingerprint, _restore_archive_index_digest(empty_index), "unavailable",
            )
            raise RuntimeError("主备恢复归档索引均不可用")
        _read_restore_archive_maintenance_state()
        return {"version": 1, "records": {}}

    index = _merge_restore_archive_indexes(parsed)
    needs_repair = any(candidate is None or candidate != index for candidate in parsed)
    maintenance = _read_restore_archive_maintenance_state()
    if needs_repair:
        fingerprint = _restore_archive_observation(parsed, index, errors)
        target_digest = _restore_archive_index_digest(index)
        maintenance = _persist_restore_archive_maintenance(True, fingerprint, target_digest, "repair")
        try:
            _write_restore_archive_index_pair(index)
        except Exception:
            if require_redundancy:
                raise RuntimeError("恢复归档索引冗余副本修复尚未完成")
            return index
        try:
            _persist_restore_archive_maintenance(False, fingerprint, target_digest, "repair")
        except Exception:
            if require_redundancy:
                raise RuntimeError("恢复归档维护状态尚未确认")
            return index
    elif maintenance and maintenance["required"]:
        target_digest = _restore_archive_index_digest(index)
        if maintenance["kind"] == "unavailable" or target_digest != maintenance["target_digest"]:
            return index
        try:
            _sync_restore_archive_directory()
            if maintenance["kind"] == "publication":
                _persist_restore_archive_maintenance(
                    False, maintenance["fingerprint"], target_digest, "publication",
                )
            else:
                _persist_restore_archive_maintenance(
                    False, maintenance["fingerprint"], target_digest, "repair",
                )
        except Exception as exc:
            if require_redundancy:
                raise RuntimeError("恢复归档维护状态尚未确认") from exc
            return index
    elif require_redundancy:
        try:
            _sync_restore_archive_directory()
        except Exception as exc:
            fingerprint = _restore_archive_observation(parsed, index, errors)
            _persist_restore_archive_maintenance(
                True, fingerprint, _restore_archive_index_digest(index), "repair",
            )
            raise RuntimeError("恢复归档索引副本目录同步尚未完成") from exc
    return index


def _restore_request_identity_matches(left, right):
    return (left.get("request_id") == right.get("request_id")
        and left.get("archive_sha256") == right.get("archive_sha256")
        and left.get("restore_items") == right.get("restore_items")
        and bool(left.get("restore_deps", False)) == bool(right.get("restore_deps", False)))


def _restore_request_archivable(record):
    return record.get("state") == "restored" and not record.get("scheduler_reload_required") \
        and not record.get("dependencies_failed") and record.get("scheduler_reloaded", True)


def _restore_archive_summary(record):
    if not _restore_request_archivable(record):
        raise RuntimeError("恢复请求仍待处理，不能归档")
    return {
        key: record.get(key)
        for key in ("request_id", "archive_sha256", "filename", "restore_items", "restore_deps",
                    "state", "scheduler_reload_required", "dependencies_failed", "scheduler_reloaded",
                    "created_at", "updated_at", "restored_at")
        if key in record
    } | {"archived": True, "archived_at": now_str()}


def _compact_restore_receipts():
    with storage_transaction(live_data=False), BACKUP_RESTORE_REQUEST_LOCK, _restore_archive_process_lock():
        directory = BACKUP_RESTORE_REQUESTS_DIR
        if directory.is_symlink():
            raise RuntimeError("备份恢复回执目录不能是符号链接")
        if not directory.exists():
            return 0
        if not directory.is_dir():
            raise RuntimeError("备份恢复回执目录无效")

        index = _read_restore_archive_index(require_redundancy=True)
        details = {}
        for path in directory.iterdir():
            if not path.name.endswith(".json"):
                continue
            request_id = path.name[:-5]
            if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                raise RuntimeError("备份恢复历史中存在无效回执文件")
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("备份恢复回执必须是普通文件")
            record = _read_restore_request(request_id)
            if record is None:
                raise RuntimeError("备份恢复历史在读取期间发生变化，请重试")
            details[request_id] = record

        # The index is committed before detail files are removed, so interrupted cleanup is repeatable.
        for request_id, archived in index["records"].items():
            record = details.get(request_id)
            if record is not None:
                if not _restore_request_identity_matches(record, archived):
                    raise RuntimeError("归档回执与原恢复请求身份不一致")
                _restore_request_path(request_id).unlink()
                details.pop(request_id)

        candidates = [record for record in details.values() if _restore_request_archivable(record)]
        candidates.sort(key=lambda item: (item.get("updated_at", item.get("created_at", "")), item["request_id"]))
        excess = max(0, len(candidates) - RESTORE_RECEIPT_DETAIL_LIMIT)
        selected = candidates[:excess]
        if not selected:
            return 0

        for record in selected:
            request_id = record["request_id"]
            summary = _restore_archive_summary(record)
            existing = index["records"].get(request_id)
            if existing and not _restore_request_identity_matches(existing, summary):
                raise RuntimeError("恢复归档请求标识冲突")
            index["records"][request_id] = existing or summary
        target_digest = _restore_archive_index_digest(index)
        fingerprint = hashlib.sha256(("archive-publication:" + target_digest).encode()).hexdigest()
        pending = _persist_restore_archive_maintenance(True, fingerprint, target_digest, "publication")
        _write_restore_archive_index_pair(index)
        _clear_restore_archive_maintenance_state(pending["event_id"])
        for record in selected:
            _restore_request_path(record["request_id"]).unlink()
        return len(selected)


def _restore_request_path(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        raise ValueError("恢复请求标识无效")
    if BACKUP_RESTORE_REQUESTS_DIR.is_symlink():
        raise RuntimeError("恢复回执目录不能是符号链接")
    path = BACKUP_RESTORE_REQUESTS_DIR / (request_id + ".json")
    if path.is_symlink() or path.exists() and not path.is_file():
        raise RuntimeError("恢复请求回执必须是普通文件")
    return path


def _read_restore_request(request_id):
    path = _restore_request_path(request_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if (not isinstance(record, dict) or record.get("request_id") != request_id
        or record.get("state") not in ("received", "committing", "restored", "failed", "unknown")
        or not isinstance(record.get("archive_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", record["archive_sha256"])
        or record.get("restore_items") not in (["data"], ["scripts"], ["data", "scripts"])
        or any(type(record.get(key, False)) is not bool for key in ("restore_deps", "scheduler_reload_required", "dependencies_failed"))):
        raise RuntimeError("备份恢复回执损坏，未覆盖原文件")
    return record


def _save_restore_request(record):
    record["updated_at"] = now_str()
    write_json(_restore_request_path(record["request_id"]), record)


def _bounded_restore_diagnostic(value):
    if not isinstance(value, str):
        return ""
    return "".join(
        char for char in value
        if ord(char) >= 32 and not 127 <= ord(char) <= 159
    )[:1000]


def _restore_request_response(record):
    restored = record.get("state") == "restored"
    unknown = record.get("state") == "unknown"
    archived = record.get("archived") is True
    filename = record.get("filename", "")
    updated_at = record.get("updated_at", record.get("created_at", ""))
    if not isinstance(filename, str):
        filename = ""
    if not isinstance(updated_at, str):
        updated_at = ""
    reload_required = bool(record.get("scheduler_reload_required")) or unknown and not record.get("scheduler_reloaded", False)
    dependencies_failed = bool(record.get("dependencies_failed"))
    resolved = restored and not reload_required and not dependencies_failed
    status = 410 if archived else 200 if resolved or record.get("state") == "received" else 503 if restored or unknown else 409 if record.get("state") == "committing" else 500
    if archived:
        message = "该恢复请求已归档，不会再次覆盖目录；如需恢复请创建新的请求。"
    elif unknown:
        message = "恢复期间连接中断，无法确认目录是否已替换。请检查任务和脚本；不要重新导入。"
    elif reload_required:
        message = "目录已恢复，调度器尚未确认加载。请仅重新加载调度器，不要重新导入。"
    elif dependencies_failed:
        message = "目录已恢复，依赖恢复未完成。请修复依赖，不要重新导入目录。"
    elif restored:
        message = "该恢复请求已完成，未重复导入目录。"
    elif record.get("state") == "committing":
        message = "原恢复请求正在提交目录，请稍后查询。"
    elif record.get("state") == "received":
        message = "原恢复请求尚未提交目录。请选择完全相同的备份文件后，可继续原请求。"
    else:
        message = "恢复未完成且目录未提交。检查错误后可使用相同文件继续原请求。"
    return {
        "ok": resolved, "resolved": resolved, "msg": message,
        "request_id": record["request_id"], "state": record["state"],
        "archived": archived,
        "filename": _bounded_restore_diagnostic(filename)[:240],
        "updated_at": updated_at,
        "restore_deps": bool(record.get("restore_deps", False)),
        "restored": restored, "restore_outcome_unknown": unknown,
        "restored_items": record.get("restore_items", []),
        "scheduler_reload_required": reload_required,
        "reload_url": f"/api/backup/restore/{record['request_id']}/reload" if reload_required else "",
        "dependencies_failed": dependencies_failed,
        "dependencies": record.get("dependencies", ""),
        "dependency_log": record.get("dependency_log", ""),
        "error": _bounded_restore_diagnostic(record.get("error"))
        if record.get("state") in ("failed", "unknown") else "",
        "scheduler_error": _bounded_restore_diagnostic(record.get("scheduler_error"))
        if reload_required else "",
    }, status


@bp.route("/api/backup/restore-history")
def api_backup_restore_history():
    try:
        raw_limit = request.args.get("limit", "100")
        raw_offset = request.args.get("offset", "0")
        if not re.fullmatch(r"[0-9]{1,6}", raw_limit) or not re.fullmatch(r"[0-9]{1,9}", raw_offset):
            raise ValueError
        limit, offset = int(raw_limit), int(raw_offset)
        if not 1 <= limit <= 100 or offset > 100000000:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="恢复历史分页参数无效"), 400

    try:
        with storage_transaction(live_data=False), BACKUP_RESTORE_REQUEST_LOCK, _restore_archive_process_lock():
            maintenance_notices = []
            try:
                _compact_restore_receipts()
            except Exception as exc:
                maintenance_notices.append(_bounded_restore_diagnostic(str(exc)))

            directory = BACKUP_RESTORE_REQUESTS_DIR
            if directory.is_symlink():
                raise RuntimeError("备份恢复回执目录不能是符号链接")
            index = _read_restore_archive_index()
            records_by_id = dict(index["records"])
            if directory.exists():
                if not directory.is_dir():
                    raise RuntimeError("备份恢复回执目录无效")
                for path in directory.iterdir():
                    if not path.name.endswith(".json"):
                        continue
                    request_id = path.name[:-5]
                    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                        raise RuntimeError("备份恢复历史中存在无效回执文件")
                    if path.is_symlink() or not path.is_file():
                        raise RuntimeError("备份恢复回执必须是普通文件")
                    record = _read_restore_request(request_id)
                    if record is None:
                        raise RuntimeError("备份恢复历史在读取期间发生变化，请重试")
                    archived = records_by_id.get(request_id)
                    if archived:
                        if not _restore_request_identity_matches(record, archived):
                            raise RuntimeError("归档回执与原恢复请求身份不一致")
                        continue
                    if record["state"] == "committing" and not restore_activity_active():
                        record["state"] = "unknown"
                        record["restore_outcome_unknown"] = True
                        _save_restore_request(record)
                    records_by_id[request_id] = record

            maintenance = _read_restore_archive_maintenance_state()
            maintenance_payload = _restore_archive_maintenance_payload(maintenance)
            if maintenance_payload:
                maintenance_notices.append(maintenance_payload["notice"])

            records = []
            for record in records_by_id.values():
                payload, _ = _restore_request_response(record)
                records.append(payload)

            records.sort(
                key=lambda item: (item.get("updated_at", ""), item.get("request_id", "")),
                reverse=True,
            )
            snapshot = hashlib.sha256(json.dumps(
                records, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            total = len(records)
            items = records[offset:offset + limit]
            return jsonify(_attach_restore_archive_maintenance(
                {
                    "ok": True,
                    "items": items,
                    "snapshot": snapshot,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "has_more": offset + len(items) < total,
                    "active": restore_activity_active(),
                    "maintenance_error": "；".join(dict.fromkeys(filter(None, maintenance_notices))),
                },
                maintenance,
            ))
    except Exception:
        return jsonify(_attach_current_restore_archive_maintenance(
            {"ok": False, "msg": "备份恢复历史无法完整读取，请修复回执后重试"},
        )), 500


@bp.route("/api/backup/restore/<request_id>")
def api_backup_restore_request(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return jsonify(ok=False, msg="恢复请求标识无效"), 400
    try:
        with storage_transaction(live_data=False), BACKUP_RESTORE_REQUEST_LOCK, _restore_archive_process_lock():
            archived = _read_restore_archive_index()["records"].get(request_id)
            maintenance = _read_restore_archive_maintenance_state()
            record = _read_restore_request(request_id)
            if archived:
                if record and not _restore_request_identity_matches(record, archived):
                    raise RuntimeError("归档回执与原恢复请求身份不一致")
                record = archived
            if record and record["state"] == "committing" and not restore_activity_active():
                record["state"] = "unknown"
                record["restore_outcome_unknown"] = True
                try:
                    _save_restore_request(record)
                except Exception:
                    pass
    except Exception:
        return jsonify(_attach_current_restore_archive_maintenance(
            {"ok": False, "msg": "备份恢复回执无法读取，请修复文件后重试", "request_id": request_id},
        )), 500
    if record is None:
        return jsonify(_attach_restore_archive_maintenance(
            {"ok": False, "msg": "未找到原恢复请求。请重新选择文件后提交新请求。", "request_received": False},
            maintenance,
        )), 404
    payload, status = _restore_request_response(record)
    return jsonify(_attach_restore_archive_maintenance(payload, maintenance)), status


def _ack_restore_archive_maintenance(event_id):
    if not re.fullmatch(r"[a-f0-9]{32}", event_id or ""):
        return {"ok": False, "msg": "恢复维护事件标识无效"}, 400
    try:
        with storage_transaction(live_data=False), BACKUP_RESTORE_REQUEST_LOCK, _restore_archive_process_lock():
            state = _read_restore_archive_maintenance_state()
            if state is None:
                return {"ok": True, "cleared": True, "maintenance": None}, 200
            if state["event_id"] != event_id:
                payload = _attach_restore_archive_maintenance(
                    {"ok": False, "msg": "恢复维护状态已变化，请重新核对历史"}, state,
                )
                return payload, 409
            if state.get("acknowledged"):
                _sync_restore_archive_directory()
                return {"ok": True, "cleared": True, "maintenance": None}, 200

            _read_restore_archive_index(require_redundancy=True)
            state = _read_restore_archive_maintenance_state()
            if state is None:
                return {"ok": True, "cleared": True, "maintenance": None}, 200
            if state["event_id"] != event_id:
                payload = _attach_restore_archive_maintenance(
                    {"ok": False, "msg": "恢复维护状态已变化，请重新核对历史"}, state,
                )
                return payload, 409
            if state["required"]:
                payload = {"ok": False, "msg": "恢复归档仍待维护，不能清除提示"}
                return _attach_restore_archive_maintenance(payload, state), 409

            acknowledged = dict(state, acknowledged=True)
            _write_restore_archive_maintenance_state(acknowledged)
            return {"ok": True, "cleared": True, "maintenance": None}, 200
    except Exception:
        payload = _attach_current_restore_archive_maintenance(
            {"ok": False, "msg": "恢复维护确认尚未持久核实；原恢复请求与目录未更改，可重试确认"},
        )
        if payload.get("maintenance") is None and not payload.get("maintenance_error"):
            return {"ok": True, "cleared": True, "maintenance": None}, 200
        return payload, 503


@bp.route("/api/backup/restore-maintenance/<event_id>/ack", methods=["POST"])
def api_backup_restore_maintenance_ack(event_id):
    payload, status = _ack_restore_archive_maintenance(event_id)
    return jsonify(payload), status


@bp.route("/backup/restore-maintenance/ack", methods=["POST"])
def backup_restore_maintenance_ack():
    event_id = request.form.get("event_id", "")
    payload, status = _ack_restore_archive_maintenance(event_id)
    if status != 200:
        return restore_error(payload.get("msg", "恢复维护状态未能清除"), status,
                             request_id=request.form.get("request_id", ""),
                             maintenance=payload.get("maintenance"))
    request_id = request.form.get("request_id", "")
    target = "/backup"
    if re.fullmatch(r"[a-f0-9]{32}", request_id):
        target += "?restore_request_id=" + request_id
    return redirect(target)


def _reload_restore_scheduler(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return {"ok": False, "request_id": request_id, "msg": "恢复请求标识无效"}, 400
    reserved = False
    try:
        if not try_begin_restore_activity():
            return {"ok": False, "request_id": request_id, "msg": "备份、恢复或目录现场待处理，请完成后重试"}, 409
        reserved = True
        with models.TASKS_MUTATION_LOCK, storage_transaction(), BACKUP_RESTORE_REQUEST_LOCK:
            record = _read_restore_request(request_id)
            if not record:
                return {"ok": False, "request_id": request_id, "msg": "恢复请求不存在"}, 404
            if record["state"] not in ("restored", "unknown"):
                return {"ok": False, "request_id": request_id, "msg": "恢复请求尚未结束，请稍后查询"}, 409
            reload_required = record.get("scheduler_reload_required", False) or (
                record["state"] == "unknown" and not record.get("scheduler_reloaded", False)
            )
            if reload_required:
                try:
                    reload_scheduler()
                except Exception as exc:
                    scheduler_error = str(exc)
                    record.update(
                        scheduler_reload_required=True,
                        scheduler_reloaded=False,
                        scheduler_error=scheduler_error,
                    )
                    message = f"调度器重载未确认：{scheduler_error}。请查询原恢复请求后重试。"
                    try:
                        _save_restore_request(record)
                    except Exception as persist_exc:
                        message += f"恢复诊断保存失败：{persist_exc}。"
                    return {
                        "ok": False,
                        "request_id": request_id,
                        "state": record["state"],
                        "restored": record["state"] == "restored",
                        "scheduler_reload_required": True,
                        "scheduler_error": _bounded_restore_diagnostic(scheduler_error),
                        "reload_url": f"/api/backup/restore/{request_id}/reload",
                        "msg": message,
                    }, 503
                record.update(scheduler_reload_required=False, scheduler_reloaded=True, scheduler_error="")
                _save_restore_request(record)
            return {
                "ok": True,
                "request_id": request_id,
                "state": record["state"],
                "restored": record["state"] == "restored",
                "scheduler_reload_required": False,
                "msg": "已重新加载当前任务，目录保持不变",
            }, 200
    except Exception as exc:
        return {
            "ok": False,
            "request_id": request_id,
            "scheduler_reload_required": True,
            "reload_url": f"/api/backup/restore/{request_id}/reload",
            "msg": f"调度器重载未确认：{exc}。请查询原恢复请求后重试。",
        }, 503
    finally:
        if reserved:
            end_restore_activity()


@bp.route("/backup/restore/<request_id>/reload", methods=["POST"])
def backup_restore_reload(request_id):
    payload, status = _reload_restore_scheduler(request_id)
    if payload.get("ok"):
        return redirect(url_for("backup.backup_page", _anchor="backup-restore"))
    return restore_error(
        payload.get("msg") or "调度器重载未确认",
        status,
        restored=bool(payload.get("restored")),
        request_id=request_id,
        scheduler_reload_required=bool(payload.get("scheduler_reload_required")),
        scheduler_error=payload.get("scheduler_error", ""),
        state=payload.get("state", ""),
    )


@bp.route("/api/backup/restore/<request_id>/reload", methods=["POST"])
def api_backup_restore_reload(request_id):
    payload, status = _reload_restore_scheduler(request_id)
    return jsonify(payload), status


def restore_error(message, status, *, restored=False, restored_items=None, **details):
    payload = {"ok": False, "msg": message, "restored": restored,
               "restored_items": restored_items or [], "scheduler_reload_required": restored, **details}
    maintenance = details.get("maintenance")
    if isinstance(maintenance, dict) and "notice" not in maintenance:
        maintenance = _restore_archive_maintenance_payload(maintenance)
        payload["maintenance"] = maintenance
    if maintenance is None and request.path in ("/api/backup/import", "/backup/import"):
        try:
            maintenance = _restore_archive_maintenance_payload()
        except Exception:
            maintenance = _restore_archive_maintenance_payload(unreadable=True)
        payload["maintenance"] = maintenance
    if isinstance(maintenance, dict):
        payload["maintenance_error"] = maintenance.get("notice", "")
        payload["maintenance_required"] = bool(maintenance.get("required"))
    if payload["scheduler_reload_required"]:
        if isinstance(details.get("request_id"), str) and re.fullmatch(r"[a-f0-9]{32}", details["request_id"]):
            payload["reload_url"] = f"/api/backup/restore/{details['request_id']}/reload"
        else:
            payload["reload_url"] = "/api/scheduler/reload"
    if request.path.startswith("/api/"):
        return jsonify(payload), status
    body = f'<section class="section" role="alert"><p>{h(message)}</p></section>'
    request_id = details.get("request_id")
    if isinstance(maintenance, dict):
        body += f'<section class="section" role="status"><p>{h(maintenance.get("notice", ""))}</p>'
        if not maintenance.get("required") and re.fullmatch(r"[a-f0-9]{32}", str(maintenance.get("event_id", ""))):
            event_id = h(maintenance["event_id"])
            preserved_id = h(request_id) if isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id) else ""
            body += (
                '<form method="post" action="/backup/restore-maintenance/ack">'
                f'<input type="hidden" name="csrf_token" value="{h(csrf_token())}">'
                f'<input type="hidden" name="event_id" value="{event_id}">'
                f'<input type="hidden" name="request_id" value="{preserved_id}">'
                '<button class="btn btn-gray" type="submit">确认已读并清除维护提示</button></form>'
            )
        body += "</section>"
    if isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id):
        body += f'<a class="btn btn-blue" href="/backup?restore_request_id={h(request_id)}">核对原恢复请求</a>'
    if payload["scheduler_reload_required"]:
        request_id = details.get("request_id")
        reload_action = (
            f"/backup/restore/{request_id}/reload"
            if isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id)
            else "/scheduler/reload?back=/backup"
        )
        csrf_field = f'<input type="hidden" name="csrf_token" value="{h(csrf_token())}">'
        body += f'<form method="post" action="{h(reload_action)}">{csrf_field}<section class="section"><button class="btn btn-primary" type="submit">仅重新加载调度器</button></section></form><a class="btn btn-blue" href="/tasks">查看已恢复任务</a>'
    if payload.get("dependencies_failed"):
        body += f'<section class="section"><p>{h(payload.get("dependencies", ""))}</p><pre>{h(payload.get("dependency_log", ""))}</pre></section>'
        body += '<a class="btn btn-blue" href="/deps">查看并修复依赖</a>'
    body += '<a class="btn btn-gray" href="/backup">返回备份恢复</a>'
    return layout("备份恢复结果", "backup", body), status


def _remove_restore_path(path):
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _stage_restore_directory(source, target, keep_backups=False):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(
        prefix=".{}-restore-".format(target.name),
        dir=str(target.parent),
    ))
    staged = workspace / "replacement"

    try:
        shutil.copytree(source, staged)
        if keep_backups:
            current_backup_dir = DATA_DIR / "backups"
            if current_backup_dir.exists():
                shutil.copytree(
                    current_backup_dir,
                    staged / "backups",
                    dirs_exist_ok=True,
                )
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise

    return {
        "target": target,
        "workspace": workspace,
        "staged": staged,
        "previous": None,
        "installed": False,
        "preserve_workspace": False,
    }


_RUNTIME_IDENTITY_NAMES = frozenset({
        "secret_key.txt", "backup_jobs.json", "task_history.json",
        "task_history_index.sqlite3", "dependency_install_jobs.json",
        "notify_history.json", "proxy_diagnostics.json", "script_debug_runs.json",
        "script_pull_jobs.json", "script_repo_pull_jobs.json", "script_import_jobs.json",
        "script_renames.json", "online_task_link_requests.json",
        "online_task_import_requests.json", "online_install_jobs.json",
        "online_script_refresh_requests.json", "config_apply_state.json",
        "native_sessions.sqlite3", "native_notify_deliveries", "proxy_checks",
        "proxy_checks_index.json", "online_script_refresh.json",
        "online_script_refresh_failures", "fls_code.json", "scheduler_state.json",
        "fls-manager.pid", "fls-manager.lock",
})


def _runtime_identity_names(source_data, destination_data):
    names = set(_RUNTIME_IDENTITY_NAMES)
    for directory in (source_data, destination_data):
        if directory.is_dir() and not directory.is_symlink():
            for path in directory.iterdir():
                if (path.name.endswith(".sqlite3") or path.name.endswith(".sqlite3-wal")
                    or path.name.endswith(".sqlite3-shm") or path.name.endswith(".sqlite3-journal")):
                    names.add(path.name)
                if path.name.endswith(("_requests", "_receipts", "_jobs.json", "_history.json")):
                    names.add(path.name)
    return sorted(names)


def _move_runtime_identity_files(source_data, destination_data, moved):
    source_data = Path(source_data)
    destination_data = Path(destination_data)
    names = _runtime_identity_names(source_data, destination_data)
    live_names = []
    for name in names:
        source = source_data / name
        try:
            info = source.lstat()
        except FileNotFoundError:
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError("运行时身份文件类型无效")
        live_names.append(name)

    # Strip archived identities even when this instance has no matching state.
    for name in names:
        _remove_restore_path(destination_data / name)
    for name in live_names:
        os.replace(source_data / name, destination_data / name)
        moved.append(name)


def _rollback_runtime_identity_files(source_data, destination_data, moved):
    # Only move identities transferred by this commit; never scan the archive back.
    while moved:
        name = moved[-1]
        source = source_data / name
        destination = destination_data / name
        if destination.exists() or destination.is_symlink():
            raise RuntimeError("回滚时原运行态路径被重新创建，保留恢复副本")
        os.replace(source, destination)
        moved.pop()


def _commit_restore_directories(entries, *, preserve_runtime_identity=False, request_id=None):
    with directory_access(exclusive=True):
        return _commit_restore_directories_locked(
            entries, preserve_runtime_identity=preserve_runtime_identity, request_id=request_id,
        )


def _commit_restore_directories_locked(entries, *, preserve_runtime_identity=False, request_id=None):
    if request_id is not None:
        from ... import backup_commit
        if not preserve_runtime_identity:
            raise RuntimeError("journaled restore requires runtime identity protection")
        names = set()
        for entry in entries:
            if entry["target"] == DATA_DIR:
                names.update(_runtime_identity_names(DATA_DIR, entry["staged"]))
        try:
            backup_commit.commit(BASE_DIR, request_id, entries, names)
        except backup_commit.DirectoryCommitError as exc:
            raise BackupRestoreWriteError(str(exc), exc.live_state_unchanged) from exc
        return
    try:
        for entry in entries:
            target = entry["target"]
            if target.exists() or target.is_symlink():
                previous = entry["workspace"] / "previous"
                os.replace(target, previous)
                entry["previous"] = previous

            if preserve_runtime_identity and target == DATA_DIR:
                entry["runtime_identity_moves"] = []
                previous = entry.get("previous")
                _move_runtime_identity_files(
                    previous if previous is not None else target,
                    entry["staged"],
                    entry["runtime_identity_moves"],
                )

            os.replace(entry["staged"], target)
            entry["installed"] = True
    except Exception as commit_error:
        rollback_errors = []
        for entry in reversed(entries):
            target = entry["target"]
            previous = entry.get("previous")
            try:
                if previous is not None and (previous.exists() or previous.is_symlink()):
                    if preserve_runtime_identity and target == DATA_DIR:
                        source = target if entry.get("installed") else entry["staged"]
                        _rollback_runtime_identity_files(
                            source, previous, entry.get("runtime_identity_moves", []),
                        )
                    if target.exists() or target.is_symlink():
                        _remove_restore_path(target)
                    os.replace(previous, target)
                elif entry.get("installed"):
                    _remove_restore_path(target)
            except Exception as rollback_error:
                entry["preserve_workspace"] = True
                rollback_errors.append(
                    "{}: {} (恢复副本保留在 {})".format(
                        target,
                        rollback_error,
                        entry["workspace"],
                    )
                )

        detail = str(commit_error)
        if rollback_errors:
            detail += "；回滚失败：" + "；".join(rollback_errors)
        raise BackupRestoreWriteError(
            detail,
            live_state_unchanged=not rollback_errors,
        ) from commit_error


@bp.route("/backup/download/<path:filename>")
def backup_download(filename):
    try:
        validate_backup_filename(filename)
        with backup_directory_fd() as directory:
            source = open_backup_entry(directory, filename)
        try:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                source.close()
                abort(404)
            response = send_file(source, mimetype="application/gzip", as_attachment=True, download_name=filename)
            response.content_length = info.st_size
            response.call_on_close(source.close)
            return response
        except BaseException:
            source.close()
            raise
    except (ValueError, OSError):
        abort(404)

@bp.route("/backup/export")
def backup_export():
    return redirect(url_for("backup.backup_page", _anchor="backup-create"))

@bp.route("/api/backup/import", methods=["POST"])
@bp.route("/backup/import", methods=["POST"])
def backup_import():
    if (
        request.content_length is not None
        and request.content_length > MAX_BACKUP_UPLOAD_BYTES + MIB
    ):
        return restore_error("备份导入失败：请求超过上传上限。请调低文件大小或调整服务端备份限制。", 413)

    f = request.files.get("file")

    if not f:
        return restore_error("未上传备份文件。下一步：选择 .tar.gz、.tgz、.tar 或 .zip 文件后重试", 400)

    selected = request.form.getlist("restore_items")
    deps = request.form.getlist("restore_deps")
    if (any(item not in ("data", "scripts") for item in selected) or len(selected) != len(set(selected))
        or len(deps) > 1 or deps and deps[0] not in ("0", "1")):
        return restore_error("恢复内容或依赖选择无效，未写入任何目录。请重新选择后提交。", 400)
    restore_items = parse_items_from_form("restore_items")

    if not restore_items:
        return restore_error("请至少选择一个恢复内容。下一步：勾选配置或脚本后重试", 400)

    restore_deps = request.form.get("restore_deps") == "1"
    request_id = request.form.get("restore_request_id", "").strip()
    if request_id and not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return restore_error("恢复请求标识无效，未写入任何目录。", 400)
    tmp_dir = tempfile.mkdtemp()
    restore_reserved = False
    directories_restored = False
    scheduler_reloaded = False
    restore_record = None
    request_owned = False
    commit_attempted = False
    staged = []
    maintenance = None
    directory_guard = ExitStack()

    try:
        original = os.path.basename(f.filename or "backup")
        backup_file = Path(tmp_dir) / original
        save_backup_upload(f, backup_file)
        digest = hashlib.sha256()
        with backup_file.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)

        if request_id:
            with storage_transaction(live_data=False), BACKUP_RESTORE_REQUEST_LOCK, _restore_archive_process_lock():
                try:
                    archived = _read_restore_archive_index()["records"].get(request_id)
                    maintenance = _read_restore_archive_maintenance_state()
                except Exception:
                    maintenance = _restore_archive_maintenance_payload(unreadable=True)
                    raise
                restore_record = _read_restore_request(request_id)
                if archived:
                    if restore_record and not _restore_request_identity_matches(restore_record, archived):
                        return restore_error("归档请求标识已绑定其他文件或恢复范围，未覆盖任何目录。", 409,
                                             request_id=request_id, request_conflict=True)
                    payload, status = _restore_request_response(archived)
                    payload = _attach_restore_archive_maintenance(payload, maintenance)
                    if request.path.startswith("/api/"):
                        return jsonify(payload), status
                    return restore_error(payload["msg"], status, restored=payload["restored"],
                                         restored_items=payload["restored_items"], request_id=request_id,
                                         state=payload["state"], archived=True,
                                         maintenance=payload["maintenance"])
                if restore_record:
                    if (restore_record["archive_sha256"] != digest.hexdigest()
                        or restore_record["restore_items"] != restore_items
                        or restore_record.get("restore_deps", False) != restore_deps):
                        return restore_error("该恢复请求标识已绑定其他文件或恢复范围，未覆盖任何目录。请明确开始新的恢复请求。", 409,
                                             request_id=request_id, request_conflict=True)
                    if restore_record["state"] in ("restored", "unknown", "committing"):
                        if restore_record["state"] == "committing" and not restore_activity_active():
                            restore_record["state"] = "unknown"
                            restore_record["restore_outcome_unknown"] = True
                            try:
                                _save_restore_request(restore_record)
                            except Exception:
                                pass
                        payload, status = _restore_request_response(restore_record)
                        payload = _attach_restore_archive_maintenance(payload, maintenance)
                        if request.path.startswith("/api/"):
                            return jsonify(payload), status
                        return restore_error(payload["msg"], status, restored=payload["restored"],
                                             restored_items=payload["restored_items"],
                                             scheduler_reload_required=payload["scheduler_reload_required"],
                                             dependencies_failed=payload["dependencies_failed"],
                                             dependencies=payload["dependencies"],
                                             dependency_log=payload["dependency_log"],
                                             restore_outcome_unknown=payload["restore_outcome_unknown"],
                                             request_id=request_id,
                                             maintenance=payload["maintenance"])
                else:
                    restore_record = {
                        "request_id": request_id,
                        "archive_sha256": digest.hexdigest(),
                        "filename": original,
                        "restore_items": restore_items,
                        "restore_deps": restore_deps,
                        "state": "received",
                        "scheduler_reload_required": False,
                        "dependencies_failed": False,
                        "created_at": now_str(),
                    }
                if restore_record.get("state") == "failed":
                    restore_record.update(state="received", error="")
                _save_restore_request(restore_record)
                request_owned = True

        extract_dir = Path(tmp_dir) / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        extract_archive(backup_file, extract_dir)

        root = find_backup_root(extract_dir)

        if not root:
            raise RuntimeError("未在备份中找到 data 或 scripts 目录")

        dep_file = find_dependency_file(extract_dir, root)

        data_src = root / "data"
        scripts_src = root / "scripts"

        restore_sources = []

        if "data" in restore_items:
            if not data_src.exists():
                raise RuntimeError("备份中没有 data 目录，无法恢复配置")
            restore_sources.append((data_src, DATA_DIR, True, "配置 data"))

        if "scripts" in restore_items:
            if not scripts_src.exists():
                raise RuntimeError("备份中没有 scripts 目录，无法恢复脚本")
            restore_sources.append((scripts_src, SCRIPT_DIR, False, "脚本 scripts"))

        if not try_begin_restore_activity():
            return restore_error("备份正在创建或恢复，请等待当前操作完成后重试。", 409)
        restore_reserved = True
        directory_guard.enter_context(directory_access(exclusive=True))

        staged = []
        try:
            for source, target, keep_backups, _label in restore_sources:
                staged.append(
                    _stage_restore_directory(source, target, keep_backups)
                )
        except Exception as e:
            for entry in staged:
                shutil.rmtree(entry["workspace"], ignore_errors=True)
            raise BackupRestoreWriteError(str(e)) from e

        scheduler_error = ""
        restore_changes_data = "data" in restore_items
        collection_lock = (
            models.COLLECTION_MUTATION_LOCK
            if restore_changes_data
            else nullcontext()
        )
        task_lock = (
            models.TASKS_MUTATION_LOCK
            if restore_changes_data
            else nullcontext()
        )
        storage_guard = (
            storage_transaction()
            if restore_changes_data
            else nullcontext()
        )
        try:
            with collection_lock:
                with task_lock:
                    with storage_guard:
                        if restore_record:
                            with BACKUP_RESTORE_REQUEST_LOCK:
                                current = _read_restore_request(request_id)
                                if current is None or current["state"] not in ("received", "failed"):
                                    return restore_error("恢复请求状态已变化，目录未提交。请查询原请求后再继续。", 409, request_id=request_id)
                                restore_record.update(state="committing", started_at=now_str(), error="")
                                _save_restore_request(restore_record)
                        commit_attempted = True
                        _commit_restore_directories(staged)
                        directories_restored = True
                        if restore_record:
                            restore_record.update(state="restored", restored_at=now_str(),
                                                  scheduler_reload_required=True,
                                                  dependencies_failed=restore_deps,
                                                  scheduler_reloaded=False)
                            _save_restore_request(restore_record)
                        try:
                            reload_scheduler()
                            scheduler_reloaded = True
                            if restore_record:
                                restore_record.update(scheduler_reload_required=False, scheduler_reloaded=True)
                                _save_restore_request(restore_record)
                        except Exception as e:
                            scheduler_error = str(e)
                            if restore_record:
                                restore_record.update(scheduler_reload_required=True, scheduler_error=str(e))
                                _save_restore_request(restore_record)
        finally:
            for entry in staged:
                if not entry["preserve_workspace"]:
                    shutil.rmtree(entry["workspace"], ignore_errors=True)

        directory_guard.close()
        restored = [label for _source, _target, _keep, label in restore_sources]

        deps_msg = ""
        deps_log = ""
        dependencies_failed = False

        if restore_deps:
            if dep_file:
                ok, deps_log = install_dependencies(dep_file)
                dependencies_failed = not ok
                deps_msg = "依赖恢复成功" if ok else "依赖恢复失败，请查看日志"
            else:
                deps_msg = "已勾选恢复依赖，但备份中没有 dependencies.txt / requirements.txt"
                dependencies_failed = True
            if restore_record:
                restore_record.update(dependencies_failed=dependencies_failed, dependencies=deps_msg,
                                      dependency_log=deps_log)
                _save_restore_request(restore_record)

        if scheduler_error:
            return restore_error(
                f"备份已恢复，但调度器重载失败：{scheduler_error}。请查看已恢复任务并仅重新加载调度器。",
                503, restored=True, restored_items=restore_items,
                request_id=request_id,
                state=restore_record.get("state", "restored") if restore_record else "restored",
                scheduler_error=scheduler_error,
                dependencies=deps_msg or "未恢复依赖", dependency_log=deps_log, dependencies_failed=dependencies_failed,
            )

        if dependencies_failed:
            return restore_error("目录已恢复，依赖恢复未完成，请检查依赖日志并修复依赖，不要重复导入目录。", 503,
                                 restored=True, restored_items=restore_items, scheduler_reload_required=False,
                                 dependencies_failed=True, dependencies=deps_msg, dependency_log=deps_log)

        if restore_record and _restore_request_archivable(restore_record):
            try:
                _compact_restore_receipts()
            except Exception as exc:
                print(f"[备份恢复] 回执归档未完成，保留明细供后续重试: {exc}")

        if request.path.startswith("/api/"):
            return jsonify(ok=True, msg="备份导入完成", restored=True, restored_items=restore_items,
                           scheduler_reload_required=False, dependencies=deps_msg or "未恢复依赖", dependency_log=deps_log,
                           request_id=request_id, state="restored")

        body = page_header_card(
            "备份导入完成",
            help_html=f"""
        已恢复：{h("、".join(restored) or "-")}<br>
        依赖恢复：{h(deps_msg or "未恢复依赖")}<br>
        日志：{h(deps_log or "-")}
""",
            actions_html="""
<a class="btn btn-primary" href="/backup">返回备份恢复</a>
<a class="btn btn-gray" href="/logs">查看日志</a>
""",
        )
        return layout("备份导入完成", "backup", body)

    except BackupLimitExceeded as e:
        details = {"request_id": request_id} if request_id else {}
        if request_owned and restore_record:
            details.update(restored=False, live_state_unchanged=True)
            try:
                with storage_transaction(), BACKUP_RESTORE_REQUEST_LOCK:
                    current = _read_restore_request(request_id)
                    if current and current.get("state") in ("received", "failed"):
                        current.update(state="failed", error=str(e))
                        _save_restore_request(current)
                        details["state"] = "failed"
                    elif current:
                        details["state"] = current.get("state", "unknown")
            except Exception:
                # The upload is rejected before live directories are staged or changed.
                pass
        return restore_error(
            f"备份导入失败：{e}。请调低文件大小或调整服务端备份限制。",
            413,
            maintenance=maintenance,
            **details,
        )
    except BackupRestoreWriteError as e:
        state = (
            "现有目录已恢复到原状态"
            if e.live_state_unchanged
            else "现有目录状态可能不完整"
        )
        outcome_unknown = not e.live_state_unchanged
        if request_owned:
            restore_record.update(state="failed" if e.live_state_unchanged else "unknown", error=str(e))
            try:
                _save_restore_request(restore_record)
            except Exception:
                pass
        return restore_error(
            f"备份恢复失败，{state}：{e}。"
            + ("请检查任务和脚本状态，不要重复导入。" if outcome_unknown else "请检查磁盘空间和目录权限后再操作。"),
            503 if outcome_unknown else 500,
            live_state_unchanged=e.live_state_unchanged,
            restore_outcome_unknown=outcome_unknown,
            request_id=request_id,
            state="unknown" if outcome_unknown else "failed",
            maintenance=maintenance,
        )
    except Exception as e:
        if request_owned and not directories_restored:
            restore_record.update(state="unknown" if commit_attempted else "failed", error=str(e))
            try:
                with storage_transaction(), BACKUP_RESTORE_REQUEST_LOCK:
                    _save_restore_request(restore_record)
            except Exception:
                pass
        if directories_restored:
            if restore_record:
                restore_record.update(state="restored", scheduler_reload_required=not scheduler_reloaded,
                                      scheduler_reloaded=scheduler_reloaded, dependencies_failed=bool(restore_deps), error=str(e))
                try:
                    _save_restore_request(restore_record)
                except Exception:
                    pass
            return restore_error(f"所选目录已恢复，但后续处理失败：{e}。请检查已恢复数据，不要重复导入。", 503,
                                 restored=True, restored_items=restore_items,
                                 scheduler_reload_required=not scheduler_reloaded,
                                 dependencies_failed=bool(restore_deps), dependency_log=str(e), request_id=request_id,
                                 maintenance=maintenance)
        if commit_attempted:
            return restore_error(f"恢复提交结果未知：{e}。请检查目录和调度器状态，不要重复导入。", 503,
                                 restore_outcome_unknown=True, scheduler_reload_required=True, request_id=request_id,
                                 maintenance=maintenance)
        return restore_error(f"备份导入失败：{e}。下一步：检查备份内容和恢复范围后重试", 500 if isinstance(e, OSError) or request_id and not request_owned else 400,
                             maintenance=maintenance)

    finally:
        for entry in staged:
            if not entry["preserve_workspace"]:
                shutil.rmtree(entry["workspace"], ignore_errors=True)
        directory_guard.close()
        if restore_reserved:
            end_restore_activity()
        shutil.rmtree(tmp_dir, ignore_errors=True)
