import json
import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task


class NativeEnvApiTests(unittest.TestCase):
    headers = {"X-Token": TOKEN}

    def seed(self):
        from fls_manager import models
        models.save_global_env({"ORIGINAL": "secret-original", "TOKEN": "secret-token", "EMPTY": ""})
        models.save_tasks([sample_task("a", env={"IMPORTED": "secret-source", "ORIGINAL": "replacement"})])

    def get(self, client, path):
        result = client.get(path, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.headers["Cache-Control"], "no-store")
        return result.get_json()

    def data(self, action="edit", **extra):
        from fls_manager import models
        from fls_manager.global_env import env_version
        env = models.load_global_env()
        values = {"action": action, "request_id": "b" * 32,
                  "version": env_version(env, ["ORIGINAL"] if action in ("edit", "delete") else None)}
        if action in ("edit", "new"):
            values.update(key="RENAMED", value="new-secret")
        if action in ("edit", "delete"):
            values["original_key"] = "ORIGINAL"
        return dict(values, **extra)

    def post(self, client, data):
        return client.post("/api/env/mutate", json=data, headers=self.headers)

    def test_native_session_auth_csrf_and_no_store(self):
        with isolated_app() as (app, _):
            from fls_manager.native_sessions import create_native_session
            client = app.test_client()
            for path in ("variables", "variable/TOKEN?reveal=1", "snapshot?reveal=1", "task-sources", "requests/" + "b" * 32):
                self.assertEqual(client.get("/api/env/" + path).status_code, 401)
            self.assertEqual(client.post("/api/env/mutate", json={}).status_code, 400)
            with app.app_context():
                session_id, csrf, _ = create_native_session(TOKEN, remember=True, verified=True)
            client.set_cookie("fls_native_session", session_id)
            self.seed()
            self.assertEqual(client.get("/api/env/variables").status_code, 200)
            self.assertEqual(client.post("/api/env/mutate", json=self.data()).status_code, 400)
            result = client.post("/api/env/mutate", json=self.data(), headers={"X-CSRF-Token": csrf})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers["Cache-Control"], "no-store")

    def test_paged_reads_hide_values_and_reveal_is_explicit(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            page = self.get(client, "/api/env/variables?limit=1&page=2")
            self.assertEqual((page["total"], page["pages"], len(page["items"])), (3, 3, 1))
            self.assertNotIn("secret-", json.dumps(page))
            self.assertEqual(self.get(client, "/api/env/variables?q=token")["items"][0]["key"], "TOKEN")
            self.assertNotIn("value", self.get(client, "/api/env/variable/TOKEN"))
            self.assertEqual(self.get(client, "/api/env/variable/TOKEN?reveal=1")["value"], "secret-token")
            self.assertEqual(self.get(client, "/api/env/variable/EMPTY?reveal=1")["value"], "")
            self.assertEqual(client.get("/api/env/snapshot", headers=self.headers).status_code, 400)
            self.assertIn('TOKEN="secret-token"', self.get(client, "/api/env/snapshot?reveal=1")["env_text"])
            self.assertEqual(client.get("/api/env/variable/MISSING", headers=self.headers).status_code, 404)

    def test_new_edit_delete_replace_and_replay_do_not_repeat_writes(self):
        for action in ("new", "edit", "delete", "replace"):
            with self.subTest(action=action), isolated_app() as (app, base):
                from fls_manager import models
                self.seed()
                client = app.test_client()
                data = self.data(action, **({"env_text": 'A="a\\nb"\nB=""'} if action == "replace" else {}))
                self.assertEqual(self.post(client, data).status_code, 200)
                after = models.load_global_env()
                self.assertEqual(self.get(client, "/api/env/requests/" + data["request_id"])["saved"], True)
                models.save_global_env({"LATER": "keep"})
                with patch.object(models, "save_global_env") as save:
                    self.assertEqual(self.post(client, data).status_code, 200)
                    save.assert_not_called()
                self.assertNotIn("secret-", (base / "data/global_env_requests" / (data["request_id"] + ".json")).read_text())
                if action == "replace":
                    self.assertEqual(after, {"A": "a\nb", "B": ""})
                elif action == "delete":
                    self.assertNotIn("ORIGINAL", after)
                else:
                    self.assertEqual(after["RENAMED"], "new-secret")

    def test_conflict_and_unknown_responses_never_include_values(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            data = self.data()
            models.save_global_env({"ORIGINAL": "later-secret"})
            result = self.post(client, data)
            self.assertEqual(result.status_code, 409)
            self.assertTrue(result.get_json()["conflict"])
            self.assertNotIn("current", result.get_json())
            self.assertNotIn("secret", result.get_data(as_text=True))
            data = self.data()
            with patch("fls_manager.routes.env.native.save_global_env", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.post(client, data)
            state = self.get(client, "/api/env/requests/" + data["request_id"])
            self.assertEqual((state["saved"], state["state"]), (False, "pending"))
            self.assertEqual(self.post(client, data).get_json()["confirm_required"], True)
            self.assertEqual(self.post(client, dict(data, confirm_retry=True)).status_code, 200)

    def test_import_uses_source_and_global_versions_and_skip_semantics(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            sources = self.get(client, "/api/env/task-sources")
            self.assertNotIn("secret-", json.dumps(sources))
            data = self.data("import", items=[item["id"] for item in sources["items"]],
                sources={item["id"]: item["version"] for item in sources["items"]}, overwrite=False)
            models.save_tasks([sample_task("a", env={"IMPORTED": "changed", "ORIGINAL": "replacement"})])
            self.assertEqual(self.post(client, data).status_code, 409)
            self.assertNotIn("IMPORTED", models.load_global_env())
            self.seed()
            result = self.post(client, data)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()["skipped_count"], 1)
            self.assertEqual(models.load_global_env()["ORIGINAL"], "secret-original")

    def test_invalid_inputs_and_limits_leave_environment_untouched(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            before = models.load_global_env()
            for change in ({"version": None}, {"request_id": "../x"}, {"key": "1NO"},
                           {"value": "\x00"}, {"current": {}}, {"action": "bad"}, {"action": []},
                           {"value": "x" * 65537}):
                with self.subTest(change=list(change)):
                    self.assertGreaterEqual(self.post(client, self.data(**change)).status_code, 400)
            self.assertEqual(client.post("/api/env/mutate", data="x", headers=self.headers).status_code, 415)
            self.assertEqual(client.post("/api/env/mutate", data='"x"', content_type="application/json", headers=self.headers).status_code, 400)
            self.assertEqual(client.post("/api/env/mutate", data="x" * (1200 * 1024), content_type="application/json", headers=self.headers).status_code, 413)
            for path in ("variables?limit=101", "variables?page=0", "variables?page=x", "task-sources?q=" + "a" * 201):
                self.assertEqual(client.get("/api/env/" + path, headers=self.headers).status_code, 400)
            self.assertEqual(models.load_global_env(), before)


if __name__ == "__main__":
    unittest.main()
