import contextlib
import json
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineTaskImportRetentionTests(unittest.TestCase):
    @contextlib.contextmanager
    def app(self):
        with isolated_app() as (app, base_dir), patch("fls_manager.app.dispatch_frontend_open_event"):
            yield app, base_dir

    def item(self):
        return {"id": "demo", "name": "Demo", "type": "raw", "link": "https://fixture.invalid/demo.py",
                "task_cron": [{"name": "Task", "command": "echo demo", "cron": ""}]}

    def record(self, constants, request_id, state="completed", updated_at="2099-01-01 00:00:00"):
        from fls_manager.online_scripts import source, tasks

        item = source.normalize_online_scripts([self.item()])[0]
        return constants._task_import_snapshot({
            "request_id": request_id, "script_id": "demo", "expected_version": tasks.online_task_import_version(item),
            "selected_task_indexes": ["1"], "enable_task": False, "state": state,
            "message": "original import diagnosis", "scheduler_reload_error": "original reload diagnosis",
            "updated_at": updated_at,
        })

    def seed(self, constants, count=200, state="completed"):
        from fls_manager.online_scripts import source

        source.save_online_script_cache([self.item()])
        records = {f"{index:032x}": self.record(constants, f"{index:032x}", state) for index in range(count)}
        constants.mutate_json(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE, {}, lambda _: records, strict=True)
        return records

    def test_pruning_keeps_all_unresolved_states_active_and_current_receipts(self):
        for operation in ("persist", "claim"):
            with self.subTest(operation=operation), self.app() as (_app, _base_dir):
                from fls_manager.online_scripts import constants

                records = self.seed(constants)
                protected = []
                for index, state in enumerate(("prepared", "running", "failed", "unknown", "scheduler_pending", "completed")):
                    request_id = f"{index:032x}"
                    protected.append(request_id)
                    records[request_id].update(state=state, updated_at="old")
                constants.ONLINE_TASK_IMPORT_ACTIVE.add(protected[-1])
                constants.mutate_json(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE, {}, lambda _: records, strict=True)
                new_id = "f" * 32
                record = self.record(constants, new_id, "prepared", "older")
                if operation == "claim":
                    self.assertIsNone(constants.claim_online_task_import_request(record))
                else:
                    constants.persist_online_task_import_request(record)
                actual = json.loads(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_text())
                self.assertEqual(len(actual), 200)
                self.assertTrue(set(protected + [new_id]) <= actual.keys())
                self.assertNotIn(f"{199:032x}", actual)

    def test_full_capacity_rejects_api_and_form_without_import_or_reload(self):
        with self.app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import install_select

            self.seed(constants, state="failed")
            before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
            client, headers = app.test_client(), {"X-Token": TOKEN}
            with patch.object(install_select, "import_task_if_needed") as importer, patch.object(install_select, "reload_scheduler") as reload:
                data = {"request_id": "e" * 32, "task_indexes": "1"}
                api = client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers)
                self.assertEqual(api.status_code, 409)
                self.assertFalse(api.json["accepted"])
                self.assertFalse(api.json["retry_allowed"])
                self.assertEqual(api.json["history_url"], "/online-scripts/import-tasks-history?recovery=1")
                form = client.post("/online-scripts/import-tasks/demo", data=data, headers=headers)
                self.assertEqual(form.status_code, 409)
                self.assertIn(api.json["history_url"], form.get_data(as_text=True))
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                self.assertEqual(load_tasks(), [])
                self.assertNotIn(data["request_id"], constants.ONLINE_TASK_IMPORT_ACTIVE)
                importer.assert_not_called()
                reload.assert_not_called()

    def test_missing_active_receipt_blocks_reclaim_and_reserves_writeback_capacity(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import install_select

            self.seed(constants, count=199, state="failed")
            request_id = "e" * 32
            constants.ONLINE_TASK_IMPORT_ACTIVE.add(request_id)
            client, headers = app.test_client(), {"X-Token": TOKEN}
            before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
            with patch.object(install_select, "import_task_if_needed") as importer:
                for url in ("/api/online-scripts/import-tasks-status/", "/online-scripts/import-tasks-status/"):
                    self.assertEqual(client.get(url + request_id, headers=headers).status_code, 503)
                for suffix in ("archive", "reload"):
                    failed = client.post(f"/api/online-scripts/import-tasks-{suffix}/{request_id}", json={"acknowledge": "archive"}, headers=headers)
                    self.assertEqual(failed.status_code, 503)
                response = client.post("/api/online-scripts/import-tasks/demo", json={"request_id": request_id, "task_indexes": "1"}, headers=headers)
                self.assertEqual(response.status_code, 503)
                self.assertTrue(response.json["receipt_missing"])
                self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json={"request_id": "f" * 32, "task_indexes": "1"}, headers=headers).status_code, 409)
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                importer.assert_not_called()
            constants.persist_online_task_import_request(self.record(constants, request_id, "running", "old"))
            self.assertEqual(len(json.loads(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_text())), 200)
            self.assertTrue(constants.get_online_task_import_request(request_id)["active"])

    def test_archive_preserves_real_result_frees_capacity_and_blocks_retry_and_reload(self):
        for entry in ("api", "form"):
            for state in ("failed", "unknown", "scheduler_pending"):
                with self.subTest(entry=entry, state=state), self.app() as (app, _base_dir):
                    from fls_manager.models import load_tasks
                    from fls_manager.online_scripts import constants, tasks

                    records = self.seed(constants, state=state)
                    request_id = "0" * 32
                    client, headers = app.test_client(), {"X-Token": TOKEN}
                    before = records[request_id]
                    url = ("/api" if entry == "api" else "") + "/online-scripts/import-tasks-archive/" + request_id
                    with patch.object(tasks, "reload_scheduler") as reload:
                        archived = client.post(url, data={"acknowledge": "archive"}, headers=headers)
                        self.assertEqual(archived.status_code, 200 if entry == "api" else 302)
                        actual = constants.get_online_task_import_request(request_id)
                        self.assertTrue(actual["archived"])
                        for key, value in before.items():
                            if key not in ("archived", "archived_at"):
                                self.assertEqual(actual[key], value)
                        stored = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
                        repeat = client.post("/api/online-scripts/import-tasks-archive/" + request_id, json={"acknowledge": "archive"}, headers=headers)
                        self.assertTrue(repeat.json["replayed"])
                        self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), stored)
                        data = {"request_id": request_id, "task_indexes": "1"}
                        self.assertEqual(client.post("/api/online-scripts/import-tasks/demo", json=data, headers=headers).status_code, 409)
                        self.assertEqual(client.post("/api/online-scripts/import-tasks-reload/" + request_id, headers=headers).status_code, 409)
                        self.assertEqual(load_tasks(), [])
                        reload.assert_not_called()
                        accepted = client.post("/api/online-scripts/import-tasks/demo", json=dict(data, request_id="f" * 32), headers=headers)
                        self.assertEqual(accepted.status_code, 200)
                        self.assertEqual(len(load_tasks()), 1)
                        reload.assert_called_once()
                    self.assertIsNone(constants.get_online_task_import_request(request_id))

    def test_archive_invalid_choice_permissions_csrf_and_active_states_have_no_side_effects(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import install_select

            self.seed(constants, count=1, state="failed")
            client, request_id = app.test_client(), "0" * 32
            api = "/api/online-scripts/import-tasks-archive/" + request_id
            form = "/online-scripts/import-tasks-archive/" + request_id
            headers = {"X-Token": TOKEN}
            before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
            with client.session_transaction() as session:
                session["csrf_token"] = "archive-csrf"
            with patch.object(install_select, "import_task_if_needed") as importer, patch.object(install_select, "reload_scheduler") as reload:
                self.assertEqual(client.post(api, json={"acknowledge": "archive"}, headers={"X-CSRF-Token": "archive-csrf"}).status_code, 401)
                for choice in (None, "", "yes", True, ["archive"]):
                    self.assertEqual(client.post(api, json={"acknowledge": choice}, headers=headers).status_code, 400)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="archive-csrf")
                for csrf in ("", "wrong"):
                    self.assertEqual(client.post(api, json={"acknowledge": "archive"}, headers={"X-CSRF-Token": csrf}).status_code, 400)
                    self.assertEqual(client.post(form, data={"acknowledge": "archive", "csrf_token": csrf}).status_code, 400)
                self.assertEqual(client.post(api + "?token=blocked", json={"acknowledge": "archive"}, headers={"X-CSRF-Token": "archive-csrf"}).status_code, 403)
                constants.ONLINE_TASK_IMPORT_ACTIVE.add(request_id)
                self.assertEqual(client.post(api, json={"acknowledge": "archive"}, headers=headers).status_code, 409)
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                constants.ONLINE_TASK_IMPORT_ACTIVE.clear()
                importer.assert_not_called()
                reload.assert_not_called()
                self.assertEqual(client.post(form, data={"acknowledge": "archive", "csrf_token": "archive-csrf"}).status_code, 302)

    def test_archive_publication_failures_recover_real_state_and_preserve_diagnosis(self):
        for after in (False, True):
            with self.subTest(after=after), self.app() as (app, _base_dir):
                from fls_manager.online_scripts import constants
                from fls_manager.routes.online_scripts import install_select

                self.seed(constants, count=1, state="unknown")
                request_id, headers = "0" * 32, {"X-Token": TOKEN}
                real = install_select.persist_online_task_import_request

                def fail(record):
                    if after:
                        real(record)
                    raise OSError("archive acknowledgement unavailable")

                client, url = app.test_client(), "/api/online-scripts/import-tasks-archive/" + request_id
                with patch.object(install_select, "persist_online_task_import_request", side_effect=fail):
                    response = client.post(url, json={"acknowledge": "archive"}, headers=headers)
                    self.assertEqual(response.status_code, 503)
                actual = client.get("/api/online-scripts/import-tasks-status/" + request_id, headers=headers).json
                self.assertEqual(actual["archived"], after)
                self.assertEqual(actual["state"], "unknown")
                self.assertEqual(actual["message"], "original import diagnosis")
                recovered = client.post(url, json={"acknowledge": "archive"}, headers=headers)
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.json["replayed"], after)

    def test_recovery_history_paginates_older_receipts_and_invalid_filters_do_not_write(self):
        with self.app() as (app, _base_dir):
            from fls_manager.online_scripts import constants

            self.seed(constants, state="unknown")
            client, headers = app.test_client(), {"X-Token": TOKEN}
            seen = []
            for offset in (0, 100):
                response = client.get(f"/api/online-scripts/import-tasks-history?recovery=1&limit=100&offset={offset}", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["total"], 200)
                self.assertEqual(response.json["offset"], offset)
                self.assertEqual(response.json["has_more"], offset == 0)
                seen += [item["request_id"] for item in response.json["items"]]
            self.assertEqual(len(set(seen)), 200)
            page = client.get("/online-scripts/import-tasks-history?recovery=1&script_id=demo&limit=100", headers=headers).get_data(as_text=True)
            self.assertIn("offset=100", page)
            self.assertIn("recovery=1", page)
            self.assertIn("script_id=demo", page)
            before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
            invalid_queries = (
                "recovery=bad", "offset=-1", "offset=201", "offset=bad", "limit=0",
                "script_id=%01", "script_id=demo%0A", "script_id=%7f",
                "script_id=" + ("x" * 201),
            )
            for query in invalid_queries:
                for prefix in ("/api", ""):
                    self.assertEqual(client.get(prefix + "/online-scripts/import-tasks-history?" + query, headers=headers).status_code, 400)
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)

    def test_concurrent_new_imports_cannot_steal_last_recovery_slot(self):
        with self.app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import constants, tasks
            from fls_manager.routes.online_scripts import install_select

            self.seed(constants, count=199, state="failed")
            entered, release = threading.Event(), threading.Event()
            responses = []
            real = install_select.import_task_if_needed

            def block(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("import not released")
                return real(*args, **kwargs)

            headers = {"X-Token": TOKEN}
            with patch.object(install_select, "import_task_if_needed", side_effect=block) as importer, patch.object(tasks, "reload_scheduler") as reload:
                worker = threading.Thread(target=lambda: responses.append(app.test_client().post("/api/online-scripts/import-tasks/demo", json={"request_id": "e" * 32, "task_indexes": "1"}, headers=headers)))
                worker.start()
                try:
                    self.assertTrue(entered.wait(5))
                    rejected = app.test_client().post("/api/online-scripts/import-tasks/demo", json={"request_id": "f" * 32, "task_indexes": "1"}, headers=headers)
                    self.assertEqual(rejected.status_code, 409)
                    self.assertFalse(rejected.json["accepted"])
                    self.assertEqual(load_tasks(), [])
                finally:
                    release.set()
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(responses[0].status_code, 200)
                self.assertEqual(len(load_tasks()), 1)
                importer.assert_called_once()
                reload.assert_called_once()

    def test_archive_and_retry_serialize_without_restarting_archived_request(self):
        for archive_first in (False, True):
            with self.subTest(archive_first=archive_first), self.app() as (app, _base_dir):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts import constants, tasks
                from fls_manager.routes.online_scripts import install_select

                self.seed(constants, count=1, state="failed")
                request_id = "0" * 32
                entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
                results = {}
                target = "persist_online_task_import_request" if archive_first else "import_task_if_needed"
                real = getattr(install_select, target)

                def block(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test did not release first operation")
                    return real(*args, **kwargs)

                def submit(archive, second=False):
                    if second:
                        second_started.set()
                    url = "/api/online-scripts/import-tasks-archive/" + request_id if archive else "/api/online-scripts/import-tasks/demo"
                    data = {"acknowledge": "archive"} if archive else {"request_id": request_id, "task_indexes": "1"}
                    results[archive] = app.test_client().post(url, json=data, headers={"X-Token": TOKEN})

                with patch.object(install_select, target, side_effect=block), patch.object(tasks, "reload_scheduler") as reload:
                    first = threading.Thread(target=submit, args=(archive_first,))
                    second = threading.Thread(target=submit, args=(not archive_first, True))
                    first.start()
                    try:
                        self.assertTrue(entered.wait(5))
                        second.start()
                        self.assertTrue(second_started.wait(5))
                        if not archive_first:
                            second.join(timeout=2)
                            self.assertFalse(second.is_alive())
                            self.assertEqual(results[True].status_code, 409)
                    finally:
                        release.set()
                        first.join(timeout=5)
                        if second.ident is not None:
                            second.join(timeout=5)
                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                    self.assertEqual(results[archive_first].status_code, 200)
                    self.assertEqual(results[not archive_first].status_code, 409)
                    self.assertEqual(len(load_tasks()), 0 if archive_first else 1)
                    self.assertEqual(reload.call_count, 0 if archive_first else 1)
                self.assertEqual(constants.get_online_task_import_request(request_id)["archived"], archive_first)

    def test_invalid_archive_storage_ids_and_completed_receipts_do_not_change_files(self):
        for kind in ("corrupt", "symlink", "invalid_archive", "completed"):
            with self.subTest(kind=kind), self.app() as (app, base_dir):
                from fls_manager.online_scripts import constants
                from fls_manager.routes.online_scripts import install_select

                self.seed(constants, count=1, state="completed" if kind == "completed" else "unknown")
                path = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE
                if kind == "corrupt":
                    path.write_text("invalid JSON", encoding="utf-8")
                elif kind == "symlink":
                    target = base_dir / "external.json"
                    path.rename(target)
                    path.symlink_to(target)
                elif kind == "invalid_archive":
                    records = json.loads(path.read_text())
                    records["0" * 32]["archived"] = "yes"
                    path.write_text(json.dumps(records), encoding="utf-8")
                before = path.read_bytes()
                client, headers = app.test_client(), {"X-Token": TOKEN}
                with patch.object(install_select, "reload_scheduler") as reload, patch.object(install_select, "import_task_if_needed") as importer:
                    for prefix in ("/api", ""):
                        response = client.post(prefix + "/online-scripts/import-tasks-archive/" + "0" * 32,
                                               data={"acknowledge": "archive"}, headers=headers)
                        self.assertEqual(response.status_code, 409 if kind == "completed" else 500)
                        self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(client.post("/api/online-scripts/import-tasks-archive/invalid", json={"acknowledge": "archive"}, headers=headers).status_code, 400)
                    if kind == "completed":
                        self.assertEqual(client.post("/api/online-scripts/import-tasks-archive/" + "e" * 32, json={"acknowledge": "archive"}, headers=headers).status_code, 404)
                    importer.assert_not_called()
                    reload.assert_not_called()

    def test_scheduler_recovery_at_capacity_keeps_original_receipt_and_does_not_reimport(self):
        with self.app() as (app, _base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import install_select

            records = self.seed(constants, state="failed")
            request_id = "0" * 32
            records[request_id].update(state="scheduler_pending", updated_at="old")
            constants.mutate_json(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE, {}, lambda _: records, strict=True)
            client, headers = app.test_client(), {"X-Token": TOKEN}
            with patch.object(install_select, "reload_scheduler") as reload, patch.object(install_select, "import_task_if_needed") as importer:
                url = "/api/online-scripts/import-tasks-reload/" + request_id
                self.assertEqual(client.post(url, headers=headers).status_code, 200)
                self.assertTrue(client.post(url, headers=headers).json["replayed"])
                reload.assert_called_once()
                importer.assert_not_called()
                self.assertEqual(load_tasks(), [])
            self.assertEqual(constants.get_online_task_import_request(request_id)["state"], "completed")
            self.assertEqual(len(json.loads(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_text())), 200)


if __name__ == "__main__":
    unittest.main()
