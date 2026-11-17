import json
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineRefreshStorageRecoveryTests(unittest.TestCase):
    def test_corrupt_refresh_state_is_reported_and_preserved(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_REFRESH_STATE_FILE

            ONLINE_REFRESH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            raw = b'{"id":'
            ONLINE_REFRESH_STATE_FILE.write_bytes(raw)
            client = app.test_client()

            status = client.get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN})
            self.assertEqual(status.status_code, 500)
            self.assertIn("未覆盖原文件", status.json["error"])
            self.assertEqual(ONLINE_REFRESH_STATE_FILE.read_bytes(), raw)

            with patch("fls_manager.routes.online_scripts.refresh.start_refresh_thread") as start:
                failed = client.post(
                    "/online-scripts/refresh",
                    data={"proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed.status_code, 500)
            self.assertIn("未覆盖原文件", failed.get_data(as_text=True))
            start.assert_not_called()
            self.assertEqual(ONLINE_REFRESH_STATE_FILE.read_bytes(), raw)

            page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("未覆盖原文件", page.get_data(as_text=True))
            self.assertEqual(ONLINE_REFRESH_STATE_FILE.read_bytes(), raw)

    def test_repaired_refresh_state_can_start_and_running_worker_is_not_marked_unknown(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_REFRESH_STATE_FILE

            ONLINE_REFRESH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_REFRESH_STATE_FILE.write_text(json.dumps({}), encoding="utf-8")
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.refresh.start_refresh_thread") as start:
                response = client.post("/api/online-scripts/refresh", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 202)
                self.assertTrue(start.called)
                running = client.get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN})

            self.assertEqual(running.status_code, 200)
            self.assertTrue(running.json["running"])
            self.assertEqual(running.json["status"], "运行中")

            repaired = client.get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN})
            self.assertTrue(repaired.json["running"])


if __name__ == "__main__":
    unittest.main()
