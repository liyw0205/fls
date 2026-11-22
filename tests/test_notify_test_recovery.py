import importlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests import test_notify_action_recovery as action_tests

isolated_app = action_tests.isolated_app


class NotifyTestRecoveryTests(unittest.TestCase):
    seed = action_tests.NotifyActionRecoveryTests.seed
    authenticate = action_tests.NotifyActionRecoveryTests.authenticate
    form = action_tests.NotifyActionRecoveryTests.form

    def values(self):
        from fls_manager.notify_actions import notification_config, notification_version
        return {"request_id": "b" * 32, "version": notification_version(notification_config(), "n")}

    def post(self, client, data, item_id="n"):
        return client.post("/api/notify/test/" + item_id, json=data, headers={"X-Token": TOKEN})

    def test_completed_replay_survives_restart_config_change_and_history_pruning(self):
        with isolated_app() as (app, base):
            from fls_manager import notify, notify_test_actions, paths
            from fls_manager.config import save_config
            self.seed()
            data = self.values()
            with patch.object(notify, "send_one", return_value=(True, "accepted")) as send:
                first = self.post(app.test_client(), data)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json["delivery_status"], "succeeded")
                save_config({"notify_items": [], "notify_default_ids": []})
                importlib.reload(notify_test_actions)
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                paths.NOTIFY_HISTORY_FILE.write_text("[]")
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                send.assert_called_once()
                self.assertEqual(self.post(app.test_client(), dict(data, version="e" * 64)).status_code, 409)
            self.assertNotIn("private-secret", (base / "data/notify_test_requests" / ("b" * 32 + ".json")).read_text())

    def test_invalid_stale_disabled_and_get_have_zero_side_effects(self):
        for kind in ("request", "version", "confirm", "missing", "stale", "disabled"):
            with self.subTest(kind=kind), isolated_app() as (app, base):
                from fls_manager import notify, paths
                from fls_manager.config import load_config, save_config
                self.seed()
                data = self.values()
                if kind in ("stale", "disabled"):
                    cfg = load_config()
                    cfg["notify_items"][0]["enabled" if kind == "disabled" else "name"] = False if kind == "disabled" else "Changed"
                    save_config(cfg)
                elif kind == "request":
                    data["request_id"] = "../x"
                elif kind == "version":
                    data["version"] = None
                elif kind == "confirm":
                    data["confirm_retry"] = 1
                before = paths.CONFIG_FILE.read_bytes()
                with patch.object(notify, "send_one") as send:
                    response = self.post(app.test_client(), data, "missing" if kind == "missing" else "n")
                    self.assertIn(response.status_code, (400, 404, 409))
                    self.assertEqual(app.test_client().get("/notify/test/n", headers={"X-Token": TOKEN}).status_code, 302)
                    send.assert_not_called()
                self.assertFalse((base / "data/notify_test_requests").exists())
                self.assertFalse(paths.NOTIFY_HISTORY_FILE.exists())
                self.assertEqual(paths.CONFIG_FILE.read_bytes(), before)

    def test_publication_faults_expose_real_delivery_without_automatic_resend(self):
        modes = ("request", "history_before", "history_after", "sending_receipt", "result_before", "result_after", "completed_receipt")
        for mode in modes:
            with self.subTest(mode=mode), isolated_app() as (app, _):
                from fls_manager import notify, notify_test_actions
                self.seed()
                data = self.values()
                save_history, save_request = notify.write_json, notify_test_actions.write_json
                def history(path, rows):
                    state = rows[0]["status"]
                    if (mode.startswith("history_") and state == "running") or (mode.startswith("result_") and state == "succeeded"):
                        if mode.endswith("after"):
                            save_history(path, rows)
                        raise OSError("publication fault")
                    return save_history(path, rows)
                def receipt(path, row):
                    if mode == "request" or mode == "sending_receipt" and row["state"] == "sending" or mode == "completed_receipt" and row["state"] == "completed":
                        raise OSError("request fault")
                    return save_request(path, row)
                with patch.object(notify, "send_one", return_value=(True, "accepted")) as send:
                    with patch.object(notify, "write_json", side_effect=history), patch.object(notify_test_actions, "write_json", side_effect=receipt):
                        response = self.post(app.test_client(), data)
                    sent = mode.startswith("result_") or mode == "completed_receipt"
                    self.assertEqual(send.call_count, int(sent))
                    self.assertEqual(response.status_code, 503 if sent else 500)
                    replay = self.post(app.test_client(), data)
                    if mode == "request":
                        self.assertEqual(replay.status_code, 200)
                        self.assertEqual(send.call_count, 1)
                    elif mode == "history_before":
                        self.assertTrue(replay.json["confirm_required"])
                        self.assertEqual(self.post(app.test_client(), dict(data, confirm_retry=True)).status_code, 200)
                        self.assertEqual(send.call_count, 1)
                    else:
                        self.assertEqual(send.call_count, int(sent))
                        self.assertEqual(replay.json["delivery_status"], "succeeded" if mode in ("result_after", "completed_receipt") else "unknown")

    def test_network_failure_and_explicit_retry_reuse_original_delivery(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            self.seed()
            data = self.values()
            with patch.object(notify, "send_one", side_effect=RuntimeError("connection failed")) as send:
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json["delivery_status"], "failed")
                self.assertEqual(self.post(app.test_client(), data).status_code, 502)
                send.assert_called_once()
            delivery_id = response.json["delivery_id"]
            with patch.object(notify, "send_one", return_value=(True, "recovered")) as send:
                self.assertEqual(app.test_client().post("/notify/retry/" + delivery_id, headers={"X-Token": TOKEN}).status_code, 200)
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                self.assertEqual(app.test_client().post("/notify/retry/" + delivery_id, headers={"X-Token": TOKEN}).status_code, 409)
                send.assert_called_once()
            self.assertEqual(notify.get_notify_delivery(delivery_id)["attempt"], 2)

    def test_full_recoverable_history_reports_not_sent_and_resumes_same_test_request(self):
        with isolated_app() as (app, base):
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            self.seed()
            notify.NOTIFY_HISTORY_LIMIT = 1
            write_json(paths.NOTIFY_HISTORY_FILE, [{"id": "pending", "status": "unknown"}])
            original_history = paths.NOTIFY_HISTORY_FILE.read_bytes()
            data = self.values()

            with patch.object(notify, "send_one", return_value=(True, "accepted")) as send, \
                 patch("fls_manager.routes.notify.test.send_one", return_value=(True, "accepted")) as html_send:
                client = app.test_client()
                blocked = self.post(client, data)
                self.assertEqual(blocked.status_code, 409)
                self.assertFalse(blocked.json["sent"])
                self.assertEqual(blocked.json["delivery_status"], "not_started")
                self.assertTrue(blocked.json["confirm_required"])
                self.assertFalse(blocked.json["resolved"])
                send.assert_not_called()
                self.assertEqual(paths.NOTIFY_HISTORY_FILE.read_bytes(), original_history)

                form = self.form(client.get("/notify", headers={"X-Token": TOKEN}), "/notify/test/n")
                page = client.post(form["action"], data=form["fields"], headers={"X-Token": TOKEN})
                self.assertEqual(page.status_code, 409)
                self.assertIn("测试未发送", page.get_data(as_text=True))
                self.assertIn("确认后继续测试", page.get_data(as_text=True))
                self.assertIn('href="/notify/history"', page.get_data(as_text=True))
                send.assert_not_called()

                write_json(paths.NOTIFY_HISTORY_FILE, [{"id": "pending", "status": "succeeded"}])
                retry_form = self.form(page, "/notify/test/n")
                resumed_page = client.post(retry_form["action"], data=retry_form["fields"], headers={"X-Token": TOKEN})
                self.assertEqual(resumed_page.status_code, 200)
                self.assertIn("测试通知已发送", resumed_page.get_data(as_text=True))
                self.assertEqual(retry_form["fields"]["request_id"], form["fields"]["request_id"])
                html_send.assert_called_once()
                resumed = self.post(client, dict(data, confirm_retry=True))

            self.assertEqual(resumed.status_code, 200)
            self.assertEqual(resumed.json["delivery_status"], "succeeded")
            send.assert_called_once()
            html_send.assert_called_once()
            for request_id in (data["request_id"], form["fields"]["request_id"]):
                receipt = base / "data/notify_test_requests" / (request_id + ".json")
                self.assertEqual(json.loads(receipt.read_text())["state"], "completed")

    def test_concurrent_replay_does_not_hold_storage_or_history_during_network(self):
        with isolated_app() as (app, _):
            from fls_manager import notify, models, notify_test_actions
            from fls_manager.config import save_config
            from fls_manager.storage import storage_transaction
            self.seed()
            data = self.values()
            entered, release = threading.Event(), threading.Event()
            def send(*args):
                entered.set()
                self.assertTrue(release.wait(5))
                return True, "accepted"
            def update():
                with models.TASKS_MUTATION_LOCK, storage_transaction():
                    save_config({"task_timeout_seconds": 12})
                with notify.NOTIFY_HISTORY_LOCK:
                    self.assertEqual(len(notify.load_notify_history()), 1)
            with patch.object(notify, "send_one", side_effect=send) as sender, ThreadPoolExecutor(3) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(3))
                try:
                    pool.submit(update).result(3)
                    second = pool.submit(self.post, app.test_client(), data).result(3)
                    self.assertEqual(second.status_code, 409)
                    self.assertEqual(second.json["delivery_status"], "running")
                    with patch.object(notify_test_actions, "_strict_history", side_effect=PermissionError("denied")):
                        self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                    self.assertIn(second.json["delivery_id"], notify.NOTIFY_ACTIVE_DELIVERIES)
                    self.assertEqual(notify.retry_notify_delivery(second.json["delivery_id"])[1], "通知投递正在发送")
                finally:
                    release.set()
                self.assertEqual(first.result(3).status_code, 200)
                sender.assert_called_once()

    def test_corrupt_history_or_receipt_never_overwritten_or_sent(self):
        for target in ("history", "receipt"):
            for raw in ("{", "null", "{}", "[null]"):
                with self.subTest(target=target, raw=raw), isolated_app() as (app, base):
                    from fls_manager import paths, notify
                    self.seed()
                    data = self.values()
                    path = paths.NOTIFY_HISTORY_FILE if target == "history" else base / "data/notify_test_requests" / ("b" * 32 + ".json")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(raw)
                    with patch.object(notify, "send_one") as send:
                        self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                        send.assert_not_called()
                    self.assertEqual(path.read_text(), raw)

    def test_corruption_during_send_and_missing_history_do_not_resend(self):
        with isolated_app() as (app, base):
            from fls_manager import notify, paths
            self.seed()
            data = self.values()
            def send(*args):
                paths.NOTIFY_HISTORY_FILE.write_text("{")
                return True, "accepted"
            with patch.object(notify, "send_one", side_effect=send):
                response = self.post(app.test_client(), data)
            self.assertEqual(response.status_code, 503)
            self.assertFalse(response.json["resolved"])
            self.assertEqual(paths.NOTIFY_HISTORY_FILE.read_text(), "{")
            paths.NOTIFY_HISTORY_FILE.write_text("[]")
            with patch.object(notify, "send_one") as send:
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 409)
                self.assertIsNone(response.json["sent"])
                send.assert_not_called()

    def test_form_csrf_permissions_recovery_and_confirmation(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            self.seed()
            data = self.values()
            client = app.test_client()
            self.assertEqual(client.post("/api/notify/test/n", json=data).status_code, 400)
            self.authenticate(client)
            self.assertEqual(client.post("/api/notify/test/n", json=data).status_code, 400)
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                self.assertEqual(client.post("/api/notify/test/n?token=" + TOKEN, json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 403)
            form = self.form(client.get("/notify"), "/notify/test/n")
            self.assertEqual(client.post(form["action"], data=dict(form["fields"], csrf_token="wrong")).status_code, 400)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.post("/api/notify/test/n", json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 401)
            self.authenticate(client)
            with patch.object(notify, "create_notify_delivery", side_effect=OSError("full")):
                response = client.post(form["action"], data=form["fields"])
            self.assertEqual(response.status_code, 500)
            retry = self.form(response, form["action"])
            self.assertEqual(retry["fields"]["request_id"], form["fields"]["request_id"])
            response = client.post(retry["action"], data=retry["fields"])
            self.assertEqual(response.status_code, 409)
            confirm = self.form(response, form["action"])
            self.assertEqual(confirm["fields"]["confirm_retry"], "1")
            with patch("fls_manager.routes.notify.test.send_one", return_value=(False, "offline")) as send:
                response = client.post(confirm["action"], data=confirm["fields"])
                self.assertEqual(response.status_code, 502)
                self.assertIn("确认后重试此投递", response.get_data(as_text=True))
                send.assert_called_once()
