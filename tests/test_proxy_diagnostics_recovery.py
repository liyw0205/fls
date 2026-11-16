import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "proxy-diagnostics-token"


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def shutdown_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None
    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix="fls-proxy-diagnostics-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "proxy-diagnostics-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        try:
            from fls_manager.app import create_app
            yield create_app(), Path(temp_dir)
        finally:
            shutdown_scheduler()
            purge_fls_modules()
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def seed_proxy(base_dir, proxy_id="p1"):
    path = Path(base_dir) / "data" / "proxies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{
            "id": proxy_id,
            "name": "Fixture proxy",
            "type": "http",
            "host": "proxy.local",
            "port": "8080",
            "enabled": True,
        }], ensure_ascii=False),
        encoding="utf-8",
    )


class ProxyDiagnosticsRecoveryTests(unittest.TestCase):
    def seed_diagnostic(self, base_dir, status="failed", kind="test"):
        from fls_manager import proxy
        from fls_manager.paths import PROXY_DIAGNOSTICS_FILE
        from fls_manager.storage import write_json

        seed_proxy(base_dir)
        write_json(PROXY_DIAGNOSTICS_FILE, [{"id": "d1", "proxy_id": "p1", "proxy_name": "Fixture proxy", "kind": kind,
                    "urls": ["https://fixture.local/"], "items": [], "status": status, "attempt": 1, "error": "initial failure"}])
        proxy.load_proxy_diagnostics()
        return PROXY_DIAGNOSTICS_FILE

    def test_saved_connection_test_persists_success_snapshot(self):
        with isolated_app() as (app, base_dir):
            seed_proxy(base_dir)
            with patch(
                "fls_manager.routes.proxy.api.test_proxy_object",
                return_value={"status_code": 204, "elapsed_ms": 7},
            ):
                response = app.test_client().post(
                    "/api/proxy/test/p1",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"ok": True, "name": "Fixture proxy", "status_code": 204, "elapsed_ms": 7})
            rows = read_json(base_dir / "data" / "proxy_diagnostics.json")
            self.assertEqual(rows[0]["kind"], "test")
            self.assertEqual(rows[0]["status"], "succeeded")
            self.assertEqual(rows[0]["items"][0]["status_code"], 204)

    def test_saved_quality_persists_terminal_failure_without_changing_api_shape(self):
        with isolated_app() as (app, base_dir):
            seed_proxy(base_dir)
            result = [{"url": "https://a.example", "ok": False, "status_code": "-", "elapsed": "down"}]
            with patch("fls_manager.routes.proxy.api.quality_proxy_object", return_value=result):
                response = app.test_client().get(
                    "/api/proxy/quality/p1?urls=https://a.example",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"ok": True, "name": "Fixture proxy", "items": result})
            rows = read_json(base_dir / "data" / "proxy_diagnostics.json")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(rows[0]["kind"], "quality")
            self.assertEqual(rows[0]["items"], result)

    def test_running_diagnostic_recovers_to_unknown_and_retries(self):
        with isolated_app() as (app, base_dir):
            seed_proxy(base_dir)
            from fls_manager import paths
            from fls_manager.storage import write_json

            write_json(paths.PROXY_DIAGNOSTICS_FILE, [{
                "id": "d1", "proxy_id": "p1", "proxy_name": "Fixture proxy", "proxy_type": "http",
                "kind": "quality", "urls": ["https://a.example"], "items": [], "status": "running",
                "attempt": 1, "message": "检测中",
            }])
            from fls_manager import proxy
            self.assertEqual(proxy.load_proxy_diagnostics()[0]["status"], "unknown")
            result = [{"url": "https://a.example", "ok": True, "status_code": 200, "elapsed": "1 ms"}]
            with patch.object(proxy, "quality_proxy_object", return_value=result):
                response = app.test_client().post(
                    "/api/proxy/diagnostics/retry/d1",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(response.get_json()["diagnostic"]["status"], "succeeded")
            self.assertEqual(response.get_json()["diagnostic"]["attempt"], 2)

    def test_concurrent_retry_claims_once(self):
        with isolated_app() as (_app, base_dir):
            seed_proxy(base_dir)
            from fls_manager import paths, proxy
            from fls_manager.storage import write_json

            write_json(paths.PROXY_DIAGNOSTICS_FILE, [{
                "id": "d1", "proxy_id": "p1", "proxy_name": "Fixture proxy", "proxy_type": "http",
                "kind": "quality", "urls": ["https://a.example"], "items": [], "status": "failed",
                "attempt": 1, "error": "down",
            }])
            entered = threading.Event()
            release = threading.Event()
            results = []

            def detect(*_args):
                entered.set()
                release.wait(2)
                return [{"url": "https://a.example", "ok": True, "status_code": 200, "elapsed": "1 ms"}]

            with patch.object(proxy, "quality_proxy_object", side_effect=detect):
                first = threading.Thread(target=lambda: results.append(proxy.retry_proxy_diagnostic("d1")))
                first.start()
                self.assertTrue(entered.wait(1))
                second = proxy.retry_proxy_diagnostic("d1")
                release.set()
                first.join(2)

            self.assertFalse(second[0])
            self.assertEqual(second[1], "代理诊断正在检测")
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0][0])

    def test_page_and_api_share_history_and_error_contract(self):
        with isolated_app() as (app, base_dir):
            seed_proxy(base_dir)
            from fls_manager import paths
            from fls_manager.storage import write_json

            write_json(paths.PROXY_DIAGNOSTICS_FILE, [{
                "id": "d1", "proxy_id": "p1", "proxy_name": "Fixture proxy", "proxy_type": "http",
                "kind": "quality", "urls": ["https://a.example"], "items": [], "status": "failed",
                "attempt": 1, "error": "remote down",
            }])
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            api = client.get("/api/proxy/diagnostics?status=failed&proxy_id=p1", headers=headers)
            page = client.get("/proxy/diagnostics?status=failed&proxy_id=p1", headers=headers)
            self.assertEqual(api.status_code, 200)
            self.assertEqual(api.get_json()["items"][0]["id"], "d1")
            self.assertIn("remote down", page.get_data(as_text=True))
            self.assertIn("/proxy/diagnostics/retry/d1", page.get_data(as_text=True))
            self.assertEqual(client.get("/api/proxy/diagnostics?limit=bad", headers=headers).status_code, 400)
            self.assertEqual(client.post("/api/proxy/diagnostics/retry/missing", headers=headers).status_code, 404)
            from fls_manager import proxy
            with patch.object(proxy, "quality_proxy_object", side_effect=RuntimeError("fixture failure")):
                self.assertEqual(client.post("/api/proxy/diagnostics/retry/d1", headers=headers).status_code, 500)

    def test_malformed_history_is_bounded_and_restart_safe(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager import paths, proxy
            from fls_manager.storage import write_json

            huge = "x" * 3000
            write_json(paths.PROXY_DIAGNOSTICS_FILE, [
                None,
                {
                    "id": "d1",
                    "status": "running",
                    "kind": "unexpected",
                    "urls": [huge],
                    "items": [{"url": huge, "elapsed": huge}, "bad"],
                    "error": huge,
                },
            ])

            row = proxy.load_proxy_diagnostics()[0]
            self.assertEqual(row["status"], "unknown")
            self.assertEqual(row["kind"], "quality")
            self.assertEqual(len(row["urls"][0]), 500)
            self.assertEqual(len(row["items"][0]["url"]), 500)
            self.assertEqual(len(row["items"][0]["elapsed"]), 2000)
            self.assertLessEqual(len(row["error"]), 12000)
            self.assertIn("面板重启后无法确认代理检测结果", row["error"])
            persisted = json.loads((base_dir / "data" / "proxy_diagnostics.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted[0]["status"], "unknown")
            self.assertEqual(len(persisted[0]["items"]), 1)

    def test_retry_of_disabled_proxy_is_persisted_as_failure(self):
        with isolated_app() as (app, base_dir):
            seed_proxy(base_dir)
            from fls_manager import paths
            from fls_manager.storage import write_json

            proxies = json.loads((base_dir / "data" / "proxies.json").read_text(encoding="utf-8"))
            proxies[0]["enabled"] = False
            (base_dir / "data" / "proxies.json").write_text(json.dumps(proxies), encoding="utf-8")
            write_json(paths.PROXY_DIAGNOSTICS_FILE, [{
                "id": "d1", "proxy_id": "p1", "proxy_name": "Fixture proxy", "kind": "quality",
                "urls": ["https://a.example"], "items": [], "status": "failed", "attempt": 1,
            }])

            response = app.test_client().post("/api/proxy/diagnostics/retry/d1", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.get_json()["error"], "代理已停用")
            row = json.loads((base_dir / "data" / "proxy_diagnostics.json").read_text(encoding="utf-8"))[0]
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error"], "代理已停用")

    def test_corrupt_history_is_never_overwritten_or_treated_as_an_empty_success(self):
        for data in (b"not json", b"\xff", b"{}"):
            with self.subTest(data=data), isolated_app() as (app, base_dir):
                from fls_manager import proxy
                target = self.seed_diagnostic(base_dir)
                target.write_bytes(data)
                client = app.test_client()
                headers = {"X-Token": TOKEN}
                with patch.object(proxy, "test_proxy_object") as detect, patch("fls_manager.routes.proxy.api.test_proxy_object") as initial:
                    api = client.get("/api/proxy/diagnostics", headers=headers)
                    page = client.get("/proxy/diagnostics", headers=headers)
                    retry = client.post("/api/proxy/diagnostics/retry/d1", headers=headers)
                    form = client.post("/proxy/diagnostics/retry/d1", headers=headers)
                    create = client.post("/api/proxy/test/p1", headers=headers)
                self.assertEqual([item.status_code for item in (api, page, retry, form, create)], [500] * 5)
                self.assertFalse(api.get_json()["ok"])
                self.assertIn("重新读取诊断记录", page.get_data(as_text=True))
                self.assertEqual(target.read_bytes(), data)
                detect.assert_not_called()
                initial.assert_not_called()
                self.assertEqual(proxy.PROXY_ACTIVE_DIAGNOSTICS, set())

    def test_claim_failure_has_zero_network_side_effect_and_preserves_original_attempt(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import proxy
            target = self.seed_diagnostic(base_dir)
            before = target.read_bytes()
            with patch.object(proxy, "write_json", side_effect=OSError("claim rejected")), patch.object(proxy, "test_proxy_object") as detect:
                response = app.test_client().post("/api/proxy/diagnostics/retry/d1", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 500)
            self.assertIn("claim rejected", response.get_json()["error"])
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(proxy.PROXY_ACTIVE_DIAGNOSTICS, set())
            detect.assert_not_called()

    def test_initial_lookup_and_network_failures_return_json_with_actionable_recovery(self):
        with isolated_app() as (app, base_dir):
            target = self.seed_diagnostic(base_dir)
            headers = {"X-Token": TOKEN}
            client = app.test_client()
            for kind in ("test", "quality"):
                with self.subTest(kind=kind):
                    with patch("fls_manager.routes.proxy.api.get_proxy_for_test", side_effect=OSError("lookup rejected")):
                        lookup = client.post("/api/proxy/" + kind + "/p1", headers=headers)
                    self.assertEqual(lookup.status_code, 500)
                    self.assertEqual(lookup.get_json()["error"], "lookup rejected")
                    with patch("fls_manager.routes.proxy.api." + kind + "_proxy_object", side_effect=OSError("network rejected")):
                        network = client.post("/api/proxy/" + kind + "/p1", headers=headers)
                    self.assertEqual(network.status_code, 502)
                    self.assertEqual(network.get_json()["recovery_url"], "/proxy/diagnostics")
                    self.assertEqual(read_json(target)[0]["status"], "failed")
                    self.assertEqual(read_json(target)[0]["error"], "network rejected")

    def test_terminal_save_failure_does_not_fabricate_network_failure_and_unknown_can_retry_once(self):
        for initial in (False, True):
            with self.subTest(initial=initial), isolated_app() as (app, base_dir):
                from fls_manager import proxy
                target = self.seed_diagnostic(base_dir)
                write = proxy.write_json
                calls = 0

                def fail_terminal(path, rows):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError("terminal rejected")
                    return write(path, rows)

                client = app.test_client()
                headers = {"X-Token": TOKEN}
                url = "/api/proxy/test/p1" if initial else "/api/proxy/diagnostics/retry/d1"
                detect_name = "fls_manager.routes.proxy.api.test_proxy_object" if initial else "fls_manager.proxy.test_proxy_object"
                with patch.object(proxy, "write_json", side_effect=fail_terminal), patch(detect_name, return_value={"status_code": 204, "elapsed_ms": 1}) as detect:
                    response = client.post(url, headers=headers)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(calls, 2)
                detect.assert_called_once()
                payload = response.get_json()
                self.assertEqual(payload["status"], "unknown")
                self.assertEqual(payload["recovery_url"], "/proxy/diagnostics")
                self.assertIn("terminal rejected", payload["error"])
                self.assertEqual(read_json(target)[0]["status"], "running")
                self.assertEqual(proxy.PROXY_ACTIVE_DIAGNOSTICS, set())
                rows = client.get("/api/proxy/diagnostics", headers=headers).get_json()["items"]
                self.assertEqual(rows[0]["status"], "unknown")
                retry_url = "/api/proxy/diagnostics/retry/" + payload["diagnostic_id"]
                with patch.object(proxy, "test_proxy_object", return_value={"status_code": 204, "elapsed_ms": 1}) as detect:
                    recovered = client.post(retry_url, headers=headers)
                    duplicate = client.post(retry_url, headers=headers)
                self.assertEqual((recovered.status_code, duplicate.status_code), (200, 409))
                detect.assert_called_once()
                self.assertEqual(recovered.get_json()["diagnostic"]["status"], "succeeded")

    def test_post_claim_lookup_and_finish_read_failures_release_claim_and_report_real_state(self):
        for stage in ("lookup", "finish_read", "missing"):
            with self.subTest(stage=stage), isolated_app() as (app, base_dir):
                from fls_manager import proxy
                target = self.seed_diagnostic(base_dir)
                read = proxy._read_diagnostic_history
                count = 0

                def read_history():
                    nonlocal count
                    count += 1
                    if stage == "finish_read" and count == 2:
                        raise PermissionError("history unreadable")
                    return read()

                def detect(*args):
                    if stage == "missing":
                        target.write_text("[]")
                    return {"status_code": 204, "elapsed_ms": 1}

                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(proxy, "_read_diagnostic_history", side_effect=read_history))
                    stack.enter_context(patch.object(proxy, "test_proxy_object", side_effect=detect))
                    if stage == "lookup":
                        stack.enter_context(patch.object(proxy, "get_proxy_for_test", side_effect=OSError("lookup rejected")))
                    response = app.test_client().post("/api/proxy/diagnostics/retry/d1", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 500)
                self.assertEqual(proxy.PROXY_ACTIVE_DIAGNOSTICS, set())
                if stage == "lookup":
                    self.assertEqual(read_json(target)[0]["status"], "failed")
                    self.assertIn("lookup rejected", read_json(target)[0]["error"])
                else:
                    self.assertEqual(response.get_json()["status"], "unknown")
                if stage == "finish_read":
                    self.assertEqual(proxy.load_proxy_diagnostics()[0]["status"], "unknown")

    def test_error_after_terminal_publication_never_overwrites_success_or_resends(self):
        for form in (False, True):
            with self.subTest(form=form), isolated_app() as (app, base_dir):
                from fls_manager import proxy
                target = self.seed_diagnostic(base_dir)
                write = proxy.write_json

                def publish_then_error(path, rows):
                    write(path, rows)
                    if rows[0]["status"] == "succeeded":
                        raise OSError("published but acknowledgment failed")

                client = app.test_client()
                headers = {"X-Token": TOKEN}
                url = "/proxy/diagnostics/retry/d1" if form else "/api/proxy/diagnostics/retry/d1"
                with patch.object(proxy, "write_json", side_effect=publish_then_error), patch.object(
                    proxy, "test_proxy_object", return_value={"status_code": 204, "elapsed_ms": 1},
                ) as detect:
                    response = client.post(url, headers=headers)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(read_json(target)[0]["status"], "succeeded")
                self.assertEqual(proxy.PROXY_ACTIVE_DIAGNOSTICS, set())
                detect.assert_called_once()
                with patch.object(proxy, "test_proxy_object") as detect:
                    self.assertEqual(client.post(url, headers=headers).status_code, 409)
                detect.assert_not_called()

    def test_invalid_filters_reject_api_page_and_retry_before_normalizing_or_sending(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import proxy
            target = self.seed_diagnostic(base_dir)
            before = target.read_bytes()
            headers = {"X-Token": TOKEN}
            client = app.test_client()
            for query in ({"status": "invalid"}, {"limit": "bad"}, {"limit": "0"}, {"limit": "101"},
                          {"proxy_id": "bad\x00id"}, {"status": ["failed", "unknown"]}):
                with self.subTest(query=query), patch.object(proxy, "test_proxy_object") as detect:
                    self.assertEqual(client.get("/api/proxy/diagnostics", query_string=query, headers=headers).status_code, 400)
                    self.assertEqual(client.get("/proxy/diagnostics", query_string=query, headers=headers).status_code, 400)
                    self.assertEqual(client.post("/proxy/diagnostics/retry/d1", query_string=query, headers=headers).status_code, 400)
                    self.assertEqual(target.read_bytes(), before)
                    detect.assert_not_called()

    def test_filtered_form_keeps_selection_csrf_and_recovers_over_actual_local_http(self):
        from tests.test_task_save_recovery import TaskFormParser

        class Handler(BaseHTTPRequestHandler):
            calls = 0

            def do_GET(self):
                Handler.calls += 1
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with isolated_app() as (app, base_dir):
                from fls_manager.models import save_proxies
                target = self.seed_diagnostic(base_dir)
                save_proxies([{"id": "p1", "name": "Local", "type": "github", "enabled": True,
                               "url": f"http://127.0.0.1:{server.server_port}"}])
                client = app.test_client()
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="proxy-csrf")
                page = client.get("/proxy/diagnostics?status=failed&proxy_id=p1&limit=5")
                self.assertEqual(page.status_code, 200)
                html = page.get_data(as_text=True)
                self.assertIn('for="diagnostic-status"', html)
                self.assertIn('id="diagnostic-proxy"', html)
                form = next(form for form in TaskFormParser(html).forms if form["method"] == "post")
                self.assertIn("status=failed", form["action"])
                self.assertEqual(form["fields"]["csrf_token"], "proxy-csrf")
                before = target.read_bytes()
                ajax = {"X-Requested-With": "FLS-Ajax"}
                self.assertEqual(client.post(form["action"], headers=ajax).status_code, 400)
                self.assertEqual(target.read_bytes(), before)
                recovered = client.post(form["action"], data=form["fields"], headers=ajax)
                self.assertEqual(recovered.status_code, 200)
                self.assertIn("重新检测成功", recovered.get_data(as_text=True))
                self.assertIn('value="p1" selected', recovered.get_data(as_text=True))
                self.assertEqual(read_json(target)[0]["status"], "succeeded")
                self.assertEqual(Handler.calls, 1)
                self.assertEqual(client.post(form["action"], data=form["fields"], headers=ajax).status_code, 409)
                self.assertEqual(Handler.calls, 1)
                for url in (form["action"], "/api/proxy/diagnostics/retry/d1"):
                    with client.session_transaction() as session:
                        session["token_expire_at"] = 1
                    self.assertEqual(client.post(url, data=form["fields"], headers=ajax).status_code, 401)
                    with client.session_transaction() as session:
                        session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="proxy-csrf")
                    separator = "&" if "?" in url else "?"
                    with patch("fls_manager.auth.query_token_enabled", return_value=False):
                        self.assertEqual(client.post(url + separator + "token=" + TOKEN, data=form["fields"], headers=ajax).status_code, 403)
                self.assertEqual(Handler.calls, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
