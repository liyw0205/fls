import json
import shutil
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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
            save_proxies([{"id": "chosen", "name": "Chosen proxy", "type": "http", "enabled": True}])
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


if __name__ == "__main__":
    unittest.main()
