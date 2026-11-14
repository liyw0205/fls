import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineTaskImportConcurrencyTests(unittest.TestCase):
    def run_concurrent_imports(self, importer, items):
        gate = threading.Barrier(len(items) + 1)
        results = []
        errors = []

        def run(item):
            try:
                gate.wait(timeout=2)
                results.append(importer(item))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(item,)) for item in items]
        for thread in threads:
            thread.start()
        gate.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "online task import did not finish")
        self.assertEqual(errors, [])
        return results

    def task_item(self, script_id, task_name):
        return {
            "id": script_id,
            "name": f"Script {script_id}",
            "type": "raw",
            "link": f"https://example.invalid/{script_id}.py",
            "task_cron": [{"name": task_name, "command": f"task {script_id}"}],
        }

    def test_concurrent_same_script_import_is_idempotent(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import tasks

            item = self.task_item("demo", "Daily")
            with patch.object(tasks, "reload_scheduler") as reload_scheduler:
                results = self.run_concurrent_imports(
                    lambda current: tasks.import_task_if_needed(current), [item, item],
                )

            self.assertEqual(sum(bool(result[0]) for result in results), 1)
            self.assertEqual(len(load_tasks()), 1)
            self.assertEqual(load_tasks()[0]["online_script_task_key"], "demo:1:Daily")
            reload_scheduler.assert_called_once()

    def test_concurrent_distinct_imports_preserve_both_task_sets(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import tasks

            items = [self.task_item("alpha", "Alpha task"), self.task_item("beta", "Beta task")]
            active_reloads = 0
            max_active_reloads = 0
            reload_guard = threading.Lock()

            def reload_scheduler():
                nonlocal active_reloads, max_active_reloads
                with reload_guard:
                    active_reloads += 1
                    max_active_reloads = max(max_active_reloads, active_reloads)
                time.sleep(0.03)
                with reload_guard:
                    active_reloads -= 1

            with patch.object(tasks, "reload_scheduler", side_effect=reload_scheduler) as reload_mock:
                results = self.run_concurrent_imports(
                    lambda current: tasks.import_task_if_needed(current), items,
                )

            self.assertTrue(all(result[0] for result in results))
            self.assertEqual(
                {task["online_script_id"] for task in load_tasks()},
                {"alpha", "beta"},
            )
            self.assertEqual(reload_mock.call_count, 2)
            self.assertEqual(max_active_reloads, 1)

    def test_persistence_failure_is_reported_without_scheduler_reload(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.online_scripts import tasks

            item = self.task_item("demo", "Daily")
            with patch("fls_manager.models.mutate_json", side_effect=OSError("task store is read-only")), patch.object(
                tasks, "reload_scheduler",
            ) as reload_scheduler:
                with self.assertRaisesRegex(OSError, "task store is read-only"):
                    tasks.import_task_if_needed(item)
            reload_scheduler.assert_not_called()

    def test_legacy_marker_and_task_selection_are_preserved(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.models import load_tasks, save_tasks
            from fls_manager.online_scripts import tasks

            save_tasks([{
                "id": "legacy",
                "name": "Legacy task",
                "command": "task demo",
                "online_script_id": "demo",
            }])
            item = self.task_item("demo", "Daily")
            with patch.object(tasks, "reload_scheduler") as reload_scheduler:
                imported, _message = tasks.import_task_if_needed(item)
            self.assertFalse(imported)
            reload_scheduler.assert_not_called()

            item["task_cron"].append({"name": "Hourly", "command": "task demo-hourly"})
            with patch.object(tasks, "reload_scheduler"):
                imported, _message = tasks.import_task_if_needed(item, selected_task_indexes=[2])
            self.assertTrue(imported)
            self.assertEqual(
                {task.get("online_script_task_key") for task in load_tasks()},
                {None, "demo:2:Hourly"},
            )

    def test_scheduler_reload_error_reports_that_task_was_persisted(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import tasks

            with patch.object(tasks, "reload_scheduler", side_effect=RuntimeError("scheduler unavailable")):
                imported, message = tasks.import_task_if_needed(self.task_item("demo", "Daily"))

            self.assertTrue(imported)
            self.assertIn("任务已持久化", message)
            self.assertIn("scheduler unavailable", message)
            self.assertEqual(len(load_tasks()), 1)

    def test_import_route_keeps_storage_error_visible(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps([self.task_item("demo", "Daily")]), encoding="utf-8")
            with patch("fls_manager.models.mutate_json", side_effect=OSError("task store is read-only")):
                response = app.test_client().post(
                    "/online-scripts/import-tasks/demo",
                    data={"task_indexes": "1"},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            query = parse_qs(urlsplit(response.headers["Location"]).query)
            self.assertIn("task store is read-only", query["err"][0])


if __name__ == "__main__":
    unittest.main()
