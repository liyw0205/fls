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

    def test_worker_does_not_continue_actions_without_durable_phase_or_output_receipt(self):
        for failure_point in (
            "validating_tasks", "downloading", "running_command", "download_output", "task_output",
            "scheduler_pending",
        ):
            with self.subTest(failure_point=failure_point), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import install
                from fls_manager.online_scripts import tasks as online_tasks
                from fls_manager.online_scripts.constants import persist_online_install
                from fls_manager.online_scripts.source import save_online_script_cache

                item = self.item("exit 0")
                importing = failure_point in (
                    "validating_tasks", "download_output", "task_output", "scheduler_pending",
                )
                if importing:
                    item["task_cron"] = [{"name": "Imported task", "command": "echo imported", "cron": ""}]
                save_online_script_cache([item])
                info = self.register(base_dir)
                info.update(
                    import_task=importing,
                    enable_task=False,
                    selected_task_indexes=["1"] if importing else [],
                    selected_tasks=item.get("task_cron", []) if importing else [],
                )
                target = base_dir / "scripts" / "demo.sh"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("echo downloaded\n", encoding="utf-8")
                real_persist = persist_online_install
                real_import = install.import_task_if_needed
                real_validate = install.validate_online_task_import
                failed = False

                def persist_with_one_failure(value):
                    nonlocal failed
                    should_fail = (
                        not failed
                        and value.get("running")
                        and (
                            (failure_point in ("validating_tasks", "downloading", "running_command")
                             and value.get("phase") == failure_point)
                            or (failure_point == "download_output" and value.get("target_fingerprint"))
                            or (failure_point == "task_output" and value.get("task_snapshot"))
                            or (failure_point == "scheduler_pending"
                                and value.get("scheduler_reload_pending"))
                        )
                    )
                    if should_fail:
                        failed = True
                        raise OSError("checkpoint disk full")
                    return real_persist(value)

                with patch.object(install, "persist_online_install", side_effect=persist_with_one_failure), patch.object(
                    install, "download_online_script_logged", return_value=target,
                ) as download, patch.object(install, "run_logged_command") as command, patch.object(
                    install, "import_task_if_needed", wraps=real_import,
                ) as importer, patch.object(
                    install, "validate_online_task_import", wraps=real_validate,
                ) as validator, patch.object(
                    online_tasks, "reload_scheduler",
                    side_effect=RuntimeError("scheduler unavailable")
                    if failure_point == "scheduler_pending" else None,
                ):
                    install.install_worker(
                        "install",
                        item,
                        import_task=importing,
                        enable_task=False,
                        selected_task_indexes=["1"] if importing else [],
                    )

                expected_download_calls = 0 if failure_point in ("validating_tasks", "downloading") else 1
                self.assertEqual(download.call_count, expected_download_calls)
                self.assertEqual(importer.call_count, 1 if failure_point in ("task_output", "scheduler_pending") else 0)
                self.assertEqual(validator.call_count, 0 if failure_point == "validating_tasks" else int(importing))
                command.assert_not_called()
                self.assertTrue(failed)
                self.assertFalse(info["running"])
                self.assertEqual(info["status"], "失败")
                self.assertIn("保存安装状态失败", info["error"])
                self.assertEqual(self.snapshot()["status"], "失败")
                api = app.test_client().get("/api/online-scripts/log/install", headers={"X-Token": TOKEN})
                self.assertEqual(api.status_code, 200)
                self.assertEqual(api.json["status"], "失败")
                self.assertIn("recovery", api.json, repr(api.json))
                page = app.test_client().get("/online-scripts/log/install", headers={"X-Token": TOKEN})
                self.assertEqual(page.status_code, 200)
                if failure_point == "scheduler_pending":
                    self.assertTrue(api.json["scheduler_reload_pending"])
                    self.assertEqual(api.json["recovery"]["label"], "重新加载调度器")
                    self.assertIn("重新加载调度器", page.get_data(as_text=True))
                elif failure_point in ("download_output", "task_output"):
                    self.assertEqual(api.json["recovery"].get("label"), "仅恢复任务导入", repr(api.json))
                    self.assertIn("仅恢复任务导入", page.get_data(as_text=True))
                else:
                    self.assertEqual(api.json["recovery"]["label"], "重试安装")
                    self.assertIn("重试安装", page.get_data(as_text=True))

    def test_install_log_does_not_report_completed_phase_for_noncompleted_status(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                _load_persisted_online_install, persist_online_install,
            )

            client = app.test_client()
            for index, (status, expected_phase, stored_phase) in enumerate((
                ("结果未知", "unknown", "completed"),
                ("失败", "failed", " completed "),
            )):
                with self.subTest(status=status):
                    install_id = f"{index + 1:032x}"
                    log_file = base_dir / f"{install_id}.log"
                    log_file.write_text("install output\n", encoding="utf-8")
                    persist_online_install({
                        "id": install_id,
                        "script_id": "demo",
                        "script_name": "Demo",
                        "log_file": str(log_file),
                        "running": False,
                        "status": status,
                        "phase": stored_phase,
                        "returncode": None,
                        "error": "receipt mismatch",
                        "start_time": 1,
                        "updated_at": "2026-11-23 00:00:00",
                    })

                    response = client.get(
                        f"/api/online-scripts/log/{install_id}", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json["status"], status)
                    self.assertEqual(response.json["phase"], expected_phase)
                    self.assertIn("不是已完成", response.json["error"])
                    self.assertIn("原回执未修改", response.json["error"])

                    page = client.get(
                        f"/online-scripts/log/{install_id}", headers={"X-Token": TOKEN},
                    )
                    html = page.get_data(as_text=True)
                    self.assertEqual(page.status_code, 200)
                    self.assertIn(f'id="installPhase">{expected_phase}</span>', html)
                    self.assertIn("不是已完成", html)
                    self.assertEqual(_load_persisted_online_install(install_id)["phase"], stored_phase)

            for install_id, status, phase in (
                ("3" * 32, "已完成", "completed"),
                ("4" * 32, "结果未知", "installing"),
            ):
                log_file = base_dir / f"{install_id}.log"
                log_file.write_text("install output\n", encoding="utf-8")
                persist_online_install({
                    "id": install_id,
                    "script_id": "demo",
                    "script_name": "Demo",
                    "log_file": str(log_file),
                    "running": False,
                    "status": status,
                    "phase": phase,
                    "returncode": 0 if phase == "completed" else None,
                    "error": "",
                    "start_time": 1,
                    "updated_at": "2026-11-23 00:00:00",
                })
                response = client.get(
                    f"/api/online-scripts/log/{install_id}", headers={"X-Token": TOKEN},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["phase"], phase)
                if phase == "completed":
                    self.assertNotIn("阶段与状态不一致", response.json["error"])

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

            def stop(**_kwargs):
                if not exited.is_set():
                    raise PermissionError("tree cleanup denied")

            proc = SimpleNamespace(
                pid=4321, returncode=-9,
                poll=lambda: -9 if exited.is_set() else None,
                start=Mock(),
                wait=lambda **_kwargs: -9 if exited.is_set() else None,
                stop=stop,
            )
            worker = threading.Thread(target=install_worker, args=("install", self.item("sleep 30")))
            with patch("fls_manager.online_scripts.install.download_online_script_logged"), patch(
                "fls_manager.online_scripts.install.DirectoryProcess", return_value=proc,
            ):
                worker.start()
                try:
                    self.wait_for(lambda: info.get("process") is proc)
                    client = app.test_client()
                    response = client.post("/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("tree cleanup denied", response.json["msg"])
                    self.assertTrue(info["running"])
                    self.assertIs(info["process"], proc)
                    self.assertEqual(self.snapshot()["status"], "停止失败")
                    page = client.post("/online-scripts/install-stop/install", headers={"X-Token": TOKEN})
                    self.assertEqual(page.status_code, 409)
                    self.assertIn("tree cleanup denied", page.get_data(as_text=True))
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
            self.assertIn("tree cleanup denied", self.snapshot()["error"])

    def test_stop_failure_receipt_write_failure_keeps_query_and_safe_stop_retry(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_STOPPING, persist_online_install

            info = self.register(base_dir)
            stopped = threading.Event()
            process = SimpleNamespace(pid=4321, poll=lambda: 0 if stopped.is_set() else None)
            info["process"] = process
            original_persist = persist_online_install
            fail_once = True
            terminate_calls = 0

            def persist_with_stop_failure(value):
                nonlocal fail_once
                if fail_once and value.get("status") == "停止失败":
                    fail_once = False
                    raise OSError("stop receipt disk full")
                return original_persist(value)

            def terminate(_process):
                nonlocal terminate_calls
                terminate_calls += 1
                if terminate_calls == 1:
                    return False, "permission denied"
                if not stopped.is_set():
                    stopped.set()
                    return True, ""
                return False, "process still running"

            client = app.test_client()
            with patch.object(install, "persist_online_install", side_effect=persist_with_stop_failure), patch.object(
                install, "terminate_install_process", side_effect=terminate,
            ) as terminate_mock:
                first = client.post(
                    "/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN},
                )

                self.assertEqual(first.status_code, 503)
                self.assertFalse(first.json["ok"])
                self.assertTrue(first.json["receipt_confirmation_required"])
                self.assertTrue(first.json["stop_receipt_confirmation_required"])
                self.assertEqual(first.json["state_url"], "/api/online-scripts/log/install?state_only=1")
                self.assertTrue(info["running"])
                self.assertEqual(info["status"], "停止失败")
                self.assertTrue(info["stop_receipt_confirmation_required"])
                self.assertEqual(self.snapshot()["status"], "停止中")

                state = client.get(first.json["state_url"], headers={"X-Token": TOKEN})
                self.assertEqual(state.status_code, 200)
                self.assertEqual(state.json["status"], "停止失败")
                self.assertTrue(state.json["running"])
                self.assertTrue(state.json["persistence_error"])
                self.assertTrue(state.json["stop_receipt_confirmation_required"])

                recovered = client.post(
                    "/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN},
                )
                self.assertEqual(recovered.status_code, 200)
                self.assertTrue(recovered.json["ok"])
                self.assertEqual(terminate_mock.call_count, 2)
                self.assertNotIn("stop_receipt_confirmation_required", info)
                self.assertEqual(self.snapshot()["status"], "停止中")

            with patch.object(install, "download_online_script_logged") as download:
                install.install_worker("install", self.item())
            download.assert_not_called()
            self.assertEqual(info["status"], "已停止")
            self.assertNotIn("install", ONLINE_INSTALL_STOPPING)
            self.assertEqual(self.snapshot()["status"], "已停止")

    def test_stop_failure_receipt_write_failure_html_keeps_csrf_state_recovery(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import persist_online_install

            info = self.register(base_dir)
            info["process"] = SimpleNamespace(pid=4321, poll=lambda: None)
            original_persist = persist_online_install
            terminate = Mock(return_value=(False, "permission denied"))
            calls = {"receipt": 0}

            def fail_stop_receipt(value):
                if value.get("status") == "停止失败":
                    calls["receipt"] += 1
                    raise OSError("stop receipt read only")
                return original_persist(value)

            client = app.test_client()
            with client.session_transaction() as session:
                session.update(
                    token=TOKEN,
                    token_expire_at=int(time.time()) + 3600,
                    security_verified=True,
                    csrf_token="stop-receipt-csrf",
                )

            with patch.object(install, "persist_online_install", side_effect=fail_stop_receipt), patch.object(
                install, "terminate_install_process", terminate,
            ):
                rejected = client.post(
                    "/online-scripts/install-stop/install",
                    data={"csrf_token": "wrong"},
                )
                self.assertEqual(rejected.status_code, 400)
                self.assertEqual(terminate.call_count, 0)

                response = client.post(
                    "/online-scripts/install-stop/install?back=%2Fonline-scripts%2Finstall-history",
                    data={"csrf_token": "stop-receipt-csrf"},
                )
                self.assertEqual(response.status_code, 503)
                page = response.get_data(as_text=True)
                self.assertIn("停止失败状态尚未确认保存", page)
                self.assertIn("查询原安装状态", page)
                self.assertIn("online-scripts/log/install", page)
                self.assertIn("install-history", page)
                self.assertEqual(calls["receipt"], 1)
                self.assertEqual(terminate.call_count, 1)
                self.assertEqual(self.snapshot()["status"], "停止中")

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

    def test_stop_receipt_published_before_storage_error_still_sends_stop_signal(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_STOPPING, persist_online_install

            info = self.register(base_dir)
            info["process"] = SimpleNamespace(pid=4321, poll=lambda: None)
            original_persist = persist_online_install
            raised = False

            def publish_then_raise(value):
                nonlocal raised
                original_persist(value)
                if not raised and value.get("status") == "停止中":
                    raised = True
                    raise OSError("connection dropped after replace")

            with patch.object(install, "persist_online_install", side_effect=publish_then_raise), patch.object(
                install, "terminate_install_process", return_value=(False, "permission denied"),
            ) as terminate:
                response = app.test_client().post(
                    "/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN},
                )

            self.assertTrue(raised)
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.json["ok"])
            self.assertEqual(response.json["status"], "停止失败")
            self.assertTrue(info["running"])
            self.assertEqual(self.snapshot()["status"], "停止失败")
            self.assertIn("install", ONLINE_INSTALL_STOPPING)
            terminate.assert_called_once_with(info["process"])

    def test_stop_log_failure_keeps_committed_stop_request_and_recovers_without_download(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING, ONLINE_INSTALL_STOPPING
            from fls_manager.online_scripts.install import install_worker

            client = app.test_client()
            api_info = self.register(base_dir, "api-stop")
            with patch("fls_manager.online_scripts.install.append_log", side_effect=OSError("log read only")):
                response = client.post(
                    "/api/online-scripts/install-stop/api-stop", headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            self.assertEqual(response.json["install_id"], "api-stop")
            self.assertEqual(response.json["status"], "停止中")
            self.assertIn("停止请求日志写入失败：log read only", response.json["msg"])
            self.assertEqual(response.json["warning"], "停止请求日志写入失败：log read only")
            self.assertIn("api-stop", ONLINE_INSTALL_STOPPING)
            self.assertEqual(self.snapshot("api-stop")["status"], "停止中")
            self.assertEqual(self.snapshot("api-stop")["stop_warning"], "停止请求日志写入失败：log read only")

            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="stop-log-csrf")
            html_info = self.register(base_dir, "html-stop")
            with patch("fls_manager.online_scripts.install.append_log", side_effect=OSError("log read only")):
                form = client.post(
                    "/online-scripts/install-stop/html-stop?back=%2Fonline-scripts%2Finstall-history",
                    data={"csrf_token": "stop-log-csrf"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(form.status_code, 200)
            page = form.get_data(as_text=True)
            self.assertIn("停止请求已提交", page)
            self.assertIn("停止请求日志写入失败：log read only", page)
            self.assertIn("查询原安装状态", page)
            self.assertIn("online-scripts/log/html-stop", page)
            self.assertEqual(self.snapshot("html-stop")["status"], "停止中")
            self.assertEqual(self.snapshot("html-stop")["stop_warning"], "停止请求日志写入失败：log read only")
            self.assertEqual(html_info["status"], "停止中")

            for install_id, info in (("api-stop", api_info), ("html-stop", html_info)):
                with self.subTest(install_id=install_id):
                    state = client.get(
                        f"/api/online-scripts/log/{install_id}?state_only=1", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(state.status_code, 200)
                    self.assertEqual(state.json["install_id"], install_id)
                    self.assertEqual(state.json["status"], "停止中")
                    self.assertEqual(state.json["stop_warning"], "停止请求日志写入失败：log read only")
                    with patch("fls_manager.online_scripts.install.download_online_script_logged") as download:
                        install_worker(install_id, self.item())
                    download.assert_not_called()
                    self.assertEqual(info["status"], "已停止")
                    self.assertNotIn(install_id, ONLINE_INSTALL_STOPPING)

            for install_id in ("api-stop", "html-stop"):
                with self.subTest(reloaded=install_id):
                    ONLINE_INSTALL_RUNNING.pop(install_id, None)
                    state = client.get(
                        f"/api/online-scripts/log/{install_id}?state_only=1", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(state.status_code, 200)
                    self.assertEqual(state.json["status"], "已停止")
                    self.assertEqual(state.json["stop_warning"], "停止请求日志写入失败：log read only")

    def test_stop_log_warning_receipt_failure_does_not_undo_stop(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_STOPPING

            info = self.register(base_dir, "warning-receipt")
            client = app.test_client()
            persist = install.persist_online_install
            calls = {"count": 0}

            def fail_warning_receipt(value):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("warning receipt read only")
                return persist(value)

            with patch.object(install, "append_log", side_effect=PermissionError("log read only")), patch.object(
                install, "persist_online_install", side_effect=fail_warning_receipt,
            ):
                response = client.post(
                    "/api/online-scripts/install-stop/warning-receipt", headers={"X-Token": TOKEN},
                )

            self.assertEqual(calls["count"], 2)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(info["status"], "停止中")
            self.assertIn("warning-receipt", ONLINE_INSTALL_STOPPING)
            self.assertIn("停止请求日志写入失败：log read only", response.json["warning"])
            self.assertIn("停止诊断回执保存失败：warning receipt read only", response.json["warning"])
            state = client.get(
                "/api/online-scripts/log/warning-receipt?state_only=1", headers={"X-Token": TOKEN},
            )
            self.assertEqual(state.status_code, 200)
            self.assertIn("停止诊断回执保存失败：warning receipt read only", state.json["stop_warning"])

    def test_stop_html_failure_keeps_log_query_and_history_back_context(self):
        with isolated_app() as (app, _base_dir):
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="stop-context-csrf")
            with patch("fls_manager.routes.online_scripts.install.request_stop_online_install",
                       return_value=(False, "停止失败：permission denied")):
                response = client.post(
                    "/online-scripts/install-stop/missing?back=%2Fonline-scripts%2Finstall-history%3Fscript_id%3Ddemo%26status%3D%E5%A4%B1%E8%B4%A5",
                    data={"csrf_token": "stop-context-csrf"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 404)
            html = response.get_data(as_text=True)
            self.assertIn("permission denied", html)
            self.assertIn("install-history?script_id%3Ddemo", html)
            self.assertIn("查询原安装状态", html)

    def test_stop_state_query_reports_real_states_without_reading_log_or_changing_job(self):
        with isolated_app() as (app, base_dir):
            info = self.register(base_dir, "stop-query")
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.logs._install_log_file") as log_path, patch(
                "fls_manager.routes.online_scripts.logs.tail_file",
            ) as read_log:
                for running, status, code in (
                    (True, "准备中", None), (True, "停止中", None),
                    (True, "停止失败", None), (False, "已停止", -15),
                    (False, "结果未知", None),
                ):
                    with self.subTest(status=status):
                        info.update(running=running, status=status, returncode=code)
                        before = info.copy()
                        response = client.get(
                            "/api/online-scripts/log/stop-query?state_only=1", headers={"X-Token": TOKEN},
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertTrue(response.json["ok"])
                        self.assertEqual(response.json["install_id"], "stop-query")
                        self.assertEqual(response.json["running"], running)
                        self.assertEqual(response.json["status"], status)
                        self.assertEqual(response.json["returncode"], code)
                        self.assertIn("recovery", response.json)
                        self.assertNotIn("log", response.json)
                        self.assertEqual(info, before)
                log_path.assert_not_called()
                read_log.assert_not_called()
            missing = client.get(
                "/api/online-scripts/log/missing?state_only=1", headers={"X-Token": TOKEN},
            )
            self.assertEqual(missing.status_code, 404)
            self.assertFalse(missing.json["ok"])

    def test_stop_query_and_form_require_auth_csrf_and_preserve_history_after_success(self):
        with isolated_app() as (app, base_dir):
            info = self.register(base_dir, "stop-session")
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.request_stop_online_install") as stop:
                self.assertEqual(client.get(
                    "/api/online-scripts/log/stop-session?state_only=1",
                ).status_code, 401)
                with client.session_transaction() as session:
                    session["csrf_token"] = "stop-session-csrf"
                self.assertEqual(client.post(
                    "/api/online-scripts/install-stop/stop-session",
                    headers={"X-CSRF-Token": "stop-session-csrf"},
                ).status_code, 401)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="stop-session-csrf")
                for path in ("/api/online-scripts/install-stop/stop-session",
                             "/online-scripts/install-stop/stop-session"):
                    response = client.post(path, data={"csrf_token": "wrong"})
                    self.assertEqual(response.status_code, 400)
                stop.assert_not_called()
            self.assertEqual(info["status"], "准备中")
            back = "/online-scripts/install-history?script_id=demo&status=失败&limit=1"
            response = client.post(
                "/online-scripts/install-stop/stop-session", query_string={"back": back},
                data={"csrf_token": "stop-session-csrf"},
            )
            self.assertEqual(response.status_code, 302)
            from urllib.parse import parse_qs, urlsplit
            location = urlsplit(response.location)
            self.assertEqual(location.path, "/online-scripts/log/stop-session")
            self.assertEqual(parse_qs(location.query)["back"], [back])
            self.assertEqual(info["status"], "停止中")
            repeat = client.post(
                "/api/online-scripts/install-stop/stop-session",
                headers={"X-CSRF-Token": "stop-session-csrf"},
            )
            self.assertEqual(repeat.status_code, 200)
            state = client.get(repeat.json["state_url"])
            self.assertEqual(state.status_code, 200)
            self.assertEqual(state.json["status"], "停止中")
            self.assertTrue(state.json["running"])

    @unittest.skipIf(os.name == "nt", "Install commands use sh")
    def test_stop_log_page_uses_csrf_api_and_exposes_query_after_request_error(self):
        with isolated_app() as (app, base_dir), patch("fls_manager.app.dispatch_frontend_open_event"):
            info = self.register(base_dir, "stop-log-ui")
            log_file = base_dir / "log" / "stop-log-ui.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("preserved stop output\n", encoding="utf-8")
            info.update(log_file=str(log_file), status="运行中")
            with app.test_client() as client:
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="stop-log-csrf")
                response = client.get(
                    "/online-scripts/log/stop-log-ui?back=%2Fonline-scripts%2Finstall-history%3Fscript_id%3Ddemo",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('data-api-action="/api/online-scripts/install-stop/stop-log-ui"', html)
                self.assertIn('id="installStopQuery"', html)
                api = client.post(
                    "/api/online-scripts/install-stop/stop-log-ui",
                    headers={"X-CSRF-Token": "wrong"},
                )
                self.assertIn(api.status_code, (400, 403))
                self.assertTrue(info["running"])

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

    def test_unknown_recovery_receipt_write_failure_disables_retry_until_query_recovers(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts import constants
            from fls_manager.online_scripts.constants import persist_online_install
            from fls_manager.online_scripts.source import save_online_script_cache

            info = self.register(base_dir)
            save_online_script_cache([self.item()])
            target = base_dir / "scripts" / "demo.sh"
            real_persist = persist_online_install

            def fail_both_terminal_receipts(value):
                if not value.get("running") and value.get("status") in ("已完成", "结果未知"):
                    raise OSError("terminal receipt unavailable")
                return real_persist(value)

            with patch.object(constants, "persist_online_install", side_effect=fail_both_terminal_receipts), patch.object(
                install, "persist_online_install", side_effect=fail_both_terminal_receipts,
            ), patch.object(
                install, "download_online_script_logged", return_value=target,
            ) as download:
                install.install_worker("install", self.item())
                payload = app.test_client().get(
                    "/api/online-scripts/log/install", headers={"X-Token": TOKEN},
                ).get_json()
                self.assertEqual(payload["install_id"], "install")
                self.assertEqual(payload["status"], "结果未知")
                self.assertTrue(payload["persistence_error"])
                self.assertFalse(payload["recovery"]["available"])
                self.assertTrue(os.path.isfile(info["log_file"]))
                self.assertEqual(self.snapshot()["status"], "运行中")
                download.assert_called_once()

            recovered = app.test_client().get(
                "/api/online-scripts/log/install", headers={"X-Token": TOKEN},
            ).get_json()
            self.assertEqual(recovered["status"], "结果未知")
            self.assertNotIn("persistence_error", recovered)
            self.assertTrue(recovered["recovery"]["available"])
            self.assertEqual(self.snapshot()["status"], "结果未知")
            persisted = constants._load_persisted_online_install("install")
            self.assertNotIn("recovery_persistence_error", persisted)
            constants.ONLINE_INSTALL_RUNNING.clear()
            after_restart = app.test_client().get(
                "/api/online-scripts/log/install", headers={"X-Token": TOKEN},
            ).get_json()
            self.assertEqual(after_restart["status"], "结果未知")
            self.assertNotIn("persistence_error", after_restart)
            self.assertTrue(after_restart["recovery"]["available"])
            download.assert_called_once()

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
    def test_missing_log_while_install_running_keeps_stop_and_resumes_polling(self):
        with isolated_app() as (app, base_dir):
            info = self.register(base_dir)
            client = app.test_client()
            response = client.get("/api/online-scripts/log/install", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["ok"])
            self.assertTrue(response.json["running"])
            self.assertEqual(response.json["log_status"], 404)
            self.assertEqual(response.json["status"], "准备中")

            page = client.get("/online-scripts/log/install", headers={"X-Token": TOKEN})
            html = page.get_data(as_text=True)
            script = next(
                value for value in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "async function loadLog(" in value
            )
            result = subprocess.run(
                ["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const names=['installStatus','installPhase','installError','installReturncode','installStopAction',
  'installRecoveryAction','installPollError','installPollLogin','installPollLogs','installPollRetry',
  'installStopQuery','log','flsLogNewTip'];
const elements=Object.fromEntries(names.map(id=>[id,{hidden:id!=='installStopAction',textContent:'',style:{},dataset:{},innerHTML:'',addEventListener(){}}]));
let intervalCalls=0,clearCalls=0;
const context={window:{innerHeight:800,scrollY:0,addEventListener(){},scrollTo(){}},
  document:{documentElement:{scrollHeight:800},getElementById:id=>elements[id],querySelector:()=>null},
  flsReadJsonResponse:async response=>response.json(),
  fetch:async()=>({ok:true,status:200,json:async()=>input.payload}),
  setInterval:()=>{intervalCalls++;return 37;},clearInterval:()=>{clearCalls++;},
};
vm.createContext(context);vm.runInContext(input.script,context);
setImmediate(()=>{
  assert.equal(elements.installStatus.textContent,'运行中');
  assert.equal(elements.installPhase.textContent,'downloading');
  assert.equal(elements.installStopAction.hidden,false);
  assert.equal(elements.installRecoveryAction.hidden,true);
  assert.equal(elements.installPollLogs.hidden,true);
  assert.equal(elements.installPollRetry.hidden,false);
  assert.match(elements.installPollError.textContent,/日志尚未生成/);
  assert.equal(intervalCalls,1);
  assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__,37);
  assert.equal(clearCalls,0);
  console.log('running install with delayed log keeps stop and polling');
});
"""],
                input=json.dumps({"script": script, "payload": {
                    "ok": False, "running": True, "status": "运行中", "phase": "downloading",
                    "log_status": 404, "error": "安装日志尚未生成。", "log": "", "recovery_url": "/logs",
                }}),
                capture_output=True, text=True, encoding="utf-8", timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(info["running"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise install log retry recovery")
    def test_running_install_log_recovers_transient_failures_with_bounded_backoff(self):
        with isolated_app() as (app, base_dir):
            from tests.test_ui_route_components import ROOT

            self.register(base_dir)
            html = app.test_client().get(
                "/online-scripts/log/install", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                value for value in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "async function loadLog(" in value
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[
                source.index("function flsResponseErrorDiagnostic("):
                source.index("async function flsReadTextResponse(")
            ]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const names=['installStatus','installPhase','installError','installReturncode','installStopAction',
  'installRecoveryAction','installPollError','installPollLogin','installPollLogs','installPollRetry',
  'installStopQuery','log','flsLogNewTip'];
const elements=Object.fromEntries(names.map(id=>[id,{hidden:id==='installStopAction',textContent:'',style:{},dataset:{},innerHTML:'',
  addEventListener(name,fn){this[name]=fn;},querySelector(){return null;}}]));
elements.log.textContent='last good output';
const listeners={}; const intervals=new Map(); const timeouts=new Map(); let nextTimer=1; let calls=0;
const response=(status,payload)=>({ok:status>=200&&status<300,status,text:async()=>JSON.stringify(payload)});
const failures=[503,'network',408,429,502,503];
const context={window:{innerHeight:800,scrollY:0,addEventListener(name,fn){listeners[name]=fn;},scrollTo(){}},
  document:{documentElement:{scrollHeight:800},getElementById:id=>elements[id],querySelector:()=>null},
  location:{pathname:'/online-scripts/log/install',search:''},
  fetch:async()=>{calls++;const failure=failures.shift();if(failure==='network')throw new Error('network unavailable');
    return response(failure,{msg:'temporary outage',running:true,status:'运行中'});},
  setInterval(fn,ms){const id=nextTimer++;intervals.set(id,{fn,ms});return id;},
  clearInterval(id){intervals.delete(id);},
  setTimeout(fn,ms){const id=nextTimer++;timeouts.set(id,{fn,ms});return id;},
  clearTimeout(id){timeouts.delete(id);},
};
vm.createContext(context);vm.runInContext(input.reader+input.script,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const fireTimeout=async()=>{const [id,timer]=timeouts.entries().next().value;timeouts.delete(id);timer.fn();await flush();};
(async()=>{
  await flush();
  assert.equal(calls,1);assert.equal(elements.log.textContent,'last good output');
  assert.equal(timeouts.size,1);assert.deepEqual([...timeouts.values()].map(x=>x.ms),[2000]);
  for(const delay of [4000,8000,16000,30000]){
    await fireTimeout();
    assert.equal([...timeouts.values()][0].ms,delay);
  }
  await fireTimeout();
  assert.equal(calls,6);assert.equal(timeouts.size,0);
  assert.equal(elements.installPollRetry.hidden,false);
  assert.match(elements.installPollError.textContent,/自动重试已用尽/);

  context.fetch=async()=>{calls++;return response(200,{ok:true,running:true,status:'运行中',phase:'downloading',log:'recovered output'});};
  await elements.installPollRetry.onclick();await flush();
  assert.equal(elements.log.textContent,'recovered output');
  assert.equal(elements.installPollError.hidden,true);assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__!==null,true);

  const beforePageHide=calls;
  listeners.pagehide();assert.equal(intervals.size,0);assert.equal(timeouts.size,0);
  listeners.pageshow({persisted:false});await flush();assert.equal(calls,beforePageHide);
  listeners.pageshow({persisted:true});await flush();assert.equal(calls,beforePageHide+1);
  assert.equal(intervals.size,1);

  const interval=[...intervals.values()][0];
  context.fetch=async()=>{calls++;return response(503,{ok:false,running:true,status:'运行中',log_status:503,error:'retryable log error'});};
  interval.fn();await flush();
  assert.equal(elements.log.textContent,'recovered output');assert.equal(timeouts.size,1);
  listeners.pagehide();
  assert.equal(timeouts.size,0);assert.equal(intervals.size,0);
  const beforeRestore=calls;
  listeners.pageshow({persisted:false});await flush();assert.equal(calls,beforeRestore);
  listeners.pageshow({persisted:true});await flush();assert.equal(calls,beforeRestore+1);

  context.fetch=async()=>response(500,{ok:false,running:false,status:'记录不可读',error:'temporary receipt read failure'});
  await elements.installPollRetry.onclick();await flush();
  assert.equal(timeouts.size,1);assert.equal([...timeouts.values()][0].ms,2000);
  assert.match(elements.installPollError.textContent,/1\/5/);
  context.fetch=async()=>response(500,{ok:false,install_id:'install',running:false,status:'失败',
    log:'',error:'terminal log read failure'});
  await fireTimeout();
  assert.equal(timeouts.size,0);assert.equal(intervals.size,0);
  assert.equal(elements.log.textContent,'recovered output');
  context.fetch=async()=>response(200,{ok:false,running:false,status:'已完成',phase:'completed',
    log_status:404,log:'',error:'terminal log missing'});
  await elements.installPollRetry.onclick();await flush();
  assert.equal(timeouts.size,0);assert.equal(intervals.size,0);
  assert.equal(elements.log.textContent,'recovered output');
  assert.equal(elements.installPollLogs.hidden,false);

  for(const status of [401,403]){
    const authElements=Object.fromEntries(names.map(id=>[id,{hidden:true,textContent:'',style:{},dataset:{},innerHTML:'',addEventListener(){}}]));
    const authListeners={};let authTimers=0;let authCalls=0;
    const auth={window:{innerHeight:800,scrollY:0,addEventListener(name,fn){authListeners[name]=fn;},scrollTo(){}},
      document:{documentElement:{scrollHeight:800},getElementById:id=>authElements[id],querySelector:()=>null},
      location:{pathname:'/online-scripts/log/install',search:''},
      fetch:async()=>{authCalls++;return response(status,{msg:'login expired'});},
      setInterval:()=>1,clearInterval(){},setTimeout(){authTimers++;return 1;},clearTimeout(){}};
    vm.createContext(auth);vm.runInContext(input.reader+input.script,auth);await flush();
    assert.equal(authTimers,0);assert.equal(authElements.installPollLogin.hidden,false);
    assert.equal(authElements.installPollRetry.hidden,false);
    authListeners.pagehide();authListeners.pageshow({persisted:true});await flush();
    assert.equal(authCalls,1,'permission errors must stay paused after BFCache restore');
  }
  console.log('install log transient retry, cap, manual recovery and page lifecycle passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script": script, "reader": reader}), capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise stop query retry")
    def test_stop_query_retries_original_state_after_lost_post_without_reposting(self):
        with isolated_app() as (app, base_dir):
            from tests.test_ui_route_components import ROOT

            self.register(base_dir)
            html = app.test_client().get(
                "/online-scripts/log/install", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(value for value in re.findall(r"<script>(.*?)</script>", html, re.S)
                          if "async function loadLog(" in value)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("function flsResponseErrorDiagnostic("):
                            source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const names=['installStatus','installPhase','installError','installReturncode','installStopAction',
  'installRecoveryAction','installPollError','installPollLogin','installPollLogs','installPollRetry',
  'installStopQuery','log','flsLogNewTip'];
const elements=Object.fromEntries(names.map(id=>[id,{hidden:false,textContent:'',style:{},dataset:{},innerHTML:'',
  addEventListener(name,fn){this[name]=fn;}}]));
const stopButton={disabled:false};
const stopForm={dataset:{apiAction:'/api/online-scripts/install-stop/install'},querySelector:()=>stopButton,
  addEventListener(name,fn){this[name]=fn;}};
const calls=[],timers=new Map(),listeners={};let nextTimer=1;let oldLog;let stateAttempt=0;
elements.log.textContent='original output';
const response=(status,payload)=>({ok:status>=200&&status<300,status,text:async()=>JSON.stringify(payload)});
const context={window:{innerHeight:800,scrollY:0,scrollTo(){},addEventListener(name,fn){listeners[name]=fn;}},
  document:{documentElement:{scrollHeight:800},getElementById:id=>elements[id],querySelector:()=>stopForm},
  location:{pathname:'/online-scripts/log/install',search:''},flsGetCsrfToken:()=> 'stop-csrf',
  fetch:async(url,options)=>{calls.push({url,options});
    if(url.endsWith('?lines=1600'))return new Promise(resolve=>{oldLog=resolve;});
    if(options&&options.method==='POST')return response(502,{msg:'stop response lost'});
    stateAttempt++;
    if(stateAttempt===1)return response(503,{msg:'temporary status failure'});
    return response(200,{ok:true,install_id:'install',running:false,status:'已停止',phase:'stopped',returncode:-15});},
  setInterval:()=>1,clearInterval(){},
  setTimeout(fn,ms){const id=nextTimer++;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);}};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
vm.createContext(context);vm.runInContext(input.reader+input.script,context);
(async()=>{
  assert.equal(calls.length,1);
  await stopForm.submit({preventDefault(){}});
  assert.equal(calls.length,2);assert.equal(calls[1].options.headers['X-CSRF-Token'],'stop-csrf');
  assert.equal(elements.installStopQuery.hidden,false);
  await elements.installStopQuery.click();
  assert.equal(calls[2].url,'/api/online-scripts/log/install?state_only=1');
  assert.equal(timers.size,1);assert.equal([...timers.values()][0].ms,2000);
  oldLog(response(200,{ok:true,running:true,log:'stale output',status:'运行中'}));await flush();
  assert.equal(elements.log.textContent,'original output');
  const [id,timer]=timers.entries().next().value;timers.delete(id);timer.fn();await flush();
  assert.equal(calls.length,4);assert.equal(calls[3].url,calls[2].url);
  assert.equal(calls.filter(call=>call.options&&call.options.method==='POST').length,1);
  assert.equal(elements.installStatus.textContent,'已停止');assert.equal(elements.installStopAction.hidden,true);
  assert.equal(elements.log.textContent,'original output');assert.equal(timers.size,0);
  assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__,null);
  const beforeRestore=calls.length;listeners.pagehide();listeners.pageshow({persisted:true});await flush();
  assert.equal(calls.length,beforeRestore,'terminal installs must not resume polling after BFCache restore');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script": script, "reader": reader}), capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_log_read_failure_then_recovery_keeps_original_job_and_permissions(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, persist_online_install

            info = self.register(base_dir)
            log_file = base_dir / "install.log"
            log_file.write_text("installer output\n", encoding="utf-8")
            info.update(status="运行中", phase="downloading", log_file=str(log_file))
            persist_online_install(info)
            original = ONLINE_INSTALL_JOBS_FILE.read_bytes()
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.logs.tail_file", return_value="读取日志失败: temporary IO failure"), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
            ) as start:
                failed = client.get("/api/online-scripts/log/install?lines=1600", headers={"X-Token": TOKEN})
                self.assertEqual(failed.status_code, 500)
                self.assertEqual(failed.json["install_id"], "install")
                self.assertTrue(failed.json["running"])
                self.assertIn("temporary IO failure", failed.json["error"])
                self.assertEqual(failed.json["log"], "")
                state = client.get("/api/online-scripts/log/install?state_only=1", headers={"X-Token": TOKEN})
                self.assertEqual(state.status_code, 200)
                self.assertTrue(state.json["running"])
                start.assert_not_called()
            recovered = client.get("/api/online-scripts/log/install?lines=1600", headers={"X-Token": TOKEN})
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.json["log"], "installer output")
            self.assertEqual(recovered.json["install_id"], "install")
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)
            with patch("fls_manager.routes.online_scripts.logs.get_online_install_job") as read:
                denied = client.get("/api/online-scripts/log/install")
                self.assertEqual(denied.status_code, 401)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    forbidden = client.get("/api/online-scripts/log/install", query_string={"token": TOKEN})
                self.assertEqual(forbidden.status_code, 403)
                read.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the polling script")
    def test_log_polling_keeps_terminal_error_and_exit_code_visible(self):
        with isolated_app() as (app, base_dir):
            self.register(base_dir)
            response = app.test_client().get("/online-scripts/log/install", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
            script = next(script for script in scripts if "async function loadLog(" in script)
            result = subprocess.run(
                ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
  'installStopAction', 'log', 'flsLogNewTip'].map(id => [id, {textContent: '', style: {}}]));
const context = {
  window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
  document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id], querySelector: () => null},
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
                capture_output=True, text=True, encoding="utf-8", timeout=5,
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
            script = next(script for script in scripts if "async function loadLog(" in script)
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
  document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id], querySelector: () => null},
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
                capture_output=True, text=True, encoding="utf-8", timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise stop recovery")
    def test_stop_query_does_not_wait_for_or_apply_an_old_log_request(self):
        with isolated_app() as (app, base_dir):
            self.register(base_dir)
            html = app.test_client().get(
                "/online-scripts/log/install", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                          if "async function loadLog(" in script)
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
    'installStopAction', 'installRecoveryAction', 'installPollError', 'installPollLogin',
    'installPollLogs', 'installPollRetry', 'installStopQuery', 'log', 'flsLogNewTip']
    .map(id => [id, {textContent: '', hidden: false, style: {}, dataset: {},
      addEventListener(name, handler) { this[name] = handler; }}]));
  elements.log.textContent = 'previous output';
  const button = {disabled: false};
  const form = {dataset: {apiAction: '/api/online-scripts/install-stop/install'},
    querySelector: () => button, addEventListener(name, handler) { this[name] = handler; }};
  let resolveOldLog;
  const calls = [];
  let intervals = 0;
  const response = (payload, status = 200) => ({ok: status < 400, status, json: async () => payload});
  const context = {
    window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
    document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id],
      querySelector: () => form},
    flsGetCsrfToken: () => 'stop-csrf', flsReadJsonResponse: async res => {
      const payload = await res.json();
      if(!res.ok) { const error = new Error(payload.msg || 'HTTP ' + res.status); error.status = res.status; error.payload = payload; throw error; }
      return payload;
    },
    fetch: (url, options) => {
      calls.push({url, options});
      if(calls.length === 1) return new Promise(done => { resolveOldLog = done; });
      if(options && options.method === 'POST' && calls.filter(call => call.options && call.options.method === 'POST').length === 1) {
        return Promise.resolve(response({ok: false, install_id: 'install', msg: 'receipt pending',
          receipt_confirmation_required: true, stop_receipt_confirmation_required: true}, 503));
      }
      if(options && options.method === 'POST') return Promise.resolve(response({ok: true, install_id: 'install'}));
      const pending = calls.filter(call => call.options && call.options.method === 'POST').length === 1;
      return Promise.resolve(response({ok: true, install_id: 'install', running: pending,
        persistence_error: pending, stop_receipt_confirmation_required: pending,
        status: pending ? '停止失败' : '已停止', returncode: pending ? null : -15,
        recovery: {available: false}}));
    },
    setInterval: () => { intervals++; return intervals; }, clearInterval() { intervals = Math.max(0, intervals - 1); },
  };
  vm.createContext(context); vm.runInContext(input.script, context);
  assert.equal(calls.length, 1);
  await form.submit({preventDefault() {}});
  assert.equal(calls.length, 3, 'stop state query must not wait for an unrelated log response');
  assert.equal(calls[1].options.headers['X-CSRF-Token'], 'stop-csrf');
  assert.equal(calls[2].url, '/api/online-scripts/log/install?state_only=1');
  assert.equal(elements.installStatus.textContent, '停止失败');
  assert.equal(button.disabled, false, 'confirmed live stop failure remains safely retryable');
  assert.match(elements.installPollError.textContent, /修复存储后可再次提交停止请求/);
  await form.submit({preventDefault() {}});
  assert.equal(calls.length, 5, 'confirmed stop retry must post once and query the original install');
  assert.equal(calls[3].url, '/api/online-scripts/install-stop/install');
  assert.equal(calls[4].url, '/api/online-scripts/log/install?state_only=1');
  assert.equal(elements.installStatus.textContent, '已停止');
  assert.equal(elements.log.textContent, 'previous output');
  assert.equal(elements.installStopAction.hidden, true);
  resolveOldLog(response({running: true, status: '运行中', log: 'outdated response'}));
  await new Promise(done => setImmediate(done));
  assert.equal(elements.installStatus.textContent, '已停止');
  assert.equal(elements.log.textContent, 'previous output');
  assert.equal(intervals, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""], input=json.dumps({"script": script}), capture_output=True, text=True, encoding="utf-8", timeout=5)
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

    def test_install_history_filters_pages_and_preserves_log_return_context(self):
        from urllib.parse import parse_qs, unquote, urlparse

        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            jobs = {}
            for index in range(5):
                install_id = f"page-{index}"
                jobs[install_id] = {
                    "id": install_id, "script_id": "demo" if index < 4 else "other",
                    "script_name": "Demo", "log_file": str(base_dir / f"{install_id}.log"),
                    "running": False, "status": "失败" if index < 3 else "已完成",
                    "phase": "failed", "start_time": 1, "returncode": 1,
                    "error": "fixture", "updated_at": f"2026-11-19 00:00:0{index}",
                    "retry_of": "", "retried_by": "",
                }
            jobs["page-4"]["status"] = "停止失败"
            jobs["page-2"]["retry_of"] = "page-0"
            jobs["page-0"]["retried_by"] = "page-2"
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs), encoding="utf-8")
            client = app.test_client()

            unauthorized = client.get("/api/online-scripts/install-history")
            self.assertEqual(unauthorized.status_code, 401)
            unauthorized_page = client.get("/online-scripts/install-history")
            self.assertEqual(unauthorized_page.status_code, 302)

            first = client.get(
                "/api/online-scripts/install-history?script_id=demo&status=%E5%A4%B1%E8%B4%A5&limit=2&offset=0",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual((first.json["total"], first.json["offset"], first.json["limit"]), (3, 0, 2))
            self.assertTrue(first.json["has_more"])
            self.assertEqual([item["id"] for item in first.json["items"]], ["page-2", "page-1"])
            self.assertEqual(first.json["items"][0]["retry_parent_id"], "page-0")

            second = client.get(
                "/api/online-scripts/install-history?script_id=demo&status=%E5%A4%B1%E8%B4%A5&limit=2&offset=2",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json["total"], 3)
            self.assertEqual([item["id"] for item in second.json["items"]], ["page-0"])
            self.assertEqual(second.json["items"][0]["retry_child_id"], "page-2")
            self.assertFalse(second.json["has_more"])

            stop_failed = client.get(
                "/api/online-scripts/install-history", query_string={"status": "停止失败"},
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(stop_failed.status_code, 200)
            self.assertEqual([item["id"] for item in stop_failed.json["items"]], ["page-4"])

            page = client.get(
                "/online-scripts/install-history?script_id=demo&status=%E5%A4%B1%E8%B4%A5&limit=2&offset=2",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn('value="demo"', html)
            self.assertIn('value="失败" selected', html)
            self.assertIn('value="2" selected', html)
            self.assertIn("当前显示 3–3 / 3 条", html)
            self.assertNotIn("offset=", html)
            log_href = re.search(r'href="([^\"]*online-scripts/log/page-0[^\"]*)"', html).group(1)
            return_url = unquote(parse_qs(urlparse(log_href).query)["back"][0])
            self.assertEqual(
                return_url,
                "/online-scripts/install-history?script_id=demo&status=失败&limit=2&offset=2",
            )

            empty = client.get(
                "/api/online-scripts/install-history?script_id=missing", headers={"X-Token": TOKEN},
            )
            self.assertEqual((empty.status_code, empty.json["total"], empty.json["items"]), (200, 0, []))
            empty_page = client.get(
                "/online-scripts/install-history?script_id=missing", headers={"X-Token": TOKEN},
            )
            self.assertIn("暂无安装历史", empty_page.get_data(as_text=True))

    def test_install_history_api_paginates_legacy_records_and_rejects_changed_snapshot(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            jobs = {}
            for index in range(205):
                install_id = f"legacy-{index:03d}"
                jobs[install_id] = {
                    "id": install_id, "script_id": "demo", "script_name": "Demo",
                    "log_file": str(base_dir / f"{install_id}.log"), "running": False,
                    "status": "已完成", "phase": "completed", "start_time": 205 - index,
                    "returncode": 0, "error": "", "updated_at": f"2026-11-19 00:{index:03d}",
                    "retry_of": "", "retried_by": "",
                }
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs), encoding="utf-8")
            client = app.test_client()
            pages = []
            for offset in (0, 100, 200):
                query = {"limit": 100, "offset": offset}
                if pages:
                    query["snapshot"] = pages[0].json["snapshot"]
                page = client.get(
                    "/api/online-scripts/install-history", query_string=query,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(page.status_code, 200)
                pages.append(page)

            self.assertEqual([page.json["total"] for page in pages], [205, 205, 205])
            self.assertEqual([len(page.json["items"]) for page in pages], [100, 100, 5])
            self.assertEqual([page.json["has_more"] for page in pages], [True, True, False])
            self.assertEqual({page.json["snapshot"] for page in pages}, {pages[0].json["snapshot"]})
            self.assertEqual(
                [item["id"] for page in pages for item in page.json["items"]],
                [f"legacy-{index:03d}" for index in range(205)],
            )

            jobs["legacy-204"]["status"] = "失败"
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs), encoding="utf-8")
            changed = client.get(
                "/api/online-scripts/install-history?limit=100&offset=200&snapshot="
                + pages[0].json["snapshot"], headers={"X-Token": TOKEN},
            )
            self.assertEqual(changed.status_code, 409)
            self.assertIn("变化", changed.json["msg"])

            invalid_snapshot = client.get(
                "/api/online-scripts/install-history?limit=100&snapshot=invalid",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid_snapshot.status_code, 400)

    def test_install_history_rejects_invalid_pagination_filters_without_writing(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_text("{}", encoding="utf-8")
            original = ONLINE_INSTALL_JOBS_FILE.read_bytes()
            client = app.test_client()
            for query in (
                "limit=0", "limit=101", "limit=1.5", "offset=-1", "offset=1000001",
                "offset=1.5", "status=not-a-status", "script_id=" + ("x" * 201),
            ):
                with self.subTest(query=query):
                    response = client.get(
                        "/api/online-scripts/install-history?" + query, headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json["ok"])
                    page = client.get(
                        "/online-scripts/install-history?" + query, headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(page.status_code, 400)
                    self.assertIn("install-history-script", page.get_data(as_text=True))
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)

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
            page = client.get(
                "/online-scripts/install-history?script_id=demo&status=失败&limit=25&offset=25",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(page.status_code, 500)
            html = page.get_data(as_text=True)
            self.assertIn("无法读取", html)
            self.assertIn('value="demo"', html)
            self.assertIn('value="失败" selected', html)
            self.assertIn('value="25" selected', html)
            self.assertIn('offset=25">重新读取', html)
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)

    def test_malformed_task_recovery_receipt_keeps_install_queries_controlled_and_read_only(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RUNNING,
                get_online_install_job, persist_online_install,
            )

            install_id = "a" * 32
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "id": install_id,
                "script_id": "demo",
                "script_name": "Demo",
                "log_file": str(base_dir / "log" / f"{install_id}.log"),
                "running": False,
                "status": "失败",
                "phase": "importing_tasks",
                "import_task": True,
                "selected_task_indexes": ["1"],
                "selected_tasks": [{"name": "Daily", "command": "task demo.sh"}],
                "task_recovery": "truncated-receipt",
            }
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps({install_id: record}), encoding="utf-8")
            original = ONLINE_INSTALL_JOBS_FILE.read_bytes()
            client = app.test_client()

            state = client.get(
                f"/api/online-scripts/log/{install_id}?state_only=1", headers={"X-Token": TOKEN},
            )
            self.assertEqual(state.status_code, 500)
            self.assertEqual(state.json["install_id"], install_id)
            self.assertIn("任务恢复回执", state.json["error"])

            page = client.get(f"/online-scripts/log/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 500)
            self.assertIn(f"/online-scripts/log/{install_id}", page.get_data(as_text=True))

            retry = client.post(
                f"/api/online-scripts/install-task-retry/{install_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(retry.status_code, 500)
            self.assertEqual(retry.json["request_id"], install_id)
            self.assertIn("任务恢复回执", retry.json["msg"])

            denied = client.post(
                f"/online-scripts/install-task-retry/{install_id}", data={"csrf_token": "wrong"},
            )
            self.assertEqual(denied.status_code, 400)
            with client.session_transaction() as session:
                session.update(
                    token=TOKEN, token_expire_at=int(time.time()) + 3600,
                    security_verified=True, csrf_token="install-recovery-csrf",
                )
            form_retry = client.post(
                f"/online-scripts/install-task-retry/{install_id}",
                data={"csrf_token": "install-recovery-csrf"},
            )
            self.assertEqual(form_retry.status_code, 500)
            self.assertIn(f"/online-scripts/log/{install_id}", form_retry.get_data(as_text=True))

            with self.assertRaisesRegex(RuntimeError, "任务恢复回执"):
                persist_online_install({"id": "b" * 32, "script_id": "other"})
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)

            runtime_record = {
                "id": install_id,
                "script_id": "demo",
                "script_name": "Demo",
                "log_file": str(base_dir / "log" / f"{install_id}.log"),
                "running": True,
                "status": "运行中",
                "task_recovery": "truncated-receipt",
            }
            ONLINE_INSTALL_RUNNING[install_id] = runtime_record
            runtime_before = dict(runtime_record)
            runtime_state = client.get(
                f"/api/online-scripts/log/{install_id}?state_only=1", headers={"X-Token": TOKEN},
            )
            self.assertEqual(runtime_state.status_code, 500)
            self.assertEqual(runtime_state.json["install_id"], install_id)
            self.assertIn("任务恢复回执", runtime_state.json["error"])
            runtime_page = client.get(f"/online-scripts/log/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(runtime_page.status_code, 500)
            self.assertIn(f"/online-scripts/log/{install_id}", runtime_page.get_data(as_text=True))
            runtime_retry = client.post(
                f"/api/online-scripts/install-task-retry/{install_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(runtime_retry.status_code, 500)
            self.assertEqual(runtime_retry.json["request_id"], install_id)
            self.assertEqual(runtime_record, runtime_before)
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)
            ONLINE_INSTALL_RUNNING.pop(install_id, None)

            for invalid_recovery in (
                {"pending": "false"},
                {"id": [], "pending": True},
                {"id": "not-an-install-receipt", "pending": False},
                {"id": "c" * 32, "pending": True, "task_baseline_captured": True},
                {"id": "c" * 32, "pending": True, "task_baseline_captured": True, "task_baseline": "bad"},
                {"id": "c" * 32, "pending": False, "confirmation": [], "confirmation_source": "d" * 64},
                {"id": "c" * 32, "pending": False, "confirmation": "published", "confirmation_source": "d" * 64},
                {"pending": True, "publication": []},
                {"id": "c" * 32, "pending": True, "publication": {
                    "keys": ["demo:1:Daily"], "task_ids": ["task"], "single_task": True,
                    "before": "bad", "after": "d" * 64, "before_source": "e" * 64,
                }},
            ):
                with self.subTest(task_recovery=invalid_recovery):
                    record["task_recovery"] = invalid_recovery
                    ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps({install_id: record}), encoding="utf-8")
                    invalid_bytes = ONLINE_INSTALL_JOBS_FILE.read_bytes()
                    with self.assertRaisesRegex(RuntimeError, "任务.*(回执|基线)"):
                        get_online_install_job(install_id)
                    self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), invalid_bytes)

            record["task_recovery"] = {
                "id": "f" * 32, "pending": False, "task_baseline_captured": True,
                "task_baseline": "", "confirmation": "not_published", "confirmation_source": "",
            }
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps({install_id: record}), encoding="utf-8")
            self.assertEqual(get_online_install_job(install_id)["task_recovery"], record["task_recovery"])

    def test_install_history_clips_whole_retry_chains_and_exposes_recursive_recovery_navigation(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RUNNING, persist_online_install,
            )
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
            ONLINE_INSTALL_RUNNING.update((item["id"], dict(item)) for item in old)
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
            self.assertEqual(history.json["total"], len(stored))
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
            self.assertFalse(response.json["accepted"])
            self.assertTrue(response.json["history_full"])
            self.assertEqual(response.json["history_url"], "/online-scripts/install-history")
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
