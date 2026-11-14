import contextlib
import json
import os
import sys
import tempfile
import threading
import unittest
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


if __name__ == "__main__":
    unittest.main()
