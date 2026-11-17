import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app
from tests import test_proxy_action_recovery as proxy_tests


class BackupCancelRecoveryTests(unittest.TestCase):
    authenticate = proxy_tests.ProxyActionRecoveryTests.authenticate
    form = proxy_tests.ProxyActionRecoveryTests.form

    def seed(self):
        from fls_manager.routes.backup import _common as backup
        info = {"id": "cancel-job", "items": ["scripts"], "running": True,
                "status": "正在压缩", "updated_at": "2026-11-17 10:00:00"}
        event = threading.Event()
        backup.BACKUP_JOBS[info["id"]] = info
        backup.BACKUP_CANCEL_EVENTS[info["id"]] = event
        backup.persist_backup_job(info)
        return info, event

    def post(self, client, job_id="cancel-job"):
        return client.post("/api/backup/cancel/" + job_id, headers={"X-Token": TOKEN})

    def test_publication_before_after_errors_match_disk_event_and_worker_terminal(self):
        for mode in ("before", "after"):
            with self.subTest(mode=mode), isolated_app() as (app, _):
                from fls_manager.routes.backup import _common as backup
                info, event = self.seed()
                original = backup.BACKUP_JOBS_FILE.read_bytes()
                replace = Path.replace

                def publish(path, target):
                    if target == backup.BACKUP_JOBS_FILE:
                        if mode == "after":
                            replace(path, target)
                        raise OSError("fixture cancel publication failure")
                    return replace(path, target)

                with patch.object(Path, "replace", publish):
                    response = self.post(app.test_client())
                accepted = mode == "after"
                self.assertEqual(response.status_code, 503 if accepted else 500)
                self.assertEqual(response.json["accepted"], accepted)
                self.assertEqual(response.json["cancel_saved"], accepted)
                self.assertEqual(event.is_set(), accepted)
                self.assertEqual(backup.BACKUP_JOBS_FILE.read_bytes() == original, not accepted)
                self.assertEqual(info["status"], "正在取消" if accepted else "正在压缩")
                if not accepted:
                    self.assertTrue(response.json["retry_allowed"])
                    self.assertEqual(self.post(app.test_client()).status_code, 200)
                saved = backup.BACKUP_JOBS_FILE.read_bytes()
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: self.post(app.test_client()).status_code, range(2)))
                self.assertEqual(results, [200, 200])
                self.assertEqual(backup.BACKUP_JOBS_FILE.read_bytes(), saved)
                statuses = []
                persist = backup.persist_backup_job

                def track(row):
                    statuses.append(row["status"])
                    return persist(row)

                with patch.object(backup, "persist_backup_job", side_effect=track), patch.object(backup, "write_dependencies_file") as dependencies:
                    backup.create_backup_worker(info["id"], info["items"], event)
                    dependencies.assert_not_called()
                self.assertEqual(statuses, ["正在取消", "已取消"])
                self.assertEqual(info["status"], "已取消")
                self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)
                self.assertNotIn(info["id"], backup.BACKUP_CANCEL_EVENTS)
                self.assertFalse(backup.BACKUP_DIR.exists())
                backup.BACKUP_JOBS.clear()
                response = self.post(app.test_client())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["job"]["status"], "已取消")

    def test_unreadable_publication_verification_signals_live_worker_but_never_claims_saved(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            info, event = self.seed()
            previous = backup._load_persisted_backup_job(info["id"])
            with patch.object(backup, "persist_backup_job", side_effect=OSError("disk fault")), patch.object(
                backup, "_load_persisted_backup_job", side_effect=[previous, OSError("unreadable")]
            ):
                response = self.post(app.test_client())
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json["accepted"])
            self.assertIsNone(response.json["cancel_saved"])
            self.assertTrue(event.is_set())
            self.assertEqual(info["status"], "正在取消")
            self.assertIn("无法确认", response.json["msg"])
            # An accepted replay needs no storage write, even while storage is broken.
            with patch.object(backup, "_load_persisted_backup_job", side_effect=OSError("still unreadable")):
                self.assertEqual(self.post(app.test_client()).status_code, 200)
            backup.create_backup_worker(info["id"], info["items"], event)
            self.assertEqual(backup._load_persisted_backup_job(info["id"])["status"], "已取消")
            self.assertFalse(info.get("cancel_error"))

    def test_real_running_worker_cancellation_releases_activity_and_leaves_no_archive(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            entered, release = threading.Event(), threading.Event()
            workers = []
            start = threading.Thread.start

            def capture(thread):
                workers.append(thread)
                return start(thread)

            def dependencies(*_):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("fixture worker release timed out")

            try:
                with patch.object(threading.Thread, "start", capture), patch.object(backup, "write_dependencies_file", side_effect=dependencies):
                    response = app.test_client().post("/api/backup/create", json={"request_id": "c" * 32, "items": ["scripts"]}, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(entered.wait(3))
                    job_id = response.json["job_id"]
                    self.assertEqual(self.post(app.test_client(), job_id).status_code, 200)
                    self.assertEqual(self.post(app.test_client(), job_id).status_code, 200)
                    release.set()
                    for thread in workers:
                        thread.join(timeout=5)
                        self.assertFalse(thread.is_alive())
            finally:
                release.set()
                for thread in workers:
                    thread.join(timeout=5)
            job = app.test_client().get("/api/backup/job/" + job_id, headers={"X-Token": TOKEN}).json
            self.assertEqual(job["status"], "已取消")
            self.assertFalse(job["running"])
            self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)
            self.assertFalse(list(backup.BACKUP_DIR.glob("*.tar.gz")))
            self.assertTrue(backup.try_begin_restore_activity())
            backup.end_restore_activity()

    def test_invalid_missing_corrupt_changed_and_lost_handles_have_zero_side_effects(self):
        for kind in ("invalid", "missing", "corrupt", "corrupt_cancel", "changed", "lost_handle", "restart", "get"):
            with self.subTest(kind=kind), isolated_app() as (app, _):
                from fls_manager.routes.backup import _common as backup
                info, event = self.seed()
                if kind == "corrupt":
                    backup.BACKUP_JOBS_FILE.write_text("{")
                elif kind == "corrupt_cancel":
                    backup.BACKUP_JOBS_FILE.write_text(json.dumps({info["id"]: dict(info, cancel_requested="false")}))
                elif kind == "changed":
                    backup.persist_backup_job(dict(info, status="已完成", running=False))
                elif kind == "lost_handle":
                    backup.BACKUP_CANCEL_EVENTS.clear()
                elif kind == "restart":
                    backup.BACKUP_JOBS.clear()
                original = backup.BACKUP_JOBS_FILE.read_bytes()
                job_id = "bad%20id" if kind == "invalid" else "missing" if kind == "missing" else info["id"]
                response = app.test_client().get("/api/backup/cancel/" + job_id, headers={"X-Token": TOKEN}) if kind == "get" else self.post(app.test_client(), job_id)
                self.assertEqual(response.status_code, {"invalid": 400, "missing": 404, "corrupt": 500, "corrupt_cancel": 500, "get": 405}.get(kind, 409))
                if kind != "get":
                    self.assertFalse(response.json["accepted"])
                self.assertFalse(event.is_set())
                self.assertEqual(info["status"], "正在压缩")
                self.assertEqual(backup.BACKUP_JOBS_FILE.read_bytes(), original)

    def test_form_permissions_csrf_failure_retry_and_plain_html_progress(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            info, event = self.seed()
            client = app.test_client()
            self.authenticate(client)
            form = self.form(client.get("/backup?job_id=" + info["id"]), "/backup/cancel/")
            original = backup.BACKUP_JOBS_FILE.read_bytes()
            for path in (form["action"], "/api/backup/cancel/" + info["id"]):
                self.assertEqual(client.get(path).status_code, 405)
                self.assertEqual(client.post(path, data={"csrf_token": "bad"}).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    self.assertEqual(client.post(path + "?token=" + TOKEN, data=form["fields"]).status_code,
                                     403 if path.startswith("/api/") else 302)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                response = client.post(path, data=form["fields"])
                self.assertIn(response.status_code, (302, 401))
                self.authenticate(client)
            self.assertFalse(event.is_set())
            self.assertEqual(backup.BACKUP_JOBS_FILE.read_bytes(), original)
            with patch.object(backup, "persist_backup_job", side_effect=OSError("disk full")):
                response = client.post(form["action"], data=form["fields"])
            self.assertEqual(response.status_code, 500)
            retry = self.form(response, "/backup/cancel/")
            self.assertEqual(retry["action"], form["action"])
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 200)
            self.assertTrue(event.is_set())
            self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 200)
            progress = client.get("/backup?job_id=" + info["id"]).get_data(as_text=True)
            self.assertIn("状态：正在取消", progress)
            self.assertNotIn('action="/backup/cancel/', progress)

    def test_old_export_get_opens_selection_without_work_and_post_form_still_creates(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            client = app.test_client()
            with patch.object(backup, "start_backup_job") as start:
                response = client.get("/backup/export", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/backup#backup-create")
                start.assert_not_called()
                self.assertFalse(backup.BACKUP_JOBS_FILE.exists())
            self.authenticate(client)
            form = self.form(client.get(response.location), "/backup/create")
            with patch.object(backup, "write_dependencies_file"), patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                response = client.post(form["action"], data=dict(form["fields"], items=["scripts"]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(backup.get_backup_job(form["fields"]["request_id"])["status"], "已完成")
