import contextlib
import importlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app as bulk_isolated_app, sample_task
from tests import test_proxy_action_recovery as proxy_tests


@contextlib.contextmanager
def isolated_app():
    with bulk_isolated_app() as fixture, patch("fls_manager.app.dispatch_frontend_open_event"):
        yield fixture


class NotifyActionRecoveryTests(unittest.TestCase):
    authenticate = proxy_tests.ProxyActionRecoveryTests.authenticate
    form = proxy_tests.ProxyActionRecoveryTests.form

    def seed(self):
        from fls_manager.config import save_config
        from fls_manager import notify
        save_config({"notify_items": [
            {"id": "n", "name": "Original", "channel": "webhook", "enabled": True, "log_mode": "full", "config": {"WEBHOOK_URL": "http://127.0.0.1:9/", "TOKEN": "private-secret"}},
            {"id": "other", "name": "Other", "channel": "bark", "enabled": True, "log_mode": "full", "config": {}},
        ], "notify_default_ids": ["n", "other"]})

    def values(self, action="toggle", **extra):
        from fls_manager.notify_actions import notification_config, notification_version
        return dict({"action": action, "item_id": "n", "request_id": "a" * 32, "enabled": False,
                     "default_ids": ["other"], "version": notification_version(notification_config(), None if action == "default" else "n")}, **extra)

    def post(self, client, data):
        return client.post("/api/notify/action", json=data, headers={"X-Token": TOKEN})

    def test_actions_atomic_defaults_and_restart_replay_preserve_later_config(self):
        for action in ("default", "toggle", "delete"):
            with self.subTest(action=action), isolated_app() as (app, base):
                from fls_manager import notify_actions
                from fls_manager.config import load_config
                self.seed()
                data = self.values(action)
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                cfg = load_config()
                self.assertEqual(cfg["notify_default_ids"], ["other"])
                if action == "toggle":
                    self.assertFalse(cfg["notify_items"][0]["enabled"])
                elif action == "delete":
                    self.assertEqual([item["id"] for item in cfg["notify_items"]], ["other"])
                self.assertNotIn("private-secret", (base / "data/notify_requests" / ("a" * 32 + ".json")).read_text())
                self.seed()
                importlib.reload(notify_actions)
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                self.assertEqual(load_config()["notify_default_ids"], ["n", "other"])
                self.assertTrue(load_config()["notify_items"][0]["enabled"])
                self.assertEqual(self.post(app.test_client(), dict(data, version="f" * 64)).status_code, 409)

    def test_invalid_selection_stale_reference_and_get_have_zero_side_effects(self):
        for kind in ("missing", "invalid", "stale", "reference", "delete_reference", "default_missing", "default_disabled"):
            with self.subTest(kind=kind), isolated_app() as (app, base):
                from fls_manager import models, paths
                from fls_manager.config import load_config, save_config
                self.seed()
                if kind == "default_disabled":
                    cfg = load_config()
                    cfg["notify_items"][0]["enabled"] = False
                    save_config(cfg)
                data = self.values("default" if kind.startswith("default") else "delete" if kind == "delete_reference" else "toggle")
                if kind == "missing":
                    data["item_id"] = "missing"
                elif kind == "invalid":
                    data["enabled"] = 1
                elif kind == "stale":
                    data["version"] = "f" * 64
                elif kind in ("reference", "delete_reference"):
                    models.save_tasks([sample_task("t", notify={"mode": "custom", "ids": ["n"]})])
                else:
                    data["default_ids"] = ["other", "missing" if kind == "default_missing" else "n"]
                before = paths.CONFIG_FILE.read_bytes()
                self.assertIn(self.post(app.test_client(), data).status_code, (400, 404, 409))
                self.assertFalse((base / "data/notify_requests").exists())
                self.assertEqual(paths.CONFIG_FILE.read_bytes(), before)
                self.assertEqual(app.test_client().get("/notify/toggle/n", headers={"X-Token": TOKEN}).status_code, 302)
                self.assertEqual(paths.CONFIG_FILE.read_bytes(), before)

    def test_publication_before_after_and_receipt_failures_match_real_file(self):
        for action in ("default", "toggle", "delete"):
            for mode in ("before", "after", "receipt", "prepared"):
                with self.subTest(action=action, mode=mode), isolated_app() as (app, _):
                    from fls_manager import paths, notify_actions
                    from fls_manager.config import load_config
                    self.seed()
                    data = self.values(action)
                    before = paths.CONFIG_FILE.read_bytes()
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
                    self.assertEqual(load_config()["notify_default_ids"], ["other"])

    def test_corrupt_config_receipt_and_unknown_verification_never_overwrite(self):
        for raw in ("{", "[]", '{"notify_items":[null]}', '{"notify_items":[{"id":"n","channel":"missing"}]}'):
            with self.subTest(raw=raw), isolated_app() as (app, _):
                from fls_manager import paths
                self.seed()
                data = self.values()
                paths.CONFIG_FILE.write_text(raw, encoding="utf-8")
                self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                self.assertEqual(paths.CONFIG_FILE.read_text(), raw)
        with isolated_app() as (app, base):
            from fls_manager import notify_actions
            from fls_manager.config import save_config
            self.seed()
            data = self.values()
            path = base / "data/notify_requests" / ("a" * 32 + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            for raw in ("{", "null", "{}"):
                path.write_text(raw)
                self.assertEqual(self.post(app.test_client(), data).status_code, 500)
            path.unlink()
            original = notify_actions.notification_config
            def publish(values):
                save_config(values)
                raise OSError("after publish")
            with patch("fls_manager.config.save_config", side_effect=publish), patch.object(notify_actions, "notification_config", side_effect=[original(), PermissionError("denied")]):
                response = self.post(app.test_client(), data)
            self.assertEqual(response.status_code, 500)
            self.assertIsNone(response.json["saved"])
            self.assertEqual(self.post(app.test_client(), data).status_code, 200)

    def test_concurrent_duplicate_and_panel_change_keep_both_results(self):
        with isolated_app() as (app, _):
            from fls_manager.config import load_config, save_config
            from fls_manager.storage import storage_transaction
            self.seed()
            data = self.values()
            entered, release = threading.Event(), threading.Event()
            def persist(values):
                entered.set()
                self.assertTrue(release.wait(5))
                save_config(values)
            def panel_update():
                with storage_transaction():
                    save_config({"task_timeout_seconds": 99})
            with patch("fls_manager.config.save_config", side_effect=persist) as save, ThreadPoolExecutor(3) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client(), data)
                third = pool.submit(panel_update)
                release.set()
                self.assertEqual([f.result(5).status_code for f in (first, second)], [200, 200])
                third.result(5)
                self.assertEqual(save.call_count, 1)
            cfg = load_config()
            self.assertEqual(cfg["task_timeout_seconds"], 99)
            self.assertEqual(cfg["notify_default_ids"], ["other"])
            self.assertFalse(cfg["notify_items"][0]["enabled"])

    def test_form_permissions_csrf_preserved_selection_and_retry(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.notify import actions
            from fls_manager.config import load_config
            self.seed()
            client = app.test_client()
            data = self.values()
            self.assertEqual(client.post("/api/notify/action", json=data).status_code, 400)
            self.authenticate(client)
            self.assertEqual(client.post("/api/notify/action", json=data).status_code, 400)
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                self.assertEqual(client.post("/api/notify/action?token=" + TOKEN, json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 403)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.post("/api/notify/action", json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 401)
            self.authenticate(client)
            form = self.form(client.get("/notify"), "/notify/toggle/n")
            self.assertEqual(client.post(form["action"], data=dict(form["fields"], csrf_token="wrong")).status_code, 400)
            with patch.object(actions, "save_config", side_effect=OSError("full")):
                response = client.post(form["action"], data=form["fields"])
            self.assertEqual(response.status_code, 500)
            retry = self.form(response, "/notify/toggle/n")
            self.assertEqual(retry["fields"]["request_id"], form["fields"]["request_id"])
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertEqual(client.post(form["action"], data=form["fields"]).status_code, 302)
            self.assertFalse(load_config()["notify_items"][0]["enabled"])
            default = self.form(client.get("/notify"), "/notify/default")
            response = client.post(default["action"], data=dict(default["fields"], default_ids=["missing", "other"]))
            self.assertEqual(response.status_code, 400)
            self.assertIn('aria-label="重新核对默认通知"', response.get_data(as_text=True))

    def test_task_custom_reference_blocks_edit_bypass_and_default_consumes_saved_selection(self):
        with isolated_app() as (app, _):
            from fls_manager import models, notify
            self.seed()
            models.save_tasks([sample_task("t", notify={"mode": "custom", "ids": ["n"]})])
            client = app.test_client()
            self.authenticate(client)
            form = self.form(client.get("/notify/edit/n"), "")
            form["fields"].pop("enabled", None)
            form["fields"].update(channel="webhook", log_mode="full")
            self.assertEqual(client.post("/notify/edit/n", data=form["fields"]).status_code, 409)
            data = self.values("default")
            self.assertEqual(self.post(client, data).status_code, 200)
            with patch.object(notify, "send_one", return_value=(True, "sent")) as send:
                results = notify.send_by_ids("Actual task", "Body", notify.task_notify_ids({"notify": {"mode": "default"}}))
            self.assertEqual([row["id"] for row in results], ["other"])
            self.assertEqual(send.call_args.args[0]["id"], "other")

    def test_retry_checks_new_references_and_does_not_overwrite_external_changes(self):
        for changed in (False, True):
            with self.subTest(changed=changed), isolated_app() as (app, _):
                from fls_manager import models
                from fls_manager.config import load_config, save_config
                self.seed()
                data = self.values("delete")
                with patch("fls_manager.config.save_config", side_effect=OSError("full")):
                    self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                if changed:
                    cfg = load_config()
                    cfg["notify_items"][0]["name"] = "Later"
                    save_config(cfg)
                else:
                    models.save_tasks([sample_task("t", notify={"mode": "custom", "ids": ["n"]})])
                before = load_config()
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(load_config(), before)
                if changed:
                    self.assertIsNone(response.json["saved"])
                else:
                    self.assertEqual(response.json["references"][0]["id"], "t")

    def test_edit_test_send_runs_after_storage_and_task_locks_are_released(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            from fls_manager.config import save_config, load_config
            from fls_manager.storage import storage_transaction
            from fls_manager.routes.notify import actions
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            form = self.form(client.get("/notify/edit/n"), "")
            def concurrent_config():
                with models.TASKS_MUTATION_LOCK, storage_transaction():
                    save_config({"task_timeout_seconds": 123})
            def send(*args, **kwargs):
                with ThreadPoolExecutor(1) as pool:
                    pool.submit(concurrent_config).result(3)
                return True, "sent"
            form["fields"].update(channel="webhook", enabled="1", log_mode="full", action="test")
            with patch.object(actions, "send_one", side_effect=send) as sending:
                self.assertEqual(client.post("/notify/edit/n", data=form["fields"]).status_code, 200)
                sending.assert_called_once()
            self.assertEqual(load_config()["task_timeout_seconds"], 123)

    def test_legacy_migration_and_page_version_use_same_snapshot(self):
        with isolated_app() as (app, _):
            from fls_manager.config import save_config, load_config
            from fls_manager.notify_actions import notification_snapshot
            from fls_manager.routes.notify import pages
            save_config({"notify_items": [{"channel": "bark", "config": {}}]})
            client = app.test_client()
            self.authenticate(client)
            self.assertEqual(client.get("/notify").status_code, 200)
            item_id = load_config()["notify_items"][0]["id"]
            form = self.form(client.get("/notify/edit/" + item_id), "")
            form["fields"].update(channel="bark", enabled="1", log_mode="full")
            self.assertEqual(client.post("/notify/edit/" + item_id, data=form["fields"]).status_code, 302)
            def changed_after_snapshot():
                cfg = notification_snapshot()
                save_config({"notify_default_ids": [item_id]})
                return cfg
            with patch.object(pages, "notification_snapshot", side_effect=changed_after_snapshot):
                form = self.form(client.get("/notify"), "/notify/default")
            self.assertEqual(client.post(form["action"], data=form["fields"]).status_code, 409)
            self.assertEqual(load_config()["notify_default_ids"], [item_id])
