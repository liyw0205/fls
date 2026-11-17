import importlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests import test_notify_action_recovery as action_tests

isolated_app = action_tests.isolated_app


class NotifyRetryRecoveryTests(unittest.TestCase):
    authenticate = action_tests.NotifyActionRecoveryTests.authenticate
    form = action_tests.NotifyActionRecoveryTests.form

    def seed(self):
        action_tests.NotifyActionRecoveryTests.seed(self)
        from fls_manager import notify
        delivery_id = notify.create_notify_delivery({"id": "n", "name": "Original", "channel": "webhook"},
                                                   "Original title", "Original body")
        notify.finish_notify_delivery(delivery_id, False, "initial failure")
        return delivery_id

    def values(self, delivery_id, **extra):
        from fls_manager import notify
        from fls_manager.notify_retry_actions import delivery_version
        row = notify.get_notify_delivery(delivery_id)
        return dict({"request_id": "d" * 32, "version": delivery_version(row), "expected_attempt": row["attempt"]}, **extra)

    def post(self, client, delivery_id, data):
        return client.post("/api/notify/retry/" + delivery_id, json=data, headers={"X-Token": TOKEN})

    def test_failure_replay_restart_and_later_attempt_never_resend(self):
        with isolated_app() as (app, base):
            from fls_manager import notify, notify_retry_actions
            delivery_id = self.seed()
            data = self.values(delivery_id)
            with patch.object(notify, "send_one", return_value=(False, "still offline")) as send:
                response = self.post(app.test_client(), delivery_id, data)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json["attempt"], 2)
                importlib.reload(notify_retry_actions)
                self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 502)
                send.assert_called_once()
                self.assertEqual(send.call_args.args[1:], ("Original title", "Original body"))
            next_data = self.values(delivery_id, request_id="e" * 32)
            with patch.object(notify, "send_one", return_value=(True, "accepted")) as send:
                self.assertEqual(self.post(app.test_client(), delivery_id, next_data).status_code, 200)
                self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 502)
                self.assertEqual(self.post(app.test_client(), delivery_id, next_data).status_code, 200)
                send.assert_called_once()
            self.assertEqual(notify.get_notify_delivery(delivery_id)["attempt"], 3)
            receipt = (base / "data/notify_retry_requests" / ("d" * 32 + ".json")).read_text()
            self.assertNotIn("Original body", receipt)
            self.assertNotIn("private-secret", receipt)
            notify.NOTIFY_HISTORY_FILE.write_text("[]")
            self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 502)
            self.assertEqual(self.post(app.test_client(), delivery_id, next_data).status_code, 200)

    def test_history_api_exposes_an_executable_stable_retry_request(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            delivery_id = self.seed()
            history = app.test_client().get("/api/notify/history", headers={"X-Token": TOKEN})
            data = history.json["items"][0]["retry_request"]
            with patch.object(notify, "send_one", return_value=(False, "offline")) as send:
                response = self.post(app.test_client(), delivery_id, data)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 502)
                send.assert_called_once()

    def test_deleted_channel_can_explicitly_retry_original_content_with_selected_replacement(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            from fls_manager.config import load_config, save_config
            delivery_id = self.seed()
            data = self.values(delivery_id)
            cfg = load_config()
            cfg["notify_items"].pop(0)
            save_config(cfg)
            history = notify.NOTIFY_HISTORY_FILE.read_bytes()
            response = self.post(app.test_client(), delivery_id, data)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json["channels"], [{"id": "other", "name": "Other"}])
            self.assertEqual(self.post(app.test_client(), delivery_id, dict(data, item_id="missing")).status_code, 400)
            self.assertEqual(notify.NOTIFY_HISTORY_FILE.read_bytes(), history)
            with patch.object(notify, "send_one", return_value=(True, "accepted")) as send:
                selected = dict(data, request_id="e" * 32, item_id="other")
                self.assertEqual(self.post(app.test_client(), delivery_id, selected).status_code, 200)
                self.assertEqual(self.post(app.test_client(), delivery_id, selected).status_code, 200)
                send.assert_called_once()
                self.assertEqual(send.call_args.args[0]["id"], "other")
                self.assertEqual(send.call_args.args[1:], ("Original title", "Original body"))
            self.assertEqual(notify.get_notify_delivery(delivery_id)["item_id"], "other")

    def test_invalid_stale_missing_disabled_and_deleted_have_zero_side_effects(self):
        for mode in ("request", "version", "attempt", "bool_attempt", "negative_attempt", "confirm", "missing", "stale", "disabled", "deleted"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager import notify, paths
                from fls_manager.config import load_config, save_config
                delivery_id = self.seed()
                data = self.values(delivery_id)
                invalid = {"request": ("request_id", "bad"), "version": ("version", "bad"),
                           "attempt": ("expected_attempt", []), "bool_attempt": ("expected_attempt", True),
                           "negative_attempt": ("expected_attempt", -1), "confirm": ("confirm_retry", 1)}
                if mode in invalid:
                    data[invalid[mode][0]] = invalid[mode][1]
                elif mode == "stale":
                    data["expected_attempt"] += 1
                elif mode in ("disabled", "deleted"):
                    cfg = load_config()
                    if mode == "disabled":
                        cfg["notify_items"][0]["enabled"] = False
                    else:
                        cfg["notify_items"].pop(0)
                    save_config(cfg)
                history, config = paths.NOTIFY_HISTORY_FILE.read_bytes(), paths.CONFIG_FILE.read_bytes()
                with patch.object(notify, "send_one") as send:
                    response = self.post(app.test_client(), "missing" if mode == "missing" else delivery_id, data)
                    self.assertIn(response.status_code, (400, 404, 409))
                    send.assert_not_called()
                self.assertEqual(paths.NOTIFY_HISTORY_FILE.read_bytes(), history)
                self.assertEqual(paths.CONFIG_FILE.read_bytes(), config)
                self.assertFalse((base / "data/notify_retry_requests").exists())

    def test_publication_failures_match_real_attempt_and_require_explicit_next_retry(self):
        for mode in ("prepared_before", "prepared_after", "claim_before", "claim_after", "claimed_receipt", "result_before", "result_after", "completed_receipt"):
            with self.subTest(mode=mode), isolated_app() as (app, _):
                from fls_manager import notify, notify_retry_actions
                delivery_id = self.seed()
                data = self.values(delivery_id)
                history_write, receipt_write = notify.write_json, notify_retry_actions.write_json
                def history(path, rows):
                    state = rows[0]["status"]
                    if (mode.startswith("claim_") and state == "running") or (mode.startswith("result_") and state == "succeeded"):
                        if mode.endswith("after"):
                            history_write(path, rows)
                        raise OSError("history fault")
                    return history_write(path, rows)
                def receipt(path, row):
                    if mode.startswith("prepared_") and row["state"] == "prepared" or mode == "claimed_receipt" and row["state"] == "claimed" or mode == "completed_receipt" and row["state"] == "completed":
                        if mode == "prepared_after":
                            receipt_write(path, row)
                        raise OSError("receipt fault")
                    return receipt_write(path, row)
                with patch.object(notify, "send_one", return_value=(True, "accepted")) as send:
                    with patch.object(notify, "write_json", side_effect=history), patch.object(notify_retry_actions, "write_json", side_effect=receipt):
                        response = self.post(app.test_client(), delivery_id, data)
                    sent = mode.startswith("result_") or mode == "completed_receipt"
                    self.assertEqual(send.call_count, int(sent))
                    self.assertEqual(response.status_code, 500 if mode.startswith("prepared_") or mode == "claim_before" else 503)
                    replay = self.post(app.test_client(), delivery_id, data)
                    if mode in ("prepared_before", "claim_before"):
                        self.assertEqual(replay.status_code, 200)
                        self.assertEqual(send.call_count, 1)
                    elif mode == "prepared_after":
                        self.assertEqual(replay.status_code, 409)
                        self.assertTrue(replay.json["confirm_required"])
                        self.assertEqual(self.post(app.test_client(), delivery_id, dict(data, confirm_retry=True)).status_code, 200)
                        self.assertEqual(send.call_count, 1)
                    else:
                        self.assertEqual(send.call_count, int(sent))
                        self.assertEqual(replay.json["delivery_status"], "succeeded" if mode in ("result_after", "completed_receipt") else "unknown")
                    self.assertNotIn(delivery_id, notify.NOTIFY_ACTIVE_DELIVERIES)

    def test_concurrent_duplicate_and_independent_attempt_have_one_network_send(self):
        with isolated_app() as (app, _):
            from fls_manager import notify, notify_retry_actions, models
            from fls_manager.config import save_config
            from fls_manager.storage import storage_transaction
            delivery_id = self.seed()
            data = self.values(delivery_id)
            entered, release = threading.Event(), threading.Event()
            def sender(*args):
                entered.set()
                self.assertTrue(release.wait(5))
                return False, "offline"
            def update():
                with models.TASKS_MUTATION_LOCK, storage_transaction():
                    save_config({"task_timeout_seconds": 42})
            with patch.object(notify, "send_one", side_effect=sender) as send, ThreadPoolExecutor(3) as pool:
                first = pool.submit(self.post, app.test_client(), delivery_id, data)
                self.assertTrue(entered.wait(3))
                try:
                    pool.submit(update).result(3)
                    duplicate = pool.submit(self.post, app.test_client(), delivery_id, data).result(3)
                    self.assertEqual(duplicate.status_code, 409)
                    self.assertEqual(duplicate.json["delivery_status"], "running")
                    self.assertEqual(self.post(app.test_client(), delivery_id, dict(data, request_id="e" * 32)).status_code, 409)
                    with patch.object(notify_retry_actions, "notification_config", side_effect=PermissionError("denied")):
                        self.assertEqual(self.post(app.test_client(), delivery_id, dict(data, request_id="e" * 32)).status_code, 409)
                    self.assertIn(delivery_id, notify.NOTIFY_ACTIVE_DELIVERIES)
                finally:
                    release.set()
                self.assertEqual(first.result(3).status_code, 502)
                self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 502)
                send.assert_called_once()

    def test_corrupt_history_receipts_and_permissions_never_overwrite_or_send(self):
        for target in ("history", "receipt"):
            for raw in ("{", "null", "{}", "[null]", '[{"id":"x"},{"id":"x"}]',
                        '[{"id":" "}]', '[{"id":"x"},{"id":" x "}]', '[{"id":"x\\u0000"}]'):
                with self.subTest(target=target, raw=raw), isolated_app() as (app, base):
                    from fls_manager import notify, paths
                    delivery_id = self.seed()
                    data = self.values(delivery_id)
                    path = paths.NOTIFY_HISTORY_FILE if target == "history" else base / "data/notify_retry_requests" / ("d" * 32 + ".json")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(raw)
                    with patch.object(notify, "send_one") as send:
                        self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 500)
                        send.assert_not_called()
                    self.assertEqual(path.read_text(), raw)
                    if target == "history":
                        self.assertEqual(app.test_client().get("/api/notify/history", headers={"X-Token": TOKEN}).status_code, 500)
                        self.assertEqual(app.test_client().get("/notify/history", headers={"X-Token": TOKEN}).status_code, 500)
                        self.assertEqual(path.read_text(), raw)
        with isolated_app() as (app, _):
            from fls_manager import notify_retry_actions, notify
            delivery_id = self.seed()
            data = self.values(delivery_id)
            before = notify.NOTIFY_HISTORY_FILE.read_bytes()
            with patch.object(notify_retry_actions, "notification_config", side_effect=PermissionError("denied")), patch.object(notify, "send_one") as send:
                self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 500)
                send.assert_not_called()
            self.assertEqual(notify.NOTIFY_HISTORY_FILE.read_bytes(), before)

    def test_history_becomes_unreadable_after_network_releases_claim_and_preserves_bytes(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            delivery_id = self.seed()
            data = self.values(delivery_id)
            def send(*args):
                notify.NOTIFY_HISTORY_FILE.write_text("{")
                return True, "accepted"
            with patch.object(notify, "send_one", side_effect=send):
                self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 503)
            self.assertNotIn(delivery_id, notify.NOTIFY_ACTIVE_DELIVERIES)
            self.assertEqual(notify.NOTIFY_HISTORY_FILE.read_text(), "{")
            # Repairing storage must allow querying the old attempt, not resending it.
            notify.NOTIFY_HISTORY_FILE.write_text(json.dumps([{"id": delivery_id, "item_id": "n", "status": "failed", "attempt": 3}]))
            with patch.object(notify, "send_one") as send:
                self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 409)
                send.assert_not_called()

    def test_external_history_change_during_send_is_not_marked_successful(self):
        with isolated_app() as (app, _):
            from fls_manager import notify
            delivery_id = self.seed()
            data = self.values(delivery_id)
            changed = []
            def sender(*args):
                rows = notify.read_notify_history_rows()
                rows[0]["content"] = "Externally changed body"
                rows[0]["status"] = "failed"
                notify.write_json(notify.NOTIFY_HISTORY_FILE, rows)
                changed.append(notify.NOTIFY_HISTORY_FILE.read_bytes())
                return True, "accepted original body"
            with patch.object(notify, "send_one", side_effect=sender) as send:
                response = self.post(app.test_client(), delivery_id, data)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(notify.NOTIFY_HISTORY_FILE.read_bytes(), changed[0])
                self.assertEqual(self.post(app.test_client(), delivery_id, data).status_code, 409)
                self.assertEqual(notify.NOTIFY_HISTORY_FILE.read_bytes(), changed[0])
                send.assert_called_once()
            self.assertNotIn(delivery_id, notify.NOTIFY_ACTIVE_DELIVERIES)

    def test_legacy_channel_read_failure_and_completion_read_failure_release_claim(self):
        for mode in ("config", "result"):
            with self.subTest(mode=mode), isolated_app():
                from fls_manager import notify, notify_actions
                delivery_id = self.seed()
                if mode == "config":
                    with patch.object(notify_actions, "notification_config", side_effect=PermissionError("denied")), patch.object(notify, "send_one") as send:
                        ok, message, row = notify.retry_notify_delivery(delivery_id)
                        self.assertFalse(ok)
                        self.assertEqual(row["status"], "failed")
                        send.assert_not_called()
                else:
                    def sender(*args):
                        notify.NOTIFY_HISTORY_FILE.write_text("{")
                        return True, "accepted"
                    with patch.object(notify, "send_one", side_effect=sender):
                        with self.assertRaises(ValueError):
                            notify.retry_notify_delivery(delivery_id)
                self.assertNotIn(delivery_id, notify.NOTIFY_ACTIVE_DELIVERIES)

    def test_form_csrf_permissions_prepared_confirmation_and_retry_failure(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_retry_actions, notify
            delivery_id = self.seed()
            data = self.values(delivery_id)
            client = app.test_client()
            self.assertEqual(client.post("/api/notify/retry/" + delivery_id, json=data).status_code, 400)
            self.authenticate(client)
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                self.assertEqual(client.post("/api/notify/retry/" + delivery_id + "?token=" + TOKEN, json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 403)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.post("/api/notify/retry/" + delivery_id, json=data, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 401)
            self.authenticate(client)
            form = self.form(client.get("/notify/history"), "/notify/retry/")
            self.assertEqual(client.post(form["action"], data=dict(form["fields"], csrf_token="wrong")).status_code, 400)
            with patch.object(notify, "write_json", side_effect=OSError("claim rejected")), patch.object(notify_retry_actions, "write_json", side_effect=OSError("receipt rejected")):
                response = client.post(form["action"], data=form["fields"])
            self.assertEqual(response.status_code, 500)
            retry = self.form(response, form["action"])
            with patch.object(notify, "send_one", return_value=(False, "offline")) as send:
                response = client.post(retry["action"], data=retry["fields"])
                self.assertEqual(response.status_code, 502)
                self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 502)
                send.assert_called_once()
            next_form = self.form(response, form["action"])
            self.assertNotEqual(next_form["fields"]["request_id"], retry["fields"]["request_id"])
            self.assertEqual(next_form["fields"]["expected_attempt"], "2")
