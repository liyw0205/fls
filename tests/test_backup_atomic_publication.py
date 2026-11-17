import io
import tarfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app


class BackupAtomicPublicationTests(unittest.TestCase):
    def seed(self, base):
        from fls_manager.routes.backup import _common as backup
        script = base / "scripts/atomic.py"
        script.write_text("print('complete archive')", encoding="utf-8")
        return backup, script

    def create(self, app, request_id="a" * 32):
        return app.test_client().post("/api/backup/create", json={"request_id": request_id, "items": ["scripts"]}, headers={"X-Token": TOKEN})

    def job(self, app, request_id="a" * 32):
        response = app.test_client().get("/api/backup/job/" + request_id, headers={"X-Token": TOKEN})
        self.assertEqual(response.status_code, 200)
        return response.json

    def listed(self, app):
        return app.test_client().get("/api/backup/list", headers={"X-Token": TOKEN}).json["items"]

    def assert_archive(self, app, filename):
        response = app.test_client().get("/backup/download/" + filename, headers={"X-Token": TOKEN})
        self.assertEqual(response.status_code, 200)
        with tarfile.open(fileobj=io.BytesIO(response.data), mode="r:gz") as archive:
            self.assertEqual(archive.extractfile("scripts/atomic.py").read(), b"print('complete archive')")
            self.assertFalse(any(name.startswith("data/") for name in archive.getnames()))
        response.close()

    def test_partial_archive_is_hidden_until_completion_or_cancelled_and_restore_is_excluded(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), isolated_app() as (app, base):
                backup, script = self.seed(base)
                entered, release = threading.Event(), threading.Event()
                threads = []
                add, start = tarfile.TarFile.add, threading.Thread.start

                def pause(archive, path, *args, **kwargs):
                    result = add(archive, path, *args, **kwargs)
                    if Path(path) == script:
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError("fixture archive release timed out")
                    return result

                def capture(thread):
                    threads.append(thread)
                    return start(thread)

                with patch.object(backup, "write_dependencies_file"), patch.object(tarfile.TarFile, "add", pause), patch.object(threading.Thread, "start", capture):
                    try:
                        self.assertEqual(self.create(app).status_code, 200)
                        self.assertTrue(entered.wait(3))
                        self.assertTrue(self.job(app)["running"])
                        partials = list(backup.BACKUP_DIR.glob("*.part"))
                        self.assertEqual(len(partials), 1)
                        self.assertEqual(self.listed(app), [])
                        self.assertEqual(app.test_client().get("/backup/download/" + partials[0].name, headers={"X-Token": TOKEN}).status_code, 404)
                        self.assertFalse(backup.try_begin_restore_activity())
                        if cancel:
                            response = app.test_client().post("/api/backup/cancel/" + "a" * 32, headers={"X-Token": TOKEN})
                            self.assertTrue(response.json["accepted"])
                    finally:
                        release.set()
                        for thread in threads:
                            thread.join(5)
                            self.assertFalse(thread.is_alive())
                job = self.job(app)
                self.assertFalse(job["running"])
                self.assertEqual(job["status"], "已取消" if cancel else "已完成")
                self.assertFalse(list(backup.BACKUP_DIR.glob("*.part")))
                self.assertEqual(len(self.listed(app)), 0 if cancel else 1)
                if not cancel:
                    self.assert_archive(app, job["filename"])
                self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)
                self.assertTrue(backup.try_begin_restore_activity())
                backup.end_restore_activity()

    def test_archive_write_and_cleanup_failures_never_expose_partial_file(self):
        for cleanup_denied in (False, True):
            with self.subTest(cleanup_denied=cleanup_denied), isolated_app() as (app, base):
                backup, _ = self.seed(base)
                unlink = Path.unlink

                def deny(path, *args, **kwargs):
                    if cleanup_denied and path.suffix == ".part":
                        raise PermissionError("fixture partial cleanup denied")
                    return unlink(path, *args, **kwargs)

                with patch.object(backup, "write_dependencies_file"), patch.object(tarfile.TarFile, "add", side_effect=OSError("fixture compression fault")), patch.object(Path, "unlink", deny), patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()) as start:
                    self.assertEqual(self.create(app).status_code, 200)
                    job = self.job(app)
                    self.assertEqual(job["status"], "失败")
                    self.assertIn("fixture compression fault", job["error"])
                    if cleanup_denied:
                        self.assertIn("fixture partial cleanup denied", job["error"])
                    self.assertEqual(self.create(app).status_code, 200)
                    start.assert_called_once()
                self.assertEqual(self.listed(app), [])
                partials = list(backup.BACKUP_DIR.glob("*.part"))
                self.assertEqual(len(partials), int(cleanup_denied))
                if partials:
                    self.assertEqual(app.test_client().get("/backup/download/" + partials[0].name, headers={"X-Token": TOKEN}).status_code, 404)
                self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)

    def test_publish_failure_before_after_and_unreadable_confirmation_match_real_archive(self):
        for mode in ("before", "after", "unreadable"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                backup, _ = self.seed(base)
                link, lstat = backup.os.link, Path.lstat
                published = False

                def publish(source, target, *args, **kwargs):
                    nonlocal published
                    if Path(source).suffix == ".part":
                        if mode != "before":
                            link(source, target, *args, **kwargs)
                        published = True
                        raise OSError("fixture publication acknowledgement fault")
                    return link(source, target, *args, **kwargs)

                def check(path, *args, **kwargs):
                    if published and mode == "unreadable" and path.name.endswith(".tar.gz"):
                        raise PermissionError("fixture archive confirmation unavailable")
                    return lstat(path, *args, **kwargs)

                with patch.object(backup, "write_dependencies_file"), patch.object(backup.os, "link", side_effect=publish), patch.object(Path, "lstat", check), patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()) as start:
                    self.assertEqual(self.create(app).status_code, 200)
                    self.assertEqual(self.create(app).status_code, 200)
                    start.assert_called_once()
                job = self.job(app)
                self.assertEqual(job["status"], {"before": "失败", "after": "已完成", "unreadable": "结果未知"}[mode])
                self.assertIn("fixture publication acknowledgement fault", job["error"])
                listed = self.listed(app)
                self.assertEqual(len(listed), int(mode != "before"))
                if listed:
                    self.assert_archive(app, listed[0]["name"])
                self.assertFalse(list(backup.BACKUP_DIR.glob("*.part")))
                backup.BACKUP_JOBS.clear()
                self.assertEqual(self.job(app)["status"], job["status"])
                self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)

    def test_completed_job_confirmation_failure_is_checked_without_destroying_success(self):
        for mode in ("before", "after", "unreadable", "mismatch"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                backup, _ = self.seed(base)
                persist, load = backup.persist_backup_job, backup._load_persisted_backup_job

                def save(info):
                    if info["status"] == "已完成":
                        if mode in ("after", "unreadable"):
                            persist(info)
                        if mode == "mismatch":
                            mismatched = dict(info)
                            mismatched["filename"] = "different.tar.gz"
                            persist(mismatched)
                        raise OSError("fixture completed state acknowledgement fault")
                    return persist(info)

                def read(job_id):
                    if mode == "unreadable" and backup.BACKUP_JOBS.get(job_id, {}).get("status") == "已完成":
                        raise OSError("fixture job confirmation unreadable")
                    return load(job_id)

                with patch.object(backup, "write_dependencies_file"), patch.object(backup, "persist_backup_job", side_effect=save), patch.object(backup, "_load_persisted_backup_job", side_effect=read), patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                    self.assertEqual(self.create(app).status_code, 200)
                job = self.job(app)
                self.assertEqual(job["status"], {"before": "结果未知", "after": "已完成", "unreadable": "结果未知", "mismatch": "结果未知"}[mode])
                self.assertIn("fixture completed state acknowledgement fault", job["error"])
                listed = self.listed(app)
                self.assertEqual(len(listed), 1)
                if listed:
                    self.assert_archive(app, listed[0]["name"])
                self.assertFalse(list(backup.BACKUP_DIR.glob("*.part")))

    def test_existing_archive_collision_is_preserved_and_only_staging_is_removed(self):
        with isolated_app() as (app, base):
            backup, _ = self.seed(base)
            target = backup.backup_dir() / "existing.tar.gz"
            target.write_bytes(b"previous archive")
            with patch.object(backup, "backup_safe_file", return_value=target), patch.object(backup, "write_dependencies_file"), patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                self.assertEqual(self.create(app).status_code, 200)
            self.assertEqual(self.job(app)["status"], "失败")
            self.assertIn("未覆盖原归档", self.job(app)["error"])
            self.assertEqual(target.read_bytes(), b"previous archive")
            self.assertFalse(list(backup.BACKUP_DIR.glob("*.part")))

    def test_archive_collision_created_during_publication_is_never_overwritten(self):
        with isolated_app() as (app, base):
            backup, _ = self.seed(base)
            target = backup.backup_dir() / "race.tar.gz"
            link = backup.os.link

            def race(source, destination, *args, **kwargs):
                target.write_bytes(b"concurrent archive")
                return link(source, destination, *args, **kwargs)

            with patch.object(backup, "backup_safe_file", return_value=target), patch.object(backup, "write_dependencies_file"), patch.object(backup.os, "link", side_effect=race), patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                self.assertEqual(self.create(app).status_code, 200)
            self.assertEqual(self.job(app)["status"], "失败")
            self.assertIn("未覆盖原归档", self.job(app)["error"])
            self.assertEqual(target.read_bytes(), b"concurrent archive")
            self.assertFalse(list(backup.BACKUP_DIR.glob("*.part")))

    def test_cancel_cleanup_failure_is_visible_but_partial_cannot_be_downloaded(self):
        with isolated_app() as (app, base):
            backup, _ = self.seed(base)
            add, unlink = tarfile.TarFile.add, Path.unlink

            def cancel_after_add(archive, path, *args, **kwargs):
                result = add(archive, path, *args, **kwargs)
                backup.BACKUP_CANCEL_EVENTS["a" * 32].set()
                return result

            def deny(path, *args, **kwargs):
                if path.suffix == ".part":
                    raise PermissionError("fixture cancel cleanup denied")
                return unlink(path, *args, **kwargs)

            with patch.object(backup, "write_dependencies_file"), patch.object(tarfile.TarFile, "add", cancel_after_add), patch.object(Path, "unlink", deny), patch.object(threading.Thread, "start", autospec=True, side_effect=lambda thread: thread.run()):
                self.assertEqual(self.create(app).status_code, 200)
            job = self.job(app)
            self.assertEqual(job["status"], "已取消")
            self.assertIn("fixture cancel cleanup denied", job["error"])
            self.assertEqual(self.listed(app), [])
            partials = list(backup.BACKUP_DIR.glob("*.part"))
            self.assertEqual(len(partials), 1)
            self.assertEqual(app.test_client().get("/backup/download/" + partials[0].name, headers={"X-Token": TOKEN}).status_code, 404)
            self.assertEqual(backup.ACTIVE_BACKUP_WORKERS, 0)
