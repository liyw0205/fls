import os
import stat
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import MultiDict
from tests.test_ui_route_components import TOKEN, isolated_app


class FileFormParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.textarea = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action", ""), "data": MultiDict()}
            self.forms.append(self.current)
        if self.current and tag == "input" and attrs.get("name"):
            self.current["data"].add(attrs["name"], attrs.get("value", ""))
        if self.current and tag == "textarea" and attrs.get("name"):
            self.textarea = attrs["name"]
            self.current["data"].add(self.textarea, "")

    def handle_data(self, data):
        if self.current and self.textarea:
            self.current["data"][self.textarea] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.textarea = None
        if tag == "form":
            self.current = None


class ScriptFileRecoveryTests(unittest.TestCase):
    def seed(self, base_dir, directory=False):
        from fls_manager.models import save_tasks
        from fls_manager.scheduler import reload_scheduler

        script = base_dir / "scripts" / "demo.py"
        if directory:
            script = base_dir / "scripts" / "bundle" / "demo.py"
            script.parent.mkdir()
        script.write_bytes(b"print('script recovery')\n")
        rel = str(script.relative_to(base_dir / "scripts"))
        save_tasks([{"id": "linked", "name": "Linked task", "command": f"task {rel}", "cron": "0 8 * * *",
                     "config_path": rel, "run_count": 7},
                    {"id": "unrelated", "name": "Other", "command": "echo untouched"}])
        reload_scheduler()
        return script

    def form(self, response, action=None):
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:300])
        forms = FileFormParser(response.get_data(as_text=True)).forms
        return next(form for form in forms if action is None or form["action"] == action)

    def rename_form(self, client, raw="demo.py", name="renamed.py", headers=None):
        headers = headers or {"X-Token": TOKEN}
        initial = self.form(client.get("/scripts/rename", query_string={"path": raw}, headers=headers))
        initial["data"]["new_name"] = name
        initial["data"]["confirm"] = "0"
        preview = client.post(initial["action"], data=initial["data"], headers=headers)
        confirmed = self.form(preview)
        confirmed["data"]["confirm"] = "1"
        return confirmed

    def test_edit_is_atomic_preserves_permissions_and_exact_bytes_and_retry_is_idempotent(self):
        with isolated_app() as (app, base_dir):
            script = self.seed(base_dir)
            script.chmod(0o750)
            before = script.stat()
            client = app.test_client()
            form = self.form(client.get("/scripts/view?path=demo.py", headers={"X-Token": TOKEN}))
            draft = "print('new')\r\n# exact text\r\n"
            form["data"]["content"] = draft
            response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(script.read_bytes(), draft.encode("utf-8"))
            self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o750)
            self.assertEqual((script.stat().st_uid, script.stat().st_gid), (before.st_uid, before.st_gid))
            saved_inode = script.stat().st_ino
            repeated = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(script.stat().st_ino, saved_inode)
            self.assertEqual(list(script.parent.glob(".fls-script-*.tmp")), [])

    def test_write_fsync_replace_and_cleanup_failures_preserve_draft_and_real_saved_status(self):
        for stage in ("fsync", "replace", "chmod", "cleanup_after", "cleanup_before"):
            with self.subTest(stage=stage), isolated_app() as (app, base_dir):
                script = self.seed(base_dir)
                client = app.test_client()
                form = self.form(client.get("/scripts/view?path=demo.py", headers={"X-Token": TOKEN}))
                draft = "</textarea><script>untrusted()</script>\n"
                form["data"]["content"] = draft
                before = script.read_bytes()
                from contextlib import ExitStack
                with ExitStack() as stack:
                    if stage in ("fsync", "replace"):
                        stack.enter_context(patch("fls_manager.script_files.os." + stage, side_effect=OSError("write rejected")))
                    elif stage == "chmod":
                        stack.enter_context(patch("pathlib.Path.chmod", side_effect=OSError("write rejected")))
                    else:
                        stack.enter_context(patch("pathlib.Path.unlink", side_effect=PermissionError("cleanup rejected")))
                        if stage == "cleanup_before":
                            stack.enter_context(patch("fls_manager.script_files.os.replace", side_effect=OSError("write rejected")))
                    response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"})
                self.assertEqual(response.status_code, 500)
                html = response.get_data(as_text=True)
                self.assertNotIn("<script>untrusted()</script>", html)
                if stage == "cleanup_after":
                    self.assertIn("文件已保存", html)
                    self.assertEqual(script.read_bytes(), draft.encode())
                else:
                    self.assertIn("write rejected", html)
                    self.assertEqual(script.read_bytes(), before)
                retry = self.form(response if response.status_code == 200 else client.get(form["action"], headers={"X-Token": TOKEN}))
                retry["data"]["content"] = "print('recovered')\n"
                recovered = client.post(retry["action"], data=retry["data"], headers={"X-Token": TOKEN})
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(script.read_bytes(), b"print('recovered')\n")

    def test_stale_editor_shows_current_content_keeps_draft_and_merges_on_retry(self):
        with isolated_app() as (app, base_dir):
            script = self.seed(base_dir)
            client = app.test_client()
            form = self.form(client.get("/scripts/view?path=demo.py", headers={"X-Token": TOKEN}))
            script.write_text("# external\n")
            form["data"]["content"] = "# local\n"
            stale = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(stale.status_code, 409)
            html = stale.get_data(as_text=True)
            self.assertIn('aria-label="当前文件内容"', html)
            self.assertIn("# external", html)
            self.assertIn("# local", html)
            retry = FileFormParser(html).forms[0]
            retry["data"]["content"] = "# external\n# local\n"
            self.assertEqual(client.post(retry["action"], data=retry["data"], headers={"X-Token": TOKEN}).status_code, 200)
            self.assertEqual(script.read_bytes(), b"# external\n# local\n")

    def test_read_failure_after_publication_reports_saved_and_keeps_draft(self):
        for failure in (PermissionError("read rejected"), FileNotFoundError("removed"), ValueError("invalid encoding")):
            with self.subTest(failure=type(failure).__name__), isolated_app() as (app, base_dir):
                from fls_manager.routes.scripts import files

                script = self.seed(base_dir)
                client = app.test_client()
                form = self.form(client.get("/scripts/view?path=demo.py", headers={"X-Token": TOKEN}))
                form["data"]["content"] = "# published draft\n"
                snapshot = files.script_file_snapshot
                calls = 0

                def read(*args):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise failure
                    return snapshot(*args)

                with patch.object(files, "script_file_snapshot", side_effect=read):
                    response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 500)
                self.assertIn("文件已保存", response.get_data(as_text=True))
                self.assertIn("# published draft", response.get_data(as_text=True))
                self.assertEqual(script.read_bytes(), b"# published draft\n")

    def test_concurrent_edit_and_create_do_not_overwrite_other_submissions(self):
        with isolated_app() as (app, base_dir):
            script = self.seed(base_dir)
            form = self.form(app.test_client().get("/scripts/view?path=demo.py", headers={"X-Token": TOKEN}))
            for create in (False, True):
                with self.subTest(create=create):
                    barrier = threading.Barrier(2)

                    def submit(content):
                        data = {"item_type": "file", "name": "concurrent.py", "content": content} if create else form["data"].copy()
                        data["content"] = content
                        barrier.wait(timeout=3)
                        return app.test_client().post("/pull/new" if create else form["action"], data=data, headers={"X-Token": TOKEN}).status_code

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        statuses = list(pool.map(submit, ("# first\n", "# second\n")))
                    self.assertEqual(sorted(statuses), [302 if create else 200, 409])
                    target = script.with_name("concurrent.py") if create else script
                    self.assertIn(target.read_bytes(), (b"# first\n", b"# second\n"))

    def test_invalid_file_paths_special_files_and_bad_encoding_are_not_read_or_overwritten(self):
        with isolated_app() as (app, base_dir):
            self.seed(base_dir)
            scripts = base_dir / "scripts"
            (scripts / "folder").mkdir()
            os.mkfifo(scripts / "fifo")
            (scripts / "binary").write_bytes(b"\xff")
            (scripts / "inside").symlink_to(scripts / "demo.py")
            (scripts / "outside").symlink_to(base_dir / "outside")
            client = app.test_client()
            for path in ("../outside", "/demo.py", "C:\\demo.py", "bad\x00path", ".", "inside", "outside", "folder", "fifo", "binary"):
                with self.subTest(path=path):
                    response = client.post("/scripts/view", query_string={"path": path}, data={"content": "unsaved draft"}, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("unsaved draft", response.get_data(as_text=True))
            self.assertEqual((scripts / "demo.py").read_bytes(), b"print('script recovery')\n")
            self.assertFalse((base_dir / "outside").exists())

    def test_creation_failure_and_illegal_type_have_zero_side_effects_then_same_request_is_idempotent(self):
        with isolated_app() as (app, base_dir):
            self.seed(base_dir)
            client = app.test_client()
            for data in ({"item_type": "invalid", "name": "created.py"}, {"name": "../outside"},
                         {"name": "created.py", "current_rel": "../outside"}, {"name": "created.py", "current_rel": "missing"}):
                with self.subTest(data=data):
                    response = client.post("/pull/new", data={"content": "draft", **data}, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 400)
            data = {"item_type": "file", "name": "created.py", "content": "print('created')\n"}
            with patch("fls_manager.script_files.os.fsync", side_effect=OSError("disk rejected")):
                failed = client.post("/pull/new", data=data, headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            target = base_dir / "scripts" / "created.py"
            self.assertFalse(target.exists())
            for _ in range(2):
                self.assertEqual(client.post("/pull/new", data=data, headers={"X-Token": TOKEN}).status_code, 302)
                self.assertEqual(target.read_bytes(), data["content"].encode())
            conflict = client.post("/pull/new", data={**data, "content": "different"}, headers={"X-Token": TOKEN})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(target.read_bytes(), data["content"].encode())

    def test_rename_updates_task_command_and_config_and_new_api_run_executes_the_file(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import get_task, load_task_history
            from fls_manager import task_runner
            from fls_manager.script_rename import get_script_rename

            source = self.seed(base_dir)
            client = app.test_client()
            form = self.rename_form(client)
            before = source.read_bytes()
            response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            destination = source.with_name("renamed.py")
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), before)
            task = get_task("linked")
            self.assertEqual(task["command"], "task renamed.py")
            self.assertEqual(task["config_path"], "renamed.py")
            self.assertEqual(task["run_count"], 7)
            self.assertEqual(get_task("unrelated")["command"], "echo untouched")
            self.assertEqual(get_script_rename(form["data"]["operation_id"])["status"], "succeeded")
            with patch("fls_manager.command.PYTHON_BIN", sys.executable):
                run = client.post("/api/task/action/run/linked", headers={"X-Token": TOKEN})
            self.assertEqual(run.status_code, 200)
            deadline = time.monotonic() + 5
            history = load_task_history()[0]
            while (task_runner.is_running("linked") or history["status"] in ("starting", "running")) and time.monotonic() < deadline:
                time.sleep(0.02)
                history = load_task_history()[0]
            self.assertFalse(task_runner.is_running("linked"))
            self.assertEqual(history["status"], "success")
            self.assertIn("script recovery", Path(history["log_file"]).read_text())

    def test_directory_rename_handles_task_lines_absolute_paths_config_and_manual_shell_confirmation(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks, save_tasks, get_task

            script = self.seed(base_dir, directory=True)
            tasks = load_tasks()
            tasks.extend([
                {"id": "absolute", "name": "Absolute", "command": f"task '{script}' argument"},
                {"id": "shell", "name": "Shell", "command": "cd bundle && python demo.py"},
            ])
            save_tasks(tasks)
            client = app.test_client()
            form = self.rename_form(client, "bundle", "renamed")
            self.assertEqual(form["data"]["command_linked"], "task renamed/demo.py")
            self.assertIn(str(script).replace("bundle", "renamed"), form["data"]["command_absolute"])
            refused = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(refused.status_code, 400)
            self.assertTrue(script.exists())
            form["data"]["command_shell"] = "cd renamed && python demo.py"
            renamed = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(renamed.status_code, 302)
            self.assertTrue((base_dir / "scripts" / "renamed" / "demo.py").exists())
            self.assertEqual(get_task("shell")["command"], "cd renamed && python demo.py")

    def test_invalid_name_selection_path_conflict_and_stale_preview_never_move_files_or_tasks(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import get_task, mutate_tasks

            source = self.seed(base_dir)
            (source.parent / "occupied.py").write_bytes(b"occupied")
            client = app.test_client()
            form = self.rename_form(client)
            before = (base_dir / "data" / "tasks.json").read_bytes()
            for data, status in (({"new_name": ".."}, 400), ({"new_name": "occupied.py"}, 409),
                                 ({"new_name": "C:bad"}, 400), ({"new_name": "bad\x00name"}, 400),
                                 ({"task_id": "missing"}, 400), ({"confirm": "invalid"}, 400),
                                 ({"command_linked": "task demo.py"}, 400), ({"config_linked": "../outside"}, 400)):
                with self.subTest(data=data):
                    changed = form["data"].copy()
                    for key, value in data.items():
                        changed[key] = value
                    response = client.post(form["action"], data=changed, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, status)
                    self.assertTrue(source.exists())
                    self.assertFalse((source.parent / "renamed.py").exists())
                    self.assertEqual((base_dir / "data" / "tasks.json").read_bytes(), before)
                    self.assertFalse((base_dir / "data" / "script_renames.json").exists())
            mutate_tasks(lambda tasks: (tasks[0].update(command="task demo.py new-argument") or True))
            stale = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(get_task("linked")["command"], "task demo.py new-argument")
            self.assertTrue(source.exists())

    def test_task_write_failure_after_move_is_visible_blocks_run_and_resumes_from_get_after_restart(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import models, script_rename
            from fls_manager.task_runner import run_task_now

            source = self.seed(base_dir)
            client = app.test_client()
            form = self.rename_form(client)
            with patch.object(models, "mutate_tasks", side_effect=OSError("task store rejected")):
                response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 503)
            self.assertFalse(source.exists())
            self.assertTrue(source.with_name("renamed.py").exists())
            self.assertEqual(models.get_task("linked")["command"], "task demo.py")
            record = script_rename.get_script_rename(form["data"]["operation_id"])
            recovery_url = "/scripts/rename-recovery/" + record["id"]
            ok, message = run_task_now("linked")
            self.assertFalse(ok)
            self.assertIn(recovery_url, message)
            blocked = client.post("/api/task/action/run/linked", headers={"X-Token": TOKEN})
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.get_json()["recovery_url"], recovery_url)
            bulk = client.post("/api/task/bulk-action", json={"action": "run", "task_ids": ["linked"]}, headers={"X-Token": TOKEN})
            self.assertEqual(bulk.status_code, 409)
            self.assertEqual(bulk.get_json()["recovery_url"], recovery_url)
            form_run = client.post("/run/linked", headers={"X-Token": TOKEN})
            self.assertEqual(form_run.status_code, 409)
            self.assertIn(f'href="{recovery_url}"', form_run.get_data(as_text=True))
            self.assertIn(recovery_url, client.get("/pull", headers={"X-Token": TOKEN}).get_data(as_text=True))
            page = client.get(recovery_url, headers={"X-Token": TOKEN})
            self.assertIn("原路径：不存在", page.get_data(as_text=True))
            self.assertIn("仍为原引用", page.get_data(as_text=True))
            self.assertEqual(client.get("/scripts/debug/renamed.py", headers={"X-Token": TOKEN}).headers["Location"], recovery_url)
            for url, data in (("/task/config/linked", {"content": "# unsaved config"}),
                              ("/scripts/view?path=renamed.py", {"content": "# unsaved file"}),
                              ("/scripts/delete/renamed.py", {}),
                              ("/pull/new", {"name": "demo.py", "content": "# unsaved new"})):
                with self.subTest(url=url):
                    guarded = client.post(url, data=data, headers={"X-Token": TOKEN})
                    self.assertEqual(guarded.status_code, 409)
                    self.assertIn(recovery_url, guarded.get_data(as_text=True))
                    self.assertFalse(source.exists())
                    self.assertEqual(source.with_name("renamed.py").read_bytes(), b"print('script recovery')\n")
            for _ in range(2):
                recovered = client.post(recovery_url, data={"action": "resume"}, headers={"X-Token": TOKEN})
                self.assertEqual(recovered.status_code, 302)
            self.assertEqual(models.get_task("linked")["command"], "task renamed.py")
            self.assertEqual(script_rename.get_script_rename(record["id"])["status"], "succeeded")

    def test_relative_shell_reference_requires_confirmation_and_replayed_rename_is_bound_to_paths(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import models, script_rename

            source = self.seed(base_dir)
            models.mutate_tasks(lambda tasks: (tasks[0].update(command="python ./demo.py") or True))
            client = app.test_client()
            form = self.rename_form(client)
            self.assertEqual(client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN}).status_code, 400)
            self.assertTrue(source.exists())
            form["data"]["command_linked"] = "python ./renamed.py"
            self.assertEqual(client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN}).status_code, 302)
            record = script_rename.get_script_rename(form["data"]["operation_id"])
            self.assertEqual(client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN}).headers["Location"], "/scripts/rename-recovery/" + record["id"])
            form["data"]["new_name"] = "other.py"
            self.assertEqual(client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN}).status_code, 409)
            self.assertFalse(source.with_name("other.py").exists())

    def test_interrupted_link_move_and_new_task_references_have_safe_recovery(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import models
            from fls_manager.task_runner import run_task_now

            source = self.seed(base_dir)
            client = app.test_client()
            form = self.rename_form(client)
            unlink = Path.unlink

            def fail_old_unlink(path, *args, **kwargs):
                if path == source:
                    raise PermissionError("old link removal rejected")
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=fail_old_unlink):
                response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            if response.status_code == 302:
                # Filesystems without hard-link support use an atomic no-replace move.
                destination = source.with_name("renamed.py")
                self.assertFalse(source.exists())
                self.assertEqual(destination.read_bytes(), b"print('script recovery')\n")
                return
            self.assertEqual(response.status_code, 503)
            destination = source.with_name("renamed.py")
            self.assertEqual(source.stat().st_ino, destination.stat().st_ino)
            models.mutate_tasks(lambda tasks: (tasks.append({"id": "new", "name": "New", "command": "task demo.py"}) or True))
            self.assertFalse(run_task_now("new")[0])
            url = "/scripts/rename-recovery/" + form["data"]["operation_id"]
            self.assertEqual(client.post(url, data={"action": "resume"}, headers={"X-Token": TOKEN}).status_code, 409)
            self.assertTrue(source.exists())
            models.mutate_tasks(lambda tasks: (tasks.pop() or True))
            self.assertEqual(client.post(url, data={"action": "resume"}, headers={"X-Token": TOKEN}).status_code, 302)
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"print('script recovery')\n")

    def test_corrupt_journal_and_symlink_source_fail_closed(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.task_runner import run_task_now

            source = self.seed(base_dir)
            client = app.test_client()
            form = self.rename_form(client)
            journal = base_dir / "data" / "script_renames.json"
            journal.write_bytes(b"not json")
            self.assertEqual(client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN}).status_code, 500)
            self.assertEqual(journal.read_bytes(), b"not json")
            self.assertTrue(source.exists())
            self.assertFalse(run_task_now("linked")[0])
            journal.unlink()
            source.with_name("alias.py").symlink_to(source)
            self.assertEqual(client.get("/scripts/rename?path=alias.py", headers={"X-Token": TOKEN}).status_code, 400)
            self.assertFalse(journal.exists())

    def test_partial_move_can_be_rolled_back_without_losing_new_task_counters(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import models, script_rename

            source = self.seed(base_dir)
            client = app.test_client()
            form = self.rename_form(client)
            with patch(
                "fls_manager.scheduler.reload_scheduler", side_effect=RuntimeError("scheduler rejected"),
            ):
                response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 503)
            self.assertFalse(source.exists())
            self.assertEqual(models.get_task("linked")["command"], "task renamed.py")
            models.mutate_tasks(lambda tasks: (tasks[0].update(run_count=11) or True))
            url = "/scripts/rename-recovery/" + form["data"]["operation_id"]
            for _ in range(2):
                self.assertEqual(client.post(url, data={"action": "rollback"}, headers={"X-Token": TOKEN}).status_code, 302)
            self.assertTrue(source.exists())
            self.assertEqual(models.get_task("linked")["command"], "task demo.py")
            self.assertEqual(models.get_task("linked")["run_count"], 11)
            self.assertEqual(client.post(url, data={"action": "resume"}, headers={"X-Token": TOKEN}).status_code, 409)

    def test_journal_write_and_move_failures_keep_diagnostics_and_idempotent_recovery(self):
        for stage in ("prepare", "move", "after_move", "terminal"):
            with self.subTest(stage=stage), isolated_app() as (app, base_dir):
                from fls_manager import script_rename

                source = self.seed(base_dir)
                client = app.test_client()
                form = self.rename_form(client)
                original_save = script_rename.save_script_rename

                def save(record):
                    failure_status = {"prepare": "prepared", "after_move": "moved", "terminal": "succeeded"}.get(stage)
                    if record["status"] == failure_status:
                        raise OSError("journal rejected")
                    return original_save(record)

                with patch.object(script_rename, "save_script_rename", side_effect=save), patch.object(
                    script_rename, "_move_no_replace", side_effect=OSError("move rejected") if stage == "move" else script_rename._move_no_replace,
                ):
                    response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 500 if stage == "prepare" else 503)
                self.assertIn("rejected", response.get_data(as_text=True))
                if stage == "prepare":
                    self.assertTrue(source.exists())
                    self.assertFalse(source.with_name("renamed.py").exists())
                    response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
                else:
                    response = client.post("/scripts/rename-recovery/" + form["data"]["operation_id"], data={"action": "resume"}, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 302)
                self.assertFalse(source.exists())
                self.assertTrue(source.with_name("renamed.py").exists())

    def test_running_tasks_and_debug_runs_prevent_rename_before_any_journal_or_file_change(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import script_rename

            source = self.seed(base_dir)
            client = app.test_client()
            form = self.rename_form(client)
            with patch("fls_manager.task_runner.is_running", return_value=True):
                response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            self.assertTrue(source.exists())
            self.assertEqual(script_rename.load_script_renames(), [])
            from fls_manager.routes.scripts import debug
            debug.SCRIPT_DEBUG_RUNNING["running"] = {"script": str(source), "running": True}
            response = client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 409)
            self.assertTrue(source.exists())
            self.assertEqual(script_rename.load_script_renames(), [])

    def test_recovery_conflicts_and_illegal_choice_never_overwrite_external_task_or_destination(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import models

            source = self.seed(base_dir)
            client = app.test_client()
            form = self.rename_form(client)
            with patch.object(models, "mutate_tasks", side_effect=OSError("store rejected")):
                self.assertEqual(client.post(form["action"], data=form["data"], headers={"X-Token": TOKEN}).status_code, 503)
            destination = source.with_name("renamed.py")
            inode = destination.stat().st_ino
            url = "/scripts/rename-recovery/" + form["data"]["operation_id"]
            before = (base_dir / "data" / "tasks.json").read_bytes()
            for action in ("invalid", ""):
                self.assertEqual(client.post(url, data={"action": action}, headers={"X-Token": TOKEN}).status_code, 400)
                self.assertEqual(destination.stat().st_ino, inode)
                self.assertEqual((base_dir / "data" / "tasks.json").read_bytes(), before)
            models.mutate_tasks(lambda tasks: (tasks[0].update(command="echo external") or True))
            conflict = client.post(url, data={"action": "resume"}, headers={"X-Token": TOKEN})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(models.get_task("linked")["command"], "echo external")
            source.write_bytes(b"external replacement")
            rollback = client.post(url, data={"action": "rollback"}, headers={"X-Token": TOKEN})
            self.assertEqual(rollback.status_code, 409)
            self.assertEqual(source.read_bytes(), b"external replacement")
            self.assertEqual(destination.stat().st_ino, inode)

    def test_permissions_csrf_expiry_and_forbidden_file_actions_have_no_side_effects(self):
        with isolated_app() as (app, base_dir):
            source = self.seed(base_dir)
            client = app.test_client()
            form = self.rename_form(client)
            urls = ("/scripts/view?path=demo.py", "/scripts/rename?path=demo.py", "/pull/new",
                    "/scripts/rename-recovery/" + form["data"]["operation_id"])
            before = source.read_bytes()
            headers = {"X-Requested-With": "FLS-Ajax"}
            for url in urls:
                with self.subTest(url=url):
                    with client.session_transaction() as session:
                        session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="script-csrf")
                    self.assertEqual(client.post(url, headers=headers).status_code, 400)
                    with client.session_transaction() as session:
                        session["token_expire_at"] = 1
                    self.assertEqual(client.post(url, data={"csrf_token": "script-csrf"}, headers=headers).status_code, 401)
                    with client.session_transaction() as session:
                        session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600, security_verified=True, csrf_token="script-csrf")
                    with patch("fls_manager.auth.query_token_enabled", return_value=False):
                        token_url = url + ("&" if "?" in url else "?") + "token=" + TOKEN
                        self.assertEqual(client.post(token_url, data={"csrf_token": "script-csrf"}, headers=headers).status_code, 403)
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse((base_dir / "data" / "script_renames.json").exists())


if __name__ == "__main__":
    unittest.main()
