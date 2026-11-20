import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, sample_task
from tests.test_notify_action_recovery import isolated_app
from tests.test_high_side_effect_workflows import restore_test_archive
from tests import test_proxy_action_recovery as proxy_tests


class BackupRestoreRecoveryTests(unittest.TestCase):
    authenticate = proxy_tests.ProxyActionRecoveryTests.authenticate
    form = proxy_tests.ProxyActionRecoveryTests.form

    def post(self, client, *, api=True, fields=None, headers=None):
        task = sample_task("restored", command="echo restored", cron="0 0 1 1 *")
        archive = restore_test_archive({"tasks.json": json.dumps([task]).encode()})
        data = {"file": (archive, "restore.tar.gz"), "restore_items": ["data", "scripts"], **(fields or {})}
        return client.post("/api/backup/import" if api else "/backup/import", data=data,
                           headers={"X-Token": TOKEN} if headers is None else headers)

    def test_partial_restore_api_only_reloads_current_tasks_and_preserves_later_edits(self):
        with isolated_app() as (app, base):
            from fls_manager import scheduler, models
            from fls_manager.routes.backup import restore
            with patch.object(scheduler, "_reload_scheduler_locked", side_effect=RuntimeError("fixture scheduler unavailable")):
                response = self.post(app.test_client())
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json["restored"])
            self.assertTrue(response.json["scheduler_reload_required"])
            self.assertEqual(models.load_tasks()[0]["id"], "restored")
            self.assertTrue((base / "scripts/demo.py").exists())
            tasks = models.load_tasks()
            tasks[0]["name"] = "Edited after restore"
            models.save_tasks(tasks)
            (base / "scripts/demo.py").write_text("print('later edit')")
            before = (base / "data/tasks.json").read_bytes()
            with patch.object(restore, "_commit_restore_directories") as commit:
                for _ in range(2):
                    result = app.test_client().post(response.json["reload_url"], headers={"X-Token": TOKEN})
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(scheduler.get_scheduler_state()["status"], "succeeded")
                    self.assertIsNotNone(scheduler.scheduler.get_job("task_restored"))
                commit.assert_not_called()
            self.assertEqual((base / "data/tasks.json").read_bytes(), before)
            self.assertEqual((base / "scripts/demo.py").read_text(), "print('later edit')")

    def test_restore_scheduler_recovery_is_bound_to_original_request(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            client = app.test_client()
            self.authenticate(client)
            request_id = "9" * 32
            fields = {"restore_request_id": request_id, "csrf_token": "proxy-csrf"}
            with patch.object(restore, "reload_scheduler", side_effect=RuntimeError("initial reload unavailable")):
                failed_restore = self.post(client, fields=fields, headers={})

            self.assertEqual(failed_restore.status_code, 503)
            failed_payload = failed_restore.get_json()
            reload_url = f"/api/backup/restore/{request_id}/reload"
            self.assertEqual(failed_payload["request_id"], request_id)
            self.assertEqual(failed_payload["reload_url"], reload_url)
            status = client.get(f"/api/backup/restore/{request_id}")
            self.assertEqual(status.status_code, 503)
            self.assertEqual(status.get_json()["reload_url"], reload_url)

            script = base / "scripts/demo.py"
            script.write_text("print('later edit')", encoding="utf-8")
            with patch.object(restore, "reload_scheduler", side_effect=RuntimeError("retry unavailable")):
                failed_reload = client.post(reload_url, headers={"X-CSRF-Token": "proxy-csrf"})
            self.assertEqual(failed_reload.status_code, 503)
            self.assertTrue(failed_reload.get_json()["scheduler_reload_required"])
            pending = client.get(f"/api/backup/restore/{request_id}")
            self.assertEqual(pending.status_code, 503)
            self.assertEqual(pending.get_json()["scheduler_error"], "retry unavailable")
            self.assertEqual(pending.get_json()["reload_url"], reload_url)

            with patch.object(restore, "reload_scheduler") as reload_scheduler:
                recovered = client.post(reload_url, headers={"X-CSRF-Token": "proxy-csrf"})
                self.assertEqual(recovered.status_code, 200)
                reload_scheduler.assert_called_once()
            resolved = client.get(f"/api/backup/restore/{request_id}")
            self.assertEqual(resolved.status_code, 200)
            self.assertTrue(resolved.get_json()["resolved"])
            self.assertFalse(resolved.get_json()["scheduler_reload_required"])
            self.assertEqual(resolved.get_json()["scheduler_error"], "")

            with patch.object(restore, "reload_scheduler") as reload_again:
                replay = client.post(reload_url, headers={"X-CSRF-Token": "proxy-csrf"})
                self.assertEqual(replay.status_code, 200)
                reload_again.assert_not_called()
            self.assertEqual(script.read_text(encoding="utf-8"), "print('later edit')")

    def test_html_restore_scheduler_recovery_keeps_csrf_and_request_identity(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            client = app.test_client()
            self.authenticate(client)
            request_id = "8" * 32
            action = f"/backup/restore/{request_id}/reload"
            fields = {"restore_request_id": request_id, "csrf_token": "proxy-csrf"}
            with patch.object(restore, "reload_scheduler", side_effect=RuntimeError("initial reload unavailable")):
                failed_restore = self.post(client, api=False, fields=fields, headers={})

            self.assertEqual(failed_restore.status_code, 503)
            form = self.form(failed_restore, action)
            self.assertEqual(form["action"], action)
            self.assertEqual(form["fields"].get("csrf_token"), "proxy-csrf")
            self.assertEqual(client.post(action, data={"csrf_token": "bad"}).status_code, 400)

            script = base / "scripts/demo.py"
            script.write_text("print('later edit')", encoding="utf-8")
            with patch.object(restore, "reload_scheduler", side_effect=RuntimeError("retry unavailable")):
                retry_failed = client.post(action, data=form["fields"])
            self.assertEqual(retry_failed.status_code, 503)
            form = self.form(retry_failed, action)

            with patch.object(restore, "reload_scheduler") as reload_scheduler:
                recovered = client.post(action, data=form["fields"])
                self.assertEqual(recovered.status_code, 302)
                reload_scheduler.assert_called_once()
            resolved = client.get(f"/api/backup/restore/{request_id}")
            self.assertEqual(resolved.status_code, 200)
            self.assertTrue(resolved.get_json()["resolved"])
            self.assertEqual(script.read_text(encoding="utf-8"), "print('later edit')")

    def test_invalid_ranges_and_dependency_selection_never_extract_or_write(self):
        for api in (False, True):
            for fields in ({"restore_items": ["data", "unknown"]}, {"restore_items": ["data", "data"]},
                           {"restore_items": []}, {"restore_deps": "yes"}, {"restore_deps": ["0", "1"]}):
                with self.subTest(api=api, fields=fields), isolated_app() as (app, base):
                    from fls_manager.routes.backup import restore
                    marker = base / "scripts/original.py"
                    marker.write_text("untouched")
                    with patch.object(restore, "save_backup_upload") as save, patch.object(restore, "extract_archive") as extract:
                        response = self.post(app.test_client(), api=api, fields=fields)
                        self.assertEqual(response.status_code, 400)
                        save.assert_not_called()
                        extract.assert_not_called()
                    self.assertEqual(marker.read_text(), "untouched")
                    if api:
                        self.assertFalse(response.json["restored"])

    def test_form_csrf_auth_and_actual_reload_retry(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            from fls_manager.routes import dashboard
            client = app.test_client()
            self.authenticate(client)
            self.assertEqual(self.post(client, fields={"csrf_token": "bad"}, headers={}).status_code, 400)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(self.post(client, fields={"csrf_token": "proxy-csrf"}, headers={}).status_code, 401)
            self.authenticate(client)
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                self.assertEqual(client.post("/api/backup/import?token=" + TOKEN,
                    data={"csrf_token": "proxy-csrf", "file": (io.BytesIO(b"x"), "restore.tar.gz"), "restore_items": "data"}).status_code, 403)
            with patch.object(restore, "reload_scheduler", side_effect=OSError("reload unavailable")):
                response = self.post(client, api=False, fields={"csrf_token": "proxy-csrf"}, headers={})
            self.assertEqual(response.status_code, 503)
            form = self.form(response, "/scheduler/reload")
            self.assertNotIn("/backup/import", response.get_data(as_text=True))
            self.assertEqual(client.post(form["action"], data=dict(form["fields"], csrf_token="bad")).status_code, 400)
            with patch.object(dashboard, "reload_scheduler", side_effect=OSError("still unavailable")):
                retry = client.post(form["action"], data=form["fields"])
            self.assertEqual(retry.status_code, 500)
            form = self.form(retry, "/scheduler/reload")
            script = base / "scripts/demo.py"
            script.write_text("print('later')")
            for _ in range(2):
                response = client.post(form["action"], data=form["fields"])
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/backup")
            self.assertEqual(script.read_text(), "print('later')")

    def test_api_selected_directory_success_and_archive_failure_are_truthful(self):
        with isolated_app() as (app, base):
            from fls_manager.config import save_config
            save_config({"panel_title": "Before restore"})
            config = base / "data/config.json"
            original = config.read_bytes()
            response = self.post(app.test_client(), fields={"restore_items": ["scripts"]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["restored_items"], ["scripts"])
            self.assertEqual(config.read_bytes(), original)
            response = app.test_client().post("/api/backup/import", data={"file": (io.BytesIO(b"broken"), "broken.zip"),
                                              "restore_items": "data"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.json["restored"])
            self.assertEqual(config.read_bytes(), original)

    def test_post_commit_exception_never_claims_directories_are_unchanged(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            with patch.object(restore, "find_dependency_file", return_value=base / "requirements.txt"), patch.object(restore, "install_dependencies", side_effect=OSError("dependency service unavailable")):
                response = self.post(app.test_client(), fields={"restore_deps": "1"})
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json["restored"])
            self.assertFalse(response.json["scheduler_reload_required"])
            self.assertTrue(response.json["dependencies_failed"])
            self.assertTrue((base / "scripts/demo.py").exists())

    def test_failed_restore_query_retains_bounded_diagnostic(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore

            request_id = "e" * 32
            marker = base / "data/restore-marker.txt"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("preserve", encoding="utf-8")
            diagnostic = "<disk> quota unavailable</disk>\x00" + "x" * 1200

            with patch.object(
                restore,
                "_stage_restore_directory",
                side_effect=OSError(diagnostic),
            ):
                response = self.post(
                    app.test_client(),
                    fields={"restore_request_id": request_id},
                )

            self.assertEqual(response.status_code, 500)
            self.assertTrue(response.json["live_state_unchanged"])
            status = app.test_client().get(
                "/api/backup/restore/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 500)
            payload = status.get_json()
            self.assertEqual(payload["state"], "failed")
            self.assertIn("<disk> quota unavailable</disk>", payload["error"])
            self.assertLessEqual(len(payload["error"]), 1000)
            self.assertNotIn("\x00", payload["error"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_rollback_failure_is_unknown_and_preserves_recovery_workspace(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            from fls_manager.routes.backup._common import SCRIPT_DIR

            request_id = "f" * 32
            marker = base / "data/restore-marker.txt"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("old data", encoding="utf-8")
            SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            old_script = SCRIPT_DIR / "original.py"
            old_script.write_text("old script", encoding="utf-8")
            task = sample_task("restored", command="echo restored", cron="0 0 1 1 *")
            archive_data = restore_test_archive(
                {"tasks.json": json.dumps([task]).encode()}
            ).getvalue()
            real_replace = restore.os.replace

            def fail_install_and_rollback(source, destination):
                source = Path(source)
                destination = Path(destination)
                if source.name == "replacement" and destination == SCRIPT_DIR:
                    raise OSError("script replacement failed")
                if source.name == "previous" and destination == SCRIPT_DIR:
                    raise OSError("script rollback failed")
                return real_replace(source, destination)

            def submit():
                return app.test_client().post(
                    "/api/backup/import",
                    data={
                        "file": (io.BytesIO(archive_data), "restore.tar.gz"),
                        "restore_items": ["data", "scripts"],
                        "restore_request_id": request_id,
                    },
                    headers={"X-Token": TOKEN},
                )

            with patch.object(restore.os, "replace", side_effect=fail_install_and_rollback):
                response = submit()

            self.assertEqual(response.status_code, 503)
            payload = response.get_json()
            self.assertTrue(payload["restore_outcome_unknown"])
            self.assertEqual(payload["state"], "unknown")
            self.assertEqual(payload["request_id"], request_id)
            self.assertIn("不要重复导入", payload["msg"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "old data")

            workspaces = list(base.glob(".scripts-restore-*"))
            self.assertEqual(len(workspaces), 1)
            self.assertEqual(
                (workspaces[0] / "previous" / "original.py").read_text(encoding="utf-8"),
                "old script",
            )

            status = app.test_client().get(
                "/api/backup/restore/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 503)
            self.assertTrue(status.get_json()["restore_outcome_unknown"])
            self.assertIn("script rollback failed", status.get_json()["error"])
            with patch.object(restore, "_stage_restore_directory") as stage:
                replay = submit()
                self.assertEqual(replay.status_code, 503)
                self.assertTrue(replay.get_json()["restore_outcome_unknown"])
                stage.assert_not_called()

    def test_dependency_failure_or_missing_list_is_partial_completion(self):
        for api in (False, True):
            for missing in (False, True):
                with self.subTest(api=api, missing=missing), isolated_app() as (app, base):
                    from fls_manager.routes.backup import restore
                    with patch.object(restore, "find_dependency_file", return_value=None if missing else base / "requirements.txt"), patch.object(restore, "install_dependencies", return_value=(False, "pip rejected package")) as install:
                        response = self.post(app.test_client(), api=api, fields={"restore_deps": "1"})
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(install.call_count, int(not missing))
                    if api:
                        self.assertTrue(response.json["restored"])
                        self.assertTrue(response.json["dependencies_failed"])
                        self.assertFalse(response.json["scheduler_reload_required"])
                    else:
                        self.assertIn('href="/deps"', response.get_data(as_text=True))
                        self.assertNotIn("/backup/import", response.get_data(as_text=True))
