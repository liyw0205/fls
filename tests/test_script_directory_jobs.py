import contextlib
import importlib
import io
import os
import stat
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_backup_activity_lease as activity_tests
from tests import test_backup_data_access as access_tests
from tests import test_native_script_pull_api as pull_tests
from tests import test_native_script_repo_pull_api as repo_tests
from tests.test_ui_route_components import TOKEN, isolated_app


KINDS = ("pull", "repo-pull", "import")
MODULES = {"pull": "native_pull", "repo-pull": "native_repo_pull", "import": "native_import"}


class ScriptDirectoryJobTests(unittest.TestCase):
    def module(self, kind):
        return importlib.import_module("fls_manager.routes.scripts." + MODULES[kind])

    def submit(self, client, kind):
        if kind == "import":
            return client.post("/api/scripts/import", data={
                "file": (io.BytesIO(b"print('imported')\n"), "imported.py"),
                "path": "imported.py", "request_id": "a" * 32,
            }, headers={"X-Token": TOKEN})
        return client.post("/api/scripts/" + kind, json={
            "url": "https://example.invalid/source", "request_id": "a" * 32,
            "path": "repo" if kind == "repo-pull" else "pulled.py",
        }, headers={"X-Token": TOKEN})

    def assert_restore_blocked(self):
        from fls_manager.backup_lease import release_process_lease, try_process_lease
        from fls_manager.paths import BASE_DIR
        from fls_manager.routes.backup import _common as backup

        acquired = backup.try_begin_restore_activity()
        if acquired:
            backup.end_restore_activity()
        self.assertFalse(acquired)
        fd = try_process_lease(BASE_DIR / ".backup_native_restore.lock")
        try:
            self.assertIsNone(fd)
        finally:
            release_process_lease(fd)

    def assert_restore_available(self):
        from fls_manager.routes.backup import _common as backup

        self.assertTrue(backup.try_begin_restore_activity())
        backup.end_restore_activity()

    @contextlib.contextmanager
    def fake_network(self, kind, base):
        module = self.module(kind)
        with contextlib.ExitStack() as stack:
            if kind == "pull":
                stack.enter_context(patch.object(module, "requests", SimpleNamespace(
                    get=lambda *args, **kwargs: pull_tests.FakeResponse([b"pulled"], 6),
                )))
            elif kind == "repo-pull":
                helper = repo_tests.NativeScriptRepoPullApiTests()
                source = helper._source_repo(base)
                stack.enter_context(patch.object(module, "build_repo_pull_command",
                                                side_effect=helper._clone_command(source, delay=0)))
            yield

    def test_restore_rejects_every_script_job_route_before_history_or_side_effects(self):
        helper = activity_tests.BackupActivityLeaseTests()
        for mode in ("legacy", "native"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                client = app.test_client()
                with helper.holder(mode), contextlib.ExitStack() as stack:
                    histories = [stack.enter_context(patch.object(self.module(kind), "_ensure_loaded"))
                                 for kind in KINDS]
                    before = sorted(str(item.relative_to(base)) for item in base.rglob("*"))
                    for kind in KINDS:
                        response = self.submit(client, kind)
                        self.assertEqual(response.status_code, 503, response.json)
                        self.assertEqual(response.json["code"], "panel_storage_busy")
                        for suffix, method in (("-history", "get"), ("/" + "b" * 32, "get"),
                                               ("/" + "b" * 32 + "/cancel", "post")):
                            response = getattr(client, method)("/api/scripts/" + kind + suffix,
                                                               headers={"X-Token": TOKEN})
                            self.assertEqual(response.status_code, 503, response.json)
                            self.assertEqual(response.json["code"], "panel_storage_busy")
                    for history in histories:
                        history.assert_not_called()
                    self.assertEqual(sorted(str(item.relative_to(base)) for item in base.rglob("*")), before)
                self.assert_restore_available()

    def test_pending_journal_and_short_io_lock_reject_admission_without_staging(self):
        helper = access_tests.BackupDataAccessTests()
        for mode in ("journal", "io"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                with contextlib.ExitStack() as stack:
                    if mode == "journal":
                        from fls_manager.backup_commit import JOURNAL_NAME

                        directory = base / JOURNAL_NAME
                        directory.mkdir()
                        journal = directory / ("b" * 32 + ".json")
                        journal.write_bytes(b"{broken")
                    else:
                        stack.enter_context(helper.paused_process(
                            "with directory_access(exclusive=True):\n"
                            " print('ready',flush=True)\n sys.stdin.readline()\n",
                        ))
                    for kind in KINDS:
                        response = self.submit(app.test_client(), kind)
                        self.assertEqual(response.status_code, 503, response.json)
                        self.assertEqual(response.json["code"], "panel_storage_busy")
                    self.assertEqual(list((base / "data").glob(".script-import-*")), [])
                    self.assertEqual(list((base / "scripts").iterdir()), [])
                    if mode == "journal":
                        self.assertEqual(journal.read_bytes(), b"{broken")
                        journal.unlink()
                self.assert_restore_available()

    def test_queued_jobs_have_no_request_to_worker_gap_and_release_after_real_cleanup(self):
        for kind in KINDS:
            with self.subTest(kind=kind), isolated_app() as (app, base), self.fake_network(kind, base):
                queued = []

                def queue_worker(worker):
                    self.assert_restore_blocked()
                    queued.append(worker)

                try:
                    with patch.object(threading.Thread, "start", autospec=True, side_effect=queue_worker):
                        response = self.submit(app.test_client(), kind)
                    self.assertEqual(response.status_code, 202, response.json)
                    self.assertEqual(len(queued), 1)
                    self.assert_restore_blocked()
                finally:
                    for worker in queued:
                        worker.run()
                result = app.test_client().get("/api/scripts/" + kind + "/" + response.json["job"]["id"],
                                               headers={"X-Token": TOKEN})
                self.assertEqual(result.json["job"]["status"], "complete", result.json)
                self.assertEqual(list((base / "scripts").glob(".fls-*")), [])
                self.assertEqual(list((base / "data").glob(".script-import-*")), [])
                self.assert_restore_available()

    def test_persistence_constructor_and_start_failures_release_leases_and_staging(self):
        for kind in KINDS:
            for failure in ("identity", "persistence", "constructor", "start", "start_and_receipt"):
                with self.subTest(kind=kind, failure=failure), isolated_app() as (app, base):
                    module = self.module(kind)
                    client = app.test_client()
                    module._ensure_loaded()
                    parent_fds = []
                    with contextlib.ExitStack() as stack:
                        if kind == "repo-pull":
                            original_open = module._open_script_parent

                            def open_parent(path):
                                result = original_open(path)
                                if result[1] is not None:
                                    parent_fds.append(result[1])
                                return result

                            stack.enter_context(patch.object(module, "_open_script_parent", side_effect=open_parent))
                        if failure == "identity":
                            job_lock = getattr(module, {
                                "pull": "SCRIPT_PULL_LOCK", "repo-pull": "SCRIPT_REPO_PULL_LOCK",
                                "import": "SCRIPT_IMPORT_LOCK",
                            }[kind])
                            stack.enter_context(patch.object(job_lock, "claim", side_effect=OSError("full")))
                        elif failure == "persistence":
                            stack.enter_context(patch.object(module, "_save_locked", side_effect=OSError("full")))
                        elif failure == "constructor":
                            stack.enter_context(patch.object(threading, "Thread", side_effect=RuntimeError("unavailable")))
                        else:
                            stack.enter_context(patch.object(threading.Thread, "start", side_effect=RuntimeError("unavailable")))
                            if failure == "start_and_receipt":
                                original = module._save_locked
                                calls = []

                                def save_once():
                                    calls.append(True)
                                    if len(calls) > 1:
                                        raise OSError("full")
                                    return original()

                                stack.enter_context(patch.object(module, "_save_locked", side_effect=save_once))
                        response = self.submit(client, kind)
                    self.assertEqual(response.status_code, 503, response.json)
                    self.assertEqual(list((base / "scripts").iterdir()), [])
                    self.assertEqual(list((base / "data").glob(".script-import-*")), [])
                    for fd in parent_fds:
                        with self.assertRaises(OSError):
                            os.fstat(fd)
                    self.assert_restore_available()

    def test_worker_exception_releases_its_preallocated_lease(self):
        with isolated_app():
            from fls_manager.directory_jobs import directory_job, start_directory_job

            queued = []

            def fail():
                self.assert_restore_blocked()
                raise RuntimeError("worker failed")

            with directory_job(), patch.object(threading.Thread, "start", autospec=True, side_effect=queued.append):
                start_directory_job(fail, name="file-job-failure")
            try:
                self.assert_restore_blocked()
            finally:
                with self.assertRaisesRegex(RuntimeError, "worker failed"):
                    queued[0].run()
            self.assert_restore_available()

    def test_response_close_keeps_restore_blocked_without_holding_storage_lock(self):
        with isolated_app() as (app, base):
            from fls_manager import directory_jobs, paths, storage
            module = self.module("pull")
            closing = threading.Event()
            release = threading.Event()
            workers = []
            response = pull_tests.FakeResponse([b"body"], 4)

            def close():
                closing.set()
                release.wait(10)
                response.closed = True

            def start(*args, **kwargs):
                worker = directory_jobs.start_directory_job(*args, **kwargs)
                workers.append(worker)
                return worker

            try:
                with patch.object(module, "requests", SimpleNamespace(get=lambda *args, **kwargs: response)), \
                        patch.object(response, "close", side_effect=close), \
                        patch.object(module, "start_directory_job", side_effect=start):
                    accepted = self.submit(app.test_client(), "pull")
                    self.assertEqual(accepted.status_code, 202)
                    self.assertTrue(closing.wait(3))
                    self.assert_restore_blocked()
                    saved = threading.Event()

                    def save_config():
                        storage.write_json(paths.DATA_DIR / "unrelated.json", {"ok": True})
                        saved.set()

                    writer = threading.Thread(target=save_config)
                    writer.start()
                    try:
                        self.assertTrue(saved.wait(3), "file job held the storage lock during response cleanup")
                    finally:
                        release.set()
                        writer.join(5)
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
            self.assertTrue(response.closed)
            self.assert_restore_available()

    def test_archive_cancel_holds_lease_until_stage_and_upload_cleanup(self):
        with isolated_app() as (app, base):
            from fls_manager import directory_jobs
            from tests.test_native_script_import_api import zip_bytes

            module = self.module("import")
            extracted = threading.Event()
            release = threading.Event()
            workers = []

            def extract(_source, destination, cancel, _progress):
                (destination / "partial.py").write_bytes(b"partial")
                extracted.set()
                release.wait(10)
                module._check_cancel(cancel)

            def start(*args, **kwargs):
                worker = directory_jobs.start_directory_job(*args, **kwargs)
                workers.append(worker)
                return worker

            client = app.test_client()
            try:
                with patch.object(module, "_extract_zip", side_effect=extract), \
                        patch.object(module, "start_directory_job", side_effect=start):
                    accepted = client.post("/api/scripts/import", data={
                        "file": (io.BytesIO(zip_bytes({"file.py": b"body"})), "test.zip"),
                        "path": "bundle", "request_id": "a" * 32,
                    }, headers={"X-Token": TOKEN})
                    self.assertEqual(accepted.status_code, 202, accepted.json)
                    self.assertTrue(extracted.wait(3))
                    self.assert_restore_blocked()
                    cancelled = client.post("/api/scripts/import/" + accepted.json["job"]["id"] + "/cancel",
                                            headers={"X-Token": TOKEN})
                    self.assertEqual(cancelled.status_code, 202, cancelled.json)
                    self.assert_restore_blocked()
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
            self.assertEqual(list((base / "scripts").iterdir()), [])
            self.assertEqual(list((base / "data").glob(".script-import-*")), [])
            result = client.get("/api/scripts/import/" + accepted.json["job"]["id"], headers={"X-Token": TOKEN})
            self.assertEqual(result.json["job"]["status"], "cancelled")
            self.assert_restore_available()

    def test_git_failure_waits_for_stderr_reader_before_removing_stage_or_releasing_lease(self):
        with isolated_app() as (app, base):
            from fls_manager import directory_jobs

            module = self.module("repo-pull")
            reader_stopping = threading.Event()
            release = threading.Event()
            workers = []
            drain = module._drain_stderr
            update = module._update

            def slow_reader(stream, output):
                drain(stream, output)
                reader_stopping.set()
                release.wait(10)

            def fail_after_spawn(job_id, **updates):
                if updates.get("status") == "cloning":
                    raise module._RepoPersistenceError()
                return update(job_id, **updates)

            def start(*args, **kwargs):
                worker = directory_jobs.start_directory_job(*args, **kwargs)
                workers.append(worker)
                return worker

            try:
                with patch.object(module, "build_repo_pull_command", return_value=[
                    sys.executable, "-c", "import time;time.sleep(10)",
                ]), patch.object(module, "_drain_stderr", side_effect=slow_reader), \
                        patch.object(module, "_update", side_effect=fail_after_spawn), \
                        patch.object(module, "start_directory_job", side_effect=start):
                    accepted = self.submit(app.test_client(), "repo-pull")
                    self.assertEqual(accepted.status_code, 202, accepted.json)
                    self.assertTrue(reader_stopping.wait(5))
                    self.assertTrue(workers[0].is_alive())
                    self.assert_restore_blocked()
                    self.assertTrue(list((base / "scripts").glob(".fls-repo-*.tmp")))
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
            self.assertEqual(list((base / "scripts").iterdir()), [])
            result = app.test_client().get("/api/scripts/repo-pull/" + accepted.json["job"]["id"],
                                           headers={"X-Token": TOKEN})
            self.assertEqual(result.json["job"]["error_code"], "job_persistence_failed")
            self.assert_restore_available()

    def test_independent_file_job_excludes_restore_and_hard_exit_releases_lease(self):
        with isolated_app():
            with activity_tests.BackupActivityLeaseTests().holder("file_job", hard_exit=True) as process:
                self.assert_restore_blocked()
                process.stdin.write("\n")
                process.stdin.flush()
                process.wait(timeout=10)
                self.assert_restore_available()

    def test_readonly_git_stage_is_removed_without_touching_replaced_directory(self):
        with isolated_app() as (_, base):
            module = self.module("repo-pull")
            target = base / "scripts" / "repo"
            stage = target.parent / ".fls-repo-test.tmp"
            stage.mkdir()
            item = stage / "readonly-object"
            item.write_bytes(b"object")
            item.chmod(stat.S_IREAD)
            identity = stage.stat()
            expected = (identity.st_dev, identity.st_ino)
            try:
                self.assertFalse(module._remove_entry(target, None, stage.name, (expected[0], expected[1] + 1)))
                self.assertTrue(item.exists())
                self.assertTrue(module._remove_entry(target, None, stage.name, expected))
                self.assertFalse(stage.exists())
            finally:
                if item.exists():
                    item.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_failed_git_stage_cleanup_reports_unknown_instead_of_complete(self):
        with isolated_app() as (app, base), self.fake_network("repo-pull", base):
            module = self.module("repo-pull")
            queued = []
            with patch.object(threading.Thread, "start", autospec=True, side_effect=queued.append):
                accepted = self.submit(app.test_client(), "repo-pull")
            try:
                with patch.object(module, "_remove_entry", return_value=False):
                    queued[0].run()
                result = app.test_client().get("/api/scripts/repo-pull/" + accepted.json["job"]["id"],
                                               headers={"X-Token": TOKEN})
                self.assertEqual(result.json["job"]["status"], "unknown", result.json)
                self.assertEqual(result.json["job"]["error_code"], "cleanup_failed")
            finally:
                stage = module._stage_name(accepted.json["job"]["id"])
                self.assertTrue(module._remove_entry(base / "scripts" / "repo", None, stage))
            self.assert_restore_available()
