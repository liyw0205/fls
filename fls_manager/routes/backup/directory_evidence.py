"""Retain orphan restore evidence before releasing its directory write gate."""

import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path

from ... import backup_commit
from ...receipt_io import open_receipt

ARCHIVE_NAME = ".backup_native_directory_evidence"
RECORD_LIMIT = 8192
RECEIPT_LIMIT = 64 * 1024
DIGEST = re.compile(r"[a-f0-9]{64}")


def _directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
        backup_commit._sync_directory(path.parent)
    identity = backup_commit._identity(path)
    if identity is not None and identity["kind"] != stat.S_IFDIR:
        raise RuntimeError("invalid directory evidence storage")
    if create:
        os.chmod(path, 0o700)
    return path


def _paths(base, request_id, *, create=False):
    base = Path(base)
    if not isinstance(request_id, str) or backup_commit.REQUEST_ID.fullmatch(request_id) is None:
        raise RuntimeError("invalid directory evidence id")
    root = _directory(base / ARCHIVE_NAME, create=create)
    return _directory(root / request_id, create=create)


def ids(base):
    root = _directory(Path(base) / ARCHIVE_NAME)
    if not root.exists():
        return []
    result = []
    for path in root.iterdir():
        if backup_commit.REQUEST_ID.fullmatch(path.name) is None or len(result) >= 1000:
            raise RuntimeError("invalid directory evidence history")
        _directory(path)
        result.append(path.name)
    return result


def _bytes(path, limit):
    identity = backup_commit._identity(path)
    if identity is None:
        return None, None
    if identity["kind"] != stat.S_IFREG:
        raise RuntimeError("invalid directory evidence file")
    fd = open_receipt(path)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev != identity["device"] or opened.st_ino != identity["inode"]
            or not stat.S_ISREG(opened.st_mode) or opened.st_size > limit):
            raise RuntimeError("directory evidence changed or exceeds limit")
        encoded = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (len(encoded) > limit or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns):
            raise RuntimeError("directory evidence changed while reading")
    backup_commit._expect(path, identity)
    return encoded, identity


def snapshot(path, limit=RECEIPT_LIMIT):
    encoded, identity = _bytes(path, limit)
    return None if encoded is None else {"identity": identity, "sha256": hashlib.sha256(encoded).hexdigest()}


def _valid_snapshot(value):
    return (isinstance(value, dict) and set(value) == {"identity", "sha256"}
            and backup_commit._valid_identity(value["identity"])
            and value["identity"]["kind"] == stat.S_IFREG
            and isinstance(value["sha256"], str) and DIGEST.fullmatch(value["sha256"]) is not None)


def read(base, request_id):
    path = _paths(base, request_id) / "intent.json"
    encoded, _ = _bytes(path, RECORD_LIMIT)
    if encoded is None:
        return None
    record = json.loads(encoded, object_pairs_hook=backup_commit._unique_object)
    if (not isinstance(record, dict) or set(record) != {
            "version", "request_id", "base", "journal", "receipt", "restore_items",
            "outcome", "confirmation_version", "created_at",
        } or record["version"] != 1 or record["request_id"] != request_id
        or not backup_commit._valid_identity(record["base"], directory=True)
        or not _valid_snapshot(record["journal"])
        or record["receipt"] is not None and not _valid_snapshot(record["receipt"])
        or record["restore_items"] not in (["data"], ["scripts"], ["data", "scripts"])
        or record["outcome"] not in ("restored", "not_applied")
        or not isinstance(record["confirmation_version"], str)
        or DIGEST.fullmatch(record["confirmation_version"]) is None
        or type(record["created_at"]) is not int or record["created_at"] < 0):
        raise RuntimeError("invalid directory evidence intent")
    backup_commit._expect(Path(base), record["base"])
    return record


def _original_location(original, archived, expected, limit):
    live = snapshot(original, limit)
    saved = snapshot(archived, limit)
    if expected is None:
        if live is not None or saved is not None:
            raise RuntimeError("unexpected directory evidence")
        return False
    if (live, saved) == (expected, None):
        return True
    if (live, saved) == (None, expected):
        return False
    raise RuntimeError("directory evidence is missing, duplicated or replaced")


def status(base, request_id, receipt_path):
    record = read(base, request_id)
    if record is None:
        return None
    archive = _paths(base, request_id)
    journal_live = _original_location(
        Path(base) / backup_commit.JOURNAL_NAME / (request_id + ".json"),
        archive / "journal.json", record["journal"], backup_commit.JOURNAL_LIMIT,
    )
    receipt_live = _original_location(receipt_path, archive / "restore.json", record["receipt"], RECEIPT_LIMIT)
    if not journal_live and receipt_live:
        raise RuntimeError("directory write gate released before receipt archival")
    return record, not journal_live and not receipt_live


def prepare(base, request_id, transaction, receipt_path, confirmation_version):
    if read(base, request_id) is not None:
        raise RuntimeError("directory evidence already has an intent")
    if transaction["state"] not in ("committed", "rolled_back"):
        raise RuntimeError("directory transaction must be recovered before finalization")
    backup_commit._verify_terminal(Path(base), transaction)
    for entry in transaction["entries"]:
        backup_commit._expect(Path(base) / entry["workspace"], entry["workspace_identity"])
    archive = _paths(base, request_id, create=True)
    if (archive / "journal.json").exists() or (archive / "restore.json").exists():
        raise RuntimeError("unidentified archived evidence")
    record = {
        "version": 1, "request_id": request_id, "base": transaction["base"],
        "journal": snapshot(Path(base) / backup_commit.JOURNAL_NAME / (request_id + ".json"), backup_commit.JOURNAL_LIMIT),
        "receipt": snapshot(receipt_path), "restore_items": sorted(entry["target"] for entry in transaction["entries"]),
        "outcome": transaction["outcome"], "confirmation_version": confirmation_version, "created_at": int(time.time()),
    }
    if record["journal"] is None:
        raise RuntimeError("directory journal disappeared")
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > RECORD_LIMIT:
        raise RuntimeError("oversized directory evidence intent")
    temporary = archive / ("intent-" + uuid.uuid4().hex + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        backup_commit._replace(temporary, archive / "intent.json")
        backup_commit._sync_directory(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def finalize(base, request_id, receipt_path):
    current = status(base, request_id, receipt_path)
    if current is None:
        raise RuntimeError("directory finalization intent is missing")
    record, complete = current
    if complete:
        return
    transaction = backup_commit.read(base, request_id)
    if transaction is None or transaction["outcome"] != record["outcome"]:
        raise RuntimeError("directory transaction changed")
    backup_commit._verify_terminal(Path(base), transaction)
    for entry in transaction["entries"]:
        backup_commit._expect(Path(base) / entry["workspace"], entry["workspace_identity"])
    archive = _paths(base, request_id)
    if record["receipt"] is not None and snapshot(receipt_path) is not None:
        backup_commit._rename(receipt_path, archive / "restore.json", record["receipt"]["identity"])
    # The durable journal is the write gate: archive it only after the receipt.
    journal = Path(base) / backup_commit.JOURNAL_NAME / (request_id + ".json")
    backup_commit._rename(journal, archive / "journal.json", record["journal"]["identity"])
    status(base, request_id, receipt_path)
