import json
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineTaskImportConcurrencyTests(unittest.TestCase):
    def run_concurrent_imports(self, importer, items):
        gate = threading.Barrier(len(items) + 1)
        results = []
        errors = []

        def run(item):
            try:
                gate.wait(timeout=2)
                results.append(importer(item))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(item,)) for item in items]
        for thread in threads:
            thread.start()
        gate.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "online task import did not finish")
        self.assertEqual(errors, [])
        return results

    def task_item(self, script_id, task_name):
        return {
            "id": script_id,
            "name": f"Script {script_id}",
            "type": "raw",
            "link": f"https://example.invalid/{script_id}.py",
            "task_cron": [{"name": task_name, "command": f"task {script_id}"}],
        }

    def import_record(self, item, request_id="a" * 32, state="prepared"):
        from fls_manager.online_scripts import source, tasks

        normalized = source.normalize_online_scripts([item])[0]
        return {
            "request_id": request_id, "script_id": normalized["id"],
            "expected_version": tasks.online_task_import_version(normalized),
            "selected_task_indexes": ["1"], "enable_task": False, "state": state,
            "message": "original preparation diagnosis", "scheduler_reload_error": "", "updated_at": "old",
        }

    def test_orphaned_prepared_import_recovers_original_selection_via_api_history_and_form(self):
        for entry in ("status", "history", "form"):
            with self.subTest(entry=entry), isolated_app() as (app, _base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts import constants, source, tasks

                item, request_id = self.task_item("demo", "Daily"), "a" * 32
                source.save_online_script_cache([item])
                constants.persist_online_task_import_request(self.import_record(item, request_id))
                client, headers = app.test_client(), {"X-Token": TOKEN}
                url = {"status": "/api/online-scripts/import-tasks-status/" + request_id,
                       "history": "/api/online-scripts/import-tasks-history",
                       "form": "/online-scripts/import-tasks-status/" + request_id}[entry]
                with patch.object(tasks, "reload_scheduler") as reload:
                    response = client.get(url, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    record = constants.get_online_task_import_request(request_id)
                    self.assertEqual(record["state"], "failed")
                    self.assertIn("未写入任务", record["message"])
                    self.assertIn("original preparation diagnosis", record["message"])
                    self.assertEqual(record["selected_task_indexes"], ["1"])
                    self.assertEqual(load_tasks(), [])
                    reload.assert_not_called()
                    page = client.get("/online-scripts/import-tasks-status/" + request_id, headers=headers).get_data(as_text=True)
                    self.assertIn("继续原导入请求", page)
                    self.assertIn('name="task_indexes" value="1"', page)
                    form = {"request_id": request_id, "task_indexes": "1"}
                    self.assertEqual(client.post("/online-scripts/import-tasks/demo", data=form, headers=headers).status_code, 302)
                    self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=form, headers=headers).status_code, 200)
                    self.assertEqual(len(load_tasks()), 1)
                    reload.assert_called_once()
                self.assertNotIn(request_id, constants.ONLINE_TASK_IMPORT_ACTIVE)

    def test_claimed_prepared_import_remains_active_during_queries_and_concurrent_submit(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import constants, source, tasks
            from fls_manager.routes.online_scripts import install_select

            item, request_id = self.task_item("demo", "Daily"), "a" * 32
            source.save_online_script_cache([item])
            real_claim = install_select.claim_online_task_import_request
            entered, release = threading.Event(), threading.Event()
            responses = []

            def claim(record):
                result = real_claim(record)
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release prepared import")
                return result

            data, headers = {"request_id": request_id, "task_indexes": ["1"]}, {"X-Token": TOKEN}
            with patch.object(install_select, "claim_online_task_import_request", side_effect=claim), patch.object(tasks, "reload_scheduler") as reload:
                worker = threading.Thread(target=lambda: responses.append(app.test_client().post("/api/online-scripts/import-tasks/demo", json=data, headers=headers)))
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertIn(request_id, constants.ONLINE_TASK_IMPORT_ACTIVE)
                    before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
                    client = app.test_client()
                    for url in ("/api/online-scripts/import-tasks-status/" + request_id, "/api/online-scripts/import-tasks-history"):
                        response = client.get(url, headers=headers)
                        record = response.json if "status/" in url else response.json["items"][0]
                        self.assertEqual(record["state"], "prepared")
                        self.assertTrue(record["active"])
                    self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers).status_code, 409)
                    self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                    self.assertEqual(load_tasks(), [])
                finally:
                    release.set()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(responses[0].status_code, 200)
                self.assertEqual(len(load_tasks()), 1)
                reload.assert_called_once()
            self.assertNotIn(request_id, constants.ONLINE_TASK_IMPORT_ACTIVE)

    def test_running_receipt_failure_before_task_write_recovers_same_import_without_duplicate_tasks(self):
        for after in (False, True):
            with self.subTest(after=after), isolated_app() as (app, _base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts import constants, source, tasks
                from fls_manager.routes.online_scripts import install_select

                item, request_id = self.task_item("demo", "Daily"), "a" * 32
                source.save_online_script_cache([item])
                original = install_select.persist_online_task_import_request

                def persist(record):
                    if record["state"] == "running":
                        if after:
                            original(record)
                        raise OSError("startup receipt unavailable")
                    return original(record)

                client, headers = app.test_client(), {"X-Token": TOKEN}
                data = {"request_id": request_id, "task_indexes": ["1"]}
                with patch.object(install_select, "persist_online_task_import_request", side_effect=persist), patch.object(tasks, "reload_scheduler") as reload:
                    failed = client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers)
                    self.assertEqual(failed.status_code, 500)
                    self.assertIn("未写入任务", failed.json["msg"])
                    self.assertTrue(failed.json["retry_allowed"])
                    self.assertEqual(load_tasks(), [])
                    self.assertEqual(constants.get_online_task_import_request(request_id)["state"], "failed")
                    reload.assert_not_called()
                self.assertNotIn(request_id, constants.ONLINE_TASK_IMPORT_ACTIVE)
                with patch.object(tasks, "reload_scheduler") as reload:
                    self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers).status_code, 200)
                    self.assertTrue(client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers).json["replayed"])
                    reload.assert_called_once()
                    self.assertEqual(len(load_tasks()), 1)

    def test_import_prepared_recovery_preserves_binding_auth_csrf_and_invalid_selection_boundaries(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import constants, source, tasks

            item, request_id = self.task_item("demo", "Daily"), "a" * 32
            item["task_cron"].append({"name": "Second", "command": "echo second"})
            source.save_online_script_cache([item, self.task_item("other", "Other")])
            constants.persist_online_task_import_request(self.import_record(item, request_id))
            before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
            client = app.test_client()
            data = {"request_id": request_id, "task_indexes": ["1"]}
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="import-startup-csrf")
            with patch.object(tasks, "import_task_if_needed") as importer, patch.object(tasks, "reload_scheduler") as reload, patch("fls_manager.app.dispatch_frontend_open_event"):
                for indexes in (["0"], ["999"], ["bad"], []):
                    failed = client.post("/api/online-scripts/import-tasks/demo", json=dict(data, task_indexes=indexes), headers={"X-CSRF-Token": "import-startup-csrf"})
                    self.assertEqual(failed.status_code, 400)
                for script_id, extra in (("other", {}), ("demo", {"task_indexes": ["2"]}), ("demo", {"enable_task": True})):
                    failed = client.post("/api/online-scripts/import-tasks/" + script_id, json=dict(data, **extra), headers={"X-CSRF-Token": "import-startup-csrf"})
                    self.assertEqual(failed.status_code, 409)
                for csrf in ("", "wrong"):
                    self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers={"X-CSRF-Token": csrf}).status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers={"X-CSRF-Token": "import-startup-csrf"}).status_code, 401)
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                self.assertEqual(load_tasks(), [])
                importer.assert_not_called()
                reload.assert_not_called()
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="import-startup-csrf")
                self.assertEqual(client.get("/api/online-scripts/import-tasks-status/" + request_id).json["state"], "failed")

    def test_import_source_recheck_and_failure_receipt_faults_release_claim_and_keep_real_startup_state(self):
        for recheck in (False, True):
            for after in (False, True):
                with self.subTest(recheck=recheck, after=after), isolated_app() as (app, _base_dir):
                    from fls_manager.models import load_tasks
                    from fls_manager.online_scripts import constants, source, tasks
                    from fls_manager.routes.online_scripts import install_select

                    item, request_id = self.task_item("demo", "Daily"), "a" * 32
                    source.save_online_script_cache([item])
                    normalized = source.load_online_script_cache()[0]
                    real_persist = install_select.persist_online_task_import_request

                    def fail_receipts(record):
                        if after and (record["state"] == "running" or recheck):
                            real_persist(record)
                        raise OSError("startup and recovery receipts unavailable")

                    gets = [normalized, OSError("source recheck unavailable")] if recheck else [normalized, normalized]
                    client, headers = app.test_client(), {"X-Token": TOKEN}
                    data = {"request_id": request_id, "task_indexes": ["1"]}
                    with patch.object(install_select, "get_online_script", side_effect=gets), patch.object(install_select, "persist_online_task_import_request", side_effect=fail_receipts), patch.object(install_select, "import_task_if_needed") as importer:
                        failed = client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers)
                        self.assertEqual(failed.status_code, 503)
                        self.assertFalse(failed.json["retry_allowed"])
                        self.assertNotIn(request_id, constants.ONLINE_TASK_IMPORT_ACTIVE)
                        self.assertEqual(load_tasks(), [])
                        importer.assert_not_called()
                    status = client.get("/api/online-scripts/import-tasks-status/" + request_id, headers=headers)
                    expected = "failed"
                    self.assertEqual(status.json["state"], expected)
                    with patch.object(tasks, "reload_scheduler") as reload:
                        retried = client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers)
                        self.assertEqual(retried.status_code, 409 if expected == "unknown" else 200)
                        self.assertEqual(reload.call_count, 0 if expected == "unknown" else 1)

    def test_import_prepared_symlink_or_corrupt_receipts_do_not_recover_or_overwrite(self):
        for kind in ("symlink", "corrupt"):
            with self.subTest(kind=kind), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import constants, source

                item, request_id = self.task_item("demo", "Daily"), "a" * 32
                source.save_online_script_cache([item])
                path = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE
                if kind == "symlink":
                    target = base_dir / "original.json"
                    target.write_text(json.dumps({request_id: self.import_record(item, request_id)}), encoding="utf-8")
                    path.symlink_to(target)
                else:
                    path.write_text("corrupt", encoding="utf-8")
                before = path.read_bytes()
                client, headers = app.test_client(), {"X-Token": TOKEN}
                for url in ("/api/online-scripts/import-tasks-status/" + request_id, "/api/online-scripts/import-tasks-history", "/online-scripts/import-tasks-status/" + request_id):
                    failed = client.get(url, headers=headers)
                    self.assertEqual(failed.status_code, 500)
                    self.assertEqual(path.read_bytes(), before)
                self.assertNotIn(request_id, constants.ONLINE_TASK_IMPORT_ACTIVE)

    def test_concurrent_same_script_import_is_idempotent(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import tasks

            item = self.task_item("demo", "Daily")
            with patch.object(tasks, "reload_scheduler") as reload_scheduler:
                results = self.run_concurrent_imports(
                    lambda current: tasks.import_task_if_needed(current), [item, item],
                )

            self.assertEqual(sum(bool(result[0]) for result in results), 1)
            self.assertEqual(len(load_tasks()), 1)
            self.assertEqual(load_tasks()[0]["online_script_task_key"], "demo:1:Daily")
            reload_scheduler.assert_called_once()

    def test_concurrent_distinct_imports_preserve_both_task_sets(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import tasks

            items = [self.task_item("alpha", "Alpha task"), self.task_item("beta", "Beta task")]
            active_reloads = 0
            max_active_reloads = 0
            reload_guard = threading.Lock()

            def reload_scheduler():
                nonlocal active_reloads, max_active_reloads
                with reload_guard:
                    active_reloads += 1
                    max_active_reloads = max(max_active_reloads, active_reloads)
                time.sleep(0.03)
                with reload_guard:
                    active_reloads -= 1

            with patch.object(tasks, "reload_scheduler", side_effect=reload_scheduler) as reload_mock:
                results = self.run_concurrent_imports(
                    lambda current: tasks.import_task_if_needed(current), items,
                )

            self.assertTrue(all(result[0] for result in results))
            self.assertEqual(
                {task["online_script_id"] for task in load_tasks()},
                {"alpha", "beta"},
            )
            self.assertEqual(reload_mock.call_count, 2)
            self.assertEqual(max_active_reloads, 1)

    def test_persistence_failure_is_reported_without_scheduler_reload(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.online_scripts import tasks

            item = self.task_item("demo", "Daily")
            with patch("fls_manager.models.mutate_json", side_effect=OSError("task store is read-only")), patch.object(
                tasks, "reload_scheduler",
            ) as reload_scheduler:
                with self.assertRaisesRegex(OSError, "task store is read-only"):
                    tasks.import_task_if_needed(item)
            reload_scheduler.assert_not_called()

    def test_legacy_marker_and_task_selection_are_preserved(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.models import load_tasks, save_tasks
            from fls_manager.online_scripts import tasks

            save_tasks([{
                "id": "legacy",
                "name": "Legacy task",
                "command": "task demo",
                "online_script_id": "demo",
            }])
            item = self.task_item("demo", "Daily")
            with patch.object(tasks, "reload_scheduler") as reload_scheduler:
                imported, _message = tasks.import_task_if_needed(item)
            self.assertFalse(imported)
            reload_scheduler.assert_not_called()

            item["task_cron"].append({"name": "Hourly", "command": "task demo-hourly"})
            with patch.object(tasks, "reload_scheduler"):
                imported, _message = tasks.import_task_if_needed(item, selected_task_indexes=[2])
            self.assertTrue(imported)
            self.assertEqual(
                {task.get("online_script_task_key") for task in load_tasks()},
                {None, "demo:2:Hourly"},
            )

    def test_scheduler_reload_error_reports_that_task_was_persisted(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import tasks

            with patch.object(tasks, "reload_scheduler", side_effect=RuntimeError("scheduler unavailable")):
                imported, message = tasks.import_task_if_needed(self.task_item("demo", "Daily"))

            self.assertTrue(imported)
            self.assertIn("任务已持久化", message)
            self.assertIn("scheduler unavailable", message)
            self.assertEqual(len(load_tasks()), 1)

    def test_import_route_keeps_storage_error_visible(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps([self.task_item("demo", "Daily")]), encoding="utf-8")
            with patch("fls_manager.models.mutate_json", side_effect=OSError("task store is read-only")):
                response = app.test_client().post(
                    "/online-scripts/import-tasks/demo",
                    data={"task_indexes": "1", "request_id": "f" * 32},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 500)
            self.assertNotIn("Location", response.headers)
            self.assertIn("task store is read-only", response.get_data(as_text=True))
            self.assertIn('/online-scripts/install-select/demo', response.get_data(as_text=True))
            status = app.test_client().get(
                "/online-scripts/import-tasks-status/" + "f" * 32,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 200)
            self.assertIn("继续原导入请求", status.get_data(as_text=True))

    def test_import_route_rejects_invalid_selection_without_task_or_scheduler_mutation(self):
        for indexes in ([], ["bad"], ["0"], ["2"], ["1", "bad"]):
            with self.subTest(indexes=indexes), isolated_app() as (app, _base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts.source import save_online_script_cache

                save_online_script_cache([self.task_item("demo", "Daily")])
                with patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/online-scripts/import-tasks/demo", data={"task_indexes": indexes},
                        headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                    )
                self.assertEqual(response.status_code, 400)
                self.assertIn("重新选择任务", response.get_data(as_text=True))
                self.assertNotIn("Location", response.headers)
                self.assertEqual(load_tasks(), [])
                reload_scheduler.assert_not_called()

    def test_import_route_reload_failure_retains_tasks_and_offers_scheduler_recovery(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.task_item("demo", "Daily")])
            client = app.test_client()
            with patch("fls_manager.online_scripts.tasks.reload_scheduler", side_effect=RuntimeError("scheduler unavailable")):
                response = client.post(
                    "/online-scripts/import-tasks/demo", data={"task_indexes": "1"},
                    headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                )
            self.assertEqual(response.status_code, 503)
            html = response.get_data(as_text=True)
            self.assertIn("任务已持久化", html)
            self.assertIn("scheduler unavailable", html)
            self.assertIn('action="/scheduler/reload?back=/tasks"', html)
            self.assertIn('name="csrf_token"', html)
            self.assertEqual(len(load_tasks()), 1)
            with patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload_scheduler:
                repeated = client.post(
                    "/online-scripts/import-tasks/demo", data={"task_indexes": "1"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(repeated.status_code, 409)
            self.assertEqual(len(load_tasks()), 1)
            reload_scheduler.assert_not_called()
            with patch("fls_manager.routes.dashboard.reload_scheduler") as reload_scheduler:
                recovered = client.post("/scheduler/reload?back=/tasks", headers={"X-Token": TOKEN})
            self.assertEqual(recovered.status_code, 302)
            self.assertEqual(recovered.headers["Location"], "/tasks")
            reload_scheduler.assert_called_once()
            self.assertEqual(len(load_tasks()), 1)

    def test_stable_import_replay_does_not_write_or_reload_twice(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.task_item("demo", "Daily")])
            request_id = "a" * 32
            client = app.test_client()
            with patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload_scheduler:
                first = client.post(
                    "/api/online-scripts/import-tasks/demo",
                    json={"request_id": request_id, "task_indexes": ["1"]},
                    headers={"X-Token": TOKEN},
                )
                repeated = client.post(
                    "/api/online-scripts/import-tasks/demo",
                    json={"request_id": request_id, "task_indexes": ["1"]},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(repeated.status_code, 200)
            self.assertTrue(repeated.json["replayed"])
            reload_scheduler.assert_called_once()
            self.assertEqual(len(load_tasks()), 1)
            status = client.get(
                "/api/online-scripts/import-tasks-status/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["state"], "completed")
            page = client.get(
                "/online-scripts/import-tasks-status/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(page.status_code, 200)
            self.assertIn("任务导入已完成", page.get_data(as_text=True))

    def test_import_history_api_and_page_expose_scheduler_recovery_without_reimport(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, source, tasks

            source.save_online_script_cache([self.task_item("demo", "Daily")])
            item = source.load_online_script_cache()[0]
            expected_version = tasks.online_task_import_version(item)
            constants.persist_online_task_import_request({
                "request_id": "9" * 32, "script_id": "demo", "expected_version": expected_version,
                "selected_task_indexes": ["1"], "enable_task": True, "state": "completed",
                "message": "任务已保存，调度器已恢复", "scheduler_reload_error": "",
                "updated_at": "2026-11-18 10:00:00",
            })
            constants.persist_online_task_import_request({
                "request_id": "a" * 32, "script_id": "demo", "expected_version": expected_version,
                "selected_task_indexes": ["1"], "enable_task": False, "state": "scheduler_pending",
                "message": "任务已保存，调度器待恢复 <script>alert(1)</script>",
                "scheduler_reload_error": "scheduler down",
                "updated_at": "2026-11-18 11:00:00",
            })
            other_item = self.task_item("other", "Other")
            constants.persist_online_task_import_request({
                **self.import_record(other_item, request_id="b" * 32, state="failed"),
                "message": "other-only diagnosis",
            })
            client = app.test_client()
            history = client.get(
                "/api/online-scripts/import-tasks-history?limit=1&script_id=demo",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json["items"][0]["request_id"], "a" * 32)
            page = client.get("/online-scripts/import-tasks-history?script_id=demo", headers={"X-Token": TOKEN})
            html = page.get_data(as_text=True)
            self.assertIn("仅重载调度器", html)
            self.assertIn("/online-scripts/import-tasks-status/" + "9" * 32, html)
            self.assertIn('name="script_id" value="demo"', html)
            self.assertIn("任务已保存，调度器待恢复 &lt;script&gt;alert(1)&lt;/script&gt;", html)
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertIn("scheduler down", html)
            self.assertIn('<th>导入后启用</th>', html)
            self.assertIn("<td>是</td>", html)
            self.assertIn("<td>否</td>", html)
            self.assertNotIn("other-only diagnosis", html)
            self.assertIn("script_id=demo", html)
            with patch("fls_manager.routes.online_scripts.install_select.reload_scheduler") as reload_scheduler:
                response = client.post(
                    "/api/online-scripts/import-tasks-reload/" + "a" * 32,
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 200)
            reload_scheduler.assert_called_once()
            self.assertEqual(client.get(
                "/api/online-scripts/import-tasks-status/" + "a" * 32,
                headers={"X-Token": TOKEN},
            ).json["state"], "completed")

    def test_import_history_rejects_invalid_limit_and_corrupt_receipt_without_overwrite(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            client = app.test_client()
            invalid = client.get(
                "/api/online-scripts/import-tasks-history?limit=0",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid.status_code, 400)
            constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.write_text("not-json", encoding="utf-8")
            original = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
            failed = client.get("/api/online-scripts/import-tasks-history", headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            page = client.get("/online-scripts/import-tasks-history", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 500)
            self.assertIn("无法读取", page.get_data(as_text=True))
            self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), original)

    def test_stale_running_import_becomes_unknown_after_restart_and_releases_active_marker(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, source, tasks

            source.save_online_script_cache([self.task_item("demo", "Daily")])
            item = source.load_online_script_cache()[0]
            request_id = "b" * 32
            constants.persist_online_task_import_request({
                "request_id": request_id, "script_id": "demo",
                "expected_version": tasks.online_task_import_version(item),
                "selected_task_indexes": ["1"], "enable_task": False,
                "state": "running", "message": "", "scheduler_reload_error": "",
                "updated_at": "2026-11-18 12:00:00",
            })
            self.assertNotIn(request_id, constants.ONLINE_TASK_IMPORT_ACTIVE)
            status = app.test_client().get(
                "/api/online-scripts/import-tasks-status/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["state"], "unknown")
            self.assertIn("面板重启", status.json["message"])

    def test_scheduler_pending_has_query_only_replay_and_dedicated_reload(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.task_item("demo", "Daily")])
            request_id = "b" * 32
            client = app.test_client()
            with patch("fls_manager.online_scripts.tasks.reload_scheduler", side_effect=RuntimeError("scheduler down")) as reload_scheduler:
                failed = client.post(
                    "/api/online-scripts/import-tasks/demo",
                    json={"request_id": request_id, "task_indexes": ["1"]},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed.status_code, 503)
            self.assertTrue(failed.json["scheduler_reload_required"])
            self.assertEqual(len(load_tasks()), 1)
            with patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload_again:
                repeated = client.post(
                    "/api/online-scripts/import-tasks/demo",
                    json={"request_id": request_id, "task_indexes": ["1"]},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(repeated.status_code, 503)
            reload_again.assert_not_called()
            with patch("fls_manager.routes.online_scripts.install_select.reload_scheduler") as reload_scheduler:
                recovered = client.post(
                    "/api/online-scripts/import-tasks-reload/" + request_id,
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(recovered.status_code, 200)
            reload_scheduler.assert_called_once()
            self.assertEqual(client.get("/api/online-scripts/import-tasks-status/" + request_id, headers={"X-Token": TOKEN}).json["state"], "completed")

    def test_invalid_import_request_id_has_zero_task_and_scheduler_side_effects(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.task_item("demo", "Daily")])
            with patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload_scheduler:
                response = app.test_client().post(
                    "/api/online-scripts/import-tasks/demo",
                    json={"request_id": "../bad", "task_indexes": ["1"]},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 400)
            reload_scheduler.assert_not_called()
            self.assertEqual(load_tasks(), [])

    def test_source_change_after_claim_rejects_before_task_write(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import source

            item = self.task_item("demo", "Daily")
            source.save_online_script_cache([item])
            replacement = self.task_item("demo", "Changed")

            def claim(record):
                source.save_online_script_cache([replacement])
                return None

            with patch("fls_manager.routes.online_scripts.install_select.claim_online_task_import_request", side_effect=claim), patch(
                "fls_manager.online_scripts.tasks.reload_scheduler",
            ) as reload_scheduler:
                response = app.test_client().post(
                    "/api/online-scripts/import-tasks/demo",
                    json={"request_id": "e" * 32, "task_indexes": ["1"]},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("脚本源已更新或删除", response.json["msg"])
            self.assertEqual(load_tasks(), [])
            reload_scheduler.assert_not_called()

    def test_import_and_install_forms_reject_invalid_exclusions_or_selection_mode(self):
        for route in ("import-tasks", "install"):
            for form in (
                {"select_mode": "all", "excluded_task_indexes": "bad"},
                {"select_mode": "all", "excluded_task_indexes": "2"},
                {"select_mode": "all", "excluded_task_indexes": "1"},
                {"select_mode": "bad", "task_indexes": "1"},
            ):
                with self.subTest(route=route, form=form), isolated_app() as (app, _base_dir):
                    from fls_manager.models import load_tasks
                    from fls_manager.online_scripts.source import save_online_script_cache

                    save_online_script_cache([self.task_item("demo", "Daily")])
                    with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start, patch(
                        "fls_manager.online_scripts.tasks.reload_scheduler",
                    ) as reload_scheduler:
                        response = app.test_client().post(
                            f"/online-scripts/{route}/demo", data={**form, "import_task": "1"},
                            headers={"X-Token": TOKEN},
                        )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(load_tasks(), [])
                    start.assert_not_called()
                    reload_scheduler.assert_not_called()

    def test_import_route_success_and_repeated_submission_preserve_selected_tasks(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.source import save_online_script_cache

            item = self.task_item("demo", "Daily")
            item["task_cron"].append({"name": "Hourly", "command": "task demo-hourly"})
            save_online_script_cache([item])
            client = app.test_client()
            with patch("fls_manager.online_scripts.tasks.reload_scheduler") as reload_scheduler:
                first = client.post(
                    "/online-scripts/import-tasks/demo",
                    data={"select_mode": "all", "excluded_task_indexes": "1", "enable_task": "1"},
                    headers={"X-Token": TOKEN},
                )
                second = client.post(
                    "/online-scripts/import-tasks/demo", data={"task_indexes": "2"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 302)
            self.assertEqual(second.status_code, 409)
            self.assertEqual([task["name"] for task in load_tasks()], ["Hourly"])
            self.assertTrue(load_tasks()[0]["enabled"])
            reload_scheduler.assert_called_once()

    def test_import_route_rejects_missing_scripts_and_checks_session_and_csrf(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.source import save_online_script_cache

            save_online_script_cache([self.task_item("demo", "Daily")])
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with patch("fls_manager.routes.online_scripts.install_select.import_task_if_needed") as importer:
                missing = client.post("/online-scripts/import-tasks/deleted", headers={"X-Token": TOKEN})
                self.assertEqual(missing.status_code, 404)
                importer.assert_not_called()
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="import-csrf")
                denied = client.post("/online-scripts/import-tasks/demo", data={"task_indexes": "1"}, headers=headers)
                self.assertEqual(denied.status_code, 400)
                importer.assert_not_called()
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(
                    "/online-scripts/import-tasks/demo", data={"task_indexes": "1", "csrf_token": "import-csrf"},
                    headers=headers,
                )
                self.assertEqual(expired.status_code, 401)
                importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
