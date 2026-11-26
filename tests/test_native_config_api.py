import json
import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app


class NativeConfigApiTests(unittest.TestCase):
    def seed(self, *, token=TOKEN, **extra):
        from fls_manager.config import save_config
        values = {"admin_token": token, "theme": "deepseek", "port": 5700,
                  "timezone_offset_hours": 8, "panel_time_offset_seconds": 123,
                  "online_script_source": "https://example.invalid/index.json"}
        values.update(extra)
        save_config(values)

    def session(self, app, token=TOKEN):
        from fls_manager.native_sessions import create_native_session
        with app.app_context():
            session_id, csrf, _ = create_native_session(token, remember=True, verified=True)
        client = app.test_client()
        client.set_cookie("fls_native_session", session_id)
        return client, {"X-CSRF-Token": csrf}

    def get(self, client, path="/api/config/settings", token=TOKEN):
        headers = {"X-Token": token} if token is not None else {}
        response = client.get(path, headers=headers)
        self.assertIn(response.status_code, (200, 503), response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        return response

    def changes(self, response, **settings):
        return {"request_id": "a" * 32, "expected_revision": response.get_json()["revision"],
                "settings": settings}

    def test_native_auth_csrf_and_no_store(self):
        with isolated_app() as (app, _):
            self.seed()
            client = app.test_client()
            self.assertEqual(client.get("/api/config/settings").status_code, 401)
            self.assertEqual(client.post("/api/config/settings", json={}).status_code, 400)
            native, headers = self.session(app)
            self.assertEqual(native.post("/api/config/settings", json={}).status_code, 400)
            self.assertEqual(native.post("/api/config/apply", json={}, headers=headers).status_code, 400)
            self.assertEqual(native.get("/api/config/settings").status_code, 200)

    def test_settings_are_whitelisted_and_credentials_and_source_secrets_are_masked(self):
        with isolated_app() as (app, _), patch.dict("os.environ", {"FLS_TOKEN": ""}):
            self.seed(admin_token="long-admin-token-value", security_verify_enabled=True,
                security_verify_type="totp", totp_secret="JBSWY3DPEHPK3PXP",
                online_script_source="https://user:pass@example.invalid/index.json?token=url-secret")
            response = self.get(app.test_client(), token="long-admin-token-value")
            payload = response.get_json()
            rendered = json.dumps(payload)
            for secret in ("long-admin-token-value", "JBSWY3DPEHPK3PXP", "user:pass", "url-secret"):
                self.assertNotIn(secret, rendered)
            self.assertNotIn("admin_token", payload["settings"])
            self.assertNotIn("totp_secret", payload["settings"])
            self.assertTrue(payload["credentials"]["token_configured"])
            self.assertTrue(payload["credentials"]["totp_configured"])
            self.assertTrue(payload["settings"]["online_script_source_requires_reveal"])
            revealed = self.get(app.test_client(), "/api/config/settings?reveal_source=1", token="long-admin-token-value").get_json()
            self.assertIn("url-secret", revealed["settings"]["online_script_source"])
            self.assertNotIn("admin_token", revealed["settings"])

    def test_update_applies_once_and_preserves_unedited_and_unknown_config(self):
        with isolated_app() as (app, base):
            from fls_manager.config import load_config
            self.seed(timezone_offset_hours=-7, panel_time_offset_seconds=8450,
                      site_extension={"preserve": True})
            client = app.test_client()
            current = self.get(client)
            data = self.changes(current, theme="editorial", random_delay_seconds=17)
            with patch("fls_manager.routes.config.native.cleanup_logs") as cleanup, \
                 patch("fls_manager.routes.config.native.reload_scheduler") as reload:
                response = client.post("/api/config/settings", json=data, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200, response.get_json())
                replay = client.post("/api/config/settings", json=data, headers={"X-Token": TOKEN})
            self.assertTrue(response.get_json()["saved"])
            self.assertEqual(response.get_json()["apply_status"], "succeeded")
            self.assertTrue(response.get_json()["applied"])
            self.assertEqual(replay.get_json()["apply_status"], "succeeded")
            cleanup.assert_called_once()
            reload.assert_called_once()
            cfg = load_config()
            self.assertEqual(cfg["theme"], "editorial")
            self.assertEqual(cfg["random_delay_seconds"], 17)
            self.assertEqual(cfg["timezone_offset_hours"], -7)
            self.assertEqual(cfg["panel_time_offset_seconds"], 8450)
            self.assertEqual(cfg["site_extension"], {"preserve": True})
            receipt = base / "data" / "config_requests" / (data["request_id"] + ".json")
            self.assertNotIn("admin_token", receipt.read_text())

    def test_invalid_fields_revision_and_duplicate_request_conflict_do_not_save(self):
        with isolated_app() as (app, _):
            from fls_manager.config import load_config
            self.seed()
            client = app.test_client()
            baseline = self.get(client)
            before = load_config()
            for settings in ({"port": True}, {"port": "5701"}, {"port": 0},
                             {"random_delay_seconds": 121}, {"theme": "not-real"},
                             {"task_types": {"py": True, "unsafe": False}},
                             {"admin_token": "leak"}, {"unknown": 1}, {"online_script_source": "\x00"}):
                result = client.post("/api/config/settings", json=self.changes(baseline, **settings), headers={"X-Token": TOKEN})
                self.assertGreaterEqual(result.status_code, 400, settings)
            stale = self.changes(baseline, theme="editorial")
            stale["expected_revision"] = "0" * 64
            self.assertEqual(client.post("/api/config/settings", json=stale, headers={"X-Token": TOKEN}).status_code, 409)
            stable = self.changes(baseline, theme="editorial")
            self.assertEqual(client.post("/api/config/settings", json=stable, headers={"X-Token": TOKEN}).status_code, 200)
            duplicate = dict(stable, settings={"theme": "glassmorphism"})
            self.assertEqual(client.post("/api/config/settings", json=duplicate, headers={"X-Token": TOKEN}).status_code, 409)
            self.assertEqual(load_config()["theme"], "editorial")
            self.assertEqual(before["timezone_offset_hours"], load_config()["timezone_offset_hours"])

    def test_totp_enrollment_is_transient_and_security_update_requires_valid_code(self):
        with isolated_app() as (app, base):
            from fls_manager.config import load_config
            self.seed()
            client, headers = self.session(app)
            enrollment = client.post("/api/config/totp/enrollment", json={}, headers=headers)
            self.assertEqual(enrollment.status_code, 200, enrollment.get_json())
            secret = enrollment.get_json()["secret"]
            self.assertTrue(secret)
            self.assertFalse((base / "data" / "config.json").read_text().find(secret) >= 0)
            current = self.get(client, token=None)
            updates = {"enabled": True, "type": "totp", "totp_action": "replace",
                       "totp_secret": secret, "totp_code": "123456"}
            data = {"request_id": "b" * 32, "expected_revision": current.get_json()["revision"],
                    "credentials": updates}
            with patch("fls_manager.routes.config.native.verify_totp", return_value=False):
                rejected = client.post("/api/config/settings", json=data, headers=headers)
            self.assertEqual(rejected.status_code, 400)
            self.assertFalse(load_config()["security_verify_enabled"])
            self.assertNotIn(secret, rejected.get_data(as_text=True))
            with patch("fls_manager.routes.config.native.verify_totp", return_value=True), \
                 patch("fls_manager.routes.config.native.cleanup_logs"), \
                 patch("fls_manager.routes.config.native.reload_scheduler"):
                accepted = client.post("/api/config/settings", json=data, headers=headers)
            self.assertTrue(accepted.get_json()["saved"])
            self.assertTrue(load_config()["security_verify_enabled"])
            self.assertEqual(load_config()["totp_secret"], secret)
            self.assertEqual(client.get("/api/config/settings").status_code, 401)
            record = base / "data" / "config_requests" / (data["request_id"] + ".json")
            self.assertNotIn(secret, record.read_text())

    def test_token_change_revokes_native_sessions_and_receipt_survives_new_login(self):
        with isolated_app() as (app, _), patch.dict("os.environ", {"FLS_TOKEN": ""}):
            from fls_manager.config import load_config
            self.seed()
            client, headers = self.session(app)
            current = self.get(client)
            data = {"request_id": "c" * 32, "expected_revision": current.get_json()["revision"],
                    "credentials": {"token_action": "replace", "admin_token": "replacement-admin-token"}}
            with patch("fls_manager.routes.config.native.cleanup_logs"), \
                 patch("fls_manager.routes.config.native.reload_scheduler"):
                result = client.post("/api/config/settings", json=data, headers=headers)
            self.assertTrue(result.get_json()["saved"])
            self.assertTrue(result.get_json()["reauth_required"])
            self.assertEqual(load_config()["admin_token"], "replacement-admin-token")
            self.assertEqual(client.get("/api/config/settings").status_code, 401)
            fresh, _ = self.session(app, "replacement-admin-token")
            recovered = self.get(fresh, "/api/config/requests/" + data["request_id"])
            self.assertTrue(recovered.get_json()["saved"])
            self.assertNotIn("replacement-admin-token", recovered.get_data(as_text=True))

    def test_environment_token_override_blocks_config_token_change(self):
        with isolated_app() as (app, _), patch.dict("os.environ", {"FLS_TOKEN": "external-token"}):
            self.seed(token="external-token")
            client, headers = self.session(app, "external-token")
            current = self.get(client)
            data = {"request_id": "d" * 32, "expected_revision": current.get_json()["revision"],
                    "credentials": {"token_action": "replace", "admin_token": "replacement-token"}}
            result = client.post("/api/config/settings", json=data, headers=headers)
            self.assertEqual(result.status_code, 409)
            self.assertNotIn("replacement-token", result.get_data(as_text=True))

    def test_save_failure_and_result_state_are_recoverable_without_reapply(self):
        with isolated_app() as (app, base):
            self.seed()
            client = app.test_client()
            current = self.get(client)
            data = self.changes(current, theme="editorial")
            with patch("fls_manager.routes.config.native.save_config", side_effect=OSError("disk failure")):
                failed = client.post("/api/config/settings", json=data, headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            self.assertNotIn("disk failure", failed.get_data(as_text=True))
            self.assertFalse(failed.get_json()["saved"])
            self.assertEqual(client.get("/api/config/requests/" + data["request_id"], headers={"X-Token": TOKEN}).get_json()["saved"], False)
            self.assertFalse((base / "data" / "config_apply_state.json").exists())

    def test_receipt_write_failure_after_config_publication_resolves_saved_without_reapplying(self):
        with isolated_app() as (app, base):
            from fls_manager import storage
            self.seed()
            client = app.test_client()
            current = self.get(client)
            data = self.changes(current, theme="editorial")
            receipt_path = base / "data" / "config_requests" / (data["request_id"] + ".json")
            real_write = storage._write_json_unlocked
            receipt_writes = 0

            def fail_completed_receipt(path, value):
                nonlocal receipt_writes
                if path == receipt_path:
                    receipt_writes += 1
                    if receipt_writes == 2:
                        raise OSError("receipt write failed")
                return real_write(path, value)

            with patch.object(storage, "_write_json_unlocked", side_effect=fail_completed_receipt), \
                 patch("fls_manager.routes.config.native.cleanup_logs") as cleanup, \
                 patch("fls_manager.routes.config.native.reload_scheduler") as reload:
                failed_response = client.post("/api/config/settings", json=data, headers={"X-Token": TOKEN})
                status = client.get("/api/config/requests/" + data["request_id"], headers={"X-Token": TOKEN})
            self.assertEqual(failed_response.status_code, 503)
            self.assertTrue(status.get_json()["saved"])
            self.assertEqual(status.get_json()["apply_status"], "unknown")
            cleanup.assert_not_called()
            reload.assert_not_called()

    def test_explicit_apply_retry_is_revision_bound_and_idempotent(self):
        with isolated_app() as (app, _), patch.dict("os.environ", {"FLS_TOKEN": ""}):
            self.seed()
            client = app.test_client()
            current = self.get(client)
            data = {"request_id": "e" * 32, "expected_revision": current.get_json()["revision"]}
            with patch("fls_manager.routes.config.native.cleanup_logs", side_effect=RuntimeError("secret stack")), \
                 patch("fls_manager.routes.config.native.reload_scheduler") as reload:
                first = client.post("/api/config/apply", json=data, headers={"X-Token": TOKEN})
                replay = client.post("/api/config/apply", json=data, headers={"X-Token": TOKEN})
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.get_json()["saved"])
            self.assertEqual(first.get_json()["apply_status"], "failed")
            self.assertNotIn("secret stack", first.get_data(as_text=True))
            self.assertEqual(replay.get_json()["apply_status"], "failed")
            reload.assert_called_once()
            stale = dict(data, request_id="f" * 32, expected_revision="0" * 64)
            self.assertEqual(client.post("/api/config/apply", json=stale, headers={"X-Token": TOKEN}).status_code, 409)

    def test_security_change_invalidates_all_sessions_even_if_receipt_publication_fails(self):
        with isolated_app() as (app, base):
            from fls_manager import storage
            self.seed()
            first, headers = self.session(app)
            second, _ = self.session(app)
            current = self.get(first, token=None)
            data = {"request_id": "7" * 32, "expected_revision": current.get_json()["revision"],
                    "credentials": {"enabled": True, "type": "code"}}
            target = base / "data" / "config_requests" / (data["request_id"] + ".json")
            original = storage._write_json_unlocked

            def reject_completed(path, value):
                if path == target and value.get("state") == "completed":
                    raise OSError("receipt publication rejected")
                return original(path, value)

            with patch.object(storage, "_write_json_unlocked", side_effect=reject_completed):
                result = first.post("/api/config/settings", json=data, headers=headers)
            self.assertEqual(result.status_code, 503)
            self.assertEqual(first.get("/api/config/settings").status_code, 401)
            self.assertEqual(second.get("/api/config/settings").status_code, 401)
            fresh, _ = self.session(app)
            recovered = fresh.get("/api/config/requests/" + data["request_id"]).get_json()
            self.assertTrue(recovered["saved"])
            self.assertEqual(recovered["apply_status"], "unknown")

    def test_web_or_direct_security_changes_invalidate_native_sessions(self):
        with isolated_app() as (app, _):
            from fls_manager.config import save_config
            self.seed()
            client, _ = self.session(app)
            save_config({"security_verify_enabled": True})
            self.assertEqual(client.get("/api/config/settings").status_code, 401)

    def test_real_totp_enrollment_keep_and_disable_workflow(self):
        with isolated_app() as (app, _), patch.dict("os.environ", {"FLS_TOKEN": ""}):
            from fls_manager.config import load_config
            from fls_manager.security import totp_now
            self.seed()
            client, headers = self.session(app)
            secret = client.post("/api/config/totp/enrollment", json={}, headers=headers).get_json()["secret"]
            current = self.get(client, token=None).get_json()
            with patch("fls_manager.routes.config.native.cleanup_logs"), patch("fls_manager.routes.config.native.reload_scheduler"):
                result = client.post("/api/config/settings", json={
                    "request_id": "8" * 32, "expected_revision": current["revision"],
                    "credentials": {"enabled": True, "type": "totp", "totp_action": "replace",
                                    "totp_secret": secret, "totp_code": totp_now(secret)}}, headers=headers)
                self.assertEqual(result.status_code, 200, result.get_json())
                fresh, csrf = self.session(app)
                snapshot = self.get(fresh, token=None)
                preserved = fresh.post("/api/config/settings", json=self.changes(snapshot, port=5701), headers=csrf)
                self.assertEqual(preserved.status_code, 200, preserved.get_json())
                self.assertEqual(load_config()["totp_secret"], secret)
                latest = self.get(fresh, token=None).get_json()
                cleared = fresh.post("/api/config/settings", json={
                    "request_id": "9" * 32, "expected_revision": latest["revision"],
                    "credentials": {"enabled": False, "totp_action": "clear"}}, headers=csrf)
                self.assertEqual(cleared.status_code, 200, cleared.get_json())
            self.assertEqual(load_config()["totp_secret"], "")
            self.assertFalse(load_config()["security_verify_enabled"])

    def test_token_clear_is_explicit_and_retains_no_credentials_in_receipt(self):
        with isolated_app() as (app, base), patch.dict("os.environ", {"FLS_TOKEN": ""}):
            from fls_manager.config import load_config
            self.seed()
            client, headers = self.session(app)
            current = self.get(client, token=None).get_json()
            with patch("fls_manager.routes.config.native.cleanup_logs"), patch("fls_manager.routes.config.native.reload_scheduler"):
                result = client.post("/api/config/settings", json={
                    "request_id": "0" * 32, "expected_revision": current["revision"],
                    "credentials": {"token_action": "clear"}}, headers=headers)
            self.assertTrue(result.get_json()["saved"])
            self.assertTrue(result.get_json()["reauth_required"])
            self.assertEqual(load_config()["admin_token"], "")
            self.assertEqual(client.get("/api/config/settings").status_code, 403)
            self.assertNotIn(TOKEN, (base / "data/config_requests" / ("0" * 32 + ".json")).read_text())

    def test_invalid_totp_blank_token_and_corrupt_replay_have_no_side_effects(self):
        with isolated_app() as (app, base), patch.dict("os.environ", {"FLS_TOKEN": ""}):
            self.seed()
            client, headers = self.session(app)
            current = self.get(client, token=None)
            for credentials in ({"token_action": "replace", "admin_token": "  "},
                                {"enabled": True, "type": "totp", "totp_action": "replace",
                                 "totp_secret": "!" * 20, "totp_code": "123456"}):
                result = client.post("/api/config/settings", json={
                    "request_id": "6" * 32, "expected_revision": current.get_json()["revision"],
                    "credentials": credentials}, headers=headers)
                self.assertEqual(result.status_code, 400)
            path = base / "data/config_requests" / ("a" * 32 + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{")
            result = client.post("/api/config/settings", json=self.changes(current, theme="editorial"), headers=headers)
            self.assertEqual(result.status_code, 503)

    def test_corrupt_request_receipt_is_not_reported_as_success(self):
        with isolated_app() as (app, base):
            self.seed()
            client = app.test_client()
            path = "/api/config/requests/" + "a" * 32
            receipt = base / "data" / "config_requests" / ("a" * 32 + ".json")
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({"request_id": "a" * 32, "state": "completed", "saved": True}))
            result = client.get(path, headers={"X-Token": TOKEN})
            self.assertEqual(result.status_code, 503)
            self.assertNotEqual(result.get_json().get("saved"), True)


if __name__ == "__main__":
    unittest.main()
