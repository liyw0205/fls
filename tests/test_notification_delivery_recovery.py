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


if __name__ == "__main__":
    unittest.main()
