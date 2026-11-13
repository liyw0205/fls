import contextlib
import io
import os
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


class BackupLimitTests(unittest.TestCase):
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
