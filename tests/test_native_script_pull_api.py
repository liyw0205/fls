import hashlib
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    from tests.test_ui_route_components import TOKEN, isolated_app
except ModuleNotFoundError:
    from test_ui_route_components import TOKEN, isolated_app


class FakeResponse:
    def __init__(self, chunks, content_length=None, status_code=200):
        self._chunks = chunks
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("remote error with private URL token=never-return-this")

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        self.closed = True


class NativeScriptPullApiTests(unittest.TestCase):
    def _wait(self, client, job_id, expected):
        deadline = time.monotonic() + 3
        latest = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/scripts/pull/{job_id}", headers={"X-Token": TOKEN})
            latest = response.json["job"]
            if latest["status"] in expected:
                return latest
            time.sleep(0.01)
        self.fail(f"job did not reach {expected}: {latest}")

    def _submit(self, client, request_id="a" * 32, **overrides):
        data = {
            "request_id": request_id,
            "url": "https://example.invalid/script.py?token=private-value",
            "path": "script.py",
        }
        data.update(overrides)
        return client.post("/api/scripts/pull", json=data, headers={"X-Token": TOKEN})

    def test_auth_csrf_and_input_validation(self):
        with isolated_app() as (app, base):
            client = app.test_client()
            unauthorized = client.get("/api/scripts/pull-history")
            self.assertEqual(unauthorized.status_code, 401)

            from fls_manager.native_sessions import create_native_session

            with app.app_context():
                session_id, csrf_token, _ = create_native_session(TOKEN, remember=True, verified=True)
            client.set_cookie("fls_native_session", session_id)
            data = {"request_id": "a" * 32, "url": "https://example.invalid/a.py", "path": "a.py"}
            missing_csrf = client.post("/api/scripts/pull", json=data)
            self.assertEqual(missing_csrf.status_code, 400)
            import fls_manager.routes.scripts.native_pull as native_pull

            with patch.object(
                native_pull,
                "requests",
                SimpleNamespace(get=Mock(side_effect=RuntimeError("offline"))),
            ):
                accepted = client.post("/api/scripts/pull", json=data, headers={"X-CSRF-Token": csrf_token})
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(self._wait(client, accepted.json["job"]["id"], {"failed"})["error_code"], "download_failed")

            bad_url = self._submit(client, request_id="b" * 32, url="https://user:pass@example.invalid/a.py")
            traversal = self._submit(client, request_id="c" * 32, path="../outside.py")
            self.assertEqual(bad_url.json["code"], "invalid_url")
            self.assertEqual(traversal.json["code"], "invalid_path")
            self.assertFalse((base / "outside.py").exists())

    def test_download_reports_verified_progress_and_never_persists_url_secrets(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            body = b"print('native pull')\n"
            response = FakeResponse([body[:8], body[8:]], len(body))
            client = app.test_client()
            with patch.object(native_pull, "requests", SimpleNamespace(get=lambda *args, **kwargs: response)):
                submitted = self._submit(client)
                self.assertEqual(submitted.status_code, 202)
                job_id = submitted.json["job"]["id"]
                job = self._wait(client, job_id, {"complete"})

            self.assertEqual((base / "scripts" / "script.py").read_bytes(), body)
            self.assertEqual(job["progress"]["downloaded_bytes"], len(body))
            self.assertEqual(job["progress"]["total_bytes"], len(body))
            self.assertEqual(job["progress"]["percent"], 100)
            self.assertEqual(job["path"], "script.py")
            self.assertEqual(job["source_host"], "example.invalid")
            persisted = (base / "data" / "script_pull_jobs.json").read_text(encoding="utf-8")
            self.assertNotIn("private-value", persisted)
            self.assertNotIn("https://", persisted)
            self.assertNotIn(str(base), persisted)
            self.assertTrue(response.closed)
            self.assertEqual(list((base / "scripts").glob(".fls-pull-*.tmp")), [])

    def test_request_id_replays_same_job_and_rejects_different_payload(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            response = FakeResponse([b"ok"], 2)
            client = app.test_client()
            with patch.object(native_pull, "requests", SimpleNamespace(get=lambda *args, **kwargs: response)):
                first = self._submit(client)
                job_id = first.json["job"]["id"]
                self._wait(client, job_id, {"complete"})
                replay = self._submit(client)
                conflict = self._submit(client, url="https://other.invalid/script.py")

            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json["job"]["id"], job_id)
            self.assertTrue(replay.json["job"]["replayed"])
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json["code"], "request_id_conflict")
            self.assertEqual((base / "scripts" / "script.py").read_bytes(), b"ok")

    def test_existing_target_and_download_limit_do_not_overwrite_or_leave_stage(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            client = app.test_client()
            target = base / "scripts" / "script.py"
            target.write_bytes(b"keep")
            collision = self._submit(client)
            self.assertEqual(collision.status_code, 409)
            self.assertEqual(target.read_bytes(), b"keep")

            target.unlink()
            response = FakeResponse([b"12345"])
            with patch.object(native_pull, "SCRIPT_PULL_MAX_BYTES", 4), patch.object(
                native_pull, "requests", SimpleNamespace(get=lambda *args, **kwargs: response),
            ):
                accepted = self._submit(client)
                failed = self._wait(client, accepted.json["job"]["id"], {"failed"})

            self.assertEqual(failed["error_code"], "too_large")
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob(".fls-pull-*.tmp")), [])

    def test_unknown_content_length_never_reports_a_percent(self):
        with isolated_app() as (app, _base):
            import fls_manager.routes.scripts.native_pull as native_pull

            client = app.test_client()
            response = FakeResponse([b"chunk"])
            with patch.object(native_pull, "requests", SimpleNamespace(get=lambda *args, **kwargs: response)):
                accepted = self._submit(client)
                completed = self._wait(client, accepted.json["job"]["id"], {"complete"})

            self.assertEqual(completed["progress"]["total_bytes"], None)
            self.assertEqual(completed["progress"]["percent"], None)

    def test_cancel_stops_before_publish_and_removes_staging_file(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            entered = threading.Event()
            release = threading.Event()

            class BlockingResponse(FakeResponse):
                def iter_content(self, chunk_size):
                    yield b"partial"
                    entered.set()
                    release.wait(2)
                    yield b"finish"

            client = app.test_client()
            response = BlockingResponse([], 13)
            with patch.object(
                native_pull, "requests", SimpleNamespace(get=lambda *args, **kwargs: response),
            ):
                accepted = self._submit(client)
                self.assertTrue(entered.wait(2))
                cancel = client.post(
                    f"/api/scripts/pull/{accepted.json['job']['id']}/cancel",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(cancel.status_code, 202)
                release.set()
                result = self._wait(client, accepted.json["job"]["id"], {"cancelled"})

            target = base / "scripts" / "script.py"
            self.assertEqual(result["status"], "cancelled")
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob(".fls-pull-*.tmp")), [])

    def test_cancel_is_not_applied_when_job_state_cannot_be_persisted(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            entered = threading.Event()
            release = threading.Event()

            class BlockingResponse(FakeResponse):
                def iter_content(self, chunk_size):
                    yield b"partial"
                    entered.set()
                    release.wait(2)
                    yield b"finish"

            client = app.test_client()
            response = BlockingResponse([], 13)
            with patch.object(
                native_pull, "requests", SimpleNamespace(get=lambda *args, **kwargs: response),
            ):
                accepted = self._submit(client)
                job_id = accepted.json["job"]["id"]
                self.assertTrue(entered.wait(2))
                with patch.object(native_pull, "_save_locked", side_effect=OSError("disk unavailable")):
                    cancel = client.post(
                        f"/api/scripts/pull/{job_id}/cancel",
                        headers={"X-Token": TOKEN},
                    )
                self.assertEqual(cancel.status_code, 503)
                self.assertEqual(cancel.json["code"], "job_persistence_failed")
                self.assertFalse(native_pull.SCRIPT_PULL_CANCEL[job_id].is_set())
                release.set()
                result = self._wait(client, job_id, {"complete"})

            self.assertEqual(result["status"], "complete")
            self.assertEqual((base / "scripts" / "script.py").read_bytes(), b"partialfinish")

    def test_same_content_created_by_another_publisher_is_not_claimed(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            body = b"same content"
            target = base / "scripts" / "script.py"

            def competing_publish(*_args):
                target.write_bytes(body)
                raise FileExistsError("target created by another publisher")

            client = app.test_client()
            with patch.object(
                native_pull, "requests", SimpleNamespace(get=lambda *args, **kwargs: FakeResponse([body], len(body))),
            ), patch.object(native_pull, "_publish_no_replace", side_effect=competing_publish):
                accepted = self._submit(client)
                failed = self._wait(client, accepted.json["job"]["id"], {"failed"})

            self.assertEqual(failed["error_code"], "target_exists")
            self.assertEqual(target.read_bytes(), body)

    def test_parent_symlink_swap_does_not_publish_outside_original_directory(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            scripts = base / "scripts"
            parent = scripts / "nested"
            moved = scripts / "nested-moved"
            external = base / "external"
            parent.mkdir()
            external.mkdir()

            def swap_parent(*_args, **_kwargs):
                parent.rename(moved)
                parent.symlink_to(external, target_is_directory=True)
                return FakeResponse([b"must not publish"], 15)

            client = app.test_client()
            with patch.object(native_pull, "requests", SimpleNamespace(get=swap_parent)):
                accepted = self._submit(client, path="nested/script.py")
                failed = self._wait(client, accepted.json["job"]["id"], {"failed"})

            self.assertEqual(failed["error_code"], "path_changed")
            self.assertFalse((external / "script.py").exists())
            self.assertFalse((moved / "script.py").exists())
            self.assertEqual(list(moved.glob(".fls-pull-*.tmp")), [])

    def test_replaced_parent_directory_is_not_mistaken_for_original_target(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            scripts = base / "scripts"
            parent = scripts / "nested"
            moved = scripts / "nested-moved"
            parent.mkdir()

            def replace_parent(*_args, **_kwargs):
                parent.rename(moved)
                parent.mkdir()
                return FakeResponse([b"must not publish"], 15)

            client = app.test_client()
            with patch.object(native_pull, "requests", SimpleNamespace(get=replace_parent)):
                accepted = self._submit(client, path="nested/script.py")
                failed = self._wait(client, accepted.json["job"]["id"], {"failed"})

            self.assertEqual(failed["error_code"], "path_changed")
            self.assertFalse((parent / "script.py").exists())
            self.assertFalse((moved / "script.py").exists())
            self.assertEqual(list(moved.glob(".fls-pull-*.tmp")), [])

    def test_restart_recovers_publish_receipt_only_when_target_identity_and_hash_match(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            job_id = "f" * 32
            request_id = "1" * 32
            target = base / "scripts" / "published.py"
            target.write_bytes(b"published")
            row = {
                "id": job_id,
                "request_id": request_id,
                "fingerprint": hashlib.sha256(b"request").hexdigest(),
                "kind": "file",
                "path": "published.py",
                "source_host": "example.invalid",
                "status": "publishing",
                "downloaded_bytes": 9,
                "total_bytes": 9,
                "sha256": hashlib.sha256(b"published").hexdigest(),
                "target_dev": target.stat().st_dev,
                "target_ino": target.stat().st_ino,
                "error_code": "",
                "created_at": "2026-11-25 00:00:00",
                "updated_at": "2026-11-25 00:00:00",
            }
            (base / "data" / "script_pull_jobs.json").write_text(json.dumps([row]), encoding="utf-8")
            native_pull.SCRIPT_PULL_INITIALIZED = False
            native_pull.SCRIPT_PULL_JOBS.clear()
            client = app.test_client()
            status = client.get(f"/api/scripts/pull/{job_id}", headers={"X-Token": TOKEN})

            self.assertEqual(status.json["job"]["status"], "complete")
            self.assertEqual(target.read_bytes(), b"published")

            row["target_ino"] += 1
            (base / "data" / "script_pull_jobs.json").write_text(json.dumps([row]), encoding="utf-8")
            native_pull.SCRIPT_PULL_INITIALIZED = False
            native_pull.SCRIPT_PULL_JOBS.clear()
            status = client.get(f"/api/scripts/pull/{job_id}", headers={"X-Token": TOKEN})

            self.assertEqual(status.json["job"]["status"], "unknown")
            self.assertEqual(status.json["job"]["error_code"], "worker_interrupted")

            row["target_ino"] = target.stat().st_ino
            row["sha256"] = hashlib.sha256(b"different").hexdigest()
            (base / "data" / "script_pull_jobs.json").write_text(json.dumps([row]), encoding="utf-8")
            native_pull.SCRIPT_PULL_INITIALIZED = False
            native_pull.SCRIPT_PULL_JOBS.clear()
            status = client.get(f"/api/scripts/pull/{job_id}", headers={"X-Token": TOKEN})

            self.assertEqual(status.json["job"]["status"], "unknown")
            self.assertEqual(status.json["job"]["error_code"], "worker_interrupted")

    def test_restart_recovery_marks_interrupted_download_unknown_and_cleans_temp(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_pull as native_pull

            job_id = "d" * 32
            request_id = "e" * 32
            target = base / "scripts" / "recover.py"
            temp = target.with_name(f".fls-pull-{job_id}.tmp")
            temp.write_bytes(b"partial")
            row = {
                "id": job_id,
                "request_id": request_id,
                "fingerprint": hashlib.sha256(b"request").hexdigest(),
                "kind": "file",
                "path": "recover.py",
                "source_host": "example.invalid",
                "status": "downloading",
                "downloaded_bytes": 7,
                "total_bytes": 9,
                "sha256": "",
                "error_code": "",
                "created_at": "2026-11-25 00:00:00",
                "updated_at": "2026-11-25 00:00:00",
            }
            (base / "data" / "script_pull_jobs.json").write_text(json.dumps([row]), encoding="utf-8")
            native_pull.SCRIPT_PULL_INITIALIZED = False
            native_pull.SCRIPT_PULL_JOBS.clear()
            client = app.test_client()
            status = client.get(f"/api/scripts/pull/{job_id}", headers={"X-Token": TOKEN})

            self.assertEqual(status.json["job"]["status"], "unknown")
            self.assertEqual(status.json["job"]["error_code"], "worker_interrupted")
            self.assertFalse(temp.exists())
            self.assertFalse(target.exists())

    def test_history_is_bounded_and_does_not_return_internal_fingerprint(self):
        with isolated_app() as (app, _base):
            import fls_manager.routes.scripts.native_pull as native_pull

            client = app.test_client()
            invalid = client.get("/api/scripts/pull-history?limit=101", headers={"X-Token": TOKEN})
            self.assertEqual(invalid.status_code, 400)
            response = client.get("/api/scripts/pull-history?limit=5", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["items"], [])
            self.assertNotIn("fingerprint", response.get_data(as_text=True))
            self.assertEqual(native_pull.SCRIPT_PULL_HISTORY_LIMIT, 200)

    def test_proxy_options_are_whitelisted_and_disabled_proxies_are_hidden(self):
        with isolated_app() as (app, _base):
            import fls_manager.routes.scripts.native_pull as native_pull

            client = app.test_client()
            proxies = [
                {
                    "id": "enabled-id",
                    "name": "Office",
                    "type": "http",
                    "host": "proxy.invalid",
                    "port": 8080,
                    "username": "private-user",
                    "password": "private-password",
                    "enabled": True,
                },
                {"id": "disabled-id", "name": "Off", "type": "http", "enabled": False},
            ]
            with patch.object(native_pull, "load_proxies", return_value=proxies):
                response = client.get("/api/scripts/pull-options", headers={"X-Token": TOKEN})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["proxies"], [{"id": "enabled-id", "name": "Office", "type": "http"}])
            body = response.get_data(as_text=True)
            self.assertNotIn("private-user", body)
            self.assertNotIn("private-password", body)
            self.assertNotIn("proxy.invalid", body)
            self.assertNotIn("disabled-id", body)


if __name__ == "__main__":
    unittest.main()
