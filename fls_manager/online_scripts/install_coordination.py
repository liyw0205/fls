"""Cross-worker admission, receipt transactions and legacy install identities."""

import hashlib
import re
from contextlib import contextmanager

from ..directory_jobs import DirectoryJobLock
from ..storage import storage_transaction


class OnlineInstallLock(DirectoryJobLock):
    def lease_path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id):
            raise ValueError("invalid online install ID")
        identity = hashlib.sha256(("online-install:" + job_id).encode("ascii")).hexdigest()[:32]
        return super().lease_path(identity)


ONLINE_INSTALL_LOCK = OnlineInstallLock("online-install", lambda: None)
_RECEIPT_LOCK = DirectoryJobLock("online-install-receipts", lambda: None)


@contextmanager
def install_receipt_transaction():
    # Task publication already holds storage; every receipt access uses this order.
    with storage_transaction(), _RECEIPT_LOCK:
        yield
