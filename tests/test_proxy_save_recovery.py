import importlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import isolated_app, sample_task
from tests import test_proxy_action_recovery as proxy_tests


class ProxySaveRecoveryTests(unittest.TestCase):
    seed = proxy_tests.ProxyActionRecoveryTests.seed
    authenticate = proxy_tests.ProxyActionRecoveryTests.authenticate
    form = proxy_tests.ProxyActionRecoveryTests.form
    post = proxy_tests.ProxyActionRecoveryTests.post

    def values(self, action="new", **extra):
        from fls_manager import models
        from fls_manager.proxy_actions import proxy_version
        values = {"action": action, "request_id": "a" * 32, "name": "My proxy", "type": "socks5",
                  "host": "localhost", "port": "1080", "username": "user", "password": "private-value",
                  "url": "", "enabled": True}
        if action == "edit":
            values.update(proxy_id="p", version=proxy_version(models.load_proxies()[0]))
        return dict(values, **extra)

    def test_new_edit_and_completed_replay_do_not_overwrite_later_settings(self):
        for action in ("new", "edit"):
            with self.subTest(action=action), isolated_app() as (app, base):
                from fls_manager import models, proxy_actions
                self.seed()
                data = self.values(action)
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 200)
                target = response.json["proxy_id"]
                current = next(p for p in models.load_proxies() if p["id"] == target)
                self.assertEqual(current["password"], "private-value")
                receipt = (base / "data/proxy_requests" / ("a" * 32 + ".json")).read_text()
                self.assertNotIn("private-value", receipt)
                self.assertNotIn("localhost", receipt)
                self.assertEqual(len(models.load_proxies()), 3 if action == "new" else 2)
                proxies = models.load_proxies()
                proxies = [dict(p, name="Later", password="later-value") if p["id"] == target else p for p in proxies]
                models.save_proxies(proxies)
                importlib.reload(proxy_actions)
                with patch.object(models, "save_proxies") as save:
                    self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                    self.assertEqual(self.post(app.test_client(), dict(data, host="changed")).status_code, 409)
                    save.assert_not_called()
                current = next(p for p in models.load_proxies() if p["id"] == target)
                self.assertEqual(current["password"], "later-value")

    def test_new_edit_publication_and_receipt_failures_recover_fixed_target(self):
        for action in ("new", "edit"):
            for mode in ("before", "after", "receipt", "prepared"):
                with self.subTest(action=action, mode=mode), isolated_app() as (app, base):
                    from fls_manager import models, paths, proxy_actions
                    self.seed()
                    data = self.values(action)
                    replace, write = Path.replace, proxy_actions.write_json
                    original = models.load_proxies()
                    def publish(path, target):
                        if target == paths.PROXY_FILE and mode in ("before", "after", "prepared"):
                            if mode == "after":
                                replace(path, target)
                            raise OSError("disk fault")
                        return replace(path, target)
                    def receipt(path, row):
                        if mode in ("receipt", "prepared") and row["state"] != "prepared":
                            raise OSError("receipt fault")
                        return write(path, row)
                    with patch.object(Path, "replace", publish), patch.object(proxy_actions, "write_json", side_effect=receipt):
                        response = self.post(app.test_client(), data)
                    self.assertEqual(response.status_code, 503 if mode in ("after", "receipt") else 500)
                    self.assertEqual(response.json["saved"], mode in ("after", "receipt"))
                    if mode in ("before", "prepared"):
                        self.assertEqual(models.load_proxies(), original)
                    if mode == "prepared":
                        self.assertEqual(self.post(app.test_client(), data).status_code, 409)
                    result = self.post(app.test_client(), dict(data, confirm_retry=True))
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(len(models.load_proxies()), 3 if action == "new" else 2)

    def test_validation_stale_and_reference_boundaries_have_zero_side_effects(self):
        invalid = [("type", "invalid"), ("host", "http://bad"), ("host", "a b"), ("host", "bad:80"),
                   ("port", "0"), ("port", "65536"), ("port", "abc"), ("name", "a\x00b"),
                   ("password", ["bad"]), ("enabled", 1), ("request_id", "bad")]
        for field, value in invalid:
            with self.subTest(field=field, value=value), isolated_app() as (app, base):
                from fls_manager import models, paths
                self.seed()
                before = paths.PROXY_FILE.read_bytes()
                with patch.object(models, "save_proxies") as save:
                    self.assertEqual(self.post(app.test_client(), self.values(**{field: value})).status_code, 400)
                    save.assert_not_called()
                self.assertFalse((base / "data/proxy_requests").exists())
                self.assertEqual(paths.PROXY_FILE.read_bytes(), before)
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            data = self.values("edit")
            models.save_tasks([sample_task("t", proxy_id="p")])
            self.assertEqual(self.post(app.test_client(), dict(data, enabled=False)).status_code, 409)
            self.assertEqual(self.post(app.test_client(), dict(data, version="f" * 64)).status_code, 409)
            self.assertFalse((base / "data/proxy_requests").exists())
            self.assertEqual(self.post(app.test_client(), data).status_code, 200)

    def test_github_ipv6_and_disabled_creation(self):
        for url in ("", "file:///tmp/a", "https://user:pass@host/", "https://host/?secret=1", "https://host/#fragment", "https://a b/", "https://host:99999/"):
            with self.subTest(url=url), isolated_app() as (app, base):
                self.seed()
                self.assertEqual(self.post(app.test_client(), self.values(type="github", url=url)).status_code, 400)
                self.assertFalse((base / "data/proxy_requests").exists())
        for values in ({"type": "github", "url": "https://proxy.example/base/"}, {"host": "::1"}, {"host": "[::1]", "enabled": False}, {"host": "::1", "type": "socks5h"}, {"host": "::1", "type": "socks4"}):
            with self.subTest(values=values), isolated_app() as (app, _):
                from fls_manager import models, proxy
                self.seed()
                result = self.post(app.test_client(), self.values(**values))
                self.assertEqual(result.status_code, 200)
                current = next(p for p in models.load_proxies() if p["id"] == result.json["proxy_id"])
                if current["type"] != "github":
                    self.assertIn("[::1]:1080", proxy.build_proxy_url(current))
                self.assertEqual(current["enabled"], values.get("enabled", True))

    def test_saved_credentials_are_encoded_for_real_proxy_clients(self):
        with isolated_app() as (app, _):
            from fls_manager import proxy
            result = self.post(app.test_client(), self.values(username="user@company", password="p:a/s?#@ word"))
            self.assertEqual(result.status_code, 200)
            env = proxy.apply_proxy_env({}, result.json["proxy_id"])
            self.assertEqual(env["HTTP_PROXY"], "socks5://user%40company:p%3Aa%2Fs%3F%23%40%20word@localhost:1080")

    def test_concurrent_duplicate_and_independent_creates(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            data = self.values()
            entered, release = threading.Event(), threading.Event()
            save = models.save_proxies
            def persist(values):
                entered.set()
                self.assertTrue(release.wait(5))
                return save(values)
            with patch.object(models, "save_proxies", side_effect=persist) as writes, ThreadPoolExecutor(3) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(5))
                duplicate = pool.submit(self.post, app.test_client(), data)
                independent = pool.submit(self.post, app.test_client(), dict(data, request_id="b" * 32, name="Independent"))
                release.set()
                results = [future.result(5) for future in (first, duplicate, independent)]
                self.assertEqual([r.status_code for r in results], [200, 200, 200])
                self.assertEqual(results[0].json["proxy_id"], results[1].json["proxy_id"])
                self.assertNotEqual(results[0].json["proxy_id"], results[2].json["proxy_id"])
                self.assertEqual(writes.call_count, 2)
            self.assertEqual(len(models.load_proxies()), 4)

    def test_real_form_draft_validation_disk_failure_conflict_and_deleted_save_as_new(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            from fls_manager.routes.proxy import actions
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            form = self.form(client.get("/proxy/new"), "/proxy/new")
            data = dict(form["fields"], name="Draft", type="http", host="127.0.0.1", port="bad", password="draft-secret", enabled="1")
            self.assertEqual(client.post(form["action"], data=dict(data, csrf_token="wrong")).status_code, 400)
            response = client.post(form["action"], data=data)
            self.assertEqual(response.status_code, 400)
            retry = self.form(response, "/proxy/new")
            self.assertEqual(retry["fields"]["password"], "draft-secret")
            retry["fields"].update(type="http", port="8080")
            failed_request = retry["fields"]["request_id"]
            with patch.object(actions, "save_proxies", side_effect=OSError("full")):
                response = client.post(retry["action"], data=retry["fields"])
            self.assertEqual(response.status_code, 500)
            retry = self.form(response, "/proxy/new")
            self.assertEqual(retry["fields"]["request_id"], failed_request)
            retry["fields"]["type"] = "http"
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
            self.assertEqual(len(models.load_proxies()), 3)
            edit = self.form(client.get("/proxy/edit/p"), "/proxy/edit/p")
            edit["fields"].update(type="http", name="Merged draft")
            proxies = models.load_proxies()
            proxies[0]["host"] = "later.example"
            models.save_proxies(proxies)
            response = client.post(edit["action"], data=edit["fields"])
            self.assertEqual(response.status_code, 409)
            self.assertIn("later.example", response.get_data(as_text=True))
            merged = self.form(response, "/proxy/edit/p")
            merged["fields"]["type"] = "http"
            self.assertEqual(merged["fields"]["name"], "Merged draft")
            self.assertEqual(client.post(merged["action"], data=merged["fields"]).status_code, 302)
            models.save_proxies([p for p in models.load_proxies() if p["id"] != "p"])
            response = client.post(edit["action"], data=dict(edit["fields"], request_id="c" * 32))
            self.assertEqual(response.status_code, 404)
            recovered = self.form(response, "/proxy/new")
            recovered["fields"]["type"] = "http"
            self.assertEqual(client.post(recovered["action"], data=recovered["fields"]).status_code, 302)

    def test_failed_save_external_change_cannot_resume_and_receipt_corruption_is_500(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            data = self.values("edit")
            with patch.object(models, "save_proxies", side_effect=OSError("full")):
                self.assertEqual(self.post(app.test_client(), data).status_code, 500)
            proxies = models.load_proxies()
            proxies[0]["name"] = "Later"
            models.save_proxies(proxies)
            self.assertEqual(self.post(app.test_client(), data).status_code, 409)
            self.assertEqual(models.load_proxies()[0]["name"], "Later")
            path = base / "data/proxy_requests" / ("a" * 32 + ".json")
            for raw in ("{", "null", "{}"):
                path.write_text(raw, encoding="utf-8")
                self.assertEqual(self.post(app.test_client(), data).status_code, 500)

    def test_failed_input_can_be_corrected_without_changing_creation_target(self):
        for action in ("new", "edit"):
            with self.subTest(action=action), isolated_app() as (app, _):
                from fls_manager import models
                self.seed()
                data = self.values(action)
                with patch.object(models, "save_proxies", side_effect=OSError("full")):
                    self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                changed = dict(data, name="Corrected", port="8081")
                self.assertEqual(self.post(app.test_client(), changed).status_code, 200)
                self.assertEqual(self.post(app.test_client(), data).status_code, 409)
                self.assertEqual(len(models.load_proxies()), 3 if action == "new" else 2)
                self.assertEqual([p["port"] for p in models.load_proxies() if p["name"] == "Corrected"], ["8081"])
