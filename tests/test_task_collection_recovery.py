import importlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task
from tests.test_task_save_recovery import TaskFormParser


class TaskCollectionRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        models.save_collections([{"id": "c1", "name": "First"}, {"id": "c2", "name": "Second"}])
        models.save_tasks([sample_task("a", command="echo collection", collection_id="c1")])

    def post(self, client, request_id="a" * 32, expected="c1"):
        return client.post("/api/task/action/clear_collection/a", json={
            "request_id": request_id, "expected_collection": expected,
        }, headers={"X-Token": TOKEN})

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                           security_verified=True, csrf_token="collection-csrf")

    def move(self, collection):
        from fls_manager import models
        def update(tasks):
            tasks[0]["collection_id"] = collection
            return True
        models.mutate_tasks(update)

    def test_replay_after_restart_preserves_later_membership_and_rejects_changed_selection(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_bulk
            self.seed()
            first = self.post(app.test_client())
            self.assertEqual(first.status_code, 200)
            self.assertEqual(models.get_task("a")["collection_id"], "")
            self.move("c2")
            importlib.reload(task_bulk)
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                replay = self.post(app.test_client())
                self.assertEqual(replay.get_json(), first.get_json())
                self.assertEqual(self.post(app.test_client(), expected="c2").status_code, 409)
                mutate.assert_not_called()
            self.assertEqual(models.get_task("a")["collection_id"], "c2")

    def test_stale_form_and_transaction_membership_change_have_zero_write(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            self.move("c2")
            before = models.load_tasks()
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                stale = self.post(app.test_client())
                self.assertEqual(stale.status_code, 409)
                self.assertFalse(stale.get_json()["saved"])
                self.assertEqual(stale.get_json()["expected_collection"], "c1")
                mutate.assert_not_called()
            self.assertEqual(models.load_tasks(), before)
            self.move("c1")
            mutate = models.mutate_tasks
            def change_before_update(*args, **kwargs):
                self.move("c2")
                return mutate(*args, **kwargs)
            with patch("fls_manager.routes.api.mutate_tasks", side_effect=change_before_update), patch("fls_manager.routes.api.reload_scheduler") as reload:
                changed = self.post(app.test_client(), request_id="b" * 32)
            self.assertEqual(changed.status_code, 409)
            self.assertFalse(changed.get_json()["saved"])
            self.assertEqual(models.get_task("a")["collection_id"], "c2")
            reload.assert_not_called()

    def test_atomic_file_failure_before_and_after_publication_reports_actual_membership(self):
        for published in (False, True):
            with self.subTest(published=published), isolated_app() as (app, _):
                from fls_manager import models, paths
                self.seed()
                replace = Path.replace
                def fail(path, target):
                    if target == paths.TASK_FILE:
                        if published:
                            replace(path, target)
                        raise OSError("publication fault")
                    return replace(path, target)
                with patch.object(Path, "replace", fail), patch("fls_manager.routes.api.reload_scheduler") as reload:
                    response = self.post(app.test_client())
                self.assertEqual(response.status_code, 503 if published else 500)
                self.assertEqual(response.get_json()["saved"], published)
                self.assertFalse(response.get_json().get("reload_required"))
                self.assertEqual(models.get_task("a")["collection_id"], "" if published else "c1")
                reload.assert_not_called()

    def test_lost_receipt_reconciles_without_removing_later_collection(self):
        with isolated_app() as (app, _):
            from fls_manager import task_bulk, models
            self.seed()
            write = task_bulk.write_json
            def fail(path, record):
                if "payload" in record:
                    raise OSError("receipt full")
                return write(path, record)
            with patch.object(task_bulk, "write_json", side_effect=fail):
                first = self.post(app.test_client())
            self.assertEqual(first.status_code, 500)
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                replay = self.post(app.test_client())
                self.assertEqual(replay.status_code, 409)
                self.assertTrue(replay.get_json()["saved"])
                self.assertEqual(replay.get_json()["completed_ids"], ["a"])
                mutate.assert_not_called()
            self.move("c2")
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                unknown = self.post(app.test_client())
                self.assertEqual(unknown.get_json()["unknown_ids"], ["a"])
                self.assertIsNone(unknown.get_json()["saved"])
                mutate.assert_not_called()
            self.assertEqual(models.get_task("a")["collection_id"], "c2")

    def test_form_save_failure_retry_and_replay_do_not_touch_new_collection(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            page = client.get("/collections")
            form = next(form for form in TaskFormParser(page.get_data(as_text=True)).forms if form["action"].startswith("/task/collection/clear/a"))
            self.assertEqual(form["fields"]["expected_collection"], "c1")
            with patch("fls_manager.routes.tasks.actions.mutate_tasks", side_effect=OSError("disk full")):
                failed = client.post(form["action"], data=form["fields"])
            self.assertEqual(failed.status_code, 500)
            retry = TaskFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertNotEqual(retry["fields"]["request_id"], form["fields"]["request_id"])
            self.assertEqual(retry["fields"]["expected_collection"], "c1")
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertEqual(models.get_task("a")["collection_id"], "")
            self.move("c2")
            with patch("fls_manager.routes.tasks.actions.mutate_tasks") as mutate:
                self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
                self.assertEqual(client.post(form["action"], data=form["fields"]).status_code, 500)
                mutate.assert_not_called()
            self.assertEqual(models.get_task("a")["collection_id"], "c2")

    def test_invalid_selection_permissions_csrf_claim_and_corrupt_record_do_not_mutate(self):
        with isolated_app() as (app, base):
            from fls_manager import task_bulk
            self.seed()
            client = app.test_client()
            before = (base / "data/tasks.json").read_bytes()
            with patch("fls_manager.routes.api.mutate_tasks") as api_mutate, patch("fls_manager.routes.tasks.actions.mutate_tasks") as form_mutate:
                for expected in (None, 1, [], {}, "bad\x00collection"):
                    self.assertEqual(self.post(client, expected=expected).status_code, 400)
                for request_id in ("", None, True, "../bad"):
                    self.assertEqual(self.post(client, request_id=request_id).status_code, 400)
                self.authenticate(client)
                data = {"request_id": "b" * 32, "expected_collection": "c1", "csrf_token": "collection-csrf"}
                self.assertEqual(client.post("/task/collection/clear/a", data={"request_id": "b" * 32}).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    self.assertEqual(client.post("/task/collection/clear/a?token=" + TOKEN, data=data, headers={"X-Requested-With": "XMLHttpRequest"}).status_code, 403)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post("/task/collection/clear/a", data=data, headers={"X-Requested-With": "XMLHttpRequest"}).status_code, 401)
                with patch.object(task_bulk, "write_json", side_effect=OSError("claim full")):
                    self.assertEqual(self.post(client).status_code, 500)
                record = task_bulk._record_path("a" * 32)
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({"request_id": "a" * 32, "action": "clear_collection", "task_ids": ["a"],
                                              "results": [{"task_id": "a", "status": "unknown", "msg": "interrupted"}], "expected_collections": []}))
                self.assertEqual(self.post(client).status_code, 500)
                api_mutate.assert_not_called()
                form_mutate.assert_not_called()
            self.assertEqual((base / "data/tasks.json").read_bytes(), before)
