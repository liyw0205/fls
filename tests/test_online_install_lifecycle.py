import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineInstallLifecycleTests(unittest.TestCase):
    def register(self, base_dir, install_id="install"):
        from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

        info = {
            "id": install_id, "script_id": "demo", "script_name": "Demo",
            "log_file": str(base_dir / f"{install_id}.log"), "running": True,
            "status": "准备中", "start_time": time.time(), "returncode": None,
            "error": "", "process": None,
        }
        ONLINE_INSTALL_RUNNING[install_id] = info
        return info

    def item(self, command=""):
        return {
            "id": "demo", "name": "Demo", "type": "raw",
            "link": "https://example.invalid/demo.sh", "link_name": "demo.sh",
            "install": command,
        }

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Install did not reach the expected state within the deadline")
            time.sleep(0.01)

    def snapshot(self, install_id="install"):
        from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

        data = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("process", data[install_id])
        self.assertNotIn("stop_lock", data[install_id])
        return data[install_id]

    @unittest.skipIf(os.name == "nt", "Installation commands use sh")
    def test_real_command_success_and_nonzero_exit_survive_restart(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING
            from fls_manager.online_scripts.install import install_worker

            for code, status in ((0, "已完成"), (17, "失败")):
                with self.subTest(code=code):
                    info = self.register(base_dir)
                    with patch("fls_manager.online_scripts.install.download_online_script_logged"):
                        install_worker("install", self.item(f"exit {code}"))
                    self.assertFalse(info["running"])
                    self.assertEqual(info["status"], status)
                    self.assertEqual(info["returncode"], code)
                    self.assertEqual(self.snapshot()["returncode"], code)
                    ONLINE_INSTALL_RUNNING.clear()
                    response = app.test_client().get(
                        "/api/online-scripts/log/install", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json["status"], status)
                    self.assertEqual(response.json["returncode"], code)
                    if code:
                        self.assertIn("17", response.json["error"])
                        self.assertIn("17", app.test_client().get(
                            "/online-scripts/log/install", headers={"X-Token": TOKEN},
                        ).get_data(as_text=True))

    @unittest.skipIf(os.name == "nt", "Installation commands use sh")
    def test_real_stop_worker_finishes_while_stop_request_waits(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.install import install_worker, terminate_install_process

            info = self.register(base_dir)
            ready = base_dir / "ready"
            code = f"import pathlib,time; pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
            command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}"
            worker = threading.Thread(target=install_worker, args=("install", self.item(command)))
            original_terminate = terminate_install_process

            def terminate_and_wait_for_worker(proc):
                result = original_terminate(proc)
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive(), "Stop request held the lifecycle lock")
                self.assertEqual(self.snapshot()["status"], "已停止")
                return result

            with patch("fls_manager.online_scripts.install.download_online_script_logged"), patch(
                "fls_manager.online_scripts.install.terminate_install_process",
                side_effect=terminate_and_wait_for_worker,
            ):
                worker.start()
                try:
                    self.wait_for(ready.exists)
                    response = app.test_client().post(
                        "/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(info["status"], "已停止")
                    self.assertEqual(info["returncode"], -signal.SIGTERM)
                    self.assertIsNone(info["process"])
                finally:
                    proc = info.get("process")
                    if proc and proc.poll() is None:
                        original_terminate(proc)
                    worker.join(timeout=3)
            self.assertFalse(worker.is_alive())

    @unittest.skipIf(os.name == "nt", "Installation commands use sh")
    def test_real_timeout_escalates_to_kill_and_persists_actual_exit(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.install import install_worker

            info = self.register(base_dir)
            code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            command = f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)}"
            with patch("fls_manager.online_scripts.install.download_online_script_logged"), patch(
                "fls_manager.online_scripts.install.ONLINE_INSTALL_TIMEOUT_SECONDS", 0.3,
            ):
                install_worker("install", self.item(command))
            self.assertEqual(info["status"], "超时")
            self.assertFalse(info["running"])
            self.assertEqual(info["returncode"], -signal.SIGKILL)
            self.assertIn("0.3", info["error"])
            self.assertEqual(self.snapshot()["status"], "超时")
            response = app.test_client().post(
                "/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(info["status"], "超时")

    def test_stop_failure_keeps_live_process_blocks_restart_and_recovers(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.install import install_worker

            info = self.register(base_dir)
            exited = threading.Event()
            proc = SimpleNamespace(
                pid=4321, returncode=-9,
                poll=lambda: -9 if exited.is_set() else None,
                wait=Mock(side_effect=subprocess.TimeoutExpired("install", 1)),
                terminate=Mock(side_effect=PermissionError("terminate denied")),
                kill=Mock(side_effect=PermissionError("kill denied")),
            )
            worker = threading.Thread(target=install_worker, args=("install", self.item("sleep 30")))
            with patch("fls_manager.online_scripts.install.download_online_script_logged"), patch(
                "fls_manager.online_scripts.install.subprocess.Popen", return_value=proc,
            ), patch("fls_manager.online_scripts.install.os.killpg", side_effect=PermissionError("group denied")), patch(
                "fls_manager.online_scripts.install.os.getpgid", return_value=4321,
            ):
                worker.start()
                try:
                    self.wait_for(lambda: info.get("process") is proc)
                    client = app.test_client()
                    response = client.post("/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("kill denied", response.json["msg"])
                    self.assertTrue(info["running"])
                    self.assertIs(info["process"], proc)
                    self.assertEqual(self.snapshot()["status"], "停止失败")
                    page = client.post("/online-scripts/install-stop/install", headers={"X-Token": TOKEN})
                    self.assertEqual(page.status_code, 409)
                    self.assertIn("group denied", page.get_data(as_text=True))
                    with patch("fls_manager.routes.online_scripts.install.get_online_script", return_value=self.item()), patch(
                        "fls_manager.routes.online_scripts.install.start_install_thread",
                    ) as start:
                        duplicate = client.post("/online-scripts/install/demo", data={"force": "1"}, headers={"X-Token": TOKEN})
                        self.assertEqual(duplicate.status_code, 302)
                        start.assert_not_called()
                finally:
                    exited.set()
                    worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(info["status"], "已停止")
            self.assertEqual(info["returncode"], -9)
            self.assertIn("kill denied", self.snapshot()["error"])

    def test_failure_during_stop_is_not_misreported_as_cancelled(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts.install import install_worker, request_stop_online_install

            info = self.register(base_dir)

            def download(**_kwargs):
                self.assertTrue(request_stop_online_install("install")[0])
                raise RuntimeError("TLS peer closed connection")

            with patch("fls_manager.online_scripts.install.download_online_script_logged", side_effect=download):
                install_worker("install", self.item())
            self.assertEqual(info["status"], "失败")
            self.assertIsNone(info["returncode"])
            self.assertEqual(self.snapshot()["error"], "TLS peer closed connection")

    def test_stop_before_worker_start_is_persisted_and_never_downloads(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING
            from fls_manager.online_scripts.install import install_worker

            info = self.register(base_dir)
            client = app.test_client()
            for _ in range(2):
                response = client.post("/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200)
            self.assertEqual(self.snapshot()["status"], "停止中")
            with patch("fls_manager.online_scripts.install.download_online_script_logged") as download:
                install_worker("install", self.item())
                download.assert_not_called()
            self.assertEqual(info["status"], "已停止")
            self.assertIsNone(info["returncode"])
            ONLINE_INSTALL_RUNNING.clear()
            response = client.get("/api/online-scripts/log/install", headers={"X-Token": TOKEN})
            self.assertEqual(response.json["status"], "已停止")
            self.assertEqual(client.post(
                "/online-scripts/install-stop/install", headers={"X-Token": TOKEN},
            ).status_code, 409)

    def test_stop_request_storage_failure_returns_json_and_does_not_cancel(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_STOPPING

            info = self.register(base_dir)
            with patch("fls_manager.online_scripts.install.persist_online_install", side_effect=OSError("read only")):
                response = app.test_client().post(
                    "/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 500)
            self.assertIn("read only", response.json["msg"])
            self.assertNotIn("install", ONLINE_INSTALL_STOPPING)
            self.assertEqual(info["status"], "准备中")

    def test_concurrent_starts_register_exactly_one_worker(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            barrier = threading.Barrier(2)
            responses, errors = [], []

            def log_file(*_args):
                barrier.wait(timeout=3)
                return base_dir / "concurrent.log"

            def start_request():
                try:
                    responses.append(app.test_client().post(
                        "/online-scripts/install/demo", data={"force": "1"}, headers={"X-Token": TOKEN},
                    ))
                except Exception as e:
                    errors.append(e)

            with patch("fls_manager.routes.online_scripts.install.get_online_script", return_value=self.item()), patch(
                "fls_manager.routes.online_scripts.install.online_install_log_file", side_effect=log_file,
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                threads = [threading.Thread(target=start_request) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual([r.status_code for r in responses], [302, 302])
                start.assert_called_once()
            self.assertEqual(len(ONLINE_INSTALL_RUNNING), 1)
            self.assertEqual(sum("/online-scripts/log/" in r.headers["Location"] for r in responses), 1)

    def test_start_failure_is_durable_and_allows_retry(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            with patch("fls_manager.routes.online_scripts.install.get_online_script", return_value=self.item()), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread", side_effect=RuntimeError("thread quota exhausted"),
            ):
                response = app.test_client().post(
                    "/online-scripts/install/demo", data={"force": "1"}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(response.status_code, 500)
                self.assertIn("thread quota exhausted", response.get_data(as_text=True))
            install_id, info = next(iter(ONLINE_INSTALL_RUNNING.items()))
            self.assertFalse(info["running"])
            self.assertEqual(self.snapshot(install_id)["status"], "启动失败")
            with patch("fls_manager.routes.online_scripts.install.get_online_script", return_value=self.item()), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
            ) as start:
                retry = app.test_client().post("/online-scripts/install/demo", data={"force": "1"}, headers={"X-Token": TOKEN})
                self.assertEqual(retry.status_code, 302)
                start.assert_called_once()

    @unittest.skipIf(os.name == "nt", "Installation commands use sh")
    def test_http_start_worker_and_poll_report_real_failure_end_to_end(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import install

            threads = []
            original_start = install.start_install_thread

            def start(**kwargs):
                thread = original_start(**kwargs)
                threads.append(thread)
                return thread

            response_body = Mock()
            response_body.iter_content.return_value = [b"echo downloaded\n"]
            with patch("fls_manager.routes.online_scripts.install.get_online_script", return_value=self.item("exit 17")), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread", side_effect=start,
            ), patch.object(install, "requests", SimpleNamespace(get=Mock(return_value=response_body))):
                response = app.test_client().post("/online-scripts/install/demo", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(len(threads), 1)
                threads[0].join(timeout=3)
                self.assertFalse(threads[0].is_alive())
            install_id = response.headers["Location"].split("?")[0].rsplit("/", 1)[1]
            response = app.test_client().get(f"/api/online-scripts/log/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["running"])
            self.assertEqual(response.json["returncode"], 17)
            self.assertIn("退出码：17", response.json["error"])
            self.assertEqual(self.snapshot(install_id)["status"], "失败")

    def test_persistence_failure_does_not_leave_a_running_placeholder(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING
            from fls_manager.online_scripts.install import install_worker

            with patch("fls_manager.routes.online_scripts.install.get_online_script", return_value=self.item()), patch(
                "fls_manager.routes.online_scripts.install.persist_online_install", side_effect=OSError("disk full"),
            ), patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = app.test_client().post("/online-scripts/install/demo", data={"force": "1"}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 500)
                self.assertIn("disk full", response.get_data(as_text=True))
                start.assert_not_called()
            self.assertTrue(all(not info["running"] for info in ONLINE_INSTALL_RUNNING.values()))

            info = self.register(base_dir)
            with patch("fls_manager.online_scripts.install.persist_online_install", side_effect=OSError("disk full")), patch(
                "fls_manager.online_scripts.install.download_online_script_logged",
            ) as download:
                install_worker("install", self.item())
                download.assert_not_called()
            self.assertFalse(info["running"])
            self.assertIn("disk full", info["error"])

    def test_unknown_is_idempotent_preserves_error_and_differs_from_missing(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING, persist_online_install

            info = self.register(base_dir)
            info["error"] = "kill denied"
            persist_online_install(info)
            ONLINE_INSTALL_RUNNING.clear()
            client = app.test_client()
            response = client.post("/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            self.assertIn("结果未知", response.json["msg"])
            first = self.snapshot()
            for _ in range(2):
                response = client.get("/api/online-scripts/log/install", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200)
                self.assertIn("kill denied", response.json["error"])
                self.assertEqual(response.json["status"], "结果未知")
            self.assertEqual(first, self.snapshot())
            for path in ("/api/online-scripts/install-stop/missing", "/online-scripts/install-stop/missing"):
                self.assertEqual(client.post(path, headers={"X-Token": TOKEN}).status_code, 404)
            self.assertEqual(client.get("/online-scripts/log/missing", headers={"X-Token": TOKEN}).status_code, 404)

    def test_retention_keeps_new_results_when_timestamps_are_equal(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, persist_online_install

            with patch("fls_manager.online_scripts.constants.ONLINE_INSTALL_LIMIT", 3):
                for index in range(4):
                    info = self.register(base_dir, f"install-{index}")
                    info.update(running=False, status="已完成", returncode=0, updated_at="2026-11-14 12:00:00")
                    persist_online_install(info)
            snapshots = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(set(snapshots), {"install-1", "install-2", "install-3"})

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the polling script")
    def test_log_polling_keeps_terminal_error_and_exit_code_visible(self):
        with isolated_app() as (app, base_dir):
            self.register(base_dir)
            response = app.test_client().get("/online-scripts/log/install", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
            script = next(script for script in scripts if "async function loadLog()" in script)
            result = subprocess.run(
                ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
  'installStopAction', 'log', 'flsLogNewTip'].map(id => [id, {textContent: '', style: {}}]));
const context = {
  window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
  document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id]},
  fetch: async () => ({json: async () => input.payload}),
  setInterval: () => 1, clearInterval() {},
};
vm.createContext(context);
vm.runInContext(input.script, context);
setImmediate(() => {
  assert.equal(elements.installStatus.textContent, '失败');
  assert.equal(elements.installReturncode.textContent, '17');
  assert.equal(elements.installError.textContent, '错误：TLS <peer> failed');
  assert.equal(elements.installStopAction.hidden, true);
  assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, null);
  assert.equal(elements.log.textContent, 'command failed');
  console.log('terminal polling diagnostics passed');
});
"""],
                input=json.dumps({"script": script, "payload": {
                    "running": False, "status": "失败", "returncode": 17,
                    "error": "TLS <peer> failed", "log": "command failed",
                }}),
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
