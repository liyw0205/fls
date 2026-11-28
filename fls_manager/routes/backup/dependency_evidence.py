"""Durable resource ownership for a dependency operation, outside restored data."""

import json
import os
import re
import stat
import time
import uuid
from pathlib import Path

from ... import backup_commit
from ...receipt_io import open_receipt

DIRECTORY_NAME = ".backup_native_dependency_evidence"
RECORD_LIMIT = 8192
HISTORY_LIMIT = 1000
STATES = {"prepared", "guarded", "start_requested", "cleanup_pending", "cleaned"}


def _directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
        backup_commit._sync_directory(path.parent)
    identity = backup_commit._identity(path)
    if identity is not None and identity["kind"] != stat.S_IFDIR:
        raise RuntimeError("invalid dependency evidence directory")
    if create:
        os.chmod(path, 0o700)
    return identity


def _path(base, request_id, operation_id, *, create=False):
    if any(not isinstance(value, str) or backup_commit.REQUEST_ID.fullmatch(value) is None
           for value in (request_id, operation_id)):
        raise RuntimeError("invalid dependency evidence identity")
    root = Path(base) / DIRECTORY_NAME
    _directory(root, create=create)
    return root / (request_id + "-" + operation_id + ".json")


def ids(base):
    root = Path(base) / DIRECTORY_NAME
    if _directory(root) is None:
        return []
    result = []
    for path in root.glob("*.json"):
        parts = path.stem.split("-")
        if (len(parts) != 2 or any(backup_commit.REQUEST_ID.fullmatch(value) is None for value in parts)
            or len(result) >= HISTORY_LIMIT):
            raise RuntimeError("invalid or excessive dependency evidence history")
        result.append(tuple(parts))
    return result


def _valid_guard(guard):
    if not isinstance(guard, dict) or type(guard.get("pid")) is not int or guard["pid"] <= 1:
        return False
    if guard.get("kind") == "posix_group":
        return set(guard) == {"kind", "pid"}
    return (set(guard) == {"kind", "pid", "name"} and guard.get("kind") == "windows_job"
            and isinstance(guard.get("name"), str)
            and re.fullmatch(r"Local\\FLSBackupDependencies_[a-f0-9]{32}", guard["name"]) is not None)


def _validate(record, request_id, operation_id):
    if (not isinstance(record, dict) or set(record) != {
            "version", "request_id", "operation_id", "base", "storage", "work_root", "workspace",
            "guard", "state", "revision", "created_at", "updated_at",
        } or type(record["version"]) is not int or record["version"] != 1
        or record["request_id"] != request_id or record["operation_id"] != operation_id
        or not all(backup_commit._valid_identity(record[key], directory=True)
                   for key in ("base", "storage", "work_root"))
        or not isinstance(record["state"], str) or record["state"] not in STATES
        or type(record["revision"]) is not int or record["revision"] < 1
        or type(record["created_at"]) is not int or record["created_at"] < 0
        or type(record["updated_at"]) is not int or record["updated_at"] < record["created_at"]
        or record["guard"] is not None and not _valid_guard(record["guard"])
        or record["state"] == "prepared" and record["guard"] is not None
        or record["state"] in {"guarded", "start_requested"} and record["guard"] is None):
        raise RuntimeError("invalid dependency evidence record")
    workspace = record["workspace"]
    if (not isinstance(workspace, dict) or set(workspace) != {"name", "device", "inode"}
        or not isinstance(workspace["name"], str)
        or re.fullmatch(r"deps-" + request_id[:8] + r"-[a-z0-9_]{1,64}", workspace["name"]) is None
        or type(workspace["device"]) is not int or workspace["device"] < 0
        or type(workspace["inode"]) is not int or workspace["inode"] < 1):
        raise RuntimeError("invalid dependency evidence workspace")


def read(base, request_id, operation_id):
    path = _path(base, request_id, operation_id)
    identity = backup_commit._identity(path)
    if identity is None:
        return None
    if identity["kind"] != stat.S_IFREG:
        raise RuntimeError("invalid dependency evidence file")
    with os.fdopen(open_receipt(path), "rb") as stream:
        before = os.fstat(stream.fileno())
        if (before.st_dev != identity["device"] or before.st_ino != identity["inode"]
            or not stat.S_ISREG(before.st_mode) or before.st_size > RECORD_LIMIT):
            raise RuntimeError("dependency evidence changed or exceeds limit")
        encoded = stream.read(RECORD_LIMIT + 1)
        after = os.fstat(stream.fileno())
        if (len(encoded) > RECORD_LIMIT or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns):
            raise RuntimeError("dependency evidence changed while reading")
    backup_commit._expect(path, identity)
    record = json.loads(encoded, object_pairs_hook=backup_commit._unique_object)
    _validate(record, request_id, operation_id)
    backup_commit._expect(Path(base), record["base"])
    backup_commit._expect(path.parent, record["storage"])
    backup_commit._expect(Path(base) / ".backup_native_work", record["work_root"])
    return record


def _publish(base, record):
    _validate(record, record["request_id"], record["operation_id"])
    path = _path(base, record["request_id"], record["operation_id"])
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > RECORD_LIMIT:
        raise RuntimeError("dependency evidence exceeds limit")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        backup_commit._expect(Path(base), record["base"])
        backup_commit._expect(path.parent, record["storage"])
        backup_commit._expect(Path(base) / ".backup_native_work", record["work_root"])
        backup_commit._replace(temporary, path)
        backup_commit._sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def create(base, request_id, operation_id, workspace):
    path = _path(base, request_id, operation_id, create=True)
    if read(base, request_id, operation_id) is not None:
        raise RuntimeError("dependency evidence operation already exists")
    if len(ids(base)) >= HISTORY_LIMIT:
        raise RuntimeError("dependency evidence history is full")
    now = int(time.time())
    record = {
        "version": 1, "request_id": request_id, "operation_id": operation_id,
        "base": _directory(Path(base)), "storage": _directory(path.parent),
        "work_root": _directory(Path(base) / ".backup_native_work"), "workspace": dict(workspace),
        "guard": None, "state": "prepared", "revision": 1, "created_at": now, "updated_at": now,
    }
    _validate(record, request_id, operation_id)
    backup_commit._expect(Path(base) / ".backup_native_work" / workspace["name"], {
        "device": workspace["device"], "inode": workspace["inode"], "kind": stat.S_IFDIR,
    })
    backup_commit._sync_directory(Path(base) / ".backup_native_work" / workspace["name"])
    backup_commit._sync_directory(Path(base) / ".backup_native_work")
    _publish(base, record)
    return record


def advance(base, previous, state, *, guard=None):
    current = read(base, previous["request_id"], previous["operation_id"])
    if current != previous:
        raise RuntimeError("dependency evidence operation changed")
    transitions = {
        "prepared": {"guarded", "cleanup_pending", "cleaned"},
        "guarded": {"start_requested", "cleanup_pending", "cleaned"},
        "start_requested": {"cleanup_pending", "cleaned"},
        "cleanup_pending": {"cleanup_pending", "cleaned"},
        "cleaned": {"cleaned"},
    }
    if state not in transitions[previous["state"]]:
        raise RuntimeError("invalid dependency evidence transition")
    if previous["guard"] is not None and guard not in (None, previous["guard"]):
        raise RuntimeError("dependency supervisor identity changed")
    following_guard = previous["guard"] if guard is None else guard
    if state == previous["state"] and following_guard == previous["guard"]:
        _publish(base, previous)
        return previous
    if state in {"guarded", "start_requested"}:
        workspace = previous["workspace"]
        backup_commit._expect(Path(base) / ".backup_native_work" / workspace["name"], {
            "device": workspace["device"], "inode": workspace["inode"], "kind": stat.S_IFDIR,
        })
    record = dict(previous, state=state, guard=following_guard,
                  revision=previous["revision"] + 1, updated_at=max(int(time.time()), previous["updated_at"]))
    _publish(base, record)
    return record
