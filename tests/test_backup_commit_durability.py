import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fls_manager import backup_commit as commit


class BackupCommitDurabilityTests(unittest.TestCase):
    request_id = "a" * 32

    def prepare(self, base):
        target = base / "data"
        target.mkdir()
        (target / "config.json").write_bytes(b"original config")
        (target / "secret_key.txt").write_bytes(b"original identity")
        workspace = Path(tempfile.mkdtemp(prefix=".data-restore-", dir=base))
        staged = workspace / "replacement"
        (staged / "nested").mkdir(parents=True)
        (staged / "config.json").write_bytes(b"restored config")
        (staged / "nested/script.py").write_bytes(b"restored script")
        (staged / "secret_key.txt").write_bytes(b"foreign identity")
        return {"target": target, "workspace": workspace, "staged": staged}

    def assert_original(self, base):
        self.assertEqual((base / "data/config.json").read_bytes(), b"original config")
        self.assertEqual((base / "data/secret_key.txt").read_bytes(), b"original identity")
        self.assertFalse((base / "data/nested").exists())

    def test_original_and_replacement_files_are_flushed_before_prepared_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entry = self.prepare(base)
            files = [base / "data/config.json", base / "data/secret_key.txt",
                     entry["staged"] / "config.json", entry["staged"] / "nested/script.py"]
            expected = {(path.stat().st_dev, path.stat().st_ino) for path in files}
            directories = [entry["staged"], entry["staged"] / "nested", entry["workspace"], base / "data"]
            expected_directories = {(path.stat().st_dev, path.stat().st_ino) for path in directories}
            flushed = set()
            flushed_directories = set()
            fsync, write = commit.os.fsync, commit._write

            def sync(fd):
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode):
                    flushed.add((info.st_dev, info.st_ino))
                elif stat.S_ISDIR(info.st_mode):
                    flushed_directories.add((info.st_dev, info.st_ino))
                return fsync(fd)

            def save(base, row):
                if row["state"] == "prepared":
                    self.assertTrue(expected <= flushed, "directory intent preceded file flushes")
                    if os.name != "nt":
                        self.assertTrue(expected_directories <= flushed_directories,
                                        "directory intent preceded tree metadata flushes")
                return write(base, row)

            with patch.object(commit.os, "fsync", side_effect=sync), \
                    patch.object(commit, "_write", side_effect=save):
                commit.commit(base, self.request_id, [entry], ["secret_key.txt"])
            self.assertEqual((base / "data/config.json").read_bytes(), b"restored config")
            self.assertEqual((base / "data/nested/script.py").read_bytes(), b"restored script")
            self.assertEqual((base / "data/secret_key.txt").read_bytes(), b"original identity")
            commit.finish(base, self.request_id)

    def test_file_flush_failure_leaves_live_data_and_identity_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entry = self.prepare(base)
            identity = (entry["staged"] / "config.json").stat()
            fsync = commit.os.fsync
            failures = []

            def fail(fd):
                if os.path.samestat(identity, os.fstat(fd)):
                    failures.append(True)
                    raise OSError("device rejected staged data flush")
                return fsync(fd)

            with patch.object(commit.os, "fsync", side_effect=fail), \
                    patch.object(commit, "_rename", wraps=commit._rename) as rename:
                with self.assertRaises(commit.DirectoryCommitError) as raised:
                    commit.commit(base, self.request_id, [entry], ["secret_key.txt"])
            self.assertEqual(failures, [True])
            self.assertTrue(raised.exception.live_state_unchanged)
            rename.assert_not_called()
            self.assert_original(base)
            self.assertEqual(commit.pending(base), [])
            self.assertFalse(entry.get("preserve_workspace", False))
            self.assertTrue(entry["workspace"].is_dir())

    def test_unreadable_subtree_is_rejected_before_live_directory_moves(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entry = self.prepare(base)
            unreadable = entry["staged"] / "nested"
            scandir = commit.os.scandir
            failures = []

            def scan(path):
                if not isinstance(path, int) and Path(path) == unreadable:
                    failures.append(True)
                    raise PermissionError("restore subtree became unreadable")
                return scandir(path)

            with patch.object(commit.os, "scandir", side_effect=scan), \
                    patch.object(commit, "_rename", wraps=commit._rename) as rename:
                with self.assertRaises(commit.DirectoryCommitError) as raised:
                    commit.commit(base, self.request_id, [entry], ["secret_key.txt"])
            self.assertEqual(failures, [True])
            self.assertTrue(raised.exception.live_state_unchanged)
            rename.assert_not_called()
            self.assert_original(base)
            self.assertEqual(commit.pending(base), [])

    @unittest.skipUnless(os.name == "nt", "requires the Windows file API")
    def test_windows_junction_in_restore_tree_cannot_flush_outside_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            entry = self.prepare(base)
            outside = base / "outside"
            outside.mkdir()
            (outside / "private").write_bytes(b"outside data")
            nested = entry["staged"] / "nested"
            shutil.rmtree(nested)
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(nested), str(outside)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertTrue(nested.lstat().st_file_attributes & 0x400)
            with patch.object(commit, "_rename", wraps=commit._rename) as rename:
                with self.assertRaises(commit.DirectoryCommitError) as raised:
                    commit.commit(base, self.request_id, [entry], ["secret_key.txt"])
            self.assertTrue(raised.exception.live_state_unchanged)
            rename.assert_not_called()
            self.assert_original(base)
            self.assertEqual((outside / "private").read_bytes(), b"outside data")
            self.assertEqual(commit.pending(base), [])

    @unittest.skipUnless(os.name == "nt", "requires the Windows file API")
    def test_windows_moves_files_and_directories_with_write_through_without_copy_fallback(self):
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        move = api.MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source, target = base / "source", base / "target"
            source.write_bytes(b"new receipt")
            target.write_bytes(b"old receipt")
            with patch.object(api, "MoveFileExW", wraps=move) as native_move, \
                    patch.object(ctypes, "WinDLL", return_value=api), \
                    patch.object(commit.os, "replace", side_effect=AssertionError("unflushed fallback")):
                commit._replace(source, target)
                self.assertEqual(target.read_bytes(), b"new receipt")
                self.assertFalse(source.exists())
                source.mkdir()
                (source / "child").write_bytes(b"directory content")
                commit._replace(source, base / "moved")
                self.assertEqual((base / "moved/child").read_bytes(), b"directory content")
                with self.assertRaises(OSError):
                    commit._replace(target, base / "moved")
                self.assertEqual(target.read_bytes(), b"new receipt")
                self.assertEqual((base / "moved/child").read_bytes(), b"directory content")
            self.assertEqual(native_move.call_count, 3)
            for call in native_move.call_args_list:
                self.assertTrue(call.args[2] & 0x8)
                self.assertFalse(call.args[2] & 0x2)
