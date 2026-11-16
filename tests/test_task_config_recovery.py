import json
import os
import shutil
import stat
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class ConfigFormParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fields = {}
        self.textareas = {}
        self.current = None
        self.actions = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")
        if tag == "textarea":
            self.current = attrs.get("name") or attrs.get("aria-label")
            self.textareas[self.current] = ""
        if tag == "button" and attrs.get("formaction"):
            self.actions.append(attrs)

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.current = None

    def handle_data(self, data):
        if self.current:
            self.textareas[self.current] += data


class TaskConfigRecoveryTests(unittest.TestCase):
    def prepare(self, base_dir, config_path="conf/app.yml", content="key: original\n"):
        from fls_manager.models import save_tasks

        save_tasks([{"id": "config", "name": "Config task", "command": "task demo.sh", "config_path": config_path}])
        target = base_dir / "scripts" / "conf" / "app.yml"
        if content is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        return target

    def form(self, client, headers=None):
        response = client.get("/task/config/config?back=/collections", headers=headers or {"X-Token": TOKEN})
        self.assertEqual(response.status_code, 200)
        return ConfigFormParser(response.get_data(as_text=True))

    def test_atomic_save_keeps_mode_owner_and_exact_text_and_refreshes_revision(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir)
            target.chmod(0o640)
            metadata = target.stat()
            client = app.test_client()
            form = self.form(client)
            draft = "key: changed\r\nnext: value\r\n"
            response = client.post("/task/config/config?back=/collections", data={**form.fields, "content": draft}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertIn("保存成功", response.get_data(as_text=True))
            self.assertEqual(target.read_bytes(), draft.encode("utf-8"))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            self.assertEqual((target.stat().st_uid, target.stat().st_gid), (metadata.st_uid, metadata.st_gid))
            updated = ConfigFormParser(response.get_data(as_text=True))
            self.assertNotEqual(updated.fields["config_revision"], form.fields["config_revision"])
            self.assertEqual(updated.textareas["content"], draft)
            self.assertIn('href="/collections"', response.get_data(as_text=True))
            self.assertEqual(list(target.parent.glob(".fls-config-*.tmp")), [])

    def test_write_failures_preserve_file_and_draft_and_allow_retry(self):
        for stage in ("fsync", "replace", "chmod"):
            for ajax in (False, True):
                with self.subTest(stage=stage, ajax=ajax), isolated_app() as (app, base_dir):
                    target = self.prepare(base_dir)
                    original = target.read_bytes()
                    client = app.test_client()
                    form = self.form(client)
                    headers = {"X-Token": TOKEN}
                    if ajax:
                        headers["X-Requested-With"] = "FLS-Ajax"
                    patch_target = f"fls_manager.routes.tasks.config_file.os.{stage}" if stage != "chmod" else "pathlib.Path.chmod"
                    draft = "\n</textarea><script>untrusted()</script>\n"
                    with patch(patch_target, side_effect=OSError("disk unavailable")):
                        response = client.post("/task/config/config", data={**form.fields, "content": draft}, headers=headers)
                    self.assertEqual(response.status_code, 500)
                    self.assertNotIn("Location", response.headers)
                    html = response.get_data(as_text=True)
                    self.assertIn("disk unavailable", html)
                    self.assertNotIn("<script>untrusted()</script>", html)
                    retry = ConfigFormParser(html)
                    self.assertEqual(retry.textareas["content"], draft)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertEqual(list(target.parent.glob(".fls-config-*.tmp")), [])
                    recovered = client.post("/task/config/config", data={**retry.fields, "content": "recovered\n"}, headers=headers)
                    self.assertEqual(recovered.status_code, 200)
                    self.assertEqual(target.read_bytes(), b"recovered\n")

    def test_cleanup_failure_does_not_hide_the_original_write_failure(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir)
            client = app.test_client()
            form = self.form(client)
            with patch("fls_manager.routes.tasks.config_file.os.replace", side_effect=OSError("write rejected")), patch(
                "pathlib.Path.unlink", side_effect=PermissionError("cleanup rejected"),
            ):
                response = client.post("/task/config/config", data={**form.fields, "content": "draft"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 500)
            self.assertIn("write rejected", response.get_data(as_text=True))
            self.assertIn("cleanup rejected", response.get_data(as_text=True))
            self.assertEqual(target.read_bytes(), b"key: original\n")

    def test_stale_form_preserves_draft_and_exposes_current_content_for_merge(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir)
            client = app.test_client()
            form = self.form(client)
            target.write_text("new: server\n", encoding="utf-8")
            response = client.post("/task/config/config", data={**form.fields, "content": "draft: local\n"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            conflict = ConfigFormParser(response.get_data(as_text=True))
            self.assertEqual(conflict.textareas["content"], "draft: local\n")
            self.assertEqual(conflict.textareas["当前配置内容"], "new: server\n")
            self.assertNotEqual(conflict.fields["config_revision"], form.fields["config_revision"])
            self.assertEqual(target.read_bytes(), b"new: server\n")
            merged = conflict.textareas["当前配置内容"] + conflict.textareas["content"]
            recovered = client.post("/task/config/config", data={**conflict.fields, "content": merged}, headers={"X-Token": TOKEN})
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(target.read_bytes(), merged.encode("utf-8"))

    def test_missing_revision_requires_review_and_never_overwrites(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir)
            response = app.test_client().post("/task/config/config", data={"content": "stale"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(target.read_bytes(), b"key: original\n")
            self.assertEqual(ConfigFormParser(response.get_data(as_text=True)).textareas["content"], "stale")

    def test_path_change_with_identical_content_does_not_authorize_overwriting_another_file(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks, save_tasks

            target = self.prepare(base_dir)
            client = app.test_client()
            form = self.form(client)
            new_target = target.with_name("other.yml")
            new_target.write_bytes(target.read_bytes())
            tasks = load_tasks()
            tasks[0]["config_path"] = "conf/other.yml"
            save_tasks(tasks)
            response = client.post("/task/config/config", data={**form.fields, "content": "old path draft"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(target.read_bytes(), b"key: original\n")
            self.assertEqual(new_target.read_bytes(), target.read_bytes())

    def test_change_during_atomic_save_is_not_overwritten(self):
        with isolated_app() as (app, base_dir):
            import fls_manager.routes.tasks.config_file as config_file

            target = self.prepare(base_dir)
            client = app.test_client()
            form = self.form(client)
            real_fsync = config_file.os.fsync

            def change_file(fd):
                target.write_bytes(b"external update\n")
                return real_fsync(fd)

            with patch.object(config_file.os, "fsync", side_effect=change_file):
                response = client.post("/task/config/config", data={**form.fields, "content": "draft\n"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(target.read_bytes(), b"external update\n")
            parser = ConfigFormParser(response.get_data(as_text=True))
            self.assertEqual(parser.textareas["当前配置内容"], "external update\n")
            self.assertEqual(parser.textareas["content"], "draft\n")
            self.assertEqual(list(target.parent.glob(".fls-config-*.tmp")), [])

    def test_parallel_forms_share_one_success_and_one_conflict(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir)
            fields = self.form(app.test_client()).fields
            barrier = threading.Barrier(2)

            def post(content):
                barrier.wait(timeout=3)
                return app.test_client().post("/task/config/config", data={**fields, "content": content}, headers={"X-Token": TOKEN})

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(post, content) for content in ("first", "second")]
                responses = [future.result(timeout=5) for future in futures]
            self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
            winner = "first" if responses[0].status_code == 200 else "second"
            self.assertEqual(target.read_text(), winner)

    def test_missing_file_creation_uses_private_mode_and_conflicts_with_external_creation(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir, content=None)
            client = app.test_client()
            form = self.form(client)
            self.assertIn("配置文件不存在", form.textareas["content"])
            created = client.post("/task/config/config", data={**form.fields, "content": ""}, headers={"X-Token": TOKEN})
            self.assertEqual(created.status_code, 200)
            self.assertEqual(target.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            # Empty existing files and absent files must have different versions.
            repeated = client.post("/task/config/config", data={**form.fields, "content": "stale"}, headers={"X-Token": TOKEN})
            self.assertEqual(repeated.status_code, 409)
            self.assertEqual(target.read_bytes(), b"")

    def test_read_failure_returns_500_without_turning_error_text_into_file_content(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir)
            client = app.test_client()
            form = self.form(client)
            with patch("fls_manager.routes.tasks.config_file._read_config_snapshot", side_effect=PermissionError("file inaccessible")), patch(
                "fls_manager.routes.tasks.config_file._save_config_atomic",
            ) as save:
                page = client.get("/task/config/config", headers={"X-Token": TOKEN})
                response = client.post("/task/config/config", data={**form.fields, "content": "draft"}, headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 500)
            self.assertEqual(ConfigFormParser(page.get_data(as_text=True)).textareas["content"], "")
            self.assertEqual(response.status_code, 500)
            self.assertEqual(ConfigFormParser(response.get_data(as_text=True)).textareas["content"], "draft")
            self.assertEqual(target.read_bytes(), b"key: original\n")
            save.assert_not_called()

    def test_post_save_read_failure_reports_that_file_was_saved(self):
        with isolated_app() as (app, base_dir):
            import fls_manager.routes.tasks.config_file as config_file

            target = self.prepare(base_dir)
            client = app.test_client()
            form = self.form(client)
            real_read = config_file._read_config_snapshot
            calls = 0

            def read(*args):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise PermissionError("post-save read failed")
                return real_read(*args)

            with patch.object(config_file, "_read_config_snapshot", side_effect=read):
                response = client.post("/task/config/config", data={**form.fields, "content": "saved"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 500)
            self.assertIn("配置已保存", response.get_data(as_text=True))
            self.assertEqual(target.read_bytes(), b"saved")
            self.assertEqual(ConfigFormParser(response.get_data(as_text=True)).fields["config_revision"], "")

    def test_post_commit_cleanup_failure_exposes_saved_state_and_requires_reread(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir)
            client = app.test_client()
            fields = self.form(client).fields
            with patch("pathlib.Path.unlink", side_effect=PermissionError("cleanup rejected")):
                response = client.post("/task/config/config", data={**fields, "content": "saved"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 500)
            self.assertIn("配置已保存", response.get_data(as_text=True))
            self.assertIn('href="/task/config/config?back=/tasks"', response.get_data(as_text=True))
            self.assertEqual(target.read_bytes(), b"saved")
            self.assertEqual(ConfigFormParser(response.get_data(as_text=True)).fields["config_revision"], "")
            retry = self.form(client)
            repeated = client.post("/task/config/config", data={**retry.fields, "content": "saved"}, headers={"X-Token": TOKEN})
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(target.read_bytes(), b"saved")

    def test_task_deleted_between_initial_lookup_and_save_preserves_draft(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import get_task

            target = self.prepare(base_dir)
            client = app.test_client()
            fields = self.form(client).fields
            task = get_task("config")
            with patch("fls_manager.routes.tasks.config_file.get_task", side_effect=[task, None]):
                response = client.post("/task/config/config", data={**fields, "content": "draft"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(ConfigFormParser(response.get_data(as_text=True)).textareas["未保存的草稿"], "draft")
            self.assertEqual(target.read_bytes(), b"key: original\n")

    def test_illegal_paths_and_outside_links_return_400_with_unsaved_draft(self):
        paths = ("../outside.yml", "/etc/config.yml", "C:\\config.yml", "..\\outside.yml", ".", "bad\x00.yml")
        for config_path in paths:
            with self.subTest(path=config_path), isolated_app() as (app, base_dir):
                target = self.prepare(base_dir, config_path=config_path)
                response = app.test_client().post("/task/config/config", data={"content": "draft"}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(ConfigFormParser(response.get_data(as_text=True)).textareas["未保存的草稿"], "draft")
                self.assertEqual(target.read_bytes(), b"key: original\n")
        with isolated_app() as (app, base_dir):
            outside = base_dir / "outside"
            outside.mkdir()
            (base_dir / "scripts" / "external").symlink_to(outside, target_is_directory=True)
            self.prepare(base_dir, config_path="external/config.yml")
            response = app.test_client().post("/task/config/config", data={"content": "draft"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(list(outside.iterdir()), [])

    def test_directory_fifo_and_non_utf8_configs_are_not_editable_or_overwritten(self):
        for kind in ("directory", "fifo", "binary"):
            with self.subTest(kind=kind), isolated_app() as (app, base_dir):
                target = self.prepare(base_dir, content=None)
                target.parent.mkdir(parents=True, exist_ok=True)
                if kind == "directory":
                    target.mkdir()
                elif kind == "fifo":
                    os.mkfifo(target)
                else:
                    target.write_bytes(b"\xff\xfe\x00")
                with patch("fls_manager.routes.tasks.config_file._save_config_atomic") as save:
                    response = app.test_client().post("/task/config/config", data={"content": "draft"}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 400)
                save.assert_not_called()

    def test_deleted_task_and_missing_config_path_preserve_unsaved_draft(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted), isolated_app() as (app, base_dir):
                from fls_manager.models import save_tasks

                self.prepare(base_dir, config_path="")
                if deleted:
                    save_tasks([])
                response = app.test_client().post("/task/config/config", data={"content": "draft"}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 404 if deleted else 400)
                self.assertEqual(ConfigFormParser(response.get_data(as_text=True)).textareas["未保存的草稿"], "draft")

    def test_csrf_expiry_and_forbidden_requests_do_not_read_or_write_config(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            client = app.test_client()
            headers = {"X-Requested-With": "FLS-Ajax"}
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="config-csrf")
            form = self.form(client, headers)
            with patch("fls_manager.routes.tasks.config_file._read_config_snapshot") as read, patch(
                "fls_manager.routes.tasks.config_file._save_config_atomic",
            ) as save:
                missing_csrf = client.post("/task/config/config", data={"content": "draft"}, headers=headers)
                self.assertEqual(missing_csrf.status_code, 400)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post("/task/config/config", data={**form.fields, "content": "draft"}, headers=headers)
                self.assertEqual(expired.status_code, 401)
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="config-csrf")
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    forbidden = client.post("/task/config/config", query_string={"token": TOKEN}, data={**form.fields, "content": "draft"}, headers=headers)
                self.assertEqual(forbidden.status_code, 403)
                read.assert_not_called()
                save.assert_not_called()

    def test_run_and_stop_form_actions_do_not_submit_config_changes(self):
        with isolated_app() as (app, base_dir):
            target = self.prepare(base_dir)
            client = app.test_client()
            form = self.form(client)
            self.assertEqual(form.actions[0]["formaction"], "/run/config?back=/collections")
            self.assertEqual(form.actions[0]["formmethod"], "post")
            with patch("fls_manager.routes.tasks.actions.run_task_now", return_value=(False, "fixture conflict")), patch(
                "fls_manager.routes.tasks.config_file._save_config_atomic",
            ) as save:
                response = client.post(form.actions[0]["formaction"], data={**form.fields, "content": "unsaved"}, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 400)
            save.assert_not_called()
            self.assertEqual(target.read_bytes(), b"key: original\n")
            with patch("fls_manager.routes.tasks.config_file.is_running", return_value=True):
                running = self.form(client)
            self.assertEqual(running.actions[0]["formaction"], "/stop/config?back=/collections")
            with patch("fls_manager.routes.tasks.actions.stop_task_now", return_value=(True, "stopped")), patch(
                "fls_manager.routes.tasks.config_file._save_config_atomic",
            ) as save:
                stopped = client.post(running.actions[0]["formaction"], data={**running.fields, "content": "unsaved"}, headers={"X-Token": TOKEN})
            self.assertEqual(stopped.status_code, 302)
            self.assertEqual(stopped.headers["Location"], "/collections")
            save.assert_not_called()
            self.assertEqual(target.read_bytes(), b"key: original\n")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise AJAX error rendering")
    def test_ajax_renders_error_page_with_draft_and_retry_token_without_follow_up_get(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            client = app.test_client()
            form = self.form(client)
            with patch("fls_manager.routes.tasks.config_file.os.replace", side_effect=OSError("write rejected")):
                response = client.post(
                    "/task/config/config", data={**form.fields, "content": "preserved draft"},
                    headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                )
            self.assertEqual(response.status_code, 500)
            js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            handler = js[js.index("    async function handleFormErrorResponse("):js.index("    async function ajaxLoad(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let rendered = 0;
const context = {
 document: {querySelector: () => ({})},
 DOMParser: class {parseFromString() {return {querySelector: () => ({})};}},
 replaceHtmlText: async (body, url, push) => {
  assert.match(body, /preserved draft/);
  assert.match(body, /config_revision/);
  assert.match(body, /csrf_token/);
  assert.match(body, /write rejected/);
  assert.equal(push, false);
  rendered += 1;
 },
 fetch: () => {throw Error('Unexpected follow-up GET');},
};
vm.createContext(context);
vm.runInContext(input.handler, context);
(async () => {
 await context.handleFormErrorResponse({status: 500, url: '/task/config/config',
  headers: {get: () => 'text/html'}, text: async () => input.html,
 }, '/task/config/config', {}, {});
 assert.equal(rendered, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""], input=json.dumps({"handler": handler, "html": response.get_data(as_text=True)}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
