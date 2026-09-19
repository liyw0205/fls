import contextlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "frontend-open-token"


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
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-frontend-open-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "frontend-open-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()
        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from fls_manager.app import create_app

            yield create_app()
        finally:
            shutdown_scheduler()
            purge_fls_modules()
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class FrontendOpenEventTests(unittest.TestCase):
    def _set_session(self, client):
        response = client.get(f"/?token={TOKEN}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("Location"), "/")

    def test_first_rendered_page_dispatches_once(self):
        with isolated_app() as app:
            client = app.test_client()
            self._set_session(client)

            with patch("fls_manager.app.dispatch_frontend_open_event") as dispatch:
                first = client.get("/")
                second = client.get("/tasks")

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            dispatch.assert_called_once_with()

    def test_ajax_reconnect_does_not_dispatch_until_a_real_page_is_opened(self):
        with isolated_app() as app:
            client = app.test_client()
            self._set_session(client)

            with patch("fls_manager.app.dispatch_frontend_open_event") as dispatch:
                ajax = client.get("/", headers={"X-Requested-With": "FLS-Ajax"})
                page = client.get("/")

            self.assertEqual(ajax.status_code, 200)
            self.assertEqual(page.status_code, 200)
            dispatch.assert_called_once_with()

    def test_successful_token_submission_does_not_send_before_frontend_open(self):
        with isolated_app() as app:
            client = app.test_client()
            login_page = client.get("/login")
            csrf_token = re.search(
                r'name="csrf_token" value="([^"]+)"',
                login_page.get_data(as_text=True),
            ).group(1)

            with patch("fls_manager.routes.auth_routes.send_all_enabled") as send_notice:
                response = client.post(
                    "/login",
                    data={"token": TOKEN, "csrf_token": csrf_token},
                )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/")
            send_notice.assert_not_called()

    def test_dispatch_starts_update_check_before_notification_thread(self):
        with isolated_app() as app:
            from fls_manager.frontend_events import dispatch_frontend_open_event

            with app.test_request_context("/", headers={"User-Agent": "test-agent"}):
                with patch(
                    "fls_manager.routes.about.helpers.start_refresh_log_job"
                ) as start_refresh, patch(
                    "fls_manager.frontend_events.threading.Thread"
                ) as worker_thread:
                    dispatch_frontend_open_event()

            start_refresh.assert_called_once_with(title="首次访问自动刷新更新日志")
            worker_thread.return_value.start.assert_called_once_with()

    def test_non_git_directory_records_refresh_error_without_background_job(self):
        with isolated_app():
            from fls_manager.routes.about.helpers import start_refresh_log_job
            from fls_manager.routes.about.state import update_log_state

            job_id, started = start_refresh_log_job()
            state = update_log_state()

            self.assertEqual(job_id, "")
            self.assertFalse(started)
            self.assertFalse(state["checking"])
            self.assertIn("不是 Git 仓库", state["error"])

    def test_notification_worker_records_send_result(self):
        with isolated_app():
            from fls_manager.frontend_events import _frontend_open_worker

            with patch(
                "fls_manager.frontend_events.send_all_enabled",
                return_value=[{"ok": True}, {"ok": False}],
            ), patch("fls_manager.frontend_events._log_frontend_event") as record:
                _frontend_open_worker("127.0.0.1", "test-agent")

            record.assert_called_once_with("首次访问通知已发送：1/2 个通道成功")


if __name__ == "__main__":
    unittest.main()
