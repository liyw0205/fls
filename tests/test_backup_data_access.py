import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app
from tests import test_native_backup_api as native_tests


class BackupDataAccessTests(unittest.TestCase):
    @contextlib.contextmanager
    def paused_process(self, body):
        script = (
            "import json,sys\n"
            "from fls_manager import models, storage, native_sessions as sessions\n"
            "from fls_manager import security, instance_lock\n"
            "from fls_manager.paths import DATA_DIR\n"
            "from fls_manager.data_access import directory_access,PanelStorageBusy\n"
            + body
        )
        with tempfile.TemporaryFile(mode="w+b") as output, tempfile.TemporaryFile(mode="w+b") as errors:
            process = subprocess.Popen(
                [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=output, stderr=errors,
                text=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            )
            try:
                deadline = time.monotonic() + 15
                while process.poll() is None and time.monotonic() < deadline:
                    output.seek(0)
                    if b"ready\n" in output.read().replace(b"\r\n", b"\n"):
                        break
                    time.sleep(0.01)
                else:
                    errors.seek(0)
                    self.fail("worker did not reach barrier: " + errors.read().decode(errors="replace"))
                yield process, output, errors
                if process.poll() is None:
                    process.communicate("\n", timeout=15)
                errors.seek(0)
                self.assertEqual(process.returncode, 0, errors.read().decode(errors="replace"))
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)

    def test_live_connections_and_storage_transaction_exclude_directory_replacement(self):
        for kind in ("json", "session", "history"):
            with self.subTest(kind=kind), isolated_app() as (_, base):
                from fls_manager import models, paths, storage
                from fls_manager.data_access import directory_access, PanelStorageBusy

                storage.write_json(paths.TASK_HISTORY_FILE, [{"id": "original", "status": "success"}])
                if kind == "json":
                    body = (
                        "with storage.storage_transaction():\n"
                        " print('ready',flush=True)\n sys.stdin.readline()\n"
                        " storage.write_json(DATA_DIR/'background.json',{'saved':True})\n"
                    )
                elif kind == "session":
                    body = (
                        "with sessions._database() as connection:\n"
                        " print('ready',flush=True)\n sys.stdin.readline()\n"
                        " connection.execute('SELECT COUNT(*) FROM native_sessions').fetchone()\n"
                        "try: connection.execute('SELECT 1')\n"
                        "except Exception as exc: assert type(exc).__name__=='ProgrammingError'\n"
                        "else: raise AssertionError('session connection leaked')\n"
                    )
                else:
                    body = (
                        "open_index=models._open_current_task_history_index\nconnections=[]\n"
                        "def paused_index():\n"
                        " connection=open_index()\n assert connection is not None\n connections.append(connection)\n"
                        " print('ready',flush=True)\n sys.stdin.readline()\n return connection\n"
                        "models._open_current_task_history_index=paused_index\n"
                        "rows,total=models.query_task_history(limit=10)\nassert rows[0]['id']=='original'\n"
                        "try: connections[0].execute('SELECT 1')\n"
                        "except Exception as exc: assert type(exc).__name__=='ProgrammingError'\n"
                        "else: raise AssertionError('history connection leaked')\n"
                    )
                with self.paused_process(body):
                    with self.assertRaises(PanelStorageBusy):
                        with directory_access(exclusive=True):
                            self.fail("entered replacement with a live database/transaction")
                with directory_access(exclusive=True):
                    self.assertEqual(models.query_task_history(limit=10)[0][0]["id"], "original")
                if kind == "json":
                    self.assertTrue(json.loads((base / "data/background.json").read_text())["saved"])

    def test_existing_worker_cannot_recreate_a_directory_during_replacement(self):
        with isolated_app() as (_, base):
            from fls_manager import paths, storage
            from fls_manager.data_access import directory_access

            storage.write_json(paths.CONFIG_FILE, {"original": True})
            body = (
                "print('ready',flush=True)\nsys.stdin.readline()\n"
                "actions=[lambda:storage.read_json(DATA_DIR/'config.json',{}),\n"
                " lambda:storage.write_json(DATA_DIR/'background.json',{}),\n"
                " lambda:models.query_task_history(limit=10),\n"
                " lambda:sessions.delete_all_native_sessions(),\n"
                " lambda:instance_lock.acquire_instance_lock(),\n"
                " lambda:security.save_random_code('123456'),\n"
                " lambda:security.load_random_code(),\n"
                " lambda:security.clear_random_code()]\n"
                "for action in actions:\n"
                " try: action()\n except PanelStorageBusy: pass\n"
                " else: raise AssertionError('background access entered replacement')\n"
                "assert not DATA_DIR.exists()\n"
            )
            with self.paused_process(body) as (process, _, _):
                with directory_access(exclusive=True):
                    previous = base / "original-data"
                    paths.DATA_DIR.rename(previous)
                    try:
                        process.communicate("\n", timeout=10)
                        self.assertFalse(paths.DATA_DIR.exists())
                        self.assertFalse((previous / "background.json").exists())
                    finally:
                        previous.rename(paths.DATA_DIR)
            self.assertTrue(storage.read_json(paths.CONFIG_FILE, {})["original"])

    def test_verification_code_files_remain_unchanged_while_another_worker_restores(self):
        with isolated_app() as (_, base):
            from fls_manager import security
            from fls_manager.data_access import PanelStorageBusy

            security.save_random_code("123456", ip="127.0.0.1", ua="test")
            before = security.FLS_CODE_FILE.read_bytes()
            body = "with directory_access(exclusive=True):\n print('ready',flush=True)\n sys.stdin.readline()\n"
            with self.paused_process(body):
                for action in (
                    lambda: security.save_random_code("654321"),
                    security.load_random_code,
                    lambda: security.random_code_valid("123456"),
                    security.clear_random_code,
                ):
                    with self.assertRaises(PanelStorageBusy):
                        action()
                    self.assertEqual(security.FLS_CODE_FILE.read_bytes(), before)
            self.assertTrue(security.random_code_valid("123456")[0])
            security.clear_random_code()
            self.assertEqual(security.load_random_code(), {})

    def test_git_probe_does_not_hold_or_recreate_live_data_during_restore(self):
        with isolated_app() as (_, base):
            from fls_manager import paths, proxy_checks
            from fls_manager.data_access import directory_access, PanelStorageBusy

            process = MagicMock(stdout=io.BytesIO(b"a" * 40 + b"\trefs/heads/main\n"), returncode=0)
            body = (
                "with directory_access(exclusive=True):\n"
                " DATA_DIR.rename(DATA_DIR.with_name('previous-data'))\n"
                " print('ready',flush=True)\n sys.stdin.readline()\n"
                " assert not DATA_DIR.exists()\n"
                " DATA_DIR.with_name('previous-data').rename(DATA_DIR)\n"
            )

            def spawn(*args, **kwargs):
                directory = Path(kwargs["cwd"])
                self.assertTrue(directory.is_dir())
                self.assertFalse(directory.is_relative_to(paths.DATA_DIR))
                self.assertEqual(kwargs["env"]["GIT_CEILING_DIRECTORIES"], str(directory))
                self.assertFalse(paths.DATA_DIR.exists())
                with self.assertRaises(PanelStorageBusy):
                    with directory_access(base, exclusive=True):
                        pass
                return process

            # A separate restore owns the lease; the probe does not enter data.
            with self.paused_process(body) as (worker, _, _):
                with patch.object(proxy_checks.shutil, "which", return_value="git"), \
                     patch.object(proxy_checks.subprocess, "Popen", side_effect=spawn) as popen:
                    result = proxy_checks._git_probe({"type": "github", "url": "https://proxy.example"})
                self.assertEqual(result["error_code"], "")
                self.assertFalse(paths.DATA_DIR.exists())
                self.assertFalse(Path(popen.call_args.kwargs["cwd"]).exists())
                worker.communicate("\n", timeout=10)

    def test_access_is_thread_local_reentrant_and_cannot_upgrade_shared_lease(self):
        with isolated_app():
            from fls_manager import storage, paths
            from fls_manager.data_access import directory_access, PanelStorageBusy

            with directory_access():
                with self.assertRaises(PanelStorageBusy):
                    with directory_access(exclusive=True):
                        pass
            with directory_access(exclusive=True):
                with storage.storage_transaction():
                    storage.write_json(paths.CONFIG_FILE, {"owner": True})
                    self.assertTrue(storage.read_json(paths.CONFIG_FILE, {})["owner"])
                result = []

                def other_thread():
                    try:
                        storage.write_json(paths.CONFIG_FILE, {"other": True})
                    except PanelStorageBusy:
                        result.append("busy")

                worker = threading.Thread(target=other_thread)
                worker.start()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result, ["busy"])
            self.assertTrue(storage.read_json(paths.CONFIG_FILE, {})["owner"])

    def test_session_auth_reports_busy_without_revoking_cookie_during_real_native_commit(self):
        with isolated_app() as (app, base):
            from fls_manager import native_sessions, storage, paths
            from fls_manager.data_access import PanelStorageBusy

            client = app.test_client()
            logged_in = client.post("/api/auth/session", json={"token": TOKEN})
            self.assertEqual(logged_in.status_code, 200, logged_in.json)
            csrf = logged_in.json["csrf_token"]
            session_before = native_sessions.NATIVE_SESSION_FILE.read_bytes()
            body = (
                "import io,threading\nfrom unittest.mock import patch\n"
                "from fls_manager.app import create_app\n"
                "from fls_manager import backup_commit\n"
                "from tests.test_native_backup_api import NativeBackupApiTests\n"
                "app=create_app()\nhelper=NativeBackupApiTests()\nclient=app.test_client()\n"
                "rename=backup_commit._rename\npaused=False\n"
                "def pause(source,target,identity):\n"
                " global paused\n rename(source,target,identity)\n"
                " if source==DATA_DIR and not paused:\n"
                "  paused=True\n  print('ready',flush=True)\n  sys.stdin.readline()\n"
                "with patch.object(threading.Thread,'start',autospec=True,side_effect=lambda thread:thread.run()):\n"
                " preflight=helper.submit_preflight(client,'b'*32)\n"
                " assert preflight.json['preflight']['status']=='ready',preflight.json\n"
                " with patch.object(backup_commit,'_rename',side_effect=pause):\n"
                "  restored=helper.submit_restore(client,'c'*32,'b'*32,preflight.json['preflight']['archive_sha256'])\n"
                " assert restored.status_code==200,restored.json\n"
                " assert restored.json['restore']['state']=='restored',restored.json\n"
            )
            with self.paused_process(body) as (process, _, _):
                self.assertFalse(paths.DATA_DIR.exists())
                with patch.object(native_sessions, "_connect") as connect:
                    for method, url, kwargs in (
                        ("get", "/api/status", {}),
                        ("post", "/api/auth/logout", {"headers": {"X-CSRF-Token": csrf}}),
                        ("post", "/api/auth/session", {"json": {"token": TOKEN}}),
                    ):
                        response = getattr(client, method)(url, **kwargs)
                        self.assertEqual(response.status_code, 503, response.json)
                        self.assertEqual(response.json["code"], "panel_storage_busy")
                        self.assertEqual(response.headers["Cache-Control"], "no-store")
                        self.assertNotIn("Set-Cookie", response.headers)
                    connect.assert_not_called()
                with self.assertRaises(PanelStorageBusy):
                    storage.write_json(paths.DATA_DIR / "background.json", {})
                self.assertFalse(paths.DATA_DIR.exists())
                process.communicate("\n", timeout=15)
            self.assertEqual(native_sessions.NATIVE_SESSION_FILE.read_bytes(), session_before)
            self.assertEqual(client.get("/api/status").status_code, 200)
            self.assertEqual(client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code, 200)

    def test_terminal_journal_allows_read_only_auth_and_recovery_but_blocks_writes(self):
        with isolated_app() as (app, base):
            from fls_manager import native_sessions, storage, paths
            from fls_manager.data_access import PanelStorageBusy

            client = app.test_client()
            logged_in = client.post("/api/auth/session", json={"token": TOKEN})
            self.assertEqual(logged_in.status_code, 200, logged_in.json)
            request_id = "a" * 32
            # Keep the real session database while constructing an interrupted commit.
            storage.write_json(paths.CONFIG_FILE, {})
            from fls_manager.routes.backup import restore, native_api
            source = base / "source/data"
            source.mkdir(parents=True)
            (source / "config.json").write_bytes(b'{}')
            entry = restore._stage_restore_directory(source, paths.DATA_DIR)
            record = {
                "version": 1, "request_id": request_id, "preflight_id": "b" * 32,
                "archive_sha256": "c" * 64, "archive_size_bytes": 12, "filename": "full.tar.gz",
                "restore_items": ["data"], "restore_dependencies": False,
                "state": "committing", "live_state_unchanged": False,
                "scheduler_state": "pending", "dependency_state": "not_requested",
                "created_at": 1, "updated_at": 2,
            }
            native_api._write_native_restore(record)
            restore._commit_restore_directories([entry], preserve_runtime_identity=True, request_id=request_id)
            before = native_sessions.NATIVE_SESSION_FILE.read_bytes()
            read = client.get("/api/backup/native/restores/" + request_id)
            self.assertEqual(read.status_code, 200, read.json)
            self.assertTrue(read.json["restore"]["directory_recovery_required"])
            with self.assertRaises(PanelStorageBusy):
                storage.write_json(paths.DATA_DIR / "background.json", {})
            response = client.post("/api/auth/logout", headers={"X-CSRF-Token": logged_in.json["csrf_token"]})
            self.assertEqual(response.status_code, 503, response.json)
            self.assertNotIn("Set-Cookie", response.headers)
            recovered = client.post("/api/backup/native/restores/" + request_id + "/recover-directories",
                                    json={"confirm": True}, headers={"X-CSRF-Token": logged_in.json["csrf_token"]})
            self.assertEqual(recovered.status_code, 200, recovered.json)
            self.assertFalse(recovered.json["restore"]["directory_recovery_required"])
            self.assertEqual(native_sessions.NATIVE_SESSION_FILE.read_bytes(), before)
            storage.write_json(paths.DATA_DIR / "background.json", {"after": True})

    def test_history_probe_and_final_connection_both_close(self):
        with isolated_app():
            from fls_manager import models, paths, storage

            storage.write_json(paths.TASK_HISTORY_FILE, [{"id": "original", "status": "success"}])
            models.query_task_history(limit=10)
            connections = []
            connect = sqlite3.connect

            def track(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connections.append(connection)
                return connection

            with patch.object(models.sqlite3, "connect", side_effect=track):
                models.query_task_history(limit=10)
            self.assertEqual(len(connections), 2)
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_invalid_session_is_not_revived_when_credentials_are_changed_back(self):
        with isolated_app() as (app, _):
            client = app.test_client()
            self.assertEqual(client.post("/api/auth/session", json={"token": TOKEN}).status_code, 200)
            with patch.dict(os.environ, FLS_TOKEN="rotated-token"):
                self.assertEqual(client.get("/api/status").status_code, 401)
            self.assertEqual(client.get("/api/status").status_code, 401)

    def test_restart_waits_for_original_request_confirmation_before_reconciling_runtime(self):
        with isolated_app() as (app, base):
            from fls_manager import backup_commit, storage, paths, instance_lock
            from fls_manager.routes.backup import restore, native_api
            from fls_manager.data_access import directory_access, PanelStorageBusy

            client = app.test_client()
            login = client.post("/api/auth/session", json={"token": TOKEN})
            self.assertEqual(login.status_code, 200, login.json)
            cookie = client.get_cookie("fls_native_session").value
            storage.write_json(paths.CONFIG_FILE, {})
            descriptor = instance_lock.acquire_instance_lock()
            self.assertIsNotNone(descriptor)
            instance_lock.release_instance_lock(descriptor)
            source = base / "source/data"
            source.mkdir(parents=True)
            (source / "config.json").write_bytes(b'{}')
            request_id = "a" * 32
            native_api._write_native_restore({
                "version": 1, "request_id": request_id, "preflight_id": "b" * 32,
                "archive_sha256": "c" * 64, "archive_size_bytes": 12, "filename": "full.tar.gz",
                "restore_items": ["data"], "restore_dependencies": False,
                "state": "committing", "live_state_unchanged": False,
                "scheduler_state": "pending", "dependency_state": "not_requested",
                "created_at": 1, "updated_at": 2,
            })
            entry = restore._stage_restore_directory(source, paths.DATA_DIR)
            rename = backup_commit._rename

            def interrupt(source, destination, identity):
                rename(source, destination, identity)
                raise KeyboardInterrupt("interrupted directory move")

            with patch.object(backup_commit, "_rename", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    restore._commit_restore_directories([entry], preserve_runtime_identity=True, request_id=request_id)
            self.assertFalse(paths.DATA_DIR.exists())
            with directory_access(exclusive=True, recovery=True):
                blocked_startup = subprocess.run(
                    [sys.executable, "-c", "from fls_manager.app import create_app; create_app()"],
                    capture_output=True, timeout=15,
                )
                self.assertNotEqual(blocked_startup.returncode, 0)
                self.assertIn(b"PanelStorageBusy", blocked_startup.stderr)
                self.assertFalse(paths.DATA_DIR.exists())
            script = textwrap.dedent(f"""
                import runpy
                from flask import Flask
                from unittest.mock import patch
                from fls_manager import scheduler

                manager = runpy.run_path({str(Path(__file__).resolve().parents[1] / 'fls-manager.py')!r})
                reload = scheduler.reload_scheduler
                attempts = []

                def reload_once_retried():
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise RuntimeError('startup reload interrupted')
                    return reload()

                def serve(app, **kwargs):
                    assert app.config['RESTORE_STARTUP_RECONCILIATION_PENDING']
                    assert app.config['RESTORE_STARTUP_RUNTIME_PENDING']
                    assert not attempts
                    client = app.test_client()
                    client.set_cookie('fls_native_session', {cookie!r})
                    response = client.get('/api/backup/native/restores/{request_id}')
                    assert response.status_code == 200, response.json
                    assert response.json['restore']['directory_recovery_required']
                    endpoint = '/api/backup/native/restores/{request_id}/recover-directories'
                    headers = {{'X-CSRF-Token': {login.json['csrf_token']!r}}}
                    response = client.post(endpoint, json={{'confirm': True}}, headers=headers)
                    assert response.status_code == 503, response.json
                    assert app.config['RESTORE_STARTUP_RECONCILIATION_PENDING']
                    assert app.config['RESTORE_STARTUP_RUNTIME_PENDING']
                    response = client.get('/api/backup/native/restores/{request_id}')
                    assert response.status_code == 200, response.json
                    assert response.json['restore']['directory_recovery_required']
                    response = client.post(endpoint, json={{'confirm': True}}, headers=headers)
                    assert response.status_code == 200, response.json
                    assert response.json['restore']['state'] == 'failed'
                    assert not app.config['RESTORE_STARTUP_RECONCILIATION_PENDING']
                    assert not app.config['RESTORE_STARTUP_RUNTIME_PENDING']
                    assert not response.json['restore']['directory_recovery_required']
                    assert len(attempts) == 2
                    assert client.get('/api/status').status_code == 200

                with patch.dict(manager['main'].__globals__, {{'_bootstrap_deps': lambda: None}}), \\
                     patch.object(Flask, 'run', serve), \\
                     patch.object(scheduler, 'reload_scheduler', side_effect=reload_once_retried):
                    result = manager['main']()
                    assert result is None, result
            """)
            completed = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=15,
                                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertEqual(completed.returncode, 0,
                             (completed.stdout + completed.stderr).decode(errors="replace"))
            self.assertEqual(backup_commit.pending(base), [])
            with directory_access(exclusive=True):
                storage.write_json(paths.DATA_DIR / "after-recovery.json", {})
