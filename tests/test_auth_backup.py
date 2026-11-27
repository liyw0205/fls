import contextlib
from datetime import datetime
import io
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import unittest
from urllib.parse import urlencode
import warnings
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def isolated_fls_env(token=None):
    keys = (
        "FLS_BASE_DIR",
        "FLS_TOKEN",
        "FLS_SECRET_KEY",
        "FLS_TRUSTED_PROXIES",
        "FLS_ALLOW_QUERY_TOKEN",
        "PYTHONDONTWRITEBYTECODE",
    )
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-test-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_SECRET_KEY"] = "unit-test-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ.pop("FLS_TRUSTED_PROXIES", None)
        os.environ.pop("FLS_ALLOW_QUERY_TOKEN", None)

        if token is None:
            os.environ.pop("FLS_TOKEN", None)
        else:
            os.environ["FLS_TOKEN"] = token

        purge_fls_modules()

        try:
            # Authentication/backup tests do not send notifications or fetch releases.
            with patch("fls_manager.app.dispatch_frontend_open_event"):
                yield Path(temp_dir)
        finally:
            shutdown_scheduler()
            purge_fls_modules()

            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


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


def load_app():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from fls_manager.app import create_app

    return create_app()


def add_tar_file(tar, name, content=b"data"):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    tar.addfile(info, io.BytesIO(content))


def add_tar_member(tar, name, member_type, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.linkname = linkname
    info.size = 0
    tar.addfile(info)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def login_client(client, token="unit-token"):
    login_page = client.get("/login")
    csrf_token = csrf_token_from_html(login_page.get_data(as_text=True))
    response = client.post(
        "/login",
        data={"token": token, "csrf_token": csrf_token},
    )
    if response.status_code == 302:
        client.get(response.headers["Location"])


def csrf_token_from_html(html):
    meta = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    hidden = re.search(r'name="csrf_token" value="([^"]+)"', html)

    if not meta or not hidden:
        raise AssertionError("CSRF token not found in rendered HTML")

    if meta.group(1) != hidden.group(1):
        raise AssertionError("CSRF meta and hidden input tokens differ")

    return hidden.group(1)


class AuthFlowTests(unittest.TestCase):
    def test_api_requires_setup_when_token_missing(self):
        with isolated_fls_env(token=None):
            client = load_app().test_client()
            response = client.get("/api/status")

            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.get_json()["ok"])
            self.assertIn("Token", response.get_json()["msg"])

    def test_page_redirects_to_setup_when_token_missing(self):
        with isolated_fls_env(token=None):
            client = load_app().test_client()
            response = client.get("/tasks")

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/setup")

    def test_api_accepts_x_token_header(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            response = client.get("/api/status", headers={"X-Token": "unit-token"})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), [])

    def test_api_rejects_wrong_query_token(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            with patch.dict(os.environ, {"FLS_ALLOW_QUERY_TOKEN": "1"}):
                response = client.get("/api/status?token=wrong")

            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.get_json()["ok"])

    def test_api_query_token_is_disabled_by_default(self):
        with isolated_fls_env(token="unit-token"):
            response = load_app().test_client().get(
                "/api/status?token=unit-token"
            )

            self.assertEqual(response.status_code, 403)
            self.assertIn("X-Token", response.get_json()["msg"])

    def test_api_query_token_opt_in_redirects_to_clean_url(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            with patch.dict(os.environ, {"FLS_ALLOW_QUERY_TOKEN": "1"}):
                response = client.get("/api/status?token=unit-token")

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/api/status")
            self.assertNotIn("unit-token", response.headers.get("Location", ""))
            api_response = client.get(response.headers["Location"])
            self.assertEqual(api_response.status_code, 200)
            self.assertEqual(api_response.get_json(), [])

    def test_query_token_is_disabled_by_default(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            response = client.get("/tasks?token=unit-token")

            self.assertEqual(response.status_code, 302)
            location = response.headers.get("Location", "")
            self.assertIn("/login", location)
            self.assertNotIn("unit-token", location)

    def test_query_token_sets_session_and_redirects_to_clean_url(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            with patch.dict(os.environ, {"FLS_ALLOW_QUERY_TOKEN": "1"}):
                response = client.get("/tasks?token=unit-token&q=abc")

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/tasks?q=abc")
            self.assertIn("session=", response.headers.get("Set-Cookie", ""))

    def test_login_next_accepts_internal_path_and_rejects_external_targets(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()

            for next_url, expected in (
                ("/tasks?q=abc", "/tasks?q=abc"),
                ("https://evil.example/", "/"),
                ("//evil.example/", "/"),
                ("/\\\\evil.example", "/"),
                ("/%2f%2fevil.example", "/"),
                ("/%252f%252fevil.example", "/"),
                ("/tasks/../login", "/"),
                ("/%252e%252e/login", "/"),
            ):
                with self.subTest(next_url=next_url):
                    login_page = client.get("/login")
                    csrf_token = csrf_token_from_html(
                        login_page.get_data(as_text=True)
                    )
                    response = client.post(
                        "/login?" + urlencode({"next": next_url}),
                        data={
                            "token": "unit-token",
                            "csrf_token": csrf_token,
                        },
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers.get("Location"), expected)
                    client.get("/logout")

    def test_client_ip_ignores_forwarded_header_from_untrusted_peer(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes import auth_routes

            with patch.dict(os.environ, {"FLS_TRUSTED_PROXIES": ""}), \
                    load_app().test_request_context(
                "/login",
                headers={"X-Forwarded-For": "198.51.100.10"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            ):
                self.assertEqual(auth_routes.client_ip(), "127.0.0.1")

    def test_client_ip_uses_forwarded_address_from_trusted_proxy(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes import auth_routes

            with patch.dict(os.environ, {"FLS_TRUSTED_PROXIES": "127.0.0.1/32"}), \
                    load_app().test_request_context(
                "/login",
                headers={"X-Forwarded-For": "198.51.100.10, 127.0.0.1"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            ):
                self.assertEqual(auth_routes.client_ip(), "198.51.100.10")

    def test_client_ip_walks_trusted_ipv6_proxy_chain_from_right(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes import auth_routes

            with patch.dict(
                os.environ,
                {"FLS_TRUSTED_PROXIES": "2001:db8::/64"},
            ), load_app().test_request_context(
                "/login",
                headers={
                    "X-Forwarded-For": "2001:db8:1::7, 2001:db8::1, 2001:db8::2",
                },
                environ_base={"REMOTE_ADDR": "2001:db8::3"},
            ):
                self.assertEqual(auth_routes.client_ip(), "2001:db8:1::7")

    def test_client_ip_ignores_malformed_forwarded_chain(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes import auth_routes

            with patch.dict(
                os.environ,
                {"FLS_TRUSTED_PROXIES": "127.0.0.1/32"},
            ), load_app().test_request_context(
                "/login",
                headers={"X-Forwarded-For": "198.51.100.10, invalid"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            ):
                self.assertEqual(auth_routes.client_ip(), "127.0.0.1")


class NativeAuthApiTests(unittest.TestCase):
    def _login(self, client, token="unit-token", remember=False):
        return client.post(
            "/api/auth/session",
            json={"token": token, "remember": remember},
        )

    def test_native_login_validates_json_token_and_rate_limits_failures(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()

            malformed = client.post("/api/auth/session", data="token=unit-token")
            self.assertEqual(malformed.status_code, 415)

            invalid = self._login(client, token="wrong")
            self.assertEqual(invalid.status_code, 403)
            self.assertFalse(invalid.get_json()["ok"])

            self._login(client, token="wrong")
            locked = self._login(client, token="wrong")
            self.assertEqual(locked.status_code, 429)
            self.assertEqual(self._login(client).status_code, 429)

    def test_native_login_sets_session_cookie_and_session_authenticates_api(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()

            response = self._login(client, remember=True)

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["authenticated"])
            self.assertFalse(payload["requires_verification"])
            self.assertTrue(payload["remember"])
            self.assertTrue(payload["csrf_token"])
            cookie = response.headers.get("Set-Cookie", "")
            self.assertIn("fls_native_session=", cookie)
            self.assertFalse(cookie.startswith("session="))
            self.assertIn("HttpOnly", cookie)
            self.assertNotIn("unit-token", cookie)
            self.assertEqual(client.get("/api/status").status_code, 200)

            from fls_manager.native_sessions import NATIVE_SESSION_FILE
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(NATIVE_SESSION_FILE.stat().st_mode), 0o600)
            self.assertNotIn(b"unit-token", NATIVE_SESSION_FILE.read_bytes())

            with patch.dict(os.environ, {"FLS_TOKEN": "rotated-token"}):
                rotated = client.get("/api/status")
            self.assertEqual(rotated.status_code, 401)

    def test_native_login_requires_second_factor_and_verification_unlocks_api(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.config import save_config
            from fls_manager.routes import auth_routes

            save_config({"security_verify_enabled": True, "security_verify_type": "code"})
            with patch.object(auth_routes, "create_and_send_security_code"):
                client = load_app().test_client()
                login = self._login(client)

            self.assertEqual(login.status_code, 202)
            self.assertTrue(login.get_json()["requires_verification"])
            csrf = login.get_json()["csrf_token"]
            self.assertEqual(client.get("/api/status").status_code, 401)

            malformed = client.post(
                "/api/auth/verify",
                json={"code": 123456},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(malformed.status_code, 400)

            with patch.object(auth_routes, "random_code_valid", return_value=(True, "验证成功")):
                verified = client.post(
                    "/api/auth/verify",
                    json={"code": "123456"},
                    headers={"X-CSRF-Token": csrf},
                )

            self.assertEqual(verified.status_code, 200)
            self.assertTrue(verified.get_json()["authenticated"])
            self.assertEqual(client.get("/api/status").status_code, 200)

    def test_native_resend_is_available_before_verification_and_requires_csrf(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.config import save_config
            from fls_manager.routes import auth_routes

            save_config({"security_verify_enabled": True, "security_verify_type": "code"})
            with patch.object(auth_routes, "create_and_send_security_code"):
                client = load_app().test_client()
                login = self._login(client)

            self.assertEqual(login.status_code, 202)
            response = client.post("/api/auth/resend-code")
            self.assertEqual(response.status_code, 400)
            self.assertIn("CSRF", response.get_json()["msg"])

            with patch.object(auth_routes, "create_and_send_security_code") as send_code:
                response = client.post(
                    "/api/auth/resend-code",
                    headers={"X-CSRF-Token": login.get_json()["csrf_token"]},
                )

            self.assertEqual(response.status_code, 200)
            send_code.assert_called_once()

    def test_native_totp_verification_uses_configured_second_factor(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.config import save_config
            from fls_manager.routes import auth_routes

            save_config({
                "security_verify_enabled": True,
                "security_verify_type": "totp",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            })
            client = load_app().test_client()
            login = self._login(client)
            self.assertEqual(login.status_code, 202)
            self.assertEqual(login.get_json()["verification_type"], "totp")
            headers = {"X-CSRF-Token": login.get_json()["csrf_token"]}

            with patch.object(auth_routes, "verify_totp", return_value=False) as verify:
                rejected = client.post(
                    "/api/auth/verify",
                    json={"code": "000000"},
                    headers=headers,
                )
            self.assertEqual(rejected.status_code, 403)
            verify.assert_called_once_with("JBSWY3DPEHPK3PXP", "000000")

            with patch.object(auth_routes, "verify_totp", return_value=True):
                accepted = client.post(
                    "/api/auth/verify",
                    json={"code": "123456"},
                    headers=headers,
                )
            self.assertEqual(accepted.status_code, 200)
            self.assertTrue(accepted.get_json()["authenticated"])

    def test_native_logout_requires_csrf_and_invalidates_session(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            login = self._login(client)
            csrf = login.get_json()["csrf_token"]

            denied = client.post("/api/auth/logout")
            self.assertEqual(denied.status_code, 400)

            response = client.post(
                "/api/auth/logout",
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(client.get("/api/status").status_code, 401)


class NativeTaskApiTests(unittest.TestCase):
    def test_task_detail_returns_task_runtime_and_bounded_safe_history(self):
        with isolated_fls_env(token="unit-token"):
            app = load_app()
            client = app.test_client()
            from fls_manager.models import save_tasks
            from fls_manager.paths import TASK_HISTORY_FILE

            save_tasks([{
                "id": "daily",
                "name": "每日任务",
                "remark": "测试备注",
                "command": "python daily.py",
                "cron": "0 8 * * *",
                "env": {"API_KEY": "task-secret"},
                "legacy_secret": "must-not-leak",
            }])
            TASK_HISTORY_FILE.write_text(json.dumps([
                {
                    "id": "history-1",
                    "task_id": "daily",
                    "start_at": "2026-11-25 08:00:00",
                    "end_at": "2026-11-25 08:00:02",
                    "duration_seconds": 2,
                    "return_code": 0,
                    "status": "success",
                    "source": "manual",
                    "message": "完成",
                    "log_file": "/private/path/secret.log",
                }
            ]), encoding="utf-8")

            login = client.post(
                "/api/auth/session",
                json={"token": "unit-token", "remember": False},
            )
            self.assertEqual(login.status_code, 200)

            denied = app.test_client().get("/api/task/daily")
            self.assertEqual(denied.status_code, 401)

            response = client.get("/api/task/daily")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["task"]["env"], {"API_KEY": "task-secret"})
            self.assertNotIn("legacy_secret", payload["task"])
            self.assertEqual(payload["runtime"]["running"], False)
            self.assertEqual(payload["history"][0]["id"], "history-1")
            self.assertNotIn("log_file", payload["history"][0])

            missing = client.get("/api/task/missing")
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.get_json(), {"ok": False, "msg": "任务不存在"})

    def test_task_log_json_is_bounded_authenticated_and_uncached(self):
        with isolated_fls_env(token="unit-token"):
            app = load_app()
            from fls_manager.models import save_tasks
            from fls_manager.paths import LOG_DIR

            save_tasks([{"id": "daily", "name": "daily", "command": "task daily.py"}])
            log_path = LOG_DIR / "daily-20261128.log"
            log_path.write_text("first\nsecond\nthird\n", encoding="utf-8")
            denied = app.test_client().get("/api/task/daily/log")
            self.assertEqual(denied.status_code, 401)

            response = app.test_client().get(
                "/api/task/daily/log?lines=2",
                headers={"X-Token": "unit-token"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(response.get_json()["log"], "second\nthird")

            invalid = app.test_client().get(
                "/api/task/daily/log?lines=1001",
                headers={"X-Token": "unit-token"},
            )
            self.assertEqual(invalid.status_code, 400)
            self.assertFalse(invalid.get_json()["ok"])


class NativeTaskMutationApiTests(unittest.TestCase):
    def _task_payload(self, **changes):
        task = {
            "name": "每日任务",
            "remark": "主账号",
            "command": "python daily.py",
            "cron": "0 8 * * *",
            "config_path": "",
            "collection_id": "",
            "enabled": True,
            "env": {"API_KEY": "task-secret"},
            "proxy_id": "",
            "notify": {"mode": "none", "ids": []},
            "random_delay": {"mode": "custom", "seconds": 5},
            "retry": {"attempts": 2, "interval_seconds": 30},
        }
        task.update(changes)
        return task

    def test_task_form_options_never_expose_proxy_or_notification_secrets(self):
        with isolated_fls_env(token="unit-token"):
            app = load_app()
            from fls_manager.config import save_config
            from fls_manager.models import save_collections, save_proxies

            save_collections([{"id": "group-1", "name": "签到"}])
            save_proxies([{
                "id": "proxy-1",
                "name": "代理 A",
                "type": "http",
                "host": "127.0.0.1",
                "port": "8080",
                "password": "proxy-secret",
            }])
            save_config({"notify_items": [{
                "id": "notify-1",
                "name": "通知 A",
                "channel": "serverj",
                "enabled": True,
                "config": {"sendkey": "notify-secret"},
            }]})

            response = app.test_client().get(
                "/api/tasks/form-options",
                headers={"X-Token": "unit-token"},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["collections"][0]["id"], "group-1")
            self.assertEqual(payload["proxies"][0]["id"], "proxy-1")
            self.assertEqual(payload["notifications"][0]["id"], "notify-1")
            serialized = json.dumps(payload)
            self.assertNotIn("proxy-secret", serialized)
            self.assertNotIn("notify-secret", serialized)

    def test_native_task_create_requires_csrf_and_is_idempotent(self):
        with isolated_fls_env(token="unit-token"):
            app = load_app()
            client = app.test_client()
            from fls_manager.models import load_tasks
            from fls_manager.routes.tasks import pages

            login = client.post(
                "/api/auth/session",
                json={"token": "unit-token", "remember": False},
            )
            self.assertEqual(login.status_code, 200)
            csrf = login.get_json()["csrf_token"]
            body = {"request_id": "a" * 32, "task": self._task_payload()}

            denied = client.post("/api/tasks", json=body)
            self.assertEqual(denied.status_code, 400)

            with patch.object(pages, "reload_scheduler") as reload_scheduler:
                created = client.post(
                    "/api/tasks",
                    json=body,
                    headers={"X-CSRF-Token": csrf},
                )
                replay = client.post(
                    "/api/tasks",
                    json=body,
                    headers={"X-CSRF-Token": csrf},
                )

            self.assertEqual(created.status_code, 201)
            self.assertTrue(created.get_json()["created"])
            self.assertEqual(replay.status_code, 200)
            self.assertFalse(replay.get_json()["created"])
            self.assertEqual(len(load_tasks()), 1)
            self.assertEqual(reload_scheduler.call_count, 2)

    def test_native_task_create_rejects_invalid_fields_without_saving(self):
        with isolated_fls_env(token="unit-token"):
            app = load_app()
            from fls_manager.models import load_tasks

            cases = (
                ("cron", {"cron": "not a cron"}),
                ("config_path", {"config_path": "../../outside.yml"}),
                ("notify", {"notify": {"mode": "custom", "ids": ["missing"]}}),
                ("retry", {"retry": {"attempts": 6, "interval_seconds": 30}}),
            )
            client = app.test_client()
            for suffix, changes in cases:
                with self.subTest(field=suffix):
                    body = {
                        "request_id": (suffix.encode().hex() * 16)[:32],
                        "task": self._task_payload(**changes),
                    }
                    response = client.post(
                        "/api/tasks",
                        json=body,
                        headers={"X-Token": "unit-token"},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.get_json()["ok"])
            self.assertEqual(load_tasks(), [])

    def test_native_task_update_detects_stale_edits_and_supports_replay(self):
        with isolated_fls_env(token="unit-token"):
            app = load_app()
            client = app.test_client()
            from fls_manager.models import load_tasks, save_tasks
            from fls_manager.routes.tasks import pages

            save_tasks([{
                "id": "daily",
                "name": "每日任务",
                "command": "python old.py",
                "updated_at": "revision-1",
            }])
            body = {
                "expected_updated_at": "revision-1",
                "task": self._task_payload(command="python new.py"),
            }

            with patch.object(pages, "reload_scheduler"):
                updated = client.put(
                    "/api/tasks/daily",
                    json=body,
                    headers={"X-Token": "unit-token"},
                )
                replay = client.put(
                    "/api/tasks/daily",
                    json=body,
                    headers={"X-Token": "unit-token"},
                )
                stale = client.put(
                    "/api/tasks/daily",
                    json={
                        "expected_updated_at": "revision-1",
                        "task": self._task_payload(command="python conflicting.py"),
                    },
                    headers={"X-Token": "unit-token"},
                )

            self.assertEqual(updated.status_code, 200)
            self.assertTrue(updated.get_json()["changed"])
            self.assertEqual(replay.status_code, 200)
            self.assertFalse(replay.get_json()["changed"])
            self.assertEqual(stale.status_code, 409)
            self.assertIn("其他操作修改", stale.get_json()["msg"])
            self.assertEqual(load_tasks()[0]["command"], "python new.py")

    def test_task_save_reports_scheduler_reload_failure_as_saved(self):
        with isolated_fls_env(token="unit-token"):
            app = load_app()
            from fls_manager.models import load_tasks
            from fls_manager.routes.tasks import pages

            with patch.object(pages, "reload_scheduler", side_effect=RuntimeError("scheduler offline")):
                response = app.test_client().post(
                    "/api/tasks",
                    json={"request_id": "b" * 32, "task": self._task_payload()},
                    headers={"X-Token": "unit-token"},
                )
                replay = app.test_client().post(
                    "/api/tasks",
                    json={"request_id": "b" * 32, "task": self._task_payload()},
                    headers={"X-Token": "unit-token"},
                )

            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.get_json()["saved"])
            self.assertTrue(response.get_json()["reload_required"])
            self.assertEqual(replay.status_code, 503)
            self.assertTrue(replay.get_json()["saved"])
            self.assertEqual(len(load_tasks()), 1)


class CsrfSafetyTests(unittest.TestCase):
    def test_layout_injects_csrf_meta_and_post_form_hidden_input(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            login_client(client)

            response = client.get("/task/new")
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            token = csrf_token_from_html(html)
            self.assertGreater(len(token), 20)

    def test_session_post_requires_csrf_token(self):
        with isolated_fls_env(token="unit-token") as base_dir:
            client = load_app().test_client()
            with patch("fls_manager.app.dispatch_frontend_open_event"):
                login_client(client)

            response = client.post(
                "/task/new",
                data={
                    "name": "csrf-task",
                    "command": "task demo.py",
                    "enabled": "1",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("CSRF token", response.get_data(as_text=True))
            self.assertFalse((base_dir / "data" / "tasks.json").exists())

    def test_session_post_accepts_valid_csrf_token(self):
        with isolated_fls_env(token="unit-token") as base_dir:
            client = load_app().test_client()
            login_client(client)

            page = client.get("/task/new")
            token = csrf_token_from_html(page.get_data(as_text=True))

            response = client.post(
                "/task/new",
                data={
                    "csrf_token": token,
                    "name": "csrf-task",
                    "command": "task demo.py",
                    "enabled": "1",
                },
            )

            self.assertEqual(response.status_code, 302)

            tasks = read_json(base_dir / "data" / "tasks.json")
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["name"], "csrf-task")

    def test_x_token_post_bypasses_csrf_for_api_clients(self):
        with isolated_fls_env(token="unit-token") as base_dir:
            from fls_manager import paths

            write_json(
                paths.TASK_FILE,
                [
                    {
                        "id": "t1",
                        "name": "copy-source",
                        "command": "task demo.py",
                        "enabled": True,
                        "notify": {"mode": "none", "ids": []},
                        "random_delay": {"mode": "none", "seconds": 0},
                        "retry": {"attempts": 0, "interval_seconds": 60},
                    }
                ],
            )

            response = load_app().test_client().post(
                "/api/task/action/copy/t1",
                headers={"X-Token": "unit-token"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(len(read_json(base_dir / "data" / "tasks.json")), 2)

    def test_destructive_routes_reject_get_requests(self):
        with isolated_fls_env(token="unit-token") as base_dir:
            from fls_manager import paths

            log_file = base_dir / "log" / "demo.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("demo", encoding="utf-8")

            write_json(
                paths.COLLECTION_FILE,
                [
                    {
                        "id": "c1",
                        "name": "合集",
                        "remark": "",
                        "created_at": "",
                        "updated_at": "",
                    }
                ],
            )
            write_json(
                paths.TASK_FILE,
                [
                    {
                        "id": "t1",
                        "name": "task",
                        "command": "task demo.py",
                        "collection_id": "c1",
                        "enabled": True,
                        "pinned": False,
                    }
                ],
            )

            client = load_app().test_client()

            for url in (
                "/logfile/delete/demo.log",
                "/collection/delete/c1",
                "/task/pin/t1",
                "/task/toggle/t1",
                "/task/collection/clear/t1",
                "/run/t1",
                "/stop/t1",
            ):
                with self.subTest(url=url):
                    response = client.get(url, headers={"X-Token": "unit-token"})
                    self.assertEqual(response.status_code, 405)

            self.assertTrue(log_file.exists())
            self.assertEqual(read_json(base_dir / "data" / "collections.json")[0]["id"], "c1")
            self.assertTrue(read_json(base_dir / "data" / "tasks.json")[0]["enabled"])
            self.assertFalse(read_json(base_dir / "data" / "tasks.json")[0]["pinned"])


class BackupSafetyTests(unittest.TestCase):
    def assert_tar_rejected(self, member_builder):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_tar

            with tempfile.TemporaryDirectory(prefix="fls-tar-bad-") as dest:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as tar:
                    member_builder(tar)

                data.seek(0)
                with tarfile.open(fileobj=data, mode="r:") as tar:
                    with self.assertRaises(RuntimeError):
                        safe_extract_tar(tar, dest)

    def test_backup_safe_file_stays_inside_backup_dir(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import backup_safe_file

            with self.assertRaises(ValueError):
                backup_safe_file("../../demo.tar.gz")
            target = backup_safe_file("demo.tar.gz")
            self.assertEqual(target.name, "demo.tar.gz")
            self.assertIn("backups", target.parts)

    def test_backup_filter_excludes_runtime_native_sessions_and_sidecars(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import _backup_tar_filter

            filter_entry = _backup_tar_filter(MagicMock(is_set=MagicMock(return_value=False)))
            excluded = (
                "data/backups",
                "data/backups/previous.tar.gz",
                "data/native_sessions.sqlite3",
                "data/native_sessions.sqlite3-wal",
                "data/native_sessions.sqlite3-shm",
                "data/native_sessions.sqlite3-journal",
            )

            for name in excluded:
                with self.subTest(name=name):
                    self.assertIsNone(filter_entry(tarfile.TarInfo(name)))

            self.assertIsNotNone(filter_entry(tarfile.TarInfo("data/config.json")))

    def test_api_backup_job_returns_stable_progress_fields(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import BACKUP_JOBS

            info = {
                "id": "job-1",
                "items": ["data", "scripts"],
                "type_text": "配置 + 脚本",
                "running": False,
                "status": "已完成",
                "filename": "fls-backup-all.tar.gz",
                "size": 2048,
                "size_text": "2.0 KB",
                "error": "",
                "created_at": "2026-07-14 10:00:00",
                "updated_at": "2026-07-14 10:01:00",
            }
            BACKUP_JOBS["job-1"] = info

            client = load_app().test_client()
            response = client.get(
                "/api/backup/job/job-1",
                headers={"X-Token": "unit-token"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"ok": True, **info})

            missing = client.get(
                "/api/backup/job/missing",
                headers={"X-Token": "unit-token"},
            )
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(
                missing.get_json(),
                {"ok": False, "msg": "任务不存在"},
            )

    def test_api_backup_job_recovers_persisted_running_as_unknown_after_restart(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import (
                BACKUP_JOBS,
                BACKUP_JOBS_FILE,
            )

            snapshot = {
                "id": "restart-job",
                "items": ["data"],
                "type_text": "仅配置",
                "running": True,
                "status": "正在压缩",
                "filename": "",
                "size": 0,
                "size_text": "-",
                "error": "",
                "created_at": "2026-07-14 10:00:00",
                "updated_at": "2026-07-14 10:01:00",
            }
            BACKUP_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            BACKUP_JOBS_FILE.write_text(
                json.dumps({"restart-job": snapshot}, ensure_ascii=False),
                encoding="utf-8",
            )
            BACKUP_JOBS.clear()

            response = load_app().test_client().get(
                "/api/backup/job/restart-job",
                headers={"X-Token": "unit-token"},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["running"])
            self.assertEqual(payload["status"], "结果未知")
            self.assertIn("面板重启后无法确认", payload["error"])
            persisted = read_json(BACKUP_JOBS_FILE)["restart-job"]
            self.assertEqual(persisted["status"], "结果未知")
            self.assertFalse(persisted["running"])

    def test_api_backup_cancel_rejects_terminal_and_restart_unknown_jobs(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import (
                BACKUP_JOBS,
                BACKUP_JOBS_FILE,
            )
            import threading

            BACKUP_JOBS["finished-job"] = {
                "id": "finished-job",
                "running": False,
                "status": "失败",
            }
            client = load_app().test_client()
            response = client.post(
                "/api/backup/cancel/finished-job",
                headers={"X-Token": "unit-token"},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["msg"], "备份任务已结束")

            # A stale cancellation handle must not make a terminal result cancellable.
            BACKUP_JOBS["finished-with-handle"] = {
                "id": "finished-with-handle",
                "running": False,
                "status": "已完成",
            }
            from fls_manager.routes.backup._common import BACKUP_CANCEL_EVENTS
            BACKUP_CANCEL_EVENTS["finished-with-handle"] = threading.Event()
            response = client.post(
                "/api/backup/cancel/finished-with-handle",
                headers={"X-Token": "unit-token"},
            )
            self.assertEqual(response.status_code, 409)
            self.assertFalse(BACKUP_CANCEL_EVENTS["finished-with-handle"].is_set())

            BACKUP_JOBS.clear()
            BACKUP_JOBS_FILE.write_text(
                json.dumps({"unknown-job": {"id": "unknown-job", "running": True}}),
                encoding="utf-8",
            )
            response = client.post(
                "/api/backup/cancel/unknown-job",
                headers={"X-Token": "unit-token"},
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("状态未知", response.get_json()["msg"])

    def test_backup_page_exposes_cancel_action_and_unknown_result_copy(self):
        with isolated_fls_env(token="unit-token"):
            response = load_app().test_client().get(
                "/backup",
                headers={"X-Token": "unit-token"},
            )

            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("flsCancelBackup", html)
            self.assertIn("/api/backup/cancel/", html)
            self.assertIn("状态读取失败", html)
            self.assertIn("重新创建备份", html)

    def test_api_backup_list_returns_stable_file_fields_in_newest_order(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import backup_dir

            target_dir = backup_dir()
            older = target_dir / "older.tar.gz"
            newer = target_dir / "newer.tar.gz"
            ignored = target_dir / "ignored.zip"
            nested = target_dir / "nested.tar.gz"

            older.write_bytes(b"o" * 1024)
            newer.write_bytes(b"n" * 2048)
            ignored.write_bytes(b"ignored")
            nested.mkdir()
            os.utime(older, (1700000000, 1700000000))
            os.utime(newer, (1700000100, 1700000100))

            response = load_app().test_client().get(
                "/api/backup/list",
                headers={"X-Token": "unit-token"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content_type, "application/json")
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(
                [item["name"] for item in payload["items"]],
                ["newer.tar.gz", "older.tar.gz"],
            )

            expected = {
                "newer.tar.gz": (newer, 2048, "2.0 KB"),
                "older.tar.gz": (older, 1024, "1.0 KB"),
            }

            for item in payload["items"]:
                path, size, size_text = expected[item["name"]]
                mtime = path.stat().st_mtime
                self.assertEqual(
                    set(item),
                    {"name", "version", "size", "size_text", "mtime", "mtime_text"},
                )
                self.assertRegex(item["version"], r"^[a-f0-9]{64}$")
                self.assertEqual(item["size"], size)
                self.assertEqual(item["size_text"], size_text)
                self.assertEqual(item["mtime"], mtime)
                self.assertEqual(
                    item["mtime_text"],
                    datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                )

    def test_backup_symlink_is_not_listed_downloaded_or_deleted(self):
        with isolated_fls_env(token="unit-token") as base_dir:
            from fls_manager.routes.backup._common import backup_dir

            outside = base_dir / "outside.tar.gz"
            outside.write_bytes(b"secret backup")
            link = backup_dir() / "linked.tar.gz"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            client = load_app().test_client()
            listed = client.get("/api/backup/list", headers={"X-Token": "unit-token"})
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(listed.get_json()["items"], [])

            downloaded = client.get(
                "/backup/download/linked.tar.gz",
                headers={"X-Token": "unit-token"},
            )
            self.assertEqual(downloaded.status_code, 404)

            deleted = client.post(
                "/api/backup/delete",
                data={"filename": "linked.tar.gz"},
                headers={"X-Token": "unit-token"},
            )
            self.assertEqual(deleted.status_code, 400)
            self.assertFalse(deleted.get_json()["ok"])
            self.assertTrue(link.is_symlink())
            self.assertEqual(outside.read_bytes(), b"secret backup")

    def test_api_backup_delete_missing_file_is_idempotent(self):
        with isolated_fls_env(token="unit-token"):
            response = load_app().test_client().post(
                "/api/backup/delete",
                data={"filename": "already-gone.tar.gz", "version": "a" * 64},
                headers={"X-Token": "unit-token"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content_type, "application/json")
            self.assertEqual(
                response.get_json(),
                {"ok": True, "msg": "原备份已不存在，未重复删除", "deleted": True},
            )

    def test_safe_extract_zip_accepts_regular_paths(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_zip

            with tempfile.TemporaryDirectory(prefix="fls-zip-ok-") as dest:
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as zf:
                    zf.writestr("data/config.json", "{}")

                data.seek(0)
                with zipfile.ZipFile(data, "r") as zf:
                    safe_extract_zip(zf, dest)

                self.assertTrue((Path(dest) / "data" / "config.json").exists())

    def test_safe_extract_zip_rejects_traversal(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_zip

            with tempfile.TemporaryDirectory(prefix="fls-zip-bad-") as dest:
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as zf:
                    zf.writestr("../evil.txt", "bad")

                data.seek(0)
                with zipfile.ZipFile(data, "r") as zf:
                    with self.assertRaises(RuntimeError):
                        safe_extract_zip(zf, dest)

    def test_safe_extract_zip_rejects_absolute_and_backslash_paths(self):
        cases = [
            "/tmp/evil.txt",
            "C:/evil.txt",
            "..\\evil.txt",
        ]

        for name in cases:
            with self.subTest(name=name):
                with isolated_fls_env(token="unit-token"):
                    from fls_manager.routes.backup._common import safe_extract_zip

                    with tempfile.TemporaryDirectory(prefix="fls-zip-bad-") as dest:
                        data = io.BytesIO()
                        with zipfile.ZipFile(data, "w") as zf:
                            zf.writestr(name, "bad")

                        data.seek(0)
                        with zipfile.ZipFile(data, "r") as zf:
                            with self.assertRaises(RuntimeError):
                                safe_extract_zip(zf, dest)

    def test_safe_extract_zip_rejects_symlink_and_special_members(self):
        cases = [
            ("data/link", stat.S_IFLNK),
            ("data/pipe", stat.S_IFIFO),
            ("data/chardev", stat.S_IFCHR),
        ]

        for name, member_type in cases:
            with self.subTest(name=name, member_type=member_type):
                with isolated_fls_env(token="unit-token"):
                    from fls_manager.routes.backup._common import safe_extract_zip

                    with tempfile.TemporaryDirectory(prefix="fls-zip-special-") as dest:
                        data = io.BytesIO()
                        with zipfile.ZipFile(data, "w") as zf:
                            info = zipfile.ZipInfo(name)
                            info.create_system = 3
                            info.external_attr = (member_type | 0o777) << 16
                            zf.writestr(info, b"")

                        data.seek(0)
                        with zipfile.ZipFile(data, "r") as zf:
                            with self.assertRaisesRegex(
                                RuntimeError, "不安全链接或特殊文件"
                            ):
                                safe_extract_zip(zf, dest)

                        self.assertEqual(list(Path(dest).iterdir()), [])

    def test_safe_extract_tar_accepts_regular_paths(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_tar

            with tempfile.TemporaryDirectory(prefix="fls-tar-ok-") as dest:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as tar:
                    add_tar_file(tar, "scripts/demo.py", b"print('ok')")

                data.seek(0)
                with tarfile.open(fileobj=data, mode="r:") as tar:
                    safe_extract_tar(tar, dest)

                self.assertTrue((Path(dest) / "scripts" / "demo.py").exists())

    def test_safe_extract_tar_does_not_emit_deprecation_warning(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_tar

            with tempfile.TemporaryDirectory(prefix="fls-tar-warn-") as dest:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as tar:
                    add_tar_file(tar, "scripts/demo.py", b"print('ok')")

                data.seek(0)
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", DeprecationWarning)

                    with tarfile.open(fileobj=data, mode="r:") as tar:
                        safe_extract_tar(tar, dest)

                deprecations = [
                    item
                    for item in caught
                    if issubclass(item.category, DeprecationWarning)
                ]
                self.assertEqual(deprecations, [])

    def test_safe_extract_tar_rejects_traversal(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_tar

            with tempfile.TemporaryDirectory(prefix="fls-tar-bad-") as dest:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as tar:
                    add_tar_file(tar, "../evil.txt", b"bad")

                data.seek(0)
                with tarfile.open(fileobj=data, mode="r:") as tar:
                    with self.assertRaises(RuntimeError):
                        safe_extract_tar(tar, dest)

    def test_safe_extract_tar_rejects_absolute_paths(self):
        cases = [
            "/tmp/evil.txt",
            "C:/evil.txt",
        ]

        for name in cases:
            with self.subTest(name=name):
                self.assert_tar_rejected(
                    lambda tar, member_name=name: add_tar_file(tar, member_name, b"bad")
                )

    def test_safe_extract_tar_rejects_symlink_and_hardlink_members(self):
        cases = [
            ("data/link", tarfile.SYMTYPE, "/tmp/evil.txt"),
            ("data/link", tarfile.SYMTYPE, "../../evil.txt"),
            ("data/hard", tarfile.LNKTYPE, "/tmp/evil.txt"),
            ("data/hard", tarfile.LNKTYPE, "../../evil.txt"),
        ]

        for name, member_type, linkname in cases:
            with self.subTest(name=name, member_type=member_type, linkname=linkname):
                self.assert_tar_rejected(
                    lambda tar, n=name, t=member_type, l=linkname: add_tar_member(
                        tar,
                        n,
                        t,
                        l,
                    )
                )

    def test_safe_extract_tar_rejects_special_members(self):
        cases = [
            ("data/pipe", tarfile.FIFOTYPE),
            ("data/chardev", tarfile.CHRTYPE),
            ("data/blockdev", tarfile.BLKTYPE),
        ]

        for name, member_type in cases:
            with self.subTest(name=name, member_type=member_type):
                self.assert_tar_rejected(
                    lambda tar, n=name, t=member_type: add_tar_member(tar, n, t)
                )


if __name__ == "__main__":
    unittest.main()
