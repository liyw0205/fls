import os
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN
from tests.test_notify_action_recovery import isolated_app


class BackupOrphanStagingTests(unittest.TestCase):
    def record(self, job_id):
        return {
            "id": job_id,
            "items": ["scripts"],
            "type_text": "仅脚本",
            "running": True,
            "status": "正在压缩",
            "filename": "",
            "size": 0,
            "size_text": "-",
            "error": "",
            "created_at": "2026-11-18 00:00:00",
            "updated_at": "2026-11-18 00:00:00",
            "staged_cleanup_pending": True,
            "staged_cleanup_error": "",
        }

    def query(self, app, job_id):
        response = app.test_client().get(
            "/api/backup/job/" + job_id,
            headers={"X-Token": TOKEN},
        )
        self.assertEqual(response.status_code, 200)
        return response.json

    def test_lost_worker_becomes_unknown_and_partial_stage_is_removed(self):
        with isolated_app() as (app, _base):
            from fls_manager.routes.backup import _common as backup

            job_id = "b" * 32
            info = self.record(job_id)
            stage = backup.backup_dir() / backup._backup_staging_name(job_id)
            stage.write_bytes(b"partial archive")
            backup.BACKUP_JOBS[job_id] = info
            backup.persist_backup_job(info)

            result = self.query(app, job_id)
            self.assertEqual(result["status"], "结果未知")
            self.assertFalse(result["running"])
            self.assertFalse(result["staged_cleanup_pending"])
            self.assertFalse(stage.exists())
            persisted = backup._load_persisted_backup_job(job_id)
            self.assertEqual(persisted["status"], "结果未知")
            self.assertFalse(persisted["staged_cleanup_pending"])

    def test_active_worker_is_not_cleaned_but_lost_worker_is_recovered(self):
        with isolated_app() as (app, _base):
            from fls_manager.routes.backup import _common as backup

            job_id = "d" * 32
            info = self.record(job_id)
            stage = backup.backup_dir() / backup._backup_staging_name(job_id)
            stage.write_bytes(b"partial archive")
            backup.BACKUP_JOBS[job_id] = info
            backup.BACKUP_CANCEL_EVENTS[job_id] = threading.Event()
            backup.persist_backup_job(info)

            active = self.query(app, job_id)
            self.assertTrue(active["running"])
            self.assertTrue(stage.exists())

            backup.BACKUP_CANCEL_EVENTS.pop(job_id)
            recovered = self.query(app, job_id)
            self.assertEqual(recovered["status"], "结果未知")
            self.assertFalse(stage.exists())

    def test_cleanup_failure_is_visible_and_next_query_retries_idempotently(self):
        with isolated_app() as (app, _base):
            from fls_manager.routes.backup import _common as backup

            job_id = "e" * 32
            info = self.record(job_id)
            stage = backup.backup_dir() / backup._backup_staging_name(job_id)
            stage.write_bytes(b"partial archive")
            backup.persist_backup_job(info)
            real_unlink = os.unlink

            def deny(name, *args, **kwargs):
                if name == stage.name and kwargs.get("dir_fd") is not None:
                    raise PermissionError("fixture staging cleanup denied")
                return real_unlink(name, *args, **kwargs)

            with patch.object(backup.os, "unlink", side_effect=deny):
                failed = self.query(app, job_id)
            self.assertEqual(failed["status"], "结果未知")
            self.assertTrue(failed["staged_cleanup_pending"])
            self.assertIn("fixture staging cleanup denied", failed["error"])
            self.assertTrue(stage.exists())
            self.assertTrue(backup._load_persisted_backup_job(job_id)["staged_cleanup_pending"])

            recovered = self.query(app, job_id)
            self.assertFalse(recovered["staged_cleanup_pending"])
            self.assertIn("暂存归档清理重试成功", recovered["error"])
            self.assertFalse(stage.exists())

    def test_symlink_stage_is_not_followed_and_can_be_retried_after_removal(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.backup import _common as backup

            job_id = "f" * 32
            info = self.record(job_id)
            outside = base / "outside.txt"
            outside.write_text("preserve", encoding="utf-8")
            stage = backup.backup_dir() / backup._backup_staging_name(job_id)
            stage.symlink_to(outside)
            backup.persist_backup_job(info)

            unsafe = self.query(app, job_id)
            self.assertTrue(unsafe["staged_cleanup_pending"])
            self.assertIn("不是常规文件", unsafe["error"])
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve")
            self.assertTrue(stage.is_symlink())

            stage.unlink()
            stage.write_bytes(b"partial archive")
            recovered = self.query(app, job_id)
            self.assertFalse(recovered["staged_cleanup_pending"])
            self.assertFalse(stage.exists())


if __name__ == "__main__":
    unittest.main()
