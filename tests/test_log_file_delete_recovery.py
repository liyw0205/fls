import os
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class LogFileDeleteRecoveryTests(unittest.TestCase):
    def seed(self, base_dir, name="task.log"):
        from fls_manager.routes.logs._common import log_file_version
        path = base_dir / "log" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n", encoding="utf-8")
        return path, log_file_version(path)

    def test_stale_page_cannot_delete_same_name_replacement(self):
        with isolated_app() as (app, base_dir):
            path, version = self.seed(base_dir)
            path.write_text("replacement\n", encoding="utf-8")
            response = app.test_client().post(
                "/logfile/delete/task.log",
                data={"version": version},
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 409)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "replacement\n")
            self.assertIn("日志文件已变化", response.get_data(as_text=True))

    def test_invalid_path_and_symlink_have_zero_side_effects(self):
        with isolated_app() as (app, base_dir):
            path, version = self.seed(base_dir)
            outside = base_dir / "outside.log"
            outside.write_text("private\n", encoding="utf-8")
            linked = path.with_name("linked.log")
            linked.symlink_to(outside)
            client = app.test_client()
            for filename in ("../task.log", "nested/task.log", "linked.log"):
                with self.subTest(filename=filename):
                    response = client.post(
                        "/logfile/delete/" + filename,
                        data={"version": version},
                        headers={"X-Token": TOKEN},
                    )
                    self.assertIn(response.status_code, (400, 404, 409))
            self.assertTrue(path.exists())
            self.assertTrue(linked.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "private\n")

    def test_delete_failure_reports_unconfirmed_state_and_retry_works(self):
        with isolated_app() as (app, base_dir):
            path, version = self.seed(base_dir)
            from fls_manager.log_access import LogAccess
            original_unlink = LogAccess.unlink
            calls = {"count": 0}

            def fail_once(access):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise PermissionError("fixture delete denied")
                return original_unlink(access)

            with patch.object(LogAccess, "unlink", autospec=True, side_effect=fail_once):
                response = app.test_client().post(
                    "/logfile/delete/task.log",
                    data={"version": version},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(response.status_code, 500)
                self.assertTrue(path.exists())
                retry = app.test_client().post(
                    "/logfile/delete/task.log",
                    data={"version": version},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(retry.status_code, 302)
            self.assertFalse(path.exists())

    def test_page_carries_file_identity_and_requires_post(self):
        with isolated_app() as (app, base_dir):
            path, version = self.seed(base_dir)
            client = app.test_client()
            page = client.get("/logfile/task.log", headers={"X-Token": TOKEN})
            html = page.get_data(as_text=True)
            self.assertEqual(page.status_code, 200)
            self.assertIn(f'name="version" value="{version}"', html)
            self.assertEqual(client.get("/logfile/delete/task.log", headers={"X-Token": TOKEN}).status_code, 405)


if __name__ == "__main__":
    unittest.main()
