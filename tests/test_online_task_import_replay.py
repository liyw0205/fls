import contextlib
import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineTaskImportReplayTests(unittest.TestCase):
    @contextlib.contextmanager
    def app(self):
        with isolated_app() as (app, base_dir), patch("fls_manager.app.dispatch_frontend_open_event"):
            yield app, base_dir

    def item(self):
        return {"id": "demo", "name": "Demo", "type": "raw", "link": "https://fixture.invalid/demo.py",
                "task_cron": [{"name": "First", "command": "echo first", "cron": ""},
                              {"name": "Excluded", "command": "echo excluded", "cron": ""}]}

    def test_completed_replay_uses_original_selection_and_receipt_after_cache_changes(self):
        for mode in ("changed", "deleted", "corrupt", "symlink"):
            with self.subTest(mode=mode), self.app() as (app, base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts import constants, source, tasks
                from fls_manager.routes.online_scripts import install_select

                source.save_online_script_cache([self.item()])
                client, headers = app.test_client(), {"X-Token": TOKEN}
                request_id = "a" * 32
                data = {"request_id": request_id, "select_mode": "all", "excluded_task_indexes": "2"}
                with patch.object(tasks, "reload_scheduler") as reload:
                    self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers).status_code, 200)
                    reload.assert_called_once()
                original_tasks = load_tasks()
                self.assertEqual(len(original_tasks), 1)
                receipt_before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
                path = constants.ONLINE_SCRIPT_CACHE_FILE
                if mode == "changed":
                    changed = self.item()
                    changed["task_cron"] = [{"name": "New task", "command": "echo new", "cron": ""}]
                    source.save_online_script_cache([changed])
                elif mode == "deleted":
                    source.save_online_script_cache([])
                elif mode == "corrupt":
                    path.write_text("broken cache", encoding="utf-8")
                else:
                    target = base_dir / "external-cache.json"
                    path.rename(target)
                    path.symlink_to(target)
                cache_before = path.read_bytes()
                with patch.object(install_select, "import_task_if_needed") as importer, patch.object(install_select, "claim_online_task_import_request") as claim, patch.object(install_select, "reload_scheduler") as reload:
                    response = client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json["replayed"])
                    self.assertEqual(response.json["state"], "completed")
                    self.assertEqual(response.json["status_url"], "/online-scripts/import-tasks-status/" + request_id)
                    self.assertEqual(client.post("/online-scripts/import-tasks/demo", data=data, headers=headers).status_code, 302)
                    importer.assert_not_called()
                    claim.assert_not_called()
                    reload.assert_not_called()
                self.assertEqual(load_tasks(), original_tasks)
                self.assertEqual(path.read_bytes(), cache_before)
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), receipt_before)

    def test_pending_unknown_and_archived_replays_keep_original_recovery_when_cache_unreadable(self):
        for state in ("scheduler_pending", "unknown", "archived"):
            with self.subTest(state=state), self.app() as (app, _base_dir):
                from fls_manager.online_scripts import constants, source, tasks
                from fls_manager.routes.online_scripts import install_select

                item = source.normalize_online_scripts([self.item()])[0]
                request_id = "a" * 32
                constants.persist_online_task_import_request({
                    "request_id": request_id, "script_id": "demo", "expected_version": tasks.online_task_import_version(item),
                    "selected_task_indexes": ["1"], "enable_task": False,
                    "state": "unknown" if state == "archived" else state,
                    "message": "original recovery diagnosis", "scheduler_reload_error": "", "updated_at": "old",
                    "archived": state == "archived", "archived_at": "old" if state == "archived" else "",
                })
                constants.ONLINE_SCRIPT_CACHE_FILE.write_text("invalid cache", encoding="utf-8")
                before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
                client, headers = app.test_client(), {"X-Token": TOKEN}
                with patch.object(install_select, "import_task_if_needed") as importer, patch.object(install_select, "reload_scheduler") as reload:
                    data = {"request_id": request_id, "task_indexes": "1"}
                    for prefix in ("/api", ""):
                        response = client.post(prefix + "/online-scripts/import-tasks/demo", data=data, headers=headers)
                        self.assertEqual(response.status_code, 503 if state == "scheduler_pending" else 409)
                        if prefix:
                            self.assertTrue(response.json["replayed"])
                            self.assertIn("original recovery diagnosis", response.json["msg"])
                        else:
                            self.assertIn("/online-scripts/import-tasks-status/" + request_id, response.get_data(as_text=True))
                    importer.assert_not_called()
                    reload.assert_not_called()
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)

    def test_wrong_binding_and_unauthorized_replay_do_not_modify_or_recover_original_receipt(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, source, tasks
            from fls_manager.routes.online_scripts import install_select

            item = source.normalize_online_scripts([self.item()])[0]
            request_id = "a" * 32
            constants.persist_online_task_import_request({
                "request_id": request_id, "script_id": "demo", "expected_version": tasks.online_task_import_version(item),
                "selected_task_indexes": ["1"], "enable_task": False, "state": "completed",
                "message": "original result", "scheduler_reload_error": "", "updated_at": "old",
            })
            constants.ONLINE_SCRIPT_CACHE_FILE.write_text("broken cache", encoding="utf-8")
            before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
            client, headers = app.test_client(), {"X-Token": TOKEN}
            data = {"request_id": request_id, "task_indexes": "1"}
            with patch.object(install_select, "import_task_if_needed") as importer:
                for script_id, extra in (("other", {}), ("demo", {"task_indexes": "2"}), ("demo", {"enable_task": True})):
                    response = client.post("/api/online-scripts/import-tasks/" + script_id, json=dict(data, **extra), headers=headers)
                    self.assertEqual(response.status_code, 409)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="replay-csrf")
                for csrf in ("", "wrong"):
                    self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers={"X-CSRF-Token": csrf}).status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers={"X-CSRF-Token": "replay-csrf"}).status_code, 401)
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                importer.assert_not_called()

    def test_failed_retry_still_requires_original_version_and_current_valid_selection(self):
        for state in ("prepared", "failed"):
            with self.subTest(state=state), self.app() as (app, _base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts import constants, source, tasks
                from fls_manager.routes.online_scripts import install_select

                item = source.normalize_online_scripts([self.item()])[0]
                request_id = "a" * 32
                constants.persist_online_task_import_request({
                    "request_id": request_id, "script_id": "demo", "expected_version": tasks.online_task_import_version(item),
                    "selected_task_indexes": ["1"], "enable_task": False, "state": state,
                    "message": "original failed result", "scheduler_reload_error": "", "updated_at": "old",
                    "task_count": 2,
                })
                changed = self.item()
                changed["task_cron"][0]["command"] = "echo changed"
                source.save_online_script_cache([changed])
                before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
                data, headers = {"request_id": request_id, "task_indexes": "1"}, {"X-Token": TOKEN}
                client = app.test_client()
                with patch.object(install_select, "import_task_if_needed") as importer, patch.object(install_select, "reload_scheduler") as reload:
                    self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers).status_code, 409)
                    for indexes in ([], ["999"], ["bad"]):
                        self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=dict(data, task_indexes=indexes), headers=headers).status_code, 400)
                    importer.assert_not_called()
                    reload.assert_not_called()
                    self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                    self.assertEqual(load_tasks(), [])

    def test_legacy_all_selection_has_status_navigation_without_guessing_current_tasks(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import install_select

            request_id = "a" * 32
            constants.persist_online_task_import_request({
                "request_id": request_id, "script_id": "demo", "expected_version": "old-version",
                "selected_task_indexes": ["1"], "enable_task": False, "state": "completed",
                "message": "legacy completed result", "scheduler_reload_error": "", "updated_at": "old",
            })
            client, headers = app.test_client(), {"X-Token": TOKEN}
            with patch.object(install_select, "get_online_script") as get_script, patch.object(install_select, "import_task_if_needed") as importer:
                data = {"request_id": request_id, "select_mode": "all", "excluded_task_indexes": "2"}
                response = client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers)
                self.assertEqual(response.status_code, 409)
                self.assertIn("原回执未保存任务总数", response.json["msg"])
                self.assertEqual(response.json["status_url"], "/online-scripts/import-tasks-status/" + request_id)
                explicit = client.post("/api/online-scripts/import-tasks/demo", json={"request_id": request_id, "task_indexes": "1"}, headers=headers)
                self.assertEqual(explicit.status_code, 200)
                get_script.assert_not_called()
                importer.assert_not_called()

    def test_claim_rechecks_terminal_receipt_after_concurrent_completion(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, source, tasks
            from fls_manager.routes.online_scripts import install_select

            source.save_online_script_cache([self.item()])
            request_id = "a" * 32
            item = source.normalize_online_scripts([self.item()])[0]
            record = {"request_id": request_id, "script_id": "demo", "expected_version": tasks.online_task_import_version(item),
                      "selected_task_indexes": ["1"], "enable_task": False, "state": "failed", "task_count": 2,
                      "message": "old failed attempt", "scheduler_reload_error": "", "updated_at": "old"}
            constants.persist_online_task_import_request(record)
            real_claim = install_select.claim_online_task_import_request

            def complete_then_claim(info):
                record.update(state="completed", message="concurrent completed result")
                constants.persist_online_task_import_request(record)
                return real_claim(info)

            with patch.object(install_select, "claim_online_task_import_request", side_effect=complete_then_claim), patch.object(install_select, "import_task_if_needed") as importer, patch.object(install_select, "reload_scheduler") as reload:
                response = app.test_client().post("/api/online-scripts/import-tasks/demo", json={"request_id": request_id, "task_indexes": "1"}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json["replayed"])
                self.assertEqual(response.json["msg"], "concurrent completed result")
                importer.assert_not_called()
                reload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
