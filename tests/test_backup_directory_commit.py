import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fls_manager import backup_commit as commit


class BackupDirectoryCommitTests(unittest.TestCase):
    request_id = "a" * 32

    def prepare(self, base):
        entries = []
        for name in ("data", "scripts"):
            target = base / name
            target.mkdir()
            (target / "original").write_bytes(b"original")
            workspace = Path(tempfile.mkdtemp(prefix="." + name + "-restore-", dir=base))
            staged = workspace / "replacement"
            staged.mkdir()
            (staged / "restored").write_bytes(b"restored")
            entries.append({"target": target, "workspace": workspace, "staged": staged})
        (base / "data/secret_key.txt").write_bytes(b"original-key")
        (entries[0]["staged"] / "secret_key.txt").write_bytes(b"foreign-key")
        (base / "data/native_sessions.sqlite3").write_bytes(b"original-session")
        (entries[0]["staged"] / "native_sessions.sqlite3").write_bytes(b"foreign-session")
        return entries

    def run_commit(self, base, entries, checkpoint):
        paths = [{key: str(value) for key, value in entry.items()} for entry in entries]
        script = (
            "import os,json\nfrom pathlib import Path\nfrom fls_manager import backup_commit as commit\n"
            f"base=Path({str(base)!r})\n"
            f"entries=[{{k:Path(v) for k,v in row.items()}} for row in {paths!r}]\n"
            "replace=os.replace\ncount=0\n"
            "def crash_after(source,target):\n"
            " global count\n"
            " replace(source,target)\n"
            " if Path(target).suffix == '.json':\n"
            f"  row=json.loads(Path(target).read_bytes())\n  stop={checkpoint!r} == 'committed' and row['state'] == 'committed'\n"
            " else:\n"
            "  count+=1\n"
            f"  stop=count == {checkpoint!r}\n"
            " if stop: os._exit(73)\n"
            "os.replace=crash_after\n"
            f"commit.commit(base,{self.request_id!r},entries,['secret_key.txt','native_sessions.sqlite3'])\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))

    def assert_original(self, base):
        self.assertEqual((base / "data/secret_key.txt").read_bytes(), b"original-key")
        self.assertEqual((base / "data/native_sessions.sqlite3").read_bytes(), b"original-session")
        for name in ("data", "scripts"):
            self.assertEqual((base / name / "original").read_bytes(), b"original")
            self.assertFalse((base / name / "restored").exists())

    def test_hard_exit_at_every_rename_restores_original_identity_before_paths_initializes(self):
        for checkpoint in range(1, 7):
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                entries = self.prepare(base)
                inode = (base / "data/secret_key.txt").stat().st_ino
                self.run_commit(base, entries, checkpoint)
                result = subprocess.run(
                    [sys.executable, "-c", "from fls_manager.paths import DATA_DIR; print(DATA_DIR)"],
                    env=dict(os.environ, FLS_BASE_DIR=str(base)), capture_output=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assert_original(base)
                self.assertEqual((base / "data/secret_key.txt").stat().st_ino, inode)
                self.assertEqual(commit.recover(base, self.request_id), "not_applied")
                commit.finish(base, self.request_id)
                self.assertEqual(commit.pending(base), [])
                self.assertTrue(all(not entry["workspace"].exists() for entry in entries))

    def test_committed_journal_keeps_new_directories_and_original_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entries = self.prepare(base)
            self.run_commit(base, entries, "committed")
            commit.recover_at_startup(base)
            self.assertEqual(commit.recover(base, self.request_id), "restored")
            self.assertEqual((base / "data/secret_key.txt").read_bytes(), b"original-key")
            self.assertEqual((base / "data/native_sessions.sqlite3").read_bytes(), b"original-session")
            for name in ("data", "scripts"):
                self.assertEqual((base / name / "restored").read_bytes(), b"restored")
            commit.finish(base, self.request_id)
            self.assertEqual(commit.pending(base), [])

    def test_recovery_can_itself_exit_after_an_identity_move_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entries = self.prepare(base)
            self.run_commit(base, entries, 3)
            script = (
                "import os\nfrom pathlib import Path\nfrom fls_manager import backup_commit as commit\n"
                "replace=os.replace\n"
                "def crash(source,target):\n"
                " replace(source,target)\n if Path(target).name == 'secret_key.txt': os._exit(74)\n"
                "os.replace=crash\n"
                f"commit.recover(Path({str(base)!r}),{self.request_id!r})\n"
            )
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 74, result.stderr.decode(errors="replace"))
            commit.recover_at_startup(base)
            self.assert_original(base)

    def test_replaced_target_and_tampered_journal_preserve_every_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entries = self.prepare(base)
            self.run_commit(base, entries, 1)
            (base / "data").mkdir()
            (base / "data/foreign").write_bytes(b"foreign")
            lease = commit.try_process_lease(base / ".backup_native_restore.lock")
            self.assertIsNotNone(lease)
            commit.release_process_lease(lease)
            before = {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()}
            with self.assertRaises(RuntimeError):
                commit.recover_at_startup(base)
            self.assertEqual(before, {str(p.relative_to(base)): p.read_bytes() for p in base.rglob("*") if p.is_file()})
            journal = base / commit.JOURNAL_NAME / (self.request_id + ".json")
            row = json.loads(journal.read_bytes())
            row["entries"][0]["workspace"] = "../outside"
            journal.write_text(json.dumps(row))
            with self.assertRaises(RuntimeError):
                commit.recover(base, self.request_id)
            self.assertTrue((base / "data/foreign").exists())

    def test_startup_recovery_waits_for_active_process_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entries = self.prepare(base)
            self.run_commit(base, entries, 1)
            lease = commit.try_process_lease(base / ".backup_native_restore.lock")
            self.assertIsNotNone(lease)
            try:
                result = subprocess.run(
                    [sys.executable, "-c", "import fls_manager.paths"],
                    env=dict(os.environ, FLS_BASE_DIR=str(base)), capture_output=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"directory restore is active", result.stderr)
                self.assertFalse((base / "data").exists())
            finally:
                commit.release_process_lease(lease)
            commit.recover_at_startup(base)
            self.assert_original(base)

    def test_cleanup_failure_keeps_terminal_journal_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entries = self.prepare(base)
            commit.commit(base, self.request_id, entries, ["secret_key.txt", "native_sessions.sqlite3"])
            remove = commit.shutil.rmtree
            count = 0

            def fail_second(path):
                nonlocal count
                count += 1
                if count == 2:
                    raise OSError("cleanup failed")
                return remove(path)

            with patch.object(commit.shutil, "rmtree", side_effect=fail_second):
                with self.assertRaises(OSError):
                    commit.finish(base, self.request_id)
            self.assertEqual(commit.read(base, self.request_id)["state"], "finalizing")
            commit.recover_at_startup(base)
            commit.finish(base, self.request_id)
            self.assertEqual(commit.pending(base), [])

    def test_journal_publication_failure_never_moves_live_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entries = self.prepare(base)
            with patch.object(commit, "_write", side_effect=OSError("journal storage failed")):
                with self.assertRaises(commit.DirectoryCommitError) as raised:
                    commit.commit(base, self.request_id, entries, ["secret_key.txt", "native_sessions.sqlite3"])
            self.assertTrue(raised.exception.live_state_unchanged)
            self.assert_original(base)
            self.assertEqual(commit.pending(base), [])
            self.assertTrue(all(not entry["preserve_workspace"] for entry in entries))
