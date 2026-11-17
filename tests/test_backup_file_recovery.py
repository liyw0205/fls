import os
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app
from tests import test_proxy_action_recovery as proxy_tests


class BackupFileRecoveryTests(unittest.TestCase):
    authenticate = proxy_tests.ProxyActionRecoveryTests.authenticate
    form = proxy_tests.ProxyActionRecoveryTests.form

    def seed(self, name="original.tar.gz"):
        from fls_manager.routes.backup import _common as backup
        target = backup.backup_dir() / name
        target.write_bytes(b"original archive")
        return target, {"filename": name, "version": backup.backup_file_version(target.stat())}

    def post(self, client, values, **kwargs):
        return client.post("/api/backup/delete", json=values, headers={"X-Token": TOKEN}, **kwargs)

    def test_delete_and_lost_response_replay_do_not_delete_replacement(self):
        with isolated_app() as (app, _):
            target, values = self.seed()
            client = app.test_client()
            self.assertTrue(self.post(client, values).json["deleted"])
            self.assertFalse(target.exists())
            self.assertTrue(self.post(client, values).json["deleted"])
            target.write_bytes(b"new archive")
            self.assertEqual(self.post(client, values).status_code, 409)
            self.assertEqual(target.read_bytes(), b"new archive")
            listed = client.get("/api/backup/list", headers={"X-Token": TOKEN}).json["items"]
            self.assertNotEqual(listed[0]["version"], values["version"])
            values["version"] = listed[0]["version"]
            response = client.post("/api/backup/delete", data=values, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(target.exists())

    def test_stale_metadata_same_size_and_mtime_is_rejected(self):
        with isolated_app() as (app, _):
            target, values = self.seed()
            original = target.stat()
            replacement = target.with_suffix(".replacement")
            replacement.write_bytes(b"replacement data")
            os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
            replacement.replace(target)
            self.assertEqual(self.post(app.test_client(), values).status_code, 409)
            self.assertEqual(target.read_bytes(), b"replacement data")

    def test_invalid_selection_has_zero_side_effects(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            target, values = self.seed()
            invalid = [None, [], {}, dict(values, version=None), dict(values, version="bad")]
            invalid += [dict(values, filename=name) for name in (
                "../original.tar.gz", "nested/original.tar.gz", "C:\\original.tar.gz",
                "/original.tar.gz", "C:original.tar.gz", "original.tar.gz\x00", "original.tar.gz\n", " original.tar.gz", "ignored.zip",
            )]
            with patch.object(backup.os, "unlink") as unlink:
                for data in invalid:
                    with self.subTest(data=data):
                        self.assertEqual(self.post(app.test_client(), data).status_code, 400)
                for field in ("filename", "version"):
                    data = MultiDict(values)
                    data.add(field, values[field])
                    self.assertEqual(app.test_client().post("/api/backup/delete", data=data, headers={"X-Token": TOKEN}).status_code, 400)
                unlink.assert_not_called()
            self.assertEqual(target.read_bytes(), b"original archive")

    def test_delete_faults_match_actual_file_and_allow_safe_retry(self):
        for mode in ("before", "after", "replacement", "unknown"):
            with self.subTest(mode=mode), isolated_app() as (app, _):
                from fls_manager.routes.backup import _common as backup
                target, values = self.seed()
                unlink, stat = backup.os.unlink, backup.os.stat
                failed = False

                def fail_unlink(name, **kwargs):
                    nonlocal failed
                    if mode in ("after", "replacement"):
                        unlink(name, **kwargs)
                    if mode == "replacement":
                        target.write_bytes(b"later replacement")
                    failed = True
                    raise PermissionError("fixture delete fault")

                def confirm(name, *args, **kwargs):
                    if mode == "unknown" and failed and name == values["filename"]:
                        raise PermissionError("fixture status unavailable")
                    return stat(name, *args, **kwargs)

                with patch.object(backup.os, "unlink", side_effect=fail_unlink), patch.object(backup.os, "stat", side_effect=confirm):
                    response = self.post(app.test_client(), values)
                self.assertEqual(response.status_code, {"before": 500, "after": 200, "replacement": 503, "unknown": 503}[mode])
                self.assertIs(response.json["deleted"], {"before": False, "after": True, "replacement": None, "unknown": None}[mode])
                self.assertEqual(self.post(app.test_client(), values).status_code, 409 if mode == "replacement" else 200)
                if mode == "replacement":
                    self.assertEqual(target.read_bytes(), b"later replacement")
                else:
                    self.assertFalse(target.exists())

    def test_links_special_files_and_directory_links_are_rejected(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import _common as backup
            directory = backup.backup_dir()
            outside = base / "outside.tar.gz"
            outside.write_bytes(b"outside archive")
            (directory / "linked.tar.gz").symlink_to(outside)
            os.mkfifo(directory / "pipe.tar.gz")
            client = app.test_client()
            for filename in ("linked.tar.gz", "pipe.tar.gz"):
                with self.subTest(filename=filename):
                    self.assertEqual(client.get("/backup/download/" + filename, headers={"X-Token": TOKEN}).status_code, 404)
                    self.assertEqual(self.post(client, {"filename": filename, "version": "a" * 64}).status_code, 400)
            self.assertEqual(client.get("/api/backup/list", headers={"X-Token": TOKEN}).json["items"], [])
            directory.rename(directory.with_name("previous-backups"))
            directory.symlink_to(base, target_is_directory=True)
            self.assertEqual(client.get("/api/backup/list", headers={"X-Token": TOKEN}).status_code, 500)
            self.assertEqual(client.get("/backup/download/outside.tar.gz", headers={"X-Token": TOKEN}).status_code, 404)
            self.assertEqual(self.post(client, {"filename": outside.name, "version": "a" * 64}).status_code, 400)
            self.assertEqual(outside.read_bytes(), b"outside archive")

    def test_download_retains_open_file_when_path_changes(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import restore
            target, _ = self.seed('quoted\'"<archive>.tar.gz')
            outside = base / "outside.tar.gz"
            outside.write_bytes(b"private outside data")
            send = restore.send_file

            def replace_after_open(source, **kwargs):
                target.unlink()
                target.symlink_to(outside)
                return send(source, **kwargs)

            with patch.object(restore, "send_file", side_effect=replace_after_open):
                response = app.test_client().get("/backup/download/" + target.name, headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"original archive")
            self.assertEqual(response.content_length, len(response.data))
            response.close()

    def test_auth_csrf_methods_and_html_recovery(self):
        with isolated_app() as (app, _):
            target, values = self.seed()
            client = app.test_client()
            self.assertEqual(client.post("/api/backup/delete", json=values).status_code, 400)
            self.assertEqual(client.get("/backup/download/" + target.name).status_code, 302)
            self.authenticate(client)
            for path in ("/api/backup/delete", "/backup/delete"):
                self.assertEqual(client.post(path, data=values).status_code, 400)
                self.assertEqual(client.get(path).status_code, 405)
            with patch("fls_manager.auth.query_token_enabled", return_value=False):
                self.assertEqual(client.post("/api/backup/delete?token=" + TOKEN, json=values, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 403)
            with client.session_transaction() as session:
                session["token_expire_at"] = 1
            self.assertEqual(client.post("/api/backup/delete", json=values, headers={"X-CSRF-Token": "proxy-csrf"}).status_code, 401)
            self.authenticate(client)
            self.assertTrue(target.exists())
            form = self.form(client.get("/backup"), "/backup/delete")
            self.assertEqual(form["fields"]["filename"], target.name)
            response = client.post("/backup/delete", data=form["fields"])
            self.assertEqual(response.status_code, 200)
            self.assertIn("查询实际备份列表", response.get_data(as_text=True))
            self.assertFalse(target.exists())

    def test_list_storage_failure_is_visible_and_does_not_hide_files(self):
        with isolated_app() as (app, _):
            from fls_manager.routes.backup import _common as backup
            target, _ = self.seed()
            with patch.object(backup.os, "listdir", side_effect=PermissionError("fixture list fault")):
                response = app.test_client().get("/api/backup/list", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 500)
                self.assertFalse(response.json["ok"])
                page = app.test_client().get("/backup", headers={"X-Token": TOKEN})
                self.assertEqual(page.status_code, 200)
                self.assertIn("备份列表无法读取", page.get_data(as_text=True))
            self.assertTrue(target.exists())

    def test_portable_file_access_preserves_versions_and_rejects_links(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import _common as backup
            target, values = self.seed()
            client = app.test_client()
            with patch.object(backup, "BACKUP_DIRECTORY_FD_SUPPORTED", False):
                listed = client.get("/api/backup/list", headers={"X-Token": TOKEN}).json["items"]
                self.assertEqual(listed[0]["version"], values["version"])
                response = client.get("/backup/download/" + target.name, headers={"X-Token": TOKEN})
                self.assertEqual(response.data, target.read_bytes())
                response.close()
                self.assertEqual(self.post(client, values).status_code, 200)
                outside = base / "outside.tar.gz"
                outside.write_bytes(b"outside data")
                target.symlink_to(outside)
                self.assertEqual(client.get("/backup/download/" + target.name, headers={"X-Token": TOKEN}).status_code, 404)
                self.assertEqual(self.post(client, values).status_code, 400)
                self.assertTrue(target.is_symlink())
                self.assertEqual(outside.read_bytes(), b"outside data")
