import os
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.test_notify_action_recovery import isolated_app


class BackupSqliteWalTests(unittest.TestCase):
    request_id = "a" * 32

    def prepare(self, base):
        from fls_manager.routes.backup import restore

        path = base / "data/external.sqlite3"
        script = (
            "import os,sqlite3,sys\n"
            "db=sqlite3.connect(sys.argv[1])\n"
            "assert db.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'\n"
            "db.execute('CREATE TABLE evidence(value TEXT)')\n"
            "db.execute(\"INSERT INTO evidence VALUES ('main')\")\n"
            "db.commit()\n"
            "assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]==0\n"
            "db.execute(\"INSERT INTO evidence VALUES ('wal-only')\")\n"
            "db.commit()\n"
            "os._exit(0)\n"
        )
        result = subprocess.run([sys.executable, "-c", script, str(path)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        group = [path.with_name(path.name + suffix) for suffix in ("", "-wal", "-shm")]
        self.assertTrue(all(item.is_file() for item in group))
        immutable = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
        try:
            self.assertEqual(immutable.execute("SELECT value FROM evidence").fetchall(), [("main",)])
        finally:
            immutable.close()
        before = {item.name: (item.stat(), item.read_bytes()) for item in group}
        source = base / "archive/data"
        source.mkdir(parents=True)
        (source / "restored").write_bytes(b"restored")
        for name in before:
            (source / name).write_bytes(b"foreign archived runtime")
        for suffix in ("", "-wal", "-shm", "-journal"):
            (source / ("archive-only.sqlite3" + suffix)).write_bytes(b"foreign runtime")
        entry = restore._stage_restore_directory(source, restore.DATA_DIR)
        names = restore._runtime_identity_names(restore.DATA_DIR, entry["staged"])
        return entry, names, before

    def assert_preserved(self, base, before):
        for name, (identity, content) in before.items():
            path = base / "data" / name
            self.assertTrue(os.path.samestat(identity, path.stat()), name)
            self.assertEqual(path.read_bytes(), content, name)
        self.assertFalse(list((base / "data").glob("archive-only.sqlite3*")))

    def read_wal(self, base):
        from fls_manager.data_access import directory_access

        with directory_access(base):
            db = sqlite3.connect((base / "data/external.sqlite3").as_uri() + "?mode=ro", uri=True)
            try:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
                self.assertEqual(db.execute("SELECT value FROM evidence ORDER BY rowid").fetchall(),
                                 [("main",), ("wal-only",)])
            finally:
                db.close()

    def test_real_orphaned_wal_survives_commit_and_rollback_and_read_only_access(self):
        for rollback in (False, True):
            with self.subTest(rollback=rollback), isolated_app() as (_, base):
                from fls_manager import backup_commit
                from fls_manager.routes.backup import restore

                entry, names, before = self.prepare(base)
                write = backup_commit._write
                terminal = []

                def save(base, row):
                    if row["state"] == "committed":
                        terminal.append(True)
                        if rollback:
                            raise OSError("terminal journal publication failed")
                    return write(base, row)

                with patch.object(backup_commit, "_write", side_effect=save):
                    if rollback:
                        with self.assertRaises(restore.BackupRestoreWriteError) as raised:
                            restore._commit_restore_directories(
                                [entry], preserve_runtime_identity=True, request_id=self.request_id,
                            )
                        self.assertTrue(raised.exception.live_state_unchanged)
                    else:
                        restore._commit_restore_directories(
                            [entry], preserve_runtime_identity=True, request_id=self.request_id,
                        )
                self.assertEqual(terminal, [True])
                self.assert_preserved(base, before)
                self.assertEqual((base / "data/restored").exists(), not rollback)
                self.read_wal(base)
                backup_commit.finish(base, self.request_id)
                self.read_wal(base)
                self.assertEqual(backup_commit.pending(base), [])
                self.assertFalse(entry["workspace"].exists())

    def test_every_interrupted_wal_move_and_commit_publication_recover_before_database_access(self):
        with isolated_app() as (_, base):
            from fls_manager import backup_commit

            entry, names, before = self.prepare(base)
            with patch.object(backup_commit, "_rename", wraps=backup_commit._rename) as rename:
                backup_commit.commit(base, self.request_id, [entry], names)
            checkpoints = rename.call_count
            self.assertGreaterEqual(checkpoints, len(before) + 2)
            self.assert_preserved(base, before)
            self.read_wal(base)
            backup_commit.finish(base, self.request_id)

        for checkpoint in (*range(1, checkpoints + 1), "committed"):
            with self.subTest(checkpoint=checkpoint), isolated_app() as (_, base):
                from fls_manager import backup_commit

                entry, names, before = self.prepare(base)
                paths = {key: str(entry[key]) for key in ("target", "workspace", "staged")}
                script = (
                    "import os\nfrom pathlib import Path\nfrom fls_manager import backup_commit as commit\n"
                    f"base=Path({str(base)!r})\n"
                    f"entry={{k:Path(v) for k,v in {paths!r}.items()}}\n"
                    "rename=commit._rename\ncount=0\n"
                    "def crash(source,target,identity):\n"
                    " global count\n rename(source,target,identity)\n count+=1\n"
                    f" if count=={checkpoint!r}: os._exit(73)\n"
                    "commit._rename=crash\n"
                    "write=commit._write\n"
                    "def save(base,row):\n write(base,row)\n"
                    f" if {checkpoint!r}=='committed' and row['state']=='committed': os._exit(73)\n"
                    "commit._write=save\n"
                    f"commit.commit(base,{self.request_id!r},[entry],{names!r})\n"
                )
                result = subprocess.run([sys.executable, "-c", script],
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))
                result = subprocess.run(
                    [sys.executable, "-c", "from fls_manager.paths import DATA_DIR; print(DATA_DIR)"],
                    env=dict(os.environ, FLS_BASE_DIR=str(base)), capture_output=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assert_preserved(base, before)
                self.assertEqual((base / "data/restored").exists(), checkpoint == "committed")
                self.assertEqual(backup_commit.recover(base, self.request_id),
                                 "restored" if checkpoint == "committed" else "not_applied")
                self.read_wal(base)
                backup_commit.finish(base, self.request_id)
                self.read_wal(base)
                self.assertEqual(backup_commit.pending(base), [])
                self.assertFalse(entry["workspace"].exists())

