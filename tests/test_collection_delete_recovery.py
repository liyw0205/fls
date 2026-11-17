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


class CollectionDeleteRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        from fls_manager.collection_delete import collection_version
        models.save_collections([{"id": cid, "name": cid} for cid in ("c1", "c2")])
        models.save_tasks([sample_task("a", collection_id="c1"), sample_task("b", collection_id="c1"), sample_task("c")])
        return {"request_id": "a" * 32, "expected_members": ["a", "b"],
                "collection_version": collection_version(models.get_collection("c1"))}

    def post(self, client, data, target="c1"):
        return client.post("/api/collection/delete/" + target, json=data, headers={"X-Token": TOKEN})

    def move(self, task_id, target):
        from fls_manager import models
        def update(tasks):
            next(task for task in tasks if task["id"] == task_id)["collection_id"] = target
            return True
        models.mutate_tasks(update)

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                           security_verified=True, csrf_token="delete-csrf")

    def test_real_delete_restart_and_replay_do_not_touch_new_assignments(self):
        with isolated_app() as (app, _):
            from fls_manager import models, collection_delete
            data = self.seed()
            with patch("fls_manager.routes.api.reload_scheduler") as reload:
                result = self.post(app.test_client(), data)
                self.assertEqual(result.status_code, 200)
                reload.assert_not_called()
            self.assertTrue(result.get_json()["saved"])
            self.assertTrue(result.get_json()["members_saved"])
            self.assertIsNone(models.get_collection("c1"))
            self.move("a", "c2")
            importlib.reload(collection_delete)
            with patch("fls_manager.routes.api.mutate_tasks") as mutate, patch.object(models, "save_collections") as write:
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                self.assertEqual(self.post(app.test_client(), data, "c2").status_code, 409)
                self.assertEqual(self.post(app.test_client(), dict(data, expected_members=["a"])).status_code, 409)
                mutate.assert_not_called()
                write.assert_not_called()
            self.assertEqual(models.get_task("a")["collection_id"], "c2")

    def test_concurrent_duplicate_deletes_once(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            data = self.seed()
            entered, release = threading.Event(), threading.Event()
            mutate = models.mutate_tasks
            def delayed(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return mutate(*args, **kwargs)
            with patch("fls_manager.routes.api.mutate_tasks", side_effect=delayed) as write, ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client(), data)
                release.set()
                self.assertEqual(first.result(5).status_code, 200)
                self.assertEqual(second.result(5).status_code, 200)
                write.assert_called_once()

    def test_task_and_collection_publication_failures_report_actual_state_then_resume(self):
        for kind in ("TASK_FILE", "COLLECTION_FILE"):
            for published in (False, True):
                with self.subTest(kind=kind, published=published), isolated_app() as (app, _):
                    from fls_manager import models, paths
                    data = self.seed()
                    replace = Path.replace
                    def fail(path, target):
                        if target == getattr(paths, kind):
                            if published:
                                replace(path, target)
                            raise OSError("publication failed")
                        return replace(path, target)
                    with patch.object(Path, "replace", fail):
                        result = self.post(app.test_client(), data)
                    deleted = kind == "COLLECTION_FILE" and published
                    cleared = kind == "COLLECTION_FILE" or published
                    self.assertEqual(result.status_code, 503 if deleted else 500)
                    self.assertEqual(result.get_json()["saved"], deleted)
                    self.assertEqual(result.get_json()["members_saved"], cleared)
                    self.assertFalse(result.get_json()["reload_required"])
                    self.assertEqual(models.get_task("a")["collection_id"], "" if cleared else "c1")
                    self.assertEqual(models.get_collection("c1") is None, deleted)
                    self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                    self.assertIsNone(models.get_collection("c1"))

    def test_partial_save_resume_preserves_new_assignment_and_unrelated_collection(self):
        with isolated_app() as (app, _):
            from fls_manager import models, collection_delete
            data = self.seed()
            with patch.object(models, "save_collections", side_effect=OSError("disk full")):
                result = self.post(app.test_client(), data)
            self.assertEqual(result.status_code, 500)
            self.assertTrue(result.get_json()["members_saved"])
            self.move("a", "c2")
            models.save_collections(models.load_collections() + [{"id": "later", "name": "later"}])
            importlib.reload(collection_delete)
            self.assertEqual(self.post(app.test_client(), data).status_code, 200)
            self.assertEqual(models.get_task("a")["collection_id"], "c2")
            self.assertEqual({c["id"] for c in models.load_collections()}, {"c2", "later"})

    def test_stale_members_and_metadata_reject_before_writes_including_resume(self):
        for resume in (False, True):
            for kind in ("member", "name", "rejoined"):
                with self.subTest(resume=resume, kind=kind), isolated_app() as (app, base):
                    from fls_manager import models
                    data = self.seed()
                    if resume:
                        with patch.object(models, "save_collections", side_effect=OSError("disk full")):
                            self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                    if kind == "member":
                        self.move("c", "c1")
                    elif kind == "rejoined":
                        self.move("a", "c1" if resume else "c2")
                    else:
                        collections = models.load_collections()
                        collections[0]["name"] = "renamed"
                        models.save_collections(collections)
                    before = [(base / "data" / name).read_bytes() for name in ("tasks.json", "collections.json")]
                    with patch("fls_manager.routes.api.mutate_tasks") as mutate, patch.object(models, "save_collections") as write:
                        result = self.post(app.test_client(), data)
                        self.assertEqual(result.status_code, 409)
                        self.assertTrue(result.get_json()["reselection_required"])
                        mutate.assert_not_called()
                        write.assert_not_called()
                    self.assertEqual([(base / "data" / name).read_bytes() for name in ("tasks.json", "collections.json")], before)

    def test_member_checkpoint_failure_leaves_collection_and_can_resume(self):
        with isolated_app() as (app, _):
            from fls_manager import models, collection_delete
            data = self.seed()
            write = collection_delete.write_json
            def fail(path, record):
                if record.get("members_cleared"):
                    raise OSError("checkpoint full")
                return write(path, record)
            with patch.object(collection_delete, "write_json", side_effect=fail), patch.object(models, "save_collections") as save:
                result = self.post(app.test_client(), data)
                self.assertEqual(result.status_code, 500)
                self.assertFalse(result.get_json()["saved"])
                self.assertTrue(result.get_json()["members_saved"])
                save.assert_not_called()
            self.assertIsNotNone(models.get_collection("c1"))
            self.assertEqual(self.post(app.test_client(), data).status_code, 200)

    def test_claim_receipt_and_unreadable_recovery_do_not_guess_success(self):
        with isolated_app() as (app, _):
            from fls_manager import models, collection_delete
            data = self.seed()
            with patch.object(collection_delete, "write_json", side_effect=OSError("claim full")), patch(
                "fls_manager.routes.api.mutate_tasks"
            ) as mutate:
                result = self.post(app.test_client(), data)
                self.assertEqual(result.status_code, 500)
                self.assertFalse(result.get_json()["saved"])
                mutate.assert_not_called()
            write = collection_delete.write_json
            def fail_receipt(path, record):
                if record.get("completed"):
                    raise OSError("receipt full")
                return write(path, record)
            with patch.object(collection_delete, "write_json", side_effect=fail_receipt):
                result = self.post(app.test_client(), data)
                self.assertEqual(result.status_code, 500)
                self.assertTrue(result.get_json()["saved"])
            self.move("a", "c2")
            with patch("fls_manager.routes.api.load_tasks", side_effect=OSError("cannot verify")):
                result = self.post(app.test_client(), data)
                self.assertEqual(result.status_code, 500)
                self.assertIsNone(result.get_json()["saved"])
            self.assertEqual(self.post(app.test_client(), data).status_code, 200)
            self.assertEqual(models.get_task("a")["collection_id"], "c2")

    def test_corrupt_records_and_collection_storage_are_not_overwritten(self):
        for kind in ("receipt", "collections"):
            for raw in ("{broken", "null", "{}"):
                with self.subTest(kind=kind, raw=raw), isolated_app() as (app, base):
                    from fls_manager import models
                    data = self.seed()
                    path = (base / "data/collections.json" if kind == "collections" else
                            base / "data/collection_delete_requests" / (data["request_id"] + ".json"))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(raw, encoding="utf-8")
                    before = (base / "data/tasks.json").read_bytes()
                    with patch("fls_manager.routes.api.mutate_tasks") as mutate, patch.object(models, "save_collections") as write:
                        self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                        mutate.assert_not_called()
                        write.assert_not_called()
                    self.assertEqual(path.read_text(encoding="utf-8"), raw)
                    self.assertEqual((base / "data/tasks.json").read_bytes(), before)

    def test_form_partial_save_retry_and_permissions(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            from fls_manager.routes.tasks import collections as routes
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            page = client.get("/collections")
            form = next(form for form in TaskFormParser(page.get_data(as_text=True)).forms
                        if form["action"].startswith("/collection/delete/c1"))
            self.assertEqual(json.loads(form["fields"]["expected_members"]), ["a", "b"])
            before = (base / "data/tasks.json").read_bytes()
            with patch.object(routes, "mutate_tasks") as mutate:
                fields = dict(form["fields"])
                fields.pop("_csrf_token", None)
                fields.pop("csrf_token", None)
                self.assertEqual(client.post(form["action"], data=fields).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    self.assertEqual(client.post(form["action"] + "&token=" + TOKEN, data=form["fields"],
                                                 headers={"X-Requested-With": "FLS-Ajax"}).status_code, 403)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post(form["action"], data=form["fields"], headers={"X-Requested-With": "FLS-Ajax"}).status_code, 401)
                mutate.assert_not_called()
            self.assertEqual((base / "data/tasks.json").read_bytes(), before)
            self.authenticate(client)
            with patch.object(routes, "save_collections", side_effect=OSError("disk full")):
                result = client.post(form["action"], data=form["fields"], headers={"X-Requested-With": "FLS-Ajax"})
            self.assertEqual(result.status_code, 500)
            self.assertIn("任务已移出该合集", result.get_data(as_text=True))
            retry = TaskFormParser(result.get_data(as_text=True)).forms[0]
            self.assertEqual(retry["fields"]["request_id"], form["fields"]["request_id"])
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertIsNone(models.get_collection("c1"))

    def test_invalid_api_selection_and_permissions_have_no_side_effects(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            data = self.seed()
            client = app.test_client()
            before = [(base / "data" / name).read_bytes() for name in ("tasks.json", "collections.json")]
            with patch("fls_manager.routes.api.mutate_tasks") as mutate, patch.object(models, "save_collections") as write:
                for invalid in ([], None, dict(data, request_id=""), dict(data, request_id="../bad"),
                                dict(data, expected_members=None), dict(data, expected_members="bad"),
                                dict(data, expected_members=[True]), dict(data, expected_members=["a", "a"]),
                                dict(data, collection_version="bad")):
                    with self.subTest(invalid=invalid):
                        response = client.post("/api/collection/delete/c1", data=json.dumps(invalid),
                                               content_type="application/json", headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 400)
                self.assertEqual(self.post(client, data, "missing").status_code, 404)
                self.authenticate(client)
                self.assertEqual(client.post("/api/collection/delete/c1", json=data).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    self.assertEqual(client.post("/api/collection/delete/c1?token=" + TOKEN, json=data,
                                                 headers={"X-CSRF-Token": "delete-csrf"}).status_code, 403)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post("/api/collection/delete/c1", json=data,
                                             headers={"X-CSRF-Token": "delete-csrf"}).status_code, 401)
                mutate.assert_not_called()
                write.assert_not_called()
            self.assertEqual([(base / "data" / name).read_bytes() for name in ("tasks.json", "collections.json")], before)
            self.assertFalse((base / "data/collection_delete_requests").exists())
