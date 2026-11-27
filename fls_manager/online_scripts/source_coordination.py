"""Coordinate source-cache publication and synchronous task-link requests."""

import re
import threading
from contextlib import contextmanager

from ..directory_jobs import DirectoryJobLock
from ..storage import storage_transaction


TASK_LINK_ADMISSION = DirectoryJobLock("online-task-link-admission", lambda: None)
TASK_LINK_IDENTITIES = DirectoryJobLock("online-task-link", lambda: None)
SOURCE_CACHE_LOCK = DirectoryJobLock("online-source-cache", lambda: None)


@contextmanager
def source_cache_transaction():
    with storage_transaction(), SOURCE_CACHE_LOCK:
        yield


@contextmanager
def task_link_receipt_transaction():
    # Cache publication callbacks already hold storage and the cache lock.
    with storage_transaction(), TASK_LINK_IDENTITIES:
        yield


def task_link_active_ids():
    active = set()
    for path in (TASK_LINK_IDENTITIES.base / ".script_job_locks").glob("online-task-link-*.lock"):
        request_id = path.name[len("online-task-link-"):-len(".lock")]
        if re.fullmatch(r"[a-f0-9]{32}", request_id):
            with TASK_LINK_IDENTITIES.probe(request_id) as running:
                if running:
                    active.add(request_id)
    return active


def finish_task_link_request(request_id):
    owner = TASK_LINK_IDENTITIES.owners.get(request_id)
    if owner is not None and getattr(owner, "execution_thread", None) == threading.get_ident():
        owner.close()
