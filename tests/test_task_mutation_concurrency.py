import threading
import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class TaskMutationConcurrencyTests(unittest.TestCase):
    def start_pair(self, first, second):
        barrier = threading.Barrier(2)
        results = {}
        errors = []

        def run(name, callback):
            try:
                barrier.wait(timeout=2)
                results[name] = callback()
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=("first", first)),
            threading.Thread(target=run, args=("second", second)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive(), "task mutation did not finish")
        self.assertEqual(errors, [])
        return results

    def test_page_creation_and_online_import_preserve_both_tasks(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import models
            from fls_manager.online_scripts import tasks as online_tasks
            from fls_manager.routes.tasks import pages

            gate = threading.Barrier(2)
            active_reloads = 0
            max_active_reloads = 0
            reload_guard = threading.Lock()

            def concurrent_mutate(*args, **kwargs):
                gate.wait(timeout=2)
                return models.mutate_tasks(*args, **kwargs)

            def record_reload():
                nonlocal active_reloads, max_active_reloads
                with reload_guard:
                    active_reloads += 1
                    max_active_reloads = max(max_active_reloads, active_reloads)
                time.sleep(0.02)
                with reload_guard:
                    active_reloads -= 1

            item = {
                "id": "online-demo",
                "name": "Online demo",
                "type": "raw",
                "link": "https://example.invalid/demo.py",
                "task_cron": [{"name": "Imported task", "command": "task imported.py"}],
            }

            def create_page_task():
                return app.test_client().post(
                    "/task/new",
                    data={"name": "Manual task", "command": "task manual.py", "enabled": "1"},
                    headers={"X-Token": TOKEN},
                ).status_code

            def import_online_task():
                return online_tasks.import_task_if_needed(item)

            with patch.object(pages, "mutate_tasks", side_effect=concurrent_mutate), \
                    patch.object(online_tasks, "mutate_tasks", side_effect=concurrent_mutate), \
                    patch.object(pages, "reload_scheduler", side_effect=record_reload), \
                    patch.object(online_tasks, "reload_scheduler", side_effect=record_reload):
                results = self.start_pair(create_page_task, import_online_task)

            self.assertEqual(results["first"], 302)
            imported, _message = results["second"]
            self.assertTrue(imported)
            self.assertEqual(
                {task["name"] for task in models.load_tasks()},
                {"Manual task", "Imported task"},
            )
            self.assertEqual(max_active_reloads, 1)

    def test_task_edit_and_run_count_update_preserve_independent_fields(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import models, task_runner
            from fls_manager.routes.tasks import pages

            models.save_tasks([{
                "id": "task-1",
                "name": "Before",
                "command": "task before.py",
                "enabled": True,
                "run_count": 0,
            }])
            gate = threading.Barrier(2)

            def concurrent_mutate(*args, **kwargs):
                gate.wait(timeout=2)
                return models.mutate_tasks(*args, **kwargs)

            def edit_task():
                return app.test_client().post(
                    "/task/edit/task-1",
                    data={"name": "After", "command": "task after.py", "enabled": "1"},
                    headers={"X-Token": TOKEN},
                ).status_code

            def update_run_count():
                task_runner.increase_run_count("task-1")
                return True

            with patch.object(pages, "mutate_tasks", side_effect=concurrent_mutate), \
                    patch.object(task_runner, "mutate_tasks", side_effect=concurrent_mutate), \
                    patch.object(pages, "reload_scheduler"):
                results = self.start_pair(edit_task, update_run_count)

            self.assertEqual(results["first"], 302)
            self.assertTrue(results["second"])
            task = models.load_tasks()[0]
            self.assertEqual(task["name"], "After")
            self.assertEqual(task["command"], "task after.py")
            self.assertEqual(task["run_count"], 1)

    def test_api_reports_scheduler_reload_error_after_persisting_toggle(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import save_tasks

            save_tasks([{
                "id": "task-1",
                "name": "Demo",
                "command": "task demo.py",
                "enabled": True,
            }])

            with patch(
                "fls_manager.routes.api.reload_scheduler",
                side_effect=RuntimeError("scheduler unavailable"),
            ):
                response = app.test_client().post(
                    "/api/task/action/toggle/task-1",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.get_json(), {"ok": False, "msg": "scheduler unavailable"})
            persisted = (base_dir / "data" / "tasks.json").read_text(encoding="utf-8")
            self.assertIn('"enabled": false', persisted)

    def test_task_create_and_edit_report_scheduler_reload_failure_after_persisting(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import models
            from fls_manager.routes.tasks import pages

            models.save_collections([{
                "id": "c1",
                "name": "合集一",
                "remark": "",
                "created_at": "2026-07-04 00:00:00",
                "updated_at": "2026-07-04 00:00:00",
            }])
            client = app.test_client()

            with patch.object(
                pages,
                "reload_scheduler",
                side_effect=RuntimeError("scheduler unavailable"),
            ):
                created = client.post(
                    "/task/new",
                    data={
                        "name": "新任务",
                        "command": "task new.py",
                        "collection_id": "c1",
                        "enabled": "1",
                    },
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(created.status_code, 500)
            self.assertIn("任务已保存", created.get_data(as_text=True))
            self.assertIn("scheduler unavailable", created.get_data(as_text=True))
            task = models.load_tasks()[0]
            self.assertEqual(task["collection_id"], "c1")
            task_id = task["id"]

            with patch.object(
                pages,
                "reload_scheduler",
                side_effect=RuntimeError("scheduler unavailable"),
            ):
                edited = client.post(
                    f"/task/edit/{task_id}",
                    data={
                        "name": "编辑后",
                        "command": "task edited.py",
                        "collection_id": "c1",
                        "enabled": "1",
                    },
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(edited.status_code, 500)
            self.assertIn("任务已保存", edited.get_data(as_text=True))
            self.assertIn("scheduler unavailable", edited.get_data(as_text=True))
            persisted = models.load_tasks()[0]
            self.assertEqual(persisted["name"], "编辑后")
            self.assertEqual(persisted["command"], "task edited.py")
            self.assertEqual(persisted["collection_id"], "c1")

            rejected_create = client.post(
                "/task/new",
                data={
                    "name": "无效归属",
                    "command": "task invalid.py",
                    "collection_id": "missing",
                    "enabled": "1",
                },
                headers={"X-Token": TOKEN},
            )
            rejected_edit = client.post(
                f"/task/edit/{task_id}",
                data={
                    "name": "不应保存",
                    "command": "task invalid.py",
                    "collection_id": "missing",
                    "enabled": "1",
                },
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(rejected_create.status_code, 400)
            self.assertEqual(rejected_edit.status_code, 400)
            persisted = models.load_tasks()[0]
            self.assertEqual(persisted["name"], "编辑后")
            self.assertEqual(persisted["collection_id"], "c1")

    def test_task_create_and_collection_delete_serialize_membership_validation(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import models
            from fls_manager.routes.tasks import collections as collection_routes
            from fls_manager.routes.tasks import pages

            collection = {
                "id": "c1",
                "name": "合集一",
                "remark": "",
                "created_at": "2026-07-04 00:00:00",
                "updated_at": "2026-07-04 00:00:00",
            }
            models.save_collections([collection])
            checked = threading.Event()
            release_create = threading.Event()
            delete_saved = threading.Event()
            results = {}
            real_get_collection = pages.get_collection
            real_save_collections = collection_routes.save_collections

            def delayed_get_collection(collection_id):
                result = real_get_collection(collection_id)
                if threading.current_thread().name == "task-create":
                    checked.set()
                    if not release_create.wait(2):
                        raise TimeoutError("task create was not released")
                return result

            def track_collection_save(collections):
                if not any(item.get("id") == "c1" for item in collections):
                    delete_saved.set()
                return real_save_collections(collections)

            def create_task():
                response = app.test_client().post(
                    "/task/new",
                    data={
                        "name": "并发任务",
                        "command": "task concurrent.py",
                        "collection_id": "c1",
                        "enabled": "1",
                    },
                    headers={"X-Token": TOKEN},
                )
                results["create"] = response.status_code

            def delete_collection():
                response = app.test_client().post(
                    "/collection/delete/c1",
                    headers={"X-Token": TOKEN},
                )
                results["delete"] = response.status_code

            create_thread = threading.Thread(name="task-create", target=create_task)
            delete_thread = threading.Thread(name="collection-delete", target=delete_collection)
            with patch.object(pages, "get_collection", side_effect=delayed_get_collection), \
                    patch.object(
                        collection_routes,
                        "save_collections",
                        side_effect=track_collection_save,
                    ), patch.object(pages, "reload_scheduler"):
                create_thread.start()
                try:
                    self.assertTrue(checked.wait(2))
                    delete_thread.start()
                    self.assertFalse(delete_saved.wait(0.05))
                finally:
                    release_create.set()

                create_thread.join(timeout=2)
                delete_thread.join(timeout=2)

            self.assertFalse(create_thread.is_alive(), "task create did not finish")
            self.assertFalse(delete_thread.is_alive(), "collection delete did not finish")
            self.assertEqual(results, {"create": 302, "delete": 302})
            self.assertEqual(models.load_collections(), [])
            persisted = models.load_tasks()[0]
            self.assertEqual(persisted["collection_id"], "")


if __name__ == "__main__":
    unittest.main()
