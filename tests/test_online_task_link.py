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
                        self.assertEqual(failed.status_code, 503)
                        self.assertIn("confirmation acknowledgement unavailable", failed.json["msg"])
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
            with patch.object(task_link, "persist_task_link_request", wraps=source.persist_task_link_request) as persist, patch.object(
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
            from fls_manager.models import save_proxies
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item()])
            save_proxies([{"id": "chosen", "name": "Chosen proxy", "type": "http",
                           "host": "127.0.0.1", "port": "8080", "enabled": True}])
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="task-link-csrf")
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
 return {tagName:tag.toUpperCase(),children:[],listeners:{},hidden:true,disabled:false,textContent:'',href:'',value:'',
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
  return {tagName:tag.toUpperCase(), hidden:true, disabled:false, textContent:'', href:'', value:'',
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
}

(async()=>{
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
})().catch(error=>{console.error(error);process.exitCode=1;});
"""], input=json.dumps({"script":script,"reader":reader}), text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
