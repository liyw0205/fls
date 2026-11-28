"""Explicit orphan cleanup attempts and retention of damaged restore bytes."""

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import dependency_evidence, directory_evidence
from ... import backup_commit
from ...backup_lease import try_process_lease, release_process_lease

DIRECTORY_NAME = ".backup_native_dependency_maintenance"
RECORD_LIMIT = 16 * 1024


@contextmanager
def transaction(base):
    fd = try_process_lease(Path(base) / ".backup_native_dependency_maintenance.lock")
    if fd is None:
        raise RuntimeError("dependency maintenance request is busy")
    try:
        yield
    finally:
        release_process_lease(fd)


def _directory(base, request_id, operation_id, *, create=False):
    dependency_evidence._path(base, request_id, operation_id)
    root = Path(base) / DIRECTORY_NAME
    dependency_evidence._directory(root, create=create)
    path = root / (request_id + "-" + operation_id)
    dependency_evidence._directory(path, create=create)
    return path


def read(base, request_id, operation_id):
    directory = _directory(base, request_id, operation_id)
    encoded, _ = directory_evidence._bytes(directory / "intent.json", RECORD_LIMIT)
    if encoded is None:
        return None
    row = json.loads(encoded, object_pairs_hook=backup_commit._unique_object)
    if (not isinstance(row, dict) or set(row) != {
            "version", "request_id", "operation_id", "attempt_id", "attempt", "evidence",
            "receipt", "confirmation_version", "status", "created_at", "storage",
        } or type(row["version"]) is not int or row["version"] != 1
        or row["request_id"] != request_id or row["operation_id"] != operation_id
        or not isinstance(row["attempt_id"], str)
        or backup_commit.REQUEST_ID.fullmatch(row["attempt_id"]) is None
        or type(row["attempt"]) is not int or not 1 <= row["attempt"] <= 1000
        or type(row["created_at"]) is not int or row["created_at"] < 0
        or row["receipt"] is not None and not directory_evidence._valid_snapshot(row["receipt"])
        or not isinstance(row["confirmation_version"], str)
        or directory_evidence.DIGEST.fullmatch(row["confirmation_version"]) is None
        or not isinstance(row["status"], str)
        or row["status"] not in {"requested", "attempted", "failed", "resources_cleaned"}
        or not backup_commit._valid_identity(row["storage"], directory=True)):
        raise RuntimeError("invalid dependency maintenance intent")
    dependency_evidence._validate(row["evidence"], request_id, operation_id)
    if row["status"] == "resources_cleaned" and row["evidence"]["state"] != "cleaned":
        raise RuntimeError("unconfirmed dependency resource result")
    backup_commit._expect(Path(base), row["evidence"]["base"])
    backup_commit._expect(directory, row["storage"])
    return row


def _publish(base, row):
    directory = _directory(base, row["request_id"], row["operation_id"])
    backup_commit._expect(directory, row["storage"])
    backup_commit._expect(Path(base), row["evidence"]["base"])
    encoded = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("ascii")
    if len(encoded) > RECORD_LIMIT:
        raise RuntimeError("dependency maintenance intent exceeds limit")
    temporary = directory / ("intent-" + uuid.uuid4().hex + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        backup_commit._expect(directory, row["storage"])
        backup_commit._replace(temporary, directory / "intent.json")
        backup_commit._sync_directory(directory)
    finally:
        temporary.unlink(missing_ok=True)


def request_cleanup(base, evidence, receipt, confirmation_version):
    request_id, operation_id = evidence["request_id"], evidence["operation_id"]
    previous = read(base, request_id, operation_id)
    directory = _directory(base, request_id, operation_id, create=True)
    if directory_evidence.snapshot(directory / "restore.json") is not None:
        raise RuntimeError("dependency original receipt is already archived")
    attempt = previous["attempt"] + 1 if previous is not None else 1
    if attempt > 1000:
        raise RuntimeError("dependency maintenance attempt limit reached")
    row = {
        "version": 1, "request_id": request_id, "operation_id": operation_id,
        "attempt_id": uuid.uuid4().hex, "attempt": attempt, "evidence": evidence,
        "receipt": receipt, "confirmation_version": confirmation_version,
        "status": "requested", "created_at": int(time.time()),
        "storage": backup_commit._identity(directory),
    }
    _publish(base, row)
    return row


def advance(base, previous, status, *, evidence=None):
    if read(base, previous["request_id"], previous["operation_id"]) != previous:
        raise RuntimeError("dependency maintenance attempt changed")
    allowed = {"requested": {"attempted", "failed"}, "attempted": {"failed", "resources_cleaned"},
               "failed": set(), "resources_cleaned": {"resources_cleaned"}}
    if status not in allowed[previous["status"]]:
        raise RuntimeError("invalid dependency maintenance transition")
    evidence = previous["evidence"] if evidence is None else evidence
    dependency_evidence._validate(evidence, previous["request_id"], previous["operation_id"])
    if (any(evidence[key] != previous["evidence"][key]
            for key in ("base", "storage", "work_root", "workspace", "guard"))
        or status == "resources_cleaned" and evidence["state"] != "cleaned"):
        raise RuntimeError("dependency maintenance ownership changed")
    row = dict(previous, status=status, evidence=evidence)
    _publish(base, row)
    return row


def archive_status(base, row, receipt_path):
    directory = _directory(base, row["request_id"], row["operation_id"])
    live = directory_evidence._original_location(
        receipt_path, directory / "restore.json", row["receipt"], directory_evidence.RECEIPT_LIMIT,
    )
    return row["status"] == "resources_cleaned" and not live


def finalize(base, row, receipt_path):
    if read(base, row["request_id"], row["operation_id"]) != row or row["status"] != "resources_cleaned":
        raise RuntimeError("dependency resources are not durably confirmed")
    if dependency_evidence.read(base, row["request_id"], row["operation_id"]) != row["evidence"]:
        raise RuntimeError("dependency resource receipt changed")
    # A prior publish/move may have succeeded before its durability acknowledgement failed.
    _publish(base, row)
    directory = _directory(base, row["request_id"], row["operation_id"])
    if archive_status(base, row, receipt_path):
        backup_commit._sync_directory(receipt_path.parent)
        backup_commit._sync_directory(directory)
        return
    backup_commit._rename(receipt_path, directory / "restore.json", row["receipt"]["identity"])
    if not archive_status(base, row, receipt_path):
        raise RuntimeError("dependency receipt archival is not confirmed")
