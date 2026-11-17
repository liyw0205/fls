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


class CollectionFormParser(TaskFormParser):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == "input" and self.current is not None and attrs.get("name"):
            self.current["fields"][attrs["name"]] = attrs.get("value", "")


class CollectionSaveRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        models.save_collections([{"id": "c1", "name": "Original", "remark": "Original remark"}])
        models.save_tasks([sample_task("a", collection_id="c1")])

    def values(self, edit=False, **extra):
        from fls_manager import models
        from fls_manager.collection_delete import collection_version
        data = {"request_id": "a" * 32, "name": "New name", "remark": "My draft", "collection_id": "c1" if edit else ""}
        if edit:
            data["collection_version"] = collection_version(models.get_collection("c1"))
        return dict(data, **extra)

    def post(self, client, data):
        return client.post("/api/collection/save", json=data, headers={"X-Token": TOKEN})

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                           security_verified=True, csrf_token="save-csrf")

    def test_create_and_edit_replay_after_restart_preserve_later_changes(self):
        for edit in (False, True):
            with self.subTest(edit=edit), isolated_app() as (app, base):
                from fls_manager import models, collection_save
                self.seed()
                tasks_before = (base / "data/tasks.json").read_bytes()
                data = self.values(edit)
                result = self.post(app.test_client(), data)
                self.assertEqual(result.status_code, 200)
                target = result.get_json()["collection_id"]
                self.assertEqual(models.get_collection(target)["name"], "New name")
                collections = models.load_collections()
                next(c for c in collections if c["id"] == target)["name"] = "Later"
                models.save_collections(collections)
                importlib.reload(collection_save)
                with patch.object(models, "save_collections") as save:
                    replay = self.post(app.test_client(), data)
                    self.assertEqual(replay.status_code, 200)
                    self.assertEqual(replay.get_json()["collection_id"], target)
                    self.assertEqual(self.post(app.test_client(), dict(data, name="different")).status_code, 409)
                    save.assert_not_called()
                self.assertEqual(models.get_collection(target)["name"], "Later")
                self.assertEqual((base / "data/tasks.json").read_bytes(), tasks_before)

    def test_concurrent_duplicate_creates_one_target(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            data = self.values()
            entered, release = threading.Event(), threading.Event()
            save = models.save_collections
            def delayed(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return save(*args, **kwargs)
            with patch.object(models, "save_collections", side_effect=delayed) as write, ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client(), data)
                release.set()
                one, two = first.result(5), second.result(5)
                self.assertEqual(one.status_code, 200)
                self.assertEqual(two.status_code, 200)
                self.assertEqual(one.get_json()["collection_id"], two.get_json()["collection_id"])
                write.assert_called_once()
            self.assertEqual(len(models.load_collections()), 2)

    def test_publication_before_after_failure_and_retry_have_truthful_saved_state(self):
        for edit in (False, True):
            for published in (False, True):
                with self.subTest(edit=edit, published=published), isolated_app() as (app, _):
                    from fls_manager import models, paths
                    self.seed()
                    data = self.values(edit)
                    replace = Path.replace
                    def fail(path, target):
                        if target == paths.COLLECTION_FILE:
                            if published:
                                replace(path, target)
                            raise OSError("publication failed")
                        return replace(path, target)
                    with patch.object(Path, "replace", fail):
                        response = self.post(app.test_client(), data)
                    self.assertEqual(response.status_code, 503 if published else 500)
                    self.assertEqual(response.get_json()["saved"], published)
                    target = response.get_json()["collection_id"]
                    self.assertEqual(models.get_collection(target)["name"] if models.get_collection(target) else None,
                                     "New name" if published else "Original" if edit else None)
                    self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                    self.assertEqual(len(models.load_collections()), 1 if edit else 2)

    def test_failed_save_can_correct_draft_without_changing_target(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            data = self.values()
            with patch.object(models, "save_collections", side_effect=OSError("disk full")):
                failed = self.post(app.test_client(), data)
            corrected = self.post(app.test_client(), dict(data, name="Corrected"))
            self.assertEqual(corrected.status_code, 200)
            self.assertEqual(corrected.get_json()["collection_id"], failed.get_json()["collection_id"])
            self.assertEqual(models.get_collection(corrected.get_json()["collection_id"])["name"], "Corrected")

    def test_receipt_failure_reconciles_saved_state_without_republishing(self):
        with isolated_app() as (app, _):
            from fls_manager import collection_save, models
            self.seed()
            data = self.values()
            write = collection_save.write_json
            def fail(path, record):
                if record["state"] == "completed":
                    raise OSError("receipt full")
                return write(path, record)
            with patch.object(collection_save, "write_json", side_effect=fail):
                failed = self.post(app.test_client(), data)
            self.assertEqual(failed.status_code, 503)
            self.assertTrue(failed.get_json()["saved"])
            importlib.reload(collection_save)
            with patch.object(models, "save_collections") as save:
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                save.assert_not_called()

    def test_interrupted_request_requires_confirmation_and_protects_later_changes(self):
        for edit in (False, True):
            with self.subTest(edit=edit), isolated_app() as (app, _):
                from fls_manager import collection_save, models
                self.seed()
                data = self.values(edit)
                with patch.object(models, "save_collections", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.post(app.test_client(), data)
                importlib.reload(collection_save)
                with patch.object(models, "save_collections") as save:
                    unknown = self.post(app.test_client(), data)
                    self.assertEqual(unknown.status_code, 409)
                    self.assertIsNone(unknown.get_json()["saved"])
                    self.assertTrue(unknown.get_json()["confirm_required"])
                    save.assert_not_called()
                confirmed = self.post(app.test_client(), dict(data, confirm_retry=True))
                self.assertEqual(confirmed.status_code, 200)
                self.assertEqual(len(models.load_collections()), 1 if edit else 2)

    def test_stale_edit_form_preserves_draft_and_can_merge_current_version(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            old = CollectionFormParser(client.get("/collection/edit/c1").get_data(as_text=True)).forms[0]
            old["fields"].update(name="My draft", remark="Keep me")
            current = models.get_collection("c1")
            current["name"] = "Someone else"
            models.save_collections([current])
            before = (base / "data/collections.json").read_bytes()
            response = client.post(old["action"], data=old["fields"], headers={"X-Requested-With": "FLS-Ajax"})
            self.assertEqual(response.status_code, 409)
            body = response.get_data(as_text=True)
            self.assertIn("Someone else", body)
            self.assertIn("合并后保存合集设置", body)
            self.assertEqual((base / "data/collections.json").read_bytes(), before)
            self.assertFalse((base / "data/collection_save_requests").exists())
            merge = CollectionFormParser(body).forms[0]
            self.assertEqual(merge["fields"]["name"], "My draft")
            self.assertEqual(merge["fields"]["remark"], "Keep me")
            self.assertNotEqual(merge["fields"]["collection_version"], old["fields"]["collection_version"])
            self.assertEqual(client.post(merge["action"], data=merge["fields"]).status_code, 302)
            self.assertEqual(models.get_collection("c1")["name"], "My draft")

    def test_form_failure_keeps_id_draft_and_published_failure_offers_saved_link(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            from fls_manager.routes.tasks import collections as routes
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            form = CollectionFormParser(client.get("/collection/new").get_data(as_text=True)).forms[0]
            form["fields"].update(name="Draft", remark="Do not lose")
            with patch.object(routes, "save_collections", side_effect=OSError("disk full")):
                failed = client.post(form["action"], data=form["fields"])
            self.assertEqual(failed.status_code, 500)
            retry = CollectionFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertEqual(retry["fields"], form["fields"])
            save = models.save_collections
            def fail_after(items):
                save(items)
                raise OSError("after publication")
            with patch.object(routes, "save_collections", side_effect=fail_after):
                saved = client.post(retry["action"], data=retry["fields"])
            self.assertEqual(saved.status_code, 503)
            self.assertIn("查看已保存合集", saved.get_data(as_text=True))
            self.assertEqual(TaskFormParser(saved.get_data(as_text=True)).forms, [])
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertEqual(len(models.load_collections()), 2)

    def test_claim_corruption_unreadable_storage_and_invalid_input_have_no_writes(self):
        with isolated_app() as (app, base):
            from fls_manager import models, collection_save
            self.seed()
            data = self.values()
            before = (base / "data/collections.json").read_bytes()
            with patch.object(models, "save_collections") as save:
                for invalid in (None, [], dict(data, request_id=""), dict(data, request_id="../bad"),
                                dict(data, name=[]), dict(data, remark="bad\x00value"), dict(data, collection_id=True),
                                dict(data, collection_version=None), dict(data, confirm_retry=[])):
                    with self.subTest(invalid=invalid):
                        response = app.test_client().post("/api/collection/save", data=json.dumps(invalid),
                            content_type="application/json", headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 400)
                self.assertFalse((base / "data/collection_save_requests").exists())
                with patch.object(collection_save, "write_json", side_effect=OSError("claim full")):
                    failed = self.post(app.test_client(), data)
                    self.assertEqual(failed.status_code, 500)
                    self.assertFalse(failed.get_json()["saved"])
                path = base / "data/collection_save_requests" / ("a" * 32 + ".json")
                path.parent.mkdir(parents=True, exist_ok=True)
                for raw in ("null", "{}", "{broken"):
                    with self.subTest(raw=raw):
                        path.write_text(raw, encoding="utf-8")
                        self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                        self.assertEqual(path.read_text(encoding="utf-8"), raw)
                with patch.object(models, "load_collections", side_effect=OSError("read denied")):
                    self.assertEqual(self.post(app.test_client(), dict(data, request_id="b" * 32)).status_code, 500)
                save.assert_not_called()
            self.assertEqual((base / "data/collections.json").read_bytes(), before)

    def test_api_and_form_csrf_expiry_and_denied_token_do_not_write(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            from fls_manager.routes.tasks import collections as routes
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            data = self.values()
            before = (base / "data/collections.json").read_bytes()
            with patch.object(models, "save_collections") as api_write, patch.object(routes, "save_collections") as form_write:
                for url in ("/api/collection/save", "/collection/new", "/collection/edit/c1"):
                    with self.subTest(url=url):
                        self.authenticate(client)
                        self.assertEqual(client.post(url, data=data, headers={"X-Requested-With": "FLS-Ajax"}).status_code, 400)
                        with patch("fls_manager.auth.query_token_enabled", return_value=False):
                            self.assertEqual(client.post(url + "?token=" + TOKEN, data=data,
                                headers={"X-CSRF-Token": "save-csrf", "X-Requested-With": "FLS-Ajax"}).status_code, 403)
                        with client.session_transaction() as session:
                            session["token_expire_at"] = 1
                        self.assertEqual(client.post(url, data=data,
                            headers={"X-CSRF-Token": "save-csrf", "X-Requested-With": "FLS-Ajax"}).status_code, 401)
                api_write.assert_not_called()
                form_write.assert_not_called()
            self.assertEqual((base / "data/collections.json").read_bytes(), before)

    def test_interrupted_edit_does_not_overwrite_new_content_or_recreate_deleted_collection(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted), isolated_app() as (app, base):
                from fls_manager import models
                self.seed()
                data = self.values(True)
                with patch.object(models, "save_collections", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        self.post(app.test_client(), data)
                current = models.get_collection("c1")
                current["remark"] = "Later edit"
                models.save_collections([] if deleted else [current])
                before = (base / "data/collections.json").read_bytes()
                with patch.object(models, "save_collections") as save:
                    response = self.post(app.test_client(), dict(data, confirm_retry=True))
                    self.assertEqual(response.status_code, 409)
                    save.assert_not_called()
                self.assertEqual((base / "data/collections.json").read_bytes(), before)

    def test_deleted_edit_form_can_explicitly_save_draft_as_new(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            form = CollectionFormParser(client.get("/collection/edit/c1").get_data(as_text=True)).forms[0]
            form["fields"].update(name="Recovered", remark="Draft")
            models.save_collections([])
            response = client.post(form["action"], data=form["fields"])
            self.assertEqual(response.status_code, 404)
            self.assertEqual(models.load_collections(), [])
            retry = CollectionFormParser(response.get_data(as_text=True)).forms[0]
            self.assertEqual(retry["action"], "/collection/new")
            self.assertEqual(retry["fields"]["name"], "Recovered")
            self.assertNotIn("collection_version", retry["fields"])
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertNotEqual(models.load_collections()[0]["id"], "c1")

    def test_invalid_hidden_fields_keep_draft_and_render_executable_recovery(self):
        for edit, invalid in ((False, "request_id"), (False, "collection_version"), (True, "collection_version")):
            with self.subTest(edit=edit, invalid=invalid), isolated_app() as (app, base):
                from fls_manager import models
                self.seed()
                client = app.test_client()
                self.authenticate(client)
                url = "/collection/edit/c1" if edit else "/collection/new"
                form = CollectionFormParser(client.get(url).get_data(as_text=True)).forms[0]
                form["fields"].update(name="My draft", remark="Keep")
                form["fields"][invalid] = "invalid"
                before = (base / "data/collections.json").read_bytes()
                failed = client.post(url, data=form["fields"])
                self.assertEqual(failed.status_code, 400)
                self.assertEqual((base / "data/collections.json").read_bytes(), before)
                retry = CollectionFormParser(failed.get_data(as_text=True)).forms[0]
                self.assertEqual(retry["fields"]["name"], "My draft")
                self.assertNotEqual(retry["fields"].get(invalid), "invalid")
                if edit:
                    self.assertIn("当前已保存内容", failed.get_data(as_text=True))
                    self.assertIn("合并后保存合集设置", failed.get_data(as_text=True))
                self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
                self.assertTrue(any(c["name"] == "My draft" for c in models.load_collections()))

    def test_invalid_version_recovery_read_failure_does_not_drop_conflict_guard(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.tasks import collections as routes
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            form = CollectionFormParser(client.get("/collection/edit/c1").get_data(as_text=True)).forms[0]
            form["fields"].update(name="Draft", collection_version="invalid")
            before = (base / "data/collections.json").read_bytes()
            with patch.object(routes, "get_collection", side_effect=OSError("read failed")), patch.object(routes, "save_collections") as save:
                response = client.post(form["action"], data=form["fields"])
                self.assertEqual(response.status_code, 400)
                retry = CollectionFormParser(response.get_data(as_text=True)).forms[0]
                self.assertEqual(retry["fields"]["collection_version"], "invalid")
                self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 400)
                save.assert_not_called()
            self.assertEqual((base / "data/collections.json").read_bytes(), before)
