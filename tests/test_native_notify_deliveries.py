import json
import threading
import unittest
from unittest.mock import Mock, patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app


class HeldThread:
    jobs = []

    def __init__(self, *, target, args, daemon):
        self.target, self.args = target, args

    def start(self):
        self.jobs.append(self)

    @classmethod
    def finish(cls):
        while cls.jobs:
            job = cls.jobs.pop(0)
            job.target(*job.args)


class NativeNotifyDeliveriesTests(unittest.TestCase):
    def seed(self):
        from fls_manager.config import save_config
        save_config({"notify_items": [{"id": "n", "name": "Test", "channel": "webhook", "enabled": True,
                                      "config": {"WEBHOOK_URL": "https://private.example/secret", "WEBHOOK_BODY": "secret-body"}}],
                     "notify_default_ids": ["n"]})

    def data(self, request_id="a" * 32):
        from fls_manager.notify_actions import notification_config, notification_version
        return dict(request_id=request_id, item_id="n", item_version=notification_version(notification_config(), "n"))

    def get(self, client, path):
        return client.get("/api/notify/" + path, headers={"X-Token": TOKEN})

    def post(self, client, path, data):
        return client.post("/api/notify/" + path, json=data, headers={"X-Token": TOKEN})

    def start(self, app, data=None, **kwargs):
        from fls_manager import notify_deliveries as service
        return service.start_delivery(data or self.data(), app.secret_key, thread_factory=HeldThread, **kwargs)

    def test_api_session_csrf_strict_json_and_readonly_empty(self):
        with isolated_app() as (app, base):
            self.seed()
            client = app.test_client()
            self.assertEqual(client.get("/api/notify/deliveries").status_code, 401)
            login = client.post("/api/auth/session", json={"token": TOKEN})
            self.assertEqual(client.post("/api/notify/deliveries/test", json=self.data()).status_code, 400)
            csrf = {"X-CSRF-Token": login.json["csrf_token"]}
            for body in ({}, dict(self.data(), secret="bad"), dict(self.data(), item_version="bad")):
                rejected = client.post("/api/notify/deliveries/test", json=body, headers=csrf)
                self.assertEqual(rejected.status_code, 400)
                self.assertFalse(rejected.json["accepted"])
            self.assertEqual(client.post("/api/notify/deliveries/test", data='{"a":1,"a":2}', content_type="application/json", headers=csrf).status_code, 400)
            self.assertEqual(client.post("/api/notify/deliveries/test", data="x" * 4097, content_type="application/json", headers=csrf).status_code, 413)
            for query in ("?page=0", "?status=other", "?limit=101", "?page=1&page=2", "?secret=1"):
                self.assertEqual(self.get(client, "deliveries" + query).status_code, 400)
            result = self.get(client, "deliveries")
            self.assertEqual(result.json["items"], [])
            self.assertEqual(result.headers["Cache-Control"], "no-store")
            self.assertFalse((base / "data/native_notify_deliveries").exists())

    def test_persist_before_send_idempotence_and_no_secrets_in_receipt(self):
        with isolated_app() as (app, base):
            from fls_manager import notify_deliveries as service
            from fls_manager.notify_transport import SendResult
            self.seed()
            data = self.data()
            sender = Mock(return_value=SendResult("succeeded", "accepted"))
            result = self.start(app, data, sender=sender)
            try:
                sender.assert_not_called()
                self.assertTrue(result["active"])
                self.assertEqual(self.start(app, data, sender=sender)["delivery_id"], result["delivery_id"])
                with self.assertRaises(service.DeliveryError):
                    self.start(app, dict(data, item_version="b" * 64), sender=sender)
                raw = (base / "data/native_notify_deliveries" / (data["request_id"] + ".json")).read_text()
                self.assertNotIn("private.example", raw)
                self.assertNotIn("secret-body", raw)
                self.assertNotIn("这是一条", raw)
            finally:
                HeldThread.finish()
            sender.assert_called_once()
            result = service.request_status(data["request_id"], app.secret_key)
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue(result["current_matches"])
            self.start(app, data, sender=sender)
            sender.assert_called_once()

    def test_api_start_and_retry_route_share_current_versions_and_original_query(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_deliveries as service
            from fls_manager.notify_transport import SendResult
            self.seed()
            client, original = app.test_client(), service.start_delivery
            sender = Mock(return_value=SendResult("unknown", "timeout"))
            def start(data, key, **kwargs):
                return original(data, key, sender=sender, thread_factory=HeldThread, **kwargs)
            with patch.object(service, "start_delivery", side_effect=start):
                first = self.post(client, "deliveries/test", self.data())
                self.assertEqual(first.status_code, 200, first.json)
                delivery_id = first.json["request"]["delivery_id"]
                HeldThread.finish()
                detail = self.get(client, "deliveries/" + delivery_id).json["item"]
                data = dict(self.data("b" * 32), expected_attempt=1, version=detail["version"], confirm_truncated=False)
                retry = self.post(client, "deliveries/" + delivery_id + "/retry", data)
                self.assertEqual(retry.status_code, 200, retry.json)
                HeldThread.finish()
                self.assertEqual(self.get(client, "delivery-requests/" + "b" * 32).json["request"]["attempt"], 2)
                self.assertEqual(self.get(client, "deliveries?status=unknown&channel=webhook").json["total"], 1)
                self.assertEqual(sender.call_count, 2)

    def test_completed_receipt_never_reports_active_during_worker_cleanup(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_deliveries as service
            from fls_manager.notify_transport import SendResult
            self.seed()
            self.start(app, sender=Mock(return_value=SendResult("succeeded", "accepted")))
            try:
                record = service._read("a" * 32, app.secret_key)
                record.update(state="completed", status="succeeded", code="accepted")
                service._save(record, app.secret_key)
                self.assertFalse(service.request_status("a" * 32, app.secret_key)["active"])
            finally:
                HeldThread.finish()

    def test_queries_never_send_claim_or_write_and_restart_is_unknown(self):
        with isolated_app() as (app, base):
            from fls_manager import notify_deliveries as service, notify
            from fls_manager.notify_transport import SendResult
            self.seed()
            result = self.start(app, sender=Mock(return_value=SendResult("unknown", "timeout")))
            HeldThread.finish()
            client = app.test_client()
            before = {p: p.read_bytes() for p in (base / "data").rglob("*.json")}
            with patch.object(service, "write_json", side_effect=AssertionError("GET wrote")), patch.object(notify, "send_one", side_effect=AssertionError("GET sent")):
                self.assertEqual(self.get(client, "delivery-requests/" + "a" * 32).json["request"]["status"], "unknown")
                self.assertEqual(self.get(client, "deliveries").json["total"], 1)
                self.assertEqual(self.get(client, "deliveries/" + result["delivery_id"]).status_code, 200)
            self.assertEqual(before, {p: p.read_bytes() for p in (base / "data").rglob("*.json")})
            record = service._read("a" * 32, app.secret_key)
            record.update(state="running", status="running", code="")
            service._save(record, app.secret_key)
            self.assertEqual(service.request_status("a" * 32, app.secret_key)["status"], "unknown")
            sender = Mock()
            self.start(app, sender=sender)
            sender.assert_not_called()

    def test_retry_same_delivery_attempt_versions_and_historical_receipt(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_deliveries as service
            from fls_manager.notify_transport import SendResult
            self.seed()
            first = self.start(app, sender=Mock(return_value=SendResult("unknown", "timeout")))
            HeldThread.finish()
            delivery_id = first["delivery_id"]
            row = service.delivery_detail(delivery_id)
            data = dict(self.data("b" * 32), version=row["version"], expected_attempt=1, confirm_truncated=False)
            with self.assertRaises(service.DeliveryError):
                self.start(app, dict(data, item_version="f" * 64), delivery_id=delivery_id)
            sender = Mock(return_value=SendResult("succeeded", "accepted"))
            retried = self.start(app, data, delivery_id=delivery_id, sender=sender)
            try:
                self.assertEqual(retried["delivery_id"], delivery_id)
                self.assertEqual(retried["attempt"], 2)
                self.assertEqual(service.request_status("a" * 32, app.secret_key)["status"], "unknown")
                self.assertFalse(service.request_status("a" * 32, app.secret_key)["current_matches"])
                with self.assertRaises(service.DeliveryError):
                    self.start(app, dict(data, request_id="c" * 32), delivery_id=delivery_id)
            finally:
                HeldThread.finish()
            sender.assert_called_once()
            self.assertEqual(service.list_deliveries()["total"], 1)
            self.assertEqual(service.delivery_detail(delivery_id)["status"], "succeeded")

    def test_thread_start_failure_and_capacity_never_send(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_deliveries as service
            from fls_manager.notify_transport import SendResult
            self.seed()
            sender = Mock(return_value=SendResult("succeeded", "accepted"))
            factory = Mock(return_value=Mock(start=Mock(side_effect=RuntimeError("thread failed"))))
            with self.assertRaises(RuntimeError):
                service.start_delivery(self.data(), app.secret_key, sender=sender, thread_factory=factory)
            sender.assert_not_called()
            self.assertEqual(service.request_status("a" * 32, app.secret_key)["code"], "start_failed")
            try:
                for char in "bcd":
                    self.start(app, self.data(char * 32), sender=sender)
                with self.assertRaises(service.DeliveryError) as caught:
                    self.start(app, self.data("e" * 32), sender=sender)
                self.assertEqual(caught.exception.status, 429)
                self.assertEqual(service.request_status("e" * 32, app.secret_key)["status"], "missing")
            finally:
                HeldThread.finish()
            self.assertEqual(sender.call_count, 3)

    def test_storage_failure_unknown_and_corrupted_receipt_blocks_replay(self):
        with isolated_app() as (app, base):
            from fls_manager import notify_deliveries as service
            from fls_manager.notify_transport import SendResult
            self.seed()
            sender = Mock(return_value=SendResult("succeeded", "accepted"))
            original = service._save
            def save(record, key):
                if record["state"] == "running":
                    raise OSError("full")
                original(record, key)
            with patch.object(service, "_save", side_effect=save), self.assertRaises(OSError):
                self.start(app, sender=sender)
            sender.assert_not_called()
            self.assertEqual(service.request_status("a" * 32, app.secret_key)["status"], "unknown")
            self.start(app, sender=sender)
            sender.assert_not_called()
            receipt = base / "data/native_notify_deliveries" / ("a" * 32 + ".json")
            receipt.write_text('{"request_id":"bad"}')
            self.assertEqual(self.get(app.test_client(), "delivery-requests/" + "a" * 32).status_code, 503)
            with self.assertRaises(RuntimeError):
                self.start(app, sender=sender)

    def test_completion_write_failure_and_cleaned_history_keep_result_boundaries(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_deliveries as service, notify
            from fls_manager.notify_transport import SendResult
            self.seed()
            self.start(app, sender=Mock(return_value=SendResult("succeeded", "accepted")))
            with patch.object(service, "write_json", side_effect=OSError("disk")):
                HeldThread.finish()
            self.assertEqual(service.request_status("a" * 32, app.secret_key)["status"], "unknown")
            self.start(app, self.data("b" * 32), sender=Mock(return_value=SendResult("succeeded", "accepted")))
            HeldThread.finish()
            service.write_json(notify.NOTIFY_HISTORY_FILE, [])
            result = service.request_status("b" * 32, app.secret_key)
            self.assertEqual(result["status"], "succeeded")
            self.assertFalse(result["history_available"])
            self.assertFalse(result["current_matches"])

    def test_body_reveal_redaction_and_truncated_retry_confirmation(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_deliveries as service, notify
            from fls_manager.notify_transport import SendResult
            self.seed()
            first = self.start(app, sender=Mock(return_value=SendResult("unknown", "network_error")))
            HeldThread.finish()
            rows = service._history()
            rows[0].update(title="secret-body", content="secret-body https://old.example/private a@b.example", content_truncated=True, message="private-response")
            service.write_json(notify.NOTIFY_HISTORY_FILE, rows)
            client = app.test_client()
            detail = self.get(client, "deliveries/" + first["delivery_id"])
            self.assertNotIn("content", detail.json["item"])
            revealed = self.get(client, "deliveries/" + first["delivery_id"] + "?reveal=1")
            for secret in ("secret-body", "old.example", "a@b.example", "private-response"):
                self.assertNotIn(secret, revealed.get_data(as_text=True))
            data = dict(self.data("b" * 32), version=detail.json["item"]["version"], expected_attempt=1, confirm_truncated=False)
            with self.assertRaises(service.DeliveryError):
                self.start(app, data, delivery_id=first["delivery_id"])
            self.start(app, dict(data, confirm_truncated=True), delivery_id=first["delivery_id"], sender=Mock(return_value=SendResult("succeeded", "accepted")))
            HeldThread.finish()

    def test_changed_persisted_claim_never_starts_sender(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_deliveries as service, notify
            from fls_manager.notify_transport import SendResult
            self.seed()
            sender = Mock(return_value=SendResult("succeeded", "accepted"))
            self.start(app, sender=sender)
            rows = service._history()
            rows[0]["content"] = "changed"
            service.write_json(notify.NOTIFY_HISTORY_FILE, rows)
            HeldThread.finish()
            sender.assert_not_called()
            self.assertEqual(service.request_status("a" * 32, app.secret_key)["status"], "unknown")

    def test_network_runs_outside_storage_and_history_locks(self):
        with isolated_app() as (app, _):
            from fls_manager import notify_deliveries as service, notify
            from fls_manager.notify_transport import SendResult
            from fls_manager.storage import storage_transaction
            self.seed()
            done, entered, release = threading.Event(), threading.Event(), threading.Event()
            threads = []
            def factory(**kwargs):
                thread = threading.Thread(**kwargs)
                threads.append(thread)
                return thread
            def sender(*args):
                entered.set()
                release.wait(5)
                return SendResult("succeeded", "accepted")
            result = service.start_delivery(self.data(), app.secret_key, sender=sender, thread_factory=factory)
            try:
                self.assertTrue(entered.wait(3))
                def reader():
                    with notify.NOTIFY_HISTORY_LOCK, storage_transaction():
                        done.set()
                reader_thread = threading.Thread(target=reader, daemon=True)
                reader_thread.start()
                self.assertTrue(done.wait(2))
                reader_thread.join(2)
                self.assertEqual(service.request_status(result["request_id"], app.secret_key)["status"], "running")
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)


class NativeNotifyTransportTests(unittest.TestCase):
    def response(self, body=b'{"code":200}', status=200):
        return Mock(status_code=status, headers={}, iter_content=Mock(return_value=[body]))

    def test_all_http_channels_have_explicit_acceptance_and_close_response(self):
        from fls_manager.notify_transport import send_native
        cases = {
            "bark": ({"BARK_PUSH": "device"}, {"code": 200}),
            "serverj": ({"PUSH_KEY": "key"}, {"code": 0}),
            "pushplus": ({"PUSH_PLUS_TOKEN": "token"}, {"code": 200}),
            "telegram": ({"TG_BOT_TOKEN": "token", "TG_USER_ID": "id"}, {"ok": True}),
            "qywxbot": ({"QYWX_KEY": "key"}, {"errcode": 0}),
            "dingding": ({"DD_BOT_TOKEN": "token", "DD_BOT_SECRET": "secret"}, {"errcode": 0}),
            "feishu": ({"FSKEY": "key"}, {"code": 0}),
            "ntfy": ({"NTFY_URL": "https://host", "NTFY_TOPIC": "topic"}, {}),
            "wxpusher": ({"WXPUSHER_APP_TOKEN": "token", "WXPUSHER_UIDS": "id"}, {"code": 1000}),
            "gotify": ({"GOTIFY_URL": "https://host", "GOTIFY_TOKEN": "token"}, {"id": 1}),
            "pushdeer": ({"DEER_KEY": "key"}, {"code": 0, "content": {"result": ["ok"]}}),
            "webhook": ({"WEBHOOK_URL": "https://host"}, {}),
        }
        for channel, (config, body) in cases.items():
            with self.subTest(channel=channel):
                response = self.response(json.dumps(body).encode())
                client = Mock(request=Mock(return_value=response))
                result = send_native(dict(channel=channel, config=config), "title", "body", client=client)
                self.assertEqual(result.status, "succeeded")
                kwargs = client.request.call_args.kwargs
                self.assertFalse(kwargs["allow_redirects"])
                self.assertTrue(kwargs["stream"])
                response.close.assert_called_once()

    def test_response_limits_redirect_malformed_and_slow_data_stay_unknown(self):
        from fls_manager.notify_transport import send_native, MAX_RESPONSE
        item = dict(channel="dingding", config={"DD_BOT_TOKEN": "token", "DD_BOT_SECRET": "secret"})
        for response, code in ((self.response(b"{}"), "upstream_rejected"),
                               (self.response(b"garbage"), "invalid_response"),
                               (self.response(b"[]"), "invalid_response"),
                               (self.response(b"x" * (MAX_RESPONSE + 1)), "response_limit"),
                               (self.response(status=302), "redirect")):
            with self.subTest(code=code):
                result = send_native(item, "t", "c", client=Mock(request=Mock(return_value=response)))
                self.assertEqual((result.status, result.code), ("unknown", code))
                response.close.assert_called_once()
        result = send_native(item, "t", "c", client=Mock(request=Mock(return_value=self.response())), clock=Mock(side_effect=[0, 31]))
        self.assertEqual(result.code, "timeout")

    def test_smtp_cleanup_and_acceptance_even_when_close_fails(self):
        from fls_manager.notify_transport import send_native
        item = dict(channel="smtp", config={"SMTP_SERVER": "host", "SMTP_EMAIL": "a@b", "SMTP_PASSWORD": "secret"})
        conn = Mock(sendmail=Mock(return_value={}), close=Mock(side_effect=OSError("close")))
        module = Mock(SMTP=Mock(return_value=conn))
        self.assertEqual(send_native(item, "t", "c", smtp_module=module).status, "succeeded")
        conn.close.assert_called_once()
        conn = Mock(login=Mock(side_effect=OSError("private-password")))
        result = send_native(item, "t", "c", smtp_module=Mock(SMTP=Mock(return_value=conn)))
        self.assertEqual(result.status, "unknown")
        self.assertNotIn("private-password", str(result))
        conn.close.assert_called_once()

    def test_missing_config_does_not_touch_network(self):
        from fls_manager.notify_transport import send_native
        client = Mock()
        self.assertEqual(send_native(dict(channel="bark", config={}), "t", "c", client=client).status, "failed")
        client.request.assert_not_called()
