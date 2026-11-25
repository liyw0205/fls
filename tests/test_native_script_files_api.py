import os
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class NativeScriptFilesApiTests(unittest.TestCase):
    def test_tree_is_bounded_searchable_and_does_not_follow_special_entries(self):
        with isolated_app() as (app, base_dir):
            scripts = base_dir / "scripts"
            (scripts / "folder").mkdir()
            (scripts / "alpha.py").write_text("print('alpha')\n", encoding="utf-8")
            (scripts / "beta.py").write_text("print('beta')\n", encoding="utf-8")
            (scripts / "link.py").symlink_to(scripts / "alpha.py")
            (scripts / "fifo").symlink_to(scripts / "alpha.py")
            client = app.test_client()

            unauthorized = client.get("/api/scripts/tree")
            first = client.get(
                "/api/scripts/tree?page=1&limit=2",
                headers={"X-Token": TOKEN},
            )
            second = client.get(
                "/api/scripts/tree?page=2&limit=2",
                headers={"X-Token": TOKEN},
            )
            filtered = client.get(
                "/api/scripts/tree?q=alpha",
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.headers.get("Cache-Control"), "no-store")
            self.assertEqual(first.json["total"], 3)
            self.assertEqual(first.json["pages"], 2)
            self.assertEqual(first.json["items"][0]["path"], "folder")
            self.assertEqual(second.json["items"][0]["path"], "beta.py")
            self.assertEqual([item["name"] for item in filtered.json["items"]], ["alpha.py"])
            self.assertNotIn(str(scripts), first.get_data(as_text=True))
            self.assertNotIn("link.py", first.get_data(as_text=True) + second.get_data(as_text=True))
            self.assertEqual(
                client.get("/api/scripts/tree?limit=101", headers={"X-Token": TOKEN}).status_code,
                400,
            )

    def test_tree_does_not_create_missing_directories_and_rejects_unsafe_paths(self):
        with isolated_app() as (app, base_dir):
            client = app.test_client()
            missing = client.get(
                "/api/scripts/tree?path=missing",
                headers={"X-Token": TOKEN},
            )
            traversal = client.get(
                "/api/scripts/tree?path=../outside",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(traversal.status_code, 400)
            self.assertFalse((base_dir / "scripts" / "missing").exists())

    def test_native_session_csrf_create_read_and_revision_protected_edit(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.native_sessions import create_native_session

            scripts = base_dir / "scripts"
            (scripts / "demo.py").write_text("print('before')\n", encoding="utf-8")
            client = app.test_client()
            unauthenticated = client.get("/api/scripts/tree")
            self.assertEqual(unauthenticated.status_code, 401)

            with app.app_context():
                session_id, csrf_token, _max_age = create_native_session(
                    TOKEN, remember=True, verified=True,
                )
            client.set_cookie("fls_native_session", session_id)
            headers = {"X-CSRF-Token": csrf_token}
            current = client.get(
                "/api/scripts/file?path=demo.py",
                headers={"X-Requested-With": "FLS-APP"},
            )
            self.assertEqual(current.status_code, 200)
            self.assertEqual(current.json["file"]["content"], "print('before')\n")
            self.assertNotIn(str(scripts), current.get_data(as_text=True))

            bad_csrf = client.put(
                "/api/scripts/file",
                json={
                    "path": "demo.py",
                    "content": "print('rejected')\n",
                    "revision": current.json["file"]["revision"],
                },
                headers={"X-CSRF-Token": "incorrect"},
            )
            self.assertEqual(bad_csrf.status_code, 400)
            self.assertEqual((scripts / "demo.py").read_text(encoding="utf-8"), "print('before')\n")

            created = client.post(
                "/api/scripts/file",
                json={"path": "folder/new.py", "kind": "file", "content": "print('new')\n"},
                headers=headers,
            )
            self.assertEqual(created.status_code, 404)
            self.assertFalse((scripts / "folder").exists())

            created_dir = client.post(
                "/api/scripts/file",
                json={"path": "folder", "kind": "directory"},
                headers=headers,
            )
            self.assertEqual(created_dir.status_code, 201)
            created_file = client.post(
                "/api/scripts/file",
                json={"path": "folder/new.py", "kind": "file", "content": "print('new')\n"},
                headers=headers,
            )
            self.assertEqual(created_file.status_code, 201)
            self.assertEqual((scripts / "folder" / "new.py").read_text(encoding="utf-8"), "print('new')\n")

            (scripts / "demo.py").write_text("print('external')\n", encoding="utf-8")
            stale = client.put(
                "/api/scripts/file",
                json={
                    "path": "demo.py",
                    "content": "print('stale')\n",
                    "revision": current.json["file"]["revision"],
                },
                headers=headers,
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json["code"], "revision_conflict")
            self.assertEqual(stale.json["current"]["content"], "print('external')\n")
            self.assertEqual((scripts / "demo.py").read_text(encoding="utf-8"), "print('external')\n")

            updated = client.put(
                "/api/scripts/file",
                json={
                    "path": "demo.py",
                    "content": "print('after')\n",
                    "revision": stale.json["current"]["revision"],
                },
                headers=headers,
            )
            self.assertEqual(updated.status_code, 200)
            self.assertTrue(updated.json["changed"])
            self.assertEqual(updated.json["file"]["content"], "print('after')\n")
            self.assertEqual((scripts / "demo.py").read_text(encoding="utf-8"), "print('after')\n")

    def test_invalid_text_paths_conflicts_and_size_limits_do_not_mutate_files(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.routes.scripts import native_files

            scripts = base_dir / "scripts"
            (scripts / "bad.txt").write_bytes(b"\xff")
            (scripts / "link.py").symlink_to(scripts / "bad.txt")
            client = app.test_client()
            headers = {"X-Token": TOKEN}

            invalid_text = client.get("/api/scripts/file?path=bad.txt", headers=headers)
            unsafe = client.get("/api/scripts/file?path=link.py", headers=headers)
            oversized_path = client.get("/api/scripts/file?path=../outside", headers=headers)
            self.assertEqual(invalid_text.status_code, 415)
            self.assertEqual(unsafe.status_code, 400)
            self.assertEqual(oversized_path.status_code, 400)

            with patch.object(native_files, "SCRIPT_FILE_MAX_BYTES", 8):
                (scripts / "large-read.py").write_text("123456789", encoding="utf-8")
                too_large_read = client.get(
                    "/api/scripts/file?path=large-read.py",
                    headers=headers,
                )
                too_large = client.post(
                    "/api/scripts/file",
                    json={"path": "large.py", "kind": "file", "content": "123456789"},
                    headers=headers,
                )
                too_large_request = client.put(
                    "/api/scripts/file",
                    json={"path": "bad.txt", "content": "123456789", "revision": "a" * 64},
                    headers=headers,
                )
            self.assertEqual(too_large_read.status_code, 413)
            self.assertEqual(too_large.status_code, 413)
            self.assertEqual(too_large_request.status_code, 413)
            self.assertFalse((scripts / "large.py").exists())
            self.assertEqual((scripts / "bad.txt").read_bytes(), b"\xff")

    def test_create_falls_back_without_hard_links_and_never_replaces_a_racing_file(self):
        from fls_manager import script_files

        with isolated_app() as (app, base_dir):
            client = app.test_client()
            target = base_dir / "scripts" / "created.py"
            with patch.object(script_files.os, "link", None, create=True):
                response = client.post(
                    "/api/scripts/file",
                    json={"path": "created.py", "kind": "file", "content": "print('created')\n"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(target.read_text(encoding="utf-8"), "print('created')\n")
            self.assertEqual(list(target.parent.glob(".fls-script-*.tmp")), [])

        with isolated_app() as (app, base_dir):
            client = app.test_client()
            target = base_dir / "scripts" / "racing.py"
            real_open = os.open

            def compete_for_destination(path, flags, mode=0o777, *args, **kwargs):
                if os.fspath(path) == os.fspath(target):
                    fd = real_open(path, flags, mode, *args, **kwargs)
                    os.write(fd, b"winner\n")
                    os.close(fd)
                    return real_open(path, flags, mode, *args, **kwargs)
                return real_open(path, flags, mode, *args, **kwargs)

            with patch.object(script_files.os, "link", None, create=True), patch.object(
                script_files.os, "open", side_effect=compete_for_destination,
            ):
                response = client.post(
                    "/api/scripts/file",
                    json={"path": "racing.py", "kind": "file", "content": "loser\n"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(target.read_bytes(), b"winner\n")
            self.assertEqual(list(target.parent.glob(".fls-script-*.tmp")), [])

    def test_fallback_replace_failure_removes_only_its_reservation(self):
        from fls_manager import script_files

        with isolated_app() as (app, base_dir):
            client = app.test_client()
            target = base_dir / "scripts" / "failed.py"
            with patch.object(script_files.os, "link", None, create=True), patch.object(
                script_files.os, "replace", side_effect=OSError("replace rejected"),
            ):
                response = client.post(
                    "/api/scripts/file",
                    json={"path": "failed.py", "kind": "file", "content": "draft\n"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 500)
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob(".fls-script-*.tmp")), [])

    def test_native_rename_preview_commit_replay_and_status(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import save_tasks
            from fls_manager.native_sessions import create_native_session

            scripts = base_dir / "scripts"
            (scripts / "demo.py").write_text("print('demo')\n", encoding="utf-8")
            save_tasks([{
                "id": "linked",
                "name": "Linked task",
                "command": "task demo.py",
                "config_path": "demo.py",
            }])
            with app.app_context():
                session_id, csrf_token, _max_age = create_native_session(
                    TOKEN, remember=True, verified=True,
                )
            client = app.test_client()
            client.set_cookie("fls_native_session", session_id)
            headers = {"X-CSRF-Token": csrf_token}

            preview = client.get(
                "/api/scripts/rename/preview",
                query_string={"path": "demo.py", "new_name": "renamed.py"},
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(preview.status_code, 200, preview.json)
            self.assertEqual(preview.json["preview"]["destination"], "renamed.py")
            reference = preview.json["preview"]["references"][0]
            self.assertEqual(reference["id"], "linked")
            self.assertEqual(reference["after"]["command"], "task renamed.py")
            self.assertNotIn(str(scripts), preview.get_data(as_text=True))

            body = {
                "path": "demo.py",
                "new_name": "renamed.py",
                "revision": preview.json["preview"]["revision"],
                "operation_id": "a" * 32,
                "references": [{
                    "task_id": "linked",
                    "command": reference["after"]["command"],
                    "config_path": reference["after"]["config_path"],
                }],
            }
            missing_csrf = client.post("/api/scripts/rename", json=body)
            self.assertEqual(missing_csrf.status_code, 400)
            self.assertTrue((scripts / "demo.py").exists())

            renamed = client.post("/api/scripts/rename", json=body, headers=headers)
            self.assertEqual(renamed.status_code, 200, renamed.json)
            self.assertFalse((scripts / "demo.py").exists())
            self.assertTrue((scripts / "renamed.py").exists())
            self.assertEqual(renamed.json["operation"]["status"], "succeeded")
            self.assertEqual(renamed.json["operation"]["actual"]["tasks"], [
                {"task_id": "linked", "state": "updated"},
            ])
            replayed = client.post("/api/scripts/rename", json=body, headers=headers)
            self.assertEqual(replayed.status_code, 200)
            self.assertTrue(replayed.json["replayed"])
            status = client.get(f"/api/scripts/rename/{'a' * 32}")
            self.assertEqual(status.status_code, 200)
            self.assertTrue(status.json["operation"]["finished"])

    def test_native_rename_revision_conflict_and_recovery_after_scheduler_failure(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.native_sessions import create_native_session

            scripts = base_dir / "scripts"
            (scripts / "demo.py").write_text("print('demo')\n", encoding="utf-8")
            with app.app_context():
                session_id, csrf_token, _max_age = create_native_session(
                    TOKEN, remember=True, verified=True,
                )
            client = app.test_client()
            client.set_cookie("fls_native_session", session_id)
            headers = {"X-CSRF-Token": csrf_token}
            preview = client.get(
                "/api/scripts/rename/preview?path=demo.py&new_name=renamed.py",
                headers={"X-Token": TOKEN},
            ).json["preview"]
            (scripts / "demo.py").write_text("print('external edit')\n", encoding="utf-8")
            stale = client.post("/api/scripts/rename", json={
                "path": "demo.py",
                "new_name": "renamed.py",
                "revision": preview["revision"],
                "operation_id": "b" * 32,
                "references": [],
            }, headers=headers)
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(stale.json["code"], "rename_revision_conflict")
            self.assertTrue((scripts / "demo.py").exists())

            current = stale.json["preview"]
            with patch("fls_manager.scheduler.reload_scheduler", side_effect=RuntimeError("offline")):
                uncertain = client.post("/api/scripts/rename", json={
                    "path": "demo.py",
                    "new_name": "renamed.py",
                    "revision": current["revision"],
                    "operation_id": "c" * 32,
                    "references": [],
                }, headers=headers)
            self.assertEqual(uncertain.status_code, 503)
            self.assertEqual(uncertain.json["code"], "rename_recovery_required")
            self.assertEqual(uncertain.json["operation"]["status"], "tasks_saved")
            self.assertTrue((scripts / "renamed.py").exists())
            self.assertFalse((scripts / "demo.py").exists())

            recovered = client.post(
                f"/api/scripts/rename/{'c' * 32}/recovery",
                json={"action": "resume"},
                headers=headers,
            )
            self.assertEqual(recovered.status_code, 200, recovered.json)
            self.assertEqual(recovered.json["operation"]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
