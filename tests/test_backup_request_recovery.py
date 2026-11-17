import contextlib
import tarfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app
from tests import test_proxy_action_recovery as proxy_tests


class BackupRequestRecoveryTests(unittest.TestCase):
    authenticate = proxy_tests.ProxyActionRecoveryTests.authenticate
    form = proxy_tests.ProxyActionRecoveryTests.form

    def values(self, **extra):
        return dict({"request_id": "b" * 32, "items": ["scripts"]}, **extra)

    def post(self, client, data):
        return client.post("/api/backup/create", json=data, headers={"X-Token": TOKEN})

    @contextlib.contextmanager
    def inline_worker(self):
        from fls_manager.routes.backup import _common as backup
        with patch.object(backup, "write_dependencies_file"), patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()) as start:
            yield start

    def test_real_archive_lost_response_restart_and_pruned_job_never_duplicate(self):
        with isolated_app() as (app, base), self.inline_worker() as start:
            from fls_manager.routes.backup import _common as backup
            script = base / "scripts/example.py"
            script.write_text("print('preserved')")
            data = self.values()
            response = self.post(app.test_client(), data)
            self.assertEqual(response.status_code, 200)
            job_id = response.json["job_id"]
            info = app.test_client().get("/api/backup/job/" + job_id, headers={"X-Token": TOKEN}).json
            self.assertEqual(info["status"], "已完成")
            with tarfile.open(backup.BACKUP_DIR / info["filename"]) as archive:
                self.assertEqual(archive.extractfile("scripts/example.py").read(), script.read_bytes())
                self.assertNotIn("data/config.json", archive.getnames())
            self.assertEqual(app.test_client().get("/backup/download/" + info["filename"], headers={"X-Token": TOKEN}).status_code, 200)
            backup.BACKUP_JOBS.clear()
            self.assertEqual(self.post(app.test_client(), data).status_code, 200)
            self.assertEqual(self.post(app.test_client(), dict(data, items=["data"])).status_code, 409)
            backup.BACKUP_JOBS_FILE.write_text("{}")
            self.assertEqual(self.post(app.test_client(), data).status_code, 409)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)

    def test_invalid_selection_and_restore_conflict_have_no_job_or_receipt(self):
        for values in ({"items": []}, {"items": ["scripts", "bad"]}, {"items": "scripts"},
                       {"items": ["scripts", "scripts"]}, {"items": [{}]}, {"request_id": "../bad"},
                       {"confirm_retry": 1}, {"restore": True}):
            with self.subTest(values=values), isolated_app() as (app, base):
                from fls_manager.routes.backup import _common as backup
                data = self.values(**{key: value for key, value in values.items() if key != "restore"})
                backup.RESTORE_ACTIVE = bool(values.get("restore"))
                try:
                    with patch.object(backup, "start_backup_job") as start:
                        response = self.post(app.test_client(), data)
                        self.assertEqual(response.status_code, 409 if values.get("restore") else 400)
                        start.assert_not_called()
                    self.assertFalse(backup.BACKUP_JOBS_FILE.exists())
                    self.assertFalse((base / "data/backup_requests").exists())
                    self.assertEqual(backup.BACKUP_JOBS, {})
                finally:
                    backup.RESTORE_ACTIVE = False

    def test_legacy_form_rejects_invalid_or_duplicate_items_without_starting(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import api
            with patch.object(api, "start_backup_job") as start:
                for items in (["scripts", "bad"], ["scripts", "scripts"]):
                    self.assertEqual(app.test_client().post("/api/backup/create", data={"items": items}, headers={"X-Token": TOKEN}).status_code, 400)
                start.assert_not_called()

    def test_job_publication_failures_and_thread_failure_report_actual_state(self):
        for mode in ("before", "after", "thread"):
            with self.subTest(mode=mode), isolated_app() as (app, _):
                from fls_manager.routes.backup import _common as backup
                replace = Path.replace
                def publish(path, target):
                    if target == backup.BACKUP_JOBS_FILE:
                        if mode == "after":
                            replace(path, target)
                        raise OSError("fixture job write fault")
                    return replace(path, target)
                with patch.object(threading.Thread, "start", side_effect=OSError("fixture worker limit")) as start:
                    if mode == "thread":
                        response = self.post(app.test_client(), self.values())
                    else:
                        with patch.object(Path, "replace", publish):
                            response = self.post(app.test_client(), self.values())
                    self.assertEqual(response.status_code, 500 if mode == "before" else 503)
                    self.assertEqual(start.call_count, int(mode == "thread"))
                self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)
                if mode == "before":
                    self.assertNotIn("job_id", response.json)
                    with self.inline_worker() as start:
                        self.assertEqual(self.post(app.test_client(), self.values()).status_code, 200)
                        start.assert_called_once()
                else:
                    job_id = response.json["job_id"]
                    self.assertEqual(backup.get_backup_job(job_id)["status"], "结果未知" if mode == "after" else "启动失败")
                    with patch.object(threading.Thread, "start") as start:
                        self.assertEqual(self.post(app.test_client(), self.values()).status_code, 200)
                        start.assert_not_called()

    def test_receipt_failures_and_confirmation_keep_same_job_identity(self):
        for mode in ("prepared_before", "prepared_after", "launching_before", "launching_after", "submitted"):
            with self.subTest(mode=mode), isolated_app() as (app, _), self.inline_worker() as start:
                from fls_manager.routes.backup import actions
                write = actions.write_json
                def receipt(path, row):
                    if mode.startswith(row["state"]) or mode == "submitted" and row["state"] == "submitted":
                        if mode.endswith("after"):
                            write(path, row)
                        raise OSError("fixture receipt fault")
                    return write(path, row)
                with patch.object(actions, "write_json", side_effect=receipt):
                    response = self.post(app.test_client(), self.values())
                self.assertEqual(response.status_code, 503 if mode == "submitted" else 500)
                self.assertEqual(start.call_count, int(mode == "submitted"))
                response = self.post(app.test_client(), self.values())
                if mode == "launching_after":
                    self.assertEqual(response.status_code, 409)
                    self.assertTrue(response.json["resolved"])
                    self.assertEqual(self.post(app.test_client(), self.values(confirm_retry=True)).status_code, 409)
                    start.assert_not_called()
                    continue
                if mode in ("prepared_after", "launching_before"):
                    self.assertEqual(response.status_code, 409)
                    self.assertTrue(response.json["confirm_required"])
                    response = self.post(app.test_client(), self.values(confirm_retry=True))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(start.call_count, 1)
                self.assertEqual(response.json["job_id"], "b" * 32)

    def test_missing_history_after_lost_submission_receipt_never_restarts(self):
        with isolated_app() as (app, _), self.inline_worker() as start:
            from fls_manager.routes.backup import actions, _common as backup
            write = actions.write_json
            def receipt(path, row):
                if row["state"] == "submitted":
                    raise OSError("fixture submitted receipt lost")
                return write(path, row)
            with patch.object(actions, "write_json", side_effect=receipt):
                self.assertEqual(self.post(app.test_client(), self.values()).status_code, 503)
            backup.BACKUP_JOBS.clear()
            backup.BACKUP_JOBS_FILE.write_text("{}")
            self.assertEqual(self.post(app.test_client(), self.values(confirm_retry=True)).status_code, 409)
            start.assert_called_once()

    def test_corrupt_job_and_receipt_storage_is_preserved(self):
        for target in ("jobs", "request"):
            for raw in ("{", "null", "[]", '{"wrong":{}}'):
                with self.subTest(target=target, raw=raw), isolated_app() as (app, base):
                    from fls_manager.routes.backup import _common as backup
                    path = backup.BACKUP_JOBS_FILE if target == "jobs" else base / "data/backup_requests" / ("b" * 32 + ".json")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(raw)
                    with patch.object(threading.Thread, "start") as start:
                        self.assertEqual(self.post(app.test_client(), self.values()).status_code, 500)
                        start.assert_not_called()
                    self.assertEqual(path.read_text(), raw)
                    if target == "jobs":
                        self.assertEqual(app.test_client().get("/api/backup/job/" + "b" * 32, headers={"X-Token": TOKEN}).status_code, 500)

    def test_concurrent_replays_reserve_restore_boundary_before_worker_runs(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            entered, release, done = threading.Event(), threading.Event(), threading.Event()
            finish = backup.end_backup_activity
            def worker(*args):
                entered.set()
                release.wait(5)
            def finished():
                finish()
                done.set()
            with patch.object(backup, "_create_backup_worker", side_effect=worker) as run, patch.object(backup, "end_backup_activity", side_effect=finished):
                try:
                    with ThreadPoolExecutor(2) as pool:
                        responses = list(pool.map(lambda _: self.post(app.test_client(), self.values()), range(2)))
                    self.assertTrue(entered.wait(2))
                    self.assertEqual([response.status_code for response in responses], [200, 200])
                    self.assertFalse(backup.try_begin_restore_activity())
                    self.assertEqual(run.call_count, 1)
                finally:
                    release.set()
                    self.assertTrue(done.wait(3))
            self.assertTrue(backup.try_begin_restore_activity())
            backup.end_restore_activity()

    def test_form_csrf_permission_and_recovery_are_executable(self):
        with isolated_app() as (app, _), self.inline_worker() as start:
            client = app.test_client()
            self.assertEqual(client.post("/api/backup/create", json=self.values()).status_code, 400)
            self.authenticate(client)
            form = self.form(client.get("/backup"), "/backup/create")
            data = dict(form["fields"], items=["scripts"])
            self.assertEqual(client.post(form["action"], data=dict(data, csrf_token="wrong")).status_code, 400)
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                self.assertEqual(client.post("/api/backup/create?token=" + TOKEN, json=self.values(), headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 403)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.post("/api/backup/create", json=self.values(), headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 401)
            self.authenticate(client)
            from fls_manager.routes.backup import actions
            with patch.object(actions, "write_json", side_effect=OSError("disk unavailable")):
                response = client.post(form["action"], data=data)
            self.assertEqual(response.status_code, 500)
            recovery = self.form(response, "/backup/create")
            response = client.post(recovery["action"], data=recovery["fields"])
            self.assertEqual(response.status_code, 200)
            self.assertIn("查看原备份进度", response.get_data(as_text=True))
            self.assertEqual(client.post(recovery["action"], data=recovery["fields"]).status_code, 200)
            start.assert_called_once()
