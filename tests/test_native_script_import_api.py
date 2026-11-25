import io
import tarfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


def zip_bytes(entries):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return data.getvalue()


def tar_bytes(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return data.getvalue()


class NativeScriptImportApiTests(unittest.TestCase):
    def _submit(self, client, body, filename, path, request_id="a" * 32):
        return client.post(
            "/api/scripts/import",
            data={
                "file": (io.BytesIO(body), filename),
                "path": path,
                "request_id": request_id,
            },
            headers={"X-Token": TOKEN},
            content_type="multipart/form-data",
        )

    def _wait(self, client, job_id, expected, timeout=3):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/scripts/import/{job_id}", headers={"X-Token": TOKEN})
            latest = response.json["job"]
            if latest["status"] in expected:
                return latest
            time.sleep(0.01)
        self.fail(f"import job did not reach {expected}: {latest}")

    def test_file_upload_is_idempotent_and_does_not_overwrite(self):
        with isolated_app() as (app, base):
            client = app.test_client()
            raw = b"print('uploaded')\n"
            submitted = self._submit(client, raw, "demo.py", "demo.py")
            self.assertEqual(submitted.status_code, 202)
            job_id = submitted.json["job"]["id"]
            complete = self._wait(client, job_id, {"complete"})
            replay = self._submit(client, raw, "demo.py", "demo.py")
            conflicting_replay = self._submit(client, b"different", "demo.py", "demo.py")

            self.assertEqual(complete["kind"], "file")
            self.assertEqual(complete["path"], "demo.py")
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["job"]["replayed"])
            self.assertEqual(conflicting_replay.status_code, 409)
            self.assertEqual(conflicting_replay.json["code"], "request_id_conflict")
            target = base / "scripts" / "demo.py"
            self.assertEqual(target.read_bytes(), raw)
            collision = self._submit(client, b"replace", "demo.py", "demo.py", "b" * 32)
            self.assertEqual(collision.status_code, 409)
            self.assertEqual(collision.json["code"], "target_exists")
            self.assertEqual(target.read_bytes(), raw)
            self.assertEqual(list((base / "scripts").glob(".fls-import-*.stage")), [])

    def test_file_publish_falls_back_when_no_replace_rename_and_hard_links_are_unavailable(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_import as native_import

            client = app.test_client()
            with patch.object(native_import, "_renameat2_no_replace", return_value=False), patch.object(
                native_import.os, "link", None, create=True,
            ):
                submitted = self._submit(client, b"safe publish", "demo.py", "demo.py")
                complete = self._wait(client, submitted.json["job"]["id"], {"complete"})

            self.assertEqual(complete["status"], "complete")
            self.assertEqual((base / "scripts" / "demo.py").read_bytes(), b"safe publish")
            self.assertEqual(list((base / "scripts").glob(".fls-import-*.stage")), [])

    def test_rejects_windows_unsafe_archive_names(self):
        import fls_manager.routes.scripts.native_import as native_import

        for name in ("folder/file:stream.py", "CON.txt", "trailing.", "bad?.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                native_import._safe_archive_name(name)

    def test_archive_upload_publishes_only_a_valid_staged_tree(self):
        with isolated_app() as (app, base):
            client = app.test_client()
            raw = zip_bytes({"nested/task.py": b"print('ok')\n", "README.md": b"bundle\n"})
            submitted = self._submit(client, raw, "bundle.zip", "bundle")
            self.assertEqual(submitted.status_code, 202)
            complete = self._wait(client, submitted.json["job"]["id"], {"complete"})

            self.assertEqual(complete["kind"], "zip")
            self.assertEqual(complete["progress"]["total_files"], 2)
            self.assertEqual(
                (base / "scripts" / "bundle" / "nested" / "task.py").read_bytes(),
                b"print('ok')\n",
            )
            self.assertEqual((base / "scripts" / "bundle" / "README.md").read_bytes(), b"bundle\n")
            self.assertEqual(list((base / "scripts").glob(".fls-import-*.stage")), [])

    def test_rejects_archive_traversal_and_existing_destination(self):
        with isolated_app() as (app, base):
            client = app.test_client()
            traversal = self._submit(client, zip_bytes({"../outside.py": b"bad"}), "bad.zip", "bundle")
            failed = self._wait(client, traversal.json["job"]["id"], {"failed"})
            self.assertEqual(failed["error_code"], "import_failed")
            self.assertFalse((base / "outside.py").exists())
            self.assertFalse((base / "scripts" / "bundle").exists())

            (base / "scripts" / "existing").mkdir()
            existing = self._submit(client, zip_bytes({"ok.py": b"ok"}), "existing.zip", "existing", "e" * 32)
            self.assertEqual(existing.status_code, 409)
            self.assertEqual(existing.json["code"], "target_exists")
            self.assertFalse((base / "scripts" / "existing" / "ok.py").exists())

    def test_rejects_tar_links_before_publishing(self):
        with isolated_app() as (app, base):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                link = tarfile.TarInfo("link.py")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../outside.py"
                archive.addfile(link)
            client = app.test_client()
            submitted = self._submit(client, raw.getvalue(), "bad.tar.gz", "bundle")
            failed = self._wait(client, submitted.json["job"]["id"], {"failed"})

            self.assertEqual(failed["error_code"], "import_failed")
            self.assertFalse((base / "outside.py").exists())
            self.assertFalse((base / "scripts" / "bundle").exists())

    def test_upload_limit_and_csrf_are_enforced(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_import as native_import

            client = app.test_client()
            headers = {"X-Token": TOKEN}
            missing_csrf = client.post(
                "/api/scripts/import",
                data={"file": (io.BytesIO(b"x"), "x.py"), "path": "x.py", "request_id": "c" * 32},
                content_type="multipart/form-data",
            )
            self.assertEqual(missing_csrf.status_code, 400)

            with patch.object(native_import, "SCRIPT_IMPORT_MAX_BYTES", 4):
                too_large = self._submit(client, b"12345", "large.py", "large.py", "d" * 32)
            self.assertEqual(too_large.status_code, 413)
            self.assertFalse((base / "scripts" / "large.py").exists())
            self.assertEqual(list((base / "data").glob(".script-import-*.upload")), [])

            with patch.object(native_import, "SCRIPT_IMPORT_MAX_REQUEST_BYTES", 32):
                oversized_request = self._submit(client, b"small", "request.py", "request.py", "9" * 32)
            self.assertEqual(oversized_request.status_code, 413)
            self.assertEqual(oversized_request.json["code"], "too_large")
            self.assertFalse((base / "scripts" / "request.py").exists())

    def test_tar_upload_uses_safe_staging_and_reports_completion(self):
        with isolated_app() as (app, base):
            client = app.test_client()
            raw = tar_bytes({"task.py": b"print('tar')\n"})
            submitted = self._submit(client, raw, "bundle.tar.gz", "tar-bundle")
            self.assertEqual(submitted.status_code, 202)
            complete = self._wait(client, submitted.json["job"]["id"], {"complete"})
            self.assertEqual(complete["kind"], "tar.gz")
            self.assertEqual((base / "scripts" / "tar-bundle" / "task.py").read_bytes(), b"print('tar')\n")

    def test_expanded_archive_limit_fails_without_publishing(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_import as native_import

            client = app.test_client()
            with patch.object(native_import, "SCRIPT_IMPORT_MAX_EXPANDED_BYTES", 4):
                submitted = self._submit(client, zip_bytes({"large.py": b"12345"}), "large.zip", "large")
                failed = self._wait(client, submitted.json["job"]["id"], {"failed"})

            self.assertEqual(failed["error_code"], "too_large")
            self.assertFalse((base / "scripts" / "large").exists())
            self.assertEqual(list((base / "scripts").glob(".fls-import-*.stage")), [])

    def test_archive_import_can_be_cancelled_before_publication(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_import as native_import

            entered = threading.Event()
            release = threading.Event()

            def pause_extraction(_source, destination, cancel, _progress):
                (destination / "partial.py").write_bytes(b"partial")
                entered.set()
                release.wait(2)
                native_import._check_cancel(cancel)

            client = app.test_client()
            with patch.object(native_import, "_extract_zip", side_effect=pause_extraction):
                submitted = self._submit(client, zip_bytes({"file.py": b"body"}), "cancel.zip", "cancelled")
                self.assertTrue(entered.wait(2))
                cancel = client.post(
                    f"/api/scripts/import/{submitted.json['job']['id']}/cancel",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(cancel.status_code, 202)
                release.set()
                result = self._wait(client, submitted.json["job"]["id"], {"cancelled"})

            self.assertEqual(result["status"], "cancelled")
            self.assertFalse((base / "scripts" / "cancelled").exists())
            self.assertEqual(list((base / "scripts").glob(".fls-import-*.stage")), [])

    def test_restart_marks_active_job_unknown_and_cleans_private_staging(self):
        with isolated_app() as (app, base):
            import json
            import fls_manager.routes.scripts.native_import as native_import

            job_id = "f" * 32
            request_id = "e" * 32
            upload = base / "data" / f".script-import-{job_id}.upload"
            stage = base / "scripts" / f".fls-import-{job_id}.stage"
            upload.write_bytes(b"private upload")
            stage.write_bytes(b"partial staged file")
            native_import.SCRIPT_IMPORT_JOBS_FILE.write_text(
                json.dumps([{
                    "id": job_id,
                    "request_id": request_id,
                    "fingerprint": "a" * 64,
                    "kind": "file",
                    "path": "after-restart.py",
                    "status": "processing",
                    "phase": "copying",
                    "uploaded_bytes": 15,
                    "processed_bytes": 4,
                    "total_bytes": 15,
                    "created_at": "2026-11-26 00:00:00",
                    "updated_at": "2026-11-26 00:00:01",
                }]),
                encoding="utf-8",
            )
            native_import.SCRIPT_IMPORT_INITIALIZED = False
            native_import.SCRIPT_IMPORT_JOBS.clear()

            history = app.test_client().get("/api/scripts/import-history", headers={"X-Token": TOKEN})

            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json["items"][0]["status"], "unknown")
            self.assertEqual(history.json["items"][0]["error_code"], "worker_interrupted")
            self.assertFalse(upload.exists())
            self.assertFalse(stage.exists())


if __name__ == "__main__":
    unittest.main()
