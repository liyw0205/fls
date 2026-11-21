import json
import re
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class OnlineInstallSchedulerRecoveryTests(unittest.TestCase):
    def unconfirmed_task_import(self, app, base_dir, enable_task=False):
        from fls_manager import storage
        from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, get_online_install_job, persist_online_install

        install_id = self.pending_task_import(base_dir)
        if enable_task:
            info = get_online_install_job(install_id)
            info["enable_task"] = True
            persist_online_install(info)
        write = storage._write_json_unlocked

        def fail_completion(path, data):
            if path == ONLINE_INSTALL_JOBS_FILE and data[install_id].get("phase") == "completed":
                raise PermissionError("completion receipt unavailable")
            return write(path, data)

        with patch("fls_manager.storage._write_json_unlocked", side_effect=fail_completion):
            response = app.test_client().post(f"/api/online-scripts/install-task-retry/{install_id}", headers={"X-Token": TOKEN})
        self.assertEqual(response.status_code, 503)
        return install_id

    def pending_task_import(self, base_dir):
        from fls_manager.online_scripts.constants import persist_online_install
        from fls_manager.online_scripts.install import capture_install_outputs
        from fls_manager.online_scripts.logs import online_install_log_file
        from fls_manager.online_scripts.source import save_online_script_cache

        item = {
            "id": "demo", "name": "Demo", "type": "raw",
            "link": "https://example.invalid/demo.sh", "link_name": "demo.sh",
            "task_cron": [{"name": "Daily", "command": "task demo.sh", "cron": "0 0 * * *"}],
        }
        save_online_script_cache([item])
        install_id = "d" * 32
        target = base_dir / "scripts" / "demo.sh"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("downloaded", encoding="utf-8")
        log_file = online_install_log_file(install_id, item["name"])
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("download completed\n", encoding="utf-8")
        info = {
            "id": install_id, "script_id": item["id"], "script_name": item["name"],
            "log_file": str(log_file), "running": False, "status": "失败",
            "phase": "importing_tasks", "returncode": 1, "error": "scheduler failed",
            "proxy_id": "", "import_task": True, "enable_task": False,
            "selected_task_indexes": ["1"], "selected_tasks": [item["task_cron"][0]],
            "target_path": str(target), "start_time": 1,
        }
        capture_install_outputs(info, target)
        persist_online_install(info)
        return install_id

    def partial_install(self, app, base_dir, command_failure=False):
        from fls_manager.models import load_tasks
        from fls_manager.online_scripts.constants import get_online_install_job
        from fls_manager.online_scripts.install import InstallCommandError, install_worker
        from fls_manager.online_scripts.source import save_online_script_cache

        item = {
            "id": "demo", "name": "Demo", "type": "raw", "link": "https://example.invalid/demo.sh",
            "link_name": "demo.sh", "install": "fixture install",
            "task_cron": [{"name": "Daily", "command": "task demo.sh", "cron": "0 0 * * *"}],
        }
        save_online_script_cache([item])
        with patch("fls_manager.online_scripts.install.download_online_script_logged", return_value=base_dir / "scripts" / "demo.sh") as download, patch(
            "fls_manager.online_scripts.install.run_logged_command",
            side_effect=InstallCommandError("install command failed", returncode=17) if command_failure else None,
        ) as command, patch(
            "fls_manager.online_scripts.tasks.reload_scheduler", side_effect=RuntimeError("scheduler unavailable"),
        ), patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=install_worker):
            response = app.test_client().post(
                "/online-scripts/install/demo", data={"import_task": "1", "enable_task": "1", "task_indexes": "1"},
                headers={"X-Token": TOKEN},
            )
        self.assertEqual(response.status_code, 302)
        install_id = response.headers["Location"].split("?")[0].rsplit("/", 1)[1]
        info = get_online_install_job(install_id)
        self.assertFalse(info["running"])
        self.assertTrue(info["scheduler_reload_pending"])
        self.assertEqual(info["phase"], "failed" if command_failure else "scheduler_reload_pending")
        self.assertEqual(info["status"], "失败" if command_failure else "部分完成")
        self.assertEqual(info["returncode"], 17 if command_failure else 0)
        self.assertEqual([task["name"] for task in load_tasks()], ["Daily"])
        download.assert_called_once()
        command.assert_called_once()
        return install_id

    def test_partial_completion_survives_restart_and_recovers_without_reinstalling(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING, get_online_install_job

            install_id = self.partial_install(app, base_dir)
            ONLINE_INSTALL_RUNNING.clear()
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            payload = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertEqual(payload["status"], "部分完成")
            self.assertEqual(payload["phase"], "scheduler_reload_pending")
            self.assertTrue(payload["scheduler_reload_pending"])
            self.assertIn("scheduler unavailable", payload["error"])
            self.assertEqual(payload["recovery"]["label"], "重新加载调度器")
            action = f"/online-scripts/install-reload/{install_id}"
            self.assertEqual(payload["recovery"]["action"], action)
            for page in (f"/online-scripts/log/{install_id}", "/online-scripts"):
                html = client.get(page, headers=headers).get_data(as_text=True)
                self.assertIn(f'action="{action}"', html)
                self.assertIn('name="csrf_token"', html)
            repeated_install = client.post(f"/online-scripts/install-retry/{install_id}", headers=headers)
            self.assertEqual(repeated_install.status_code, 409)
            self.assertIn(action, repeated_install.get_data(as_text=True))
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler, patch(
                "fls_manager.online_scripts.install.download_online_script_logged",
            ) as download, patch("fls_manager.online_scripts.install.import_task_if_needed") as importer:
                recovered = client.post(action, headers=headers)
            self.assertEqual(recovered.status_code, 302)
            self.assertEqual(recovered.headers["Location"], f"/online-scripts/log/{install_id}")
            reload_scheduler.assert_called_once()
            download.assert_not_called()
            importer.assert_not_called()
            info = get_online_install_job(install_id)
            self.assertEqual(info["status"], "已完成")
            self.assertFalse(info["scheduler_reload_pending"])
            self.assertEqual(info["error"], "")
            self.assertEqual(info["scheduler_reload_error"], "")
            self.assertEqual(len(load_tasks()), 1)
            final_payload = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertNotIn("recovery", final_payload)
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                repeated = client.post(action, headers=headers)
            self.assertEqual(repeated.status_code, 409)
            reload_scheduler.assert_not_called()

    def test_log_errors_preserve_bound_state_recovery_and_saved_tasks(self):
        for failure in ("missing", "boundary", "read"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, get_online_install_job

                install_id = self.partial_install(app, base_dir)
                info = get_online_install_job(install_id)
                log_file = Path(info["log_file"])
                protected = base_dir / "protected.txt"
                protected.write_text("do not read or modify", encoding="utf-8")
                if failure != "read":
                    log_file.unlink()
                if failure == "boundary":
                    log_file.symlink_to(protected)
                original = ONLINE_INSTALL_JOBS_FILE.read_bytes()
                tasks = load_tasks()
                headers = {"X-Token": TOKEN}
                client = app.test_client()
                with patch("fls_manager.routes.online_scripts.logs.tail_file", return_value="读取日志失败: temporary IO error"):
                    response = client.get(f"/api/online-scripts/log/{install_id}", headers=headers)
                self.assertEqual(response.status_code, 200 if failure == "missing" else 500)
                payload = response.json
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["install_id"], install_id)
                self.assertEqual(payload["log_status"], 404 if failure == "missing" else 500)
                self.assertEqual(payload["status"], "部分完成")
                self.assertEqual(payload["phase"], "scheduler_reload_pending")
                self.assertTrue(payload["scheduler_reload_pending"])
                self.assertIn("scheduler unavailable", payload["install_error"])
                self.assertTrue(payload["log_error"])
                self.assertEqual(payload["recovery"]["label"], "重新加载调度器")
                state = client.get(payload["state_url"], headers=headers)
                self.assertEqual(state.status_code, 200)
                self.assertEqual(state.json["status"], "部分完成")
                self.assertEqual(state.json["recovery"], payload["recovery"])
                self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)
                self.assertEqual(load_tasks(), tasks)
                with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload, patch(
                    "fls_manager.online_scripts.install.download_online_script_logged",
                ) as download, patch("fls_manager.online_scripts.install.import_task_if_needed") as importer:
                    restored = client.post(payload["recovery"]["api_action"], headers=headers)
                    repeated = client.post(payload["recovery"]["api_action"], headers=headers)
                self.assertEqual(restored.status_code, 200)
                self.assertEqual(repeated.status_code, 409)
                reload.assert_called_once()
                download.assert_not_called()
                importer.assert_not_called()
                self.assertEqual(load_tasks(), tasks)
                self.assertEqual(protected.read_text(encoding="utf-8"), "do not read or modify")

    def test_task_retry_completion_requires_a_durable_receipt_before_and_after_publication(self):
        for failure in ("before", "after"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager import storage
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RUNNING

                install_id = self.pending_task_import(base_dir)
                write = storage._write_json_unlocked

                def fail_completion(path, data):
                    if path == ONLINE_INSTALL_JOBS_FILE and data[install_id].get("phase") == "completed":
                        if failure == "after":
                            write(path, data)
                        raise PermissionError("completion receipt unavailable")
                    return write(path, data)

                client = app.test_client()
                headers = {"X-Token": TOKEN}
                with patch("fls_manager.storage._write_json_unlocked", side_effect=fail_completion), patch(
                    "fls_manager.online_scripts.tasks.reload_scheduler",
                ) as reload:
                    response = client.post(f"/api/online-scripts/install-task-retry/{install_id}", headers=headers)
                self.assertEqual(response.status_code, 503 if failure == "before" else 200)
                self.assertEqual(response.json["ok"], failure == "after")
                reload.assert_called_once()
                self.assertEqual(len(load_tasks()), 1)
                tasks = (base_dir / "data" / "tasks.json").read_bytes()
                ONLINE_INSTALL_RUNNING.clear()
                state = client.get(f"/api/online-scripts/log/{install_id}?state_only=1", headers=headers)
                if failure == "after":
                    self.assertEqual(state.json["status"], "已完成")
                    continue
                self.assertEqual(state.json["recovery"]["label"], "核实任务导入结果")
                with patch("fls_manager.routes.online_scripts.install.import_task_if_needed") as importer, patch(
                    "fls_manager.routes.online_scripts.install.reload_scheduler",
                ) as reload, patch("fls_manager.online_scripts.install.download_online_script_logged") as download:
                    confirmed = client.post(state.json["recovery"]["api_action"], headers=headers)
                    self.assertEqual(confirmed.status_code, 200)
                    self.assertTrue(confirmed.json["scheduler_reload_required"])
                    replayed = client.post(state.json["recovery"]["api_action"], headers=headers)
                    self.assertEqual(replayed.status_code, 200)
                    self.assertTrue(replayed.json["replayed"])
                    pending = client.get(state.json["state_url"], headers=headers).json
                    self.assertEqual(pending["recovery"]["label"], "重新加载调度器")
                    recovered = client.post(pending["recovery"]["api_action"], headers=headers)
                    self.assertEqual(recovered.status_code, 200)
                    reload.assert_called_once()
                    importer.assert_not_called()
                    download.assert_not_called()
                self.assertEqual((base_dir / "data" / "tasks.json").read_bytes(), tasks)
                self.assertEqual(client.get(state.json["state_url"], headers=headers).json["status"], "已完成")

    def test_task_retry_publication_failures_confirm_actual_files_then_resume_original_install(self):
        for failure in ("evidence_before", "evidence_after", "tasks_before", "tasks_after"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager import storage
                from fls_manager.models import TASK_FILE, load_tasks
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RUNNING

                install_id = self.pending_task_import(base_dir)
                write = storage._write_json_unlocked
                triggered = []

                def fail_publication(path, data):
                    evidence = (path == ONLINE_INSTALL_JOBS_FILE and data[install_id].get("running")
                                and (data[install_id].get("task_recovery") or {}).get("publication"))
                    if (failure.startswith("evidence") and evidence) or (failure.startswith("tasks") and path == TASK_FILE):
                        triggered.append(path)
                        if failure.endswith("after"):
                            write(path, data)
                        raise PermissionError("publication interrupted")
                    return write(path, data)

                client = app.test_client()
                headers = {"X-Token": TOKEN}
                with patch("fls_manager.storage._write_json_unlocked", side_effect=fail_publication), patch(
                    "fls_manager.online_scripts.tasks.reload_scheduler",
                ) as reload:
                    failed = client.post(f"/api/online-scripts/install-task-retry/{install_id}", headers=headers)
                self.assertTrue(triggered)
                self.assertEqual(failed.status_code, 503)
                self.assertTrue(failed.json["receipt_confirmation_required"])
                reload.assert_not_called()
                published = failure == "tasks_after"
                self.assertEqual(len(load_tasks()), 1 if published else 0)
                ONLINE_INSTALL_RUNNING.clear()
                state = client.get(failed.json["state_url"], headers=headers).json
                confirmation = client.post(state["recovery"]["api_action"], headers=headers)
                self.assertEqual(confirmation.status_code, 200)
                self.assertEqual(confirmation.json["confirmation"], "published" if published else "not_published")
                resumed = client.get(failed.json["state_url"], headers=headers).json
                self.assertEqual(resumed["recovery"]["label"], "重新加载调度器" if published else "仅恢复任务导入")
                with patch("fls_manager.online_scripts.install.download_online_script_logged") as download:
                    completed = client.post(resumed["recovery"]["api_action"], headers=headers)
                self.assertEqual(completed.status_code, 200)
                download.assert_not_called()
                self.assertEqual(len(load_tasks()), 1)
                self.assertEqual(client.get(failed.json["state_url"], headers=headers).json["status"], "已完成")

    def test_task_confirmation_api_and_native_form_keep_auth_csrf_and_zero_write_boundaries(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import TASK_FILE
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            install_id = self.unconfirmed_task_import(app, base_dir)
            api = f"/api/online-scripts/install-task-confirm/{install_id}"
            action = f"/online-scripts/install-task-confirm/{install_id}"
            client = app.test_client()
            tasks, receipt = TASK_FILE.read_bytes(), ONLINE_INSTALL_JOBS_FILE.read_bytes()
            csrf = "task-confirm-csrf"

            def login():
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token=csrf)

            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload, patch(
                "fls_manager.routes.online_scripts.install.import_task_if_needed",
            ) as importer, patch("fls_manager.online_scripts.install.download_online_script_logged") as download:
                self.assertEqual(client.post(api).status_code, 400)
                self.assertEqual(client.get(f"/api/online-scripts/log/{install_id}?state_only=1").status_code, 401)
                self.assertEqual(client.post("/api/online-scripts/install-task-confirm/invalid", headers={"X-Token": TOKEN}).status_code, 400)
                self.assertEqual(client.post("/api/online-scripts/install-task-confirm/" + "a" * 32, headers={"X-Token": TOKEN}).status_code, 404)
                self.assertEqual(client.get(api, headers={"X-Token": TOKEN}).status_code, 405)
                login()
                for page in (f"/online-scripts/log/{install_id}", "/online-scripts/install-history", "/online-scripts"):
                    html = client.get(page).get_data(as_text=True)
                    if page == "/online-scripts/install-history":
                        self.assertIn(f"/online-scripts/log/{install_id}", html)
                    else:
                        self.assertIn(action, html)
                        self.assertIn(f'name="csrf_token" value="{csrf}"', html)
                self.assertEqual(client.post(api, headers={"X-CSRF-Token": "wrong"}).status_code, 400)
                self.assertEqual(client.post(action, data={"csrf_token": "wrong"}).status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post(api, headers={"X-CSRF-Token": csrf}).status_code, 401)
                login()
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(action, data={"csrf_token": csrf})
                self.assertEqual(expired.status_code, 302)
                self.assertIn("/login?next=", expired.location)
                login()
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    self.assertEqual(client.post(api, query_string={"token": TOKEN}, headers={"X-CSRF-Token": csrf}).status_code, 403)
                self.assertEqual(TASK_FILE.read_bytes(), tasks)
                self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), receipt)
                login()
                restored = client.post(action, data={"csrf_token": csrf})
                self.assertEqual(restored.status_code, 302)
                self.assertEqual(restored.location, f"/online-scripts/log/{install_id}")
                repeated = client.post(api, headers={"X-CSRF-Token": csrf})
                self.assertEqual(repeated.status_code, 200)
                self.assertTrue(repeated.json["replayed"])
                reload.assert_not_called()
                importer.assert_not_called()
                download.assert_not_called()
                self.assertEqual(TASK_FILE.read_bytes(), tasks)

    def test_task_confirmation_refuses_later_edits_and_unreadable_evidence_without_reimport(self):
        for failure in ("edited", "deleted", "corrupt", "symlink"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager.models import TASK_FILE, load_tasks, save_tasks
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

                install_id = self.unconfirmed_task_import(app, base_dir)
                tasks = load_tasks()
                if failure == "edited":
                    tasks[0]["command"] = "task later-edit.sh"
                    save_tasks(tasks)
                elif failure == "deleted":
                    save_tasks([])
                elif failure == "corrupt":
                    TASK_FILE.write_text("broken json", encoding="utf-8")
                else:
                    protected = base_dir / "protected-tasks.json"
                    protected.write_bytes(TASK_FILE.read_bytes())
                    TASK_FILE.unlink()
                    TASK_FILE.symlink_to(protected)
                original, receipt = TASK_FILE.read_bytes(), ONLINE_INSTALL_JOBS_FILE.read_bytes()
                client = app.test_client()
                headers = {"X-Token": TOKEN}
                with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload, patch(
                    "fls_manager.routes.online_scripts.install.import_task_if_needed",
                ) as importer, patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                    result = client.post(f"/api/online-scripts/install-task-confirm/{install_id}", headers=headers)
                    self.assertEqual(result.status_code, 503)
                    self.assertFalse(result.json["ok"])
                    self.assertTrue(result.json["receipt_confirmation_required"])
                    self.assertEqual(client.post(f"/api/online-scripts/install-task-retry/{install_id}", headers=headers).status_code, 409)
                    self.assertEqual(client.post(f"/api/online-scripts/install-reload/{install_id}", headers=headers).status_code, 409)
                    reinstall = client.post(f"/online-scripts/install-retry/{install_id}", data={"force": "1"}, headers=headers)
                    self.assertEqual(reinstall.status_code, 409)
                    self.assertIn(f"/online-scripts/install-task-confirm/{install_id}", reinstall.get_data(as_text=True))
                    reload.assert_not_called()
                    importer.assert_not_called()
                    start.assert_not_called()
                self.assertEqual(TASK_FILE.read_bytes(), original)
                self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), receipt)

    def test_task_confirmation_receipt_failures_are_retryable_and_check_actual_publication(self):
        for failure in ("before", "after", "symlink", "corrupt"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager import storage
                from fls_manager.models import TASK_FILE
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RUNNING

                install_id = self.unconfirmed_task_import(app, base_dir)
                tasks = TASK_FILE.read_bytes()
                write = storage._write_json_unlocked
                if failure in ("symlink", "corrupt"):
                    ONLINE_INSTALL_RUNNING.clear()
                    if failure == "symlink":
                        protected = base_dir / "protected-receipt.json"
                        protected.write_bytes(ONLINE_INSTALL_JOBS_FILE.read_bytes())
                        ONLINE_INSTALL_JOBS_FILE.unlink()
                        ONLINE_INSTALL_JOBS_FILE.symlink_to(protected)
                    else:
                        ONLINE_INSTALL_JOBS_FILE.write_text("broken json", encoding="utf-8")
                original = ONLINE_INSTALL_JOBS_FILE.read_bytes()

                def fail_confirmation(path, data):
                    if path == ONLINE_INSTALL_JOBS_FILE and (data[install_id].get("task_recovery") or {}).get("confirmation"):
                        if failure == "after":
                            write(path, data)
                        raise PermissionError("confirmation receipt unavailable")
                    return write(path, data)

                client = app.test_client()
                headers = {"X-Token": TOKEN}
                action = f"/api/online-scripts/install-task-confirm/{install_id}"
                with patch("fls_manager.storage._write_json_unlocked", side_effect=fail_confirmation), patch(
                    "fls_manager.routes.online_scripts.install.reload_scheduler",
                ) as reload:
                    failed = client.post(action, headers=headers)
                self.assertEqual(failed.status_code, 200 if failure == "after" else 503)
                reload.assert_not_called()
                self.assertEqual(TASK_FILE.read_bytes(), tasks)
                if failure in ("before", "corrupt", "symlink"):
                    self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)
                if failure == "before":
                    self.assertEqual(client.post(action, headers=headers).status_code, 200)
                elif failure == "after":
                    state = client.get(f"/api/online-scripts/log/{install_id}?state_only=1", headers=headers).json
                    self.assertEqual(state["recovery"]["label"], "重新加载调度器")

    def test_confirmed_tasks_restore_real_scheduler_after_restart_without_source_or_reimport(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import TASK_FILE, load_tasks
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING
            from fls_manager.online_scripts.source import save_online_script_cache
            from fls_manager.scheduler import get_scheduler_state
            from fls_manager.state import scheduler

            install_id = self.unconfirmed_task_import(app, base_dir, enable_task=True)
            task_id = load_tasks()[0]["id"]
            self.assertIsNotNone(scheduler.get_job(f"task_{task_id}"))
            scheduler.remove_all_jobs()
            ONLINE_INSTALL_RUNNING.clear()
            save_online_script_cache([])
            original = TASK_FILE.read_bytes()
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            with patch("fls_manager.routes.online_scripts.install.import_task_if_needed") as importer, patch(
                "fls_manager.online_scripts.install.download_online_script_logged",
            ) as download:
                confirmed = client.post(f"/api/online-scripts/install-task-confirm/{install_id}", headers=headers)
                self.assertEqual(confirmed.status_code, 200)
                self.assertIsNone(scheduler.get_job(f"task_{task_id}"))
                state = client.get(f"/api/online-scripts/log/{install_id}?state_only=1", headers=headers).json
                recovered = client.post(state["recovery"]["action"], headers=headers)
                self.assertEqual(recovered.status_code, 302)
                self.assertIsNotNone(scheduler.get_job(f"task_{task_id}"))
                self.assertEqual(get_scheduler_state()["status"], "succeeded")
                self.assertEqual(get_scheduler_state()["scheduled_count"], 1)
                self.assertEqual(TASK_FILE.read_bytes(), original)
                importer.assert_not_called()
                download.assert_not_called()

    def test_task_publication_and_install_query_have_bounded_lock_order_and_one_import(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            install_id = self.pending_task_import(base_dir)
            entered, release, querying = threading.Event(), threading.Event(), threading.Event()
            publish = constants.persist_online_install_task_recovery
            read = constants._read_install_jobs
            responses, errors = [], []

            def paused_publication(*args, **kwargs):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("publication was not released")
                return publish(*args, **kwargs)

            def observed_read():
                if threading.current_thread().name == "install-query":
                    querying.set()
                return read()

            def request(path, method):
                try:
                    responses.append(getattr(app.test_client(), method)(path, headers={"X-Token": TOKEN}))
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=request, args=(f"/api/online-scripts/install-task-retry/{install_id}", "post"), daemon=True)
            query = threading.Thread(target=request, name="install-query", args=(f"/api/online-scripts/log/{install_id}?state_only=1", "get"), daemon=True)
            with patch("fls_manager.routes.online_scripts.install.persist_online_install_task_recovery", side_effect=paused_publication), patch(
                "fls_manager.online_scripts.constants._read_install_jobs", side_effect=observed_read,
            ), patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload:
                worker.start()
                try:
                    self.assertTrue(entered.wait(3))
                    duplicate = app.test_client().post(f"/api/online-scripts/install-task-retry/{install_id}", headers={"X-Token": TOKEN})
                    self.assertEqual(duplicate.status_code, 409)
                    query.start()
                    self.assertTrue(querying.wait(3))
                finally:
                    release.set()
                    worker.join(4)
                    if query.ident:
                        query.join(4)
                self.assertFalse(worker.is_alive())
                self.assertFalse(query.is_alive())
                self.assertFalse(errors)
                self.assertEqual(sorted(response.status_code for response in responses), [200, 200])
                reload.assert_called_once()

    def test_edit_after_confirmation_blocks_scheduler_reload_without_overwriting_tasks(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import TASK_FILE, load_tasks, save_tasks

            install_id = self.unconfirmed_task_import(app, base_dir)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            self.assertEqual(client.post(f"/api/online-scripts/install-task-confirm/{install_id}", headers=headers).status_code, 200)
            tasks = load_tasks()
            tasks[0]["command"] = "task later.sh"
            save_tasks(tasks)
            original = TASK_FILE.read_bytes()
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload:
                refused = client.post(f"/api/online-scripts/install-reload/{install_id}", headers=headers)
                self.assertEqual(refused.status_code, 503)
                self.assertIn("已变化", refused.json["msg"])
                reload.assert_not_called()
            self.assertEqual(TASK_FILE.read_bytes(), original)

    def test_partial_task_recovery_receipt_failure_keeps_original_scheduler_diagnosis(self):
        for failure in ("before", "after"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager import storage
                from fls_manager.models import TASK_FILE
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RUNNING

                install_id = self.pending_task_import(base_dir)
                write = storage._write_json_unlocked

                def fail_partial(path, data):
                    if path == ONLINE_INSTALL_JOBS_FILE and data[install_id].get("phase") == "scheduler_reload_pending":
                        if failure == "after":
                            write(path, data)
                        raise PermissionError("partial receipt unavailable")
                    return write(path, data)

                client = app.test_client()
                headers = {"X-Token": TOKEN}
                with patch("fls_manager.storage._write_json_unlocked", side_effect=fail_partial), patch(
                    "fls_manager.online_scripts.tasks.reload_scheduler", side_effect=RuntimeError("scheduler unavailable"),
                ):
                    failed = client.post(f"/api/online-scripts/install-task-retry/{install_id}", headers=headers)
                self.assertEqual(failed.status_code, 503)
                self.assertIn("scheduler unavailable", failed.json["msg"])
                original = TASK_FILE.read_bytes()
                ONLINE_INSTALL_RUNNING.clear()
                url = f"/api/online-scripts/log/{install_id}?state_only=1"
                state = client.get(url, headers=headers).json
                if failure == "before":
                    self.assertEqual(state["recovery"]["label"], "核实任务导入结果")
                    self.assertEqual(client.post(state["recovery"]["api_action"], headers=headers).status_code, 200)
                    state = client.get(url, headers=headers).json
                self.assertEqual(state["recovery"]["label"], "重新加载调度器")
                restored = client.post(state["recovery"]["api_action"], headers=headers)
                self.assertEqual(restored.status_code, 200)
                self.assertEqual(client.get(url, headers=headers).json["status"], "已完成")
                self.assertEqual(TASK_FILE.read_bytes(), original)

    def test_concurrent_task_confirmations_have_one_claim_and_idempotent_replay(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.tasks import task_import_publication_state

            install_id = self.unconfirmed_task_import(app, base_dir)
            entered, release = threading.Event(), threading.Event()
            responses = []
            action = f"/api/online-scripts/install-task-confirm/{install_id}"
            headers = {"X-Token": TOKEN}

            def paused_confirmation(record):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("confirmation was not released")
                return task_import_publication_state(record)

            worker = threading.Thread(target=lambda: responses.append(app.test_client().post(action, headers=headers)), daemon=True)
            with patch("fls_manager.routes.online_scripts.install.task_import_publication_state", side_effect=paused_confirmation) as confirm, patch(
                "fls_manager.routes.online_scripts.install.reload_scheduler",
            ) as reload:
                worker.start()
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(app.test_client().post(action, headers=headers).status_code, 409)
                finally:
                    release.set()
                    worker.join(4)
                self.assertFalse(worker.is_alive())
                self.assertEqual([response.status_code for response in responses], [200])
                replayed = app.test_client().post(action, headers=headers)
                self.assertEqual(replayed.status_code, 200)
                self.assertTrue(replayed.json["replayed"])
                confirm.assert_called_once()
                reload.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise receipt confirmation recovery")
    def test_receipt_failure_page_queries_then_confirms_and_reloads_without_reimporting(self):
        with isolated_app() as (app, base_dir):
            install_id = self.pending_task_import(base_dir)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            url = f"/api/online-scripts/log/{install_id}"
            html = client.get(f"/online-scripts/log/{install_id}", headers=headers).get_data(as_text=True)
            initial = client.get(url, headers=headers).json
            with patch("fls_manager.routes.online_scripts.install._save_install_task_result", side_effect=PermissionError("completion unavailable")):
                failed = client.post(initial["recovery"]["api_action"], headers=headers)
            unknown = client.get(url + "?state_only=1", headers=headers).json
            confirmed = client.post(unknown["recovery"]["api_action"], headers=headers)
            pending = client.get(url, headers=headers).json
            reloaded = client.post(pending["recovery"]["api_action"], headers=headers)
            completed = client.get(url, headers=headers).json
            script = next(value for value in re.findall(r"<script>(.*?)</script>", html, re.S) if "async function loadLog(" in value)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("function flsResponseErrorDiagnostic("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements=Object.fromEntries(['installStatus','installPhase','installError','installReturncode','installStopAction',
  'installPollError','installPollLogin','installPollLogs','installPollRetry','installStateQuery','installStopQuery','log','flsLogNewTip']
  .map(id=>[id,{hidden:true,textContent:'',dataset:{},style:{},addEventListener(name,fn){this[name]=fn;}}]));
const recovery={hidden:true,dataset:{},form:null,html:'',set innerHTML(value){this.html=value;
  this.form=value.includes('<form')?{button:{disabled:false},querySelector(){return this.button;},
    addEventListener(name,fn){this[name]=fn;}}:null;},get innerHTML(){return this.html;},querySelector(){return this.form;}};
elements.installRecoveryAction=recovery;
const response=(status,payload)=>({ok:status>=200&&status<300,status,text:async()=>JSON.stringify(payload)});
const replies=[response(200,input.initial),response(503,input.failed),response(503,{msg:'state unavailable'}),
  response(200,input.unknown),response(200,input.confirmed),response(200,input.pending),
  response(200,input.reloaded),response(200,input.completed)];
const calls=[];
const context={window:{innerHeight:800,scrollY:0,scrollTo(){},addEventListener(){}},
  document:{documentElement:{scrollHeight:800},getElementById:id=>elements[id],
    querySelector:selector=>selector==='#installRecoveryAction form[data-api-action]'?recovery.form:null},
  location:{pathname:'/online-scripts/log/'+input.initial.install_id,search:''},flsGetCsrfToken:()=> 'confirm-csrf',
  setInterval:()=>1,clearInterval(){},setTimeout(){throw new Error('terminal recovery must not auto poll');},clearTimeout(){},
  fetch:async(url,options={})=>{calls.push({url,options});assert.ok(replies.length,'unexpected request');return replies.shift();}};
vm.createContext(context);vm.runInContext(input.reader+input.script,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();const originalForm=recovery.form;
  assert.match(recovery.html,/仅恢复任务导入/);
  await originalForm.submit({preventDefault(){}});
  assert.equal(calls.length,3);assert.equal(calls[2].url,input.failed.state_url);
  assert.equal(originalForm.button.disabled,true);
  assert.match(elements.installPollError.textContent,/state unavailable/);
  await originalForm.submit({preventDefault(){}});assert.equal(calls.length,3);
  await elements.installStateQuery.click();
  assert.match(recovery.html,/核实任务导入结果/);assert.match(recovery.html,/confirm-csrf/);
  const confirmForm=recovery.form;
  await confirmForm.submit({preventDefault(){}});
  assert.match(recovery.html,/重新加载调度器/);
  assert.equal(calls[4].url,input.unknown.recovery.api_action);
  await confirmForm.submit({preventDefault(){}});assert.equal(calls.length,6,'stale confirm form must not post');
  await recovery.form.submit({preventDefault(){}});
  assert.equal(elements.installStatus.textContent,'已完成');assert.equal(recovery.form,null);
  assert.equal(calls.length,8);assert.equal(replies.length,0);
  const posts=calls.filter(call=>call.options.method==='POST');
  assert.equal(posts.length,3);
  assert.equal(posts[0].url,input.initial.recovery.api_action);
  assert.equal(posts[2].url,input.pending.recovery.api_action);
  assert.ok(posts.every(call=>call.options.headers['X-CSRF-Token']==='confirm-csrf'));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"reader": reader, "script": script, "initial": initial, "failed": failed.json,
                        "unknown": unknown, "confirmed": confirmed.json, "pending": pending,
                        "reloaded": reloaded.json, "completed": completed}), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise missing log recovery")
    def test_missing_log_page_keeps_recovery_and_refreshes_state_without_losing_diagnostics(self):
        with isolated_app() as (app, base_dir):
            install_id = self.partial_install(app, base_dir)
            from fls_manager.online_scripts.constants import get_online_install_job

            info = get_online_install_job(install_id)
            Path(info["log_file"]).unlink()
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            html = client.get(f"/online-scripts/log/{install_id}", headers=headers).get_data(as_text=True)
            initial = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            state = client.get(f"/api/online-scripts/log/{install_id}?state_only=1", headers=headers).json
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                client.post(initial["recovery"]["api_action"], headers=headers)
            complete = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            script = next(value for value in re.findall(r"<script>(.*?)</script>", html, re.S)
                          if "async function loadLog(" in value)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("function flsResponseErrorDiagnostic("):
                            source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const names=['installStatus','installPhase','installError','installReturncode','installStopAction',
  'installPollError','installPollLogin','installPollLogs','installPollRetry','installStopQuery',
  'installStateQuery','log','flsLogNewTip'];
const elements=Object.fromEntries(names.map(id=>[id,{hidden:true,textContent:'',style:{},dataset:{},
  addEventListener(name,fn){this[name]=fn;}}]));
elements.log.textContent='last known output';elements.installStatus.textContent='运行中';
const recovery={hidden:true,dataset:{},form:null,html:'',
  set innerHTML(value){this.html=value;this.form=value.includes('<form')?{button:{disabled:false},
    querySelector(){return this.button;},addEventListener(name,fn){this[name]=fn;}}:null;},
  get innerHTML(){return this.html;},querySelector(){return this.form;}};
elements.installRecoveryAction=recovery;
const calls=[],responses=[input.initial,input.state,{ok:true},input.complete];
const context={window:{innerHeight:800,scrollY:0,scrollTo(){},addEventListener(){}},
  document:{documentElement:{scrollHeight:800},getElementById:id=>elements[id],querySelector:()=>null},
  location:{pathname:'/online-scripts/log/'+input.initial.install_id,search:''},
  flsGetCsrfToken:()=> 'recovery-csrf',setInterval:()=>1,clearInterval(){},
  setTimeout(){throw new Error('terminal log errors must not auto poll');},clearTimeout(){},
  fetch:async(url,options={})=>{calls.push({url,options});return {ok:true,status:200,text:async()=>JSON.stringify(responses.shift())};}};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
vm.createContext(context);vm.runInContext(input.reader+input.script,context);
(async()=>{
  await flush();
  assert.equal(elements.installStatus.textContent,'部分完成');
  assert.equal(elements.installPhase.textContent,'scheduler_reload_pending');
  assert.equal(elements.installReturncode.textContent,'0');
  assert.equal(elements.log.textContent,'last known output');
  assert.equal(elements.installPollError.hidden,false);
  assert.equal(recovery.hidden,false);assert.match(recovery.html,/重新加载调度器/);
  assert.match(recovery.html,/csrf_token.*recovery-csrf/);
  assert.equal(elements.installStateQuery.hidden,false);assert.equal(elements.installPollRetry.hidden,false);
  await elements.installStateQuery.click();
  assert.equal(calls[1].url,'/api/online-scripts/log/'+input.initial.install_id+'?state_only=1');
  assert.equal(elements.installPollError.hidden,false);assert.match(elements.installPollError.textContent,/日志/);
  assert.equal(elements.log.textContent,'last known output');
  const form=recovery.form;
  await form.submit({preventDefault(){}});
  assert.equal(calls[2].url,input.initial.recovery.api_action);
  assert.equal(calls[2].options.headers['X-CSRF-Token'],'recovery-csrf');
  assert.equal(calls.length,4);
  assert.equal(elements.installStatus.textContent,'已完成');assert.equal(elements.log.textContent,'last known output');
  assert.equal(elements.installPollError.hidden,false);assert.equal(recovery.form,null);
  assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__,null);
  await form.submit({preventDefault(){}});assert.equal(calls.length,4,'stale recovery form must not resubmit');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script": script, "reader": reader, "initial": initial, "state": state, "complete": complete}),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_task_import_recovery_allows_missing_log_and_refuses_uncontrolled_log_writes(self):
        for failure in ("missing", "symlink", "uncontrolled", "empty_identity", "invalid_identity"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, get_online_install_job, persist_online_install

                install_id = self.pending_task_import(base_dir)
                info = get_online_install_job(install_id)
                log_file = Path(info["log_file"])
                protected = base_dir / "protected.txt"
                protected.write_text("protected content", encoding="utf-8")
                log_file.unlink()
                if failure == "symlink":
                    log_file.symlink_to(protected)
                if failure == "uncontrolled":
                    info["log_file"] = str(protected)
                if failure == "empty_identity":
                    info["log_file"] = " "
                if failure == "invalid_identity":
                    info["log_file"] = 123
                if failure in ("uncontrolled", "empty_identity", "invalid_identity"):
                    persist_online_install(info)
                original = ONLINE_INSTALL_JOBS_FILE.read_bytes()
                action = f"/api/online-scripts/install-task-retry/{install_id}"
                with patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload, patch(
                    "fls_manager.online_scripts.install.download_online_script_logged",
                ) as download:
                    response = app.test_client().post(action, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200 if failure == "missing" else 409)
                download.assert_not_called()
                self.assertEqual(protected.read_text(encoding="utf-8"), "protected content")
                if failure == "missing":
                    reload.assert_called_once()
                    self.assertEqual(len(load_tasks()), 1)
                    self.assertTrue(log_file.is_file())
                    self.assertEqual(get_online_install_job(install_id)["status"], "已完成")
                else:
                    reload.assert_not_called()
                    self.assertFalse(load_tasks())
                    self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)
                    self.assertEqual(response.json["request_id"], install_id)
                    self.assertIn("日志", response.json["msg"])

    def test_missing_log_native_recovery_forms_keep_csrf_session_and_single_completion(self):
        for mode in ("task_import", "scheduler"):
            with self.subTest(mode=mode), isolated_app() as (app, base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE, get_online_install_job

                install_id = self.pending_task_import(base_dir) if mode == "task_import" else self.partial_install(app, base_dir)
                Path(get_online_install_job(install_id)["log_file"]).unlink()
                client = app.test_client()
                initial = client.get(f"/api/online-scripts/log/{install_id}", headers={"X-Token": TOKEN}).json
                action = initial["recovery"]["action"]
                csrf = "missing-log-recovery-csrf"

                def login():
                    with client.session_transaction() as session:
                        session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                       security_verified=True, csrf_token=csrf)

                login()
                page = client.get(f"/online-scripts/log/{install_id}")
                self.assertEqual(page.status_code, 200)
                self.assertIn(f'action="{action}"', page.get_data(as_text=True))
                self.assertIn(f'name="csrf_token" value="{csrf}"', page.get_data(as_text=True))
                original = ONLINE_INSTALL_JOBS_FILE.read_bytes()
                tasks = load_tasks()
                with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload, patch(
                    "fls_manager.online_scripts.tasks.reload_scheduler",
                ) as import_reload, patch("fls_manager.online_scripts.install.download_online_script_logged") as download:
                    self.assertEqual(client.post(action, data={"csrf_token": "wrong"}).status_code, 400)
                    with client.session_transaction() as session:
                        session["token_expire_at"] = 1
                    expired = client.post(action, data={"csrf_token": csrf})
                    self.assertEqual(expired.status_code, 302)
                    self.assertIn("/login?next=", expired.location)
                    self.assertEqual(client.get(initial["state_url"]).status_code, 401)
                    login()
                    with patch("fls_manager.auth.query_token_enabled", return_value=False):
                        denied = client.post(initial["recovery"]["api_action"], data={"csrf_token": csrf}, query_string={"token": TOKEN})
                    self.assertEqual(denied.status_code, 403)
                    self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), original)
                    self.assertEqual(load_tasks(), tasks)
                    reload.assert_not_called()
                    import_reload.assert_not_called()
                    restored = client.post(action, data={"csrf_token": csrf})
                    self.assertEqual(restored.status_code, 302)
                    self.assertIn(f"/online-scripts/log/{install_id}", restored.location)
                    self.assertEqual(client.post(action, data={"csrf_token": csrf}).status_code, 409)
                    self.assertEqual(get_online_install_job(install_id)["status"], "已完成")
                    self.assertEqual(len(load_tasks()), 1)
                    (import_reload if mode == "task_import" else reload).assert_called_once()
                    download.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise recovery concurrency")
    def test_missing_log_recovery_lost_response_uses_single_readonly_query_and_ignores_old_log(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            install_id = self.partial_install(app, base_dir)
            Path(get_online_install_job(install_id)["log_file"]).unlink()
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            html = client.get(f"/online-scripts/log/{install_id}", headers=headers).get_data(as_text=True)
            initial = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                client.post(initial["recovery"]["api_action"], headers=headers)
            complete = client.get(f"/api/online-scripts/log/{install_id}?state_only=1", headers=headers).json
            script = next(value for value in re.findall(r"<script>(.*?)</script>", html, re.S)
                          if "async function loadLog(" in value)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("function flsResponseErrorDiagnostic("):
                            source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const response=(status,payload)=>({ok:status>=200&&status<300,status,text:async()=>JSON.stringify(payload)});
function setup(first){
  const elements=Object.fromEntries(['installStatus','installPhase','installError','installReturncode','installStopAction',
    'installPollError','installPollLogin','installPollLogs','installPollRetry','installStopQuery','installStateQuery','log','flsLogNewTip']
    .map(id=>[id,{hidden:true,textContent:'preserved',dataset:{},style:{},addEventListener(name,fn){this[name]=fn;}}]));
  const recovery={hidden:true,dataset:{},form:null,html:'',set innerHTML(value){this.html=value;
    this.form=value.includes('<form')?{button:{disabled:false},querySelector(){return this.button;},
      addEventListener(name,fn){this[name]=fn;}}:null;},get innerHTML(){return this.html;},querySelector(){return this.form;}};
  elements.installRecoveryAction=recovery;
  const calls=[],replies=[response(200,first)],listeners={};
  const context={window:{innerHeight:800,scrollY:0,scrollTo(){},addEventListener(name,fn){listeners[name]=fn;}},
    document:{documentElement:{scrollHeight:800},getElementById:id=>elements[id],querySelector:()=>null},
    location:{pathname:'/online-scripts/log/'+input.initial.install_id,search:'?back=%2Fonline-scripts%2Finstall-history'},
    URLSearchParams,flsGetCsrfToken:()=> 'csrf',
    setInterval:()=>1,clearInterval(){},setTimeout(){throw new Error('no automatic mutation or terminal polling');},clearTimeout(){},
    fetch:async(url,options={})=>{calls.push({url,options});assert.ok(replies.length,'unexpected request');return replies.shift();}};
  vm.createContext(context);vm.runInContext(input.reader+input.script,context);
  return {elements,recovery,context,calls,replies,listeners};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  const c=setup(input.initial);await flush();
  assert.match(c.recovery.html,/back=.*online-scripts/);
  const form=c.recovery.form;let resolveLog,resolvePost;
  c.replies.push(new Promise(resolve=>{resolveLog=resolve;}));
  const oldRead=c.elements.installPollRetry.onclick();assert.equal(c.calls.length,2);
  c.replies.push(new Promise(resolve=>{resolvePost=resolve;}));
  const submitted=form.submit({preventDefault(){}});
  await form.submit({preventDefault(){}});await c.elements.installStateQuery.click();
  assert.equal(c.calls.length,3,'recovery POST must block duplicate submits and status reads');
  resolvePost(response(502,{msg:'response lost after commit'}));await submitted;
  assert.equal(form.button.disabled,true);assert.equal(c.elements.installStateQuery.hidden,false);
  assert.match(c.elements.installPollError.textContent,/先查询原安装结果/);
  await form.submit({preventDefault(){}});assert.equal(c.calls.length,3);
  resolveLog(response(200,{ok:true,install_id:input.initial.install_id,running:true,status:'outdated',log:'old output'}));await oldRead;
  assert.equal(c.elements.log.textContent,'preserved');assert.equal(c.elements.installStatus.textContent,'部分完成');
  let resolveState;c.replies.push(new Promise(resolve=>{resolveState=resolve;}));
  const stateRead=c.elements.installStateQuery.click();await c.elements.installStateQuery.click();
  assert.equal(c.calls.length,4,'manual state reads must be single flight');
  resolveState(response(503,{msg:'state temporarily unavailable'}));await stateRead;
  assert.equal(c.elements.log.textContent,'preserved');assert.equal(form.button.disabled,true);
  c.replies.push(response(200,input.complete));await c.elements.installStateQuery.click();
  assert.equal(c.elements.installStatus.textContent,'已完成');assert.equal(c.recovery.form,null);
  assert.equal(c.elements.log.textContent,'preserved');assert.equal(c.elements.installPollError.hidden,false);
  assert.equal(c.calls.filter(call=>call.options.method==='POST').length,1);
  assert.ok(c.calls.slice(3).every(call=>call.url.endsWith('?state_only=1')));

  for(const showBeforeResponse of [true,false]){
    const restored=setup(input.initial);await flush();let resolveRecovery;
    restored.replies.push(new Promise(resolve=>{resolveRecovery=resolve;}));
    const request=restored.recovery.form.submit({preventDefault(){}});
    restored.listeners.pagehide();
    if(showBeforeResponse)restored.listeners.pageshow({persisted:true});
    restored.replies.push(response(200,input.complete));
    resolveRecovery(response(200,{ok:true,request_id:input.initial.install_id}));await request;
    if(!showBeforeResponse){
      assert.equal(restored.calls.length,2,'hidden pages must not issue recovery queries');
      restored.listeners.pageshow({persisted:true});
    }
    await flush();
    assert.equal(restored.calls.length,3);assert.ok(restored.calls[2].url.endsWith('?state_only=1'));
    assert.equal(restored.elements.installStatus.textContent,'已完成');
    assert.equal(restored.elements.log.textContent,'preserved');
    assert.equal(restored.calls.filter(call=>call.options.method==='POST').length,1);
  }

  for(const code of [401,403]){
    const auth=setup(input.initial);await flush();const oldForm=auth.recovery.form;
    auth.replies.push(response(code,{msg:'permission denied'}));await auth.elements.installStateQuery.click();
    assert.equal(auth.recovery.hidden,true);assert.equal(auth.elements.installPollLogin.hidden,false);
    assert.match(auth.elements.installPollLogin.href,/login\?next=/);
    await oldForm.submit({preventDefault(){}});assert.equal(auth.calls.length,2,'permission failure must block old form submission');
    auth.replies.push(response(200,input.complete));await auth.elements.installStateQuery.click();
    assert.equal(auth.elements.installStatus.textContent,'已完成');assert.equal(auth.elements.installPollLogin.hidden,true);
  }
  const wrong=setup({...input.initial,install_id:'e'.repeat(32)});await flush();
  assert.equal(wrong.elements.installStatus.textContent,'preserved');assert.equal(wrong.recovery.form,null);
  assert.match(wrong.elements.installPollError.textContent,/状态响应无效/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script": script, "reader": reader, "initial": initial, "complete": complete}),
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reload_and_persistence_failures_keep_recovery_available(self):
        for failure in ("reload", "persist"):
            with self.subTest(failure=failure), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts.constants import get_online_install_job

                install_id = self.partial_install(app, base_dir)
                action = f"/online-scripts/install-reload/{install_id}"
                client = app.test_client()
                headers = {"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"}
                with patch(
                    "fls_manager.routes.online_scripts.install.reload_scheduler",
                    side_effect=RuntimeError("still unavailable") if failure == "reload" else None,
                ), patch(
                    "fls_manager.routes.online_scripts.install.persist_online_install",
                    side_effect=PermissionError("job read-only") if failure == "persist" else None,
                ):
                    failed = client.post(action, headers=headers)
                self.assertEqual(failed.status_code, 503 if failure == "reload" else 500)
                html = failed.get_data(as_text=True)
                self.assertIn("still unavailable" if failure == "reload" else "job read-only", html)
                self.assertIn(f'action="{action}"', html)
                self.assertIn('name="csrf_token"', html)
                self.assertTrue(get_online_install_job(install_id)["scheduler_reload_pending"])
                with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                    recovered = client.post(action, headers=headers)
                self.assertEqual(recovered.status_code, 302)
                self.assertEqual(get_online_install_job(install_id)["status"], "已完成")

    def test_api_reload_reports_failure_then_recovers_without_reinstalling(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import get_online_install_job

            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            with patch(
                "fls_manager.routes.online_scripts.install.reload_scheduler",
                side_effect=RuntimeError("scheduler unavailable"),
            ) as reload_scheduler, patch(
                "fls_manager.online_scripts.install.download_online_script_logged"
            ) as download, patch(
                "fls_manager.online_scripts.install.import_task_if_needed"
            ) as importer:
                failed = client.post(
                    f"/api/online-scripts/install-reload/{install_id}",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 503)
                payload = failed.get_json()
                self.assertFalse(payload["ok"])
                self.assertTrue(payload["scheduler_reload_required"])
                self.assertEqual(payload["phase"], "scheduler_reload_pending")
                self.assertIn("scheduler unavailable", payload["msg"])
                self.assertEqual(payload["reload_url"], f"/api/online-scripts/install-reload/{install_id}")
                reload_scheduler.assert_called_once()
                download.assert_not_called()
                importer.assert_not_called()
                self.assertTrue(get_online_install_job(install_id)["scheduler_reload_pending"])

            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler, patch(
                "fls_manager.online_scripts.install.download_online_script_logged"
            ) as download, patch("fls_manager.online_scripts.install.import_task_if_needed") as importer:
                recovered = client.post(
                    f"/api/online-scripts/install-reload/{install_id}",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(recovered.status_code, 200)
                self.assertTrue(recovered.get_json()["ok"])
                reload_scheduler.assert_called_once()
                download.assert_not_called()
                importer.assert_not_called()
                repeated = client.post(
                    f"/api/online-scripts/install-reload/{install_id}",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 409)
                self.assertFalse(repeated.get_json()["ok"])
                reload_scheduler.assert_called_once()
            self.assertFalse(get_online_install_job(install_id)["scheduler_reload_pending"])
            self.assertEqual([task["name"] for task in load_tasks()], ["Daily"])

    def test_api_reload_requires_csrf_and_rejects_invalid_or_running_records(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="api-reload-csrf")
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                invalid = client.post(
                    "/api/online-scripts/install-reload/not-an-id", headers={"X-Token": TOKEN},
                )
                self.assertEqual(invalid.status_code, 400)
                denied = client.post(f"/api/online-scripts/install-reload/{install_id}")
                self.assertEqual(denied.status_code, 400)
                ONLINE_INSTALL_RUNNING[install_id]["running"] = True
                running = client.post(
                    f"/api/online-scripts/install-reload/{install_id}",
                    headers={"X-CSRF-Token": "api-reload-csrf"},
                )
                self.assertEqual(running.status_code, 409)
                reload_scheduler.assert_not_called()

    def test_recovery_loads_saved_task_into_the_real_scheduler(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.scheduler import get_scheduler_state
            from fls_manager.state import scheduler

            install_id = self.partial_install(app, base_dir)
            task_id = load_tasks()[0]["id"]
            self.assertIsNone(scheduler.get_job(f"task_{task_id}"))
            response = app.test_client().post(f"/online-scripts/install-reload/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            self.assertIsNotNone(scheduler.get_job(f"task_{task_id}"))
            self.assertEqual(get_scheduler_state()["status"], "succeeded")
            self.assertEqual(get_scheduler_state()["scheduled_count"], 1)
            self.assertEqual(get_online_install_job(install_id)["status"], "已完成")

    def test_restart_before_install_completion_keeps_unknown_after_scheduler_recovery(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_RUNNING, get_online_install_job, persist_online_install,
            )

            install_id = self.partial_install(app, base_dir)
            info = get_online_install_job(install_id)
            info.update(running=True, status="运行中", returncode=None, error="")
            persist_online_install(info)
            ONLINE_INSTALL_RUNNING.clear()
            self.assertEqual(get_online_install_job(install_id)["status"], "结果未知")
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                response = app.test_client().post(f"/online-scripts/install-reload/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            recovered = get_online_install_job(install_id)
            self.assertFalse(recovered["scheduler_reload_pending"])
            self.assertEqual(recovered["status"], "结果未知")
            self.assertIsNone(recovered["returncode"])
            payload = app.test_client().get(f"/api/online-scripts/log/{install_id}", headers={"X-Token": TOKEN}).json
            self.assertEqual(payload["recovery"]["label"], "重试安装")

    def test_reloading_tasks_preserves_an_independent_install_command_failure(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            install_id = self.partial_install(app, base_dir, command_failure=True)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            before = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertIn("install command failed", before["error"])
            self.assertIn("scheduler unavailable", before["error"])
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                response = client.post(f"/online-scripts/install-reload/{install_id}", headers=headers)
            self.assertEqual(response.status_code, 302)
            info = get_online_install_job(install_id)
            self.assertEqual(info["status"], "失败")
            self.assertEqual(info["returncode"], 17)
            self.assertIn("install command failed", info["error"])
            self.assertFalse(info["scheduler_reload_pending"])
            after = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertEqual(after["recovery"]["label"], "重试安装")
            self.assertNotIn("scheduler unavailable", after["error"])

    def test_concurrent_reloads_have_one_claim_and_release_it_after_completion(self):
        with isolated_app() as (app, base_dir):
            install_id = self.partial_install(app, base_dir)
            action = f"/online-scripts/install-reload/{install_id}"
            entered = threading.Event()
            release = threading.Event()
            responses = []

            def reload_scheduler():
                entered.set()
                self.assertTrue(release.wait(3))

            def post():
                responses.append(app.test_client().post(action, headers={"X-Token": TOKEN}))

            with patch("fls_manager.routes.online_scripts.install.reload_scheduler", side_effect=reload_scheduler) as reload:
                thread = threading.Thread(target=post)
                thread.start()
                try:
                    self.assertTrue(entered.wait(3))
                    duplicate = app.test_client().post(action, headers={"X-Token": TOKEN})
                    self.assertEqual(duplicate.status_code, 409)
                finally:
                    release.set()
                    thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                reload.assert_called_once()
            self.assertEqual([response.status_code for response in responses], [302])

    def test_deleted_source_does_not_prevent_reloading_already_saved_tasks(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.source import save_online_script_cache

            install_id = self.partial_install(app, base_dir)
            save_online_script_cache([])
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler"):
                response = app.test_client().post(f"/online-scripts/install-reload/{install_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)

    def test_missing_running_and_completed_records_do_not_reload(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                missing = client.post("/online-scripts/install-reload/deleted", headers={"X-Token": TOKEN})
                self.assertEqual(missing.status_code, 404)
                for options in ({"running": True}, {"running": False, "scheduler_reload_pending": False, "status": "已完成"}):
                    ONLINE_INSTALL_RUNNING[install_id].update(options)
                    response = client.post(f"/online-scripts/install-reload/{install_id}", headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 409)
                reload_scheduler.assert_not_called()

    def test_recovery_requires_valid_csrf_session_and_permission(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            install_id = self.partial_install(app, base_dir)
            action = f"/online-scripts/install-reload/{install_id}"
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="reload-csrf")
            with patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                denied = client.post(action, headers=headers)
                self.assertEqual(denied.status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(action, data={"csrf_token": "reload-csrf"}, headers=headers)
                self.assertEqual(expired.status_code, 401)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="reload-csrf")
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    forbidden = client.post(action, query_string={"token": TOKEN}, data={"csrf_token": "reload-csrf"}, headers=headers)
                self.assertEqual(forbidden.status_code, 403)
                reload_scheduler.assert_not_called()
                self.assertTrue(get_online_install_job(install_id)["scheduler_reload_pending"])
                recovered = client.post(action, data={"csrf_token": "reload-csrf"}, headers=headers)
            self.assertEqual(recovered.status_code, 302)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise recovery rendering")
    def test_polling_renders_scheduler_recovery_action_and_label_with_csrf(self):
        with isolated_app() as (app, base_dir):
            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            html = client.get(f"/online-scripts/log/{install_id}", headers=headers).get_data(as_text=True)
            payload = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "async function loadLog(" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
 'installStopAction', 'installRecoveryAction', 'installPollError', 'log', 'flsLogNewTip']
 .map(id => [id, {textContent: '', hidden: false, dataset: {}, style: {}}]));
const context = {
 window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
 document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id], querySelector: () => null},
 fetch: async () => ({ok: true, status: 200, json: async () => input.payload}),
 setInterval: () => 1, clearInterval() {}, flsGetCsrfToken: () => 'reload-csrf',
};
vm.createContext(context);
vm.runInContext(input.reader + input.script, context);
setImmediate(() => {
 assert.equal(elements.installStatus.textContent, '部分完成');
 assert.match(elements.installRecoveryAction.innerHTML, /重新加载调度器/);
 assert.ok(elements.installRecoveryAction.innerHTML.includes(input.payload.recovery.action));
 assert.match(elements.installRecoveryAction.innerHTML, /csrf_token.*reload-csrf/);
 assert.doesNotMatch(elements.installRecoveryAction.innerHTML, /重试安装/);
 assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, null);
});
"""], input=json.dumps({"script": script, "reader": reader, "payload": payload}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise recovery AJAX")
    def test_polling_recovery_button_posts_json_reload_and_refreshes_status(self):
        with isolated_app() as (app, base_dir):
            install_id = self.partial_install(app, base_dir)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            html = client.get(f"/online-scripts/log/{install_id}", headers=headers).get_data(as_text=True)
            payload = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "async function loadLog(" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const button = {disabled: false};
const form = {listeners: {}, querySelector: selector => selector === 'button' ? button : null,
  addEventListener(name, handler) { this.listeners[name] = handler; }};
const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
 'installStopAction', 'installPollError', 'log', 'flsLogNewTip']
 .map(id => [id, {textContent: '', hidden: false, dataset: {}, style: {}}]));
elements.installRecoveryAction = {textContent: '', hidden: false, dataset: {}, style: {}, innerHTML: '',
  querySelector: selector => selector === 'form[data-api-action]' ? form : null};
let calls = [];
let responses = [input.payload, {ok: true, request_id: input.payload.request_id, status: '已完成'},
  {running: false, status: '已完成', log: 'reloaded'}];
const context = {
 window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
 document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id], querySelector: () => null},
 fetch: async (url, options) => {
   calls.push({url, options});
   const value = responses.shift();
   return {ok: true, status: 200, json: async () => value};
 },
 setInterval: () => 1, clearInterval() {}, flsGetCsrfToken: () => 'reload-csrf',
};
vm.createContext(context);
vm.runInContext(input.reader + input.script, context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(form.listeners.submit, 'recovery form should use the JSON action');
  let prevented = false;
  await form.listeners.submit({preventDefault: () => { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(calls[1].url, input.payload.recovery.api_action);
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[1].options.headers['X-CSRF-Token'], 'reload-csrf');
  assert.equal(calls.length, 3);
  assert.equal(elements.log.textContent, 'reloaded');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""], input=json.dumps({"script": script, "reader": reader, "payload": payload}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise retry-to-reload recovery")
    def test_task_retry_503_refreshes_into_scheduler_reload_action(self):
        with isolated_app() as (app, base_dir):
            install_id = self.pending_task_import(base_dir)
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            html = client.get(f"/online-scripts/log/{install_id}", headers=headers).get_data(as_text=True)
            initial = client.get(f"/api/online-scripts/log/{install_id}", headers=headers).json
            self.assertEqual(initial["recovery"]["label"], "仅恢复任务导入")
            self.assertEqual(initial["recovery"]["api_action"], f"/api/online-scripts/install-task-retry/{install_id}")
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "async function loadLog(" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            pending = dict(initial)
            pending.update(
                status="部分完成", phase="scheduler_reload_pending", error="scheduler unavailable",
                scheduler_reload_pending=True,
                scheduler_reload_required=True,
                reload_url=f"/api/online-scripts/install-reload/{install_id}",
                recovery={
                    "available": True,
                    "action": f"/online-scripts/install-reload/{install_id}",
                    "api_action": f"/api/online-scripts/install-reload/{install_id}",
                    "label": "重新加载调度器",
                },
            )
            complete = dict(initial)
            complete.update(running=False, status="已完成", phase="completed", log="recovered")
            complete.pop("recovery", None)
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let calls = [];
let payloads = [input.initial, input.pending, input.pending, {ok:true}, input.complete];
const button = {disabled:false};
const recovery = {
  hidden:false, dataset:{}, form:null, html:'',
  set innerHTML(value) {
    this.html=value;
    this.form={listeners:{}, querySelector: selector=>selector==='button'?button:null,
      addEventListener(name, handler){this.listeners[name]=handler;}};
  },
  get innerHTML(){return this.html;},
  querySelector: selector=>selector==='form[data-api-action]'?recovery.form:null,
};
const elements = Object.fromEntries(['installStatus','installPhase','installError','installReturncode',
  'installStopAction','installPollError','installPollLogin','installPollLogs','installPollRetry','installStopQuery',
  'log','flsLogNewTip'].map(id=>[id,{textContent:'',hidden:true,dataset:{},style:{},addEventListener(){}}]));
elements.installRecoveryAction=recovery;
const context = {
  window:{innerHeight:800,scrollY:0,addEventListener(){},scrollTo(){}},
  document:{documentElement:{scrollHeight:800},getElementById:id=>elements[id],
    querySelector:()=>recovery.form},
  location:{pathname:'/online-scripts/log/'+input.initial.install_id,search:''},
  fetch:async(url,options={})=>{
    calls.push({url,options});
    const payload=payloads.shift();
        const status=calls.length===2?503:200;
    return {ok:status<400,status,json:async()=>payload};
  },
  setInterval:()=>1,clearInterval(){},flsGetCsrfToken:()=> 'reload-csrf',
};
vm.createContext(context);vm.runInContext(input.reader+input.script,context);
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(recovery.html,/仅恢复任务导入/);
  const taskRetryForm=recovery.form;
  await taskRetryForm.listeners.submit({preventDefault(){}});
  assert.equal(calls[1].url,input.initial.recovery.api_action);
  assert.equal(calls[1].options.headers['X-CSRF-Token'],'reload-csrf');
  assert.equal(calls[2].url,'/api/online-scripts/log/'+input.initial.install_id+'?lines=1600');
  assert.match(recovery.html,/重新加载调度器/);
  assert.equal(button.disabled,false);
  const reloadForm=recovery.form;
  await reloadForm.listeners.submit({preventDefault(){}});
  assert.equal(calls[3].url,input.pending.recovery.api_action);
  assert.equal(calls[3].options.headers['X-CSRF-Token'],'reload-csrf');
  assert.equal(calls[4].url,'/api/online-scripts/log/'+input.initial.install_id+'?lines=1600');
  assert.equal(elements.installStatus.textContent,'已完成');
  assert.equal(elements.log.textContent,'recovered');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script":script,"reader":reader,"initial":initial,"pending":pending,"complete":complete}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
