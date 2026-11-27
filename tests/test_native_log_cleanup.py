import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app


class NativeLogCleanupTests(unittest.TestCase):
    headers = {"X-Token": TOKEN}

    def log(self, base, name="one.log", group="Alpha"):
        path = base / "log" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"===== 启动任务: {group} =====\nhello\n", encoding="utf-8")
        return path

    def preview(self, client, path=None, **selection):
        if path is not None:
            from fls_manager.log_access import file_version
            selection = {"files": [{"filename": path.name, "version": file_version(path.lstat())}]}
        response = client.post("/api/logs/cleanup/preview", json=selection, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.json["preview"]

    def submit(self, client, preview, request_id=None):
        body = {"request_id": request_id or uuid.uuid4().hex,
                "preview_id": preview["preview_id"], "digest": preview["digest"]}
        return client.post("/api/logs/cleanup", json=body, headers=self.headers), body

    def test_confirmed_snapshot_does_not_expand_a_group_and_replay_never_deletes_a_replacement(self):
        with isolated_app() as (app, base):
            original = self.log(base)
            client = app.test_client()
            preview = self.preview(client, groups=["Alpha"])
            later = self.log(base, "later.log")
            response, body = self.submit(client, preview)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["request"]["counts"]["deleted"], 1)
            self.assertFalse(original.exists())
            self.assertTrue(later.exists())
            replacement = self.log(base)
            replay = client.post("/api/logs/cleanup", json=body, headers=self.headers)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replacement.exists())
            body["digest"] = "f" * 64
            self.assertEqual(client.post("/api/logs/cleanup", json=body, headers=self.headers).status_code, 409)

    def test_growth_same_metadata_replacement_and_parent_replacement_are_conflicts(self):
        for change in ("growth", "replacement", "parent"):
            with self.subTest(change=change), isolated_app() as (app, base):
                path = self.log(base)
                client = app.test_client()
                preview = self.preview(client, path)
                info = path.stat()
                if change == "growth":
                    with path.open("a") as stream:
                        stream.write("more\n")
                elif change == "replacement":
                    path.rename(path.with_suffix(".old"))
                    path.write_bytes(b"x" * info.st_size)
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                else:
                    path.parent.rename(base / "old-log")
                    path.parent.mkdir()
                    (base / "old-log" / path.name).rename(path)
                response, _ = self.submit(client, preview)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["request"]["items"][0]["status"], "conflict")
                self.assertTrue(path.exists())

    def test_active_writer_is_reported_and_kept_then_needs_a_new_preview(self):
        with isolated_app() as (app, base):
            from fls_manager.log_access import open_log_file
            path = self.log(base)
            client = app.test_client()
            with open_log_file(path, "ab"):
                preview = self.preview(client, path)
                self.assertEqual(preview["items"][0]["status"], "busy")
                response, _ = self.submit(client, preview)
                self.assertEqual(response.json["request"]["items"][0]["status"], "busy")
            self.assertTrue(path.exists())
            response, _ = self.submit(client, self.preview(client, path))
            self.assertEqual(response.json["request"]["counts"]["deleted"], 1)

    def test_partial_results_keep_individual_busy_changed_and_missing_files_separate(self):
        with isolated_app() as (app, base):
            from fls_manager.log_access import file_version, open_log_file
            paths = [self.log(base, name + ".log") for name in ("a", "b", "c", "d")]
            client = app.test_client()
            preview = self.preview(client, files=[{"filename": path.name, "version": file_version(path.lstat())}
                                                   for path in paths])
            paths[2].unlink()
            paths[3].write_text("changed", encoding="ascii")
            with open_log_file(paths[1], "ab"):
                response, _ = self.submit(client, preview)
            result = response.json["request"]
            self.assertEqual([item["status"] for item in result["items"]], ["deleted", "busy", "missing", "conflict"])
            self.assertEqual(result["counts"], dict(deleted=1, busy=1, missing=1, conflict=1, failed=0, unknown=0))
            self.assertFalse(paths[0].exists())
            self.assertTrue(paths[1].exists())
            self.assertEqual(paths[3].read_text(), "changed")

    def test_file_limit_expired_preview_retirement_and_body_bytes_are_bounded(self):
        with isolated_app() as (app, base):
            from fls_manager import log_cleanup
            path = self.log(base)
            client = app.test_client()
            preview = self.preview(client, path)
            with patch.object(log_cleanup, "MAX_PREVIEWS", 1):
                rejected = client.post("/api/logs/cleanup/preview", json={"groups": ["Alpha"]}, headers=self.headers)
                self.assertEqual(rejected.status_code, 409)
                with patch.object(log_cleanup.time, "time", return_value=preview["expires_at"] + 1):
                    self.assertNotEqual(self.preview(client, path)["preview_id"], preview["preview_id"])
            for i in range(199):
                self.log(base, f"{i}.log")
            self.assertEqual(self.preview(client, groups=["Alpha"])["total_files"], 200)
            too_large = client.post("/api/logs/cleanup/preview", data=b" " * (384 * 1024 + 1),
                                    content_type="application/json", headers=self.headers)
            self.assertEqual(too_large.status_code, 413)

    def test_expiry_capacity_and_strict_json_reject_without_unlink(self):
        with isolated_app() as (app, base):
            path = self.log(base)
            client = app.test_client()
            for selection in ({}, {"groups": []}, {"groups": ["Alpha"] * 2},
                              {"groups": [str(i) for i in range(21)]},
                              {"files": [{"filename": "../one.log", "version": "a" * 64}]},
                              {"files": [], "groups": ["Alpha"]}, {"groups": ["Alpha"], "extra": 1}):
                with self.subTest(selection=selection):
                    self.assertIn(client.post("/api/logs/cleanup/preview", json=selection,
                                              headers=self.headers).status_code, (400, 413))
            duplicate = client.post("/api/logs/cleanup/preview", data='{"groups":["Alpha"],"groups":["Beta"]}',
                                    content_type="application/json", headers=self.headers)
            self.assertEqual(duplicate.status_code, 400)
            preview = self.preview(client, path)
            from fls_manager import log_cleanup
            with patch.object(log_cleanup.time, "time", return_value=preview["expires_at"] + 1):
                response, _ = self.submit(client, preview)
            self.assertEqual(response.status_code, 409)
            self.assertTrue(path.exists())
            for i in range(200):
                self.log(base, f"extra-{i}.log")
            self.assertEqual(client.post("/api/logs/cleanup/preview", json={"groups": ["Alpha"]},
                                         headers=self.headers).status_code, 413)

    def test_query_and_capabilities_are_read_only_and_unknown_reconcile_never_unlinks(self):
        with isolated_app() as (app, base):
            path = self.log(base)
            client = app.test_client()
            capabilities = client.get("/api/logs/cleanup/capabilities", headers=self.headers)
            self.assertEqual(capabilities.status_code, 200)
            self.assertEqual(capabilities.json["capabilities"]["max_files"], 200)
            self.assertFalse((base / ".log_cleanup").exists())
            from fls_manager import log_cleanup
            preview = self.preview(client, path)
            real_write = log_cleanup._write

            def fail_final(kind, row):
                if kind == "request" and row["items"][0]["status"] == "deleted":
                    raise OSError("injected final save failure")
                return real_write(kind, row)

            with patch.object(log_cleanup, "_write", side_effect=fail_final):
                response, body = self.submit(client, preview)
            self.assertEqual(response.status_code, 503)
            self.assertFalse(path.exists())
            before = {p.name: p.read_bytes() for p in (base / ".log_cleanup").iterdir() if p.is_file()}
            request_url = "/api/logs/cleanup/requests/" + body["request_id"]
            with patch.object(log_cleanup, "_write", side_effect=AssertionError("GET wrote")):
                query = client.get(request_url, headers=self.headers)
            self.assertEqual(query.json["request"]["items"][0]["status"], "unknown")
            self.assertEqual(before, {p.name: p.read_bytes() for p in (base / ".log_cleanup").iterdir() if p.is_file()})
            replacement = self.log(base)
            with patch("fls_manager.log_access.LogAccess.unlink", side_effect=AssertionError("reconcile deleted")):
                resolved = client.post(request_url + "/reconcile", json={}, headers=self.headers)
                replay = client.post("/api/logs/cleanup", json=body, headers=self.headers)
            self.assertEqual(resolved.json["request"]["items"][0]["status"], "conflict")
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replacement.exists())

    def test_initial_and_intent_save_failure_do_not_delete_and_corrupt_receipts_are_not_replaced(self):
        for stage in ("initial", "intent", "corrupt"):
            with self.subTest(stage=stage), isolated_app() as (app, base):
                from fls_manager import log_cleanup
                path = self.log(base)
                client = app.test_client()
                preview = self.preview(client, path)
                real_write = log_cleanup._write

                def failure(kind, row):
                    if kind == "request" and (stage == "initial" or row["items"][0]["status"] == "deleting"):
                        raise OSError("injected persistence failure")
                    return real_write(kind, row)

                request_id = uuid.uuid4().hex
                if stage == "corrupt":
                    target = base / ".log_cleanup" / ("request-" + request_id + ".json")
                    target.write_text("broken", encoding="ascii")
                    response, _ = self.submit(client, preview, request_id)
                    self.assertEqual(target.read_text(), "broken")
                else:
                    with patch.object(log_cleanup, "_write", side_effect=failure):
                        response, _ = self.submit(client, preview, request_id)
                self.assertEqual(response.status_code, 503)
                self.assertTrue(path.exists())

    def test_missing_reconcile_is_observation_and_unknown_same_file_stays_unknown(self):
        for remove in (True, False):
            with self.subTest(remove=remove), isolated_app() as (app, base):
                from fls_manager import log_cleanup
                path = self.log(base)
                client = app.test_client()
                preview = self.preview(client, path)
                with patch("fls_manager.log_access.LogAccess.unlink", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.submit(client, preview, "1" * 32)
                if remove:
                    path.unlink()
                response = client.post("/api/logs/cleanup/requests/" + "1" * 32 + "/reconcile",
                                       json={}, headers=self.headers)
                self.assertEqual(response.json["request"]["items"][0]["status"], "missing" if remove else "unknown")

    def test_session_requires_csrf_and_denied_requests_do_not_create_cleanup_state(self):
        with isolated_app() as (app, base):
            client = app.test_client()
            self.assertEqual(client.get("/api/logs/cleanup/capabilities").status_code, 401)
            login = client.post("/api/auth/session", json={"token": TOKEN})
            self.assertEqual(login.status_code, 200)
            response = client.post("/api/logs/cleanup/preview", json={"groups": ["Alpha"]})
            self.assertEqual(response.status_code, 400)
            self.assertIn("CSRF", response.json["msg"])
            self.assertFalse((base / ".log_cleanup").exists())

    def test_replacement_while_intent_is_saved_is_checked_again_before_unlink(self):
        with isolated_app() as (app, base):
            from fls_manager import log_cleanup
            path = self.log(base)
            client = app.test_client()
            preview = self.preview(client, path)
            real_write = log_cleanup._write

            def replace(kind, row):
                real_write(kind, row)
                if kind == "request" and row["items"][0]["status"] == "deleting":
                    path.rename(path.with_suffix(".old"))
                    path.write_text("replacement", encoding="ascii")

            with patch.object(log_cleanup, "_write", side_effect=replace):
                response, _ = self.submit(client, preview)
            self.assertEqual(response.json["request"]["items"][0]["status"], "conflict")
            self.assertEqual(path.read_text(), "replacement")

    def test_corrupt_signed_snapshot_link_special_file_and_receipt_capacity_fail_closed(self):
        with isolated_app() as (app, base):
            from fls_manager import log_cleanup
            path = self.log(base)
            client = app.test_client()
            preview = self.preview(client, path)
            target = base / ".log_cleanup" / ("preview-" + preview["preview_id"] + ".json")
            row = json.loads(target.read_text())
            row["items"][0]["filename"] = "outside.log"
            original = target.read_bytes()
            target.write_text(json.dumps(row), encoding="ascii")
            response, _ = self.submit(client, preview)
            self.assertEqual(response.status_code, 503)
            self.assertTrue(path.exists())
            target.write_bytes(original)
            with patch.object(log_cleanup, "MAX_REQUESTS", 0):
                response, _ = self.submit(client, preview)
            self.assertEqual(response.status_code, 409)
            path.unlink()
            path.mkdir()
            self.assertEqual(client.post("/api/logs/cleanup/preview",
                json={"files": [{"filename": path.name, "version": "a" * 64}]},
                headers=self.headers).status_code, 409)

    def test_real_worker_killed_after_durable_intent_leaves_unknown_and_cannot_repeat(self):
        with isolated_app() as (app, base):
            path = self.log(base)
            client = app.test_client()
            preview = self.preview(client, path)
            body = {"request_id": "2" * 32, "preview_id": preview["preview_id"], "digest": preview["digest"]}
            ready = base / "intent-ready"
            code = (
                "import json,os,time\nfrom pathlib import Path\nfrom unittest.mock import patch\n"
                "from fls_manager.app import create_app\n"
                "from fls_manager.log_access import LogAccess\n"
                "def hold(self):\n Path(os.environ['CLEANUP_READY']).write_text('ready')\n"
                " while True: time.sleep(0.02)\n"
                "with patch('fls_manager.app.dispatch_frontend_open_event'):\n app=create_app()\n"
                "with patch.object(LogAccess,'unlink',hold):\n"
                " app.test_client().post('/api/logs/cleanup',json=json.loads(os.environ['CLEANUP_BODY']),"
                "headers={'X-Token':os.environ['FLS_TOKEN']})\n"
            )
            environment = {**os.environ, "CLEANUP_READY": str(ready), "CLEANUP_BODY": json.dumps(body)}
            process = subprocess.Popen([sys.executable, "-u", "-c", code], env=environment,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "independent worker did not reach durable deletion intent")
                concurrent = client.post("/api/logs/cleanup", json=body, headers=self.headers)
                self.assertEqual(concurrent.status_code, 409)
                self.assertEqual(concurrent.json["code"], "cleanup_busy")
                from fls_manager.log_access import open_log_file
                other = self.log(base, "other.log")
                with open_log_file(other, "ab", buffering=0) as writer:
                    writer.write(b"unrelated writer remains usable\n")
                process.kill()
                process.communicate(timeout=10)
                response = client.get("/api/logs/cleanup/requests/" + body["request_id"], headers=self.headers)
                self.assertEqual(response.json["request"]["items"][0]["status"], "unknown")
                with patch("fls_manager.log_access.LogAccess.unlink", side_effect=AssertionError("replay deleted")):
                    replay = client.post("/api/logs/cleanup", json=body, headers=self.headers)
                self.assertEqual(replay.json["request"]["status"], "unknown")
                self.assertTrue(path.exists())
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
