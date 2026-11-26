import copy
import io
import json
import threading
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from tests.test_bulk_workflows import TOKEN, isolated_app


class NativeProxyCheckTests(unittest.TestCase):
    def seed(self):
        from fls_manager.models import save_proxies
        save_proxies([dict(id="p", name="Alpha", type="http", host="localhost", port="8080", enabled=True,
                           username="private-user", password="private-password"),
                      dict(id="g", name="GitHub", type="github", url="https://proxy.example", enabled=True)])

    def data(self, **extra):
        from fls_manager.models import load_proxies
        from fls_manager.proxy_actions import proxy_version
        return dict(request_id="a" * 32, kind="quality", proxy_id="p", version=proxy_version(load_proxies()[0]), **extra)

    def post(self, client, data, path="/api/proxy/checks"):
        return client.post(path, json=data, headers={"X-Token": TOKEN})

    def get(self, client, suffix=""):
        return client.get("/api/proxy/checks" + suffix, headers={"X-Token": TOKEN})

    def test_session_csrf_no_store_and_zero_get_side_effects(self):
        with isolated_app() as (app, base):
            from fls_manager import proxy_checks as checks
            from fls_manager.native_sessions import create_native_session
            self.seed()
            client = app.test_client()
            self.assertEqual(client.get("/api/proxy/checks").status_code, 401)
            with app.app_context():
                session, csrf, _ = create_native_session(TOKEN, remember=True, verified=True)
            client.set_cookie("fls_native_session", session)
            data = self.data()
            self.assertEqual(client.post("/api/proxy/checks", json=data).status_code, 400)
            with patch.object(checks, "_launch") as launch, patch.object(checks, "_probe") as probe:
                response = client.post("/api/proxy/checks", json=data, headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 200, response.json)
                launch.assert_called_once()
                before = {p: p.read_bytes() for p in (base / "data").rglob("*.json")}
                for path in ("/api/proxy/checks", "/api/proxy/checks/" + data["request_id"]):
                    reply = client.get(path)
                    self.assertEqual(reply.status_code, 200)
                    self.assertEqual(reply.headers["Cache-Control"], "no-store")
                self.assertEqual(before, {p: p.read_bytes() for p in (base / "data").rglob("*.json")})
                probe.assert_not_called()

    def test_start_replay_intent_and_version_snapshot(self):
        with isolated_app() as (app, base):
            from fls_manager import models, proxy_checks as checks
            self.seed()
            client, data = app.test_client(), self.data()
            with patch.object(checks, "_launch") as launch:
                first = self.post(client, data)
                self.assertEqual(first.json["check"]["status"], "queued")
                self.assertEqual(self.post(client, data).status_code, 200)
                launch.assert_called_once()
                changed = copy.deepcopy(data)
                changed["kind"] = "test"
                self.assertEqual(self.post(client, changed).status_code, 409)
                proxies = models.load_proxies()
                proxies[0]["password"] = "later-secret"
                models.save_proxies(proxies)
                self.assertEqual(self.post(client, data).status_code, 200)
                launch.assert_called_once()
                snapshot = launch.call_args.args[1]
                self.assertEqual(snapshot["password"], "private-password")
                row = self.get(client, "/" + data["request_id"]).json["check"]
                self.assertFalse(row["current_matches"])
                new = dict(data, request_id="b" * 32)
                self.assertEqual(self.post(client, new).status_code, 409)
            text = " ".join(p.read_text() for p in checks.CHECK_DIR.glob("*.json"))
            for secret in ("private-user", "private-password", "later-secret", "localhost"):
                self.assertNotIn(secret, text)

    def test_draft_uses_keep_replace_clear_without_saving(self):
        with isolated_app() as (app, base):
            from fls_manager import models, proxy_checks as checks
            self.seed()
            before = models.load_proxies()
            data = dict(self.data(), draft={"settings": {"host": "draft.example"},
                                            "credentials": {"password_action": "replace", "password": "draft-secret",
                                                            "username_action": "clear"}})
            with patch.object(checks, "_launch") as launch:
                reply = self.post(app.test_client(), data)
                self.assertEqual(reply.status_code, 200, reply.json)
                self.assertEqual(reply.json["check"]["source"], "draft")
                snapshot = launch.call_args.args[1]
                self.assertEqual(snapshot["host"], "draft.example")
                self.assertEqual(snapshot["username"], "")
                self.assertEqual(snapshot["password"], "draft-secret")
                self.assertEqual(models.load_proxies(), before)
                checks.ACTIVE.clear()
                row = self.get(app.test_client(), "/" + data["request_id"]).json["check"]
                self.assertEqual(row["status"], "unknown")
                self.assertFalse(row["retry_allowed"])
            self.assertNotIn("draft-secret", (checks.CHECK_DIR / (data["request_id"] + ".json")).read_text())

    def test_new_disabled_draft_and_github_targets(self):
        with isolated_app() as (app, _):
            from fls_manager import proxy_checks as checks
            client = app.test_client()
            data = dict(request_id="a" * 32, kind="quality", draft={"settings": {
                "type": "github", "url": "https://proxy.example", "enabled": False}})
            with patch.object(checks, "_launch"):
                reply = self.post(client, data)
                self.assertEqual(reply.status_code, 200, reply.json)
                self.assertEqual([item["mode"] for item in reply.json["check"]["items"]], ["github_raw", "github_git"])
                bad = dict(data, request_id="b" * 32, targets=["https://custom.example"])
                self.assertEqual(self.post(client, bad).status_code, 400)

    def test_validation_and_queue_limits_have_no_start(self):
        with isolated_app() as (app, _):
            from fls_manager import proxy_checks as checks
            self.seed()
            client, data = app.test_client(), self.data()
            cases = [dict(data, request_id="../escape"), dict(data, kind="other"), dict(data, targets=[]),
                     dict(data, targets=["https://example.com"] * 6), dict(data, targets=["file:///etc/passwd"]),
                     dict(data, targets=["https://u:secret@example.com"]), dict(data, targets=["https://example.com/?token=s"]),
                     dict(data, targets=["https://example.com/#secret"]), dict(data, targets=["https://example.com"] * 2),
                     dict(data, extra=True), dict(data, draft={"settings": {"port": "0"}})]
            with patch.object(checks, "_launch") as launch:
                for body in cases:
                    response = self.post(client, body)
                    self.assertEqual(response.status_code, 400, response.json)
                self.assertEqual(client.post("/api/proxy/checks", data="{}", headers={"X-Token": TOKEN}).status_code, 415)
                self.assertEqual(client.post("/api/proxy/checks", data='{"request_id":1,"request_id":2}',
                                             content_type="application/json", headers={"X-Token": TOKEN}).status_code, 400)
                self.assertEqual(client.post("/api/proxy/checks", data=" " * 65537,
                                             content_type="application/json", headers={"X-Token": TOKEN}).status_code, 413)
                with patch.object(checks, "MAX_PENDING", 0):
                    self.assertEqual(self.post(client, data).status_code, 429)
                launch.assert_not_called()

    def test_write_before_and_after_failure_never_launches_or_replays(self):
        for after in (False, True):
            with self.subTest(after=after), isolated_app() as (app, _):
                from fls_manager import proxy_checks as checks
                self.seed()
                client, data = app.test_client(), self.data()
                original = checks._save

                def fail(row, key):
                    if after:
                        original(row, key)
                    raise OSError("private-secret /server/path")

                with patch.object(checks, "_save", side_effect=fail), patch.object(checks, "_launch") as launch:
                    reply = self.post(client, data)
                    self.assertEqual(reply.status_code, 503)
                    self.assertNotIn("private-secret", reply.get_data(as_text=True))
                    launch.assert_not_called()
                if after:
                    with patch.object(checks, "_launch") as launch:
                        reply = self.post(client, data)
                        self.assertEqual(reply.json["check"]["status"], "unknown")
                        launch.assert_not_called()

    def test_index_failure_remains_queryable_and_start_failure_is_terminal(self):
        with isolated_app() as (app, _):
            from fls_manager import proxy_checks as checks
            self.seed()
            client, data = app.test_client(), self.data()
            write = checks.write_json

            def fail_index(path, body):
                if path == checks.INDEX_FILE:
                    raise OSError("disk")
                write(path, body)

            with patch.object(checks, "write_json", side_effect=fail_index), patch.object(checks, "_launch") as launch:
                self.assertEqual(self.post(client, data).status_code, 503)
                launch.assert_not_called()
            self.assertEqual(self.get(client, "/" + data["request_id"]).json["check"]["status"], "unknown")
            with patch.object(checks, "_launch", side_effect=RuntimeError("thread")):
                reply = self.post(client, dict(data, request_id="b" * 32))
            self.assertEqual(reply.json["check"]["error_code"], "start_failed")
            self.assertEqual(reply.json["check"]["status"], "failed")

    def test_worker_progress_partial_and_finish_persistence_failure(self):
        with isolated_app() as (app, _):
            from fls_manager import proxy_checks as checks
            self.seed()
            client, data = app.test_client(), self.data(targets=["https://a.example", "https://b.example"])
            with patch.object(checks, "_launch") as launch:
                self.post(client, data)
            snapshots = []

            def probe(proxy, item):
                snapshots.append(checks.get_check(data["request_id"], app.secret_key))
                return dict(status="succeeded" if len(snapshots) == 1 else "failed", reachable=len(snapshots) == 1,
                            http_success=len(snapshots) == 1, status_code=200 if len(snapshots) == 1 else None,
                            elapsed_ms=10, error_code="" if len(snapshots) == 1 else "network")

            with patch.object(checks, "_probe", side_effect=probe):
                checks._run(*launch.call_args.args)
            self.assertEqual(snapshots[1]["completed"], 1)
            row = self.get(client, "/" + data["request_id"]).json["check"]
            self.assertEqual(row["status"], "partial")
            self.assertEqual(row["completed"], 2)
            self.assertTrue(row["retry_allowed"])
            with patch.object(checks, "_launch") as launch:
                self.post(client, dict(data, request_id="b" * 32))
            original = checks._save

            def fail_finish(row, key):
                if row["status"] == "succeeded":
                    raise OSError("disk")
                original(row, key)

            with patch.object(checks, "_probe", return_value=dict(status="succeeded")), patch.object(checks, "_save", side_effect=fail_finish):
                checks._run(*launch.call_args.args)
            row = self.get(client, "/" + "b" * 32).json["check"]
            self.assertEqual(row["status"], "unknown")
            self.assertEqual(row["completed"], 2)

    def test_retry_new_id_uses_current_version_and_preserves_history(self):
        with isolated_app() as (app, _):
            from fls_manager import models, proxy_checks as checks
            from fls_manager.proxy_actions import proxy_version
            self.seed()
            client, data = app.test_client(), self.data()
            with patch.object(checks, "_launch", side_effect=RuntimeError("thread")):
                self.post(client, data)
            original = (checks.CHECK_DIR / (data["request_id"] + ".json")).read_bytes()
            proxies = models.load_proxies()
            proxies[0]["host"] = "changed.example"
            models.save_proxies(proxies)
            retry = dict(request_id="b" * 32, version=data["version"])
            path = "/api/proxy/checks/" + data["request_id"] + "/retry"
            self.assertEqual(self.post(client, retry, path).status_code, 409)
            retry["version"] = proxy_version(proxies[0])
            with patch.object(checks, "_launch") as launch:
                reply = self.post(client, retry, path)
                self.assertEqual(reply.status_code, 200, reply.json)
                self.assertEqual(reply.json["check"]["retry_of"], data["request_id"])
                self.assertEqual(launch.call_args.args[1]["host"], "changed.example")
                self.assertEqual(self.post(client, retry, path).status_code, 200)
                launch.assert_called_once()
            self.assertEqual(original, (checks.CHECK_DIR / (data["request_id"] + ".json")).read_bytes())

    def test_legacy_history_is_safe_read_only_filtered_and_retryable(self):
        with isolated_app() as (app, _):
            from fls_manager import proxy_checks as checks
            self.seed()
            old = [dict(id="c" * 32, proxy_id="p", proxy_name="Old", proxy_type="http", kind="quality", status="failed",
                        urls=["https://example.com"], error="SECRET /server/path", created_at="2026-11-25 00:00:00",
                        items=[dict(url="https://user:SECRET@example.com?token=SECRET", elapsed="20 ms / SECRET", ok=False, status_code="-")])]
            checks.PROXY_DIAGNOSTICS_FILE.write_text(json.dumps(old))
            before = checks.PROXY_DIAGNOSTICS_FILE.read_bytes()
            client = app.test_client()
            reply = self.get(client, "?proxy_id=p&status=failed&kind=quality&limit=1&page=999")
            self.assertEqual(reply.status_code, 200, reply.json)
            self.assertEqual(reply.json["page"], 1)
            row = reply.json["items"][0]
            self.assertTrue(row["retry_allowed"])
            self.assertEqual(row["items"][0]["elapsed_ms"], 20)
            self.assertNotIn("SECRET", reply.get_data(as_text=True))
            self.assertEqual(before, checks.PROXY_DIAGNOSTICS_FILE.read_bytes())
            with patch.object(checks, "_launch"):
                reply = self.post(client, dict(request_id="d" * 32, version=self.data()["version"]),
                                  "/api/proxy/checks/" + row["id"] + "/retry")
                self.assertEqual(reply.status_code, 200, reply.json)
                self.assertEqual(reply.json["check"]["targets"], ["https://example.com"])

    def test_corruption_and_missing_are_not_reported_as_success(self):
        with isolated_app() as (app, _):
            from fls_manager import proxy_checks as checks
            self.seed()
            client, data = app.test_client(), self.data()
            self.assertEqual(self.get(client, "/" + "a" * 32).status_code, 404)
            with patch.object(checks, "_launch"):
                self.post(client, data)
            path = checks.CHECK_DIR / (data["request_id"] + ".json")
            row = json.loads(path.read_text())
            row["status"] = "succeeded"
            path.write_text(json.dumps(row))
            self.assertEqual(self.get(client, "/" + data["request_id"]).status_code, 503)
            self.assertEqual(self.get(client).status_code, 503)
            self.assertEqual(self.post(client, data).status_code, 503)

    def test_http_does_not_read_normal_bodies_and_reports_status_separately(self):
        with isolated_app():
            from fls_manager import proxy_checks as checks
            import requests
            response = MagicMock(status_code=503, headers={})
            response.__enter__.return_value = response
            session = MagicMock()
            session.__enter__.return_value = session
            session.get.return_value = response
            with patch.object(requests, "Session", return_value=session):
                result = checks._probe(dict(type="http", host="localhost", port="80"), dict(target="https://example.com", mode="http"))
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue(result["reachable"])
            self.assertFalse(result["http_success"])
            self.assertEqual(result["status_code"], 503)
            response.iter_content.assert_not_called()
            self.assertFalse(session.trust_env)
            self.assertTrue(session.get.call_args.kwargs["stream"])
            self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_http_errors_and_raw_body_limits_are_safe(self):
        with isolated_app():
            from fls_manager import proxy_checks as checks
            import requests
            session = MagicMock()
            session.__enter__.return_value = session
            for error, code in ((requests.exceptions.Timeout("SECRET"), "timeout"),
                                (requests.exceptions.ProxyError("SECRET"), "network")):
                session.get.side_effect = error
                with patch.object(requests, "Session", return_value=session):
                    result = checks._probe(dict(type="http", host="localhost", port="80"), dict(target="https://example.com", mode="http"))
                self.assertEqual(result["error_code"], code)
                self.assertNotIn("SECRET", json.dumps(result))
            session.get.side_effect = None
            response = MagicMock(status_code=200, headers={})
            response.__enter__.return_value = response
            chunks = iter([b" "] * 5000)
            response.iter_content.return_value = chunks
            session.get.return_value = response
            with patch.object(requests, "Session", return_value=session):
                result = checks._probe(dict(type="github", url="https://proxy.example"), dict(target=checks.GITHUB_QUALITY_RAW_URL, mode="github_raw"))
            self.assertEqual(result["error_code"], "body_empty")
            self.assertEqual(len(list(chunks)), 904)

    def test_actual_async_start_returns_and_snapshot_survives_edit(self):
        with isolated_app() as (app, _):
            from fls_manager import models, proxy_checks as checks
            self.seed()
            entered, release, done = threading.Event(), threading.Event(), threading.Event()
            seen = []

            def probe(proxy, item):
                seen.append(proxy["password"])
                entered.set()
                release.wait(3)
                return dict(status="succeeded", reachable=True, http_success=True, status_code=200, elapsed_ms=1, error_code="")

            def run(*args):
                try:
                    original(*args)
                finally:
                    done.set()

            original = checks._run
            with patch.object(checks, "_probe", side_effect=probe), patch.object(checks, "_run", side_effect=run):
                data = dict(self.data(), kind="test")
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(entered.wait(2))
                try:
                    proxies = models.load_proxies()
                    proxies[0]["password"] = "new-password"
                    models.save_proxies(proxies)
                    self.assertEqual(self.get(app.test_client(), "/" + data["request_id"]).json["check"]["status"], "running")
                finally:
                    release.set()
                    self.assertTrue(done.wait(3))
            self.assertEqual(seen, ["private-password"])
            self.assertEqual(self.get(app.test_client(), "/" + data["request_id"]).json["check"]["status"], "succeeded")

    def test_git_result_output_limit_timeout_and_no_credentials_helper(self):
        with isolated_app():
            from fls_manager import proxy_checks as checks
            for output, returncode, expected in ((b"a" * 40 + b"\trefs/heads/main\n", 0, ""),
                                                (b"not a branch", 0, "git_failed"),
                                                (b"SECRET" * 6000, 1, "output_limit")):
                process = MagicMock(stdout=io.BytesIO(output), returncode=returncode, pid=987654)
                with patch.object(checks.shutil, "which", return_value="git"), \
                     patch.object(checks.subprocess, "Popen", return_value=process) as popen, \
                     patch.object(checks.os, "killpg"):
                    result = checks._git_probe({"type": "github", "url": "https://proxy.example"})
                self.assertEqual(result["error_code"], expected)
                self.assertNotIn("SECRET", json.dumps(result))
                self.assertIn("credential.helper=", popen.call_args.args[0])
                self.assertEqual(popen.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
                self.assertTrue(process.stdout.closed)
            process = MagicMock(stdout=io.BytesIO(), pid=987654)
            process.wait.side_effect = [subprocess.TimeoutExpired("git", 25), 0]
            with patch.object(checks.shutil, "which", return_value="git"), \
                 patch.object(checks.subprocess, "Popen", return_value=process), \
                 patch.object(checks.os, "killpg") as kill:
                result = checks._git_probe({"type": "github", "url": "https://proxy.example"})
            self.assertEqual(result["error_code"], "timeout")
            kill.assert_called_once()

    def test_same_proxy_cannot_start_second_check_and_history_is_bounded(self):
        with isolated_app() as (app, _):
            from fls_manager import proxy_checks as checks
            self.seed()
            client, data = app.test_client(), self.data()
            with patch.object(checks, "_launch") as launch:
                self.post(client, data)
                response = self.post(client, dict(data, request_id="b" * 32))
                self.assertEqual(response.status_code, 409)
                self.assertIs(response.json["accepted"], False)
                launch.assert_called_once()
            checks.ACTIVE.clear()
            with patch.object(checks, "RECEIPT_LIMIT", 1), patch.object(checks, "_launch") as launch:
                self.assertEqual(self.post(client, dict(data, request_id="b" * 32)).status_code, 409)
                launch.assert_not_called()
            for query in ("?page=0", "?limit=101", "?status=other", "?kind=other", "?page=1&page=2", "?extra=1"):
                self.assertEqual(self.get(client, query).status_code, 400)

    def test_unsafe_redirect_is_not_followed_and_body_closed(self):
        with isolated_app():
            from fls_manager import proxy_checks as checks
            import requests
            response = MagicMock(status_code=302, headers={"Location": "https://user:SECRET@example.com"})
            session = MagicMock()
            session.__enter__.return_value = session
            session.get.return_value = response
            with patch.object(requests, "Session", return_value=session):
                result = checks._probe(dict(type="http", host="localhost", port="80"), dict(target="https://example.com", mode="http"))
            session.get.assert_called_once()
            response.close.assert_called_once()
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
