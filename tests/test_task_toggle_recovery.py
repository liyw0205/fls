import time
import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import isolated_app, TOKEN, sample_task
from tests.test_task_save_recovery import TaskFormParser


class TaskToggleRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        models.save_tasks([sample_task("a", command="echo toggle", collection_id="c1", cron="0 8 * * *")])
        models.save_collections([{"id": "c1", "name": "Collection"}])

    def test_api_explicit_state_replay_preserves_saved_disable_after_reload_failure(self):
        with isolated_app() as (app, _):
            from fls_manager import models, scheduler
            self.seed()
            scheduler.reload_scheduler()
            client = app.test_client()
            data = {"enabled": "0", "request_id": "d" * 32}
            with patch("fls_manager.routes.api.reload_scheduler", side_effect=OSError("reload rejected")):
                failed = client.post("/api/task/action/toggle/a", json=data, headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 503)
            self.assertTrue(failed.get_json()["saved"])
            self.assertFalse(failed.get_json()["enabled"])
            self.assertFalse(models.get_task("a")["enabled"])
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                replay = client.post("/api/task/action/toggle/a", json=data, headers={"X-Token": TOKEN})
                self.assertEqual(replay.get_json(), failed.get_json())
                mutate.assert_not_called()
            self.assertEqual(client.post("/api/scheduler/reload", headers={"X-Token": TOKEN}).status_code, 200)
            self.assertIsNone(scheduler.scheduler.get_job("task_a"))
            self.assertFalse(models.get_task("a")["enabled"])

    def test_collection_form_save_failure_retry_and_saved_reload_recovery_keep_target_state(self):
        with isolated_app() as (app, _):
            from fls_manager import models, scheduler
            self.seed()
            scheduler.reload_scheduler()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="toggle-csrf")
            page = client.get("/collections")
            form = next(form for form in TaskFormParser(page.get_data(as_text=True)).forms if form["action"].startswith("/task/toggle/a"))
            self.assertEqual(form["fields"]["enabled"], "0")
            with patch("fls_manager.routes.tasks.actions.mutate_tasks", side_effect=OSError("disk full")):
                failed = client.post(form["action"], data=form["fields"], headers={"X-Requested-With": "FLS-Ajax"})
            self.assertEqual(failed.status_code, 500)
            self.assertTrue(models.get_task("a")["enabled"])
            self.assertIn("data-task-toggle-recovery", failed.get_data(as_text=True))
            retry = next(form for form in TaskFormParser(failed.get_data(as_text=True)).forms if form["action"].startswith("/task/toggle/"))
            self.assertNotEqual(retry["fields"]["request_id"], form["fields"]["request_id"])
            with patch("fls_manager.routes.tasks.actions.reload_scheduler", side_effect=OSError("reload rejected")):
                saved = client.post(retry["action"], data=retry["fields"])
            self.assertEqual(saved.status_code, 503)
            self.assertFalse(models.get_task("a")["enabled"])
            recovery = TaskFormParser(saved.get_data(as_text=True)).forms[0]
            self.assertTrue(recovery["action"].startswith("/scheduler/reload?back="))
            for _ in range(2):
                self.assertEqual(client.post(recovery["action"], data=recovery["fields"]).status_code, 302)
            self.assertFalse(models.get_task("a")["enabled"])
            self.assertIsNone(scheduler.scheduler.get_job("task_a"))
            replay = client.post(retry["action"], data=retry["fields"])
            self.assertEqual(replay.status_code, 503)
            self.assertFalse(models.get_task("a")["enabled"])

    def test_invalid_state_csrf_permissions_and_expired_session_never_mutate(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            with patch("fls_manager.routes.api.mutate_tasks") as api_mutate, patch("fls_manager.routes.tasks.actions.mutate_tasks") as form_mutate:
                for value in ("false", 1, 0, 0.0, [], {}, "", "yes", None):
                    with self.subTest(value=value):
                        response = client.post("/api/task/action/toggle/a", json={"enabled": value}, headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 400)
                self.assertEqual(client.post("/api/task/action/toggle/a", json={"enabled": "0", "request_id": "bad"}, headers={"X-Token": TOKEN}).status_code, 400)
                for url in ("/api/task/action/toggle/a", "/task/toggle/a"):
                    with self.subTest(url=url):
                        with client.session_transaction() as session:
                            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="toggle-csrf")
                        self.assertEqual(client.post(url, data={"enabled": "0"}).status_code, 400)
                        with patch("fls_manager.auth.query_token_enabled", return_value=False):
                            self.assertEqual(client.post(url + "?token=" + TOKEN, data={"enabled": "0", "csrf_token": "toggle-csrf"}, headers={"X-Requested-With": "XMLHttpRequest"}).status_code, 403)
                        with client.session_transaction() as session:
                            session["token_expire_at"] = 1
                        self.assertEqual(client.post(url, data={"enabled": "0", "csrf_token": "toggle-csrf"}, headers={"X-Requested-With": "XMLHttpRequest"}).status_code, 401)
                api_mutate.assert_not_called()
                form_mutate.assert_not_called()

    def test_successful_repeated_form_and_api_requests_do_not_toggle_back(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            for url in ("/task/toggle/a", "/api/task/action/toggle/a"):
                with self.subTest(url=url):
                    for _ in range(2):
                        response = client.post(url, data={"enabled": "0", "request_id": "e" * 32}, headers={"X-Token": TOKEN})
                        self.assertIn(response.status_code, (200, 302))
                        self.assertFalse(models.get_task("a")["enabled"])

    def test_log_toggle_recovery_returns_to_log_and_old_form_cannot_reverse_later_state(self):
        with isolated_app() as (app, _):
            from fls_manager import models, scheduler
            self.seed()
            scheduler.reload_scheduler()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="toggle-csrf")
            page = client.get("/log/a?back=/collections")
            form = next(form for form in TaskFormParser(page.get_data(as_text=True)).forms
                        if form["action"].startswith("/task/toggle/a"))
            self.assertEqual(form["fields"]["enabled"], "0")
            self.assertRegex(form["fields"]["request_id"], r"^[a-f0-9]{32}$")
            with patch("fls_manager.routes.tasks.actions.mutate_tasks", side_effect=OSError("disk full")):
                failed = client.post(form["action"], data=form["fields"])
            self.assertEqual(failed.status_code, 500)
            self.assertTrue(models.get_task("a")["enabled"])
            retry = TaskFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertEqual(retry["fields"]["enabled"], "0")
            with patch("fls_manager.routes.tasks.actions.reload_scheduler", side_effect=OSError("reload rejected")):
                saved = client.post(retry["action"], data=retry["fields"])
            self.assertEqual(saved.status_code, 503)
            self.assertFalse(models.get_task("a")["enabled"])
            recovery = TaskFormParser(saved.get_data(as_text=True)).forms[0]
            response = client.post(recovery["action"], data=recovery["fields"])
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, "/log/a?back=%2Fcollections")
            self.assertIsNone(scheduler.scheduler.get_job("task_a"))
            current = client.get(response.location)
            enable = next(form for form in TaskFormParser(current.get_data(as_text=True)).forms
                          if form["action"].startswith("/task/toggle/a"))
            self.assertEqual(enable["fields"]["enabled"], "1")
            self.assertEqual(client.post(enable["action"], data=enable["fields"]).status_code, 302)
            self.assertTrue(models.get_task("a")["enabled"])
            self.assertIsNotNone(scheduler.scheduler.get_job("task_a"))
            with patch("fls_manager.routes.tasks.actions.mutate_tasks") as mutate:
                self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 503)
                mutate.assert_not_called()
            self.assertTrue(models.get_task("a")["enabled"])
