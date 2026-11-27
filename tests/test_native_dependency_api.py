import contextlib
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "native-dependency-api-token"


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix="fls-native-dependency-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "native-dependency-api-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from fls_manager.app import create_app

        app = create_app()
        try:
            yield app
        finally:
            cleanup_side_effect_state()
            purge_fls_modules()
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def cleanup_side_effect_state():
    state = sys.modules.get("fls_manager.state")
    jobs = getattr(state, "DEPS_RUNNING", {}) if state else {}
    for info in list(jobs.values()):
        stream = info.get("log_fp") if isinstance(info, dict) else None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()
    if state is not None:
        jobs.clear()
        scheduler = getattr(state, "scheduler", None)
        if scheduler is not None:
            with contextlib.suppress(Exception):
                scheduler.shutdown(wait=False)


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class NativeDependencyApiTests(unittest.TestCase):
    headers = {"X-Token": TOKEN}

    def _native_client(self, app, csrf=True):
        from fls_manager.native_sessions import create_native_session

        with app.app_context():
            session_id, csrf_token, _ = create_native_session(
                TOKEN, remember=True, verified=True,
            )
        client = app.test_client()
        client.set_cookie("fls_native_session", session_id)
        headers = {"X-CSRF-Token": csrf_token} if csrf else {}
        return client, headers

    def test_native_session_auth_and_csrf_are_enforced(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            client = app.test_client()
            self.assertEqual(client.get("/api/deps/packages").status_code, 401)
            native, headers = self._native_client(app, csrf=False)
            with patch.object(deps, "refresh_dependency_cache") as refresh:
                missing_csrf = native.post("/api/deps/refresh", json={})
                self.assertEqual(missing_csrf.status_code, 400)
                refresh.assert_not_called()

            native, headers = self._native_client(app)
            with patch.object(
                deps, "refresh_dependency_cache", return_value={"time": "now", "packages": {}}
            ) as refresh:
                accepted = native.post("/api/deps/refresh", json={}, headers=headers)
            self.assertEqual(accepted.status_code, 200)
            refresh.assert_called_once()

    def test_package_inventory_is_bounded_searchable_and_validated(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            output = json.dumps([
                {"name": "Zulu", "version": "2"},
                {"name": "alpha", "version": "1"},
                {"name": "alpine", "version": "3"},
                {"name": "editable-project", "version": ""},
                {"name": "../unsafe", "version": "1"},
            ])
            with patch.object(
                deps, "pip_cmd", return_value=SimpleNamespace(returncode=0, stdout=output)
            ):
                response = app.test_client().get(
                    "/api/deps/packages?q=alp&page=1&limit=1", headers=self.headers
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["total"], 2)
                self.assertEqual(response.json["items"], [{"name": "alpha", "version": "1"}])
                all_packages = app.test_client().get(
                    "/api/deps/packages?q=editable", headers=self.headers
                )
                self.assertEqual(
                    all_packages.json["items"], [{"name": "editable-project", "version": "未知"}]
                )
                invalid = app.test_client().get(
                    "/api/deps/packages?limit=101", headers=self.headers
                )
                self.assertEqual(invalid.status_code, 400)
                clamped = app.test_client().get(
                    "/api/deps/packages?q=missing&page=50&limit=10", headers=self.headers
                )
                self.assertEqual(clamped.status_code, 200)
                self.assertEqual(clamped.json["page"], 1)
                self.assertEqual(clamped.json["items"], [])

    def test_dependency_refresh_returns_structured_core_status(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            value = {"time": "now", "packages": {"flask": "3.0", "PySocks": "不可用"}}
            with patch.object(deps, "refresh_dependency_cache", return_value=value):
                response = app.test_client().post("/api/deps/refresh", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, {"ok": True, **value})

    def test_runtime_status_exposes_native_safe_fields_only(self):
        with isolated_app() as app:
            from fls_manager.routes import status
            from fls_manager.routes import runtime

            items = [{
                "key": "node", "name": "Node.js", "suffix": ".js",
                "command": "/private/bin/node", "status": "error",
                "version": "", "detail": "failed: /private/bin/node", "install_url": "/install/runtime/node",
            }]
            with patch.object(status, "runtime_items", return_value=items), patch.object(
                runtime, "runtime_install_command", side_effect=RuntimeError("/private/bin/node missing")
            ):
                response = app.test_client().get("/api/runtime/status", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["items"], [{
                "key": "node", "name": "Node.js", "suffix": ".js",
                "status": "error", "version": "", "detail": "运行器版本检测失败",
                "installable": False, "install_error": "该运行器不支持当前面板平台的自动安装",
            }])
            self.assertNotIn("/private/bin/node", response.get_data(as_text=True))

    def test_uninstall_job_is_idempotent_and_never_uses_shell(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            client = app.test_client()
            proc = FakeProcess()
            request_body = {"name": "REQUESTS", "request_id": "a" * 32}
            with patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2.0"}]
            ), patch.object(deps, "InstallProcess", return_value=proc) as popen:
                first = client.post("/api/deps/uninstall", json=request_body, headers=self.headers)
                second = client.post("/api/deps/uninstall", json=request_body, headers=self.headers)
                conflict = client.post(
                    "/api/deps/install",
                    json={"name": "requests", "request_id": request_body["request_id"]},
                    headers=self.headers,
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json["operation"], "uninstall")
            self.assertEqual(first.json["package"], "requests")
            self.assertEqual(second.json["install_id"], first.json["install_id"])
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args.args[0][-3:], ["uninstall", "-y", "requests"])
            self.assertEqual(popen.call_args.args[0][0:2], [sys.executable, "-m"])
            self.assertNotIn("shell", popen.call_args.kwargs)
            self.assertEqual(conflict.status_code, 409)

            history = client.get("/api/deps/install-history?kind=dependency", headers=self.headers)
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json["items"][0]["operation"], "uninstall")
            self.assertEqual(history.json["items"][0]["status_label"], "卸载中")
            self.assertIn(
                "重试卸载依赖",
                deps.install_recovery_action({
                    "id": "retry-parent", "kind": "dependency",
                    "operation": "uninstall", "package": "requests",
                }),
            )
            self.assertEqual(
                deps.install_recovery_action({
                    "kind": "dependency", "operation": "uninstall", "package": "requests",
                }),
                "",
            )

    def test_web_uninstall_and_native_replay_share_the_same_job(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            client = app.test_client()
            body = {"name": "REQUESTS", "request_id": "web-uninstall"}
            with patch.object(deps, "pip_cmd", side_effect=AssertionError("no synchronous uninstall")), patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2"}]
            ), patch.object(deps, "InstallProcess", return_value=FakeProcess()) as start:
                first = client.post("/deps/uninstall", data=body, headers=self.headers)
                replay = client.post("/deps/uninstall", data=body, headers=self.headers)
                native = client.post("/api/deps/uninstall", json=body, headers=self.headers)
            self.assertEqual(first.status_code, 302)
            self.assertEqual(first.headers["Location"], "/deps/install-log/web-uninstall?back=/deps")
            self.assertEqual(replay.status_code, 302)
            self.assertEqual(replay.headers["Location"], first.headers["Location"])
            self.assertEqual(native.status_code, 200)
            self.assertEqual(native.json["install_id"], body["request_id"])
            self.assertEqual(native.json["operation"], "uninstall")
            start.assert_called_once()
            self.assertEqual(start.call_args.args[0][-3:], ["uninstall", "-y", "requests"])

    def test_web_uninstall_rejects_invalid_stale_and_conflicting_submissions(self):
        with isolated_app() as app:
            from fls_manager.install_records import persist_install_job
            from fls_manager.routes import deps

            persist_install_job({
                "id": "busy-package", "kind": "dependency", "package": "requests",
                "operation": "install", "running": False, "finished": True,
                "status": "unknown", "returncode": None, "updated_at": "now",
                "retry_confirmation_required": True,
            })
            with patch.object(deps, "pip_cmd", side_effect=AssertionError("no synchronous uninstall")), patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2"}]
            ), patch.object(deps, "InstallProcess") as start:
                for name, status in (("", 400), ("--help", 400), ("<invalid>", 400), ("missing", 404), ("requests", 409)):
                    with self.subTest(name=name):
                        response = app.test_client().post("/deps/uninstall", data={
                            "name": name, "request_id": "web-uninstall-rejected",
                        }, headers=self.headers)
                        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
                        self.assertEqual(response.mimetype, "text/plain")
                start.assert_not_called()

    def test_web_uninstall_preserves_native_session_csrf(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            client, headers = self._native_client(app, csrf=False)
            with patch.object(deps, "_installed_packages") as inventory, patch.object(deps, "InstallProcess") as start:
                response = client.post("/deps/uninstall", data={"name": "requests", "request_id": "web-csrf"})
                self.assertEqual(response.status_code, 400)
                inventory.assert_not_called()
                start.assert_not_called()

    def test_web_uninstall_keeps_original_log_when_running_receipt_cannot_be_saved(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            persist = deps.persist_install_job

            def fail_running_receipt(info):
                if info.get("status") == "running":
                    raise OSError("receipt unavailable")
                return persist(info)

            body = {"name": "requests", "request_id": "web-receipt-pending"}
            with patch.object(deps, "persist_install_job", side_effect=fail_running_receipt), patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2"}]
            ), patch.object(deps, "InstallProcess", return_value=FakeProcess()) as start:
                client = app.test_client()
                accepted = client.post("/deps/uninstall", data=body, headers=self.headers)
                replay = client.post("/deps/uninstall", data=body, headers=self.headers)
                pending = client.post("/api/deps/uninstall", json=body, headers=self.headers)
            self.assertEqual(accepted.status_code, 302)
            self.assertEqual(replay.headers["Location"], accepted.headers["Location"])
            self.assertIn(body["request_id"], accepted.headers["Location"])
            self.assertEqual(pending.status_code, 503)
            self.assertTrue(pending.json["receipt_pending"])
            self.assertFalse(pending.json["retry_available"])
            start.assert_called_once()

    def test_web_dependency_forms_include_distinct_uninstall_request_ids(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            packages = [{"name": "requests", "version": "2"}, {"name": "Flask", "version": "3"}]
            with patch.object(deps, "pip_cmd", return_value=SimpleNamespace(stdout=json.dumps(packages))):
                response = app.test_client().get("/deps", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            forms = re.findall(r'<form\b[^>]*action="/deps/uninstall"[^>]*>(.*?)</form>', response.get_data(as_text=True), re.S)
            self.assertEqual(len(forms), 2)
            ids = [re.search(r'name="request_id" value="([a-f0-9]{32})"', form) for form in forms]
            self.assertTrue(all(ids))
            self.assertEqual(len({match.group(1) for match in ids}), 2)

    def test_uninstall_rejects_uninstalled_and_invalid_package_names(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            client = app.test_client()
            body = {"name": "requests", "request_id": "b" * 32}
            with patch.object(deps, "_installed_packages", return_value=[]), patch.object(
                deps, "InstallProcess"
            ) as popen:
                absent = client.post("/api/deps/uninstall", json=body, headers=self.headers)
                invalid = client.post(
                    "/api/deps/uninstall",
                    json={"name": "requests;touch /tmp/pwned", "request_id": "c" * 32},
                    headers=self.headers,
                )
            self.assertEqual(absent.status_code, 404)
            self.assertEqual(invalid.status_code, 400)
            popen.assert_not_called()

    def test_uninstall_conflicts_with_an_active_install(self):
        with isolated_app() as app:
            from fls_manager.state import DEPS_RUNNING
            from fls_manager.routes import deps

            DEPS_RUNNING["install-active"] = {
                "id": "install-active", "request_id": "install-active",
                "kind": "dependency", "operation": "install", "package": "Requests>=2.0",
                "log_file": "/tmp/install.log", "running": True, "finished": False,
                "status": "running", "returncode": None, "error": "", "updated_at": "now",
            }
            with patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2.0"}]
            ), patch.object(deps, "InstallProcess") as popen:
                response = app.test_client().post(
                    "/api/deps/uninstall",
                    json={"name": "requests", "request_id": "f" * 32},
                    headers=self.headers,
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("已有运行中", response.json["msg"])
            popen.assert_not_called()

    def test_uninstall_blocks_uncertain_package_job_after_panel_restart(self):
        with isolated_app() as app:
            from fls_manager.install_records import persist_install_job
            from fls_manager.routes import deps

            persist_install_job({
                "id": "restart-uninstall", "request_id": "restart-uninstall",
                "kind": "dependency", "operation": "uninstall", "package": "requests",
                "log_file": "/tmp/restart-uninstall.log", "running": True,
                "finished": False, "status": "running", "returncode": None,
                "error": "", "updated_at": "now",
            })
            with patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2.0"}]
            ), patch.object(deps, "InstallProcess") as popen:
                response = app.test_client().post(
                    "/api/deps/uninstall",
                    json={"name": "requests", "request_id": "j" * 32},
                    headers=self.headers,
                )

            self.assertEqual(response.status_code, 409)
            self.assertIn("结果未确认", response.json["msg"])
            popen.assert_not_called()

    def test_install_conflicts_with_an_active_uninstall(self):
        with isolated_app() as app:
            from fls_manager.state import DEPS_RUNNING
            from fls_manager.routes import deps

            DEPS_RUNNING["uninstall-active"] = {
                "id": "uninstall-active", "request_id": "uninstall-active",
                "kind": "dependency", "operation": "uninstall", "package": "requests",
                "log_file": "/tmp/uninstall.log", "running": True, "finished": False,
                "status": "running", "returncode": None, "error": "", "updated_at": "now",
            }
            with patch.object(deps, "InstallProcess") as popen:
                response = app.test_client().post(
                    "/api/deps/install",
                    json={"name": "requests>=2.0", "request_id": "g" * 32},
                    headers=self.headers,
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("已有运行中", response.json["msg"])
            popen.assert_not_called()

    def test_concurrent_uninstall_replays_one_request_and_blocks_another(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            entered_popen = threading.Event()
            release_popen = threading.Event()
            first_response = []

            def start_process(*_args, **_kwargs):
                entered_popen.set()
                if not release_popen.wait(3):
                    raise TimeoutError("test did not release the package process")
                return FakeProcess()

            def submit_first():
                with app.test_client() as client:
                    first_response.append(client.post(
                        "/api/deps/uninstall",
                        json={"name": "requests", "request_id": "1" * 32},
                        headers=self.headers,
                    ))

            with patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2.0"}]
            ), patch.object(deps, "InstallProcess", side_effect=start_process) as popen:
                worker = threading.Thread(target=submit_first)
                worker.start()
                self.assertTrue(entered_popen.wait(2))
                with app.test_client() as client:
                    replay = client.post(
                        "/api/deps/uninstall",
                        json={"name": "REQUESTS", "request_id": "1" * 32},
                        headers=self.headers,
                    )
                    conflict = client.post(
                        "/api/deps/uninstall",
                        json={"name": "requests", "request_id": "2" * 32},
                        headers=self.headers,
                    )
                release_popen.set()
                worker.join(3)

            self.assertFalse(worker.is_alive())
            self.assertEqual(first_response[0].status_code, 200)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json["install_id"], first_response[0].json["install_id"])
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(popen.call_count, 1)

    def test_retry_preserves_uninstall_operation(self):
        with isolated_app() as app:
            from fls_manager.routes import deps

            client = app.test_client()
            processes = [FakeProcess(returncode=1), FakeProcess()]
            with patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2.0"}]
            ), patch.object(deps, "InstallProcess", side_effect=processes) as popen:
                started = client.post(
                    "/api/deps/uninstall",
                    json={"name": "requests", "request_id": "d" * 32},
                    headers=self.headers,
                )
                retried = client.post(
                    f"/api/deps/install-retry/{'d' * 32}",
                    json={"request_id": "e" * 32},
                    headers=self.headers,
                )
            self.assertEqual(started.status_code, 200)
            self.assertEqual(retried.status_code, 200)
            self.assertEqual(retried.json["operation"], "uninstall")
            self.assertEqual(popen.call_count, 2)
            self.assertEqual(popen.call_args.args[0][-3:], ["uninstall", "-y", "requests"])

    def test_retry_does_not_claim_while_another_operation_is_active(self):
        with isolated_app() as app:
            from fls_manager.state import DEPS_RUNNING
            from fls_manager.routes import deps

            client = app.test_client()
            with patch.object(
                deps, "_installed_packages", return_value=[{"name": "requests", "version": "2.0"}]
            ), patch.object(
                deps, "InstallProcess", return_value=FakeProcess(returncode=1)
            ) as popen:
                started = client.post(
                    "/api/deps/uninstall",
                    json={"name": "requests", "request_id": "h" * 32},
                    headers=self.headers,
                )
                DEPS_RUNNING["other-operation"] = {
                    "id": "other-operation", "request_id": "other-operation",
                    "kind": "dependency", "operation": "install", "package": "Requests>=2.0",
                    "log_file": "/tmp/other-operation.log", "running": True, "finished": False,
                    "status": "running", "returncode": None, "error": "", "updated_at": "now",
                }
                retry = client.post(
                    f"/api/deps/install-retry/{'h' * 32}",
                    json={"request_id": "i" * 32},
                    headers=self.headers,
                )

            self.assertEqual(started.json["status"], "failed")
            self.assertEqual(retry.status_code, 409)
            self.assertIn("未提交重试", retry.json["msg"])
            self.assertEqual(popen.call_count, 1)
            history = client.get("/api/deps/install-history", headers=self.headers)
            original = next(item for item in history.json["items"] if item["id"] == "h" * 32)
            self.assertEqual(original["retried_by"], "")


if __name__ == "__main__":
    unittest.main()
