import importlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task
from tests.test_task_save_recovery import TaskFormParser


class CollectionAddRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        models.save_collections([{"id": cid, "name": cid} for cid in ("c1", "c2", "c3")])
        models.save_tasks([sample_task("a", collection_id="", pinned=True),
                           sample_task("b", collection_id="c1", pinned=True)])

    def post(self, client, ids=None, expected=None, request_id="a" * 32, target="c2"):
        return client.post("/api/collection/add-task/" + target, json={
            "task_ids": ids if ids is not None else ["a", "b"],
            "expected_collections": expected if expected is not None else {"a": "", "b": "c1"},
            "request_id": request_id,
        }, headers={"X-Token": TOKEN})

    def move(self, task_id, target):
        from fls_manager import models
        def update(tasks):
            task = next(task for task in tasks if task["id"] == task_id)
            task.update(collection_id=target, pinned=True)
            return True
        models.mutate_tasks(update)

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                           security_verified=True, csrf_token="add-csrf")

    def test_real_save_reset_pin_no_reload_and_replay_preserves_later_assignment(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_bulk
            self.seed()
            with patch("fls_manager.routes.api.reload_scheduler") as reload:
                response = self.post(app.test_client())
                self.assertEqual(response.status_code, 200)
                reload.assert_not_called()
            self.assertTrue(response.get_json()["saved"])
            self.assertEqual(response.get_json()["completed_ids"], ["a", "b"])
            self.assertEqual(response.get_json()["updated_count"], 2)
            self.assertTrue(all(task["collection_id"] == "c2" and not task["pinned"] for task in models.load_tasks()))
            self.move("a", "c3")
            models.save_collections([])
            importlib.reload(task_bulk)
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                self.assertEqual(self.post(app.test_client()).get_json(), response.get_json())
                self.assertEqual(self.post(app.test_client(), target="c3").status_code, 409)
                self.assertEqual(self.post(app.test_client(), expected={"a": "c3", "b": "c1"}).status_code, 409)
                mutate.assert_not_called()
            self.assertEqual(models.get_task("a")["collection_id"], "c3")
            self.assertTrue(models.get_task("a")["pinned"])

    def test_concurrent_duplicate_mutates_once(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            entered, release = threading.Event(), threading.Event()
            mutate = models.mutate_tasks
            def delayed(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return mutate(*args, **kwargs)
            with patch("fls_manager.routes.api.mutate_tasks", side_effect=delayed) as write, ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client())
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client())
                release.set()
                self.assertEqual(first.result(5).get_json(), second.result(5).get_json())
                write.assert_called_once()

    def test_stale_selection_and_transaction_change_reject_entire_selection(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            self.move("b", "c3")
            before = (base / "data/tasks.json").read_bytes()
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                response = self.post(app.test_client())
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.get_json()["saved"])
                mutate.assert_not_called()
            self.assertEqual((base / "data/tasks.json").read_bytes(), before)
            self.assertFalse((base / "data/task_bulk_requests").exists())
            self.move("b", "c1")
            mutate = models.mutate_tasks
            def interleaved(*args, **kwargs):
                self.move("b", "c3")
                return mutate(*args, **kwargs)
            with patch("fls_manager.routes.api.mutate_tasks", side_effect=interleaved):
                response = self.post(app.test_client())
            self.assertEqual(response.status_code, 409)
            self.assertEqual(models.get_task("a")["collection_id"], "")
            self.assertEqual(models.get_task("b")["collection_id"], "c3")

    def test_file_publication_failure_reports_real_state_and_safe_retry(self):
        for published in (False, True):
            with self.subTest(published=published), isolated_app() as (app, _):
                from fls_manager import models, paths
                self.seed()
                replace = Path.replace
                def fail(path, target):
                    if target == paths.TASK_FILE:
                        if published:
                            replace(path, target)
                        raise OSError("publication failed")
                    return replace(path, target)
                with patch.object(Path, "replace", fail):
                    response = self.post(app.test_client())
                self.assertEqual(response.status_code, 503 if published else 500)
                self.assertEqual(response.get_json()["saved"], published)
                self.assertFalse(response.get_json().get("reload_required"))
                self.assertEqual(models.get_task("a")["collection_id"], "c2" if published else "")
                self.assertEqual(response.get_json()["retry_ids"], [] if published else ["a", "b"])
                if not published:
                    self.assertEqual(self.post(app.test_client(), request_id="b" * 32).status_code, 200)

    def test_receipt_failure_reconciles_unknown_without_moving_later_assignment(self):
        with isolated_app() as (app, _):
            from fls_manager import models, task_bulk
            self.seed()
            write = task_bulk.write_json
            def fail(path, record):
                if "payload" in record:
                    raise OSError("receipt full")
                return write(path, record)
            with patch.object(task_bulk, "write_json", side_effect=fail):
                response = self.post(app.test_client())
            self.assertEqual(response.status_code, 500)
            self.assertTrue(response.get_json()["saved"])
            importlib.reload(task_bulk)
            self.move("b", "c3")
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                recovered = self.post(app.test_client())
                self.assertEqual(recovered.status_code, 409)
                self.assertIsNone(recovered.get_json()["saved"])
                self.assertEqual(recovered.get_json()["completed_ids"], ["a"])
                self.assertEqual(recovered.get_json()["unknown_ids"], ["b"])
                mutate.assert_not_called()
            self.assertEqual(models.get_task("b")["collection_id"], "c3")

    def test_form_keeps_selection_and_original_membership_on_retry(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            page = client.get("/collections")
            form = next(form for form in TaskFormParser(page.get_data(as_text=True)).forms
                        if form["action"].startswith("/collection/add-task/c2"))
            self.assertEqual(json.loads(form["fields"]["expected_collections"]), {"a": "", "b": "c1"})
            form["fields"]["task_ids"] = ["a"]
            with patch("fls_manager.routes.tasks.collections.mutate_tasks", side_effect=OSError("read only")):
                failed = client.post(form["action"], data=form["fields"], headers={"X-Requested-With": "FLS-Ajax"})
            self.assertEqual(failed.status_code, 500)
            retry = TaskFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertNotEqual(retry["fields"]["request_id"], form["fields"]["request_id"])
            self.assertEqual(retry["fields"]["task_ids"], "a")
            self.assertEqual(json.loads(retry["fields"]["expected_collections"]), {"a": ""})
            self.assertEqual(client.post(retry["action"], data={"request_id": retry["fields"]["request_id"]}).status_code, 400)
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertEqual(models.get_task("a")["collection_id"], "c2")
            self.move("a", "c3")
            with patch("fls_manager.routes.tasks.collections.mutate_tasks") as mutate:
                self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
                mutate.assert_not_called()
            self.assertEqual(models.get_task("a")["collection_id"], "c3")

    def test_invalid_requests_missing_choices_and_permissions_leave_files_unchanged(self):
        with isolated_app() as (app, base):
            self.seed()
            client = app.test_client()
            before = (base / "data/tasks.json").read_bytes()
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                url = "/api/collection/add-task/c2"
                for data in (None, [], {"task_ids": []}, {"task_ids": [True]}, {"task_ids": ["bad\x00id"]},
                             {"task_ids": ["a"], "request_id": ""}, {"task_ids": ["a"], "request_id": "../bad"},
                             {"task_ids": ["a"], "expected_collections": None},
                             {"task_ids": ["a"], "expected_collections": {}},
                             {"task_ids": ["a"], "expected_collections": "broken"},
                             {"task_ids": ["a"], "expected_collections": {"a": []}},
                             {"task_ids": ["a"], "expected_collections": {"a": "bad\x00id"}}):
                    with self.subTest(data=data):
                        response = client.post(url, data=json.dumps(data), content_type="application/json", headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 400)
                self.assertEqual(self.post(client, target="missing").status_code, 404)
                self.assertEqual(self.post(client, ids=["a", "missing"], expected={"a": "", "missing": ""}).status_code, 404)
                self.authenticate(client)
                self.assertEqual(client.post(url, json={"task_ids": ["a"]}).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    self.assertEqual(client.post(url + "?token=" + TOKEN, json={"task_ids": ["a"]}, headers={"X-CSRF-Token": "add-csrf"}).status_code, 403)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post(url, json={"task_ids": ["a"]}, headers={"X-CSRF-Token": "add-csrf"}).status_code, 401)
                mutate.assert_not_called()
            self.assertEqual((base / "data/tasks.json").read_bytes(), before)
            self.assertFalse((base / "data/task_bulk_requests").exists())

    def test_claim_failure_and_corrupt_selection_record_never_write_tasks(self):
        with isolated_app() as (app, _):
            from fls_manager import task_bulk
            self.seed()
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                with patch.object(task_bulk, "write_json", side_effect=OSError("claim full")):
                    self.assertEqual(self.post(app.test_client()).status_code, 500)
                path = task_bulk._record_path("a" * 32)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"request_id": "a" * 32, "action": "assign_collection", "task_ids": ["a", "b"],
                                            "results": [{"task_id": task_id, "status": "unknown", "msg": "interrupted"} for task_id in ("a", "b")],
                                            "expected_collections": [], "target_collection": "c2"}))
                before = path.read_bytes()
                self.assertEqual(self.post(app.test_client()).status_code, 500)
                self.assertEqual(path.read_bytes(), before)
                mutate.assert_not_called()

    def test_deleted_target_returns_confirmed_reselection_without_claim_or_task_write(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            page = client.get("/collections")
            form = next(form for form in TaskFormParser(page.get_data(as_text=True)).forms
                        if form["action"].startswith("/collection/add-task/c2"))
            form["fields"]["task_ids"] = ["a", "b"]
            models.save_collections([{"id": "c1", "name": "c1"}])
            before = (base / "data/tasks.json").read_bytes()
            with patch("fls_manager.routes.api.mutate_tasks") as api_write, patch(
                "fls_manager.routes.tasks.collections.mutate_tasks"
            ) as form_write:
                response = self.post(client)
                self.assertEqual(response.status_code, 404)
                self.assertFalse(response.get_json()["saved"])
                self.assertTrue(response.get_json()["reselection_required"])
                self.assertEqual(response.get_json()["retry_ids"], ["a", "b"])
                self.assertEqual(response.get_json()["unknown_ids"], [])
                response = client.post(form["action"], data=form["fields"], headers={"X-Requested-With": "FLS-Ajax"})
                self.assertEqual(response.status_code, 404)
                self.assertIn("data-task-execution-result", response.get_data(as_text=True))
                api_write.assert_not_called()
                form_write.assert_not_called()
            self.assertEqual((base / "data/tasks.json").read_bytes(), before)
            self.assertFalse((base / "data/task_bulk_requests").exists())

    def test_already_assigned_does_not_clear_new_pin_or_write_again(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            self.move("a", "c2")
            response = self.post(app.test_client(), ids=["a"], expected={"a": ""})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["updated_count"], 0)
            self.assertTrue(models.get_task("a")["pinned"])
