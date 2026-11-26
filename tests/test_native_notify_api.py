import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, sample_task
from tests.test_notify_action_recovery import isolated_app


class NativeNotifyApiTests(unittest.TestCase):
    def seed(self):
        from fls_manager.config import save_config
        save_config({"notify_items": [
            {"id": "n", "name": "Alpha", "channel": "webhook", "enabled": True, "log_mode": "full",
             "config": {"WEBHOOK_URL": "https://private.example/secret", "WEBHOOK_HEADERS": "Authorization: secret-token",
                        "WEBHOOK_BODY": "private-body", "WEBHOOK_METHOD": "POST", "extension": {"keep": True}}},
            {"id": "b", "name": "Beta", "channel": "bark", "enabled": True, "log_mode": "trimmed", "config": {"BARK_PUSH": "private-device"}},
        ], "notify_default_ids": ["n", "b"]})

    def get(self, client, suffix=""):
        return client.get("/api/notify/settings" + suffix, headers={"X-Token": TOKEN})

    def post(self, client, data):
        return client.post("/api/notify/settings", json=data, headers={"X-Token": TOKEN})

    def data(self, action="edit", **extra):
        from fls_manager.notify_actions import notification_config, notification_version
        body = {"action": action, "request_id": "a" * 32}
        if action != "new":
            body["version"] = notification_version(notification_config(), None if action == "default" else "n")
        if action not in ("new", "default"):
            body["item_id"] = "n"
        if action == "new":
            body["settings"] = {"channel": "webhook", "enabled": True, "name": "New"}
        elif action == "edit":
            body["settings"] = {"name": "Edited"}
        elif action == "toggle":
            body["enabled"] = False
        elif action == "default":
            body["default_ids"] = ["b"]
        return dict(body, **extra)

    def receipt(self, client, request_id="a" * 32):
        return client.get("/api/notify/requests/" + request_id, headers={"X-Token": TOKEN})

    def test_session_csrf_and_no_store(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            self.assertEqual(client.get("/api/notify/settings").status_code, 401)
            login = client.post("/api/auth/session", json={"token": TOKEN})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(client.get("/api/notify/settings/schema").headers["Cache-Control"], "no-store")
            self.assertEqual(client.post("/api/notify/settings", json=self.data()).status_code, 400)
            self.assertEqual(client.post("/api/notify/settings", json=self.data(),
                                        headers={"X-CSRF-Token": login.json["csrf_token"]}).status_code, 200)

    def test_schema_covers_every_channel_and_hides_urls_and_recipients(self):
        with isolated_app() as (app, _):
            from fls_manager.notify import NOTIFY_CHANNELS
            response = self.get(app.test_client(), "/schema")
            self.assertEqual(response.status_code, 200)
            channels = response.json["channels"]
            self.assertEqual({row["id"] for row in channels}, set(NOTIFY_CHANNELS))
            self.assertEqual(len(channels), 13)
            for row in channels:
                self.assertEqual({field["key"] for field in row["fields"]}, {field for field, *_ in NOTIFY_CHANNELS[row["id"]]["fields"]})
                for field in row["fields"]:
                    if field["key"] in ("SMTP_TO", "TG_USER_ID", "WEBHOOK_URL", "WEBHOOK_HEADERS", "WEBHOOK_BODY", "BARK_PUSH"):
                        self.assertTrue(field["sensitive"])

    def test_list_detail_pagination_and_search_never_reveal_secrets(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            self.assertEqual(self.get(client, "?limit=1&page=2").json["items"][0]["id"], "b")
            self.assertEqual(self.get(client, "?q=secret").json["total"], 0)
            self.assertEqual(self.get(client, "?channel=webhook").json["total"], 1)
            for suffix in ("", "/n", "/defaults"):
                response = self.get(client, suffix)
                for secret in ("private.example", "secret-token", "private-body", "private-device", "extension"):
                    self.assertNotIn(secret, response.get_data(as_text=True))
            detail = self.get(client, "/n").json["item"]
            self.assertTrue(detail["configured"]["WEBHOOK_URL"])
            self.assertEqual(detail["values"]["WEBHOOK_METHOD"], "POST")

    def test_create_all_channels_and_save_never_sends(self):
        with isolated_app() as (app, _):
            from fls_manager.notify import NOTIFY_CHANNELS
            from fls_manager.config import load_config
            self.seed()
            client = app.test_client()
            with patch("fls_manager.notify.send_one") as sender:
                for index, channel in enumerate(NOTIFY_CHANNELS):
                    data = self.data("new", request_id=f"{index:032x}", settings={"channel": channel, "enabled": True})
                    result = self.post(client, data)
                    self.assertEqual(result.status_code, 200, result.json)
                sender.assert_not_called()
            self.assertEqual(len(load_config()["notify_items"]), 15)

    def test_partial_edit_keeps_secrets_and_extension_and_explicit_clear(self):
        with isolated_app() as (app, base):
            from fls_manager.config import load_config
            self.seed()
            client = app.test_client()
            data = self.data(values={"WEBHOOK_METHOD": "PATCH"}, credentials={"WEBHOOK_BODY": {"action": "replace", "value": "line 1\nline 2"}})
            self.assertEqual(self.post(client, data).status_code, 200)
            cfg = load_config()["notify_items"][0]["config"]
            self.assertEqual(cfg["WEBHOOK_URL"], "https://private.example/secret")
            self.assertEqual(cfg["extension"], {"keep": True})
            self.assertEqual(cfg["WEBHOOK_BODY"], "line 1\nline 2")
            self.assertEqual(cfg["WEBHOOK_METHOD"], "PATCH")
            receipt = (base / "data/notify_requests" / ("a" * 32 + ".json")).read_text()
            self.assertNotIn("line 1", receipt)
            self.assertNotIn("private", receipt)
            data = self.data(request_id="b" * 32, credentials={"WEBHOOK_HEADERS": {"action": "clear"}})
            self.assertEqual(self.post(client, data).status_code, 200)
            self.assertEqual(load_config()["notify_items"][0]["config"]["WEBHOOK_HEADERS"], "")

    def test_switch_channel_requires_confirmation_and_discards_old_fields(self):
        with isolated_app() as (app, _):
            from fls_manager.config import load_config
            self.seed()
            client = app.test_client()
            data = self.data(settings={"channel": "telegram"}, credentials={"TG_BOT_TOKEN": {"action": "replace", "value": "new-token"}})
            self.assertEqual(self.post(client, data).status_code, 400)
            self.assertEqual(self.post(client, dict(data, reset_channel=True)).status_code, 200)
            cfg = load_config()["notify_items"][0]["config"]
            self.assertEqual(cfg["TG_BOT_TOKEN"], "new-token")
            self.assertNotIn("WEBHOOK_URL", cfg)
            self.assertNotIn("extension", cfg)

    def test_invalid_requests_never_write(self):
        with isolated_app() as (app, base):
            from fls_manager.paths import CONFIG_FILE
            self.seed()
            client, data, before = app.test_client(), self.data(), CONFIG_FILE.read_bytes()
            cases = [[], {**data, "action": []}, {**data, "test": True}, {**data, "confirm_retry": 1},
                     {**data, "settings": {"enabled": 1}}, {**data, "settings": {"name": "n" * 201}},
                     {**data, "values": {"WEBHOOK_URL": "secret"}}, {**data, "values": {"SMTP_SSL": "yes"}},
                     {**data, "credentials": {"WEBHOOK_URL": {"action": "keep", "value": "secret"}}},
                     {**data, "credentials": {"WEBHOOK_URL": {"action": "replace", "value": " "}}},
                     {**data, "credentials": {"WEBHOOK_HEADERS": {"action": "replace", "value": "a\x00b"}}},
                     {**data, "credentials": {"TG_USER_ID": {"action": "clear"}}},
                     {**data, "values": {"WEBHOOK_METHOD": "INVALID"}}]
            for body in cases:
                result = self.post(client, body)
                self.assertEqual(result.status_code, 400, result.json)
                self.assertIs(result.json["saved"], False)
            self.assertEqual(CONFIG_FILE.read_bytes(), before)
            self.assertFalse((base / "data/notify_requests").exists())
            for raw, status in (("x" * 65537, 413), ('{"action":"new","action":"edit"}', 400)):
                self.assertEqual(client.post("/api/notify/settings", data=raw, content_type="application/json",
                                             headers={"X-Token": TOKEN}).status_code, status)

    def test_stale_and_task_references_are_bounded_and_no_raw_config(self):
        with isolated_app() as (app, _):
            from fls_manager.models import save_tasks
            self.seed()
            save_tasks([sample_task(str(i), notify={"mode": "custom", "ids": ["n"]}) for i in range(60)])
            client = app.test_client()
            for data in (self.data("toggle"), self.data("delete"), self.data(settings={"enabled": False})):
                result = self.post(client, data)
                self.assertEqual(result.status_code, 409)
                self.assertEqual(result.json["reference_count"], 60)
                self.assertEqual(len(result.json["references"]), 50)
            result = self.post(client, self.data(version="f" * 64))
            self.assertEqual(result.status_code, 409)
            self.assertTrue(result.json["conflict"])
            self.assertNotIn("current", result.json)
            self.assertNotIn("private", result.get_data(as_text=True))

    def test_defaults_version_conflicts_with_channel_changes_and_disable_removes_default(self):
        with isolated_app() as (app, _):
            from fls_manager.config import load_config
            self.seed()
            client = app.test_client()
            defaults = self.data("default", request_id="d" * 32)
            self.assertEqual(self.post(client, self.data("toggle")).status_code, 200)
            self.assertEqual(load_config()["notify_default_ids"], ["b"])
            self.assertEqual(self.post(client, defaults).status_code, 409)
            self.assertEqual(self.post(client, self.data("default", request_id="d" * 32, default_ids=["n"])).status_code, 400)
            self.assertEqual(self.post(client, self.data("default", request_id="d" * 32, default_ids=[])).status_code, 200)
            result = self.receipt(client, "d" * 32).json
            self.assertTrue(result["saved"])
            self.assertEqual(result["action"], "default")

    def test_completed_replay_cannot_change_intent_and_preserves_later_values(self):
        with isolated_app() as (app, _):
            from fls_manager.config import load_config, save_config
            self.seed()
            data, client = self.data(), app.test_client()
            self.assertEqual(self.post(client, data).status_code, 200)
            cfg = load_config()
            cfg["notify_items"][0]["config"]["WEBHOOK_URL"] = "later-secret"
            save_config(cfg)
            with patch("fls_manager.config.save_config") as save:
                self.assertEqual(self.post(client, data).status_code, 200)
                self.assertEqual(self.post(client, dict(data, settings={"name": "Other"})).status_code, 409)
                result = self.receipt(client).json
                self.assertTrue(result["saved"])
                self.assertFalse(result["current_matches"])
                save.assert_not_called()

    def test_failed_request_cannot_repurpose_intent(self):
        with isolated_app() as (app, _):
            self.seed()
            client, data = app.test_client(), self.data()
            with patch("fls_manager.config.save_config", side_effect=OSError("private fault")):
                self.assertEqual(self.post(client, data).status_code, 500)
            self.assertIs(self.receipt(client).json["saved"], False)
            self.assertEqual(self.post(client, dict(data, settings={"name": "Changed"})).status_code, 409)
            self.assertEqual(self.post(client, data).status_code, 200)

    def test_fault_after_save_and_receipt_queries_are_read_only(self):
        with isolated_app() as (app, base):
            from fls_manager import notify_actions
            self.seed()
            original = notify_actions.write_json
            def write(path, row):
                if row["state"] == "completed":
                    raise OSError("receipt full")
                return original(path, row)
            client, data = app.test_client(), self.data()
            with patch.object(notify_actions, "write_json", side_effect=write):
                result = self.post(client, data)
            self.assertEqual(result.status_code, 503)
            self.assertTrue(result.json["saved"])
            receipt = base / "data/notify_requests" / ("a" * 32 + ".json")
            before = receipt.read_bytes()
            with patch("fls_manager.config.save_config") as save, patch("fls_manager.notify.send_one") as sender:
                self.assertTrue(self.receipt(client).json["saved"])
                self.get(client)
                self.get(client, "/n")
                save.assert_not_called()
                sender.assert_not_called()
            self.assertEqual(receipt.read_bytes(), before)

    def test_corrupt_or_missing_receipts_never_prove_success(self):
        with isolated_app() as (app, base):
            from fls_manager.storage import write_json
            self.seed()
            client = app.test_client()
            self.assertIsNone(self.receipt(client).json["saved"])
            self.post(client, self.data())
            path = base / "data/notify_requests" / ("a" * 32 + ".json")
            record = json.loads(path.read_text())
            record["state"] = "failed"
            write_json(path, record)
            result = self.receipt(client)
            self.assertEqual(result.status_code, 503)
            self.assertIsNone(result.json["saved"])
            self.assertEqual(self.post(client, self.data()).status_code, 500)

    def test_concurrent_duplicate_only_saves_once(self):
        with isolated_app() as (app, _):
            from fls_manager.config import save_config
            self.seed()
            data = self.data("new")
            entered, release = threading.Event(), threading.Event()
            def save(cfg):
                entered.set()
                self.assertTrue(release.wait(5))
                save_config(cfg)
            with patch("fls_manager.config.save_config", side_effect=save) as persist, ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client(), copy.deepcopy(data))
                self.assertTrue(entered.wait(3))
                second = pool.submit(self.post, app.test_client(), copy.deepcopy(data))
                release.set()
                self.assertEqual(first.result(5).status_code, 200)
                self.assertEqual(second.result(5).status_code, 200)
                self.assertEqual(persist.call_count, 1)

    def test_invalid_queries(self):
        with isolated_app() as (app, _):
            self.seed()
            for query in ("?limit=101", "?limit=1&limit=2", "?page=0", "?channel=unknown", "?enabled=yes", "?secret=1"):
                self.assertEqual(self.get(app.test_client(), query).status_code, 400)

    def test_all_action_receipts_recover_without_reapplying_later_config(self):
        for action in ("new", "edit", "toggle", "delete", "default"):
            with self.subTest(action=action), isolated_app() as (app, _):
                from fls_manager.config import load_config, save_config
                self.seed()
                client, data = app.test_client(), self.data(action)
                self.assertEqual(self.post(client, data).status_code, 200)
                self.assertIs(self.receipt(client).json["saved"], True)
                cfg = load_config()
                cfg["notify_items"].append({"id": "later", "name": "Later", "enabled": True, "channel": "bark", "config": {}})
                save_config(cfg)
                with patch("fls_manager.config.save_config") as save:
                    self.assertEqual(self.post(client, data).status_code, 200)
                    self.assertTrue(self.receipt(client).json["saved"])
                    save.assert_not_called()

    def test_prepared_receipt_requires_explicit_retry_and_changed_state_stays_unknown(self):
        for changed in (False, True):
            with self.subTest(changed=changed), isolated_app() as (app, _):
                from fls_manager import notify_actions
                from fls_manager.config import load_config, save_config
                self.seed()
                client, data = app.test_client(), self.data()
                write = notify_actions.write_json
                def receipt(path, row):
                    if row["state"] != "prepared":
                        raise OSError("receipt full")
                    return write(path, row)
                with patch("fls_manager.config.save_config", side_effect=OSError("disk full")), patch.object(notify_actions, "write_json", side_effect=receipt):
                    self.assertEqual(self.post(client, data).status_code, 500)
                result = self.receipt(client).json
                self.assertIs(result["saved"], False)
                self.assertTrue(result["confirm_required"])
                if changed:
                    cfg = load_config()
                    cfg["notify_items"][0]["name"] = "External"
                    save_config(cfg)
                    result = self.receipt(client).json
                    self.assertIsNone(result["saved"])
                    self.assertFalse(result["retry_allowed"])
                    with patch("fls_manager.config.save_config") as save:
                        self.assertEqual(self.post(client, dict(data, confirm_retry=True)).status_code, 409)
                        save.assert_not_called()
                else:
                    self.assertTrue(self.post(client, data).json["confirm_required"])
                    self.assertEqual(self.post(client, dict(data, confirm_retry=True)).status_code, 200)

    def test_legacy_endpoint_cannot_replay_native_receipt(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            self.assertEqual(self.post(client, self.data("toggle")).status_code, 200)
            legacy = {"action": "toggle", "item_id": "n", "request_id": "a" * 32, "enabled": False, "version": "a" * 64}
            with patch("fls_manager.config.save_config") as save:
                result = client.post("/api/notify/action", json=legacy, headers={"X-Token": TOKEN})
                self.assertEqual(result.status_code, 409)
                save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
