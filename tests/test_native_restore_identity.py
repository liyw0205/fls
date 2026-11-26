import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_notify_action_recovery import isolated_app


class NativeRestoreIdentityTests(unittest.TestCase):
    def prepare(self, base, restore):
        live = restore.DATA_DIR
        live.mkdir(parents=True, exist_ok=True)
        identities = {
            "secret_key.txt": b"live-secret",
            "native_sessions.sqlite3": b"live-sessions",
            "native_sessions.sqlite3-wal": b"live-wal",
            "proxy_requests/receipt.json": b"live-receipt",
            "native_notify_deliveries/delivery.json": b"live-delivery",
            "fls-manager.lock": b"live-instance-lock",
        }
        for name, content in identities.items():
            target = live / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (live / "config.json").write_bytes(b"live-config")
        archived = base / "archive" / "data"
        archived.mkdir(parents=True)
        for name in identities:
            target = archived / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"archived-identity")
        (archived / "config.json").write_bytes(b"archived-config")
        scripts = base / "archive" / "scripts"
        scripts.mkdir()
        (scripts / "restored.py").write_bytes(b"archived-script")
        restore.SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        (restore.SCRIPT_DIR / "original.py").write_bytes(b"live-script")
        return archived, scripts, identities

    def snapshot(self, directory):
        return {
            str(path.relative_to(directory)): path.read_bytes() if path.is_file() else None
            for path in directory.rglob("*")
        }

    def test_archived_runtime_state_is_removed_even_without_live_counterparts(self):
        with isolated_app() as (_, base):
            from fls_manager.routes.backup import restore

            archived, _, identities = self.prepare(base, restore)
            extra = {
                "native_sessions.sqlite3-shm": b"archived-shm",
                "native_sessions.sqlite3-journal": b"archived-journal",
                "old_receipts/receipt.json": b"archived-receipt",
                "proxy_checks/check.json": b"archived-probe",
                "fls_code.json": b"archived-verification",
            }
            for name, content in extra.items():
                target = archived / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            inodes = {name: (restore.DATA_DIR / name).stat().st_ino for name in identities}
            entry = restore._stage_restore_directory(archived, restore.DATA_DIR)

            restore._commit_restore_directories([entry], preserve_runtime_identity=True)

            self.assertEqual((restore.DATA_DIR / "config.json").read_bytes(), b"archived-config")
            for name, content in identities.items():
                self.assertEqual((restore.DATA_DIR / name).read_bytes(), content)
                self.assertEqual((restore.DATA_DIR / name).stat().st_ino, inodes[name])
            for name in extra:
                self.assertFalse((restore.DATA_DIR / name).exists())
            shutil.rmtree(entry["workspace"])

    def test_restore_without_existing_data_does_not_import_instance_identity(self):
        with isolated_app() as (_, base):
            from fls_manager.routes.backup import restore

            archived, _, identities = self.prepare(base, restore)
            shutil.rmtree(restore.DATA_DIR)
            entry = restore._stage_restore_directory(archived, restore.DATA_DIR)

            restore._commit_restore_directories([entry], preserve_runtime_identity=True)

            self.assertEqual((restore.DATA_DIR / "config.json").read_bytes(), b"archived-config")
            for name in identities:
                self.assertFalse((restore.DATA_DIR / name).exists())
            shutil.rmtree(entry["workspace"])

    def test_each_transfer_and_directory_publication_failure_rolls_back_original_state(self):
        failures = [
            "fls-manager.lock", "native_notify_deliveries", "native_sessions.sqlite3",
            "native_sessions.sqlite3-wal", "proxy_requests", "secret_key.txt",
            "data-publication", "scripts-publication",
        ]
        for failure in failures:
            with self.subTest(failure=failure), isolated_app() as (_, base):
                from fls_manager.routes.backup import restore

                archived, scripts, _ = self.prepare(base, restore)
                before = self.snapshot(restore.DATA_DIR)
                scripts_before = self.snapshot(restore.SCRIPT_DIR)
                entries = [restore._stage_restore_directory(archived, restore.DATA_DIR),
                           restore._stage_restore_directory(scripts, restore.SCRIPT_DIR)]
                replace = restore.os.replace

                def fail_one_transfer(source, destination):
                    source, destination = Path(source), Path(destination)
                    identity_failure = source.parent == entries[0]["workspace"] / "previous" and source.name == failure
                    publication_failure = source.name == "replacement" and (
                        failure == "data-publication" and destination == restore.DATA_DIR
                        or failure == "scripts-publication" and destination == restore.SCRIPT_DIR
                    )
                    if identity_failure or publication_failure:
                        raise OSError("injected identity/publication failure")
                    return replace(source, destination)

                with patch.object(restore.os, "replace", side_effect=fail_one_transfer):
                    with self.assertRaises(restore.BackupRestoreWriteError) as raised:
                        restore._commit_restore_directories(entries, preserve_runtime_identity=True)

                self.assertTrue(raised.exception.live_state_unchanged)
                self.assertEqual(self.snapshot(restore.DATA_DIR), before)
                self.assertEqual(self.snapshot(restore.SCRIPT_DIR), scripts_before)
                for entry in entries:
                    self.assertFalse(entry["preserve_workspace"])
                    shutil.rmtree(entry["workspace"])

    def test_rollback_failure_preserves_both_original_identity_and_recovery_workspace(self):
        with isolated_app() as (_, base):
            from fls_manager.routes.backup import restore

            archived, _, identities = self.prepare(base, restore)
            entry = restore._stage_restore_directory(archived, restore.DATA_DIR)
            replace = restore.os.replace

            def fail_publication_and_identity_rollback(source, destination):
                source, destination = Path(source), Path(destination)
                if source == entry["staged"] and destination == restore.DATA_DIR:
                    raise OSError("publication failed")
                if source == entry["staged"] / "proxy_requests":
                    raise OSError("identity rollback failed")
                return replace(source, destination)

            with patch.object(restore.os, "replace", side_effect=fail_publication_and_identity_rollback):
                with self.assertRaises(restore.BackupRestoreWriteError) as raised:
                    restore._commit_restore_directories([entry], preserve_runtime_identity=True)

            self.assertFalse(raised.exception.live_state_unchanged)
            self.assertTrue(entry["preserve_workspace"])
            for name, content in identities.items():
                candidates = [entry["staged"] / name, entry["previous"] / name]
                found = [path for path in candidates if path.exists()]
                self.assertEqual(len(found), 1, name)
                self.assertEqual(found[0].read_bytes(), content)
            restore._rollback_runtime_identity_files(
                entry["staged"], entry["previous"], entry["runtime_identity_moves"],
            )
            replace(entry["previous"], restore.DATA_DIR)
            for name, content in identities.items():
                self.assertEqual((restore.DATA_DIR / name).read_bytes(), content)
            shutil.rmtree(entry["workspace"])
