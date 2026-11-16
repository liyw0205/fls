import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineTaskValidationTests(unittest.TestCase):
    def item(self):
        return {
            "id": "demo", "name": "Demo", "type": "raw",
            "link": "https://example.invalid/demo.sh", "link_name": "demo.sh",
            "task_cron": [
                {"name": "Daily", "command": "task demo.sh", "cron": "0 0 * * *"},
                {"name": "Invalid", "command": "task demo.sh invalid", "cron": "bad cron"},
            ],
        }

    def test_invalid_cron_rejects_entire_selection_before_mutation_even_when_disabled(self):
        for cron in ("bad cron", "61 * * * *", "* * * * * * *"):
            for enabled in (False, True):
                with self.subTest(cron=cron, enabled=enabled), isolated_app() as (_app, _base_dir):
                    from fls_manager.models import load_tasks
                    from fls_manager.online_scripts import tasks

                    item = self.item()
                    item["task_cron"][1]["cron"] = cron
                    with patch.object(tasks, "mutate_tasks") as mutate, patch.object(tasks, "reload_scheduler") as reload:
                        with self.assertRaisesRegex(tasks.OnlineTaskValidationError, "第 2 个任务.*Cron 不合法"):
                            tasks.import_task_if_needed(item, selected_task_indexes=[1, 2], enable_task=enabled)
                    mutate.assert_not_called()
                    reload.assert_not_called()
                    self.assertEqual(load_tasks(), [])

    def test_explicit_empty_or_invalid_indexes_never_fall_back_to_import_all(self):
        for indexes in ([], (), set(), ["bad"], [0], [-1], [3], [True], [1, "bad"], "1"):
            with self.subTest(indexes=indexes), isolated_app() as (_app, _base_dir):
                from fls_manager.online_scripts import tasks

                item = self.item()
                item["task_cron"][1]["cron"] = ""
                with patch.object(tasks, "mutate_tasks") as mutate:
                    with self.assertRaises(tasks.OnlineTaskValidationError):
                        tasks.import_task_if_needed(item, selected_task_indexes=indexes)
                mutate.assert_not_called()

    def test_no_tasks_and_missing_script_id_are_validation_errors_not_duplicate_results(self):
        for options in ({"task_cron": []}, {"task_cron": [{"name": "missing command"}]}, {"id": ""}):
            with self.subTest(options=options), isolated_app() as (_app, _base_dir):
                from fls_manager.online_scripts import tasks

                item = self.item()
                item.update(options)
                with patch.object(tasks, "mutate_tasks") as mutate:
                    with self.assertRaises(tasks.OnlineTaskValidationError):
                        tasks.import_task_if_needed(item)
                mutate.assert_not_called()

    def test_all_selected_fields_are_validated_without_exposing_environment_values(self):
        changes = (
            {"command": "task demo\x00.sh"}, {"name": "Daily\x00"},
            {"var": {"BAD=KEY": "private-value"}}, {"var": {"BAD\x00KEY": "private-value"}},
            {"var": {"KEY": "private-value\x00"}},
            {"config_path": "../outside.yml"}, {"config_path": "/etc/config.yml"},
            {"config_path": "C:\\config.yml"}, {"config_path": "..\\outside.yml"},
            {"config_path": "bad\x00.yml"}, {"config_path": "."},
        )
        for change in changes:
            with self.subTest(change=change), isolated_app() as (_app, _base_dir):
                from fls_manager.online_scripts import tasks

                item = self.item()
                item["task_cron"][0].update(change)
                with patch.object(tasks, "mutate_tasks") as mutate:
                    with self.assertRaises(tasks.OnlineTaskValidationError) as error:
                        tasks.import_task_if_needed(item, selected_task_indexes=[1])
                self.assertNotIn("private-value", str(error.exception))
                mutate.assert_not_called()

    def test_config_symlink_outside_scripts_is_rejected_before_import(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import tasks

            outside = base_dir / "outside"
            outside.mkdir()
            link = base_dir / "scripts" / "external"
            link.symlink_to(outside, target_is_directory=True)
            item = self.item()
            item["task_cron"][0]["config_path"] = "external/config.yml"
            with self.assertRaisesRegex(tasks.OnlineTaskValidationError, "配置路径超出"):
                tasks.import_task_if_needed(item, selected_task_indexes=[1])

    def test_unselected_invalid_task_does_not_block_valid_import_or_manual_schedule(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import tasks

            item = self.item()
            item["task_cron"][0].update(cron="", config_path="demo/config.yml", var={"KEY": "value"})
            with patch.object(tasks, "reload_scheduler") as reload:
                imported, message = tasks.import_task_if_needed(item, selected_task_indexes=[1, "1"], enable_task=True)
            self.assertTrue(imported)
            self.assertIn("新增 1 个", message)
            self.assertIn("未选择 1 个", message)
            saved = load_tasks()[0]
            self.assertEqual(saved["name"], "Daily")
            self.assertEqual(saved["cron"], "")
            self.assertEqual(saved["config_path"], "demo/config.yml")
            self.assertEqual(saved["env"], {"KEY": "value"})
            self.assertTrue(saved["enabled"])
            reload.assert_called_once()

    def test_default_selection_imports_all_valid_tasks_and_reports_duplicates_separately(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import tasks

            item = self.item()
            item["task_cron"][1].update(name="Hourly", cron="0 0 * * * *")
            with patch.object(tasks, "reload_scheduler") as reload:
                first, _message = tasks.import_task_if_needed(item)
                second, message = tasks.import_task_if_needed(item)
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertIn("均已导入", message)
            self.assertIn("已存在 2 个", message)
            self.assertEqual(len(load_tasks()), 2)
            reload.assert_called_once()

    def test_mixed_import_and_install_forms_return_validation_errors_and_allow_valid_subset(self):
        for path in ("/online-scripts/import-tasks/demo", "/online-scripts/install/demo"):
            for ajax in (False, True):
                with self.subTest(path=path, ajax=ajax), isolated_app() as (app, _base_dir):
                    from fls_manager.models import load_tasks
                    from fls_manager.online_scripts import source

                    source.save_online_script_cache([self.item()])
                    client = app.test_client()
                    headers = {"X-Token": TOKEN}
                    if ajax:
                        headers["X-Requested-With"] = "FLS-Ajax"
                    with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start, patch(
                        "fls_manager.online_scripts.tasks.reload_scheduler",
                    ) as reload:
                        rejected = client.post(path, data={"import_task": "1", "task_indexes": ["1", "2"]}, headers=headers)
                        self.assertEqual(rejected.status_code, 400)
                        self.assertNotIn("Location", rejected.headers)
                        html = rejected.get_data(as_text=True)
                        self.assertIn("第 2 个任务", html)
                        self.assertIn("Cron 不合法", html)
                        self.assertIn("/online-scripts/source", html)
                        self.assertEqual(load_tasks(), [])
                        start.assert_not_called()
                        reload.assert_not_called()
                        # Excluding the invalid task must not validate it as a selection.
                        accepted = client.post(
                            path, data={"import_task": "1", "select_mode": "all", "excluded_task_indexes": "2"},
                            headers=headers,
                        )
                    self.assertEqual(accepted.status_code, 302)
                    if path.endswith("install/demo"):
                        start.assert_called_once()
                        self.assertEqual(start.call_args.kwargs["selected_task_indexes"], ["1"])
                    else:
                        self.assertEqual([task["name"] for task in load_tasks()], ["Daily"])

    def test_selection_page_displays_escaped_diagnostic_for_the_invalid_task(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            item["task_cron"][1]["name"] = "<script>untrusted()</script>"
            source.save_online_script_cache([item])
            response = app.test_client().get("/online-scripts/install-select/demo", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("Cron 不合法", html)
            self.assertIn('data-index="2"', html)
            self.assertNotIn("<script>untrusted()</script>", html)

    def test_validation_service_failure_returns_recoverable_500_not_invalid_input(self):
        for module, path in (
            ("install_select", "/online-scripts/import-tasks/demo"),
            ("install", "/online-scripts/install/demo"),
        ):
            with self.subTest(path=path), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts.source import save_online_script_cache

                save_online_script_cache([self.item()])
                with patch(
                    f"fls_manager.routes.online_scripts.{module}.validate_online_task_import",
                    side_effect=RuntimeError("validation unavailable"),
                ), patch("fls_manager.online_scripts.tasks.mutate_tasks") as mutate, patch(
                    "fls_manager.routes.online_scripts.install.start_install_thread",
                ) as start:
                    response = app.test_client().post(
                        path, data={"import_task": "1", "task_indexes": "1"},
                        headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                    )
                self.assertEqual(response.status_code, 500)
                self.assertIn("validation unavailable", response.get_data(as_text=True))
                self.assertIn("重新选择任务", response.get_data(as_text=True))
                self.assertNotIn("Location", response.headers)
                mutate.assert_not_called()
                start.assert_not_called()

    def test_worker_rejects_invalid_task_before_downloading_or_running_install_command(self):
        for selection in ([1, 2], [], ["bad"]):
            with self.subTest(selection=selection), isolated_app() as (app, base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING, get_online_install_job
                from fls_manager.online_scripts.install import install_worker

                info = {
                    "id": "worker", "script_id": "demo", "script_name": "Demo",
                    "log_file": str(base_dir / "log" / "worker.log"), "running": True,
                    "status": "准备中", "start_time": 1, "error": "", "import_task": True,
                    "selected_task_indexes": selection,
                }
                ONLINE_INSTALL_RUNNING["worker"] = info
                item = self.item()
                item["install"] = "fixture install"
                with patch("fls_manager.online_scripts.install.download_online_script_logged") as download, patch(
                    "fls_manager.online_scripts.install.run_logged_command",
                ) as command, patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload:
                    install_worker("worker", item, import_task=True, selected_task_indexes=selection)
                download.assert_not_called()
                command.assert_not_called()
                reload.assert_not_called()
                self.assertEqual(load_tasks(), [])
                self.assertEqual(info["status"], "失败")
                self.assertFalse(info["running"])
                self.assertIsNone(info["returncode"])
                self.assertTrue(info["error"])
                ONLINE_INSTALL_RUNNING.clear()
                self.assertEqual(get_online_install_job("worker")["status"], "失败")
                payload = app.test_client().get("/api/online-scripts/log/worker", headers={"X-Token": TOKEN}).json
                self.assertTrue(payload["error"])
                self.assertIn("worker", payload["log_file"])
                self.assertIn("error", payload)

    def test_install_with_existing_valid_task_is_idempotent_and_not_a_failed_import(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.online_scripts.install import install_worker
            from fls_manager.online_scripts.source import save_online_script_cache
            from fls_manager.online_scripts.tasks import import_task_if_needed

            item = self.item()
            item["task_cron"] = item["task_cron"][:1]
            save_online_script_cache([item])
            with patch("fls_manager.online_scripts.tasks.reload_scheduler"):
                import_task_if_needed(item)
            original_id = load_tasks()[0]["id"]
            with patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=install_worker), patch(
                "fls_manager.online_scripts.install.download_online_script_logged", return_value=base_dir / "scripts" / "demo.sh",
            ), patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload:
                response = app.test_client().post(
                    "/online-scripts/install/demo", data={"import_task": "1", "task_indexes": "1"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 302)
            install_id = response.headers["Location"].split("?")[0].rsplit("/", 1)[1]
            info = get_online_install_job(install_id)
            self.assertEqual(info["status"], "已完成")
            self.assertEqual(info["error"], "")
            self.assertEqual([task["id"] for task in load_tasks()], [original_id])
            reload.assert_not_called()
            payload = app.test_client().get(f"/api/online-scripts/log/{install_id}", headers={"X-Token": TOKEN}).json
            self.assertIn("均已导入", payload["log"])
            self.assertIn("已存在 1 个", payload["log"])

    def test_semantic_validation_does_not_bypass_csrf_or_expired_session(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.item()])
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="task-csrf")
            with patch("fls_manager.routes.online_scripts.install_select.validate_online_task_import") as validate:
                rejected = client.post("/online-scripts/import-tasks/demo", data={"task_indexes": "1"}, headers=headers)
                self.assertEqual(rejected.status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(
                    "/online-scripts/import-tasks/demo", data={"task_indexes": "1", "csrf_token": "task-csrf"}, headers=headers,
                )
                self.assertEqual(expired.status_code, 401)
                validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
