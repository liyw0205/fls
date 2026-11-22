import json
import shutil
import subprocess
import threading
import time
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class TaskLinkFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            if self.current is not None:
                raise AssertionError("Nested form")
            self.current = {"attrs": attrs, "controls": []}
            self.forms.append(self.current)
        elif self.current is not None and tag in ("input", "select", "option", "button"):
            self.current["controls"].append((tag, attrs))

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class OnlineTaskLinkTests(unittest.TestCase):
    def item(self, script_id="demo", tasks=None):
        return {
            "id": script_id, "name": script_id, "type": "raw",
            "link": f"https://example.invalid/{script_id}.sh", "link_name": f"{script_id}.sh",
            "task_link": f"https://example.invalid/{script_id}-tasks.json",
            "task_cron": tasks or [],
        }

    def task(self, name):
        return {"name": name, "cron": "", "command": f"task {name}.sh"}

    def seed_task_link_history(self, source, item, *, state="completed"):
        records = {}
        for index in range(200):
            request_id = f"{index:032x}"
            record = self._unknown_record(source, request_id, item, state=state)
            record["updated_at"] = "2099-01-01 00:00:00"
            records[request_id] = source._task_link_request_snapshot(record)
        source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _: records, strict=True)
        return records

    def test_history_pruning_keeps_recovery_and_current_receipts_even_with_older_timestamps(self):
        for operation in ("persist", "claim"):
            with self.subTest(operation=operation), isolated_app() as (_app, _base_dir):
                from fls_manager.online_scripts import source

                item = self.item()
                source.save_online_script_cache([item])
                records = self.seed_task_link_history(source, item)
                oldest_id = "0" * 32
                records[oldest_id].update(state="unknown", updated_at="2000-01-01 00:00:00")
                source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _: records, strict=True)
                new_id = "f" * 32
                new_record = self._unknown_record(source, new_id, item, state="completed" if operation == "persist" else "prepared")
                try:
                    if operation == "persist":
                        source.persist_task_link_request(new_record)
                    else:
                        self.assertIsNone(source.claim_task_link_request(new_record))
                    actual = json.loads(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_text())
                    self.assertEqual(len(actual), 200)
                    self.assertIn(oldest_id, actual)
                    self.assertIn(new_id, actual)
                    self.assertEqual(actual[oldest_id]["state"], "unknown")
                finally:
                    source.ONLINE_TASK_LINK_ACTIVE.discard(new_id)

    def test_full_recovery_history_blocks_new_api_and_form_requests_without_fetch_or_write(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item = self.item()
            source.save_online_script_cache([item])
            self.seed_task_link_history(source, item, state="unknown")
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
            client = app.test_client()
            with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("unexpected")]) as fetch:
                response = client.post("/api/online-scripts/task-link/demo", json={"request_id": "f" * 32}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 409)
                self.assertTrue(response.json["history_full"])
                self.assertFalse(response.json["retry_allowed"])
                page = client.post("/online-scripts/task-link/demo", data={"request_id": "e" * 32}, headers={"X-Token": TOKEN})
                self.assertEqual(page.status_code, 409)
                self.assertIn('/online-scripts/task-link-history?recovery=1', page.get_data(as_text=True))
                fetch.assert_not_called()
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
            self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)
            self.assertNotIn("f" * 32, source.ONLINE_TASK_LINK_ACTIVE)

    def test_active_request_with_missing_receipt_blocks_reclaim_and_exposes_query_recovery(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "f" * 32
            source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _: {}, strict=True)
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            source.ONLINE_TASK_LINK_ACTIVE.add(request_id)
            try:
                with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("unexpected")]) as fetch:
                    client = app.test_client()
                    response = client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 503)
                    self.assertTrue(response.json["receipt_missing"])
                    self.assertFalse(response.json["retry_allowed"])
                    status = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                    self.assertEqual(status.status_code, 503)
                    self.assertTrue(status.json["active"])
                    page = client.get("/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                    self.assertEqual(page.status_code, 503)
                    self.assertIn("重新查询", page.get_data(as_text=True))
                    fetch.assert_not_called()
                self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                self.assertIn(request_id, source.ONLINE_TASK_LINK_ACTIVE)
            finally:
                source.ONLINE_TASK_LINK_ACTIVE.discard(request_id)

    def test_retention_protects_all_unresolved_states_and_active_completed_receipts(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            records = self.seed_task_link_history(source, item)
            protected = []
            for index, state in enumerate(("prepared", "running", "failed", "unknown", "completed")):
                request_id = f"{index:032x}"
                records[request_id].update(state=state, updated_at="old")
                protected.append(request_id)
            source.ONLINE_TASK_LINK_ACTIVE.add(protected[-1])
            source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _: records, strict=True)
            try:
                for index in range(10):
                    source.persist_task_link_request(self._unknown_record(source, f"{1000 + index:032x}", item, state="completed"))
                actual = json.loads(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_text())
                self.assertEqual(len(actual), 200)
                self.assertTrue(all(key in actual for key in protected))
            finally:
                source.ONLINE_TASK_LINK_ACTIVE.discard(protected[-1])

    def test_new_claim_reserves_room_for_active_missing_receipt_and_allows_original_recovery(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item, missing_id, new_id = self.item(), "a" * 32, "f" * 32
            source.save_online_script_cache([item])
            records = self.seed_task_link_history(source, item, state="unknown")
            records.pop(f"{199:032x}")
            source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _: records, strict=True)
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            source.ONLINE_TASK_LINK_ACTIVE.add(missing_id)
            try:
                with patch.object(task_link, "fetch_task_link_tasks") as fetch:
                    rejected = app.test_client().post("/api/online-scripts/task-link/demo", json={"request_id": new_id}, headers={"X-Token": TOKEN})
                    self.assertEqual(rejected.status_code, 409)
                    self.assertTrue(rejected.json["history_full"])
                    self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                    fetch.assert_not_called()
                source.persist_task_link_request(self._unknown_record(source, missing_id, item, state="completed"))
                self.assertEqual(len(json.loads(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_text())), 200)
                self.assertEqual(source.get_task_link_request(missing_id)["state"], "completed")
            finally:
                source.ONLINE_TASK_LINK_ACTIVE.discard(missing_id)

    def test_archive_frees_capacity_without_changing_unknown_result_or_tasks_and_blocks_replay(self):
        for state, entry in (("unknown", "api"), ("failed", "form")):
            with self.subTest(state=state, entry=entry), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                item = self.item()
                source.save_online_script_cache([item])
                records = self.seed_task_link_history(source, item, state=state)
                original_id, new_id = "0" * 32, "f" * 32
                records[original_id]["updated_at"] = "2000-01-01 00:00:00"
                source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _: records, strict=True)
                original = dict(records[original_id])
                cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
                client = app.test_client()
                headers = {"X-Token": TOKEN}
                with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("new")]) as fetch:
                    url = ("/api" if entry == "api" else "") + "/online-scripts/task-link-archive/" + original_id
                    archived = client.post(url, data={"acknowledge": "archive"}, headers=headers)
                    self.assertEqual(archived.status_code, 200 if entry == "api" else 302)
                    actual = source.get_task_link_request(original_id)
                    self.assertTrue(actual["archived"])
                    self.assertEqual(actual["state"], state)
                    for key in original:
                        if key not in ("archived", "archived_at"):
                            self.assertEqual(actual[key], original[key], key)
                    self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)
                    archived_bytes = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
                    repeated = client.post("/api/online-scripts/task-link-archive/" + original_id, json={"acknowledge": "archive"}, headers=headers)
                    self.assertTrue(repeated.json["replayed"])
                    self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), archived_bytes)
                    status = client.get("/api/online-scripts/task-link-status/" + original_id, headers=headers)
                    self.assertFalse(status.json["retry_allowed"])
                    page = client.get("/online-scripts/task-link-status/" + original_id, headers=headers).get_data(as_text=True)
                    self.assertIn("已归档", page)
                    self.assertNotIn('<form', page.split('<main', 1)[-1].split('</main>', 1)[0])
                    self.assertEqual(client.post("/api/online-scripts/task-link/demo", json={"request_id": original_id}, headers=headers).status_code, 409)
                    self.assertEqual(client.post("/api/online-scripts/task-link-confirm/" + original_id, headers=headers).status_code, 409)
                    fetch.assert_not_called()
                    self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), archived_bytes)
                    accepted = client.post("/api/online-scripts/task-link/demo", json={"request_id": new_id}, headers=headers)
                    self.assertEqual(accepted.status_code, 200)
                    fetch.assert_called_once()
                actual = json.loads(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_text())
                self.assertEqual(len(actual), 200)
                self.assertNotIn(original_id, actual)
                self.assertEqual(actual[new_id]["state"], "completed")

    def test_archive_invalid_choice_auth_csrf_and_ineligible_states_have_zero_side_effects(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            source.save_online_script_cache([self.item()])
            request_id = "a" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, self.item()))
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            api = "/api/online-scripts/task-link-archive/" + request_id
            form = "/online-scripts/task-link-archive/" + request_id
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
            with client.session_transaction() as session:
                session["csrf_token"] = "archive-csrf"
            with patch.object(task_link, "fetch_task_link_tasks") as fetch, patch("fls_manager.app.dispatch_frontend_open_event"):
                self.assertEqual(client.post(api, json={"acknowledge": "archive"}, headers={"X-CSRF-Token": "archive-csrf"}).status_code, 401)
                for choice in (None, "", "yes", True, ["archive"]):
                    self.assertEqual(client.post(api, json={"acknowledge": choice}, headers=headers).status_code, 400)
                self.assertEqual(client.post(form, data={"acknowledge": "wrong"}, headers=headers).status_code, 400)
                self.assertEqual(client.post("/api/online-scripts/task-link-archive/invalid", json={"acknowledge": "archive"}, headers=headers).status_code, 400)
                self.assertEqual(client.post("/api/online-scripts/task-link-archive/" + "b" * 32, json={"acknowledge": "archive"}, headers=headers).status_code, 404)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="archive-csrf")
                for csrf in ("", "wrong"):
                    self.assertEqual(client.post(api, json={"acknowledge": "archive"}, headers={"X-CSRF-Token": csrf}).status_code, 400)
                    self.assertEqual(client.post(form, data={"acknowledge": "archive", "csrf_token": csrf}).status_code, 400)
                self.assertEqual(client.post(api + "?token=blocked", json={"acknowledge": "archive"}, headers={"X-CSRF-Token": "archive-csrf"}).status_code, 403)
                source.ONLINE_TASK_LINK_ACTIVE.add(request_id)
                try:
                    self.assertEqual(client.post(api, json={"acknowledge": "archive"}, headers=headers).status_code, 409)
                finally:
                    source.ONLINE_TASK_LINK_ACTIVE.discard(request_id)
                self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)
                fetch.assert_not_called()
                completed_id = "c" * 32
                source.persist_task_link_request(self._unknown_record(source, completed_id, self.item(), state="completed"))
                before_completed = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
                rejected = client.post("/api/online-scripts/task-link-archive/" + completed_id, json={"acknowledge": "archive"}, headers=headers)
                self.assertEqual(rejected.status_code, 409)
                self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before_completed)
                page = client.get("/online-scripts/task-link-status/" + request_id).get_data(as_text=True)
                parser = TaskLinkFormParser()
                parser.feed(page)
                archive_form = next(value for value in parser.forms if value["attrs"]["action"] == form)
                csrf = next(attrs["value"] for tag, attrs in archive_form["controls"] if attrs.get("name") == "csrf_token")
                self.assertEqual(csrf, "archive-csrf")
                success = client.post(form, data={"csrf_token": csrf, "acknowledge": "archive"})
                self.assertEqual(success.status_code, 302)

    def test_archive_publication_failures_preserve_real_state_and_allow_idempotent_recovery(self):
        for after in (False, True):
            with self.subTest(after=after), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                item, request_id = self.item(), "a" * 32
                source.save_online_script_cache([item])
                source.persist_task_link_request(self._unknown_record(source, request_id, item))
                original_cache = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
                real_persist = task_link.persist_task_link_request

                def failing_persist(record):
                    if after:
                        real_persist(record)
                    raise OSError("archive acknowledgement unavailable")

                client, headers = app.test_client(), {"X-Token": TOKEN}
                url = "/api/online-scripts/task-link-archive/" + request_id
                with patch.object(task_link, "persist_task_link_request", side_effect=failing_persist), patch.object(task_link, "fetch_task_link_tasks") as fetch:
                    failed = client.post(url, json={"acknowledge": "archive"}, headers=headers)
                    self.assertEqual(failed.status_code, 503)
                    self.assertIn("archive acknowledgement unavailable", failed.json["msg"])
                    status = client.get("/api/online-scripts/task-link-status/" + request_id, headers=headers)
                    self.assertEqual(status.json["state"], "unknown")
                    self.assertEqual(status.json["archived"], after)
                    fetch.assert_not_called()
                recovered = client.post(url, json={"acknowledge": "archive"}, headers=headers)
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.json["replayed"], after)
                self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), original_cache)

    def test_archive_and_retry_serialize_without_restarting_archived_request(self):
        for archive_first in (True, False):
            with self.subTest(archive_first=archive_first), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                item, request_id = self.item(), "a" * 32
                source.save_online_script_cache([item])
                source.persist_task_link_request(self._unknown_record(source, request_id, item, state="failed"))
                entered, release = threading.Event(), threading.Event()
                real = task_link.persist_task_link_request if archive_first else task_link.claim_task_link_request

                def hold(record):
                    result = real(record)
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test did not release task-link operation")
                    return result

                def submit(archive):
                    url = "/api/online-scripts/task-link-archive/" + request_id if archive else "/api/online-scripts/task-link/demo"
                    data = {"acknowledge": "archive"} if archive else {"request_id": request_id}
                    return app.test_client().post(url, json=data, headers={"X-Token": TOKEN})

                target = "persist_task_link_request" if archive_first else "claim_task_link_request"
                with patch.object(task_link, target, side_effect=hold), patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("new")]) as fetch, ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(submit, archive_first)
                    try:
                        self.assertTrue(entered.wait(5))
                        second = pool.submit(submit, not archive_first)
                        if not archive_first:
                            self.assertEqual(second.result(timeout=5).status_code, 409)
                    finally:
                        release.set()
                    self.assertEqual(first.result(timeout=5).status_code, 200)
                    self.assertEqual(second.result(timeout=5).status_code, 409)
                    self.assertEqual(fetch.call_count, 0 if archive_first else 1)
                actual = source.get_task_link_request(request_id)
                self.assertEqual(actual["archived"], archive_first)
                self.assertNotIn(request_id, source.ONLINE_TASK_LINK_ACTIVE)

    def test_recovery_history_pagination_reaches_older_receipts_and_rejects_invalid_filters(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            source.save_online_script_cache([item])
            records = self.seed_task_link_history(source, item, state="unknown")
            records["0" * 32].update(archived=True, archived_at="old")
            source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _: records, strict=True)
            client, headers = app.test_client(), {"X-Token": TOKEN}
            found = []
            for offset in (0, 100):
                response = client.get(f"/api/online-scripts/task-link-history?recovery=1&limit=100&offset={offset}", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["total"], 199)
                self.assertEqual(response.json["has_more"], offset == 0)
                found.extend(value["request_id"] for value in response.json["items"])
            self.assertEqual(len(set(found)), 199)
            self.assertNotIn("0" * 32, found)
            page = client.get("/online-scripts/task-link-history?recovery=1&script_id=demo&limit=100", headers=headers).get_data(as_text=True)
            self.assertIn("offset=100", page)
            self.assertIn("script_id=demo", page)
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            for query in ("recovery=bad", "offset=-1", "offset=201", "offset=nan", "limit=101"):
                for prefix in ("", "/api"):
                    self.assertEqual(client.get(prefix + "/online-scripts/task-link-history?" + query, headers=headers).status_code, 400)
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)

    def test_missing_active_receipt_and_corrupt_archive_storage_do_not_write_or_fetch(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item, request_id = self.item(), "a" * 32
            source.save_online_script_cache([item])
            source.ONLINE_TASK_LINK_ACTIVE.add(request_id)
            client, headers = app.test_client(), {"X-Token": TOKEN}
            try:
                with self.assertRaises(source.TaskLinkReceiptMissing):
                    source.claim_task_link_request(self._unknown_record(source, request_id, item, state="prepared"))
                self.assertFalse(source.ONLINE_TASK_LINK_REQUESTS_FILE.exists())
                with patch.object(task_link, "fetch_task_link_tasks") as fetch:
                    for suffix in ("archive", "confirm"):
                        failed = client.post(f"/api/online-scripts/task-link-{suffix}/{request_id}", json={"acknowledge": "archive"}, headers=headers)
                        self.assertEqual(failed.status_code, 503)
                    fetch.assert_not_called()
            finally:
                source.ONLINE_TASK_LINK_ACTIVE.discard(request_id)
            for kind in ("corrupt", "symlink", "invalid_archive"):
                with self.subTest(kind=kind):
                    receipt = source.ONLINE_TASK_LINK_REQUESTS_FILE
                    receipt.unlink(missing_ok=True)
                    if kind == "symlink":
                        target = base_dir / "original-receipt.json"
                        target.write_text("{}", encoding="utf-8")
                        receipt.symlink_to(target)
                    elif kind == "invalid_archive":
                        record = self._unknown_record(source, request_id, item)
                        record["archived"] = "yes"
                        receipt.write_text(json.dumps({request_id: record}), encoding="utf-8")
                    else:
                        receipt.write_text("broken JSON", encoding="utf-8")
                    before = receipt.read_bytes()
                    for prefix in ("", "/api"):
                        failed = client.post(prefix + "/online-scripts/task-link-archive/" + request_id, data={"acknowledge": "archive"}, headers=headers)
                        self.assertEqual(failed.status_code, 500)
                    self.assertEqual(receipt.read_bytes(), before)

    def test_upstream_errors_and_empty_tasks_preserve_cache_and_offer_retry(self):
        for ajax in (False, True):
            for status, text in ((500, "upstream offline"), (200, "not JSON"), (200, "[]")):
                with self.subTest(ajax=ajax, status=status, text=text), isolated_app() as (app, base_dir):
                    from fls_manager.online_scripts import source

                    source.save_online_script_cache([self.item(tasks=[self.task("existing")])])
                    cache = base_dir / "data" / "online_scripts_cache.json"
                    original = cache.read_bytes()
                    upstream = Mock(status_code=status, text=text, headers={"Content-Type": "application/json"})
                    headers = {"X-Token": TOKEN}
                    if ajax:
                        headers["X-Requested-With"] = "FLS-Ajax"
                    with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=upstream))):
                        response = app.test_client().post("/online-scripts/task-link/demo", headers=headers)
                    self.assertEqual(response.status_code, 422 if text == "[]" else 502)
                    self.assertNotIn("Location", response.headers)
                    self.assertIn('action="/online-scripts/task-link/demo"', response.get_data(as_text=True))
                    self.assertIn('name="csrf_token"', response.get_data(as_text=True))
                    self.assertEqual(cache.read_bytes(), original)
                    upstream.close.assert_called_once()

    def test_storage_failure_keeps_cache_and_successful_retry_is_idempotent(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item(tasks=[self.task("existing")])])
            cache = base_dir / "data" / "online_scripts_cache.json"
            original = cache.read_bytes()
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", return_value=[self.task("new")]), patch.object(
                source.os, "replace", side_effect=PermissionError("cache read-only"),
            ):
                failed = client.post("/online-scripts/task-link/demo", headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            self.assertIn("cache read-only", failed.get_data(as_text=True))
            self.assertEqual(cache.read_bytes(), original)
            self.assertEqual(list(cache.parent.glob(".online_scripts_cache.json.*.tmp")), [])
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", return_value=[self.task("new")] * 2):
                first = client.post("/online-scripts/task-link/demo", headers={"X-Token": TOKEN})
                with patch.object(source, "save_online_script_cache") as save:
                    repeated = client.post("/online-scripts/task-link/demo", headers={"X-Token": TOKEN})
                save.assert_not_called()
            self.assertEqual(first.status_code, 302)
            self.assertEqual(repeated.status_code, 302)
            self.assertEqual([task["name"] for task in source.load_online_script_cache()[0]["task_cron"]], ["existing", "new"])

    def test_stable_request_replay_does_not_fetch_or_publish_twice_and_is_queryable(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            request_id = "a" * 32
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", return_value=[self.task("new")]) as fetch:
                first = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
                repeated = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(repeated.status_code, 200)
            self.assertTrue(repeated.json["replayed"])
            fetch.assert_called_once()
            status = client.get(
                "/api/online-scripts/task-link-status/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["state"], "completed")
            self.assertEqual(status.json["added"], 1)
            html_status = client.get("/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(html_status.status_code, 200)
            self.assertIn("同步已完成", html_status.get_data(as_text=True))

    def test_orphaned_prepared_request_recovers_through_status_history_and_same_id_retry(self):
        for entry in ("status", "history", "page"):
            with self.subTest(entry=entry), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                item = self.item()
                source.save_online_script_cache([item])
                request_id = "1" * 32
                source.persist_task_link_request(self._unknown_record(source, request_id, item, state="prepared"))
                cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
                client = app.test_client()
                entry_url = {
                    "status": "/api/online-scripts/task-link-status/" + request_id,
                    "history": "/api/online-scripts/task-link-history",
                    "page": "/online-scripts/task-link-status/" + request_id,
                }[entry]
                with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("recovered")]) as fetch:
                    response = client.get(entry_url, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 200)
                    recovered = source.get_task_link_request(request_id)
                    self.assertEqual(recovered["state"], "failed")
                    self.assertIn("未发起外部请求", recovered["error"])
                    self.assertIn("interrupted", recovered["error"])
                    self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)
                    fetch.assert_not_called()
                    page = client.get("/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                    self.assertIn('name="request_id" value="' + request_id + '"', page.get_data(as_text=True))
                    self.assertIn("继续原同步请求", page.get_data(as_text=True))
                    first = client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})
                    repeated = client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})
                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(repeated.status_code, 200)
                    self.assertTrue(repeated.json["replayed"])
                    fetch.assert_called_once()

    def test_running_receipt_startup_failure_is_recoverable_before_and_after_publication(self):
        for after_write in (False, True):
            with self.subTest(after_write=after_write), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                source.save_online_script_cache([self.item()])
                cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
                request_id = "2" * 32

                def fail_running(record):
                    if record["state"] == "running":
                        if after_write:
                            source.persist_task_link_request(record)
                        raise OSError("running receipt acknowledgement unavailable")
                    return source.persist_task_link_request(record)

                client = app.test_client()
                with patch.object(task_link, "persist_task_link_request", side_effect=fail_running), patch.object(task_link, "fetch_task_link_tasks") as fetch:
                    failed = client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})
                self.assertEqual(failed.status_code, 500)
                self.assertIn("running receipt acknowledgement unavailable", failed.json["msg"])
                self.assertTrue(failed.json["retry_allowed"])
                fetch.assert_not_called()
                status = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                self.assertEqual(status.json["state"], "failed")
                self.assertIn("running receipt acknowledgement unavailable", status.json["error"])
                self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)
                with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("recovered")]) as fetch:
                    retried = client.post("/online-scripts/task-link/demo", data={"request_id": request_id}, headers={"X-Token": TOKEN})
                self.assertEqual(retried.status_code, 302)
                fetch.assert_called_once()
                self.assertEqual(source.get_task_link_request(request_id)["state"], "completed")

    def test_live_prepared_request_survives_status_history_and_concurrent_submission(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            source.save_online_script_cache([self.item()])
            request_id = "3" * 32
            prepared, release = threading.Event(), threading.Event()

            def pause_before_running(record):
                if record["state"] == "running":
                    prepared.set()
                    if not release.wait(5):
                        raise RuntimeError("live request was not released")
                return source.persist_task_link_request(record)

            def submit():
                return app.test_client().post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})

            with patch.object(task_link, "persist_task_link_request", side_effect=pause_before_running), patch.object(
                task_link, "fetch_task_link_tasks", return_value=[self.task("once")],
            ) as fetch, ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(submit)
                try:
                    self.assertTrue(prepared.wait(3))
                    before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
                    client = app.test_client()
                    for url in ("/api/online-scripts/task-link-status/" + request_id, "/api/online-scripts/task-link-history"):
                        response = client.get(url, headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 200)
                        record = response.json if "state" in response.json else response.json["items"][0]
                        self.assertEqual(record["state"], "prepared")
                        self.assertTrue(record["active"])
                    self.assertEqual(submit().status_code, 409)
                    self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                    fetch.assert_not_called()
                finally:
                    release.set()
                self.assertEqual(worker.result(timeout=5).status_code, 200)
                fetch.assert_called_once()
            self.assertFalse(source.get_task_link_request(request_id)["active"])

    def test_failed_receipt_published_then_error_preserves_upstream_status_and_same_id_retry(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            source.save_online_script_cache([self.item()])
            request_id = "7" * 32
            real_persist = source.persist_task_link_request

            def publish_failure_then_raise(record):
                real_persist(record)
                if record["state"] == "failed":
                    raise OSError("failed receipt acknowledgement lost after replace")

            client = app.test_client()
            with patch.object(task_link, "persist_task_link_request", side_effect=publish_failure_then_raise), patch.object(
                task_link, "fetch_task_link_tasks", side_effect=[
                    RuntimeError("upstream timeout"), [self.task("recovered")],
                ],
            ) as fetch:
                failed = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 502)
                self.assertTrue(failed.json["resolved"])
                self.assertTrue(failed.json["retry_allowed"])
                self.assertIn("upstream timeout", failed.json["msg"])
                status = client.get(
                    "/api/online-scripts/task-link-status/" + request_id,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(status.json["state"], "failed")
                self.assertTrue(status.json["retry_allowed"])
                retried = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(retried.status_code, 200)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(source.get_task_link_request(request_id)["state"], "completed")
            self.assertEqual([task["name"] for task in source.load_online_script_cache()[0]["task_cron"]], ["recovered"])

    def test_failed_receipt_write_rejection_requires_unknown_confirmation_before_same_id_retry(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            source.save_online_script_cache([self.item()])
            request_id = "8" * 32
            real_persist = source.persist_task_link_request
            failure_pending = {"value": True}

            def reject_failure_receipt(record):
                if record["state"] == "failed" and failure_pending["value"]:
                    failure_pending["value"] = False
                    raise OSError("failed receipt storage temporarily read-only")
                return real_persist(record)

            client = app.test_client()
            with patch.object(task_link, "persist_task_link_request", side_effect=reject_failure_receipt), patch.object(
                task_link, "fetch_task_link_tasks", side_effect=[
                    RuntimeError("upstream timeout"), [self.task("recovered")],
                ],
            ) as fetch:
                failed = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 503)
                self.assertFalse(failed.json["retry_allowed"])
                self.assertTrue(failed.json["receipt_confirmation_required"])
                self.assertEqual(failed.json["status_url"], "/online-scripts/task-link-status/" + request_id)
                status = client.get(
                    "/api/online-scripts/task-link-status/" + request_id,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(status.json["state"], "unknown")
                self.assertFalse(status.json["retry_allowed"])
                confirmed = client.post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(confirmed.status_code, 200)
                self.assertEqual(confirmed.json["confirmation"], "not_merged")
                retry = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(source.get_task_link_request(request_id)["state"], "completed")
            self.assertEqual([task["name"] for task in source.load_online_script_cache()[0]["task_cron"]], ["recovered"])

    def test_failed_receipt_still_owned_by_finishing_request_cannot_be_reclaimed(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            source.save_online_script_cache([self.item()])
            request_id = "4" * 32
            published, release = threading.Event(), threading.Event()

            def pause_after_failure(record):
                source.persist_task_link_request(record)
                if record["state"] == "failed":
                    published.set()
                    if not release.wait(5):
                        raise RuntimeError("finishing request was not released")

            def submit():
                return app.test_client().post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})

            with patch.object(task_link, "persist_task_link_request", side_effect=pause_after_failure), patch.object(
                task_link, "fetch_task_link_tasks", side_effect=RuntimeError("source offline"),
            ) as fetch, ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(submit)
                try:
                    self.assertTrue(published.wait(3))
                    client = app.test_client()
                    status = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                    self.assertEqual(status.json["state"], "failed")
                    self.assertTrue(status.json["active"])
                    self.assertFalse(status.json["retry_allowed"])
                    page = client.get("/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                    self.assertNotIn('action="/online-scripts/task-link/demo"', page.get_data(as_text=True))
                    repeated = submit()
                    self.assertEqual(repeated.status_code, 409)
                    self.assertFalse(repeated.json["retry_allowed"])
                    fetch.assert_called_once()
                finally:
                    release.set()
                self.assertEqual(worker.result(timeout=5).status_code, 502)
            self.assertFalse(source.get_task_link_request(request_id)["active"])
            with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("recovered")]) as fetch:
                self.assertEqual(submit().status_code, 200)
                fetch.assert_called_once()

    def test_unpersisted_startup_failure_requires_original_state_query_before_retry(self):
        for after_write in (False, True):
            with self.subTest(after_write=after_write), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                source.save_online_script_cache([self.item()])
                request_id = "5" * 32

                def fail_running_and_failure_receipts(record):
                    if after_write and record["state"] == "running":
                        source.persist_task_link_request(record)
                    raise OSError("startup receipts read-only")

                client = app.test_client()
                with patch.object(task_link, "persist_task_link_request", side_effect=fail_running_and_failure_receipts), patch.object(task_link, "fetch_task_link_tasks") as fetch:
                    failed = client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})
                self.assertEqual(failed.status_code, 503)
                self.assertFalse(failed.json["retry_allowed"])
                self.assertFalse(failed.json["resolved"])
                fetch.assert_not_called()
                before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
                with patch.object(source, "mutate_json", side_effect=OSError("recovery storage read-only")):
                    blocked = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                    self.assertEqual(blocked.status_code, 500)
                    self.assertIn("recovery storage read-only", blocked.json["msg"])
                self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                status = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                self.assertEqual(status.json["state"], "unknown" if after_write else "failed")
                self.assertFalse(status.json["active"])
                if after_write:
                    confirmation = client.post("/api/online-scripts/task-link-confirm/" + request_id, headers={"X-Token": TOKEN})
                    self.assertEqual(confirmation.status_code, 200)
                    self.assertEqual(confirmation.json["confirmation"], "not_merged")
                with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("recovered")]) as fetch:
                    retried = client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})
                self.assertEqual(retried.status_code, 200)
                fetch.assert_called_once()

    def test_prepared_recovery_keeps_auth_csrf_binding_and_same_request_form_boundaries(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "6" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item, state="prepared"))
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="startup-csrf")
            with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("once")]) as fetch:
                self.assertEqual(client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}).status_code, 400)
                self.assertEqual(client.post("/online-scripts/task-link/demo", data={"request_id": request_id, "csrf_token": "wrong"}).status_code, 400)
                for script_id, proxy_id in (("other", ""), ("demo", "other")):
                    wrong = client.post("/api/online-scripts/task-link/" + script_id,
                                        json={"request_id": request_id, "proxy_id": proxy_id},
                                        headers={"X-CSRF-Token": "startup-csrf"})
                    self.assertEqual(wrong.status_code, 409)
                invalid = client.post("/api/online-scripts/task-link/demo", json={"request_id": "invalid"}, headers={"X-CSRF-Token": "startup-csrf"})
                self.assertEqual(invalid.status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-CSRF-Token": "startup-csrf"}).status_code, 401)
                self.assertEqual(client.get("/api/online-scripts/task-link-status/" + request_id).status_code, 401)
                self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                fetch.assert_not_called()
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="startup-csrf")
                form = {"request_id": request_id, "csrf_token": "startup-csrf"}
                retried = client.post("/online-scripts/task-link/demo", data=form)
                self.assertEqual(retried.status_code, 302)
                replayed = client.post("/online-scripts/task-link/demo", data=form)
                self.assertEqual(replayed.status_code, 200)
                self.assertIn('href="/online-scripts/task-link-status/' + request_id + '"', replayed.get_data(as_text=True))
                fetch.assert_called_once()

    def test_orphaned_prepared_recovery_rejects_symlink_without_replacing_receipt_or_target(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "7" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item, state="prepared"))
            target = base_dir / "original-receipt.json"
            source.ONLINE_TASK_LINK_REQUESTS_FILE.rename(target)
            before = target.read_bytes()
            source.ONLINE_TASK_LINK_REQUESTS_FILE.symlink_to(target)
            with patch.object(task_link, "fetch_task_link_tasks") as fetch:
                client = app.test_client()
                for url in ("/api/online-scripts/task-link-status/" + request_id,
                            "/online-scripts/task-link-status/" + request_id,
                            "/api/online-scripts/task-link-history"):
                    self.assertEqual(client.get(url, headers={"X-Token": TOKEN}).status_code, 500)
                failed = client.post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})
                self.assertEqual(failed.status_code, 500)
                fetch.assert_not_called()
            self.assertTrue(source.ONLINE_TASK_LINK_REQUESTS_FILE.is_symlink())
            self.assertEqual(target.read_bytes(), before)

    def test_receipt_replay_survives_later_cache_and_proxy_changes(self):
        for state in ("completed", "unknown"):
            for change in ("corrupt-cache", "deleted-script", "missing-link", "changed-source", "deleted-proxy", "proxy-error"):
                with self.subTest(state=state, change=change), isolated_app() as (app, _base_dir):
                    from fls_manager.models import save_proxies
                    from fls_manager.online_scripts import source
                    from fls_manager.routes.online_scripts import task_link

                    save_proxies([{"id": "chosen", "name": "Chosen", "type": "http",
                                   "host": "127.0.0.1", "port": "8080", "enabled": True}])
                    source.save_online_script_cache([self.item()])
                    client = app.test_client()
                    request_id = "b" * 32
                    data = {"request_id": request_id, "proxy_id": "chosen"}
                    headers = {"X-Token": TOKEN}
                    with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("original")]):
                        self.assertEqual(client.post("/api/online-scripts/task-link/demo", json=data, headers=headers).status_code, 200)
                    record = source.get_task_link_request(request_id)
                    record.update(state=state, error="original receipt diagnosis")
                    source.persist_task_link_request(record)
                    if change == "corrupt-cache":
                        source.ONLINE_SCRIPT_CACHE_FILE.write_text("{broken cache", encoding="utf-8")
                    elif change == "deleted-script":
                        source.save_online_script_cache([])
                    elif change == "missing-link":
                        item = self.item()
                        item.pop("task_link")
                        source.save_online_script_cache([item])
                    elif change == "changed-source":
                        source.save_online_script_cache([dict(self.item(), task_link="https://example.invalid/later.json")])
                    elif change == "deleted-proxy":
                        save_proxies([])
                    cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
                    receipt_before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
                    with patch.object(task_link, "get_online_script", wraps=task_link.get_online_script) as cache_read, patch.object(
                        task_link, "validate_proxy_id", wraps=task_link.validate_proxy_id,
                        side_effect=RuntimeError("proxy config unavailable") if change == "proxy-error" else None,
                    ) as proxy_read, patch.object(task_link, "fetch_task_link_tasks") as fetch, patch.object(
                        task_link, "claim_task_link_request",
                    ) as claim:
                        result = client.post("/api/online-scripts/task-link/demo", json=data, headers=headers)
                        page = client.post("/online-scripts/task-link/demo", data=data, headers=headers)
                        self.assertEqual(result.status_code, 200 if state == "completed" else 409)
                        self.assertEqual(page.status_code, result.status_code)
                        self.assertEqual(result.json["state"], state)
                        self.assertTrue(result.json["replayed"])
                        self.assertFalse(result.json["retry_allowed"])
                        self.assertEqual(result.json["diagnostic"], "original receipt diagnosis")
                        self.assertEqual(result.json["state_url"], "/api/online-scripts/task-link-status/" + request_id)
                        self.assertEqual(result.json["status_url"], "/online-scripts/task-link-status/" + request_id)
                        self.assertIn('href="/online-scripts/task-link-status/' + request_id + '"', page.get_data(as_text=True))
                        self.assertIn("original receipt diagnosis", page.get_data(as_text=True))
                        cache_read.assert_not_called()
                        proxy_read.assert_not_called()
                        fetch.assert_not_called()
                        claim.assert_not_called()
                    self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)
                    self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), receipt_before)

    def test_receipt_replay_preserves_binding_auth_csrf_and_invalid_identity_boundaries(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            source.persist_task_link_request({
                "request_id": "a" * 32, "script_id": "demo", "proxy_id": "original",
                "state": "completed", "added": 1, "total": 1,
            })
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="replay-csrf")
            data = {"request_id": "a" * 32, "proxy_id": "original", "csrf_token": "replay-csrf"}
            with patch.object(task_link, "get_online_script") as cache, patch.object(task_link, "fetch_task_link_tasks") as fetch, patch.object(
                task_link, "claim_task_link_request",
            ) as claim:
                original = client.post("/online-scripts/task-link/demo", data=data)
                self.assertEqual(original.status_code, 200)
                self.assertIn('href="/online-scripts/task-link-status/' + "a" * 32 + '"', original.get_data(as_text=True))
                for script_id, proxy_id in (("other-script", "original"), ("demo", "other-proxy")):
                    denied = client.post("/api/online-scripts/task-link/" + script_id, json=dict(data, proxy_id=proxy_id), headers={"X-CSRF-Token": "replay-csrf"})
                    self.assertEqual(denied.status_code, 409)
                    self.assertFalse(denied.json["retry_allowed"])
                for csrf in ("", "wrong"):
                    self.assertEqual(client.post("/api/online-scripts/task-link/demo", json=data, headers={"X-CSRF-Token": csrf}).status_code, 400)
                self.assertEqual(client.post("/api/online-scripts/task-link/demo", json=dict(data, request_id="invalid-id"), headers={"X-CSRF-Token": "replay-csrf"}).status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post("/api/online-scripts/task-link/demo", json=data, headers={"X-CSRF-Token": "replay-csrf"})
                self.assertEqual(expired.status_code, 401)
                cache.assert_not_called()
                fetch.assert_not_called()
                claim.assert_not_called()
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)

    def test_wrong_binding_does_not_reconcile_or_change_orphaned_running_receipt(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            request_id = "a" * 32
            source.persist_task_link_request({
                "request_id": request_id, "script_id": "demo", "proxy_id": "original", "state": "running",
            })
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            with patch.object(task_link, "get_online_script") as cache, patch.object(task_link, "claim_task_link_request") as claim:
                for script_id, proxy_id in (("other-script", "original"), ("demo", "other-proxy")):
                    response = app.test_client().post("/api/online-scripts/task-link/" + script_id,
                                                     json={"request_id": request_id, "proxy_id": proxy_id}, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                cache.assert_not_called()
                claim.assert_not_called()

    def test_failed_receipt_retry_still_validates_current_source_proxy_and_cache(self):
        for change, status in (("source", 409), ("proxy", 400), ("cache", 500), ("none", 200)):
            with self.subTest(change=change), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                item = self.item()
                source.save_online_script_cache([item])
                item = source.load_online_script_cache()[0]
                source.persist_task_link_request({
                    "request_id": "a" * 32, "script_id": "demo", "proxy_id": "",
                    "state": "failed", "expected_version": source.online_task_link_version(item),
                })
                if change == "source":
                    source.save_online_script_cache([dict(item, task_link="https://example.invalid/later.json")])
                elif change == "cache":
                    source.ONLINE_SCRIPT_CACHE_FILE.write_text("{corrupt", encoding="utf-8")
                data = {"request_id": "a" * 32, "proxy_id": "missing" if change == "proxy" else ""}
                # The original proxy binding must match before validating its current availability.
                if change == "proxy":
                    source.persist_task_link_request({
                        "request_id": "a" * 32, "script_id": "demo", "proxy_id": "missing",
                        "state": "failed", "expected_version": source.online_task_link_version(item),
                    })
                before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
                cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
                with patch.object(task_link, "fetch_task_link_tasks", return_value=[self.task("once")]) as fetch:
                    response = app.test_client().post("/api/online-scripts/task-link/demo", json=data, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, status)
                self.assertEqual(fetch.call_count, 1 if change == "none" else 0)
                if change != "none":
                    self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                    self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)
                else:
                    self.assertEqual(source.get_task_link_request("a" * 32)["state"], "completed")
                    self.assertEqual(len(source.load_online_script_cache()[0]["task_cron"]), 1)

    def test_active_receipt_replay_does_not_consult_later_cache_or_claim_again(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import ONLINE_TASK_LINK_ACTIVE
            from fls_manager.routes.online_scripts import task_link

            request_id = "c" * 32
            source.persist_task_link_request({"request_id": request_id, "script_id": "demo", "proxy_id": "", "state": "running"})
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            ONLINE_TASK_LINK_ACTIVE.add(request_id)
            try:
                with patch.object(task_link, "get_online_script") as cache, patch.object(task_link, "claim_task_link_request") as claim:
                    result = app.test_client().post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})
                    self.assertEqual(result.status_code, 409)
                    self.assertEqual(result.json["state"], "running")
                    self.assertTrue(result.json["replayed"])
                    cache.assert_not_called()
                    claim.assert_not_called()
            finally:
                ONLINE_TASK_LINK_ACTIVE.discard(request_id)
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)

    def test_simultaneous_initial_receipt_misses_still_claim_and_fetch_only_once(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            source.save_online_script_cache([self.item()])
            barrier = threading.Barrier(2)
            started, release = threading.Event(), threading.Event()
            request_id = "e" * 32

            def miss_together(identity, **binding):
                previous = source.get_task_link_request(identity, **binding)
                barrier.wait(3)
                return previous

            def fetch_once(*_args, **_kwargs):
                started.set()
                self.assertTrue(release.wait(5))
                return [self.task("only-once")]

            def post():
                return app.test_client().post("/api/online-scripts/task-link/demo", json={"request_id": request_id}, headers={"X-Token": TOKEN})

            with patch.object(task_link, "get_task_link_request", side_effect=miss_together), patch.object(
                task_link, "fetch_task_link_tasks", side_effect=fetch_once,
            ) as fetch, ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(post) for _ in range(2)]
                try:
                    self.assertTrue(started.wait(3))
                    done, _pending = wait(futures, timeout=3, return_when=FIRST_COMPLETED)
                    self.assertEqual(len(done), 1)
                    observed = next(iter(done)).result()
                    self.assertEqual(observed.status_code, 409)
                    self.assertTrue(observed.json["replayed"])
                finally:
                    release.set()
                self.assertEqual(sorted(future.result(timeout=5).status_code for future in futures), [200, 409])
                fetch.assert_called_once()
            self.assertEqual(source.get_task_link_request(request_id)["state"], "completed")
            self.assertEqual(len(source.load_online_script_cache()[0]["task_cron"]), 1)

    def test_unknown_publish_result_is_query_only_and_does_not_refetch(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            request_id = "c" * 32
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", return_value=[self.task("new")]) as fetch, patch(
                "fls_manager.routes.online_scripts.task_link.merge_task_link_tasks",
                side_effect=RuntimeError("publish acknowledgement unavailable"),
            ):
                first = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
                repeated = client.post(
                    "/online-scripts/task-link/demo",
                    data={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 503)
            self.assertEqual(repeated.status_code, 409)
            self.assertFalse(repeated.json if repeated.is_json else False)
            self.assertIn("查询原同步请求", repeated.get_data(as_text=True))
            fetch.assert_called_once()
            status = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(status.json["state"], "unknown")
            page = client.get("/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn("发布结果未知，请勿直接重发", html)
            self.assertIn("action=\"/online-scripts/task-link-confirm/" + request_id + "\"", html)
            self.assertNotIn("继续原同步请求", html)

    def test_process_exit_after_task_cache_publish_is_confirmed_without_refetch(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            request_id = "d" * 32
            real_merge = source.merge_task_link_tasks
            client = app.test_client()

            def merge_then_exit(item, linked_tasks, **kwargs):
                real_merge(item, linked_tasks, **kwargs)
                raise SystemExit("simulated process exit after cache publication")

            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", return_value=[
                self.task("new"),
            ]) as fetch, patch(
                "fls_manager.routes.online_scripts.task_link.merge_task_link_tasks",
                side_effect=merge_then_exit,
            ):
                with self.assertRaisesRegex(SystemExit, "simulated process exit"):
                    client.post(
                        "/api/online-scripts/task-link/demo",
                        json={"request_id": request_id},
                        headers={"X-Token": TOKEN},
                    )

                status = client.get(
                    "/api/online-scripts/task-link-status/" + request_id,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json["state"], "unknown")
                confirmed = client.post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json["confirmation"], "merged")
            self.assertEqual(confirmed.json["state"], "completed")
            fetch.assert_called_once()
            self.assertEqual([task["name"] for task in source.load_online_script_cache()[0]["task_cron"]], ["new"])

    def test_publish_intent_distinguishes_prepublication_failure_and_allows_same_id_retry(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            request_id = "e" * 32
            client = app.test_client()
            real_save = source.save_online_script_cache
            fetch_count = 0
            fail_publish = True

            def fetch(*_args, **_kwargs):
                nonlocal fetch_count
                fetch_count += 1
                if fetch_count == 1:
                    real_save([self.item(tasks=[self.task("manual")])])
                return [self.task("remote")]

            def save_once(items):
                nonlocal fail_publish
                if fail_publish:
                    fail_publish = False
                    raise OSError("cache replacement rejected before publication")
                return real_save(items)

            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", side_effect=fetch) as fetch_mock, patch(
                "fls_manager.online_scripts.source.save_online_script_cache", side_effect=save_once,
            ):
                first = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 503)
                unknown = client.get(
                    "/api/online-scripts/task-link-status/" + request_id,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(unknown.json["state"], "unknown")
                self.assertTrue(unknown.json["publication_base_fingerprint"])
                self.assertTrue(unknown.json["publication_target_fingerprint"])
                self.assertFalse(unknown.json["published_tasks_fingerprint"])
                confirmed = client.post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(confirmed.status_code, 200)
                self.assertEqual(confirmed.json["confirmation"], "not_merged")
                self.assertTrue(confirmed.json["retry_allowed"])
                retried = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(retried.status_code, 200)
            self.assertEqual(retried.json["added"], 1)
            self.assertEqual(fetch_mock.call_count, 2)
            self.assertEqual(fetch_count, 2)
            self.assertEqual(
                [task["name"] for task in source.load_online_script_cache()[0]["task_cron"]],
                ["manual", "remote"],
            )

    def _unknown_record(self, source, request_id, item, *, published="", state="unknown"):
        normalized = source.normalize_online_scripts([item])[0]
        return {
            "request_id": request_id,
            "script_id": normalized["id"],
            "proxy_id": "",
            "expected_version": source.online_task_link_version(normalized),
            "state": state,
            "added": 1 if published else 0,
            "total": len(normalized.get("task_cron") or []),
            "error": "interrupted",
            "updated_at": source.now_str(),
            "expected_tasks_fingerprint": source.online_script_task_fingerprint({**normalized, "task_cron": []}),
            "published_tasks_fingerprint": published,
            "confirmation": "",
            "confirmation_fingerprint": "",
        }

    def test_unknown_result_confirmation_marks_merged_without_fetch_or_append(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item(tasks=[self.task("existing"), self.task("new")])
            source.save_online_script_cache([item])
            request_id = "e" * 32
            record = self._unknown_record(
                source, request_id, item,
                published=source.online_script_task_fingerprint(item),
            )
            source.persist_task_link_request(record)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks") as fetch, patch(
                "fls_manager.routes.online_scripts.task_link.merge_task_link_tasks",
            ) as merge:
                response = client.post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )
                repeated = client.post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["confirmation"], "merged")
            self.assertEqual(repeated.status_code, 200)
            self.assertTrue(repeated.json["replayed"])
            fetch.assert_not_called()
            merge.assert_not_called()
            self.assertEqual(source.load_online_script_cache()[0]["task_cron"], item["task_cron"])

    def test_unknown_result_confirmation_marks_not_merged_and_allows_same_request_retry(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "f" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item))
            client = app.test_client()
            response = client.post(
                "/api/online-scripts/task-link-confirm/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["confirmation"], "not_merged")
            self.assertTrue(response.json["retry_allowed"])
            receipt_before_replay = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            repeated = client.post(
                "/api/online-scripts/task-link-confirm/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(repeated.status_code, 200)
            self.assertTrue(repeated.json["replayed"])
            self.assertTrue(repeated.json["retry_allowed"])
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), receipt_before_replay)
            with patch(
                "fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks",
                return_value=[self.task("recovered")],
            ) as fetch:
                retried = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(retried.status_code, 200)
            fetch.assert_called_once()
            final = client.get(
                "/api/online-scripts/task-link-status/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(final.json["state"], "completed")

    def test_stale_not_merged_confirmation_replay_is_conflict_without_receipt_write(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "9" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item))
            client = app.test_client()
            first = client.post("/api/online-scripts/task-link-confirm/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json["confirmation"], "not_merged")
            changed = self.item(tasks=[self.task("manual")])
            source.save_online_script_cache([changed])
            receipt_before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
            repeated = client.post("/api/online-scripts/task-link-confirm/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(repeated.status_code, 409)
            self.assertFalse(repeated.json.get("retry_allowed", False))
            self.assertTrue(repeated.json["confirmation_stale"])
            self.assertIn("缓存", repeated.json["msg"])
            status = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(status.status_code, 200)
            self.assertTrue(status.json["confirmation_stale"])
            self.assertFalse(status.json["retry_allowed"])
            self.assertIn("缓存变化", status.json["confirmation_error"])
            status_page = client.get("/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(status_page.status_code, 200)
            self.assertIn("原未合并确认已失效", status_page.get_data(as_text=True))
            self.assertIn('href="/online-scripts">核对当前任务', status_page.get_data(as_text=True))
            self.assertNotIn('action="/online-scripts/task-link/demo"', status_page.get_data(as_text=True))
            self.assertNotIn('action="/online-scripts/task-link-confirm/' + request_id + '"', status_page.get_data(as_text=True))
            repeated_form = client.post(
                "/online-scripts/task-link-confirm/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(repeated_form.status_code, 409)
            self.assertIn('href="/online-scripts/task-link-status/' + request_id + '"', repeated_form.get_data(as_text=True))
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), receipt_before)
            self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)

    def test_not_merged_confirmation_status_blocks_retry_when_cache_cannot_be_checked(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "7" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item))
            client = app.test_client()
            confirmed = client.post(
                "/api/online-scripts/task-link-confirm/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(confirmed.status_code, 200)
            source.ONLINE_SCRIPT_CACHE_FILE.write_text("{broken cache", encoding="utf-8")
            receipt_before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()

            status = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(status.status_code, 200)
            self.assertIn("损坏", status.json["confirmation_check_error"])
            self.assertFalse(status.json["retry_allowed"])
            page = client.get("/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn("原未合并确认暂时无法复核", html)
            self.assertIn("重新查询", html)
            self.assertNotIn('action="/online-scripts/task-link/demo"', html)
            repeated = client.post(
                "/api/online-scripts/task-link-confirm/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(repeated.status_code, 503)
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), receipt_before)

    def test_confirmation_publication_failures_recover_actual_state_without_fetching(self):
        for merged in (False, True):
            for after_write in (False, True):
                with self.subTest(merged=merged, after_write=after_write), isolated_app() as (app, _base_dir):
                    from fls_manager.online_scripts import source
                    from fls_manager.routes.online_scripts import task_link

                    item = self.item(tasks=[self.task("published")] if merged else [])
                    source.save_online_script_cache([item])
                    request_id = "6" * 32
                    source.persist_task_link_request(self._unknown_record(
                        source, request_id, item,
                        published=source.online_script_task_fingerprint(item) if merged else "",
                    ))
                    receipt_before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
                    cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()

                    def fail_publication(record):
                        if after_write:
                            source.persist_task_link_request(record)
                        raise OSError("confirmation acknowledgement unavailable")

                    client = app.test_client()
                    with patch.object(task_link, "persist_task_link_request", side_effect=fail_publication), patch.object(
                        task_link, "fetch_task_link_tasks",
                    ) as fetch, patch.object(task_link, "merge_task_link_tasks") as merge:
                        failed = client.post("/api/online-scripts/task-link-confirm/" + request_id, headers={"X-Token": TOKEN})
                        self.assertEqual(failed.status_code, 200 if after_write else 503)
                        if after_write:
                            self.assertTrue(failed.json["receipt_confirmed"])
                            self.assertTrue(failed.json["receipt_recovered"])
                            self.assertEqual(failed.json["confirmation"], "merged" if merged else "not_merged")
                        else:
                            self.assertIn("confirmation acknowledgement unavailable", failed.json["msg"])
                            self.assertTrue(failed.json["receipt_confirmation_required"])
                            self.assertEqual(failed.json["state_url"], "/api/online-scripts/task-link-status/" + request_id)
                        actual = client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN})
                        expected_state = ("completed" if merged else "failed") if after_write else "unknown"
                        self.assertEqual(actual.json["state"], expected_state)
                        fetch.assert_not_called()
                        merge.assert_not_called()
                    if not after_write:
                        self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), receipt_before)
                    recovered = client.post("/api/online-scripts/task-link-confirm/" + request_id, headers={"X-Token": TOKEN})
                    self.assertEqual(recovered.status_code, 200)
                    self.assertEqual(recovered.json["confirmation"], "merged" if merged else "not_merged")
                    self.assertEqual(recovered.json.get("replayed", False), after_write)
                    self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)

    def test_confirmation_post_write_conflict_or_corrupt_receipt_stays_queryable_without_merge(self):
        for failure in ("conflict", "corrupt"):
            with self.subTest(failure=failure), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                item = self.item(tasks=[self.task("published")])
                source.save_online_script_cache([item])
                request_id = "7" * 32
                source.persist_task_link_request(self._unknown_record(
                    source, request_id, item,
                    published=source.online_script_task_fingerprint(item),
                ))
                real = source.persist_task_link_request

                def conflicting_persist(record):
                    if failure == "conflict":
                        actual = dict(record, confirmation_fingerprint="f" * 64)
                        real(actual)
                    else:
                        real(record)
                        source.ONLINE_TASK_LINK_REQUESTS_FILE.write_text("{broken", encoding="utf-8")
                    raise OSError("confirmation receipt publication interrupted")

                client = app.test_client()
                with patch.object(task_link, "persist_task_link_request", side_effect=conflicting_persist), patch.object(
                    task_link, "fetch_task_link_tasks",
                ) as fetch, patch.object(task_link, "merge_task_link_tasks") as merge:
                    response = client.post(
                        "/api/online-scripts/task-link-confirm/" + request_id,
                        headers={"X-Token": TOKEN},
                    )
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json["ok"])
                self.assertTrue(response.json["receipt_confirmation_required"])
                self.assertEqual(response.json["state_url"], "/api/online-scripts/task-link-status/" + request_id)
                self.assertIn("confirmation receipt publication interrupted", response.json["msg"])
                fetch.assert_not_called()
                merge.assert_not_called()
                if failure == "conflict":
                    status = client.get(response.json["state_url"], headers={"X-Token": TOKEN})
                    self.assertEqual(status.status_code, 200)
                    self.assertEqual(status.json["confirmation_fingerprint"], "f" * 64)
                else:
                    self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_text(encoding="utf-8"), "{broken")
                    status = client.get(response.json["state_url"], headers={"X-Token": TOKEN})
                    self.assertEqual(status.status_code, 500)

    def test_confirmation_recovers_published_refresh_recovery_receipt_after_exception(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.routes.online_scripts import task_link

            refresh_request_id = "6" * 32
            item = self.item(tasks=[self.task("published")])
            source.save_online_script_cache([item])
            constants.update_online_refresh_state(
                id="5" * 32, request_id=refresh_request_id, proxy_id="", running=False,
                status="部分完成", message="1 个任务源失败", error="", cache_published=True,
                task_link_failures=[{
                    "script_id": "demo", "name": "Demo", "task_link": item["task_link"],
                    "error": "upstream timeout", "proxy_id": "",
                }], updated_at="2026-11-21 12:30:00", log_file="",
            )
            request_id = constants.online_refresh_task_link_request_id(refresh_request_id, "demo")
            record = self._unknown_record(
                source, request_id, item,
                published=source.online_script_task_fingerprint(item),
            )
            record["origin_refresh_request_id"] = refresh_request_id
            source.persist_task_link_request(record)
            real_persist = constants.persist_online_refresh_task_link_recovery

            def publish_then_raise(*args, **kwargs):
                receipt = real_persist(*args, **kwargs)
                raise OSError("refresh recovery receipt acknowledgement lost after replace")

            client = app.test_client()
            with patch.object(task_link, "fetch_task_link_tasks") as fetch, patch.object(
                task_link, "merge_task_link_tasks",
            ) as merge, patch.object(
                task_link, "persist_online_refresh_task_link_recovery", side_effect=publish_then_raise,
            ):
                response = client.post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["confirmation"], "merged")
                repeated = client.post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )
            self.assertTrue(repeated.json["replayed"])
            fetch.assert_not_called()
            merge.assert_not_called()
            self.assertEqual(source.get_task_link_request(request_id)["state"], "completed")
            self.assertEqual(
                constants.get_online_refresh_task_link_recovery(refresh_request_id, "demo")["request_id"],
                request_id,
            )

    def test_confirmation_form_session_and_concurrent_replay_preserve_permission_boundaries(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "5" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item))
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="confirm-csrf")
            real_persist = source.persist_task_link_request

            def persist_then_raise(record):
                real_persist(record)
                if record.get("confirmation"):
                    raise OSError("confirmation form acknowledgement lost after replace")

            with patch.object(task_link, "persist_task_link_request", side_effect=persist_then_raise) as persist, patch.object(
                task_link, "fetch_task_link_tasks",
            ) as fetch:
                invalid_api = client.post(
                    "/api/online-scripts/task-link-confirm/not-a-request",
                    headers={"X-CSRF-Token": "confirm-csrf"},
                )
                self.assertEqual(invalid_api.status_code, 400)
                invalid_form = client.post(
                    "/online-scripts/task-link-confirm/not-a-request",
                    data={"csrf_token": "confirm-csrf"},
                )
                self.assertEqual(invalid_form.status_code, 400)
                self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post("/api/online-scripts/task-link-confirm/" + request_id,
                                      headers={"X-CSRF-Token": "confirm-csrf"})
                self.assertEqual(expired.status_code, 401)
                persist.assert_not_called()
                self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="confirm-csrf")
                form = client.post("/online-scripts/task-link-confirm/" + request_id, data={"csrf_token": "confirm-csrf"})
                self.assertEqual(form.status_code, 302, form.get_data(as_text=True))
                self.assertTrue(form.location.endswith("/online-scripts/task-link-status/" + request_id))
                persist.assert_called_once()
                fetch.assert_not_called()
            confirmed = source.get_task_link_request(request_id)
            self.assertEqual(confirmed["confirmation"], "not_merged")

            barrier = threading.Barrier(2)

            def confirm():
                barrier.wait(3)
                return app.test_client().post("/api/online-scripts/task-link-confirm/" + request_id, headers={"X-Token": TOKEN})

            confirmed_bytes = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            with patch.object(task_link, "persist_task_link_request") as persist, ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(confirm) for _ in range(2)]
                responses = [future.result(timeout=5) for future in futures]
            self.assertEqual([response.status_code for response in responses], [200, 200])
            self.assertTrue(all(response.json["replayed"] for response in responses))
            persist.assert_not_called()
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), confirmed_bytes)

    def test_concurrent_first_confirmations_publish_once_and_replay_same_result(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item = self.item(tasks=[self.task("published")])
            source.save_online_script_cache([item])
            request_id = "8" * 32
            source.persist_task_link_request(self._unknown_record(
                source, request_id, item,
                published=source.online_script_task_fingerprint(item),
            ))
            barrier = threading.Barrier(2)

            def confirm():
                barrier.wait(3)
                return app.test_client().post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )

            with patch.object(task_link, "persist_task_link_request", wraps=source.persist_task_link_request) as persist, ThreadPoolExecutor(max_workers=2) as pool:
                responses = [future.result(timeout=5) for future in (pool.submit(confirm), pool.submit(confirm))]
            self.assertEqual([response.status_code for response in responses], [200, 200])
            self.assertEqual(sorted(response.json.get("replayed", False) for response in responses), [False, True])
            self.assertEqual([response.json["confirmation"] for response in responses], ["merged", "merged"])
            persist.assert_called_once()
            self.assertEqual(source.get_task_link_request(request_id)["state"], "completed")

    def test_retry_claim_rechecks_not_merged_fingerprint_after_cache_read(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "8" * 32
            record = self._unknown_record(source, request_id, item, state="failed")
            record["confirmation"] = "not_merged"
            record["confirmation_fingerprint"] = source.online_script_task_fingerprint(item)
            source.persist_task_link_request(record)
            real_claim = task_link.claim_task_link_request
            claim_ready, allow_mutation = threading.Event(), threading.Event()
            worker_errors = []
            workers = []

            def mutate_after_route_read():
                claim_ready.set()
                if not allow_mutation.wait(3):
                    worker_errors.append("cache mutation was not released")
                    return
                try:
                    latest = source.load_online_script_cache()
                    latest[0]["task_cron"] = [self.task("concurrent")]
                    source.save_online_script_cache(latest)
                except Exception as exc:
                    worker_errors.append(str(exc))

            def claim_after_cache_changed(snapshot):
                worker = threading.Thread(target=mutate_after_route_read)
                worker.start()
                workers.append(worker)
                self.assertTrue(claim_ready.wait(2))
                allow_mutation.set()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                return real_claim(snapshot)

            receipt_before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            try:
                with patch.object(task_link, "claim_task_link_request", side_effect=claim_after_cache_changed), patch.object(
                    task_link, "fetch_task_link_tasks",
                ) as fetch:
                    response = app.test_client().post(
                        "/api/online-scripts/task-link/demo",
                        json={"request_id": request_id},
                        headers={"X-Token": TOKEN},
                    )
                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.json["retry_allowed"])
                self.assertIn("确认", response.json["msg"])
                fetch.assert_not_called()
                self.assertEqual(source.get_task_link_request(request_id)["confirmation"], "not_merged")
                self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), receipt_before)
                self.assertEqual(worker_errors, [])
            finally:
                allow_mutation.set()
                for worker in workers:
                    worker.join(timeout=3)

    def test_retry_claim_cache_read_failure_preserves_receipt_and_skips_fetch(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link
            from fls_manager.storage import _write_json_unlocked

            item = self.item()
            source.save_online_script_cache([item])
            item = source.load_online_script_cache()[0]
            request_id = "4" * 32
            record = self._unknown_record(source, request_id, item, state="failed")
            record["confirmation"] = "not_merged"
            record["confirmation_fingerprint"] = source.online_script_task_fingerprint(item)
            source.persist_task_link_request(record)
            receipt_before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            real_claim = task_link.claim_task_link_request

            def break_cache_then_claim(snapshot):
                source.ONLINE_SCRIPT_CACHE_FILE.unlink()
                source.ONLINE_SCRIPT_CACHE_FILE.mkdir()
                return real_claim(snapshot)

            with patch.object(task_link, "claim_task_link_request", side_effect=break_cache_then_claim), patch.object(
                task_link, "fetch_task_link_tasks",
            ) as fetch, patch("fls_manager.storage._write_json_unlocked", wraps=_write_json_unlocked) as write_json:
                response = app.test_client().post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 503)
            self.assertIn("缓存无法复核", response.json["msg"])
            fetch.assert_not_called()
            write_json.assert_not_called()
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), receipt_before)

    def test_confirmation_serializes_cache_publication_and_rejects_stale_retry(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.routes.online_scripts import task_link

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "f" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item))
            read_cache = task_link.load_online_script_cache
            persist_receipt = task_link.persist_task_link_request
            writer_ready, allow_write, write_done = threading.Event(), threading.Event(), threading.Event()
            receipt_saw_changed_cache = []
            worker_errors = []
            workers = []

            def change_cache():
                writer_ready.set()
                if not allow_write.wait(3):
                    worker_errors.append("cache writer was not released")
                    return
                try:
                    latest = source.load_online_script_cache()
                    latest[0]["task_cron"] = [self.task("concurrent")]
                    source.save_online_script_cache(latest)
                except Exception as exc:
                    worker_errors.append(str(exc))
                finally:
                    write_done.set()

            def read_and_schedule_cache_writer():
                current = read_cache()
                worker = threading.Thread(target=change_cache)
                worker.start()
                workers.append(worker)
                self.assertTrue(writer_ready.wait(2))
                return current

            def release_writer_before_receipt(record):
                allow_write.set()
                receipt_saw_changed_cache.append(write_done.wait(0.2))
                return persist_receipt(record)

            try:
                with patch.object(task_link, "load_online_script_cache", side_effect=read_and_schedule_cache_writer), patch.object(
                    task_link, "persist_task_link_request", side_effect=release_writer_before_receipt,
                ):
                    response = app.test_client().post(
                        "/api/online-scripts/task-link-confirm/" + request_id,
                        headers={"X-Token": TOKEN},
                    )
                for worker in workers:
                    worker.join(timeout=3)
                    self.assertFalse(worker.is_alive())
                self.assertEqual(worker_errors, [])
                self.assertEqual(receipt_saw_changed_cache, [False])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["confirmation"], "not_merged")
                record = source.get_task_link_request(request_id)
                self.assertEqual(record["confirmation_fingerprint"], source.online_script_task_fingerprint(item))
                with patch.object(task_link, "fetch_task_link_tasks") as fetch:
                    retry = app.test_client().post(
                        "/api/online-scripts/task-link/demo",
                        json={"request_id": request_id},
                        headers={"X-Token": TOKEN},
                    )
                self.assertEqual(retry.status_code, 409)
                self.assertFalse(retry.json.get("retry_allowed", False))
                self.assertIn("确认", retry.json.get("msg", ""))
                fetch.assert_not_called()
            finally:
                allow_write.set()
                for worker in workers:
                    worker.join(timeout=3)

    def test_confirmation_rejects_later_task_change_without_mutating_receipt_or_tasks(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "1" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item))
            changed = self.item(tasks=[self.task("manual-change")])
            source.save_online_script_cache([changed])
            client = app.test_client()
            response = client.post(
                "/api/online-scripts/task-link-confirm/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 409)
            self.assertIn("不一致", response.json["msg"])
            self.assertEqual(source.load_online_script_cache()[0]["task_cron"], changed["task_cron"])
            self.assertEqual(source.get_task_link_request(request_id)["state"], "unknown")

    def test_stale_running_request_becomes_unknown_after_restart_and_requires_confirmation(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            source.save_online_script_cache([item])
            request_id = "2" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item, state="running"))
            self.assertNotIn(request_id, __import__("fls_manager.online_scripts.constants", fromlist=["ONLINE_TASK_LINK_ACTIVE"]).ONLINE_TASK_LINK_ACTIVE)
            status = app.test_client().get(
                "/api/online-scripts/task-link-status/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["state"], "unknown")
            self.assertIn("面板重启", status.json["error"])

    def test_confirmation_requires_csrf_and_rejects_invalid_id_without_storage_write(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.task_link.persist_task_link_request") as persist:
                missing_csrf = client.post("/api/online-scripts/task-link-confirm/" + "3" * 32)
                invalid = client.post(
                    "/api/online-scripts/task-link-confirm/not-a-request",
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(missing_csrf.status_code, 400)
            self.assertEqual(invalid.status_code, 400)
            persist.assert_not_called()
            self.assertFalse(source.ONLINE_TASK_LINK_REQUESTS_FILE.exists())

    def test_task_link_history_api_is_bounded_filtered_and_marks_stale_running_unknown(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            source.save_online_script_cache([item])
            first = self._unknown_record(source, "5" * 32, item, state="running")
            second = self._unknown_record(source, "6" * 32, item, state="failed")
            second["updated_at"] = "2999-01-01 00:00:00"
            source.persist_task_link_request(first)
            source.persist_task_link_request(second)
            response = app.test_client().get(
                "/api/online-scripts/task-link-history?limit=1&script_id=demo",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json["items"]), 1)
            self.assertEqual(response.json["items"][0]["request_id"], "6" * 32)
            stale = app.test_client().get(
                "/api/online-scripts/task-link-status/" + "5" * 32,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(stale.json["state"], "unknown")
            history = app.test_client().get(
                "/online-scripts/task-link-history?script_id=demo",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            self.assertIn("外部任务源同步历史", history.get_data(as_text=True))

    def test_task_link_history_rejects_invalid_limit_and_corrupt_storage_without_overwrite(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source

            client = app.test_client()
            invalid = client.get(
                "/api/online-scripts/task-link-history?limit=0",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid.status_code, 400)
            source.ONLINE_TASK_LINK_REQUESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            source.ONLINE_TASK_LINK_REQUESTS_FILE.write_text("not-json", encoding="utf-8")
            original = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            failed = client.get("/api/online-scripts/task-link-history", headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            page = client.get("/online-scripts/task-link-history", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 500)
            self.assertIn("无法读取", page.get_data(as_text=True))
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), original)

    def test_task_link_history_completed_entry_links_to_task_selection(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item(tasks=[self.task("ready")])
            source.save_online_script_cache([item])
            request_id = "7" * 32
            record = self._unknown_record(
                source, request_id, item,
                published=source.online_script_task_fingerprint(source.load_online_script_cache()[0]),
                state="completed",
            )
            source.persist_task_link_request(record)
            page = app.test_client().get("/online-scripts/task-link-history", headers={"X-Token": TOKEN})
            html = page.get_data(as_text=True)
            self.assertIn("/online-scripts/task-link-status/" + request_id, html)
            self.assertIn("/online-scripts/install-select/demo", html)
            selected = app.test_client().get(
                "/online-scripts/install-select/demo?task_link_request_id=" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(selected.status_code, 200)
            selected_html = selected.get_data(as_text=True)
            self.assertIn('name="task_link_request_id" value="' + request_id + '"', selected_html)
            self.assertIn("外部任务源同步已确认完成", selected_html)
            self.assertIn("task_link_request_id", selected_html)

    def test_task_link_selection_rejects_unknown_or_changed_receipt_without_rendering_old_tasks(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item(tasks=[self.task("ready")])
            source.save_online_script_cache([item])
            request_id = "8" * 32
            source.persist_task_link_request(self._unknown_record(source, request_id, item, state="unknown"))
            client = app.test_client()
            unknown = client.get(
                "/online-scripts/install-select/demo?task_link_request_id=" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(unknown.status_code, 409)
            self.assertIn("尚未确认完成", unknown.get_data(as_text=True))
            changed = self.item(tasks=[self.task("changed")])
            source.save_online_script_cache([changed])
            completed = self._unknown_record(
                source, request_id, changed,
                published=source.online_script_task_fingerprint(source.load_online_script_cache()[0]),
                state="completed",
            )
            source.persist_task_link_request(completed)
            changed["link"] = "https://example.invalid/changed.sh"
            source.save_online_script_cache([changed])
            conflict = client.get(
                "/online-scripts/install-select/demo?task_link_request_id=" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(conflict.status_code, 409)
            self.assertIn("脚本源已变化", conflict.get_data(as_text=True))

    def test_completed_receipt_write_failure_is_confirmed_without_replaying_fetch(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            request_id = "b" * 32
            persist = source.persist_task_link_request

            def receipt(info):
                if info["state"] == "completed":
                    persist(info)
                    raise OSError("fixture receipt acknowledgement fault")
                return persist(info)

            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.task_link.persist_task_link_request", side_effect=receipt), patch(
                "fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", return_value=[self.task("new")],
            ) as fetch:
                first = client.post(
                    "/online-scripts/task-link/demo",
                    data={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 302)
                repeated = client.post(
                    "/online-scripts/task-link/demo",
                    data={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(repeated.status_code, 200)
            self.assertIn("外部任务源原同步结果", repeated.get_data(as_text=True))
            self.assertIn('href="/online-scripts/task-link-status/' + request_id + '"', repeated.get_data(as_text=True))
            fetch.assert_called_once()
            self.assertEqual(client.get("/api/online-scripts/task-link-status/" + request_id, headers={"X-Token": TOKEN}).json["state"], "completed")

    def test_lost_completion_receipt_persists_published_fingerprint_for_confirmation(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            request_id = "4" * 32
            real_persist = source.persist_task_link_request

            def lost_completion(info):
                if info.get("state") == "completed":
                    raise OSError("completion receipt unavailable")
                return real_persist(info)

            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.task_link.persist_task_link_request", side_effect=lost_completion), patch(
                "fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks",
                return_value=[self.task("new")],
            ) as fetch:
                first = client.post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 503)
            status = client.get(
                "/api/online-scripts/task-link-status/" + request_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.json["state"], "unknown")
            self.assertTrue(status.json["published_tasks_fingerprint"])
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks") as refetch:
                confirmed = client.post(
                    "/api/online-scripts/task-link-confirm/" + request_id,
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json["confirmation"], "merged")
            fetch.assert_called_once()
            refetch.assert_not_called()

    def test_invalid_request_id_is_rejected_before_fetch_or_receipt_write(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks") as fetch, patch(
                "fls_manager.routes.online_scripts.task_link.persist_task_link_request",
            ) as persist:
                response = app.test_client().post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": "../bad"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 400)
            fetch.assert_not_called()
            persist.assert_not_called()
            self.assertFalse((source.ONLINE_TASK_LINK_REQUESTS_FILE).exists())

    def test_source_change_or_deletion_during_fetch_returns_conflict_without_overwriting(self):
        for change in ("delete", "task_link", "link", "name"):
            with self.subTest(change=change), isolated_app() as (app, _base_dir):
                from fls_manager.online_scripts import source

                source.save_online_script_cache([self.item()])
                replacement = self.item()
                replacement[change] = "updated"
                updated = [] if change == "delete" else [replacement]

                def fetch(*_args, **_kwargs):
                    source.save_online_script_cache(updated)
                    return [self.task("obsolete")]

                with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", side_effect=fetch):
                    response = app.test_client().post("/online-scripts/task-link/demo", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 409)
                self.assertIn("脚本源已更新或删除", response.get_data(as_text=True))
                self.assertEqual(source.load_online_script_cache(), source.normalize_online_scripts(updated))

    def test_fetch_merges_into_latest_tasks_and_preserves_other_cache_updates(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])

            def fetch(*_args, **_kwargs):
                source.save_online_script_cache([
                    self.item(tasks=[self.task("manual")]), self.item("new-script"),
                ])
                return [self.task("remote")]

            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", side_effect=fetch):
                response = app.test_client().post("/online-scripts/task-link/demo", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            items = source.load_online_script_cache()
            self.assertEqual([item["id"] for item in items], ["demo", "new-script"])
            self.assertEqual([task["name"] for task in items[0]["task_cron"]], ["manual", "remote"])

    def test_concurrent_syncs_merge_latest_cache_without_losing_or_duplicating_tasks(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item(tasks=[self.task("existing")])])
            from fls_manager.models import save_proxies
            save_proxies([
                {"id": "first", "name": "First", "type": "http",
                 "host": "127.0.0.1", "port": "8080", "enabled": True},
                {"id": "second", "name": "Second", "type": "http",
                 "host": "127.0.0.1", "port": "8081", "enabled": True},
            ])
            barrier = threading.Barrier(2)

            def fetch(_item, proxy_id="", **_kwargs):
                barrier.wait(timeout=3)
                return [self.task(proxy_id), self.task("shared")]

            def post(proxy_id):
                return app.test_client().post(
                    "/online-scripts/task-link/demo", data={"proxy_id": proxy_id}, headers={"X-Token": TOKEN},
                )

            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", side_effect=fetch), ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(post, proxy_id) for proxy_id in ("first", "second")]
                responses = [future.result(timeout=5) for future in futures]
            self.assertEqual([response.status_code for response in responses], [302, 302])
            tasks = source.load_online_script_cache()[0]["task_cron"]
            self.assertEqual(len(tasks), 4)
            self.assertEqual({task["name"] for task in tasks}, {"existing", "first", "second", "shared"})

    def test_concurrent_replay_claims_one_request_before_external_fetch(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            request_id = "d" * 32
            started = threading.Event()
            release = threading.Event()

            def fetch(*_args, **_kwargs):
                started.set()
                self.assertTrue(release.wait(3))
                return [self.task("once")]

            def post():
                return app.test_client().post(
                    "/api/online-scripts/task-link/demo",
                    json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )

            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", side_effect=fetch) as fetch_mock, ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(post)
                self.assertTrue(started.wait(3))
                second = pool.submit(post).result(timeout=3)
                release.set()
                first = first_future.result(timeout=5)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 409)
            fetch_mock.assert_called_once()

    def test_missing_script_or_link_returns_recovery_page_without_fetching(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            item = self.item()
            item.pop("task_link")
            source.save_online_script_cache([item])
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks") as fetch:
                for script_id, status in (("deleted", 404), ("demo", 400)):
                    response = app.test_client().post(f"/online-scripts/task-link/{script_id}", headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, status)
                    self.assertIn("检查脚本源数据", response.get_data(as_text=True))
                fetch.assert_not_called()

    def test_rendered_form_preserves_proxy_and_csrf_through_error_and_retry(self):
        with isolated_app() as (app, _base_dir):
            import fls_manager.app as app_module
            from fls_manager.models import save_proxies
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            save_proxies([{"id": "chosen", "name": "Chosen proxy", "type": "http",
                           "host": "127.0.0.1", "port": "8080", "enabled": True}])
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="task-link-csrf")
            with patch.object(app_module, "dispatch_frontend_open_event"):
                page = client.get("/online-scripts")
            self.assertEqual(page.status_code, 200)
            parser = TaskLinkFormParser()
            parser.feed(page.get_data(as_text=True))
            form = next(form for form in parser.forms if form["attrs"].get("action") == "/online-scripts/task-link/demo")
            self.assertEqual(form["attrs"].get("method"), "post")
            self.assertTrue(any(tag == "select" and attrs.get("name") == "proxy_id" for tag, attrs in form["controls"]))
            csrf = next(attrs["value"] for tag, attrs in form["controls"] if attrs.get("name") == "csrf_token")
            headers = {"X-Requested-With": "FLS-Ajax"}
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", side_effect=TimeoutError("proxy timeout")) as fetch:
                error = client.post(form["attrs"]["action"], data={"proxy_id": "chosen", "csrf_token": csrf}, headers=headers)
            self.assertEqual(error.status_code, 502)
            fetch.assert_called_once_with(source.load_online_script_cache()[0], proxy_id="chosen", timeout=20)
            retry_parser = TaskLinkFormParser()
            retry_parser.feed(error.get_data(as_text=True))
            retry = next(form for form in retry_parser.forms if form["attrs"].get("action") == "/online-scripts/task-link/demo")
            self.assertTrue(any(tag == "option" and attrs.get("value") == "chosen" and "selected" in attrs for tag, attrs in retry["controls"]))
            retry_csrf = next(attrs["value"] for tag, attrs in retry["controls"] if attrs.get("name") == "csrf_token")
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", return_value=[self.task("importable")]):
                recovered = client.post(retry["attrs"]["action"], data={"proxy_id": "chosen", "csrf_token": retry_csrf}, headers=headers)
            self.assertEqual(recovered.status_code, 302)
            with patch.object(app_module, "dispatch_frontend_open_event"):
                selected_page = client.get("/online-scripts/install-select/demo")
            self.assertEqual(selected_page.status_code, 200)
            self.assertIn("importable", selected_page.get_data(as_text=True))

    def test_invalid_proxy_is_rejected_before_external_fetch_or_cache_write(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import save_proxies
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item(tasks=[self.task("existing")])])
            original = source.load_online_script_cache()
            save_proxies([{"id": "disabled", "name": "Disabled", "type": "http",
                           "host": "127.0.0.1", "port": "8080", "enabled": False}])
            client = app.test_client()
            for proxy_id in ("missing", "disabled"):
                with self.subTest(proxy_id=proxy_id):
                    with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks") as fetch:
                        response = client.post(
                            "/online-scripts/task-link/demo",
                            data={"proxy_id": proxy_id},
                            headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                        )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("代理", response.get_data(as_text=True))
                    fetch.assert_not_called()
                    self.assertEqual(source.load_online_script_cache(), original)

    def test_session_expiry_and_csrf_rejection_do_not_fetch_or_mutate(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            original = source.load_online_script_cache()
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="task-link-csrf")
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks") as fetch:
                for csrf in ("", "wrong"):
                    response = client.post("/online-scripts/task-link/demo", data={"csrf_token": csrf}, headers=headers)
                    self.assertEqual(response.status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post("/online-scripts/task-link/demo", data={"csrf_token": "task-link-csrf"}, headers=headers)
                self.assertEqual(expired.status_code, 401)
                fetch.assert_not_called()
            self.assertEqual(source.load_online_script_cache(), original)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise AJAX error rendering")
    def test_ajax_renders_retry_error_page_without_requesting_post_url_as_get(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            with patch("fls_manager.routes.online_scripts.task_link.fetch_task_link_tasks", side_effect=TimeoutError("upstream timeout")):
                response = app.test_client().post(
                    "/online-scripts/task-link/demo", headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                )
            self.assertEqual(response.status_code, 502)
            js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            handler = js[js.index("    async function handleFormErrorResponse("):js.index("    async function ajaxLoad(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let rendered = 0;
const context = {
 document: {querySelector: () => ({})},
 DOMParser: class {parseFromString(body) {
   assert.match(body, /重新同步外部任务源/);
   return {querySelector: () => ({})};
 }},
 replaceHtmlText: async (body, url, push) => {
   assert.match(body, /name="csrf_token"/);
   assert.match(body, /upstream timeout/);
   assert.equal(url, '/online-scripts/task-link/demo');
   assert.equal(push, false);
   rendered += 1;
 },
 fetch: () => {throw Error('Unexpected follow-up GET');},
};
vm.createContext(context);
vm.runInContext(input.handler, context);
(async () => {
 await context.handleFormErrorResponse({
   status: 502, url: '/online-scripts/task-link/demo',
   headers: {get: () => 'text/html'}, text: async () => input.html,
 }, '/online-scripts/task-link/demo', {}, {});
 assert.equal(rendered, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""], input=json.dumps({"handler": handler, "html": response.get_data(as_text=True)}),
                text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise task-link recovery")
    def test_task_link_ajax_replay_identity_and_ambiguous_errors_preserve_original_request(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            html = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN}).get_data(as_text=True)
            scripts = __import__("re").findall(r"<script>(.*?)</script>", html, __import__("re").S)
            script = next(value for value in scripts if "queryPending(pending)" in value)
            script = script[script.index("(function(){", script.index("updateOnlineRefreshStatus();")):]
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8')),requestId='a'.repeat(32);
function element(tag='div'){
    return {tagName:tag.toUpperCase(),children:[],listeners:{},dataset:{},hidden:true,disabled:false,textContent:'',href:'',value:'',
   appendChild(child){this.children.push(child);},addEventListener(name,fn){this.listeners[name]=fn;},
   setAttribute(name,value){(this.attrs||(this.attrs={}))[name]=value;},
   getAttribute(name){return this.attrs&&this.attrs[name]||'';}};
}
async function scenario(status,payload,expected){
 const recovery=element(),button=element('button'),form=element('form');
 form.attrs={'data-script-id':'demo'};form.action='/online-scripts/task-link/demo';
 form.querySelector=selector=>selector.includes('request_id')?{value:requestId}:selector.includes('proxy_id')?{value:''}:button;
 form.closest=()=>form;
 const storage={value:null,getItem(){return this.value;},setItem(_key,value){this.value=value;},removeItem(){this.value=null;}};
 let submit,calls=0,finish;
 const context={window:{location:{href:''}},location:{pathname:'/online-scripts',search:''},sessionStorage:storage,encodeURIComponent,
   document:{getElementById:()=>recovery,createElement:element,querySelectorAll:()=>[],addEventListener:(_name,fn)=>{submit=fn;}},
   FormData:class{},flsGetCsrfToken:()=> 'csrf',
   flsResponseErrorDiagnostic:(_response,body)=>{let data;try{data=JSON.parse(body);}catch{}return {payload:data,message:data&&(data.msg||data.error)||body};},
   fetch:(url,options)=>{calls++;assert.equal(url,'/api/online-scripts/task-link/demo');assert.equal(options.method,'POST');assert.equal(options.headers['X-CSRF-Token'],'csrf');return new Promise(resolve=>{finish=resolve;});}};
 vm.createContext(context);vm.runInContext(input.reader+input.script,context);
 const event={target:form,preventDefault(){},stopImmediatePropagation(){}};
 const pending=submit(event);await submit(event);assert.equal(calls,1);
 finish(status===0?Promise.reject(new Error('connection interrupted')):{ok:status===200,status,
   text:async()=>typeof payload==='string'?payload:JSON.stringify(payload),json:async()=>payload});
 await pending;
 if(expected==='completed'){
   assert.equal(storage.value,null);
   assert.equal(context.window.location.href,'/online-scripts/task-link-status/'+requestId);
 }else if(expected==='history'){
   assert.equal(storage.value,null);
   assert.equal(button.disabled,false);
   assert.equal(context.window.location.href,'');
   assert.equal(recovery.children[1].href,'/online-scripts/task-link-history?recovery=1');
   assert.equal(recovery.children.length,2);
 }else{
   assert.equal(JSON.parse(storage.value).request_id,requestId);
   assert.equal(button.disabled,false);
   assert.equal(context.window.location.href,'');
   assert.equal(recovery.children[1].href,'/online-scripts/task-link-status/'+requestId);
   const labels=recovery.children.flatMap(child=>[child.textContent,...child.children.map(value=>value.textContent)]);
   if(expected==='confirm') assert.ok(labels.includes('检查当前任务合并'));
   else if(expected==='retry') assert.ok(labels.includes('继续原同步请求'));
   else if(expected==='login') assert.ok(labels.includes('重新登录'));
   else assert.ok(labels.includes('重新读取同步状态'));
   if(expected!=='retry') assert.ok(!labels.includes('继续原同步请求'));
 }
}
(async()=>{
 for(const status of [401,403,404,409,500,503]){
   await scenario(status,'<html>temporarily unavailable</html>',status===401||status===403?'login':'query');
 }
 await scenario(0,null,'query');
 await scenario(409,{ok:false,request_id:requestId,history_full:true,accepted:false,retry_allowed:false,msg:'recovery history full'},'history');
 await scenario(409,{ok:false,request_id:'b'.repeat(32),history_full:true,accepted:false,retry_allowed:false},'query');
 await scenario(503,{ok:false,request_id:requestId,receipt_missing:true,active:true,retry_allowed:false},'query');
 await scenario(409,{ok:false,request_id:requestId,state:'unknown',archived:true,retry_allowed:false},'query');
 await scenario(409,{ok:false,request_id:requestId,state:'unknown',retry_allowed:false,msg:'original unknown diagnosis'},'confirm');
 await scenario(409,{ok:false,request_id:requestId,state:'running',retry_allowed:false,msg:'still running'},'query');
 await scenario(502,{ok:false,request_id:requestId,retry_allowed:true,msg:'upstream failed'},'retry');
 await scenario(200,{ok:true,request_id:'b'.repeat(32),replayed:true},'query');
 await scenario(200,{ok:true,replayed:true},'query');
 await scenario(200,{ok:true,request_id:requestId,replayed:true},'completed');
 console.log('task-link replay identity, errors and concurrent submission passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''], input=json.dumps({"script": script, "reader": reader}), text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise task-link recovery")
    def test_task_link_ajax_stale_confirmation_removes_confirm_and_retry_actions(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            html = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN}).get_data(as_text=True)
            scripts = __import__("re").findall(r"<script>(.*?)</script>", html, __import__("re").S)
            script = next(value for value in scripts if "queryPending(pending)" in value)
            script = script[script.index("(function(){", script.index("updateOnlineRefreshStatus();")):]
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8')),requestId='7'.repeat(32);
function element(tag='div'){
 return {tagName:tag.toUpperCase(),children:[],listeners:{},hidden:true,disabled:false,_textContent:'',
   get textContent(){return this._textContent;},set textContent(value){this._textContent=String(value);this.children=[];},href:'',value:'',
   appendChild(child){this.children.push(child);},addEventListener(name,fn){this.listeners[name]=fn;},
   setAttribute(name,value){(this.attrs||(this.attrs={}))[name]=value;},
   getAttribute(name){return this.attrs&&this.attrs[name]||'';}};
}
const recovery=element(),storage={value:JSON.stringify({request_id:requestId,script_id:'demo',proxy_id:''}),
 getItem(){return this.value;},setItem(_key,value){this.value=value;},removeItem(){this.value=null;}};
let statusCalls=0;const requests=[];
const stale={ok:true,request_id:requestId,state:'failed',confirmation:'not_merged',confirmation_stale:true,
 retry_allowed:false,confirmation_error:'原未合并确认已因任务缓存变化失效'};
const context={window:{__FLS_TASK_LINK_RECOVERY_BOUND__:false,location:{pathname:'/online-scripts',search:'',href:''}},
 location:{pathname:'/online-scripts',search:''},sessionStorage:storage,encodeURIComponent,
 document:{getElementById:()=>recovery,createElement:element,querySelectorAll:()=>[],addEventListener(){}},
 flsGetCsrfToken:()=> 'csrf',
 flsResponseErrorDiagnostic:(_response,body)=>{let payload;try{payload=JSON.parse(body);}catch{}return {payload,message:payload&&(payload.msg||payload.error)||body};},
 fetch:async(url,options={})=>{
   requests.push({url,options});
   if(url==='/api/online-scripts/task-link-status/'+requestId){
     statusCalls++;
     const body=statusCalls===1?{ok:true,request_id:requestId,state:'unknown',error:'interrupted'}:stale;
     return {ok:true,status:200,text:async()=>JSON.stringify(body)};
   }
   assert.equal(url,'/api/online-scripts/task-link-confirm/'+requestId);
   assert.equal(options.method,'POST');assert.equal(options.headers['X-CSRF-Token'],'csrf');
   return {ok:false,status:409,text:async()=>JSON.stringify({...stale,ok:false,msg:stale.confirmation_error})};
 }};
async function tick(){await new Promise(resolve=>setTimeout(resolve,0));}
function labels(){return recovery.children.flatMap(child=>[child.textContent,...child.children.map(value=>value.textContent)]);}
async function main(){
 vm.createContext(context);vm.runInContext(input.reader+input.script,context);
 await tick();
 const initial=labels().find(value=>value==='检查当前任务合并');assert.ok(initial);
 recovery.children.find(child=>child.textContent==='检查当前任务合并').listeners.click();
 await tick();
 let visible=labels();assert.ok(visible.some(value=>value.includes('缓存变化')),JSON.stringify(visible));
 assert.ok(visible.includes('重新读取同步状态'));
 assert.ok(!visible.includes('检查当前任务合并'));
 assert.ok(!visible.includes('继续原同步请求'));
 assert.equal(recovery.children[1].href,'/online-scripts/task-link-status/'+requestId);
 recovery.children.find(child=>child.textContent==='重新读取同步状态').listeners.click();
 await tick();
 visible=labels();assert.ok(visible.some(value=>value.includes('缓存变化')));
 assert.ok(visible.includes('重新读取同步状态'));
 assert.ok(!visible.includes('检查当前任务合并'));
 assert.ok(!visible.includes('继续原同步请求'));
 assert.equal(statusCalls,2);
 assert.equal(storage.value.includes(requestId),true);
 assert.equal(context.window.location.href,'');
 console.log('stale confirmation keeps original status navigation without retry actions');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
'''], input=json.dumps({"script": script, "reader": reader}), text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise task-link recovery")
    def test_task_link_page_ajax_submission_and_refresh_recovery(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            response = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('class="fls-online-task-link-form"', html)
            self.assertIn('data-script-id="demo"', html)
            scripts = [script for script in __import__("re").findall(r"<script>(.*?)</script>", html, __import__("re").S)]
            script = next(script for script in scripts if "queryPending(pending)" in script)
            script = script[script.index("(function(){", script.index("updateOnlineRefreshStatus();")):]
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const requestId = 'a'.repeat(32);

function makeElement(tag='div') {
  return {tagName:tag.toUpperCase(), dataset:{}, hidden:true, disabled:false, textContent:'', href:'', value:'',
    children:[], listeners:{}, className:'', type:'', appendChild(child){this.children.push(child);},
    addEventListener(name, fn){this.listeners[name]=fn;},
        querySelector(selector){
          if(selector.includes('request_id')) return this.requestInput || null;
          if(selector.includes('proxy_id')) return this.proxyInput || null;
          if(selector.includes('submit')) return this.button || null;
          return null;
        },
        closest(selector){return selector === '.fls-online-task-link-form' ? this : null;},
        getAttribute(name){return this.attrs && this.attrs[name] || '';},
        setAttribute(name, value){(this.attrs || (this.attrs = {}))[name] = String(value);},
  };
}

async function runLoad(response, expected) {
  const recovery = makeElement();
  const storage = {value: JSON.stringify({request_id:requestId, script_id:'demo', proxy_id:''}),
    getItem(){return this.value;}, removeItem(){this.value=null;}, setItem(_key,v){this.value=v;}};
  const context = {
    window:{__FLS_TASK_LINK_RECOVERY_BOUND__:false, location:{pathname:'/online-scripts', search:''}},
    location:{pathname:'/online-scripts', search:''}, sessionStorage:storage,
        document:{getElementById:id=>id==='onlineTaskLinkRecovery'?recovery:null,
          createElement:tag=>makeElement(tag), querySelectorAll:()=>[], addEventListener(){}},
    encodeURIComponent,
    flsResponseErrorDiagnostic:(_response, body)=>{let payload=null;try{payload=JSON.parse(body);}catch(_){}return {payload,message:payload&&(payload.msg||payload.error)||''};},
    flsReadJsonResponse: null,
    fetch: async (url, options)=>{assert.equal(url, '/api/online-scripts/task-link-status/'+requestId); assert.equal(options.credentials,'same-origin'); return {ok:response.ok,status:response.status,text:async()=>response.body};},
  };
  vm.createContext(context); vm.runInContext(input.reader + input.script, context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(storage.value, expected.storage);
  assert.equal(recovery.hidden, expected.hidden);
  if(expected.text) assert.match(recovery.children[0].textContent, expected.text);
  if(expected.href) assert.equal(recovery.children[1].href, expected.href);
  if(expected.allowContinue !== undefined){
    const hasContinue=recovery.children.some(child=>child.tagName==='FORM' && child.children.some(button=>button.textContent==='继续原同步请求'));
    assert.equal(hasContinue,expected.allowContinue);
  }
}

(async()=>{
  for(const allowed of [false,true]){
    await runLoad({ok:true,status:200,body:JSON.stringify({ok:true,request_id:requestId,state:'failed',active:!allowed,retry_allowed:allowed,error:'startup receipt failure'})},
      {storage:JSON.stringify({request_id:requestId,script_id:'demo',proxy_id:''}),hidden:false,text:/startup receipt failure/,allowContinue:allowed});
  }
  await runLoad({ok:true,status:200,body:JSON.stringify({ok:true,request_id:requestId,state:'completed'})},{storage:null,hidden:true});
  await runLoad({ok:false,status:502,body:JSON.stringify({msg:'gateway timeout'})},{storage:JSON.stringify({request_id:requestId,script_id:'demo',proxy_id:''}),hidden:false,text:/gateway timeout/,href:'/online-scripts/task-link-status/'+requestId});
  await runLoad({ok:true,status:200,body:'<html>bad gateway</html>'},{storage:JSON.stringify({request_id:requestId,script_id:'demo',proxy_id:''}),hidden:false,text:/响应解析失败/,href:'/online-scripts/task-link-status/'+requestId});
  await runLoad({ok:false,status:403,body:JSON.stringify({msg:'登录已过期'})},{storage:JSON.stringify({request_id:requestId,script_id:'demo',proxy_id:''}),hidden:false,text:/登录已过期/,href:'/online-scripts/task-link-status/'+requestId});

  const recovery=makeElement(), button=makeElement('button'), form=makeElement('form');
  form.attrs={'data-script-id':'demo'}; form.action='/online-scripts/task-link/demo';
  form.requestInput=Object.assign(makeElement('input'),{value:requestId});
  form.proxyInput=Object.assign(makeElement('select'),{value:'proxy'}); form.button=button;
  const storage={value:null,getItem(){return this.value;},setItem(_key,v){this.value=v;},removeItem(){this.value=null;}};
  let submitHandler;
  const context={window:{__FLS_TASK_LINK_RECOVERY_BOUND__:false,location:{pathname:'/online-scripts',search:''}},location:{pathname:'/online-scripts',search:''},sessionStorage:storage,
    document:{getElementById:id=>id==='onlineTaskLinkRecovery'?recovery:null,createElement:tag=>makeElement(tag),querySelectorAll:()=>[],addEventListener:(name,fn)=>{submitHandler=fn;}},encodeURIComponent,
    flsGetCsrfToken:()=> 'csrf',FormData:class{constructor(){this.form=form;}},
    flsReadJsonResponse:async response=>{if(!response.ok){const error=new Error('save failed');error.status=response.status;throw error;}return {ok:true};},
    fetch:async(url,options)=>{assert.equal(url,'/api/online-scripts/task-link/demo');assert.equal(options.method,'POST');assert.equal(options.headers['X-CSRF-Token'],'csrf');return {ok:false,status:500,text:async()=>JSON.stringify({msg:'save failed'})};},
  };
  vm.createContext(context); vm.runInContext(input.script, context);
  await submitHandler({target:form,preventDefault(){},stopImmediatePropagation(){}});
  assert.equal(button.disabled,false); assert.match(recovery.children[0].textContent,/save failed/); assert.ok(storage.value);
  const secondRecovery=makeElement(), previousChildren=recovery.children.length;
  context.document.getElementById=id=>id==='onlineTaskLinkRecovery'?secondRecovery:null;
  storage.value=null;
  vm.runInContext(input.script,context);
  await submitHandler({target:form,preventDefault(){},stopImmediatePropagation(){}});
  assert.equal(recovery.children.length,previousChildren);
  assert.match(secondRecovery.children[0].textContent,/save failed/);
  assert.equal(button.disabled,false);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script":script,"reader":reader}), text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
