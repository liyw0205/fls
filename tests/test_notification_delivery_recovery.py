import contextlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "notify-delivery-token"


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def shutdown_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None
    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


@contextlib.contextmanager
def isolated_modules():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix="fls-notify-delivery-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "notify-delivery-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        try:
            yield Path(temp_dir)
        finally:
            shutdown_scheduler()
            purge_fls_modules()
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class NotificationDeliveryRecoveryTests(unittest.TestCase):
    def test_send_by_ids_persists_each_delivery_result(self):
        with isolated_modules() as base_dir:
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {
                    "notify_items": [
                        {"id": "ok", "name": "OK", "channel": "bark", "enabled": True, "config": {}},
                        {"id": "bad", "name": "Bad", "channel": "bark", "enabled": True, "config": {}},
                    ],
                },
            )
            with patch.object(
                notify,
                "send_one",
                side_effect=[(True, "accepted"), (False, "remote rejected")],
            ):
                result = notify.send_by_ids("Task", "output", ["ok", "bad"])

            self.assertEqual([item["ok"] for item in result], [True, False])
            history = read_json(base_dir / "data" / "notify_history.json")
            self.assertEqual([item["status"] for item in history], ["failed", "succeeded"])
            self.assertEqual(history[0]["source"], "task")
            self.assertEqual(history[0]["message"], "remote rejected")
            self.assertEqual(history[1]["content"], "output")

    def test_send_by_ids_reports_deleted_and_disabled_task_channels(self):
        with isolated_modules():
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {
                    "notify_items": [
                        {"id": "enabled", "name": "Enabled", "channel": "bark", "enabled": True, "config": {}},
                        {"id": "disabled", "name": "Disabled", "channel": "bark", "enabled": False, "config": {}},
                    ],
                    "notify_default_ids": ["deleted", "disabled"],
                },
            )
            with patch.object(notify, "send_one", return_value=(True, "accepted")) as sender:
                unavailable = notify.send_by_ids("Task", "Body", ["deleted", "disabled"])
                self.assertEqual([row["status"] for row in unavailable], ["not_sent", "not_sent"])
                self.assertIn("不存在", unavailable[0]["msg"])
                self.assertIn("停用", unavailable[1]["msg"])
                sender.assert_not_called()

                mixed = notify.send_by_ids("Task", "Body", ["enabled", "deleted", "disabled"])
                defaults = notify.send_by_ids("Task", "Body", ["__default__"])

            self.assertEqual(sender.call_count, 1)
            self.assertEqual([row["id"] for row in mixed], ["deleted", "disabled", "enabled"])
            self.assertEqual([row["status"] for row in mixed], ["not_sent", "not_sent", "succeeded"])
            self.assertEqual([row["status"] for row in defaults], ["not_sent", "not_sent"])

    def test_send_by_ids_rejects_corrupt_config_without_rewriting_or_sending(self):
        corrupt_configs = (
            b'{"notify_items":[{"id":"n1","channel":"webhook","config":{"TOKEN":"private-secret"}}',
            b"[]",
            b'{"notify_items":[{"id":"n1","channel":"invalid","config":{}}]}',
            b'{"notify_items":[{"id":"n1","channel":"bark"},{"id":"n1","channel":"bark"}]}'
        )
        for corrupt in corrupt_configs:
            with self.subTest(corrupt=corrupt[:20]), isolated_modules():
                from fls_manager import notify, paths

                paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
                paths.CONFIG_FILE.write_bytes(corrupt)

                with patch.object(notify, "send_one") as sender:
                    result = notify.send_by_ids("Task", "Body", ["n1"])

                self.assertEqual(len(result), 1)
                self.assertFalse(result[0]["ok"])
                self.assertEqual(result[0]["status"], "not_sent")
                self.assertIn("配置", result[0]["msg"])
                self.assertNotIn("private-secret", result[0]["msg"])
                sender.assert_not_called()
                self.assertEqual(paths.CONFIG_FILE.read_bytes(), corrupt)

    def test_send_success_is_unknown_when_terminal_history_write_fails(self):
        with isolated_modules() as base_dir:
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {"notify_items": [{"id": "n1", "name": "One", "channel": "bark",
                                   "enabled": True, "config": {}}]},
            )
            with patch.object(notify, "send_one", return_value=(True, "remote accepted")), \
                 patch.object(notify, "finish_notify_delivery", side_effect=OSError("history disk full")):
                result = notify.send_by_ids("Task", "output", ["n1"])

            self.assertEqual(result[0]["status"], "unknown")
            self.assertFalse(result[0]["ok"])
            self.assertIn("无法确认", result[0]["msg"])
            self.assertEqual(notify.load_notify_history()[0]["status"], "unknown")
            self.assertIn("无法确认", read_json(base_dir / "data" / "notify_history.json")[0]["message"])

    def test_terminal_history_is_used_when_finish_call_reports_failure(self):
        with isolated_modules():
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {"notify_items": [{"id": "n1", "name": "One", "channel": "bark",
                                   "enabled": True, "config": {}}]},
            )
            original_finish = notify.finish_notify_delivery

            def finish_then_report_failure(delivery_id, ok, message):
                original_finish(delivery_id, ok, message)
                return False

            with patch.object(notify, "send_one", return_value=(True, "remote accepted")), \
                 patch.object(notify, "finish_notify_delivery", side_effect=finish_then_report_failure):
                result = notify.send_by_ids("Task", "output", ["n1"])

            self.assertTrue(result[0]["ok"])
            self.assertEqual(result[0]["status"], "succeeded")
            self.assertIn("已按历史记录确认", result[0]["msg"])
            self.assertEqual(notify.load_notify_history()[0]["status"], "succeeded")

    def test_failed_send_with_terminal_write_failure_stays_unknown_not_failed(self):
        with isolated_modules():
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {"notify_items": [{"id": "n1", "name": "One", "channel": "bark",
                                   "enabled": True, "config": {}}]},
            )
            with patch.object(notify, "send_one", return_value=(False, "remote rejected")), \
                 patch.object(notify, "finish_notify_delivery", return_value=False):
                result = notify.send_by_ids("Task", "output", ["n1"])

            self.assertFalse(result[0]["ok"])
            self.assertEqual(result[0]["status"], "unknown")
            self.assertIn("无法确认", result[0]["msg"])

    def test_running_rows_recover_to_unknown_and_retry_is_persisted(self):
        with isolated_modules() as base_dir:
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {
                    "notify_items": [
                        {"id": "n1", "name": "One", "channel": "bark", "enabled": True, "config": {}},
                    ],
                },
            )
            write_json(
                paths.NOTIFY_HISTORY_FILE,
                [{
                    "id": "delivery-1",
                    "item_id": "n1",
                    "item_name": "One",
                    "channel": "bark",
                    "title": "Title",
                    "content": "Body",
                    "status": "running",
                    "attempt": 1,
                    "message": "发送中",
                }],
            )

            recovered = notify.load_notify_history()
            self.assertEqual(recovered[0]["status"], "unknown")
            self.assertIn("无法确认", recovered[0]["message"])
            self.assertEqual(notify.load_notify_history()[0]["status"], "unknown")

            with patch.object(notify, "send_one", return_value=(True, "retried")):
                ok, msg, row = notify.retry_notify_delivery("delivery-1")

            self.assertTrue(ok)
            self.assertEqual(msg, "retried")
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(row["attempt"], 2)
            self.assertEqual(read_json(base_dir / "data" / "notify_history.json")[0]["status"], "succeeded")

    def test_history_cache_reuses_stable_file_and_returns_copies(self):
        with isolated_modules() as base_dir:
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            row = {
                "id": "delivery-cache",
                "item_id": "n1",
                "item_name": "One",
                "channel": "bark",
                "title": "Title",
                "content": "Body",
                "status": "failed",
                "attempt": 1,
                "message": "failed",
                "created_at": "2026-11-16 00:00:00",
                "updated_at": "2026-11-16 00:00:00",
            }
            write_json(paths.NOTIFY_HISTORY_FILE, [row])

            with patch.object(notify, "read_notify_history_rows", wraps=notify.read_notify_history_rows) as read:
                first = notify.load_notify_history()
                second = notify.load_notify_history()

            self.assertEqual(read.call_count, 1)
            self.assertEqual(first, second)
            first[0]["status"] = "succeeded"
            self.assertEqual(second[0]["status"], "failed")

            write_json(paths.NOTIFY_HISTORY_FILE, [{**row, "status": "succeeded"}])
            refreshed = notify.load_notify_history()
            self.assertEqual(refreshed[0]["status"], "succeeded")

    def test_history_cache_tracks_active_delivery_and_restart_recovery(self):
        with isolated_modules() as base_dir:
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            row = {
                "id": "delivery-running",
                "item_id": "n1",
                "item_name": "One",
                "channel": "bark",
                "title": "Title",
                "content": "Body",
                "status": "running",
                "attempt": 1,
                "message": "发送中",
            }
            write_json(paths.NOTIFY_HISTORY_FILE, [row])
            notify.NOTIFY_ACTIVE_DELIVERIES.add(row["id"])
            try:
                self.assertEqual(notify.load_notify_history()[0]["status"], "running")
            finally:
                notify.NOTIFY_ACTIVE_DELIVERIES.clear()

            recovered = notify.load_notify_history()
            self.assertEqual(recovered[0]["status"], "unknown")
            self.assertIn("无法确认", recovered[0]["message"])
            self.assertEqual(read_json(base_dir / "data" / "notify_history.json")[0]["status"], "unknown")

    def test_new_delivery_retains_running_and_unknown_rows_when_pruning_history(self):
        with isolated_modules() as base_dir:
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            notify.NOTIFY_HISTORY_LIMIT = 4
            rows = [
                {"id": "done-new", "status": "succeeded"},
                {"id": "done-middle", "status": "failed"},
                {"id": "delivery-unknown", "status": "unknown"},
                {"id": "delivery-running", "status": "running"},
            ]
            write_json(paths.NOTIFY_HISTORY_FILE, rows)
            notify.NOTIFY_ACTIVE_DELIVERIES.add("delivery-running")

            notify.create_notify_delivery(
                {"id": "n1", "name": "One", "channel": "bark"}, "Title", "Body"
            )

            persisted = read_json(base_dir / "data" / "notify_history.json")
            persisted_ids = {row["id"] for row in persisted}
            self.assertEqual(len(persisted), notify.NOTIFY_HISTORY_LIMIT)
            self.assertIn("delivery-unknown", persisted_ids)
            self.assertIn("delivery-running", persisted_ids)
            self.assertEqual(sum(row["status"] == "running" for row in persisted), 2)

    def test_full_recoverable_history_rejects_delivery_before_external_send(self):
        with isolated_modules() as base_dir:
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            notify.NOTIFY_HISTORY_LIMIT = 2
            write_json(
                paths.CONFIG_FILE,
                {"notify_items": [{"id": "n1", "name": "One", "channel": "bark",
                                   "enabled": True, "config": {}}]},
            )
            history = [
                {"id": "delivery-unknown", "status": "unknown"},
                {"id": "delivery-running", "status": "running"},
            ]
            write_json(paths.NOTIFY_HISTORY_FILE, history)
            notify.NOTIFY_ACTIVE_DELIVERIES.add("delivery-running")
            original = paths.NOTIFY_HISTORY_FILE.read_bytes()

            with patch.object(notify, "send_one", return_value=(True, "sent")) as sender:
                result = notify.send_by_ids("Task", "Body", ["n1"])

            self.assertEqual(len(result), 1)
            self.assertFalse(result[0]["ok"])
            self.assertIn("待核对通知记录达到上限", result[0]["msg"])
            sender.assert_not_called()
            self.assertEqual(paths.NOTIFY_HISTORY_FILE.read_bytes(), original)

    def test_concurrent_retry_allows_one_claim(self):
        with isolated_modules():
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {
                    "notify_items": [
                        {"id": "n1", "name": "One", "channel": "bark", "enabled": True, "config": {}},
                    ],
                },
            )
            write_json(
                paths.NOTIFY_HISTORY_FILE,
                [{
                    "id": "delivery-1", "item_id": "n1", "item_name": "One", "channel": "bark",
                    "title": "Title", "content": "Body", "status": "failed", "attempt": 1,
                    "message": "failed",
                }],
            )
            started = threading.Event()
            release = threading.Event()

            def send(_item, _title, _content):
                started.set()
                release.wait(2)
                return True, "accepted"

            results = []
            with patch.object(notify, "send_one", side_effect=send):
                first = threading.Thread(target=lambda: results.append(notify.retry_notify_delivery("delivery-1")))
                first.start()
                self.assertTrue(started.wait(1))
                second = notify.retry_notify_delivery("delivery-1")
                release.set()
                first.join(2)

            self.assertEqual(second[0], False)
            self.assertEqual(second[1], "通知投递正在发送")
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0][0])

    def test_api_and_page_expose_same_history_and_retry_status(self):
        with isolated_modules() as base_dir:
            from fls_manager.app import create_app
            from fls_manager import paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {
                    "notify_items": [
                        {"id": "n1", "name": "One", "channel": "bark", "enabled": True, "config": {}},
                    ],
                },
            )
            write_json(
                paths.NOTIFY_HISTORY_FILE,
                [{
                    "id": "delivery-1", "item_id": "n1", "item_name": "One", "channel": "bark",
                    "title": "Title", "content": "Body", "status": "failed", "attempt": 1,
                    "message": "remote rejected",
                }],
            )
            app = create_app()
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            api = client.get("/api/notify/history?status=failed&limit=10", headers=headers)
            page = client.get("/notify/history?status=failed&limit=10", headers=headers)
            self.assertEqual(api.status_code, 200)
            self.assertEqual(api.get_json()["items"][0]["id"], "delivery-1")
            self.assertIn("remote rejected", page.get_data(as_text=True))
            self.assertIn("/notify/retry/delivery-1", page.get_data(as_text=True))

            with patch("fls_manager.notify.send_one", return_value=(True, "accepted")):
                retried = client.post("/api/notify/retry/delivery-1", headers=headers)
            self.assertEqual(retried.status_code, 200)
            self.assertTrue(retried.get_json()["ok"])
            conflict = client.post("/api/notify/retry/delivery-1", headers=headers)
            self.assertEqual(conflict.status_code, 409)
            missing = client.post("/api/notify/retry/missing", headers=headers)
            self.assertEqual(missing.status_code, 404)

    def test_page_retry_conflict_returns_recoverable_html_error(self):
        with isolated_modules() as base_dir:
            from fls_manager import notify, paths
            from fls_manager.storage import write_json

            write_json(
                paths.CONFIG_FILE,
                {
                    "notify_items": [
                        {"id": "n1", "name": "One", "channel": "bark", "enabled": True, "config": {}},
                    ],
                },
            )
            write_json(
                paths.NOTIFY_HISTORY_FILE,
                [{
                    "id": "delivery-running",
                    "item_id": "n1",
                    "item_name": "One",
                    "channel": "bark",
                    "title": "Title",
                    "content": "Body",
                    "status": "running",
                    "attempt": 1,
                    "message": "发送中",
                }],
            )
            notify.NOTIFY_ACTIVE_DELIVERIES.add("delivery-running")

            try:
                from fls_manager.app import create_app

                client = create_app().test_client()
                response = client.post(
                    "/notify/retry/delivery-running",
                    headers={"X-Token": TOKEN},
                )
            finally:
                notify.NOTIFY_ACTIVE_DELIVERIES.discard("delivery-running")

            self.assertEqual(response.status_code, 409)
            self.assertIn("通知投递正在发送", response.get_data(as_text=True))
            self.assertIn("text/html", response.content_type)

    def test_retry_api_requires_authentication_before_delivery_claim(self):
        with isolated_modules():
            from fls_manager.app import create_app
            from fls_manager import notify

            client = create_app().test_client()
            with client.session_transaction() as session:
                session["csrf_token"] = "notify-csrf"

            with patch.object(notify, "retry_notify_delivery") as retry:
                response = client.post(
                    "/api/notify/retry/delivery-1",
                    headers={"X-CSRF-Token": "notify-csrf"},
                )

            self.assertEqual(response.status_code, 401)
            self.assertFalse(response.get_json()["ok"])
            retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
