"""Coordinate synchronous online task imports without holding admission during I/O."""

import re
import threading
from contextlib import contextmanager

from ..directory_jobs import DirectoryJobLock
from ..storage import storage_transaction


ONLINE_TASK_IMPORT_LOCK = DirectoryJobLock("online-task-import-admission", lambda: None)
TASK_IMPORT_IDENTITIES = DirectoryJobLock("online-task-import", lambda: None)
TASK_IMPORT_PUBLICATION_LOCK = DirectoryJobLock("online-task-publication", lambda: None)


@contextmanager
def task_import_receipt_transaction():
    # Publication callbacks already hold storage; identities and receipts share one lock.
    with storage_transaction(), TASK_IMPORT_IDENTITIES:
        yield


def task_import_active_ids():
    directory = TASK_IMPORT_IDENTITIES.base / ".script_job_locks"
    active = set()
    for path in directory.glob("online-task-import-*.lock"):
        request_id = path.name[len("online-task-import-"):-len(".lock")]
        if re.fullmatch(r"[a-f0-9]{32}", request_id):
            with TASK_IMPORT_IDENTITIES.probe(request_id) as running:
                if running:
                    active.add(request_id)
    return active


def finish_task_import_request(request_id):
    owner = TASK_IMPORT_IDENTITIES.owners.get(request_id)
    if owner is not None and getattr(owner, "execution_thread", None) == threading.get_ident():
        owner.close()
