import io
import tarfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


def archive_bytes(kind, entries):
    data = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(data, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
    else:
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for name, content in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return data.getvalue()


class ScriptImportRecoveryTests(unittest.TestCase):
    def post_archive(self, client, kind, raw, save_as="bundle", current_rel=""):
        suffix = ".zip" if kind == "zip" else ".tar.gz"
        return client.post(
            "/pull/import",
            data={
                "file": (io.BytesIO(raw), "import" + suffix),
                "save_as": save_as,
                "current_rel": current_rel,
            },
            headers={"X-Token": TOKEN},
            content_type="multipart/form-data",
        )

    def test_extract_failure_leaves_live_scripts_unchanged_for_zip_and_tar(self):
        for kind, extractor in (("zip", "safe_extract_zip"), ("tar", "safe_extract_tar")):
            with self.subTest(kind=kind), isolated_app() as (app, base):
                scripts = base / "scripts"
                scripts.mkdir(parents=True, exist_ok=True)
                (scripts / "existing.py").write_text("old\n", encoding="utf-8")
                raw = archive_bytes(kind, {"new.py": b"new\n"})

                def fail_after_partial(_archive, staged):
                    Path(staged, "partial.py").write_text("partial\n", encoding="utf-8")
                    raise RuntimeError("fixture extraction interrupted")

                with patch("fls_manager.routes.scripts.pull." + extractor, side_effect=fail_after_partial):
                    response = self.post_archive(app.test_client(), kind, raw, current_rel="new-parent")

                self.assertEqual(response.status_code, 200)
                self.assertIn("导入失败", response.get_data(as_text=True))
                self.assertEqual((scripts / "existing.py").read_text(encoding="utf-8"), "old\n")
                self.assertFalse((scripts / "new-parent").exists())
                self.assertEqual(list(scripts.glob(".fls-import-*")), [])

    def test_existing_file_collision_is_rejected_without_publishing_new_files(self):
        with isolated_app() as (app, base):
            bundle = base / "scripts" / "bundle"
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "same.py").write_text("old\n", encoding="utf-8")
            raw = archive_bytes("zip", {"same.py": b"new\n", "new.py": b"new file\n"})

            response = self.post_archive(app.test_client(), "zip", raw)

            self.assertEqual(response.status_code, 200)
            self.assertIn("目标已存在", response.get_data(as_text=True))
            self.assertEqual((bundle / "same.py").read_text(encoding="utf-8"), "old\n")
            self.assertFalse((bundle / "new.py").exists())

    def test_success_publishes_archive_contents_after_staging(self):
        with isolated_app() as (app, base):
            raw = archive_bytes("tar", {"nested/task.py": b"print('ok')\n"})

            response = self.post_archive(app.test_client(), "tar", raw)

            self.assertEqual(response.status_code, 200)
            self.assertIn("压缩包导入成功", response.get_data(as_text=True))
            self.assertEqual(
                (base / "scripts" / "bundle" / "nested" / "task.py").read_text(encoding="utf-8"),
                "print('ok')\n",
            )
            self.assertEqual(list((base / "scripts").glob(".fls-import-*")), [])

    def test_publish_failure_rolls_back_new_files_and_directories(self):
        with isolated_app() as (app, base):
            raw = archive_bytes("zip", {"new.py": b"new\n", "nested/task.py": b"task\n"})
            from fls_manager.routes.scripts import pull

            real_replace = pull.os.replace
            calls = 0

            def fail_after_first(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("fixture publish interrupted")
                return real_replace(source, destination)

            with patch.object(pull.os, "replace", side_effect=fail_after_first):
                response = self.post_archive(app.test_client(), "zip", raw, save_as="")

            scripts = base / "scripts"
            self.assertEqual(response.status_code, 200)
            self.assertIn("导入失败", response.get_data(as_text=True))
            self.assertFalse((scripts / "new.py").exists())
            self.assertFalse((scripts / "nested").exists())
            self.assertEqual(list(scripts.glob(".fls-import-*")), [])

    def test_archive_symlink_is_rejected_before_live_publish(self):
        with isolated_app() as (app, base):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                info = tarfile.TarInfo("link.py")
                info.type = tarfile.SYMTYPE
                info.linkname = "../outside.py"
                archive.addfile(info)

            response = self.post_archive(app.test_client(), "tar", raw.getvalue())

            self.assertEqual(response.status_code, 200)
            self.assertIn("链接或特殊文件", response.get_data(as_text=True))
            self.assertFalse((base / "scripts" / "bundle").exists())
            self.assertFalse((base / "outside.py").exists())


if __name__ == "__main__":
    unittest.main()
