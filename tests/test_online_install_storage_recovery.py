import json
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineInstallStorageRecoveryTests(unittest.TestCase):
    def seed_cache(self):
        from fls_manager.online_scripts.source import save_online_script_cache

        save_online_script_cache([{
            "id": "demo",
            "name": "Demo",
            "type": "raw",
            "link": "https://example.invalid/demo.sh",
            "link_name": "demo.sh",
        }])

    def test_corrupt_history_is_visible_and_never_overwritten(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            self.seed_cache()
            raw = b'{"broken":'
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_bytes(raw)
            client = app.test_client()

            api = client.get("/api/online-scripts/log/missing", headers={"X-Token": TOKEN})
            self.assertEqual(api.status_code, 500)
            self.assertIn("未覆盖原文件", api.json["error"])
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), raw)

            page = client.get("/online-scripts/log/missing", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 500)
            html = page.get_data(as_text=True)
            self.assertIn("安装记录无法读取", html)
            self.assertIn('href="/logs"', html)
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), raw)

            listing = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(listing.status_code, 200)
            self.assertIn("安装记录无法读取", listing.get_data(as_text=True))
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), raw)

    def test_corrupt_history_blocks_retry_and_scheduler_recovery_without_worker(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            self.seed_cache()
            raw = b"[]"
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_bytes(raw)
            client = app.test_client()

            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start, \
                 patch("fls_manager.routes.online_scripts.install.reload_scheduler") as reload_scheduler:
                retry = client.post(
                    "/online-scripts/install-retry/known",
                    headers={"X-Token": TOKEN},
                )
                reload = client.post(
                    "/online-scripts/install-reload/known",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(retry.status_code, 500)
            self.assertEqual(reload.status_code, 500)
            self.assertIn("未覆盖原文件", retry.get_data(as_text=True))
            self.assertIn("未覆盖原文件", reload.get_data(as_text=True))
            start.assert_not_called()
            reload_scheduler.assert_not_called()
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), raw)

    def test_new_install_does_not_replace_corrupt_history(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            self.seed_cache()
            raw = b'{"old":{"id":"different"}}'
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_bytes(raw)
            client = app.test_client()

            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = client.post(
                    "/online-scripts/install/demo",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 500)
            self.assertIn("保存安装状态失败", response.get_data(as_text=True))
            start.assert_not_called()
            self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), raw)

    def test_valid_history_can_resume_after_record_is_repaired(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            self.seed_cache()
            raw = b"{"
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_bytes(raw)
            client = app.test_client()
            self.assertEqual(
                client.get("/api/online-scripts/log/missing", headers={"X-Token": TOKEN}).status_code,
                500,
            )

            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps({}), encoding="utf-8")
            response = client.get("/api/online-scripts/log/missing", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 404)
            self.assertIn("记录不存在", response.json["error"])


if __name__ == "__main__":
    unittest.main()
