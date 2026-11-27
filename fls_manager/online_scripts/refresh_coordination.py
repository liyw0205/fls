"""Coordinate refresh receipts with cache publication and task-link recovery."""

import re
import threading
from contextlib import ExitStack

from ..directory_jobs import DirectoryJobLock
from ..storage import storage_transaction
from .source_coordination import SOURCE_CACHE_LOCK, TASK_LINK_IDENTITIES


REFRESH_IDENTITIES = DirectoryJobLock("online-refresh", lambda: None)


class RefreshReceiptLock:
    def __init__(self):
        self.local = threading.local()

    def __enter__(self):
        # Publication callbacks use cache -> task-link receipts. Never invert it.
        stack = ExitStack()
        try:
            for context in (storage_transaction(), SOURCE_CACHE_LOCK,
                            REFRESH_IDENTITIES, TASK_LINK_IDENTITIES):
                stack.enter_context(context)
        except BaseException:
            stack.close()
            raise
        if not hasattr(self.local, "stacks"):
            self.local.stacks = []
        self.local.stacks.append(stack)
        return self

    def __exit__(self, *error):
        return self.local.stacks.pop().__exit__(*error)


ONLINE_REFRESH_LOCK = RefreshReceiptLock()


def refresh_active_ids():
    active = set()
    for path in (REFRESH_IDENTITIES.base / ".script_job_locks").glob("online-refresh-*.lock"):
        request_id = path.name[len("online-refresh-"):-len(".lock")]
        if re.fullmatch(r"[a-f0-9]{32}", request_id):
            with REFRESH_IDENTITIES.probe(request_id) as running:
                if running:
                    active.add(request_id)
    return active


def refresh_active(request_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(request_id or "")):
        return False
    with REFRESH_IDENTITIES.probe(request_id) as active:
        return active


def refresh_action_busy(request_id):
    owner = REFRESH_IDENTITIES.owners.get(request_id)
    if owner is not None and getattr(owner, "recovery_thread", None) == threading.get_ident():
        return False
    return refresh_active(request_id)
