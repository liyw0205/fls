import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class SourceEditorParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.textareas = {}
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")
        elif tag == "textarea":
            self.current = attrs.get("name") or attrs.get("aria-label")
            self.textareas[self.current] = ""

    def handle_data(self, data):
        if self.current:
            self.textareas[self.current] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.current = None


class OnlineSourceEditorTests(unittest.TestCase):
    def item(self, script_id="demo"):
        return {
            "id": script_id, "name": script_id, "type": "raw",
            "link": f"https://example.invalid/{script_id}.sh",
            "task_cron": [{"name": f"{script_id}-task", "command": f"task {script_id}.sh"}],
        }

    def form(self, client, headers=None):
        response = client.get("/online-scripts/source", headers=headers or {"X-Token": TOKEN})
        self.assertEqual(response.status_code, 200)
        return SourceEditorParser(response.get_data(as_text=True))

    def test_validation_errors_preserve_exact_draft_and_never_replace_cache(self):
        drafts = (
            "", " \n ", "\n</textarea><script>untrusted()</script>", "{}",
            json.dumps([self.item(), {"id": "incomplete"}]),
            json.dumps([self.item(), self.item()]),
        )
        for ajax in (False, True):
            for draft in drafts:
                with self.subTest(ajax=ajax, draft=draft), isolated_app() as (app, base_dir):
                    from fls_manager.online_scripts import source

                    source.save_online_script_cache([self.item("stable")])
                    original = (base_dir / "data" / "online_scripts_cache.json").read_bytes()
                    client = app.test_client()
                    form = self.form(client)
                    headers = {"X-Token": TOKEN}
                    if ajax:
                        headers["X-Requested-With"] = "FLS-Ajax"
                    with patch("fls_manager.routes.online_scripts.source_json.save_online_script_cache") as save:
                        response = client.post("/online-scripts/source", data={**form.fields, "json_text": draft}, headers=headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn("Location", response.headers)
                    parser = SourceEditorParser(response.get_data(as_text=True))
                    self.assertEqual(parser.textareas["json_text"], draft)
                    self.assertNotIn("<script>untrusted()</script>", response.get_data(as_text=True))
                    self.assertEqual((base_dir / "data" / "online_scripts_cache.json").read_bytes(), original)
                    save.assert_not_called()

    def test_storage_failure_preserves_draft_and_retry_reaches_task_import(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item("stable")])
            cache = base_dir / "data" / "online_scripts_cache.json"
            original = cache.read_bytes()
            client = app.test_client()
            form = self.form(client)
            draft = "\n" + json.dumps([self.item("new")], indent=2) + "\n"
            headers = {"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"}
            with patch.object(source.os, "replace", side_effect=PermissionError("cache read-only")):
                failed = client.post("/online-scripts/source", data={**form.fields, "json_text": draft}, headers=headers)
            self.assertEqual(failed.status_code, 500)
            self.assertIn("cache read-only", failed.get_data(as_text=True))
            retry_form = SourceEditorParser(failed.get_data(as_text=True))
            self.assertEqual(retry_form.textareas["json_text"], draft)
            self.assertEqual(cache.read_bytes(), original)
            self.assertEqual(list(cache.parent.glob(".online_scripts_cache.json.*.tmp")), [])
            recovered = client.post("/online-scripts/source", data={**retry_form.fields, "json_text": draft}, headers=headers)
            self.assertEqual(recovered.status_code, 302)
            saved = client.get(recovered.headers["Location"], headers=headers)
            self.assertEqual(saved.status_code, 200)
            self.assertIn("保存成功", saved.get_data(as_text=True))
            self.assertNotEqual(SourceEditorParser(saved.get_data(as_text=True)).fields["cache_revision"], form.fields["cache_revision"])
            select = client.get("/online-scripts/install-select/new", headers=headers)
            self.assertEqual(select.status_code, 200)
            self.assertIn("new-task", select.get_data(as_text=True))
            with patch("fls_manager.online_scripts.tasks.reload_scheduler"):
                imported = client.post("/online-scripts/import-tasks/new", data={"task_indexes": "1"}, headers=headers)
            self.assertEqual(imported.status_code, 302)
            self.assertEqual([task["name"] for task in load_tasks()], ["new-task"])

    def test_stale_editor_shows_current_cache_and_keeps_draft_for_merge(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item("original")])
            client = app.test_client()
            form = self.form(client)
            draft = json.dumps([self.item("draft")])
            source.save_online_script_cache([self.item("latest")])
            current_text = source.read_cache_text()
            with patch("fls_manager.routes.online_scripts.source_json.save_online_script_cache") as save:
                response = client.post("/online-scripts/source", data={**form.fields, "json_text": draft}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            parser = SourceEditorParser(response.get_data(as_text=True))
            self.assertEqual(parser.textareas["json_text"], draft)
            self.assertEqual(parser.textareas["当前已保存缓存"], current_text)
            self.assertNotEqual(parser.fields["cache_revision"], form.fields["cache_revision"])
            save.assert_not_called()
            self.assertEqual(source.read_cache_text(), current_text)
            merged = json.loads(parser.textareas["当前已保存缓存"]) + json.loads(parser.textareas["json_text"])
            recovered = client.post("/online-scripts/source", data={**parser.fields, "json_text": json.dumps(merged)}, headers={"X-Token": TOKEN})
            self.assertEqual(recovered.status_code, 302)
            self.assertEqual([item["id"] for item in source.load_online_script_cache()], ["latest", "draft"])

    def test_missing_revision_requires_review_and_does_not_overwrite_cache(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item("current")])
            with patch("fls_manager.routes.online_scripts.source_json.save_online_script_cache") as save:
                response = app.test_client().post(
                    "/online-scripts/source", data={"json_text": json.dumps([self.item("stale")])}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("编辑状态已失效", response.get_data(as_text=True))
            save.assert_not_called()

    def test_stale_invalid_draft_still_requires_review_of_current_cache(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item("original")])
            client = app.test_client()
            form = self.form(client)
            source.save_online_script_cache([self.item("latest")])
            response = client.post(
                "/online-scripts/source", data={**form.fields, "json_text": "invalid draft"},
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 409)
            parser = SourceEditorParser(response.get_data(as_text=True))
            self.assertEqual(parser.textareas["json_text"], "invalid draft")
            self.assertEqual(json.loads(parser.textareas["当前已保存缓存"])[0]["id"], "latest")

    def test_parallel_editors_with_same_revision_cannot_overwrite_each_other(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            source.save_online_script_cache([self.item("original")])
            fields = self.form(app.test_client()).fields
            barrier = threading.Barrier(2)

            def post(script_id):
                barrier.wait(timeout=3)
                return app.test_client().post(
                    "/online-scripts/source", data={**fields, "json_text": json.dumps([self.item(script_id)])},
                    headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(post, script_id) for script_id in ("first", "second")]
                responses = [future.result(timeout=5) for future in futures]
            self.assertEqual(sorted(response.status_code for response in responses), [302, 409])
            winner = "first" if responses[0].status_code == 302 else "second"
            loser = "second" if winner == "first" else "first"
            self.assertEqual(source.load_online_script_cache()[0]["id"], winner)
            conflict = next(response for response in responses if response.status_code == 409)
            parser = SourceEditorParser(conflict.get_data(as_text=True))
            self.assertEqual(json.loads(parser.textareas["json_text"])[0]["id"], loser)
            self.assertEqual(json.loads(parser.textareas["当前已保存缓存"])[0]["id"], winner)

    def test_missing_cache_can_be_created_or_explicitly_cleared(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import source

            client = app.test_client()
            form = self.form(client)
            self.assertEqual(form.textareas["json_text"], "[]")
            created = client.post("/online-scripts/source", data={**form.fields, "json_text": json.dumps([self.item()])}, headers={"X-Token": TOKEN})
            self.assertEqual(created.status_code, 302)
            saved_form = self.form(client)
            cleared = client.post("/online-scripts/source", data={**saved_form.fields, "json_text": "[]"}, headers={"X-Token": TOKEN})
            self.assertEqual(cleared.status_code, 302)
            self.assertEqual(source.load_online_script_cache(), [])

    def test_unreadable_cache_returns_error_and_never_writes(self):
        with isolated_app() as (app, _base_dir):
            client = app.test_client()
            draft = json.dumps([self.item()])
            with patch("fls_manager.routes.online_scripts.source_json.read_cache_text", side_effect=PermissionError("cache inaccessible")), patch(
                "fls_manager.routes.online_scripts.source_json.save_online_script_cache",
            ) as save:
                page = client.get("/online-scripts/source", headers={"X-Token": TOKEN})
                response = client.post("/online-scripts/source", data={"json_text": draft}, headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 500)
            self.assertEqual(response.status_code, 500)
            self.assertIn("cache inaccessible", response.get_data(as_text=True))
            self.assertEqual(SourceEditorParser(response.get_data(as_text=True)).textareas["json_text"], draft)
            save.assert_not_called()

    def test_session_and_csrf_boundaries_reject_save_before_reading_cache(self):
        with isolated_app() as (app, _base_dir):
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="source-csrf")
            fields = self.form(client, headers={"X-Requested-With": "FLS-Ajax"}).fields
            headers = {"X-Requested-With": "FLS-Ajax"}
            with patch("fls_manager.routes.online_scripts.source_json.read_cache_text") as read, patch(
                "fls_manager.routes.online_scripts.source_json.save_online_script_cache",
            ) as save:
                for csrf in ("", "wrong"):
                    denied = client.post("/online-scripts/source", data={**fields, "json_text": "[]", "csrf_token": csrf}, headers=headers)
                    self.assertEqual(denied.status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post("/online-scripts/source", data={**fields, "json_text": "[]"}, headers=headers)
                self.assertEqual(expired.status_code, 401)
                read.assert_not_called()
                save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
