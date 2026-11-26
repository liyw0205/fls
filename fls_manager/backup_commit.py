"""Durable directory replacement, loaded before paths creates runtime state."""

import json
import os
import re
import shutil
import stat
from pathlib import Path

from .backup_lease import release_process_lease, try_process_lease
from .data_access import directory_access

JOURNAL_NAME = ".backup_native_commits"
JOURNAL_LIMIT = 128 * 1024
REQUEST_ID = re.compile(r"[a-f0-9]{32}")


class DirectoryCommitError(RuntimeError):
    def __init__(self, message, live_state_unchanged=False):
        super().__init__(message)
        self.live_state_unchanged = live_state_unchanged


def _identity(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    kind = stat.S_IFMT(info.st_mode)
    if kind not in (stat.S_IFREG, stat.S_IFDIR) or not info.st_ino:
        raise RuntimeError("unverifiable restore object")
    return {"device": info.st_dev, "inode": info.st_ino, "kind": kind}


def _expect(path, identity):
    if _identity(path) != identity:
        raise RuntimeError("restore object identity changed")


def _sync_directory(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_tree(root):
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(current) / name
            identity = _identity(path)
            if identity["kind"] == stat.S_IFREG and os.name != "nt":
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags)
                try:
                    if os.fstat(fd).st_ino != identity["inode"]:
                        raise RuntimeError("restore stage changed")
                    os.fsync(fd)
                finally:
                    os.close(fd)
        _sync_directory(Path(current))


def _rename(source, destination, identity):
    _expect(source, identity)
    _expect(destination, None)
    os.replace(source, destination)
    _sync_directory(source.parent)
    if destination.parent != source.parent:
        _sync_directory(destination.parent)


def _journal_path(base, request_id):
    if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
        raise RuntimeError("invalid restore transaction id")
    directory = base / JOURNAL_NAME
    if directory.is_symlink() or directory.exists() and not directory.is_dir():
        raise RuntimeError("invalid restore transaction directory")
    return directory / (request_id + ".json")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate transaction field")
        result[key] = value
    return result


def _valid_identity(value, *, directory=False):
    return (isinstance(value, dict) and set(value) == {"device", "inode", "kind"}
            and type(value["device"]) is int and value["device"] >= 0
            and type(value["inode"]) is int and value["inode"] > 0
            and value["kind"] in ((stat.S_IFDIR,) if directory else (stat.S_IFDIR, stat.S_IFREG)))


def read(base, request_id):
    base = Path(base)
    path = _journal_path(base, request_id)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > JOURNAL_LIMIT:
        raise RuntimeError("invalid restore transaction file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise RuntimeError("restore transaction changed")
        encoded = stream.read(JOURNAL_LIMIT + 1)
    if len(encoded) > JOURNAL_LIMIT:
        raise RuntimeError("oversized restore transaction")
    row = json.loads(encoded, object_pairs_hook=_unique_object)
    if (not isinstance(row, dict) or row.get("version") != 1 or row.get("request_id") != request_id
        or row.get("state") not in ("prepared", "committed", "rolled_back", "finalizing")
        or row.get("outcome") not in ("pending", "restored", "not_applied")
        or row["state"] == "prepared" and row["outcome"] != "pending"
        or row["state"] == "committed" and row["outcome"] != "restored"
        or row["state"] == "rolled_back" and row["outcome"] != "not_applied"
        or row["state"] == "finalizing" and row["outcome"] == "pending"
        or not _valid_identity(row.get("base"), directory=True)
        or not isinstance(row.get("entries"), list) or not 1 <= len(row["entries"]) <= 2):
        raise RuntimeError("invalid restore transaction")
    seen = set()
    for entry in row["entries"]:
        if (not isinstance(entry, dict) or entry.get("target") not in ("data", "scripts")
            or entry["target"] in seen or not isinstance(entry.get("workspace"), str)
            or not re.fullmatch(r"\." + entry["target"] + r"-restore-[a-z0-9_]{1,64}", entry["workspace"])
            or not _valid_identity(entry.get("workspace_identity"), directory=True)
            or not _valid_identity(entry.get("replacement"), directory=True)
            or entry.get("original") is not None and not _valid_identity(entry["original"], directory=True)
            or not isinstance(entry.get("identities"), dict) or len(entry["identities"]) > 1000):
            raise RuntimeError("invalid restore transaction entry")
        seen.add(entry["target"])
        if entry["target"] != "data" and entry["identities"]:
            raise RuntimeError("invalid restore transaction identities")
        for name, identity in entry["identities"].items():
            if (not isinstance(name, str) or not name or name in (".", "..")
                or len(name) > 255 or any(char in name for char in "/\\:\x00")
                or any(ord(char) < 32 for char in name) or not _valid_identity(identity)):
                raise RuntimeError("invalid restore identity name")
    _expect(base, row["base"])
    return row


def _write(base, row):
    path = _journal_path(base, row["request_id"])
    path.parent.mkdir(mode=0o700, exist_ok=True)
    _sync_directory(base)
    temporary = path.with_suffix(".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise RuntimeError("invalid restore transaction target")
    encoded = json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(encoded) > JOURNAL_LIMIT:
        raise RuntimeError("oversized restore transaction")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_directory(path.parent)


def pending(base):
    base = Path(base)
    directory = _journal_path(base, "0" * 32).parent
    result = []
    if not directory.exists():
        return result
    for path in directory.iterdir():
        if path.suffix == ".tmp":
            continue
        if path.suffix != ".json" or REQUEST_ID.fullmatch(path.stem) is None or len(result) >= 1000:
            raise RuntimeError("invalid restore transaction history")
        result.append(path.stem)
    return result


def recovery_required(base, request_id):
    # Status reads must remain available even when recovery evidence is damaged.
    try:
        path = _journal_path(Path(base), request_id)
        return path.exists() or path.is_symlink()
    except RuntimeError:
        return True


def _locations(base, entry):
    workspace = base / entry["workspace"]
    _expect(workspace, entry["workspace_identity"])
    return base / entry["target"], workspace / "previous", workspace / "replacement"


def _verify_terminal(base, row):
    for entry in row["entries"]:
        target = base / entry["target"]
        expected = entry["replacement"] if row["outcome"] == "restored" else entry["original"]
        _expect(target, expected)
        for name, identity in entry["identities"].items():
            _expect(target / name, identity)


def assert_data_accessible(base, *, write=False):
    base = Path(base)
    for request_id in pending(base):
        row = read(base, request_id)
        if row is None or row["state"] == "prepared" or write:
            raise RuntimeError("directory recovery must finish before accessing live data")
        _verify_terminal(base, row)


def recover(base, request_id):
    with directory_access(base, exclusive=True, recovery=True):
        return _recover(base, request_id)


def _recover(base, request_id):
    base = Path(base)
    row = read(base, request_id)
    if row is None:
        return None
    if row["state"] != "prepared":
        _verify_terminal(base, row)
        return row["outcome"]
    for entry in reversed(row["entries"]):
        target, previous, replacement = _locations(base, entry)
        original_id, replacement_id = entry["original"], entry["replacement"]
        target_id, previous_id, staged_id = _identity(target), _identity(previous), _identity(replacement)
        if target_id not in (None, original_id, replacement_id) or previous_id not in (None, original_id):
            raise RuntimeError("restore rollback found a replaced directory")
        if staged_id not in (None, replacement_id) or (target_id == replacement_id) == (staged_id == replacement_id):
            raise RuntimeError("restore replacement cannot be uniquely located")
        if original_id is not None and (target_id == original_id) == (previous_id == original_id):
            raise RuntimeError("restore original cannot be uniquely located")
        if original_id is None and previous_id is not None:
            raise RuntimeError("unexpected restore original")
        original = target if original_id is not None and target_id == original_id else previous
        new = target if target_id == replacement_id else replacement
        # Validate every identity before moving any file in this entry.
        for name, identity in entry["identities"].items():
            old_id, new_id = _identity(original / name), _identity(new / name)
            if (old_id, new_id) not in ((identity, None), (None, identity)):
                raise RuntimeError("restore runtime identity cannot be uniquely located")
        for name, identity in entry["identities"].items():
            if _identity(original / name) is None:
                _rename(new / name, original / name, identity)
        if target_id == replacement_id:
            _rename(target, replacement, replacement_id)
        if original_id is not None and previous_id == original_id:
            _rename(previous, target, original_id)
    row.update(state="rolled_back", outcome="not_applied")
    _write(base, row)
    _verify_terminal(base, row)
    return "not_applied"


def commit(base, request_id, entries, identity_names):
    with directory_access(base, exclusive=True):
        return _commit(base, request_id, entries, identity_names)


def _commit(base, request_id, entries, identity_names):
    base = Path(base)
    if pending(base):
        raise DirectoryCommitError("a previous directory transaction requires recovery", True)
    row = {"version": 1, "request_id": request_id, "base": _identity(base),
           "state": "prepared", "outcome": "pending", "entries": []}
    for entry in entries:
        target, workspace, staged = entry["target"], entry["workspace"], entry["staged"]
        if target not in (base / "data", base / "scripts") or workspace.parent != base or staged != workspace / "replacement":
            raise DirectoryCommitError("invalid directory transaction paths", True)
        identities = {}
        if target.name == "data":
            for name in identity_names:
                identity = _identity(target / name)
                if identity is not None:
                    identities[name] = identity
                archived = staged / name
                if archived.is_dir() and not archived.is_symlink():
                    shutil.rmtree(archived)
                elif archived.exists() or archived.is_symlink():
                    archived.unlink()
        if len(identities) > 1000:
            raise DirectoryCommitError("too many runtime identity objects", True)
        _sync_tree(staged)
        row["entries"].append({"target": target.name, "workspace": workspace.name,
                               "workspace_identity": _identity(workspace), "replacement": _identity(staged),
                               "original": _identity(target), "identities": identities})
    for entry in entries:
        entry["preserve_workspace"] = True
    try:
        _write(base, row)
        for entry in row["entries"]:
            target, previous, replacement = _locations(base, entry)
            if entry["original"] is not None:
                _rename(target, previous, entry["original"])
            for name, identity in entry["identities"].items():
                _rename(previous / name, replacement / name, identity)
            _rename(replacement, target, entry["replacement"])
        row.update(state="committed", outcome="restored")
        _write(base, row)
    except Exception as exc:
        try:
            outcome = recover(base, request_id)
        except Exception as rollback_error:
            raise DirectoryCommitError("directory rollback requires recovery") from rollback_error
        if outcome is None:
            for entry in entries:
                entry["preserve_workspace"] = False
        raise DirectoryCommitError("directory commit failed", outcome in (None, "not_applied")) from exc


def finish(base, request_id):
    with directory_access(base, exclusive=True, recovery=True):
        return _finish(base, request_id)


def _finish(base, request_id):
    base = Path(base)
    row = read(base, request_id)
    if row is None:
        return
    if row["state"] == "prepared":
        raise RuntimeError("directory transaction has not recovered")
    _verify_terminal(base, row)
    row["state"] = "finalizing"
    _write(base, row)
    for entry in row["entries"]:
        workspace = base / entry["workspace"]
        if _identity(workspace) is None:
            continue
        _expect(workspace, entry["workspace_identity"])
        shutil.rmtree(workspace)
        _sync_directory(base)
    _journal_path(base, request_id).unlink()
    _sync_directory(base / JOURNAL_NAME)


def recover_at_startup(base):
    base = Path(base)
    requests = pending(base)
    if not requests:
        return
    fd = try_process_lease(base / ".backup_native_restore.lock")
    if fd is None:
        raise RuntimeError("directory restore is active; retry worker startup after it finishes")
    try:
        for request_id in requests:
            recover(base, request_id)
    finally:
        release_process_lease(fd)
