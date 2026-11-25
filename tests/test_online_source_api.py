import hashlib
import json
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineSourceApiTests(unittest.TestCase):
    def item(self, script_id="demo"):
        return {
            "id": script_id,
            "name": script_id,
            "type": "raw",
            "link": f"https://example.invalid/{script_id}.sh",
            "task_cron": [{"name": f"{script_id}-task", "command": "python task.py"}],
        }

    def test_source_read_requires_auth_and_returns_revision_without_cache_headers(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            cache_text = (base_dir / "data" / "online_scripts_cache.json").read_text(encoding="utf-8")
            client = app.test_client()
            unauthorized = client.get("/api/online-scripts/source")
            response = client.get("/api/online-scripts/source", headers={"X-Token": TOKEN})

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(response.json["json_text"], cache_text)
            self.assertEqual(response.json["cache_revision"], hashlib.sha256(cache_text.encode()).hexdigest())

    def test_source_write_normalizes_and_returns_the_new_revision(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item("old")])
            client = app.test_client()
            current = client.get("/api/online-scripts/source", headers={"X-Token": TOKEN}).json
            draft = json.dumps([self.item("new")], ensure_ascii=False, indent=2)
            response = client.put(
                "/api/online-scripts/source",
                json={"json_text": draft, "cache_revision": current["cache_revision"]},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            self.assertEqual(response.json["count"], 1)
            self.assertEqual(source.load_online_script_cache()[0]["id"], "new")
            saved = (base_dir / "data" / "online_scripts_cache.json").read_text(encoding="utf-8")
            self.assertEqual(response.json["json_text"], saved)
            self.assertEqual(response.json["cache_revision"], hashlib.sha256(saved.encode()).hexdigest())

    def test_stale_write_returns_current_cache_without_overwriting_it(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item("first")])
            client = app.test_client()
            old_revision = client.get("/api/online-scripts/source", headers={"X-Token": TOKEN}).json["cache_revision"]
            source.save_online_script_cache([self.item("latest")])
            latest_text = source.read_cache_text()
            response = client.put(
                "/api/online-scripts/source",
                json={"json_text": json.dumps([self.item("draft")]), "cache_revision": old_revision},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json["code"], "revision_conflict")
            self.assertEqual(response.json["current_json_text"], latest_text)
            self.assertEqual(source.read_cache_text(), latest_text)

    def test_invalid_source_json_never_replaces_current_cache(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item("stable")])
            before = source.read_cache_text()
            client = app.test_client()
            revision = client.get("/api/online-scripts/source", headers={"X-Token": TOKEN}).json["cache_revision"]
            for invalid in (
                "",
                "{}",
                json.dumps([self.item("duplicate"), self.item("duplicate")]),
                json.dumps([self.item("surrogate") | {"name": chr(0xD800)}]),
            ):
                with self.subTest(invalid=invalid):
                    response = client.put(
                        "/api/online-scripts/source",
                        json={"json_text": invalid, "cache_revision": revision},
                        headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(source.read_cache_text(), before)

    def test_cache_and_request_size_limits_are_enforced_before_write(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import source_json

            source.save_online_script_cache([self.item("stable")])
            before = source.read_cache_text()
            client = app.test_client()
            current = client.get("/api/online-scripts/source", headers={"X-Token": TOKEN}).json
            with patch.object(source_json, "SOURCE_CACHE_MAX_BYTES", 16):
                oversized_get = client.get("/api/online-scripts/source", headers={"X-Token": TOKEN})
                oversized_source = client.put(
                    "/api/online-scripts/source",
                    json={"json_text": json.dumps([self.item("large")]), "cache_revision": current["cache_revision"]},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(oversized_get.status_code, 413)
            self.assertEqual(oversized_source.status_code, 413)
            self.assertEqual(source.read_cache_text(), before)

    def test_source_rejects_malformed_json_and_oversized_request(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import source_json

            client = app.test_client()
            headers = {"X-Token": TOKEN, "Content-Type": "application/json"}
            malformed = client.put("/api/online-scripts/source", data="{", headers=headers)
            with patch.object(source_json, "SOURCE_REQUEST_MAX_BYTES", 8):
                oversized = client.put(
                    "/api/online-scripts/source",
                    data='{"json_text":"too large"}',
                    headers=headers,
                )

            self.assertEqual(malformed.status_code, 400)
            self.assertEqual(oversized.status_code, 413)

    def test_refresh_options_returns_only_enabled_proxy_metadata(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import refresh

            with patch.object(
                refresh,
                "load_proxies",
                return_value=[
                    {"id": "enabled", "name": "Enabled", "type": "http", "enabled": True, "password": "secret"},
                    {"id": "disabled", "name": "Disabled", "type": "socks", "enabled": False},
                ],
            ):
                response = app.test_client().get(
                    "/api/online-scripts/refresh-options", headers={"X-Token": TOKEN}
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(response.json["proxies"], [{"id": "enabled", "name": "Enabled", "type": "http"}])
            self.assertNotIn("secret", response.get_data(as_text=True))

    def test_catalog_exposes_only_a_boolean_for_external_task_source(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import catalog_api

            scripts = [self.item("linked") | {"task_link": "https://example.invalid/tasks.json?token=private"}]
            with patch.object(catalog_api, "load_online_script_cache", return_value=scripts):
                response = app.test_client().get("/api/online-scripts/catalog", headers={"X-Token": TOKEN})

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["items"][0]["has_task_source"])
            self.assertNotIn("task_link", response.json["items"][0])
            self.assertNotIn("private", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
