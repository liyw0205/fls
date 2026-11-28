import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fls_manager import backup_commit, instance_lock


class BackupInstanceLockTests(unittest.TestCase):
    def contender(self, base, expected):
        result = subprocess.run(
            [sys.executable, "-c", (
                "from fls_manager.instance_lock import acquire_instance_lock, release_instance_lock\n"
                "fd = acquire_instance_lock()\n"
                "print('locked' if fd is None else 'acquired')\n"
                "if fd is not None: release_instance_lock(fd)\n"
            )],
            env=dict(os.environ, FLS_BASE_DIR=str(base)),
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), expected)

    def test_live_instance_lock_survives_commit_and_rollback_without_releasing_identity(self):
        for rollback in (False, True):
            with self.subTest(rollback=rollback), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                data = base / "data"
                data.mkdir()
                (data / "original").write_bytes(b"original")
                workspace = Path(tempfile.mkdtemp(prefix=".data-restore-", dir=base))
                staged = workspace / "replacement"
                staged.mkdir()
                (staged / "restored").write_bytes(b"restored")
                (staged / "fls-manager.lock").write_bytes(b"foreign-lock")
                entry = {"target": data, "workspace": workspace, "staged": staged}
                request_id = "a" * 32
                with patch.object(instance_lock, "DATA_DIR", data):
                    descriptor = instance_lock.acquire_instance_lock()
                    self.assertIsNotNone(descriptor)
                    try:
                        self.assertFalse(os.get_inheritable(descriptor))
                        identity = os.fstat(descriptor)
                        self.contender(base, "locked")
                        write = backup_commit._write

                        def fail_commit(base, row):
                            if rollback and row["state"] == "committed":
                                raise OSError("injected terminal publication failure")
                            return write(base, row)

                        with patch.object(backup_commit, "_write", side_effect=fail_commit):
                            if rollback:
                                with self.assertRaises(backup_commit.DirectoryCommitError) as raised:
                                    backup_commit.commit(base, request_id, [entry], ["fls-manager.lock"])
                                self.assertTrue(raised.exception.live_state_unchanged)
                            else:
                                backup_commit.commit(base, request_id, [entry], ["fls-manager.lock"])
                        self.assertTrue(os.path.samestat(identity, (data / "fls-manager.lock").stat()))
                        self.assertEqual((data / ("original" if rollback else "restored")).read_bytes(),
                                         b"original" if rollback else b"restored")
                        self.contender(base, "locked")
                        backup_commit.finish(base, request_id)
                        self.contender(base, "locked")
                    finally:
                        instance_lock.release_instance_lock(descriptor)
                    self.contender(base, "acquired")

