import importlib
import json
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task
from tests.test_collection_save_recovery import CollectionFormParser


class EnvFormParser(CollectionFormParser):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == "textarea" and self.current is not None:
            self.textarea = attrs.get("name")
            self.current["fields"][self.textarea] = ""

    def handle_data(self, data):
        if getattr(self, "textarea", None) and self.current is not None:
            self.current["fields"][self.textarea] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.textarea = None
        super().handle_endtag(tag)


class GlobalEnvRecoveryTests(unittest.TestCase):
    def seed(self):
        from fls_manager import models
        models.save_global_env({"ORIGINAL": "old", "OTHER": "untouched"})
        models.save_tasks([sample_task("a", env={"IMPORTED": "source", "ORIGINAL": "from task"}),
                           sample_task("b", env={"IMPORTED": "different"})])

    def values(self, action="edit", **extra):
        from fls_manager import models
        from fls_manager.global_env import env_version
        env = models.load_global_env()
        data = {"action": action, "request_id": "a" * 32}
        if action in ("edit", "delete"):
            data.update(original_key="ORIGINAL", version=env_version(env, ["ORIGINAL"]))
        if action in ("new", "edit"):
            data.update(key="RENAMED", value="new value")
        elif action == "replace":
            data.update(env_text='REPLACED="value"', version=env_version(env))
        elif action == "import":
            data.update(items=["a::IMPORTED", "a::ORIGINAL"], version=env_version(env), overwrite=True,
                        sources={"a::IMPORTED": env_version({"IMPORTED": "source"}),
                                 "a::ORIGINAL": env_version({"ORIGINAL": "from task"})})
        return dict(data, **extra)

    def post(self, client, data):
        return client.post("/api/env/action", json=data, headers={"X-Token": TOKEN})

    def authenticate(self, client):
        with client.session_transaction() as session:
            session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                           security_verified=True, csrf_token="env-csrf")

    def test_all_actions_persist_and_restart_replay_preserves_later_values(self):
        for action in ("new", "edit", "delete", "replace", "import"):
            with self.subTest(action=action), isolated_app() as (app, base):
                from fls_manager import models, global_env
                self.seed()
                data = self.values(action)
                response = self.post(app.test_client(), data)
                self.assertEqual(response.status_code, 200)
                env = models.load_global_env()
                if action == "edit":
                    self.assertNotIn("ORIGINAL", env)
                    self.assertEqual(env["RENAMED"], "new value")
                elif action == "delete":
                    self.assertNotIn("ORIGINAL", env)
                elif action == "import":
                    self.assertEqual(env["IMPORTED"], "source")
                    self.assertEqual(env["ORIGINAL"], "from task")
                elif action == "replace":
                    self.assertEqual(env, {"REPLACED": "value"})
                else:
                    self.assertEqual(env["RENAMED"], "new value")
                receipt = (base / "data/global_env_requests" / (data["request_id"] + ".json")).read_text()
                for value in ("new value", "from task", "untouched"):
                    self.assertNotIn(value, receipt)
                later = {"ORIGINAL": "later", "RENAMED": "later", "OTHER": "later"}
                models.save_global_env(later)
                importlib.reload(global_env)
                with patch.object(models, "save_global_env") as save:
                    self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                    self.assertEqual(self.post(app.test_client(), dict(data, action="new", key="X", value="x")).status_code, 409)
                    save.assert_not_called()
                self.assertEqual(models.load_global_env(), later)

    def test_concurrent_independent_and_duplicate_writes_do_not_lose_values(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            self.seed()
            data = self.values("new")
            entered, release = threading.Event(), threading.Event()
            save = models.save_global_env
            def delayed(*args):
                entered.set()
                self.assertTrue(release.wait(5))
                return save(*args)
            with patch.object(models, "save_global_env", side_effect=delayed) as write, ThreadPoolExecutor(3) as pool:
                first = pool.submit(self.post, app.test_client(), data)
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.post, app.test_client(), data)
                third = pool.submit(self.post, app.test_client(), dict(data, request_id="b" * 32, key="CONCURRENT"))
                release.set()
                self.assertEqual([future.result(5).status_code for future in (first, second, third)], [200, 200, 200])
                self.assertEqual(write.call_count, 2)
            self.assertEqual(models.load_global_env(), {"ORIGINAL": "old", "OTHER": "untouched", "RENAMED": "new value", "CONCURRENT": "new value"})

    def test_file_publication_failure_reports_actual_state_and_retry_is_idempotent(self):
        for action in ("new", "edit", "delete", "replace", "import"):
            for published in (False, True):
                with self.subTest(action=action, published=published), isolated_app() as (app, _):
                    from fls_manager import models, paths
                    self.seed()
                    data = self.values(action)
                    before = models.load_global_env()
                    replace = Path.replace
                    def fail(path, target):
                        if target == paths.GLOBAL_ENV_FILE:
                            if published:
                                replace(path, target)
                            raise OSError("publication failed")
                        return replace(path, target)
                    with patch.object(Path, "replace", fail):
                        response = self.post(app.test_client(), data)
                    self.assertEqual(response.status_code, 503 if published else 500)
                    self.assertEqual(response.get_json()["saved"], published)
                    self.assertEqual(models.load_global_env() == before, not published)
                    self.assertEqual(self.post(app.test_client(), data).status_code, 200)

    def test_stale_edit_delete_replace_and_target_collision_have_zero_side_effects(self):
        for action in ("edit", "delete", "replace", "import", "new"):
            with self.subTest(action=action), isolated_app() as (app, base):
                from fls_manager import models
                self.seed()
                data = self.values(action, key="OTHER")
                if action not in ("new", "edit"):
                    models.save_global_env({"ORIGINAL": "later", "OTHER": "untouched"})
                before = (base / "data/global_env.json").read_bytes()
                with patch.object(models, "save_global_env") as save:
                    response = self.post(app.test_client(), data)
                    self.assertEqual(response.status_code, 409)
                    save.assert_not_called()
                self.assertEqual((base / "data/global_env.json").read_bytes(), before)
                self.assertFalse((base / "data/global_env_requests").exists())

    def test_import_missing_stale_and_duplicate_sources_reject_entire_selection(self):
        for kind in ("missing", "changed", "duplicate", "invalid"):
            with self.subTest(kind=kind), isolated_app() as (app, base):
                from fls_manager import models
                self.seed()
                data = self.values("import")
                if kind == "missing":
                    data["items"].append("missing::X")
                    data["sources"]["missing::X"] = "f" * 64
                elif kind == "changed":
                    tasks = models.load_tasks()
                    tasks[0]["env"]["IMPORTED"] = "changed"
                    models.save_tasks(tasks)
                elif kind == "duplicate":
                    data["items"].append("b::IMPORTED")
                    data.pop("sources")
                else:
                    data["items"].append("bad")
                before = (base / "data/global_env.json").read_bytes()
                with patch.object(models, "save_global_env") as save:
                    response = self.post(app.test_client(), data)
                    self.assertEqual(response.status_code, {"missing": 404, "changed": 409, "duplicate": 400, "invalid": 400}[kind])
                    save.assert_not_called()
                self.assertEqual((base / "data/global_env.json").read_bytes(), before)
                self.assertFalse((base / "data/global_env_requests").exists())

    def test_import_skip_and_env_text_roundtrip_preserve_exact_values(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            from fls_manager.utils import env_to_text, parse_env_text
            self.seed()
            response = self.post(app.test_client(), self.values("import", overwrite=False))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["skipped_count"], 1)
            self.assertEqual(models.load_global_env()["ORIGINAL"], "old")
            expected = {"TOKEN": 'x"\\y\nsecond line\t', "EMPTY": "", "SPACES": " a b "}
            text = env_to_text(expected)
            self.assertEqual(parse_env_text(text, strict=True), expected)
            response = self.post(app.test_client(), self.values("replace", request_id="b" * 32, env_text=text))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(models.load_global_env(), expected)

    def test_import_retry_without_client_source_version_keeps_original_intent(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            data = self.values("import")
            data.pop("sources")
            with patch.object(models, "save_global_env", side_effect=OSError("disk full")):
                self.assertEqual(self.post(app.test_client(), data).status_code, 500)
            tasks = models.load_tasks()
            tasks[0]["env"]["IMPORTED"] = "later source"
            models.save_tasks(tasks)
            before = (base / "data/global_env.json").read_bytes()
            with patch.object(models, "save_global_env") as save:
                result = self.post(app.test_client(), data)
                self.assertEqual(result.status_code, 409)
                save.assert_not_called()
            self.assertEqual((base / "data/global_env.json").read_bytes(), before)

    def test_invalid_values_and_malformed_text_are_not_silently_dropped(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            before = (base / "data/global_env.json").read_bytes()
            invalid = [None, [], self.values("new", key="BAD=KEY"), self.values("new", key="1BAD"),
                       self.values("new", value="nul\x00value"), self.values("new", request_id=""),
                       self.values("edit", version="invalid"), self.values("import", items=[]),
                       self.values("import", overwrite="yes"), self.values("import", sources={})]
            invalid += [self.values("replace", env_text=text) for text in ('MISSING', 'A=x\nA=y', 'A="bad', 'A="x" junk', '=value', 'A="\\u0000"')]
            with patch.object(models, "save_global_env") as save:
                for data in invalid:
                    with self.subTest(data=data):
                        response = app.test_client().post("/api/env/action", data=json.dumps(data), content_type="application/json", headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 400)
                save.assert_not_called()
            self.assertEqual((base / "data/global_env.json").read_bytes(), before)

    def test_receipt_and_interruption_recovery_do_not_overwrite_later_changes(self):
        with isolated_app() as (app, base):
            from fls_manager import models, global_env
            self.seed()
            data = self.values()
            with patch.object(models, "save_global_env", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.post(app.test_client(), data)
            importlib.reload(global_env)
            result = self.post(app.test_client(), data)
            self.assertEqual(result.status_code, 409)
            self.assertIsNone(result.get_json()["saved"])
            self.assertTrue(result.get_json()["confirm_required"])
            write = global_env.write_json
            def fail_receipt(path, record):
                if record["state"] == "completed":
                    raise OSError("receipt full")
                return write(path, record)
            with patch.object(global_env, "write_json", side_effect=fail_receipt):
                saved = self.post(app.test_client(), dict(data, confirm_retry=True))
            self.assertEqual(saved.status_code, 503)
            self.assertTrue(saved.get_json()["saved"])
            with patch.object(models, "save_global_env") as save:
                self.assertEqual(self.post(app.test_client(), data).status_code, 200)
                save.assert_not_called()
            self.assertNotIn("new value", (base / "data/global_env_requests" / (data["request_id"] + ".json")).read_text())

    def test_corrupt_data_and_claim_failure_preserve_files(self):
        with isolated_app() as (app, base):
            from fls_manager import models, global_env
            self.seed()
            data = self.values()
            before = (base / "data/global_env.json").read_bytes()
            with patch.object(models, "save_global_env") as save:
                with patch.object(global_env, "write_json", side_effect=OSError("claim full")):
                    result = self.post(app.test_client(), data)
                    self.assertEqual(result.status_code, 500)
                    self.assertFalse(result.get_json()["saved"])
                self.assertEqual((base / "data/global_env.json").read_bytes(), before)
                for raw in ("{broken", "[]", "null"):
                    with self.subTest(raw=raw):
                        (base / "data/global_env.json").write_text(raw)
                        result = self.post(app.test_client(), data)
                        self.assertEqual(result.status_code, 500)
                        self.assertIsNone(result.get_json()["saved"])
                        self.assertEqual((base / "data/global_env.json").read_text(), raw)
                save.assert_not_called()

    def test_actual_task_process_uses_saved_globals_with_task_override(self):
        with isolated_app() as (app, base):
            from fls_manager import command, models
            self.seed()
            code = "import os;print('env-proof:' + os.environ['SHARED'] + ':' + os.environ['IMPORTED'])"
            models.save_tasks([sample_task("run", command=command.command_list_to_shell([sys.executable, "-c", code]), env={"SHARED": "task"})])
            response = self.post(app.test_client(), self.values("replace", env_text='SHARED="global"\nIMPORTED="saved"'))
            self.assertEqual(response.status_code, 200)
            client = app.test_client()
            result = client.post("/api/task/action/run/run", json={"request_id": "c" * 32}, headers={"X-Token": TOKEN})
            self.assertEqual(result.status_code, 200)
            deadline = time.monotonic() + 8
            history = []
            while time.monotonic() < deadline:
                history = models.load_task_history()
                if history and history[0]["status"] not in ("starting", "running"):
                    break
                time.sleep(0.02)
            self.assertEqual(history[0]["status"], "success")
            logs = "".join(path.read_text() for path in (base / "log").glob("*.log"))
            self.assertIn("env-proof:task:saved", logs)

    def test_form_save_failure_conflict_and_permissions_preserve_draft(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            from fls_manager.routes.env import _common
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            form = EnvFormParser(client.get("/env/edit/ORIGINAL").get_data(as_text=True)).forms[0]
            form["fields"].update(key="RENAMED", value="my draft\nsecond")
            with patch.object(_common, "save_global_env", side_effect=OSError("disk full")):
                failed = client.post(form["action"], data=form["fields"], headers={"X-Requested-With": "FLS-Ajax"})
            self.assertEqual(failed.status_code, 500)
            retry = EnvFormParser(failed.get_data(as_text=True)).forms[0]
            self.assertEqual(retry["fields"]["value"], "my draft\nsecond")
            models.save_global_env({"ORIGINAL": "later", "OTHER": "untouched"})
            conflict = client.post(retry["action"], data=retry["fields"])
            self.assertEqual(conflict.status_code, 409)
            merged = EnvFormParser(conflict.get_data(as_text=True)).forms[0]
            before = (base / "data/global_env.json").read_bytes()
            with patch.object(_common, "save_global_env") as save:
                no_csrf = dict(merged["fields"])
                no_csrf.pop("csrf_token")
                self.assertEqual(client.post(merged["action"], data=no_csrf).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    self.assertEqual(client.post(merged["action"] + "?token=" + TOKEN, data=merged["fields"], headers={"X-Requested-With": "FLS-Ajax"}).status_code, 403)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post(merged["action"], data=merged["fields"], headers={"X-Requested-With": "FLS-Ajax"}).status_code, 401)
                save.assert_not_called()
            self.assertEqual((base / "data/global_env.json").read_bytes(), before)
            self.authenticate(client)
            self.assertEqual(client.post(merged["action"], data=merged["fields"]).status_code, 302)
            self.assertEqual(models.load_global_env()["RENAMED"], "my draft\nsecond")

    def test_interrupted_then_changed_reports_unknown_without_reapplying(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            data = self.values()
            with patch.object(models, "save_global_env", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.post(app.test_client(), data)
            models.save_global_env({"ORIGINAL": "later", "OTHER": "untouched"})
            before = (base / "data/global_env.json").read_bytes()
            with patch.object(models, "save_global_env") as save:
                response = self.post(app.test_client(), dict(data, confirm_retry=True))
                self.assertEqual(response.status_code, 409)
                self.assertIsNone(response.get_json()["saved"])
                self.assertTrue(response.get_json()["conflict"])
                save.assert_not_called()
            self.assertEqual((base / "data/global_env.json").read_bytes(), before)

    def test_api_csrf_expiry_and_form_newline_contract(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            client = app.test_client()
            self.authenticate(client)
            data = self.values("new")
            before = (base / "data/global_env.json").read_bytes()
            with patch.object(models, "save_global_env") as save:
                self.assertEqual(client.post("/api/env/action", json=data).status_code, 400)
                with patch("fls_manager.auth.query_token_enabled", return_value=False):
                    self.assertEqual(client.post("/api/env/action?token=" + TOKEN, json=data,
                        headers={"X-CSRF-Token": "env-csrf"}).status_code, 403)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                self.assertEqual(client.post("/api/env/action", json=data, headers={"X-CSRF-Token": "env-csrf"}).status_code, 401)
                save.assert_not_called()
            self.assertEqual((base / "data/global_env.json").read_bytes(), before)
            self.assertEqual(client.post("/api/env/action", data=dict(data, value="a\r\nb"), headers={"X-Token": TOKEN}).status_code, 200)
            self.assertEqual(models.load_global_env()["RENAMED"], "a\nb")
            self.assertEqual(self.post(client, self.values("new", request_id="b" * 32, key="JSON_VALUE", value="a\r\nb")).status_code, 200)
            self.assertEqual(models.load_global_env()["JSON_VALUE"], "a\r\nb")

    def test_deleted_edit_and_invalid_version_offer_executable_draft_recovery(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted), isolated_app() as (app, _):
                from fls_manager import models
                self.seed()
                client = app.test_client()
                self.authenticate(client)
                form = EnvFormParser(client.get("/env/edit/ORIGINAL").get_data(as_text=True)).forms[0]
                form["fields"].update(key="RECOVERED", value="draft")
                if deleted:
                    models.save_global_env({"OTHER": "untouched"})
                else:
                    form["fields"]["version"] = "invalid"
                response = client.post(form["action"], data=form["fields"])
                self.assertEqual(response.status_code, 404 if deleted else 400)
                retry = EnvFormParser(response.get_data(as_text=True)).forms[0]
                self.assertEqual(retry["fields"]["value"], "draft")
                if deleted:
                    self.assertEqual(retry["action"], "/env/new")
                else:
                    self.assertIn("合并后保存变量", response.get_data(as_text=True))
                self.assertEqual(client.post(retry["action"], data=retry["fields"]).status_code, 302)
                self.assertEqual(models.load_global_env(), {"OTHER": "untouched", "RECOVERED": "draft"})

    def test_corrupt_receipt_and_unreadable_publication_do_not_claim_success(self):
        with isolated_app() as (app, base):
            from fls_manager import models
            self.seed()
            data = self.values()
            path = base / "data/global_env_requests" / (data["request_id"] + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            before = (base / "data/global_env.json").read_bytes()
            with patch.object(models, "save_global_env") as save:
                for raw in ("null", "{}", "{bad"):
                    with self.subTest(raw=raw):
                        path.write_text(raw)
                        self.assertEqual(self.post(app.test_client(), data).status_code, 500)
                        self.assertEqual(path.read_text(), raw)
                save.assert_not_called()
            self.assertEqual((base / "data/global_env.json").read_bytes(), before)
            data["request_id"] = "b" * 32
            with patch.object(models, "load_global_env", side_effect=[models.load_global_env(), OSError("cannot confirm")]), patch.object(models, "save_global_env", side_effect=OSError("save failed")):
                result = self.post(app.test_client(), data)
                self.assertEqual(result.status_code, 500)
                self.assertIsNone(result.get_json()["saved"])
