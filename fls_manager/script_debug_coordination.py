"""Serialize debug receipts in storage order and preserve command identities."""

import re
import threading
from contextlib import ExitStack

from .backup_lease import release_process_lease, try_process_lease
from .directory_jobs import DirectoryJobLock
from .storage import storage_transaction


DEBUG_IDENTITIES = DirectoryJobLock("script-debug", lambda: None)


class DebugReceiptLock:
    def __init__(self):
        self.local = threading.local()

    def __enter__(self):
        stack = ExitStack()
        try:
            stack.enter_context(storage_transaction())
            stack.enter_context(DEBUG_IDENTITIES)
        except BaseException:
            stack.close()
            raise
        if not hasattr(self.local, "stacks"):
            self.local.stacks = []
        self.local.stacks.append(stack)
        return self

    def __exit__(self, *error):
        return self.local.stacks.pop().__exit__(*error)


DEBUG_RECEIPTS = DebugReceiptLock()


def debug_active(debug_id):
    if not re.fullmatch(r"[a-f0-9]{32}", str(debug_id or "")):
        return False
    # GET must never retire a gate or remove an identity file.
    try:
        fd = try_process_lease(DEBUG_IDENTITIES.lease_path(debug_id), create=False)
    except FileNotFoundError:
        return False
    if fd is None:
        return True
    release_process_lease(fd)
    return False


def debug_active_ids():
    return {
        path.name[len("script-debug-"):-len(".lock")]
        for path in (DEBUG_IDENTITIES.base / ".script_job_locks").glob("script-debug-*.lock")
        if debug_active(path.name[len("script-debug-"):-len(".lock")])
    }
