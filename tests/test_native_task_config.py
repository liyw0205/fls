import json
import os
import stat
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import test_task_config_recovery as config_tests
from tests import test_script_directory_access as directory_tests
from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class NativeTaskConfigTests(unittest.TestCase):
    path = "/api/tasks/config/config-file"
    headers = {"X-Token": TOKEN}

    def prepare(self, base, content="key: original\n"):
        return config_tests.TaskConfigRecoveryTests().prepare(base, content=content)

    def snapshot(self, client):
        response = client.get(self.path, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.json["file"]

    def data(self, snapshot, content="new: secret\r\n", request_id="a" * 32):
        return {"request_id": request_id, "path": snapshot["path"], "expected_revision": snapshot["revision"], "content": content}

    def query(self, client, request_id="a" * 32):
        return client.get(self.path + "/requests/" + request_id, headers=self.headers)

    def test_native_session_csrf_missing_file_and_exact_save_without_execution(self):
        with isolated_app() as (app, base):
            from fls_manager.native_sessions import create_native_session

            target = self.prepare(base, content=None)
            client = app.test_client()
            self.assertEqual(client.get(self.path).status_code, 401)
            with app.app_context():
                session, csrf, _ = create_native_session(TOKEN, remember=True, verified=True)
            client.set_cookie("fls_native_session", session)
            file = client.get(self.path).json["file"]
            self.assertFalse(file["exists"])
            self.assertEqual(file["content"], "")
            self.assertFalse(target.exists())
            self.assertFalse((base / ".task_config_requests").exists())
            body = self.data(file)
            self.assertEqual(client.post(self.path, json=body).status_code, 400)
            response = client.post(self.path, json=body, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(response.json["status"], "saved")
            self.assertEqual(target.read_bytes(), b"new: secret\r\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(list((base / "log").glob("config_*")), [])

    def test_durable_receipt_has_no_content_and_replay_never_publishes_again(self):
        with isolated_app() as (app, base):
            target = self.prepare(base)
            target.chmod(0o640)
            expected_mode = stat.S_IMODE(target.stat().st_mode)
            client = app.test_client()
            body = self.data(self.snapshot(client))
            response = client.post(self.path, json=body, headers=self.headers)
            self.assertTrue(response.json["saved"])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), expected_mode)
            receipt = base / ".task_config_requests" / (body["request_id"] + ".json")
            raw = receipt.read_text(encoding="utf-8")
            self.assertNotIn("secret", raw)
            self.assertNotIn("content", raw)
            self.assertNotIn("expected_revision", raw)
            original = receipt.read_bytes(), receipt.stat().st_mtime_ns
            target.write_bytes(b"later change\n")
            with patch("fls_manager.routes.tasks.config_file._save_config_atomic", side_effect=AssertionError("replayed")):
                replay = client.post(self.path, json=body, headers=self.headers)
                query = self.query(client)
            self.assertEqual(replay.json["status"], "saved")
            self.assertEqual(query.json["status"], "saved")
            self.assertEqual(target.read_bytes(), b"later change\n")
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_mtime_ns), original)
            mismatch = client.post(self.path, json={**body, "content": "other"}, headers=self.headers)
            self.assertEqual(mismatch.status_code, 409)
            self.assertEqual(mismatch.json["code"], "request_mismatch")

    def test_task_binding_conflict_and_pending_rename_never_write_either_path(self):
        with isolated_app() as (app, base):
            from fls_manager.models import load_tasks, save_tasks

            target = self.prepare(base)
            client = app.test_client()
            body = self.data(self.snapshot(client))
            other = target.with_name("other.yml")
            other.write_bytes(target.read_bytes())
            tasks = load_tasks()
            tasks[0]["config_path"] = "conf/other.yml"
            save_tasks(tasks)
            response = client.post(self.path, json=body, headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.json["saved"])
            self.assertEqual(response.json["current"]["path"], "conf/other.yml")
            self.assertEqual(target.read_bytes(), other.read_bytes())
            self.assertFalse((base / ".task_config_requests").exists())
            with patch("fls_manager.routes.tasks.native_config_file.pending_script_rename", return_value={"id": "r"}):
                self.assertEqual(client.get(self.path, headers=self.headers).json["code"], "rename_pending")
                self.assertEqual(client.post(self.path, json=body, headers=self.headers).status_code, 409)

    def test_parallel_forms_share_one_success_and_one_conflict_and_old_web_still_works(self):
        with isolated_app() as (app, base):
            target = self.prepare(base)
            client = app.test_client()
            file = self.snapshot(client)
            barrier = threading.Barrier(2)

            def post(number):
                barrier.wait(5)
                return app.test_client().post(self.path, json=self.data(file, str(number), str(number) * 32), headers=self.headers)

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(post, (1, 2)))
            self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
            loser = next(response for response in responses if response.status_code == 409)
            self.assertEqual(loser.json["current"]["content"], target.read_text())
            form = config_tests.TaskConfigRecoveryTests().form(client)
            response = client.post("/task/config/config", data={**form.fields, "content": "web\r\n"}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(target.read_bytes(), b"web\r\n")

    def test_prepared_and_missing_queries_are_read_only_and_never_repeat_save(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.tasks import native_config_file as native

            target = self.prepare(base)
            client = app.test_client()
            body = self.data(self.snapshot(client))
            self.assertEqual(self.query(client).json["status"], "missing")
            self.assertFalse(native.REQUESTS_DIR.exists())
            with patch("fls_manager.routes.tasks.config_file._save_config_atomic", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    client.post(self.path, json=body, headers=self.headers)
            receipt = native.REQUESTS_DIR / (body["request_id"] + ".json")
            original = receipt.read_bytes(), receipt.stat().st_mtime_ns
            with patch("fls_manager.routes.tasks.config_file._save_config_atomic", side_effect=AssertionError("unexpected save")):
                for _ in range(2):
                    self.assertEqual(self.query(client).json["status"], "unknown")
                    replay = client.post(self.path, json=body, headers=self.headers)
                    self.assertIsNone(replay.json["saved"])
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_mtime_ns), original)
            self.assertEqual(target.read_bytes(), b"key: original\n")

    def test_failure_before_publish_is_definite_but_lost_terminal_receipt_is_unknown(self):
        for stage in ("publish", "ack"):
            with self.subTest(stage=stage), isolated_app() as (app, base):
                from fls_manager.routes.tasks import native_config_file as native

                target = self.prepare(base)
                client = app.test_client()
                body = self.data(self.snapshot(client))
                if stage == "publish":
                    context = patch("fls_manager.routes.tasks.config_file._save_config_atomic", side_effect=OSError("secret error"))
                else:
                    original = native._write_receipt

                    def fail_ack(row):
                        if row["state"] == "saved":
                            raise OSError("secret error")
                        return original(row)

                    context = patch.object(native, "_write_receipt", side_effect=fail_ack)
                with context:
                    response = client.post(self.path, json=body, headers=self.headers)
                self.assertNotIn("secret error", response.get_data(as_text=True))
                self.assertEqual(self.query(client).json["status"], "not_saved" if stage == "publish" else "unknown")
                self.assertEqual(target.read_bytes(), b"key: original\n" if stage == "publish" else b"new: secret\r\n")

    def test_restore_remains_excluded_until_terminal_receipt_is_durable(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.tasks import native_config_file as native

            self.prepare(base)
            client = app.test_client()
            body = self.data(self.snapshot(client))
            helper = directory_tests.ScriptDirectoryAccessTests()
            original = native._write_receipt
            observed = []

            def check(row):
                helper.assert_restore_blocked()
                observed.append(row["state"])
                return original(row)

            with patch.object(native, "_write_receipt", side_effect=check):
                response = client.post(self.path, json=body, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(observed, ["prepared", "saved"])
            helper.assert_restore_available()

    def test_real_worker_crash_before_and_after_publication_stays_unknown_without_replay(self):
        for stage in ("before", "after"):
            with self.subTest(stage=stage), isolated_app() as (app, base):
                target = self.prepare(base)
                client = app.test_client()
                body = self.data(self.snapshot(client))
                (base / "input.json").write_text(json.dumps(body), encoding="utf-8")
                code = """
import json, os, pathlib, sys
from fls_manager.app import create_app
from fls_manager.routes.tasks import config_file, native_config_file as native
base = pathlib.Path(os.environ['FLS_BASE_DIR'])
original = native._write_receipt
def ack(row):
    if row['state'] == 'saved': os._exit(24)
    return original(row)
def before(*args): os._exit(23)
if sys.argv[1] == 'before': config_file._save_config_atomic = before
else: native._write_receipt = ack
create_app().test_client().post('/api/tasks/config/config-file', json=json.loads((base / 'input.json').read_text(encoding='utf-8')), headers={'X-Token': os.environ['FLS_TOKEN']})
"""
                completed = subprocess.run([sys.executable, "-c", code, stage], cwd=ROOT, capture_output=True, timeout=25)
                self.assertEqual(completed.returncode, 23 if stage == "before" else 24, completed.stderr)
                self.assertEqual(self.query(client).json["status"], "unknown")
                with patch("fls_manager.routes.tasks.config_file._save_config_atomic", side_effect=AssertionError("crash replay")):
                    response = client.post(self.path, json=body, headers=self.headers)
                self.assertEqual(response.json["status"], "unknown")
                self.assertEqual(target.read_bytes(), b"key: original\n" if stage == "before" else b"new: secret\r\n")

    def test_restore_busy_invalid_json_utf8_limits_and_bad_receipts_fail_closed(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.tasks import native_config_file as native, config_file

            target = self.prepare(base)
            client = app.test_client()
            body = self.data(self.snapshot(client))
            helper = directory_tests.ScriptDirectoryAccessTests()
            for mode in ("legacy", "native", "journal", "io"):
                with self.subTest(mode=mode), helper.blocked(mode, base):
                    for method, suffix, args in (("get", "", {}), ("post", "", {"json": body}), ("get", "/requests/" + "a" * 32, {})):
                        response = getattr(client, method)(self.path + suffix, headers=self.headers, **args)
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(response.json["code"], "panel_storage_busy")
            for data in ("[]", '{"content":"a","content":"b"}', '{"content":NaN}'):
                self.assertEqual(client.post(self.path, data=data, content_type="application/json", headers=self.headers).status_code, 400)
            with patch.object(config_file, "MAX_CONFIG_BYTES", 10):
                response = client.post(self.path, json={**body, "content": "\u4e2d" * 4}, headers=self.headers)
                self.assertEqual(response.status_code, 413)
            target.write_bytes(b"\xff")
            self.assertEqual(client.get(self.path, headers=self.headers).status_code, 415)
            native.REQUESTS_DIR.mkdir()
            receipt = native.REQUESTS_DIR / (body["request_id"] + ".json")
            receipt.write_text('{"state":"saved"}', encoding="utf-8")
            self.assertEqual(self.query(client).status_code, 503)
            self.assertEqual(client.post(self.path, json=body, headers=self.headers).status_code, 503)

    def test_separate_worker_serializes_path_rebinding(self):
        with isolated_app() as (app, base):
            self.prepare(base)
            client = app.test_client()
            body = self.data(self.snapshot(client))
            body_path = base / "input.json"
            body_path.write_text(json.dumps(body), encoding="utf-8")
            code = """
import json, os, pathlib, sys, time
from fls_manager.app import create_app
from fls_manager.routes.tasks import config_file
base = pathlib.Path(os.environ['FLS_BASE_DIR'])
original = config_file._save_config_atomic
def pause(*args):
    (base / 'ready').touch()
    deadline = time.monotonic() + 30
    while not (base / 'release').exists():
        if time.monotonic() > deadline: raise RuntimeError('worker not released')
        time.sleep(.02)
    return original(*args)
config_file._save_config_atomic = pause
response = create_app().test_client().post('/api/tasks/config/config-file', json=json.loads((base / 'input.json').read_text(encoding='utf-8')), headers={'X-Token': os.environ['FLS_TOKEN']})
assert response.status_code == 200, response.get_data(as_text=True)
"""
            proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            failures = []
            worker = None
            started, finished = threading.Event(), threading.Event()

            def rebind():
                try:
                    from fls_manager.models import mutate_tasks

                    started.set()
                    def change(tasks):
                        tasks[0]["config_path"] = "conf/other.yml"
                        return True
                    mutate_tasks(change)
                    finished.set()
                except BaseException as exc:
                    failures.append(exc)

            try:
                deadline = time.monotonic() + 20
                while not (base / "ready").exists():
                    if proc.poll() is not None or time.monotonic() > deadline:
                        self.fail("worker did not enter publication: " + str(proc.communicate(timeout=2)))
                    time.sleep(.02)
                worker = threading.Thread(target=rebind)
                worker.start()
                self.assertTrue(started.wait(5))
                self.assertFalse(finished.wait(.2))
                (base / "release").touch()
                stdout, stderr = proc.communicate(timeout=20)
                worker.join(10)
                self.assertEqual(proc.returncode, 0, (stdout, stderr))
                self.assertEqual(failures, [])
                self.assertTrue(finished.is_set())
                self.assertEqual((base / "scripts/conf/app.yml").read_bytes(), b"new: secret\r\n")
                self.assertFalse((base / "scripts/conf/other.yml").exists())
            finally:
                (base / "release").touch()
                if worker is not None:
                    worker.join(10)
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=10)
