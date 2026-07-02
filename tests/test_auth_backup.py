import contextlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def isolated_fls_env(token=None):
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-test-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_SECRET_KEY"] = "unit-test-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        if token is None:
            os.environ.pop("FLS_TOKEN", None)
        else:
            os.environ["FLS_TOKEN"] = token

        purge_fls_modules()

        try:
            yield Path(temp_dir)
        finally:
            shutdown_scheduler()
            purge_fls_modules()

            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def shutdown_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None

    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


def load_app():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from fls_manager.app import create_app

    return create_app()


def add_tar_file(tar, name, content=b"data"):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    tar.addfile(info, io.BytesIO(content))


def add_tar_member(tar, name, member_type, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.linkname = linkname
    info.size = 0
    tar.addfile(info)


class AuthFlowTests(unittest.TestCase):
    def test_api_requires_setup_when_token_missing(self):
        with isolated_fls_env(token=None):
            client = load_app().test_client()
            response = client.get("/api/status")

            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.get_json()["ok"])
            self.assertIn("Token", response.get_json()["msg"])

    def test_page_redirects_to_setup_when_token_missing(self):
        with isolated_fls_env(token=None):
            client = load_app().test_client()
            response = client.get("/tasks")

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/setup")

    def test_api_accepts_x_token_header(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            response = client.get("/api/status", headers={"X-Token": "unit-token"})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), [])

    def test_api_rejects_wrong_query_token(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            response = client.get("/api/status?token=wrong")

            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.get_json()["ok"])

    def test_query_token_sets_session_and_redirects_to_clean_url(self):
        with isolated_fls_env(token="unit-token"):
            client = load_app().test_client()
            response = client.get("/tasks?token=unit-token&q=abc")

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/tasks?q=abc")
            self.assertIn("session=", response.headers.get("Set-Cookie", ""))


class BackupSafetyTests(unittest.TestCase):
    def assert_tar_rejected(self, member_builder):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_tar

            with tempfile.TemporaryDirectory(prefix="fls-tar-bad-") as dest:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as tar:
                    member_builder(tar)

                data.seek(0)
                with tarfile.open(fileobj=data, mode="r:") as tar:
                    with self.assertRaises(RuntimeError):
                        safe_extract_tar(tar, dest)

    def test_backup_safe_file_stays_inside_backup_dir(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import backup_safe_file

            target = backup_safe_file("../../demo.tar.gz")
            self.assertEqual(target.name, "demo.tar.gz")
            self.assertIn("backups", target.parts)

    def test_safe_extract_zip_accepts_regular_paths(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_zip

            with tempfile.TemporaryDirectory(prefix="fls-zip-ok-") as dest:
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as zf:
                    zf.writestr("data/config.json", "{}")

                data.seek(0)
                with zipfile.ZipFile(data, "r") as zf:
                    safe_extract_zip(zf, dest)

                self.assertTrue((Path(dest) / "data" / "config.json").exists())

    def test_safe_extract_zip_rejects_traversal(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_zip

            with tempfile.TemporaryDirectory(prefix="fls-zip-bad-") as dest:
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as zf:
                    zf.writestr("../evil.txt", "bad")

                data.seek(0)
                with zipfile.ZipFile(data, "r") as zf:
                    with self.assertRaises(RuntimeError):
                        safe_extract_zip(zf, dest)

    def test_safe_extract_zip_rejects_absolute_and_backslash_paths(self):
        cases = [
            "/tmp/evil.txt",
            "C:/evil.txt",
            "..\\evil.txt",
        ]

        for name in cases:
            with self.subTest(name=name):
                with isolated_fls_env(token="unit-token"):
                    from fls_manager.routes.backup._common import safe_extract_zip

                    with tempfile.TemporaryDirectory(prefix="fls-zip-bad-") as dest:
                        data = io.BytesIO()
                        with zipfile.ZipFile(data, "w") as zf:
                            zf.writestr(name, "bad")

                        data.seek(0)
                        with zipfile.ZipFile(data, "r") as zf:
                            with self.assertRaises(RuntimeError):
                                safe_extract_zip(zf, dest)

    def test_safe_extract_tar_accepts_regular_paths(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_tar

            with tempfile.TemporaryDirectory(prefix="fls-tar-ok-") as dest:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as tar:
                    add_tar_file(tar, "scripts/demo.py", b"print('ok')")

                data.seek(0)
                with tarfile.open(fileobj=data, mode="r:") as tar:
                    safe_extract_tar(tar, dest)

                self.assertTrue((Path(dest) / "scripts" / "demo.py").exists())

    def test_safe_extract_tar_does_not_emit_deprecation_warning(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_tar

            with tempfile.TemporaryDirectory(prefix="fls-tar-warn-") as dest:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as tar:
                    add_tar_file(tar, "scripts/demo.py", b"print('ok')")

                data.seek(0)
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", DeprecationWarning)

                    with tarfile.open(fileobj=data, mode="r:") as tar:
                        safe_extract_tar(tar, dest)

                deprecations = [
                    item
                    for item in caught
                    if issubclass(item.category, DeprecationWarning)
                ]
                self.assertEqual(deprecations, [])

    def test_safe_extract_tar_rejects_traversal(self):
        with isolated_fls_env(token="unit-token"):
            from fls_manager.routes.backup._common import safe_extract_tar

            with tempfile.TemporaryDirectory(prefix="fls-tar-bad-") as dest:
                data = io.BytesIO()
                with tarfile.open(fileobj=data, mode="w") as tar:
                    add_tar_file(tar, "../evil.txt", b"bad")

                data.seek(0)
                with tarfile.open(fileobj=data, mode="r:") as tar:
                    with self.assertRaises(RuntimeError):
                        safe_extract_tar(tar, dest)

    def test_safe_extract_tar_rejects_absolute_paths(self):
        cases = [
            "/tmp/evil.txt",
            "C:/evil.txt",
        ]

        for name in cases:
            with self.subTest(name=name):
                self.assert_tar_rejected(
                    lambda tar, member_name=name: add_tar_file(tar, member_name, b"bad")
                )

    def test_safe_extract_tar_rejects_symlink_and_hardlink_members(self):
        cases = [
            ("data/link", tarfile.SYMTYPE, "/tmp/evil.txt"),
            ("data/link", tarfile.SYMTYPE, "../../evil.txt"),
            ("data/hard", tarfile.LNKTYPE, "/tmp/evil.txt"),
            ("data/hard", tarfile.LNKTYPE, "../../evil.txt"),
        ]

        for name, member_type, linkname in cases:
            with self.subTest(name=name, member_type=member_type, linkname=linkname):
                self.assert_tar_rejected(
                    lambda tar, n=name, t=member_type, l=linkname: add_tar_member(
                        tar,
                        n,
                        t,
                        l,
                    )
                )

    def test_safe_extract_tar_rejects_special_members(self):
        cases = [
            ("data/pipe", tarfile.FIFOTYPE),
            ("data/chardev", tarfile.CHRTYPE),
            ("data/blockdev", tarfile.BLKTYPE),
        ]

        for name, member_type in cases:
            with self.subTest(name=name, member_type=member_type):
                self.assert_tar_rejected(
                    lambda tar, n=name, t=member_type: add_tar_member(tar, n, t)
                )


if __name__ == "__main__":
    unittest.main()
