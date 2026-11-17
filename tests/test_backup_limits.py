import contextlib
import io
import os
import re
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-backup-limits-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = "backup-limit-token"
        os.environ["FLS_SECRET_KEY"] = "backup-limit-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in list(sys.modules):
            if name == "fls_manager" or name.startswith("fls_manager."):
                sys.modules.pop(name, None)

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        try:
            from fls_manager.app import create_app

            yield create_app(), Path(temp_dir)
        finally:
            state = sys.modules.get("fls_manager.state")
            scheduler = getattr(state, "scheduler", None) if state else None
            if scheduler is not None:
                with contextlib.suppress(Exception):
                    scheduler.shutdown(wait=False)
            for name in list(sys.modules):
                if name == "fls_manager" or name.startswith("fls_manager."):
                    sys.modules.pop(name, None)
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def make_archive(kind, members):
    stream = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(stream, "w") as archive:
            for name, content in members:
                archive.writestr(name, content)
    else:
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, content in members:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return stream.getvalue()


class BackupLimitTests(unittest.TestCase):
    def test_archive_limit_failure_closes_restore_request_without_changing_live_data(self):
        cases = (
            ("api", "zip", "entries", "a" * 32),
            ("api", "tar", "size", "b" * 32),
            ("form", "zip", "size", "c" * 32),
            ("form", "tar", "entries", "d" * 32),
        )

        for endpoint, archive_kind, limit_kind, request_id in cases:
            with self.subTest(
                endpoint=endpoint,
                archive_kind=archive_kind,
                limit_kind=limit_kind,
            ):
                with isolated_app() as (app, base_dir):
                    if limit_kind == "entries":
                        members = (("data/a.json", b"a"), ("data/b.json", b"b"))
                    else:
                        members = (("data/a.json", b"12345"),)
                    archive_data = make_archive(archive_kind, members)
                    data_dir = base_dir / "data"
                    data_dir.mkdir(parents=True, exist_ok=True)
                    marker = data_dir / "restore-marker.txt"
                    marker.write_text("keep", encoding="utf-8")

                    client = app.test_client()
                    csrf_token = None
                    if endpoint == "form":
                        with patch("fls_manager.app.dispatch_frontend_open_event"):
                            login_page = client.get("/login")
                            login_match = re.search(
                                r'name="csrf_token" value="([^"]+)"',
                                login_page.get_data(as_text=True),
                            )
                            self.assertIsNotNone(login_match)
                            client.post(
                                "/login",
                                data={
                                    "token": "backup-limit-token",
                                    "csrf_token": login_match.group(1),
                                },
                            )
                        backup_page = client.get("/backup")
                        page_match = re.search(
                            r'name="csrf_token" value="([^"]+)"',
                            backup_page.get_data(as_text=True),
                        )
                        self.assertIsNotNone(page_match)
                        csrf_token = page_match.group(1)

                    archive_limit = 1 if limit_kind == "entries" else 10000
                    extracted_limit = (
                        4 if limit_kind == "size" else 2 * 1024 * 1024 * 1024
                    )
                    headers = (
                        {"X-Token": "backup-limit-token"}
                        if endpoint == "api"
                        else {}
                    )
                    path = (
                        "/api/backup/import"
                        if endpoint == "api"
                        else "/backup/import"
                    )

                    def submit_archive():
                        form_data = {
                            "file": (
                                io.BytesIO(archive_data),
                                "backup."
                                + ("zip" if archive_kind == "zip" else "tar"),
                            ),
                            "restore_items": "data",
                            "restore_request_id": request_id,
                        }
                        if csrf_token:
                            form_data["csrf_token"] = csrf_token
                        return client.post(
                            path,
                            data=form_data,
                            headers=headers,
                            content_type="multipart/form-data",
                        )

                    with patch(
                        "fls_manager.routes.backup._common.MAX_BACKUP_ARCHIVE_ENTRIES",
                        archive_limit,
                    ), patch(
                        "fls_manager.routes.backup._common.MAX_BACKUP_EXTRACTED_BYTES",
                        extracted_limit,
                    ):
                        response = submit_archive()
                        self.assertEqual(response.status_code, 413)
                        if endpoint == "api":
                            response_payload = response.get_json()
                            self.assertEqual(response_payload["state"], "failed")
                            self.assertEqual(response_payload["request_id"], request_id)
                            self.assertTrue(response_payload["live_state_unchanged"])
                        else:
                            self.assertIn("备份导入失败", response.get_data(as_text=True))
                        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                        self.assertFalse((data_dir / "a.json").exists())

                        status_response = client.get(
                            "/api/backup/restore/" + request_id,
                            headers=headers,
                        )
                        self.assertEqual(status_response.status_code, 500)
                        self.assertEqual(status_response.get_json()["state"], "failed")
                        self.assertFalse(status_response.get_json()["restored"])

                        retry_response = submit_archive()
                        self.assertEqual(retry_response.status_code, 413)
                        status_response = client.get(
                            "/api/backup/restore/" + request_id,
                            headers=headers,
                        )
                        self.assertEqual(status_response.get_json()["state"], "failed")
                        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_upload_limit_rejects_before_leaving_partial_file(self):
        with isolated_app():
            from fls_manager.routes.backup._common import (
                BackupLimitExceeded,
                save_backup_upload,
            )
            from werkzeug.datastructures import FileStorage

            with tempfile.TemporaryDirectory(prefix="fls-backup-upload-limit-") as temp_dir:
                target = Path(temp_dir) / "backup.zip"
                with patch("fls_manager.routes.backup._common.MAX_BACKUP_UPLOAD_BYTES", 4):
                    with self.assertRaises(BackupLimitExceeded):
                        save_backup_upload(FileStorage(io.BytesIO(b"12345")), target)
                self.assertFalse(target.exists())

    def test_tar_extract_limits_total_uncompressed_size(self):
        with isolated_app():
            from fls_manager.routes.backup._common import (
                BackupLimitExceeded,
                safe_extract_tar,
            )

            archive_data = io.BytesIO()
            with tarfile.open(fileobj=archive_data, mode="w") as archive:
                info = tarfile.TarInfo("data/config.json")
                info.size = 5
                archive.addfile(info, io.BytesIO(b"12345"))
            archive_data.seek(0)

            with tempfile.TemporaryDirectory(prefix="fls-backup-tar-limit-") as temp_dir:
                with patch("fls_manager.routes.backup._common.MAX_BACKUP_EXTRACTED_BYTES", 4):
                    with tarfile.open(fileobj=archive_data, mode="r:") as archive:
                        with self.assertRaises(BackupLimitExceeded):
                            safe_extract_tar(archive, temp_dir)
                self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_tar_extract_limits_member_count(self):
        with isolated_app():
            from fls_manager.routes.backup._common import (
                BackupLimitExceeded,
                safe_extract_tar,
            )

            archive_data = io.BytesIO()
            with tarfile.open(fileobj=archive_data, mode="w") as archive:
                for name in ("data/a.json", "data/b.json"):
                    info = tarfile.TarInfo(name)
                    info.size = 0
                    archive.addfile(info, io.BytesIO())
            archive_data.seek(0)

            with tempfile.TemporaryDirectory(prefix="fls-backup-tar-entry-limit-") as temp_dir:
                with patch("fls_manager.routes.backup._common.MAX_BACKUP_ARCHIVE_ENTRIES", 1):
                    with tarfile.open(fileobj=archive_data, mode="r:") as archive:
                        with self.assertRaises(BackupLimitExceeded):
                            safe_extract_tar(archive, temp_dir)
                self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_zip_extract_limits_member_count(self):
        with isolated_app():
            from fls_manager.routes.backup._common import (
                BackupLimitExceeded,
                safe_extract_zip,
            )

            archive_data = io.BytesIO()
            with zipfile.ZipFile(archive_data, "w") as archive:
                archive.writestr("data/a.json", "{}")
                archive.writestr("data/b.json", "{}")
            archive_data.seek(0)

            with tempfile.TemporaryDirectory(prefix="fls-backup-zip-limit-") as temp_dir:
                with patch("fls_manager.routes.backup._common.MAX_BACKUP_ARCHIVE_ENTRIES", 1):
                    with zipfile.ZipFile(archive_data) as archive:
                        with self.assertRaises(BackupLimitExceeded):
                            safe_extract_zip(archive, temp_dir)
                self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_zip_extract_limits_total_uncompressed_size(self):
        with isolated_app():
            from fls_manager.routes.backup._common import (
                BackupLimitExceeded,
                safe_extract_zip,
            )

            archive_data = io.BytesIO()
            with zipfile.ZipFile(archive_data, "w") as archive:
                archive.writestr("data/config.json", "12345")
            archive_data.seek(0)

            with tempfile.TemporaryDirectory(prefix="fls-backup-zip-size-limit-") as temp_dir:
                with patch("fls_manager.routes.backup._common.MAX_BACKUP_EXTRACTED_BYTES", 4):
                    with zipfile.ZipFile(archive_data) as archive:
                        with self.assertRaises(BackupLimitExceeded):
                            safe_extract_zip(archive, temp_dir)
                self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_import_returns_413_when_uploaded_file_exceeds_limit(self):
        with isolated_app() as (app, _):
            with patch("fls_manager.routes.backup._common.MAX_BACKUP_UPLOAD_BYTES", 4):
                response = app.test_client().post(
                    "/backup/import",
                    data={
                        "file": (io.BytesIO(b"12345"), "backup.zip"),
                        "restore_items": "data",
                    },
                    headers={"X-Token": "backup-limit-token"},
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 413)
            self.assertIn("上传上限", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
