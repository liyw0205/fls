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

    def test_install_persists_download_and_command_phases_before_side_effects(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, get_online_install_job
            from fls_manager.online_scripts.install import install_worker

            info = self.register(base_dir)
            phases = []

            def download(**_kwargs):
                phases.append(get_online_install_job("install")["phase"])
                return base_dir / "scripts" / "demo.sh"

            def command(*_args, **_kwargs):
                phases.append(get_online_install_job("install")["phase"])

            with patch("fls_manager.online_scripts.install.download_online_script_logged", side_effect=download), patch(
                "fls_manager.online_scripts.install.run_logged_command", side_effect=command,
            ):
                install_worker("install", self.item("fixture install"))

            self.assertEqual(phases, ["downloading", "running_command"])
            persisted = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))["install"]
            self.assertEqual(persisted["phase"], "completed")
            self.assertEqual(info["status"], "已完成")

    def test_install_log_reports_later_target_and_imported_task_changes(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks, save_tasks
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.online_scripts.install import install_worker
            from fls_manager.online_scripts.source import save_online_script_cache

            info = self.register(base_dir)
            info.update(import_task=True, selected_task_indexes=["1"], enable_task=False)
            item = self.item()
            item["task_cron"] = [{"name": "Daily", "command": "task demo.sh"}]
            save_online_script_cache([item])
            target = base_dir / "scripts" / "demo.sh"

            def download(**_kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("original", encoding="utf-8")
                return target

            with patch("fls_manager.online_scripts.install.download_online_script_logged", side_effect=download), patch(
                "fls_manager.online_scripts.tasks.reload_scheduler",
            ):
                install_worker("install", item, import_task=True, selected_task_indexes=["1"])

            client = app.test_client()
            unchanged = client.get("/api/online-scripts/log/install", headers={"X-Token": TOKEN})
            self.assertEqual(unchanged.status_code, 200)
            self.assertFalse(unchanged.json["consistency"]["changed"])
            target.write_text("changed later", encoding="utf-8")
            tasks = load_tasks()
            tasks[0]["command"] = "edited later"
            save_tasks(tasks)
            changed = client.get("/api/online-scripts/log/install", headers={"X-Token": TOKEN})
            self.assertTrue(changed.json["consistency"]["changed"])
            self.assertEqual(changed.json["consistency"]["target"], "changed")
            self.assertEqual(changed.json["consistency"]["tasks"], "changed")
            page = client.get("/online-scripts/log/install", headers={"X-Token": TOKEN})
            self.assertIn("后来修改", page.get_data(as_text=True))
            self.assertEqual(get_online_install_job("install")["phase"], "completed")

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

    def test_terminal_persistence_failure_is_unknown_and_keeps_install_recovery(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import persist_online_install
            from fls_manager.online_scripts.source import save_online_script_cache

            info = self.register(base_dir)
            save_online_script_cache([self.item()])
            target = base_dir / "scripts" / "demo.sh"
            real_persist = persist_online_install
            calls = 0

            def persist(value):
                nonlocal calls
                calls += 1
                if value.get("running") is False and value.get("status") == "已完成":
                    raise OSError("state disk full")
                return real_persist(value)

            with patch.object(install, "persist_online_install", side_effect=persist), patch.object(
                install, "download_online_script_logged", return_value=target,
            ):
                install.install_worker("install", self.item())

            self.assertEqual(info["status"], "结果未知")
            self.assertIsNone(info["returncode"])
            self.assertIn("终态尚未可靠保存", info["error"])
            snapshot = self.snapshot()
            self.assertEqual(snapshot["status"], "结果未知")
            payload = app.test_client().get(
                "/api/online-scripts/log/install", headers={"X-Token": TOKEN},
            ).get_json()
            self.assertEqual(payload["status"], "结果未知")
            self.assertTrue(payload["recovery"]["available"])

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

    def test_restart_unknown_install_exposes_log_and_retries_with_saved_options(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            item = self.item()
            item["task_cron"] = [{"name": "Daily", "command": "task demo"}]
            from fls_manager.models import save_proxies
            save_proxies([{"id": "proxy-1", "name": "Proxy 1", "type": "http",
                           "host": "127.0.0.1", "port": "8080", "enabled": True}])
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps([item]), encoding="utf-8")

            log_file = base_dir / "log" / "restart-install.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("download started\n", encoding="utf-8")
            snapshot = {
                "id": "restart-install",
                "script_id": "demo",
                "script_name": "Demo",
                "log_file": str(log_file),
                "proxy_id": "proxy-1",
                "import_task": True,
                "enable_task": True,
                "selected_task_indexes": ["1"],
                "force": True,
                "running": True,
                "status": "运行中",
                "start_time": 1,
                "returncode": None,
                "error": "",
                "updated_at": "2026-11-14 04:00:00",
            }
            ONLINE_INSTALL_JOBS_FILE.write_text(
                json.dumps({"restart-install": snapshot}, ensure_ascii=False),
                encoding="utf-8",
            )

            client = app.test_client()
            response = client.get(
                "/api/online-scripts/log/restart-install",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["status"], "结果未知")
            self.assertTrue(payload["recovery"]["available"])
            self.assertIn("/online-scripts/install-retry/restart-install", payload["recovery"]["action"])
            self.assertIn("download started", payload["log"])

            page = client.get(
                "/online-scripts/log/restart-install",
                headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            self.assertIn('action="/online-scripts/install-retry/restart-install"', page)
            self.assertIn("重试安装", page)

            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                retry = client.post(
                    "/online-scripts/install-retry/restart-install",
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(retry.status_code, 302)
            start.assert_called_once()
            kwargs = start.call_args.kwargs
            self.assertEqual(kwargs["proxy_id"], "proxy-1")
            self.assertTrue(kwargs["import_task"])
            self.assertTrue(kwargs["enable_task"])
            self.assertEqual(kwargs["selected_task_indexes"], ["1"])
            self.assertFalse(kwargs["force"])

    def test_install_and_retry_reject_invalid_proxy_without_start_or_new_record(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import save_proxies
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE
            from fls_manager.online_scripts.source import save_online_script_cache

            save_proxies([{"id": "disabled", "name": "Disabled", "type": "http",
                           "host": "127.0.0.1", "port": "8080", "enabled": False}])
            item = self.item()
            save_online_script_cache([item])
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                initial = client.post(
                    "/online-scripts/install/demo",
                    data={"proxy_id": "missing"},
                    headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                )
            self.assertEqual(initial.status_code, 400)
            start.assert_not_called()
            self.assertFalse(ONLINE_INSTALL_JOBS_FILE.exists())

            log_file = base_dir / "log" / "invalid-proxy.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("partial\n", encoding="utf-8")
            original = {
                "id": "invalid-proxy", "script_id": "demo", "script_name": "Demo",
                "log_file": str(log_file), "proxy_id": "disabled", "import_task": False,
                "enable_task": False, "selected_task_indexes": [], "force": False,
                "running": False, "status": "失败", "returncode": 1, "error": "failed",
                "updated_at": "2026-11-18 00:00:00",
            }
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps({"invalid-proxy": original}), encoding="utf-8")
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                retry = client.post(
                    "/online-scripts/install-retry/invalid-proxy",
                    headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                )
            self.assertEqual(retry.status_code, 400)
            start.assert_not_called()
            self.assertEqual(json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))["invalid-proxy"], original)

    def test_retry_unknown_install_reports_deleted_script_without_starting(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            log_file = base_dir / "log" / "missing-script.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("partial install\n", encoding="utf-8")
            snapshot = {
                "id": "missing-script",
                "script_id": "deleted",
                "script_name": "Deleted",
                "log_file": str(log_file),
                "running": True,
                "status": "运行中",
                "start_time": 1,
                "returncode": None,
                "error": "",
                "updated_at": "2026-11-14 04:00:00",
            }
            ONLINE_INSTALL_JOBS_FILE.write_text(
                json.dumps({"missing-script": snapshot}, ensure_ascii=False),
                encoding="utf-8",
            )
            client = app.test_client()

            response = client.post(
                "/online-scripts/install-retry/missing-script",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("脚本已不在本地缓存中", response.get_data(as_text=True))
            self.assertIn("同步在线脚本源", response.get_data(as_text=True))

            payload = client.get(
                "/api/online-scripts/log/missing-script",
                headers={"X-Token": TOKEN},
            ).get_json()
            self.assertFalse(payload["recovery"]["available"])
            self.assertIn("脚本已不在本地缓存中", payload["recovery"]["reason"])

    def test_online_scripts_page_marks_restart_unknown_as_recoverable(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps([self.item()]), encoding="utf-8")
            log_file = base_dir / "log" / "page-unknown.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("state uncertain\n", encoding="utf-8")
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps({
                "page-unknown": {
                    "id": "page-unknown", "script_id": "demo", "script_name": "Demo",
                    "log_file": str(log_file), "running": True, "status": "运行中",
                    "start_time": 1, "returncode": None, "error": "",
                    "updated_at": "2026-11-14 04:00:00",
                },
            }, ensure_ascii=False), encoding="utf-8")

            html = app.test_client().get(
                "/online-scripts", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            self.assertIn("上次安装状态：结果未知", html)
            self.assertIn('action="/online-scripts/install-retry/page-unknown"', html)
            self.assertIn("查看安装日志", html)

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
  flsReadJsonResponse: async response => {
    if(!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  },
  fetch: async () => ({ok: true, status: 200, json: async () => input.payload}),
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

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the polling script")
    def test_log_polling_inserts_recovery_action_for_terminal_unknown(self):
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
  'installStopAction', 'installRecoveryAction', 'log', 'flsLogNewTip']
  .map(id => [id, {textContent: '', style: {}, dataset: {}, innerHTML: ''}]));
const context = {
  window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
  document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id]},
  flsGetCsrfToken: () => 'csrf-token',
  flsReadJsonResponse: async response => {
    if(!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  },
  fetch: async () => ({ok: true, status: 200, json: async () => input.payload}),
  setInterval: () => 1, clearInterval() {},
};
vm.createContext(context);
vm.runInContext(input.script, context);
setImmediate(() => {
  assert.equal(elements.installStatus.textContent, '结果未知');
  assert.match(elements.installRecoveryAction.innerHTML, /install-retry\/install/);
  assert.match(elements.installRecoveryAction.innerHTML, /csrf-token/);
  assert.equal(elements.installRecoveryAction.dataset.ready, '1');
  console.log('terminal recovery polling passed');
});
"""],
                input=json.dumps({"script": script, "payload": {
                    "running": False, "status": "结果未知", "returncode": None,
                    "error": "panel restarted", "log": "uncertain",
                    "recovery": {
                        "available": True,
                        "action": "/online-scripts/install-retry/install",
                    },
                }}),
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    def test_install_history_api_and_page_recover_stale_running_without_starting(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_RUNNING, persist_online_install,
            )

            first = self.register(base_dir, "history-running")
            first["updated_at"] = "2026-11-18 12:00:00"
            persist_online_install(first)
            ONLINE_INSTALL_RUNNING.clear()
            second = self.register(base_dir, "history-completed")
            second.update(running=False, status="已完成", phase="completed", updated_at="2026-11-18 13:00:00")
            persist_online_install(second)
            client = app.test_client()
            history = client.get(
                "/api/online-scripts/install-history?limit=1&script_id=demo",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json["items"][0]["id"], "history-completed")
            stale = client.get(
                "/api/online-scripts/install-history?limit=10&script_id=demo",
                headers={"X-Token": TOKEN},
            )
            stale_info = next(item for item in stale.json["items"] if item["id"] == "history-running")
            self.assertEqual(stale_info["status"], "结果未知")
            page = client.get("/online-scripts/install-history?script_id=demo", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("/online-scripts/log/history-running", page.get_data(as_text=True))

    def test_install_history_rejects_invalid_limit_and_corrupt_record_without_overwrite(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            client = app.test_client()
            invalid = client.get("/api/online-scripts/install-history?limit=0", headers={"X-Token": TOKEN})
            self.assertEqual(invalid.status_code, 400)
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_text("not-json", encoding="utf-8")
            original = ONLINE_INSTALL_JOBS_FILE.read_bytes()
            failed = client.get("/api/online-scripts/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            page = client.get("/online-scripts/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 500)
            self.assertIn("无法读取", page.get_data(as_text=True))
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)

    def test_install_history_clips_whole_retry_chains_and_exposes_recursive_recovery_navigation(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, persist_online_install
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.item()])

            def record(install_id, *, started, updated, retry_of="", retried_by="", status="失败"):
                log_file = base_dir / f"{install_id}.log"
                log_file.write_text(f"log for {install_id}\n", encoding="utf-8")
                return {
                    "id": install_id, "script_id": "demo", "script_name": "Demo",
                    "log_file": str(log_file), "proxy_id": "", "import_task": False,
                    "enable_task": False, "selected_task_indexes": [], "selected_tasks": [],
                    "force": False, "retry_of": retry_of, "retried_by": retried_by,
                    "running": False, "status": status, "phase": "failed",
                    "start_time": started, "returncode": 1, "error": "fixture failure",
                    "scheduler_reload_pending": False, "scheduler_reload_error": "",
                    "updated_at": updated,
                }

            jobs = {}
            old = [
                record("old-attempt", started=1, updated="2026-11-01 00:00:00", retried_by="old-retry"),
                record("old-retry", started=2, updated="2026-11-01 00:01:00", retry_of="old-attempt"),
            ]
            latest = [
                record("latest-attempt", started=200, updated="2026-11-18 20:00:00", retried_by="latest-retry"),
                record("latest-retry", started=201, updated="2026-11-18 20:01:00", retry_of="latest-attempt", retried_by="latest-final"),
                record("latest-final", started=202, updated="2026-11-18 20:02:00", retry_of="latest-retry"),
            ]
            jobs.update((item["id"], item) for item in old + latest)
            for index in range(95):
                item = record(f"filler-{index:03d}", started=10 + index, updated="2026-11-10 00:00:00")
                jobs[item["id"]] = item
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")

            newest = record("new-install", started=1000, updated="2026-11-19 00:00:00")
            persist_online_install(newest)
            stored = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertLessEqual(len(stored), 100)
            self.assertNotIn("old-attempt", stored)
            self.assertNotIn("old-retry", stored)
            self.assertEqual(stored["latest-attempt"]["retried_by"], "latest-retry")
            self.assertEqual(stored["latest-retry"]["retry_of"], "latest-attempt")
            self.assertEqual(stored["latest-retry"]["retried_by"], "latest-final")
            self.assertEqual(stored["latest-final"]["retry_of"], "latest-retry")

            client = app.test_client()
            history = client.get("/api/online-scripts/install-history?limit=100", headers={"X-Token": TOKEN})
            self.assertEqual(history.status_code, 200)
            by_id = {item["id"]: item for item in history.json["items"]}
            self.assertNotIn("old-attempt", by_id)
            self.assertNotIn("old-retry", by_id)
            self.assertEqual(by_id["latest-attempt"]["retry_child_id"], "latest-retry")
            self.assertEqual(by_id["latest-retry"]["retry_parent_id"], "latest-attempt")
            self.assertEqual(by_id["latest-retry"]["retry_child_id"], "latest-final")
            self.assertEqual(by_id["latest-final"]["retry_parent_id"], "latest-retry")
            self.assertTrue(all(not item["retry_relation_error"] for item in by_id.values()))

            page = client.get("/online-scripts/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertNotIn("old-attempt", html)
            self.assertIn('/online-scripts/log/latest-retry?back=/online-scripts/install-history', html)
            log_page = client.get("/online-scripts/log/latest-final", headers={"X-Token": TOKEN})
            log_html = log_page.get_data(as_text=True)
            self.assertEqual(log_page.status_code, 200)
            self.assertIn('/online-scripts/log/latest-retry?back=/online-scripts/install-history', log_html)
            self.assertIn('action="/online-scripts/install-retry/latest-final"', log_html)

    def test_install_history_protects_running_stopping_and_scheduler_pending_retry_chains(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, persist_online_install

            def record(install_id, *, started, updated, retry_of="", retried_by="", status="失败", running=False, pending=False):
                return {
                    "id": install_id, "script_id": "demo", "script_name": "Demo",
                    "log_file": str(base_dir / f"{install_id}.log"), "proxy_id": "",
                    "import_task": False, "enable_task": False,
                    "selected_task_indexes": [], "selected_tasks": [], "force": False,
                    "retry_of": retry_of, "retried_by": retried_by, "running": running,
                    "status": status, "phase": status, "start_time": started,
                    "returncode": None if running else 1, "error": "",
                    "scheduler_reload_pending": pending, "scheduler_reload_error": "",
                    "updated_at": updated,
                }

            jobs = {}
            for prefix, active in (
                ("running", {"status": "运行中", "running": True}),
                ("stopping", {"status": "停止中"}),
                ("reload", {"status": "部分完成", "pending": True}),
            ):
                first = record(f"{prefix}-parent", started=1, updated="2026-11-01", retried_by=f"{prefix}-active")
                second = record(
                    f"{prefix}-active", started=2, updated="2026-11-02", retry_of=f"{prefix}-parent", **active,
                )
                jobs[first["id"]] = first
                jobs[second["id"]] = second
            for index in range(94):
                item = record(f"terminal-{index:03d}", started=10 + index, updated="2026-10-01")
                jobs[item["id"]] = item
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")

            persist_online_install(record("new-terminal", started=1000, updated="2026-11-19"))
            stored = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(len(stored), 100)
            for prefix in ("running", "stopping", "reload"):
                self.assertIn(f"{prefix}-parent", stored)
                self.assertIn(f"{prefix}-active", stored)
            self.assertTrue(stored["reload-active"]["scheduler_reload_pending"])
            self.assertTrue(any(f"terminal-{index:03d}" not in stored for index in range(94)))

    def test_install_history_capacity_conflict_has_no_receipt_or_install_side_effect(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RUNNING

            jobs = {}
            for index in range(100):
                install_id = f"active-{index:03d}"
                jobs[install_id] = {
                    "id": install_id, "script_id": f"other-{index}", "running": True,
                    "status": "运行中", "updated_at": f"2026-11-{index % 28 + 1:02d}",
                }
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs), encoding="utf-8")
            before = ONLINE_INSTALL_JOBS_FILE.read_bytes()
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.get_online_script", return_value=self.item()), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
            ) as start:
                response = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": "9" * 32},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.json["ok"])
            self.assertIn("未启动", response.json["msg"])
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), before)
            self.assertNotIn("9" * 32, ONLINE_INSTALL_RUNNING)
            start.assert_not_called()

    def test_retry_capacity_conflict_does_not_claim_parent_or_launch_download(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RUNNING
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.item()])
            jobs = {
                "retry-parent": {
                    "id": "retry-parent", "script_id": "demo", "script_name": "Demo",
                    "log_file": str(base_dir / "retry-parent.log"), "retry_of": "", "retried_by": "",
                    "running": False, "status": "失败", "phase": "failed", "start_time": 1,
                    "returncode": 1, "error": "fixture failure", "updated_at": "2026-11-19",
                },
            }
            for index in range(99):
                install_id = f"active-{index:03d}"
                jobs[install_id] = {
                    "id": install_id, "script_id": f"other-{index}", "running": True,
                    "status": "运行中", "updated_at": "2026-11-18",
                }
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
            before = ONLINE_INSTALL_JOBS_FILE.read_bytes()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(
                    token=TOKEN, token_expire_at=int(time.time()) + 3600,
                    security_verified=True, csrf_token="retry-capacity-csrf",
                )
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = client.post(
                    "/online-scripts/install-retry/retry-parent",
                    data={"csrf_token": "retry-capacity-csrf"},
                    headers={"X-Requested-With": "FLS-Ajax"},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("未启动下载或安装进程", response.get_data(as_text=True))
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), before)
            self.assertFalse(json.loads(before)["retry-parent"].get("retried_by"))
            self.assertFalse(any(info.get("running") for info in ONLINE_INSTALL_RUNNING.values()))
            start.assert_not_called()

    def test_damaged_online_retry_relation_is_visible_and_blocks_retry(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.item()])

            parent = {
                "id": "broken-parent", "script_id": "demo", "script_name": "Demo",
                "log_file": str(base_dir / "broken-parent.log"), "retry_of": "",
                "retried_by": "missing-child", "running": False, "status": "失败",
                "phase": "failed", "start_time": 1, "returncode": 1,
                "error": "fixture failure", "updated_at": "2026-11-19",
            }
            (base_dir / "broken-parent.log").write_text("failed\n", encoding="utf-8")
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps({parent["id"]: parent}), encoding="utf-8")
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(
                    token=TOKEN, token_expire_at=int(time.time()) + 3600,
                    security_verified=True, csrf_token="broken-chain-csrf",
                )
            history = client.get("/api/online-scripts/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(history.status_code, 200)
            self.assertIn("缺失或不匹配", history.json["items"][0]["retry_relation_error"])
            page = client.get("/online-scripts/log/broken-parent", headers={"X-Token": TOKEN})
            self.assertIn("已禁用重试", page.get_data(as_text=True))
            script_page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            script_html = script_page.get_data(as_text=True)
            self.assertIn("已禁用重试", script_html)
            self.assertNotIn('action="/online-scripts/install-retry/broken-parent"', script_html)
            original = ONLINE_INSTALL_JOBS_FILE.read_bytes()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = client.post(
                    "/online-scripts/install-retry/broken-parent",
                    data={"csrf_token": "broken-chain-csrf"},
                    headers={"X-Requested-With": "FLS-Ajax"},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("已禁用重试", response.get_data(as_text=True))
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)
            start.assert_not_called()

    def test_install_log_and_history_reject_uncontrolled_paths_and_symlink_storage(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            client = app.test_client()
            outside = base_dir.parent / f"{base_dir.name}-outside-install.log"
            outside.write_text("outside secret\n", encoding="utf-8")
            record = {
                "id": "unsafe-install",
                "script_id": "demo",
                "script_name": "Demo",
                "log_file": str(outside),
                "running": False,
                "status": "失败",
                "start_time": "not-a-number",
                "returncode": 1,
                "error": "failed",
                "updated_at": "2026-11-18 14:00:00",
            }
            ONLINE_INSTALL_JOBS_FILE.write_text(
                json.dumps({"unsafe-install": record}, ensure_ascii=False),
                encoding="utf-8",
            )
            original = outside.read_bytes()
            response = client.get(
                "/api/online-scripts/log/unsafe-install",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json["status"], "失败")
            self.assertIn("受控日志目录", response.json["error"])
            self.assertEqual(outside.read_bytes(), original)
            history = client.get(
                "/api/online-scripts/install-history?script_id=demo",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json["items"][0]["id"], "unsafe-install")

            target = base_dir / "real-install.log"
            target.write_text("target\n", encoding="utf-8")
            link = base_dir / "unsafe-install-link.log"
            link.symlink_to(target)
            record["log_file"] = str(link)
            ONLINE_INSTALL_JOBS_FILE.write_text(
                json.dumps({"unsafe-install": record}, ensure_ascii=False),
                encoding="utf-8",
            )
            linked = client.get(
                "/api/online-scripts/log/unsafe-install",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(linked.status_code, 500)
            self.assertIn("受控日志目录", linked.json["error"])
            self.assertEqual(target.read_text(encoding="utf-8"), "target\n")

            jobs_target = base_dir.parent / f"{base_dir.name}-jobs-target.json"
            jobs_target.write_text("{}", encoding="utf-8")
            jobs_link = ONLINE_INSTALL_JOBS_FILE.with_name("online-install-jobs-link.json")
            jobs_link.symlink_to(jobs_target)
            ONLINE_INSTALL_JOBS_FILE.unlink()
            jobs_link.rename(ONLINE_INSTALL_JOBS_FILE)
            corrupt = client.get(
                "/api/online-scripts/install-history",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(corrupt.status_code, 500)
            self.assertIn("符号链接", corrupt.json["msg"])
            self.assertEqual(jobs_target.read_text(encoding="utf-8"), "{}")
            outside.unlink()
            jobs_target.unlink()


if __name__ == "__main__":
    unittest.main()
