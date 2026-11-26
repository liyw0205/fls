import copy
import importlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task


class NativeProxyApiTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        models.save_proxies([
            {"id": "p", "name": "Alpha", "type": "socks5", "host": "localhost", "port": "1080",
             "username": "private-user", "password": "private-password", "enabled": True,
             "extension": {"preserve": True}},
            {"id": "g", "name": "Beta", "type": "github", "url": "https://proxy.example/", "enabled": False},
        ])

    def get(self, client, path="/api/proxy/settings"):
        return client.get(path, headers={"X-Token": TOKEN})

    def post(self, client, data):
        return client.post("/api/proxy/settings", json=data, headers={"X-Token": TOKEN})

    def data(self, action="edit", **extra):
        from fls_manager import models
        from fls_manager.proxy_actions import proxy_version
        data = {"request_id": "a" * 32, "action": action}
        if action == "new":
            data["settings"] = {"name": "New", "type": "http", "host": "localhost", "port": "8080", "enabled": True}
        else:
            data.update(proxy_id="p", version=proxy_version(models.load_proxies()[0]))
            if action == "edit":
                data["settings"] = {"name": "Edited"}
            elif action == "toggle":
                data["enabled"] = False
        return dict(data, **extra)

    def receipt(self, base):
        return base / "data/proxy_requests" / ("a" * 32 + ".json")

    def test_session_csrf_and_no_store(self):
        with isolated_app() as (app, _):
            from fls_manager.native_sessions import create_native_session
            self.seed()
            client = app.test_client()
            self.assertEqual(client.get("/api/proxy/settings").status_code, 401)
            with app.app_context():
                session_id, csrf, _ = create_native_session(TOKEN, remember=True, verified=True)
            client.set_cookie("fls_native_session", session_id)
            data = self.data("toggle")
            self.assertEqual(client.post("/api/proxy/settings", json=data).status_code, 400)
            response = client.post("/api/proxy/settings", json=data, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200, response.json)
            for response in (response, client.get("/api/proxy/settings"), client.get("/api/proxy/settings/p"),
                             client.get("/api/proxy/requests/" + "a" * 32)):
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_list_filters_paging_and_safe_details(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            models.save_tasks([sample_task("t", proxy_id="p", env={"secret": "task-secret"})])
            client = app.test_client()
            response = self.get(client, "/api/proxy/settings?limit=1&page=1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["total"], 2)
            self.assertEqual(response.json["pages"], 2)
            self.assertEqual(response.json["items"][0]["id"], "p")
            self.assertEqual(response.json["items"][0]["reference_count"], 1)
            self.assertIn("socks5h", response.json["types"])
            self.assertEqual(self.get(client, "/api/proxy/settings?page=99&limit=1").json["page"], 2)
            for query in ("q=PRIVATE-password", "q=private-user", "type=http", "enabled=0&type=socks5"):
                self.assertEqual(self.get(client, "/api/proxy/settings?" + query).json["total"], 0)
            filtered = self.get(client, "/api/proxy/settings?type=github&enabled=0").json
            self.assertEqual([p["id"] for p in filtered["items"]], ["g"])
            detail = self.get(client, "/api/proxy/settings/p").json
            self.assertTrue(detail["proxy"]["username_configured"])
            self.assertTrue(detail["proxy"]["password_configured"])
            self.assertEqual(detail["proxy"]["references"], [{"id": "t", "name": "Task t"}])
            for secret in ("private-password", "private-user", "task-secret", "extension"):
                self.assertNotIn(secret, json.dumps([response.json, detail]))
            self.assertEqual(self.get(client, "/api/proxy/settings/missing").status_code, 404)

    def test_legacy_unsafe_addresses_are_hidden_and_not_searched(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            models.save_proxies([
                {"id": "unsafe", "type": "github", "url": "https://url-user:url-password@proxy.example/?token=url-secret#fragment"},
                {"id": "host", "host": "http://host-user:host-password@proxy.example/", "port": "80"},
            ])
            client = app.test_client()
            for proxy_id in ("unsafe", "host"):
                result = self.get(client, "/api/proxy/settings/" + proxy_id)
                self.assertEqual(result.status_code, 200)
                text = result.get_data(as_text=True)
                for secret in ("url-user", "url-password", "url-secret", "fragment", "host-user", "host-password"):
                    self.assertNotIn(secret, text)
            self.assertTrue(self.get(client, "/api/proxy/settings/unsafe").json["proxy"]["url_requires_replace"])
            self.assertTrue(self.get(client, "/api/proxy/settings/host").json["proxy"]["host_requires_replace"])
            self.assertEqual(self.get(client, "/api/proxy/settings?q=url-secret").json["total"], 0)

    def test_partial_edit_keeps_credentials_and_extensions(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            response = self.post(client, self.data())
            self.assertEqual(response.status_code, 200, response.json)
            current = models.load_proxies()[0]
            self.assertEqual(current["name"], "Edited")
            self.assertEqual(current["host"], "localhost")
            self.assertEqual(current["username"], "private-user")
            self.assertEqual(current["password"], "private-password")
            self.assertEqual(current["extension"], {"preserve": True})
            record = self.receipt(base).read_text()
            for secret in ("private-user", "private-password", "localhost", "Edited"):
                self.assertNotIn(secret, record)
            self.assertEqual(json.loads(record)["intent_kind"], "hmac-sha256-v1")

    def test_credentials_explicit_replace_clear_and_type_change(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            data = self.data(credentials={"username_action": "replace", "username": " new-user ",
                                          "password_action": "replace", "password": " new password "},
                             settings={"type": "github", "url": "https://proxy.example/"})
            self.assertEqual(self.post(client, data).status_code, 200)
            current = models.load_proxies()[0]
            self.assertEqual(current["username"], "new-user")
            self.assertEqual(current["password"], "new password")
            self.assertEqual(current["type"], "github")
            data = self.data(request_id="b" * 32, settings={}, credentials={"password_action": "clear"})
            self.assertEqual(self.post(client, data).status_code, 200)
            self.assertEqual(models.load_proxies()[0]["password"], "")
            self.assertEqual(models.load_proxies()[0]["username"], "new-user")

    def test_create_all_types_and_ipv6(self):
        for kind in ("http", "https", "socks4", "socks5", "socks5h", "github"):
            with self.subTest(kind=kind), isolated_app() as (app, _):
                from fls_manager import models
                self.seed()
                settings = {"name": "New", "type": kind, "enabled": False}
                settings.update({"url": "https://proxy.example/base/"} if kind == "github" else {"host": "::1", "port": "1080"})
                data = self.data("new", settings=settings)
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 200, response.json)
                created = next(p for p in models.load_proxies() if p["id"] == response.json["proxy_id"])
                self.assertEqual(created["type"], kind)
                self.assertFalse(created["enabled"])
                if kind != "github":
                    self.assertEqual(created["host"], "[::1]")

    def test_invalid_requests_have_zero_writes(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            baseline = self.data()
            invalid = [[], {**baseline, "extra": "secret"}, {**baseline, "version": []},
                       {**baseline, "request_id": "../bad"}, {**baseline, "action": []},
                       {**baseline, "settings": {"password": "secret"}},
                       {**baseline, "settings": {"enabled": 1}},
                       {**baseline, "settings": {"port": 123}},
                       {**baseline, "settings": {"name": "x" * 201}},
                       {**baseline, "settings": {"name": "a\nb"}},
                       {**baseline, "settings": {"type": "unknown"}},
                       {**baseline, "settings": {"type": "github", "url": "https://user:password@host/"}},
                       {**baseline, "settings": {"port": "0"}},
                       {**baseline, "settings": {"host": "http://host/"}},
                       {**baseline, "credentials": {"password": "secret"}},
                       {**baseline, "credentials": {"password_action": "keep", "password": "secret"}},
                       {**baseline, "credentials": {"password_action": "replace"}},
                       {**baseline, "credentials": {"password_action": "replace", "password": ""}},
                       {**baseline, "credentials": {"password_action": "replace", "password": "   "}},
                       {**baseline, "credentials": {"password_action": "clear", "password": "secret"}},
                       {**baseline, "credentials": {"password_action": "replace", "password": "a\x00b"}},
                       {**baseline, "credentials": {"unknown": "value"}},
                       {**baseline, "confirm_retry": 1}]
            before = models.load_proxies()
            client = app.test_client()
            for data in invalid:
                with self.subTest(data=data):
                    result = self.post(client, data)
                    self.assertEqual(result.status_code, 400, result.json)
                    self.assertIs(result.json["saved"], False)
            self.assertEqual(models.load_proxies(), before)
            self.assertFalse((base / "data/proxy_requests").exists())
            result = client.post("/api/proxy/settings", data="x" * (64 * 1024 + 1),
                                 content_type="application/json", headers={"X-Token": TOKEN})
            self.assertEqual(result.status_code, 413)
            self.assertEqual(client.post("/api/proxy/settings", data={}, headers={"X-Token": TOKEN}).status_code, 415)
            self.assertEqual(client.post("/api/proxy/settings", data='{"action":"new","action":"delete"}',
                                         content_type="application/json", headers={"X-Token": TOKEN}).status_code, 400)

    def test_invalid_and_duplicate_query_fields(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            for query in ("limit=101", "page=0", "page=x", "enabled=true", "type=bad", "limit=1&limit=2", "unknown=1", "q=" + "x" * 201):
                result = self.get(client, "/api/proxy/settings?" + query)
                self.assertEqual(result.status_code, 400, query)
                self.assertEqual(result.headers.get("Cache-Control"), "no-store")

    def test_stale_and_referenced_actions_are_safe_and_bounded(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            models.save_tasks([sample_task(str(i), proxy_id="p", env={"secret": "task-secret"}) for i in range(75)])
            client = app.test_client()
            detail = self.get(client, "/api/proxy/settings/p").json["proxy"]
            self.assertEqual(detail["reference_count"], 75)
            self.assertEqual(len(detail["references"]), 50)
            for data in (self.data("toggle"), self.data("delete"), self.data(settings={"enabled": False})):
                result = self.post(client, data)
                self.assertEqual(result.status_code, 409)
                self.assertEqual(result.json["reference_count"], 75)
                self.assertEqual(len(result.json["references"]), 50)
                for secret in ("private-password", "private-user", "task-secret"):
                    self.assertNotIn(secret, result.get_data(as_text=True))
            result = self.post(client, self.data(version="f" * 64))
            self.assertEqual(result.status_code, 409)
            self.assertTrue(result.json["conflict"])
            self.assertNotIn("current", result.json)
            self.assertFalse((base / "data/proxy_requests").exists())

    def test_completed_replay_keeps_original_intent_after_later_credential_change(self):
        with isolated_app() as (app, _):
            from fls_manager import models, proxy_actions
            self.seed()
            client = app.test_client()
            data = self.data()
            self.assertEqual(self.post(client, data).status_code, 200)
            proxies = models.load_proxies()
            proxies[0].update(name="Later", password="later-password")
            models.save_proxies(proxies)
            importlib.reload(proxy_actions)
            with patch.object(models, "save_proxies") as save:
                self.assertEqual(self.post(client, data).status_code, 200)
                changed = dict(data, settings={"name": "Other"})
                self.assertEqual(self.post(client, changed).status_code, 409)
                save.assert_not_called()
            self.assertEqual(models.load_proxies()[0]["password"], "later-password")

    def test_failed_native_intent_cannot_be_repurposed(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            data = self.data("new")
            with patch.object(models, "save_proxies", side_effect=OSError("full")):
                self.assertEqual(self.post(client, data).status_code, 500)
            changed = copy.deepcopy(data)
            changed["settings"]["name"] = "Other"
            self.assertEqual(self.post(client, changed).status_code, 409)
            self.assertEqual(self.post(client, data).status_code, 200)
            self.assertEqual(len(models.load_proxies()), 3)

    def test_read_only_recovery_before_after_publication_and_later_changes(self):
        for mode in ("failed", "prepared", "published", "completed", "changed"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager import models, proxy_actions
                self.seed()
                client = app.test_client()
                data = self.data()
                save, write = models.save_proxies, proxy_actions.write_json
                def publish(values):
                    if mode in ("published", "completed", "changed"):
                        save(values)
                    if mode != "completed":
                        raise OSError("publication fault private-password")
                def receipt(path, row):
                    if mode != "failed" and row["state"] != "prepared" and mode != "completed":
                        raise OSError("receipt fault")
                    return write(path, row)
                with patch.object(models, "save_proxies", side_effect=publish), \
                     patch.object(proxy_actions, "write_json", side_effect=receipt):
                    result = self.post(client, data)
                self.assertEqual(result.status_code, 200 if mode == "completed" else 503 if mode in ("published", "changed") else 500)
                if mode == "changed":
                    proxies = models.load_proxies()
                    proxies[0]["name"] = "Later"
                    save(proxies)
                before = self.receipt(base).read_bytes()
                with patch.object(models, "save_proxies") as saving, patch.object(proxy_actions, "write_json") as writing, \
                     patch("fls_manager.proxy.test_proxy_object") as probe, patch("fls_manager.proxy.quality_proxy_object") as quality:
                    recovered = self.get(client, "/api/proxy/requests/" + data["request_id"])
                    self.assertEqual(recovered.status_code, 200, recovered.json)
                    self.assertIs(recovered.json["saved"], None if mode == "changed" else mode in ("published", "completed"))
                    saving.assert_not_called()
                    writing.assert_not_called()
                    probe.assert_not_called()
                    quality.assert_not_called()
                self.assertEqual(self.receipt(base).read_bytes(), before)

    def test_completed_query_is_historical_and_delete_has_no_side_effects(self):
        for action in ("new", "edit", "toggle", "delete"):
            with self.subTest(action=action), isolated_app() as (app, _):
                from fls_manager import models
                self.seed()
                client = app.test_client()
                data = self.data(action)
                self.assertEqual(self.post(client, data).status_code, 200)
                first = self.get(client, "/api/proxy/requests/" + "a" * 32).json
                self.assertTrue(first["saved"])
                self.assertTrue(first["current_matches"])
                models.save_proxies([])
                second = self.get(client, "/api/proxy/requests/" + "a" * 32).json
                self.assertTrue(second["saved"])
                self.assertTrue(second["historical"])
                self.assertEqual(second["current_matches"], action == "delete")

    def test_prepared_requires_explicit_confirm_and_stale_cannot_resume(self):
        with isolated_app() as (app, _):
            from fls_manager import models, proxy_actions
            self.seed()
            client = app.test_client()
            data = self.data()
            write = proxy_actions.write_json
            def receipt(path, row):
                if row["state"] != "prepared":
                    raise OSError("receipt failed")
                return write(path, row)
            with patch.object(models, "save_proxies", side_effect=OSError("full")), \
                 patch.object(proxy_actions, "write_json", side_effect=receipt):
                self.assertEqual(self.post(client, data).status_code, 500)
            self.assertTrue(self.get(client, "/api/proxy/requests/" + "a" * 32).json["confirm_required"])
            self.assertEqual(self.post(client, data).status_code, 409)
            self.assertEqual(self.post(client, dict(data, confirm_retry=True)).status_code, 200)

    def test_missing_and_corrupt_receipts_never_claim_success(self):
        with isolated_app() as (app, base):
            self.seed()
            client = app.test_client()
            path = "/api/proxy/requests/" + "a" * 32
            missing = self.get(client, path)
            self.assertEqual(missing.json["state"], "missing")
            self.assertIsNone(missing.json["saved"])
            self.assertEqual(self.get(client, "/api/proxy/requests/bad").status_code, 400)
            self.assertEqual(self.post(client, self.data()).status_code, 200)
            record_path = self.receipt(base)
            original = json.loads(record_path.read_text())
            corruptions = [{}, [], None]
            for field, value in (("request_id", "b" * 32), ("scope", "other:p"), ("scope", "edit:"),
                                 ("scope", "delete:p"), ("state", "not-real"), ("after", "bad"),
                                 ("before", []), ("intent", 123), ("updated_at", "bad"),
                                 ("intent_kind", "unknown"), ("input_version", "bad")):
                corruptions.append(dict(original, **{field: value}))
            for value in corruptions:
                with self.subTest(value=value):
                    record_path.write_text(json.dumps(value), encoding="utf-8")
                    recovered = self.get(client, path)
                    self.assertEqual(recovered.status_code, 503, recovered.json)
                    self.assertIsNone(recovered.json["saved"])
                    self.assertEqual(self.post(client, self.data()).status_code, 500)
            record_path.write_bytes(b"\xff")
            self.assertEqual(self.get(client, path).status_code, 503)

    def test_concurrent_same_request_creates_once(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            data = self.data("new", credentials={"password_action": "replace", "password": "new-secret"})
            with ThreadPoolExecutor(3) as pool:
                results = list(pool.map(lambda _: self.post(app.test_client(), data), range(3)))
            self.assertEqual([r.status_code for r in results], [200, 200, 200])
            self.assertEqual(len({r.json["proxy_id"] for r in results}), 1)
            self.assertEqual(len(models.load_proxies()), 3)

    def test_query_does_not_normalize_write_legacy_proxy_storage(self):
        with isolated_app() as (app, base):
            from fls_manager import models, paths
            self.seed()
            client = app.test_client()
            self.assertEqual(self.post(client, self.data()).status_code, 200)
            proxies = models.load_proxies()
            proxies[0].pop("enabled")
            paths.PROXY_FILE.write_text(json.dumps(proxies), encoding="utf-8")
            before = paths.PROXY_FILE.read_bytes()
            receipt_before = self.receipt(base).read_bytes()
            with patch("fls_manager.storage._write_json_unlocked") as write:
                result = self.get(client, "/api/proxy/requests/" + "a" * 32)
                self.assertEqual(result.status_code, 200)
                self.assertTrue(result.json["saved"])
                write.assert_not_called()
            self.assertEqual(paths.PROXY_FILE.read_bytes(), before)
            self.assertEqual(self.receipt(base).read_bytes(), receipt_before)
            proxies.append({"name": "Missing id"})
            paths.PROXY_FILE.write_text(json.dumps(proxies), encoding="utf-8")
            self.assertEqual(self.get(client, "/api/proxy/requests/" + "a" * 32).status_code, 503)

    def test_recovery_accepts_legacy_web_receipts_without_repeating_actions(self):
        for action in ("new", "edit", "toggle", "delete"):
            with self.subTest(action=action), isolated_app() as (app, _):
                self.seed()
                client = app.test_client()
                data = self.data(action)
                if action in ("new", "edit"):
                    data.pop("settings")
                    data.update(name="Legacy", type="http", host="localhost", port="8080", enabled=True)
                result = client.post("/api/proxy/action", json=data, headers={"X-Token": TOKEN})
                self.assertEqual(result.status_code, 200)
                recovered = self.get(client, "/api/proxy/requests/" + "a" * 32)
                self.assertTrue(recovered.json["saved"])
                self.assertTrue(recovered.json["current_matches"])

    def test_keep_credentials_obeys_concurrent_version_check(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            replace = self.data(credentials={"password_action": "replace", "password": "new-secret"})
            keep = self.data(request_id="b" * 32, settings={"host": "other.example"})
            entered, release = threading.Event(), threading.Event()
            save = models.save_proxies
            def persist(values):
                entered.set()
                self.assertTrue(release.wait(5))
                save(values)
            with patch.object(models, "save_proxies", side_effect=persist) as saving, ThreadPoolExecutor(2) as pool:
                first = pool.submit(self.post, app.test_client(), replace)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client(), keep)
                release.set()
                self.assertEqual(first.result(5).status_code, 200)
                self.assertEqual(second.result(5).status_code, 409)
                saving.assert_called_once()
            current = models.load_proxies()[0]
            self.assertEqual(current["password"], "new-secret")
            self.assertEqual(current["host"], "localhost")

    def test_receipt_write_before_and_after_publication_preserves_real_outcome(self):
        for published in (False, True):
            with self.subTest(published=published), isolated_app() as (app, base):
                from fls_manager import models, paths, proxy_actions
                self.seed()
                client = app.test_client()
                data = self.data()
                replace = Path.replace
                def fail_receipt(path, target):
                    if target == self.receipt(base):
                        if published:
                            replace(path, target)
                        raise OSError("receipt storage failure")
                    return replace(path, target)
                before = paths.PROXY_FILE.read_bytes()
                with patch.object(Path, "replace", fail_receipt), patch.object(models, "save_proxies") as save:
                    result = self.post(client, data)
                    self.assertEqual(result.status_code, 500)
                    self.assertFalse(result.json["saved"])
                    save.assert_not_called()
                recovered = self.get(client, "/api/proxy/requests/" + "a" * 32).json
                self.assertEqual(recovered["state"], "prepared" if published else "missing")
                self.assertEqual(paths.PROXY_FILE.read_bytes(), before)

    def test_signed_receipt_tamper_and_changed_stale_target_remain_unknown(self):
        with isolated_app() as (app, base):
            from fls_manager import models, proxy_actions
            self.seed()
            client = app.test_client()
            data = self.data()
            write = proxy_actions.write_json
            def receipt(path, row):
                if row["state"] != "prepared":
                    raise OSError("receipt failed")
                return write(path, row)
            with patch.object(models, "save_proxies", side_effect=OSError("full")), \
                 patch.object(proxy_actions, "write_json", side_effect=receipt):
                self.assertEqual(self.post(client, data).status_code, 500)
            proxies = models.load_proxies()
            proxies[0]["password"] = "concurrent-secret"
            models.save_proxies(proxies)
            result = self.post(client, dict(data, confirm_retry=True))
            self.assertEqual(result.status_code, 409)
            self.assertIsNone(result.json["saved"])
            self.assertFalse(result.json["resolved"])
            self.assertNotIn("concurrent-secret", result.get_data(as_text=True))
            record = json.loads(self.receipt(base).read_text())
            record["state"] = "completed"
            self.receipt(base).write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(self.get(client, "/api/proxy/requests/" + "a" * 32).status_code, 503)


if __name__ == "__main__":
    unittest.main()
