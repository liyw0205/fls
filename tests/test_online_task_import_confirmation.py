import contextlib
import json
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineTaskImportConfirmationTests(unittest.TestCase):
    request_id = "a" * 32
    headers = {"X-Token": TOKEN}

    @contextlib.contextmanager
    def app(self):
        with isolated_app() as (app, base_dir), patch("fls_manager.app.dispatch_frontend_open_event"):
            from fls_manager.models import save_tasks
            from fls_manager.online_scripts import source

            source.save_online_script_cache([{
                "id": "demo", "name": "Demo", "type": "raw", "link": "https://fixture.invalid/demo.py",
                "task_cron": [{"name": "Daily", "command": "echo original", "cron": "", "var": {"A": "original"}},
                              {"name": "Excluded", "command": "echo excluded", "cron": ""}],
            }])
            save_tasks([])
            yield app, base_dir

    def submit(self, app):
        return app.test_client().post("/api/online-scripts/import-tasks/demo", headers=self.headers,
                                      json={"request_id": self.request_id, "task_indexes": ["1"], "enable_task": True})

    def confirm(self, app):
        return app.test_client().post("/api/online-scripts/import-tasks-confirm/" + self.request_id,
                                      headers=self.headers, json={})

    def failed_publication(self, app, after=True):
        from fls_manager import models, storage
        from fls_manager.online_scripts import tasks

        real = storage._write_json_unlocked

        def write(path, data):
            if path == models.TASK_FILE:
                if after:
                    real(path, data)
                raise OSError("task publication confirmation lost")
            return real(path, data)

        with patch.object(storage, "_write_json_unlocked", side_effect=write), patch.object(tasks, "reload_scheduler") as reload:
            result = self.submit(app)
            reload.assert_not_called()
        return result

    def test_published_task_error_requires_confirmation_then_only_scheduler_recovery(self):
        with self.app() as (app, _base_dir):
            from fls_manager import models
            from fls_manager.online_scripts import constants, tasks
            from fls_manager.routes.online_scripts import install_select

            failed = self.failed_publication(app)
            self.assertEqual(failed.status_code, 503)
            self.assertFalse(failed.json["retry_allowed"])
            original = models.TASK_FILE.read_bytes()
            record = constants.get_online_task_import_request(self.request_id)
            self.assertEqual(record["state"], "unknown")
            self.assertIn("task publication confirmation lost", record["message"])
            with patch.object(tasks, "reload_scheduler") as importer_reload, patch.object(install_select, "reload_scheduler") as recovery:
                self.assertEqual(self.submit(app).status_code, 409)
                checked = self.confirm(app)
                self.assertEqual(checked.status_code, 200)
                self.assertEqual(checked.json["state"], "scheduler_pending")
                self.assertEqual(checked.json["confirmation"], "published")
                self.assertTrue(self.confirm(app).json["replayed"])
                recovery.assert_not_called()
                url = "/api/online-scripts/import-tasks-reload/" + self.request_id
                self.assertEqual(app.test_client().post(url, headers=self.headers).status_code, 200)
                self.assertEqual(app.test_client().post(url, headers=self.headers).status_code, 200)
                recovery.assert_called_once()
                importer_reload.assert_not_called()
            self.assertEqual(models.TASK_FILE.read_bytes(), original)
            self.assertEqual(len(json.loads(original)), 1)

    def test_unpublished_task_error_can_explicitly_continue_original_request(self):
        with self.app() as (app, _base_dir):
            from fls_manager import models
            from fls_manager.online_scripts import constants, tasks

            original = models.TASK_FILE.read_bytes()
            failed = self.failed_publication(app, after=False)
            self.assertEqual(failed.status_code, 503)
            self.assertEqual(models.TASK_FILE.read_bytes(), original)
            checked = self.confirm(app)
            self.assertEqual(checked.status_code, 200)
            self.assertEqual(checked.json["confirmation"], "not_published")
            self.assertEqual(checked.json["state"], "failed")
            self.assertTrue(checked.json["retry_allowed"])
            with patch.object(tasks, "reload_scheduler") as reload:
                self.assertEqual(self.submit(app).status_code, 200)
                self.assertTrue(self.submit(app).json["replayed"])
                reload.assert_called_once()
            self.assertEqual(len(json.loads(models.TASK_FILE.read_text(encoding="utf-8"))), 1)
            self.assertEqual(constants.get_online_task_import_request(self.request_id)["state"], "completed")

    def test_history_offers_confirmation_then_only_scheduler_recovery(self):
        with self.app() as (app, _base_dir):
            self.failed_publication(app)
            client = app.test_client()
            history_url = "/online-scripts/import-tasks-history?recovery=1"
            confirm_action = f'action="/online-scripts/import-tasks-confirm/{self.request_id}"'
            reload_action = f'action="/online-scripts/import-tasks-reload/{self.request_id}"'

            unknown = client.get(history_url, headers=self.headers).get_data(as_text=True)
            self.assertIn(confirm_action, unknown)
            self.assertNotIn(reload_action, unknown)

            confirmed = self.confirm(app)
            self.assertEqual(confirmed.status_code, 200)
            pending = client.get(history_url, headers=self.headers).get_data(as_text=True)
            self.assertNotIn(confirm_action, pending)
            self.assertIn(reload_action, pending)

    def test_not_published_confirmation_becomes_non_retryable_after_later_task_edit(self):
        with self.app() as (app, _base_dir):
            from fls_manager import models
            from fls_manager.online_scripts import tasks

            self.failed_publication(app, after=False)
            confirmed = self.confirm(app)
            self.assertEqual(confirmed.json["confirmation"], "not_published")
            self.assertTrue(confirmed.json["retry_allowed"])
            changed = models.load_tasks()
            changed.append({"id": "manual", "name": "Manual", "command": "echo manual"})
            models.save_tasks(changed)
            current = models.TASK_FILE.read_bytes()
            status = app.test_client().get("/api/online-scripts/import-tasks-status/" + self.request_id,
                                          headers=self.headers)
            self.assertFalse(status.json["retry_allowed"])
            self.assertTrue(status.json["confirmation_stale"])
            with patch.object(tasks, "reload_scheduler") as reload:
                response = self.submit(app)
                self.assertEqual(response.status_code, 409)
                reload.assert_not_called()
            self.assertEqual(models.TASK_FILE.read_bytes(), current)

    def test_confirmation_receipt_write_failure_is_idempotently_recoverable(self):
        for after in (False, True):
            with self.subTest(after=after), self.app() as (app, _base_dir):
                from fls_manager.online_scripts import constants
                from fls_manager.routes.online_scripts import install_select

                self.failed_publication(app)
                real = constants.persist_online_task_import_request
                failed_once = False

                def persist(record):
                    nonlocal failed_once
                    if record.get("confirmation") == "published":
                        if failed_once:
                            return real(record)
                        failed_once = True
                        if after:
                            real(record)
                        raise OSError("confirmation receipt publication failed")
                    return real(record)

                with patch.object(install_select, "persist_online_task_import_request", side_effect=persist):
                    failed = self.confirm(app)
                    self.assertEqual(failed.status_code, 200 if after else 503)
                    if after:
                        self.assertTrue(failed.json["receipt_confirmed"])
                        self.assertTrue(failed.json["receipt_recovered"])
                        self.assertEqual(failed.json["request_id"], self.request_id)
                        self.assertEqual(failed.json["state_url"], "/api/online-scripts/import-tasks-status/" + self.request_id)
                        self.assertEqual(failed.json["state"], "scheduler_pending")
                    else:
                        recovered = self.confirm(app)
                        self.assertEqual(recovered.status_code, 200)
                        self.assertFalse(recovered.json["receipt_recovered"])
                    repeated = self.confirm(app)
                    self.assertEqual(repeated.status_code, 200)
                    self.assertTrue(repeated.json["replayed"])
                self.assertEqual(constants.get_online_task_import_request(self.request_id)["state"], "scheduler_pending")

    def test_confirmation_does_not_accept_a_different_persisted_snapshot(self):
        def conflict(record, field):
            updated = dict(record)
            if field == "request_id":
                updated[field] = "b" * 32
            elif field == "script_id":
                updated[field] = "other-script"
                updated["publication"] = dict(record["publication"], keys=["other-script:1:key"])
            elif field == "expected_version":
                updated[field] = "different-version"
            elif field == "selected_task_indexes":
                updated[field] = ["2"]
                updated["publication"] = dict(record["publication"], keys=["demo:2:key"], task_ids=["changed-task"])
            elif field == "publication":
                updated[field] = dict(record[field], after="f" * 64)
            elif field == "confirmation_source":
                updated[field] = "f" * 64
            elif field == "state":
                updated[field] = "failed"
            elif field == "confirmation":
                updated[field] = "not_published"
            return updated

        for field in ("request_id", "script_id", "expected_version", "selected_task_indexes",
                      "publication", "confirmation_source", "state", "confirmation"):
            with self.subTest(field=field), self.app() as (app, _base_dir):
                from fls_manager.online_scripts import constants
                from fls_manager.routes.online_scripts import install_select

                self.failed_publication(app)
                real = constants.persist_online_task_import_request
                changed = False
                conflicting_value = None

                def persist(record):
                    nonlocal changed, conflicting_value
                    if record.get("confirmation") == "published" and not changed:
                        changed = True
                        conflicting = conflict(record, field)
                        conflicting_value = conflicting[field]
                        real(conflicting)
                        raise OSError("confirmation response interrupted after a conflicting write")
                    return real(record)

                with patch.object(install_select, "persist_online_task_import_request", side_effect=persist):
                    response = self.confirm(app)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json["ok"])
                self.assertTrue(response.json["receipt_confirmation_required"])
                self.assertIn("不一致", response.json["msg"])
                state_url = "/api/online-scripts/import-tasks-status/" + self.request_id
                self.assertEqual(response.json["state_url"], state_url)
                status = app.test_client().get(state_url, headers=self.headers)
                self.assertEqual(status.status_code, 200)
                if field == "request_id":
                    self.assertEqual(status.json["state"], "unknown")
                else:
                    self.assertEqual(status.json[field], conflicting_value)

    def test_confirmation_receipt_read_failure_keeps_read_only_recovery_link(self):
        with self.app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_TASK_IMPORT_REQUESTS_FILE
            from fls_manager.routes.online_scripts import install_select

            self.failed_publication(app)
            original_write = ONLINE_TASK_IMPORT_REQUESTS_FILE.write_text

            def persist(record):
                if record.get("confirmation") == "published":
                    original_write("broken receipt", encoding="utf-8")
                    raise OSError("confirmation receipt fsync unavailable")
                raise AssertionError("unexpected receipt write")

            with patch.object(install_select, "persist_online_task_import_request", side_effect=persist):
                response = self.confirm(app)
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json["receipt_confirmation_required"])
            self.assertEqual(response.json["state_url"], "/api/online-scripts/import-tasks-status/" + self.request_id)
            self.assertIn("回执复核失败", response.json["msg"])
            self.assertEqual(ONLINE_TASK_IMPORT_REQUESTS_FILE.read_text(encoding="utf-8"), "broken receipt")

    def test_concurrent_confirmations_publish_once_and_replay_same_request(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import install_select

            self.failed_publication(app)
            real = constants.persist_online_task_import_request
            entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
            writes = []
            results = {}

            def persist(record):
                if record.get("confirmation") == "published":
                    writes.append(record["request_id"])
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("concurrent confirmation was not released")
                return real(record)

            def confirm(key, done=None):
                results[key] = self.confirm(app)
                if done:
                    done.set()

            with patch.object(install_select, "persist_online_task_import_request", side_effect=persist):
                first = threading.Thread(target=confirm, args=("first",))
                second = threading.Thread(target=confirm, args=("second", second_done))
                first.start()
                self.assertTrue(entered.wait(5))
                second.start()
                self.assertTrue(second_done.wait(2))
                self.assertEqual(results["second"].status_code, 409)
                self.assertTrue(results["second"].json["active"])
                release.set()
                first.join(5)
                second.join(5)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())

            self.assertEqual(len(writes), 1)
            self.assertEqual(results["first"].status_code, 200)
            self.assertEqual({response.json["request_id"] for response in results.values()}, {self.request_id})
            self.assertFalse(results["first"].json["replayed"])
            replay = self.confirm(app)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(constants.get_online_task_import_request(self.request_id)["state"], "scheduler_pending")

    def test_native_csrf_form_recovers_atomic_confirmation_write_exception(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import install_select

            self.failed_publication(app)
            client = app.test_client()
            csrf = "import-confirm-csrf"
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token=csrf)
            real = constants.persist_online_task_import_request

            def persist(record):
                real(record)
                if record.get("confirmation") == "published":
                    raise OSError("confirmation form response interrupted after replace")

            with patch.object(install_select, "persist_online_task_import_request", side_effect=persist):
                response = client.post(
                    "/online-scripts/import-tasks-confirm/" + self.request_id,
                    data={"csrf_token": csrf},
                )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.location, "/online-scripts/import-tasks-status/" + self.request_id)
            status = client.get("/api/online-scripts/import-tasks-status/" + self.request_id,
                                headers=self.headers)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["state"], "scheduler_pending")
            self.assertEqual(status.json["confirmation"], "published")

    def test_completed_receipt_failure_keeps_publication_evidence_for_restarted_query(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, tasks
            from fls_manager.routes.online_scripts import install_select

            real = install_select.persist_online_task_import_request

            def persist(record):
                if record["state"] == "completed":
                    raise OSError("completed receipt unavailable")
                return real(record)

            with patch.object(install_select, "persist_online_task_import_request", side_effect=persist), patch.object(tasks, "reload_scheduler") as reload:
                self.assertEqual(self.submit(app).status_code, 503)
                reload.assert_called_once()
            self.assertNotIn(self.request_id, constants.ONLINE_TASK_IMPORT_ACTIVE)
            self.assertEqual(constants.get_online_task_import_request(self.request_id)["state"], "unknown")
            checked = self.confirm(app)
            self.assertEqual(checked.status_code, 200)
            self.assertEqual(checked.json["confirmation"], "published")
            self.assertEqual(checked.json["state"], "scheduler_pending")

    def test_scheduler_reload_receipt_publication_failure_confirms_disk_state(self):
        for after in (False, True):
            with self.subTest(after=after), self.app() as (app, _base_dir):
                from fls_manager import models, storage
                from fls_manager.online_scripts import constants, tasks
                from fls_manager.routes.online_scripts import install_select

                with patch.object(tasks, "reload_scheduler", side_effect=RuntimeError("scheduler unavailable")):
                    pending = self.submit(app)
                self.assertEqual(pending.status_code, 503)
                self.assertEqual(constants.get_online_task_import_request(self.request_id)["state"], "scheduler_pending")
                task_file_before = models.TASK_FILE.read_bytes()

                receipt_file = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE
                write = storage._write_json_unlocked
                fail_once = {"value": True}

                def fail_receipt(path, data):
                    record = data.get(self.request_id, {}) if path == receipt_file else {}
                    if fail_once["value"] and record.get("state") == "completed":
                        fail_once["value"] = False
                        if after:
                            write(path, data)
                        raise OSError("scheduler completion receipt unavailable")
                    return write(path, data)

                reload_calls = []
                client = app.test_client()
                action = "/api/online-scripts/import-tasks-reload/" + self.request_id
                with patch.object(storage, "_write_json_unlocked", side_effect=fail_receipt), patch.object(
                    install_select, "reload_scheduler", side_effect=lambda: reload_calls.append(self.request_id),
                ):
                    response = client.post(action, headers=self.headers)
                    if after:
                        self.assertEqual(response.status_code, 200)
                        self.assertTrue(response.json["receipt_confirmed"])
                        self.assertEqual(response.json["state"], "completed")
                        repeated = client.post(action, headers=self.headers)
                        self.assertEqual(repeated.status_code, 200)
                        self.assertTrue(repeated.json["replayed"])
                    else:
                        self.assertEqual(response.status_code, 503)
                        self.assertTrue(response.json["receipt_confirmation_required"])
                        self.assertEqual(response.json["state_url"], "/api/online-scripts/import-tasks-status/" + self.request_id)
                        self.assertTrue(response.json["scheduler_reload_required"])
                        status = client.get(response.json["state_url"], headers=self.headers)
                        self.assertEqual(status.json["state"], "scheduler_pending")
                        status_page = client.get(response.json["status_url"], headers=self.headers)
                        self.assertIn("仅重新加载调度器", status_page.get_data(as_text=True))
                        with client.session_transaction() as session:
                            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                           security_verified=True, csrf_token="reload-csrf")
                        form_action = "/online-scripts/import-tasks-reload/" + self.request_id
                        denied = client.post(form_action, data={"csrf_token": "wrong"})
                        self.assertEqual(denied.status_code, 400)
                        self.assertEqual(len(reload_calls), 1)
                        recovered = client.post(form_action, data={"csrf_token": "reload-csrf"})
                        self.assertEqual(recovered.status_code, 302)
                self.assertEqual(len(reload_calls), 1 if after else 2)
                self.assertEqual(models.TASK_FILE.read_bytes(), task_file_before)
                self.assertEqual(constants.get_online_task_import_request(self.request_id)["state"], "completed")

    def test_scheduler_reload_error_keeps_actionable_html_form_and_csrf_boundary(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, tasks
            from fls_manager.routes.online_scripts import install_select

            with patch.object(tasks, "reload_scheduler", side_effect=RuntimeError("initial scheduler unavailable")):
                self.assertEqual(self.submit(app).status_code, 503)
            client = app.test_client()
            action = "/online-scripts/import-tasks-reload/" + self.request_id
            api_action = "/api/online-scripts/import-tasks-reload/" + self.request_id
            with patch.object(install_select, "reload_scheduler", side_effect=RuntimeError("scheduler still unavailable")) as reload:
                failed = client.post(api_action, headers=self.headers)
                self.assertEqual(failed.status_code, 503)
                self.assertTrue(failed.json["scheduler_reload_required"])
                self.assertEqual(failed.json["state"], "scheduler_pending")
                self.assertEqual(failed.json["status_url"], "/online-scripts/import-tasks-status/" + self.request_id)
                self.assertEqual(failed.json["state_url"], "/api/online-scripts/import-tasks-status/" + self.request_id)
                reload.assert_called_once()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="reload-csrf")
            with patch.object(install_select, "reload_scheduler", side_effect=RuntimeError("scheduler still unavailable")) as reload:
                denied = client.post(action, data={"csrf_token": "wrong"})
                self.assertEqual(denied.status_code, 400)
                reload.assert_not_called()
                failed_form = client.post(action, data={"csrf_token": "reload-csrf"})
                self.assertEqual(failed_form.status_code, 503)
                html = failed_form.get_data(as_text=True)
                self.assertIn(f'action="{action}"', html)
                self.assertIn("查询原导入请求", html)
                reload.assert_called_once()
            with patch.object(install_select, "reload_scheduler") as recovered:
                completed = client.post(action, data={"csrf_token": "reload-csrf"})
            self.assertEqual(completed.status_code, 302)
            recovered.assert_called_once()
            self.assertEqual(constants.get_online_task_import_request(self.request_id)["state"], "completed")

    def test_content_change_or_corrupt_task_file_keeps_unknown_and_never_reloads(self):
        for damage in ("edit", "corrupt"):
            with self.subTest(damage=damage), self.app() as (app, _base_dir):
                from fls_manager import models
                from fls_manager.online_scripts import tasks
                from fls_manager.routes.online_scripts import install_select

                self.failed_publication(app)
                if damage == "edit":
                    current = json.loads(models.TASK_FILE.read_text(encoding="utf-8"))
                    current[0]["command"] = "echo changed later"
                    models.save_tasks(current)
                else:
                    models.TASK_FILE.write_text("{broken", encoding="utf-8")
                with patch.object(tasks, "reload_scheduler") as importer_reload, patch.object(install_select, "reload_scheduler") as recovery_reload:
                    result = self.confirm(app)
                    self.assertEqual(result.status_code, 409)
                    self.assertEqual(self.submit(app).status_code, 409)
                    self.assertEqual(app.test_client().post("/api/online-scripts/import-tasks-reload/" + self.request_id,
                                                            headers=self.headers).status_code, 409)
                    importer_reload.assert_not_called()
                    recovery_reload.assert_not_called()
                state = app.test_client().get("/api/online-scripts/import-tasks-status/" + self.request_id,
                                              headers=self.headers).json
                self.assertEqual(state["state"], "unknown")
                self.assertFalse(state["retry_allowed"])
                page = app.test_client().get("/online-scripts/import-tasks-status/" + self.request_id,
                                             headers=self.headers).get_data(as_text=True)
                self.assertIn("检查原导入结果", page)
                self.assertNotIn("继续原导入请求", page)

    def test_confirmation_requires_auth_and_csrf_for_session_requests_and_form(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants

            self.failed_publication(app)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="confirm-csrf")
            for csrf in ("", "wrong"):
                response = client.post("/api/online-scripts/import-tasks-confirm/" + self.request_id,
                                       headers={"X-CSRF-Token": csrf})
                self.assertEqual(response.status_code, 400)
            with client.session_transaction() as session:
                session.pop("token")
            self.assertEqual(client.post("/api/online-scripts/import-tasks-confirm/" + self.request_id,
                                         headers={"X-CSRF-Token": "confirm-csrf"}).status_code, 401)
            self.assertEqual(constants.get_online_task_import_request(self.request_id)["state"], "unknown")
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True)
            self.assertEqual(client.get("/online-scripts/import-tasks-status/" + self.request_id).status_code, 200)
            response = client.post("/online-scripts/import-tasks-confirm/" + self.request_id,
                                   data={"csrf_token": "confirm-csrf"})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(client.get("/api/online-scripts/import-tasks-status/" + self.request_id,
                                        headers=self.headers).json["state"], "scheduler_pending")

    def test_confirmation_and_task_mutation_are_serialized_before_scheduler_recovery(self):
        with self.app() as (app, _base_dir):
            from fls_manager import models
            from fls_manager.online_scripts import tasks

            self.failed_publication(app)
            entered, release, changed = threading.Event(), threading.Event(), threading.Event()
            from fls_manager.routes.online_scripts import install_select
            real_confirm = install_select.task_import_publication_state
            results = {}

            def block(record):
                result = real_confirm(record)
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("confirmation was not released")
                return result

            def confirm():
                results["confirm"] = self.confirm(app)

            def mutate():
                current = models.load_tasks()
                current[0]["command"] = "echo concurrent change"
                models.save_tasks(current)
                changed.set()

            with patch.object(install_select, "task_import_publication_state", side_effect=block):
                checking = threading.Thread(target=confirm)
                checking.start()
                try:
                    self.assertTrue(entered.wait(5))
                    mutation = threading.Thread(target=mutate)
                    mutation.start()
                    self.assertFalse(changed.wait(0.1))
                    release.set()
                    checking.join(5)
                    mutation.join(5)
                    self.assertFalse(checking.is_alive())
                    self.assertFalse(mutation.is_alive())
                finally:
                    release.set()
            self.assertEqual(results["confirm"].status_code, 200)
            with patch("fls_manager.routes.online_scripts.install_select.reload_scheduler") as reload:
                response = app.test_client().post("/api/online-scripts/import-tasks-reload/" + self.request_id,
                                                  headers=self.headers)
                self.assertEqual(response.status_code, 409)
                reload.assert_not_called()
            status = app.test_client().get("/api/online-scripts/import-tasks-status/" + self.request_id,
                                          headers=self.headers)
            self.assertTrue(status.json.get("confirmation_stale") or status.json.get("confirmation_check_error"))
