import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class TaskFormParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action", ""), "fields": {}, "method": attrs.get("method")}
            self.forms.append(self.current)
        if tag == "input" and self.current is not None and attrs.get("type") == "hidden":
            self.current["fields"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class TaskSaveRecoveryTests(unittest.TestCase):
    def seed(self, base_dir):
        from fls_manager import models, scheduler
        from fls_manager.config import save_config

        save_config({"admin_token": TOKEN, "notify_items": [
            {"id": "notice", "name": "Notice", "channel": "bark", "enabled": True, "config": {}},
            {"id": "disabled-notice", "name": "Disabled", "channel": "bark", "enabled": False, "config": {}},
        ]})
        models.save_proxies([
            {"id": "proxy", "name": "Proxy", "host": "127.0.0.1", "port": "9", "enabled": True},
            {"id": "disabled-proxy", "name": "Disabled", "enabled": False},
        ])
        models.save_tasks([{
            "id": "existing", "name": "Existing", "command": "echo existing",
            "cron": "0 8 * * *", "enabled": True,
        }])
        scheduler.reload_scheduler()
        return base_dir / "data" / "tasks.json"

    def draft(self, **updates):
        data = {"name": "New task", "command": "echo new", "cron": "0 9 * * *", "enabled": "1",
                "notify_mode": "default", "random_delay_mode": "none", "back": "/tasks?q=recovery"}
        data.update(updates)
        return data

    def new_form(self, client, headers=None):
        response = client.get("/task/new", headers=headers or {"X-Token": TOKEN})
        self.assertEqual(response.status_code, 200)
        return TaskFormParser(response.get_data(as_text=True)).forms[0]["fields"]

    def test_create_storage_failure_keeps_draft_token_and_real_file_then_retry_saves_once(self):
        for ajax in (False, True):
            with self.subTest(ajax=ajax), isolated_app() as (app, base_dir):
                from fls_manager import scheduler, storage

                target = self.seed(base_dir)
                before = target.read_bytes()
                client = app.test_client()
                fields = self.new_form(client)
                headers = {"X-Token": TOKEN, **({"X-Requested-With": "FLS-Ajax"} if ajax else {})}
                real_write = storage._write_json_unlocked

                def write(path, data):
                    if path == target:
                        raise OSError("task disk unavailable")
                    return real_write(path, data)

                with patch.object(storage, "_write_json_unlocked", side_effect=write), patch(
                    "fls_manager.routes.tasks.pages.reload_scheduler",
                ) as reload:
                    response = client.post("/task/new", data={**fields, **self.draft()}, headers=headers)
                self.assertEqual(response.status_code, 500)
                self.assertEqual(target.read_bytes(), before)
                reload.assert_not_called()
                html = response.get_data(as_text=True)
                self.assertIn("task disk unavailable", html)
                self.assertIn("echo new", html)
                retry = TaskFormParser(html).forms[0]["fields"]
                self.assertEqual(retry["create_id"], fields["create_id"])
                saved = client.post("/task/new", data={**retry, **self.draft()}, headers=headers)
                repeated = client.post("/task/new", data={**retry, **self.draft()}, headers=headers)
                self.assertEqual((saved.status_code, repeated.status_code), (302, 302))
                self.assertEqual(len(json.loads(target.read_text())), 2)
                self.assertIsNotNone(scheduler.scheduler.get_job("task_" + fields["create_id"]))

    def test_edit_replace_failure_preserves_persisted_task_and_retries_from_error_form(self):
        with isolated_app() as (app, base_dir):
            from pathlib import Path

            target = self.seed(base_dir)
            before = target.read_bytes()
            client = app.test_client()
            real_replace = Path.replace

            def replace(path, destination):
                if destination == target:
                    raise PermissionError("task publication rejected")
                return real_replace(path, destination)

            with patch.object(Path, "replace", new=replace), patch("fls_manager.routes.tasks.pages.reload_scheduler") as reload:
                response = client.post("/task/edit/existing", data=self.draft(), headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 500)
            self.assertEqual(target.read_bytes(), before)
            self.assertIn("task publication rejected", response.get_data(as_text=True))
            reload.assert_not_called()
            fields = TaskFormParser(response.get_data(as_text=True)).forms[0]["fields"]
            retried = client.post("/task/edit/existing", data={**fields, **self.draft()}, headers={"X-Token": TOKEN})
            self.assertEqual(retried.status_code, 302)
            self.assertEqual(json.loads(target.read_text())[0]["name"], "New task")

    def test_reload_failure_recovery_only_applies_saved_tasks_with_session_csrf(self):
        for creating in (False, True):
            with self.subTest(creating=creating), isolated_app() as (app, base_dir):
                from fls_manager import scheduler

                target = self.seed(base_dir)
                client = app.test_client()
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="task-save-csrf")
                headers = {"X-Requested-With": "FLS-Ajax"}
                fields = self.new_form(client, headers)
                url = "/task/new" if creating else "/task/edit/existing"
                with patch.object(scheduler, "_apply_scheduler_plan", side_effect=RuntimeError("scheduler application rejected")):
                    response = client.post(url, data={**fields, **self.draft()}, headers=headers)
                self.assertEqual(response.status_code, 503)
                html = response.get_data(as_text=True)
                self.assertIn("任务已保存", html)
                self.assertIn("scheduler application rejected", html)
                self.assertNotIn('name="command"', html)
                recovery = TaskFormParser(html).forms[0]
                self.assertEqual(recovery["method"], "post")
                self.assertEqual(recovery["fields"]["csrf_token"], "task-save-csrf")
                self.assertTrue(recovery["action"].startswith("/scheduler/reload?back="))
                persisted = target.read_bytes()
                count = len(json.loads(persisted))
                status = client.get("/api/scheduler/status", headers=headers).get_json()
                self.assertEqual(status["status"], "failed")
                with patch.object(scheduler, "_apply_scheduler_plan", side_effect=RuntimeError("still unavailable")):
                    failed = client.post(recovery["action"], data=recovery["fields"], headers=headers)
                self.assertEqual(failed.status_code, 500)
                self.assertEqual(target.read_bytes(), persisted)
                retry = TaskFormParser(failed.get_data(as_text=True)).forms[0]
                for _ in range(2):
                    restored = client.post(retry["action"], data=retry["fields"], headers=headers)
                    self.assertEqual(restored.status_code, 302)
                    self.assertEqual(restored.headers["Location"], "/tasks?q=recovery")
                    self.assertEqual(target.read_bytes(), persisted)
                self.assertEqual(len(json.loads(target.read_text())), count)
                task_id = fields["create_id"] if creating else "existing"
                self.assertIsNotNone(scheduler.scheduler.get_job("task_" + task_id))
                self.assertEqual(client.get("/api/scheduler/status", headers=headers).get_json()["status"], "succeeded")

    def test_duplicate_form_and_concurrent_submissions_never_create_another_task(self):
        with isolated_app() as (app, base_dir):
            target = self.seed(base_dir)
            fields = self.new_form(app.test_client())
            barrier = threading.Barrier(2)

            def create():
                barrier.wait(timeout=3)
                return app.test_client().post("/task/new", data={**fields, **self.draft()}, headers={"X-Token": TOKEN})

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(create) for _ in range(2)]
                responses = [future.result(timeout=5) for future in futures]
            self.assertEqual([response.status_code for response in responses], [302, 302])
            tasks = json.loads(target.read_text())
            self.assertEqual(len(tasks), 2)
            before = target.read_bytes()
            with patch("fls_manager.routes.tasks.pages.reload_scheduler") as reload:
                changed = app.test_client().post("/task/new", data={**fields, **self.draft(command="echo different")}, headers={"X-Token": TOKEN})
            self.assertEqual(changed.status_code, 409)
            self.assertIn(f'/task/edit/{fields["create_id"]}', changed.get_data(as_text=True))
            self.assertEqual(target.read_bytes(), before)
            reload.assert_not_called()

    def test_invalid_selection_and_command_have_zero_task_or_scheduler_side_effects(self):
        invalid = (
            {"proxy_id": "missing"}, {"proxy_id": "disabled-proxy"},
            {"notify_mode": "custom", "notify_ids": "missing"},
            {"notify_mode": " custom ", "notify_ids": "missing"},
            {"notify_mode": "custom", "notify_ids": "disabled-notice"},
            {"notify_mode": "custom"}, {"notify_mode": "invalid"},
            {"random_delay_mode": "invalid"}, {"collection_id": "missing"},
            {"config_path": "../outside.yml"}, {"config_path": "/etc/config.yml"},
            {"command": "echo\x00bad"}, {"env_key": "BAD=KEY", "env_value": "x"},
            {"env_key": "KEY", "env_value": "\x00"}, {"env_key": "", "env_value": "lost"},
            {"env_key": ["KEY", "KEY"], "env_value": ["first", "second"]},
            {"env_key": "KEY"}, {"cron": "invalid"},
        )
        with isolated_app() as (app, base_dir):
            from fls_manager import scheduler

            target = self.seed(base_dir)
            before = target.read_bytes()
            original_jobs = [job.id for job in scheduler.scheduler.get_jobs()]
            for url in ("/task/new", "/task/edit/existing"):
                for selection in invalid:
                    with self.subTest(url=url, selection=selection), patch("fls_manager.routes.tasks.pages.reload_scheduler") as reload:
                        response = app.test_client().post(url, data=self.draft(**selection), headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"})
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(target.read_bytes(), before)
                        self.assertEqual([job.id for job in scheduler.scheduler.get_jobs()], original_jobs)
                        reload.assert_not_called()

    def test_disabled_selection_between_validation_and_commit_is_rejected(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import models
            from fls_manager.routes.tasks import pages

            target = self.seed(base_dir)
            before = target.read_bytes()
            original_mutate = models.mutate_tasks

            def mutate(*args, **kwargs):
                models.save_proxies([])
                return original_mutate(*args, **kwargs)

            with patch.object(pages, "mutate_tasks", side_effect=mutate), patch.object(pages, "reload_scheduler") as reload:
                response = app.test_client().post("/task/new", data=self.draft(proxy_id="proxy"), headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(target.read_bytes(), before)
            reload.assert_not_called()

    def test_corrupt_task_files_are_not_replaced_and_scheduler_reports_real_failure(self):
        for corrupted in (b"{broken", b"\xff", b'{}', b'null'):
            with self.subTest(corrupted=corrupted), isolated_app() as (app, base_dir):
                from fls_manager import scheduler

                target = self.seed(base_dir)
                original = target.read_bytes()
                original_jobs = [job.id for job in scheduler.scheduler.get_jobs()]
                client = app.test_client()
                fields = self.new_form(client)
                target.write_bytes(corrupted)
                for url in ("/task/new", "/task/edit/existing", "/task/config/existing"):
                    response = client.post(url, data={**fields, **self.draft(), "content": "config draft"}, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 500)
                    self.assertEqual(target.read_bytes(), corrupted)
                failed = client.post("/api/scheduler/reload", headers={"X-Token": TOKEN})
                self.assertEqual(failed.status_code, 500)
                self.assertEqual(failed.get_json()["state"]["status"], "failed")
                self.assertIn("未更改现有调度", failed.get_json()["msg"])
                self.assertEqual([job.id for job in scheduler.scheduler.get_jobs()], original_jobs)
                self.assertEqual(target.read_bytes(), corrupted)
                target.write_bytes(original)
                recovered = client.post("/task/new", data={**fields, **self.draft()}, headers={"X-Token": TOKEN})
                self.assertEqual(recovered.status_code, 302)
                self.assertEqual(len(json.loads(target.read_text())), 2)

    def test_task_read_permission_failure_keeps_file_and_submitted_draft(self):
        with isolated_app() as (app, base_dir):
            from pathlib import Path
            from fls_manager import models

            target = self.seed(base_dir)
            before = target.read_bytes()
            real_read = Path.read_text

            def read(path, *args, **kwargs):
                if path == target:
                    raise PermissionError("task file inaccessible")
                return real_read(path, *args, **kwargs)

            models._clear_tasks_read_cache()
            with patch.object(Path, "read_text", new=read), patch("fls_manager.routes.tasks.pages.reload_scheduler") as reload:
                for url in ("/task/new", "/task/edit/existing"):
                    response = app.test_client().post(url, data=self.draft(), headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 500)
                    self.assertIn("task file inaccessible", response.get_data(as_text=True))
                    self.assertIn("echo new", response.get_data(as_text=True))
                    self.assertEqual(target.read_bytes(), before)
                reload.assert_not_called()
            real_stat = Path.stat

            def stat(path, *args, **kwargs):
                if path == target:
                    raise PermissionError("task metadata inaccessible")
                return real_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", new=stat):
                with self.assertRaisesRegex(PermissionError, "task metadata inaccessible"):
                    models.load_tasks()

    def test_permissions_csrf_expiry_and_recovery_get_do_not_mutate_or_reload(self):
        with isolated_app() as (app, base_dir):
            target = self.seed(base_dir)
            before = target.read_bytes()
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="task-csrf")
            with patch("fls_manager.routes.tasks.pages.mutate_tasks") as mutate, patch("fls_manager.routes.dashboard.reload_scheduler") as reload:
                for url in ("/task/new", "/task/edit/existing", "/scheduler/reload"):
                    with self.subTest(url=url):
                        with client.session_transaction() as session:
                            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                           security_verified=True, csrf_token="task-csrf")
                        self.assertEqual(client.post(url, data=self.draft(), headers=headers).status_code, 400)
                        with client.session_transaction() as session:
                            session["token_expire_at"] = 1
                        self.assertEqual(client.post(url, data={**self.draft(), "csrf_token": "task-csrf"}, headers=headers).status_code, 401)
                        with client.session_transaction() as session:
                            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                           security_verified=True, csrf_token="task-csrf")
                        with patch("fls_manager.auth.query_token_enabled", return_value=False):
                            self.assertEqual(client.post(url, query_string={"token": TOKEN}, data={**self.draft(), "csrf_token": "task-csrf"}, headers=headers).status_code, 403)
                self.assertEqual(client.get("/scheduler/reload", headers=headers).status_code, 405)
                mutate.assert_not_called()
                reload.assert_not_called()
            self.assertEqual(target.read_bytes(), before)

    def test_scheduler_reload_and_task_write_share_lock_order(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager import models, scheduler

            self.seed(base_dir)
            sequence = []

            class TraceLock:
                def __init__(self, name):
                    self.name = name

                def __enter__(self):
                    sequence.append(self.name)

                def __exit__(self, *_args):
                    pass

            with patch.object(scheduler, "TASKS_MUTATION_LOCK", TraceLock("tasks")), patch.object(
                scheduler, "SCHEDULER_RELOAD_LOCK", TraceLock("scheduler"),
            ), patch.object(scheduler, "_reload_scheduler_locked", return_value={}):
                scheduler.reload_scheduler()
            self.assertEqual(sequence, ["tasks", "scheduler"])
            entered = threading.Event()
            release = threading.Event()

            def after_write():
                entered.set()
                self.assertTrue(release.wait(timeout=3))

            def change(tasks):
                tasks[0]["command"] = "echo edited"
                return True

            with ThreadPoolExecutor(max_workers=2) as pool:
                writer = pool.submit(models.mutate_tasks, change, scheduler.reload_scheduler, after_write)
                self.assertTrue(entered.wait(timeout=3))
                reloader = pool.submit(scheduler.reload_scheduler)
                release.set()
                writer.result(timeout=5)
                self.assertEqual(reloader.result(timeout=5)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
