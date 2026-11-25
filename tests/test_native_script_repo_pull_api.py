import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from tests.test_ui_route_components import TOKEN, isolated_app
except ModuleNotFoundError:
    from test_ui_route_components import TOKEN, isolated_app


class NativeScriptRepoPullApiTests(unittest.TestCase):
    def _source_repo(self, base):
        source = base / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "FLS Test"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "fls-test@example.invalid"], check=True)
        (source / "README.md").write_text("fixture repository\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-qm", "fixture"], check=True)
        return source / ".git"

    def _clone_command(self, source_git, *, delay=0.4):
        script = (
            "import pathlib,shutil,sys,time\n"
            "source=pathlib.Path(sys.argv[1]); target=pathlib.Path(sys.argv[2])\n"
            "target.mkdir(parents=True); shutil.copytree(source,target/'.git')\n"
            "(target/'README.md').write_text('fixture repository\\n')\n"
            "sys.stderr.write('Enumerating objects: 1, done.\\r'); sys.stderr.flush()\n"
            "sys.stderr.write('Receiving objects: 50% (1/2), 1 KiB | 1 KiB/s\\r'); sys.stderr.flush()\n"
            "time.sleep(float(sys.argv[3]))\n"
            "sys.stderr.write('Resolving deltas: 100% (1/1)\\n'); sys.stderr.flush()\n"
        )

        def build(_url, target, _proxy_id, *, progress=False):
            return [sys.executable, "-c", script, str(source_git), str(target), str(delay)]

        return build

    def _submit(self, client, request_id="a" * 32, **overrides):
        data = {
            "request_id": request_id,
            "url": "https://example.invalid/repo.git",
            "path": "repo",
        }
        data.update(overrides)
        return client.post("/api/scripts/repo-pull", json=data, headers={"X-Token": TOKEN})

    def _wait(self, client, job_id, expected, timeout=4):
        deadline = time.monotonic() + timeout
        latest = None
        phases = []
        progress_rows = []
        while time.monotonic() < deadline:
            response = client.get(f"/api/scripts/repo-pull/{job_id}", headers={"X-Token": TOKEN})
            latest = response.json["job"]
            phases.append(latest["progress"]["phase"])
            progress_rows.append(latest["progress"])
            if latest["status"] in expected:
                return latest, phases, progress_rows
            time.sleep(0.01)
        self.fail(f"repository job did not reach {expected}: {latest}")

    def test_clone_reports_git_phases_publishes_atomically_and_replays_idempotently(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_repo_pull as native_repo_pull

            source_git = self._source_repo(base)
            client = app.test_client()
            with patch.object(native_repo_pull, "build_repo_pull_command", side_effect=self._clone_command(source_git)):
                accepted = self._submit(client)
                self.assertEqual(accepted.status_code, 202)
                job_id = accepted.json["job"]["id"]
                complete, phases, progress_rows = self._wait(client, job_id, {"complete"})
                replay = self._submit(client)

            self.assertIn("receiving", phases)
            self.assertIn("resolving", phases)
            receiving = next(row for row in progress_rows if row["phase"] == "receiving")
            self.assertEqual(receiving["label"], "接收对象")
            self.assertEqual((receiving["current"], receiving["total"]), (1, 2))
            self.assertNotIn("percent", receiving)
            self.assertEqual(complete["kind"], "repo")
            self.assertEqual(complete["path"], "repo")
            self.assertNotIn("percent", complete["progress"])
            self.assertEqual((base / "scripts" / "repo" / "README.md").read_text(encoding="utf-8"), "fixture repository\n")
            self.assertTrue((base / "scripts" / "repo" / ".git").is_dir())
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json["job"]["id"], job_id)
            self.assertTrue(replay.json["job"]["replayed"])
            persisted = (base / "data" / "script_repo_pull_jobs.json").read_text(encoding="utf-8")
            self.assertNotIn("https://", persisted)
            self.assertNotIn(str(base), persisted)
            self.assertEqual(list((base / "scripts").glob(".fls-repo-*.tmp")), [])

    def test_cancel_terminates_clone_and_removes_staging_tree(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_repo_pull as native_repo_pull

            source_git = self._source_repo(base)
            client = app.test_client()
            with patch.object(
                native_repo_pull,
                "build_repo_pull_command",
                side_effect=self._clone_command(source_git, delay=5),
            ):
                accepted = self._submit(client)
                job_id = accepted.json["job"]["id"]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    current = client.get(
                        f"/api/scripts/repo-pull/{job_id}", headers={"X-Token": TOKEN},
                    ).json["job"]
                    if current["progress"]["phase"] == "receiving":
                        break
                    time.sleep(0.01)
                self.assertEqual(current["progress"]["phase"], "receiving")
                cancelled = client.post(
                    f"/api/scripts/repo-pull/{job_id}/cancel", headers={"X-Token": TOKEN},
                )
                self.assertEqual(cancelled.status_code, 202)
                result, _phases, _progress_rows = self._wait(client, job_id, {"cancelled"})

            self.assertEqual(result["status"], "cancelled")
            self.assertFalse((base / "scripts" / "repo").exists())
            self.assertEqual(list((base / "scripts").glob(".fls-repo-*.tmp")), [])

    def test_url_path_collision_and_history_are_bounded(self):
        with isolated_app() as (app, base):
            client = app.test_client()
            invalid_url = self._submit(client, request_id="b" * 32, url="https://example.invalid/repo.git?token=secret")
            invalid_path = self._submit(client, request_id="c" * 32, path="../outside")
            existing = base / "scripts" / "existing"
            existing.mkdir()
            collision = self._submit(client, request_id="d" * 32, path="existing")
            bad_limit = client.get("/api/scripts/repo-pull-history?limit=101", headers={"X-Token": TOKEN})

            self.assertEqual(invalid_url.json["code"], "invalid_url")
            self.assertEqual(invalid_path.json["code"], "invalid_path")
            self.assertEqual(collision.json["code"], "target_exists")
            self.assertEqual(bad_limit.json["code"], "invalid_limit")
            self.assertFalse((base / "outside").exists())

    def test_repository_publish_receipt_recovers_only_for_matching_identity_and_head(self):
        with isolated_app() as (app, base):
            import fls_manager.routes.scripts.native_repo_pull as native_repo_pull

            source_git = self._source_repo(base)
            target = base / "scripts" / "repo"
            target.mkdir()
            shutil.copytree(source_git, target / ".git")
            (target / "README.md").write_text("fixture repository\n", encoding="utf-8")
            head = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            job_id = "f" * 32
            row = {
                "id": job_id,
                "request_id": "1" * 32,
                "fingerprint": hashlib.sha256(b"repo-request").hexdigest(),
                "kind": "repo",
                "path": "repo",
                "source_host": "example.invalid",
                "status": "publishing",
                "phase": "publishing",
                "current": None,
                "total": None,
                "target_dev": target.stat().st_dev,
                "target_ino": target.stat().st_ino,
                "head": head,
                "error_code": "",
                "created_at": "2026-11-25 00:00:00",
                "updated_at": "2026-11-25 00:00:00",
            }
            state_file = base / "data" / "script_repo_pull_jobs.json"
            state_file.write_text(json.dumps([row]), encoding="utf-8")
            native_repo_pull.SCRIPT_REPO_PULL_INITIALIZED = False
            native_repo_pull.SCRIPT_REPO_PULL_JOBS.clear()
            client = app.test_client()
            recovered = client.get(f"/api/scripts/repo-pull/{job_id}", headers={"X-Token": TOKEN})

            self.assertEqual(recovered.json["job"]["status"], "complete")
            row["target_ino"] += 1
            state_file.write_text(json.dumps([row]), encoding="utf-8")
            native_repo_pull.SCRIPT_REPO_PULL_INITIALIZED = False
            native_repo_pull.SCRIPT_REPO_PULL_JOBS.clear()
            unknown = client.get(f"/api/scripts/repo-pull/{job_id}", headers={"X-Token": TOKEN})

            self.assertEqual(unknown.json["job"]["status"], "unknown")
            self.assertEqual(unknown.json["job"]["error_code"], "worker_interrupted")


if __name__ == "__main__":
    unittest.main()
