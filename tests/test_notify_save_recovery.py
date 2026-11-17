import importlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, sample_task
from tests import test_notify_action_recovery as actions_tests

isolated_app = actions_tests.isolated_app


class NotifySaveRecoveryTests(unittest.TestCase):
    seed = actions_tests.NotifyActionRecoveryTests.seed
    authenticate = actions_tests.NotifyActionRecoveryTests.authenticate
    form = actions_tests.NotifyActionRecoveryTests.form
    post = actions_tests.NotifyActionRecoveryTests.post

    def values(self, action="new", **extra):
        from fls_manager.notify_actions import notification_config, notification_version
        data = {"action": action, "request_id": "c" * 32, "name": "Saved webhook", "enabled": True,
                "channel": "webhook", "log_mode": "trimmed",
                "config": {"WEBHOOK_URL": "http://127.0.0.1:9/", "WEBHOOK_HEADERS": "Authorization: private-value", "WEBHOOK_BODY": "Line one\nLine two"}}
        if action == "edit":
            data.update(item_id="n", version=notification_version(notification_config(), "n"))
        return dict(data, **extra)

    def test_saved_replay_preserves_later_config_after_restart(self):
        for action in ("new", "edit"):
            with self.subTest(action=action), isolated_app() as (app, base):
                from fls_manager import notify_actions
                from fls_manager.config import load_config, save_config
                self.seed()
                data = self.values(action)
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 200)
                target = response.json["item_id"]
                cfg = load_config()
                item = next(row for row in cfg["notify_items"] if row["id"] == target)
                self.assertEqual(item["config"]["WEBHOOK_BODY"], "Line one\nLine two")
                self.assertEqual(len(cfg["notify_items"]), 3 if action == "new" else 2)
                receipt = (base / "data/notify_requests" / ("c" * 32 + ".json")).read_text()
                self.assertNotIn("private-value", receipt)
                self.assertNotIn("127.0.0.1", receipt)
                item["name"] = "Later"
                save_config(cfg)
                importlib.reload(notify_actions)
                with patch("fls_manager.config.save_config") as save:
                    self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                    self.assertEqual(self.post(app.test_client(), dict(data, name="Different")).status_code, 409)
                    save.assert_not_called()
                self.assertEqual(next(row for row in load_config()["notify_items"] if row["id"] == target)["name"], "Later")

    def test_publication_and_receipt_faults_recover_fixed_target(self):
        for action in ("new", "edit"):
            for mode in ("before", "after", "receipt", "prepared"):
                with self.subTest(action=action, mode=mode), isolated_app() as (app, _):
                    from fls_manager import paths, notify_actions
                    from fls_manager.config import load_config
                    self.seed()
                    data, before = self.values(action), paths.CONFIG_FILE.read_bytes()
                    replace, write = Path.replace, notify_actions.write_json
                    def publish(path, target):
                        if target == paths.CONFIG_FILE and mode in ("before", "after", "prepared"):
                            if mode == "after":
                                replace(path, target)
                            raise OSError("disk fault")
                        return replace(path, target)
                    def receipt(path, row):
                        if mode in ("receipt", "prepared") and row["state"] != "prepared":
                            raise OSError("receipt fault")
                        return write(path, row)
                    with patch.object(Path, "replace", publish), patch.object(notify_actions, "write_json", side_effect=receipt):
                        response = self.post(app.test_client(), data)
                    self.assertEqual(response.status_code, 503 if mode in ("after", "receipt") else 500)
                    self.assertEqual(response.json["saved"], mode in ("after", "receipt"))
                    self.assertEqual(paths.CONFIG_FILE.read_bytes() == before, mode in ("before", "prepared"))
                    if mode == "prepared":
                        self.assertTrue(self.post(app.test_client(), data).json["confirm_required"])
                    self.assertEqual(self.post(app.test_client(), dict(data, confirm_retry=True)).status_code, 200)
                    self.assertEqual(len(load_config()["notify_items"]), 3 if action == "new" else 2)

    def test_illegal_choices_and_task_references_have_zero_side_effects(self):
        invalid = [("channel", "missing"), ("channel", []), ("log_mode", "invalid"), ("enabled", 1),
                   ("config", []), ("config", {"unknown": "value"}), ("config", {"WEBHOOK_BODY": "nul\x00"}),
                   ("name", "a\x00b"), ("name", {}), ("request_id", "bad"), ("test", 1), ("item_id", "n")]
        for field, value in invalid:
            with self.subTest(field=field, value=value), isolated_app() as (app, base):
                from fls_manager import paths, notify
                self.seed()
                before = paths.CONFIG_FILE.read_bytes()
                with patch.object(notify, "send_one") as send:
                    self.assertEqual(self.post(app.test_client(), self.values(**{field: value})).status_code, 400)
                    send.assert_not_called()
                self.assertEqual(paths.CONFIG_FILE.read_bytes(), before)
                self.assertFalse((base / "data/notify_requests").exists())
        with isolated_app() as (app, base):
            from fls_manager import models, paths
            self.seed()
            models.save_tasks([sample_task("t", notify={"mode": "custom", "ids": ["n"]})])
            before = paths.CONFIG_FILE.read_bytes()
            self.assertEqual(self.post(app.test_client(), self.values("edit", enabled=False)).status_code, 409)
            self.assertEqual(self.post(app.test_client(), self.values(enabled=False, test=True)).status_code, 400)
            self.assertEqual(self.post(app.test_client(), self.values("edit", version="f" * 64)).status_code, 409)
            self.assertEqual(paths.CONFIG_FILE.read_bytes(), before)
            self.assertFalse((base / "data/notify_requests").exists())

    def test_failed_request_can_correct_draft_without_reusing_changed_target(self):
        for changed in (False, True):
            with self.subTest(changed=changed), isolated_app() as (app, _):
                from fls_manager.config import load_config, save_config
                self.seed()
                data = self.values("edit")
                with patch("fls_manager.config.save_config", side_effect=OSError("full")):
                    self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                if changed:
                    cfg = load_config()
                    cfg["notify_items"][0]["name"] = "External"
                    save_config(cfg)
                response = self.post(app.test_client(), dict(data, name="Corrected"))
                self.assertEqual(response.status_code, 409 if changed else 200)
                self.assertEqual(load_config()["notify_items"][0]["name"], "External" if changed else "Corrected")

    def test_concurrent_creates_unique_names_and_defaults_removed_on_disable(self):
        with isolated_app() as (app, _):
            from fls_manager.config import load_config, save_config
            self.seed()
            entered, release = threading.Event(), threading.Event()
            data = self.values()
            def persist(cfg):
                entered.set()
                self.assertTrue(release.wait(5))
                return save_config(cfg)
            with patch("fls_manager.config.save_config", side_effect=persist) as save, ThreadPoolExecutor(3) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(3))
                second = pool.submit(self.post, app.test_client(), data)
                third = pool.submit(self.post, app.test_client(), dict(data, request_id="d" * 32))
                release.set()
                results = [f.result(5) for f in (first, second, third)]
                self.assertEqual([r.status_code for r in results], [200, 200, 200])
                self.assertEqual(results[0].json["item_id"], results[1].json["item_id"])
                self.assertEqual(save.call_count, 2)
            names = [row["name"] for row in load_config()["notify_items"]]
            self.assertIn("Saved webhook", names)
            self.assertIn("Saved webhook-1", names)
            self.assertEqual(self.post(app.test_client(), self.values("edit", request_id="e" * 32, enabled=False)).status_code, 200)
            self.assertEqual(load_config()["notify_default_ids"], ["other"])

    def test_save_and_test_failure_replay_and_explicit_delivery_recovery(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            from fls_manager.config import load_config
            self.seed()
            data = self.values(test=True)
            with patch.object(notify, "send_one", return_value=(False, "offline")) as send:
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 502)
                self.assertTrue(response.json["saved"])
                self.assertEqual(len(load_config()["notify_items"]), 3)
                self.assertEqual(self.post(app.test_client(), data).status_code, 502)
                send.assert_called_once()
            delivery_id = response.json["test_result"]["delivery_id"]
            with patch.object(notify, "send_one", return_value=(True, "accepted")) as send:
                self.assertEqual(app.test_client().post("/api/notify/retry/" + delivery_id, headers={"X-Token": TOKEN}).status_code, 200)
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                send.assert_called_once()

    def test_partial_save_offers_test_only_with_original_target_version(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            from fls_manager.config import load_config, save_config
            self.seed()
            def persist(cfg):
                save_config(cfg)
                raise OSError("after publish")
            with patch("fls_manager.config.save_config", side_effect=persist), patch.object(notify, "send_one") as send:
                response = self.post(app.test_client(), self.values(test=True))
                self.assertEqual(response.status_code, 503)
                self.assertTrue(response.json["saved"])
                send.assert_not_called()
            test = response.json["test_request"]
            with patch.object(notify, "send_one", return_value=(True, "accepted")) as send:
                self.assertEqual(app.test_client().post("/api/notify/test/" + test["item_id"], json=test, headers={"X-Token": TOKEN}).status_code, 200)
                send.assert_called_once()
            self.assertEqual(len(load_config()["notify_items"]), 3)

    def test_pending_test_cannot_send_later_edited_configuration(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            from fls_manager.config import load_config, save_config
            self.seed()
            data = self.values(test=True)
            def persist(cfg):
                save_config(cfg)
                raise OSError("after publish")
            with patch("fls_manager.config.save_config", side_effect=persist):
                response = self.post(app.test_client(), data)
            cfg = load_config()
            target = next(row for row in cfg["notify_items"] if row["id"] == response.json["item_id"])
            target["config"]["WEBHOOK_URL"] = "http://127.0.0.1:8/changed"
            save_config(cfg)
            with patch.object(notify, "send_one") as send:
                replay = self.post(app.test_client(), data)
                self.assertEqual(replay.status_code, 409)
                self.assertTrue(replay.json["saved"])
                self.assertIn("已变化", replay.json["test_result"]["msg"])
                send.assert_not_called()
            self.assertEqual(load_config(), cfg)

    def test_unknown_publication_retains_draft_and_replay_recovers_saved_test(self):
        with isolated_app() as (app, _):
            from fls_manager import notify, notify_actions
            from fls_manager.config import save_config, load_config
            self.seed()
            data = self.values(test=True)
            cfg = notify_actions.notification_config()
            def persist(values):
                save_config(values)
                raise OSError("after publish")
            with patch("fls_manager.config.save_config", side_effect=persist), patch.object(notify_actions, "notification_config", side_effect=[cfg, PermissionError("unreadable")]), patch.object(notify, "send_one") as send:
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 500)
                self.assertIsNone(response.json["saved"])
                send.assert_not_called()
            with patch.object(notify, "send_one", return_value=(True, "accepted")) as send:
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                send.assert_called_once()
            self.assertEqual(len(load_config()["notify_items"]), 3)

    def test_concurrent_save_and_test_only_sends_one_delivery(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            self.seed()
            data = self.values(test=True)
            entered, release = threading.Event(), threading.Event()
            def send(*args):
                entered.set()
                self.assertTrue(release.wait(5))
                return True, "accepted"
            with patch.object(notify, "send_one", side_effect=send) as sender, ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(3))
                try:
                    second = pool.submit(self.post, app.test_client(), data).result(3)
                    self.assertEqual(second.status_code, 409)
                    self.assertTrue(second.json["saved"])
                    self.assertEqual(second.json["test_result"]["delivery_status"], "running")
                finally:
                    release.set()
                self.assertEqual(first.result(3).status_code, 200)
                sender.assert_called_once()

    def test_real_form_draft_disk_conflict_deleted_and_csrf_permissions(self):
        with isolated_app() as (app, _):
            from fls_manager.config import load_config, save_config
            from fls_manager.routes.notify import actions
            self.seed()
            client = app.test_client()
            data = self.values()
            self.assertEqual(client.post("/api/notify/action", json=data).status_code, 400)
            self.authenticate(client)
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                self.assertEqual(client.post("/api/notify/action?token=" + TOKEN, json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 403)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.post("/api/notify/action", json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 401)
            self.authenticate(client)
            form = self.form(client.get("/notify/new?channel=webhook"), "/notify/new")
            data = dict(form["fields"], channel="webhook", name="Draft", log_mode="bad", enabled="1", WEBHOOK_HEADERS="Secret draft", WEBHOOK_BODY="Line 1\nLine 2")
            self.assertEqual(client.post(form["action"], data=dict(data, csrf_token="wrong")).status_code, 400)
            response = client.post(form["action"], data=data)
            self.assertEqual(response.status_code, 400)
            self.assertIn("Line 1\nLine 2", response.get_data(as_text=True))
            retry = self.form(response, "/notify/new")
            retry["fields"].update(channel="webhook", log_mode="trimmed", WEBHOOK_HEADERS="Secret draft", WEBHOOK_BODY="Line 1\nLine 2")
            with patch.object(actions, "save_config", side_effect=OSError("full")):
                response = client.post(retry["action"], data=retry["fields"])
            self.assertEqual(response.status_code, 500)
            again = self.form(response, "/notify/new")
            self.assertEqual(again["fields"]["request_id"], retry["fields"]["request_id"])
            again["fields"].update(channel="webhook", log_mode="trimmed", WEBHOOK_HEADERS="Secret draft", WEBHOOK_BODY="Line 1\nLine 2")
            self.assertEqual(client.post(again["action"], data=again["fields"]).status_code, 302)
            edit = self.form(client.get("/notify/edit/n"), "/notify/edit/n")
            edit["fields"].update(channel="webhook", name="Merged draft", log_mode="full", enabled="1")
            cfg = load_config()
            cfg["notify_items"][0]["name"] = "External name"
            save_config(cfg)
            response = client.post(edit["action"], data=edit["fields"])
            self.assertEqual(response.status_code, 409)
            self.assertIn("External name", response.get_data(as_text=True))
            merged = self.form(response, "/notify/edit/n")
            self.assertEqual(merged["fields"]["name"], "Merged draft")
            merged["fields"].update(channel="webhook", log_mode="full")
            self.assertEqual(client.post(merged["action"], data=merged["fields"]).status_code, 302)
            cfg = load_config()
            cfg["notify_items"] = [row for row in cfg["notify_items"] if row["id"] != "n"]
            save_config(cfg)
            response = client.post(edit["action"], data=dict(edit["fields"], request_id="f" * 32))
            self.assertEqual(response.status_code, 404)
            recovered = self.form(response, "/notify/new")
            recovered["fields"].update(channel="webhook", log_mode="full")
            self.assertEqual(client.post(recovered["action"], data=recovered["fields"]).status_code, 302)
