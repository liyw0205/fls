import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class LogGroupDeleteRecoveryTests(unittest.TestCase):
    def seed(self, base_dir):
        from fls_manager.routes.logs._common import log_group_version

        log_dir = base_dir / "log"
        files = {}
        for name, group in (("a.log", "Group A"), ("b.log", "Group A"), ("c.log", "Group B")):
            path = log_dir / name
            path.write_text(f"===== 启动任务: {group} =====\n{name}\n", encoding="utf-8")
            files[name] = path
        from fls_manager.routes.logs._common import load_log_groups
        groups = load_log_groups()
        versions = {name: log_group_version(items) for name, items in groups.items()}
        return files, versions

    def post(self, client, groups, versions):
        return client.post(
            "/api/logs/groups/delete",
            json={"groups": groups, "group_versions": versions},
            headers={"X-Token": TOKEN},
        )

    def test_stale_group_does_not_delete_replacement_and_returns_retry_group(self):
        with isolated_app() as (app, base_dir):
            files, versions = self.seed(base_dir)
            files["c.log"].write_text("===== 启动任务: Group B =====\nreplacement\n", encoding="utf-8")
            response = self.post(app.test_client(), ["Group A", "Group B"], versions)
            payload = response.get_json()
            self.assertEqual(response.status_code, 409)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["retry_groups"], ["Group B"])
            self.assertFalse(files["a.log"].exists())
            self.assertFalse(files["b.log"].exists())
            self.assertTrue(files["c.log"].exists())
            self.assertEqual(payload["results"][0]["status"], "deleted")
            self.assertEqual(payload["results"][1]["status"], "conflict")

            from fls_manager.routes.logs._common import load_log_groups, log_group_version
            current = load_log_groups()
            retry = self.post(app.test_client(), ["Group B"], {"Group B": log_group_version(current["Group B"])})
            self.assertEqual(retry.status_code, 200)
            self.assertFalse(files["c.log"].exists())

    def test_invalid_snapshot_and_csrf_are_zero_side_effects(self):
        with isolated_app() as (app, base_dir):
            files, versions = self.seed(base_dir)
            client = app.test_client()
            bad = client.post(
                "/api/logs/groups/delete",
                json={"groups": ["Group A"], "group_versions": {"Group A": "bad"}},
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(bad.status_code, 400)
            self.assertFalse(bad.get_json()["ok"])
            self.assertTrue(files["a.log"].exists())
            self.assertTrue(files["b.log"].exists())
            no_csrf = client.post(
                "/api/logs/groups/delete",
                json={"groups": ["Group A"], "group_versions": versions},
            )
            self.assertEqual(no_csrf.status_code, 400)
            self.assertTrue(files["a.log"].exists())

    def test_invalid_group_types_and_duplicates_are_zero_side_effects(self):
        with isolated_app() as (app, base_dir):
            files, versions = self.seed(base_dir)
            client = app.test_client()
            for groups in (["Group A", 7], ["Group A", "Group A"], ["Group A", ""]):
                with self.subTest(groups=groups):
                    response = client.post(
                        "/api/logs/groups/delete",
                        json={"groups": groups, "group_versions": versions},
                        headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.get_json()["ok"])
                    self.assertTrue(files["a.log"].exists())
                    self.assertTrue(files["b.log"].exists())
                    self.assertTrue(files["c.log"].exists())

    def test_logs_page_exposes_group_versions_and_recovery_request_code(self):
        with isolated_app() as (app, base_dir):
            self.seed(base_dir)
            html = app.test_client().get("/logs", headers={"X-Token": TOKEN}).get_data(as_text=True)
            self.assertIn('data-version="', html)
            self.assertIn("group_versions", html)
            self.assertIn("fls-log-delete-recovery", html)
            self.assertIn("flsLogsRestorePendingGroups", html)
            self.assertIn('sessionStorage.setItem("fls-log-delete-recovery", JSON.stringify({groups: groups}))', html)
            self.assertIn('sessionStorage.removeItem("fls-log-delete-recovery")', html)
            self.assertIn('"X-CSRF-Token": flsGetCsrfToken()', html)

    def test_file_replaced_after_group_snapshot_is_not_deleted(self):
        with isolated_app() as (app, base_dir):
            files, versions = self.seed(base_dir)
            from fls_manager.routes.logs import files as log_routes

            original_snapshot = log_routes.log_group_file_versions

            def snapshot_then_replace(paths):
                result = original_snapshot(paths)
                files["c.log"].write_text(
                    "===== 启动任务: Group B =====\nreplacement\n",
                    encoding="utf-8",
                )
                return result

            with patch.object(log_routes, "log_group_file_versions", side_effect=snapshot_then_replace):
                response = self.post(app.test_client(), ["Group B"], {"Group B": versions["Group B"]})

            payload = response.get_json()
            self.assertEqual(response.status_code, 409)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["retry_groups"], ["Group B"])
            self.assertEqual(payload["results"][0]["status"], "conflict")
            self.assertTrue(files["c.log"].exists())
            self.assertEqual(files["c.log"].read_text(encoding="utf-8"), "===== 启动任务: Group B =====\nreplacement\n")

    def test_replacement_after_one_file_delete_is_reported_and_preserved(self):
        with isolated_app() as (app, base_dir):
            files, versions = self.seed(base_dir)

            from fls_manager.log_access import LogAccess
            real_unlink = LogAccess.unlink
            calls = {"count": 0}

            def unlink_then_replace(access):
                calls["count"] += 1
                path = files["a.log"] if calls["count"] == 1 else files["b.log"]
                self.assertEqual(access.path.resolve(), path.resolve())
                result = real_unlink(access)
                if path == files["b.log"]:
                    path.write_text(
                        "===== 启动任务: Group A =====\nreplacement\n",
                        encoding="utf-8",
                    )
                return result

            with patch.object(LogAccess, "unlink", autospec=True, side_effect=unlink_then_replace):
                response = self.post(app.test_client(), ["Group A"], {"Group A": versions["Group A"]})

            payload = response.get_json()
            self.assertEqual(response.status_code, 409)
            self.assertTrue(payload["partial"])
            self.assertEqual(payload["retry_groups"], ["Group A"])
            self.assertEqual(payload["results"][0]["status"], "conflict")
            self.assertFalse(files["a.log"].exists())
            self.assertTrue(files["b.log"].exists())
            self.assertEqual(files["b.log"].read_text(encoding="utf-8"), "===== 启动任务: Group A =====\nreplacement\n")

    def test_symlink_member_is_rejected_without_deleting_group_files(self):
        with isolated_app() as (app, base_dir):
            files, versions = self.seed(base_dir)
            outside = base_dir / "outside.log"
            outside.write_text("===== 启动任务: Group A =====\nprivate\n", encoding="utf-8")
            linked = files["b.log"]
            linked.unlink()
            linked.symlink_to(outside)

            response = self.post(app.test_client(), ["Group A"], {"Group A": versions["Group A"]})
            payload = response.get_json()
            self.assertEqual(response.status_code, 500)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["results"][0]["status"], "failed")
            self.assertTrue(files["a.log"].exists())
            self.assertTrue(linked.is_symlink())
            self.assertEqual(outside.read_text(encoding="utf-8"), "===== 启动任务: Group A =====\nprivate\n")


if __name__ == "__main__":
    unittest.main()
