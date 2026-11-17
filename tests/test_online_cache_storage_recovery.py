import json
import os
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineCacheStorageRecoveryTests(unittest.TestCase):
    def seed_cache(self):
        from fls_manager.online_scripts.source import save_online_script_cache

        save_online_script_cache([{
            "id": "demo",
            "name": "Demo",
            "type": "raw",
            "link": "https://example.invalid/demo.sh",
            "link_name": "demo.sh",
            "task_link": "https://example.invalid/tasks.json",
        }])

    def test_corrupt_cache_is_visible_in_list_and_source_editor_without_overwrite(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_SCRIPT_CACHE_FILE

            self.seed_cache()
            raw = b'{"not": "an array"}'
            ONLINE_SCRIPT_CACHE_FILE.write_bytes(raw)
            client = app.test_client()

            page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn("缓存无法读取", html)
            self.assertIn('href="/online-scripts/source"', html)
            self.assertEqual(ONLINE_SCRIPT_CACHE_FILE.read_bytes(), raw)

            editor = client.get("/online-scripts/source", headers={"X-Token": TOKEN})
            self.assertEqual(editor.status_code, 200)
            self.assertIn("an array", editor.get_data(as_text=True))
            self.assertEqual(ONLINE_SCRIPT_CACHE_FILE.read_bytes(), raw)

    def test_corrupt_cache_blocks_task_link_before_network_and_preserves_file(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_SCRIPT_CACHE_FILE

            self.seed_cache()
            raw = b"[] trailing"
            ONLINE_SCRIPT_CACHE_FILE.write_bytes(raw)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks") as fetch:
                response = client.post(
                    "/online-scripts/task-link/demo",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 500)
            self.assertIn("缓存无法读取", response.get_data(as_text=True))
            fetch.assert_not_called()
            self.assertEqual(ONLINE_SCRIPT_CACHE_FILE.read_bytes(), raw)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is needed for cache boundary")
    def test_cache_symlink_is_rejected_and_repair_restores_list(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_SCRIPT_CACHE_FILE

            outside = base_dir / "outside-cache.json"
            outside.write_text(json.dumps([]), encoding="utf-8")
            ONLINE_SCRIPT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_SCRIPT_CACHE_FILE.symlink_to(outside)
            client = app.test_client()
            page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("不能是符号链接", page.get_data(as_text=True))
            self.assertTrue(ONLINE_SCRIPT_CACHE_FILE.is_symlink())

            ONLINE_SCRIPT_CACHE_FILE.unlink()
            ONLINE_SCRIPT_CACHE_FILE.write_text(json.dumps([{
                "id": "fixed", "name": "Fixed", "type": "raw", "link": "x",
            }]), encoding="utf-8")
            recovered = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(recovered.status_code, 200)
            self.assertIn("Fixed", recovered.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
