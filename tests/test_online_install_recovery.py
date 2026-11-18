import json
import re
import shutil
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class OnlineInstallRecoveryTests(unittest.TestCase):
    def prepare(self, base_dir, **options):
        from fls_manager.online_scripts.constants import persist_online_install
        from fls_manager.online_scripts.source import save_online_script_cache
        from fls_manager.online_scripts.tasks import online_script_task_crons

        item = {
            "id": "demo", "name": "Demo", "type": "raw",
            "link": "https://example.invalid/demo.sh", "link_name": "demo.sh",
            "task_cron": [
                {"name": "Daily", "command": "task demo.sh"},
                {"name": "Hourly", "command": "task demo.sh hourly"},
            ],
        }
        save_online_script_cache([item])
        info = {
            "id": "previous", "script_id": "demo", "script_name": "Demo",
            "log_file": str(base_dir / "log" / "previous.log"),
            "running": False, "status": "结果未知", "start_time": 1,
            "returncode": None, "error": "panel restarted",
            "proxy_id": "", "import_task": True, "enable_task": False,
            "selected_task_indexes": ["2"], "force": True,
            "selected_tasks": [online_script_task_crons(item)[1]],
        }
        info.update(options)
        persist_online_install(info)
        return item, info

    def test_existing_target_requires_confirmation_then_completes_selected_import(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.online_scripts.install import install_worker

            self.prepare(base_dir)
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("original", encoding="utf-8")
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                confirmation = client.post(
                    "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                )
                self.assertEqual(confirmation.status_code, 200)
                self.assertIn("覆盖确认", confirmation.get_data(as_text=True))
                self.assertIn('action="/online-scripts/install-retry/previous"', confirmation.get_data(as_text=True))
                start.assert_not_called()
                self.assertFalse(get_online_install_job("previous").get("retried_by"))
                self.assertEqual(target.read_text(encoding="utf-8"), "original")

            def download(**kwargs):
                self.assertTrue(kwargs["force"])
                target.write_text("replacement", encoding="utf-8")
                return target

            with patch("fls_manager.online_scripts.install.download_online_script_logged", side_effect=download), patch(
                "fls_manager.online_scripts.tasks.reload_scheduler",
            ), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
                side_effect=install_worker,
            ):
                response = client.post(
                    "/online-scripts/install-retry/previous", data={"force": "1"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 302)
            retry_id = response.headers["Location"].split("?")[0].rsplit("/", 1)[1]
            self.assertEqual(get_online_install_job(retry_id)["status"], "已完成")
            self.assertEqual(get_online_install_job(retry_id)["retry_of"], "previous")
            self.assertEqual(get_online_install_job(retry_id)["selected_tasks"][0]["name"], "Hourly")
            self.assertEqual(get_online_install_job("previous")["retried_by"], retry_id)
            self.assertEqual([task["name"] for task in load_tasks()], ["Hourly"])

    def test_consumed_retry_remains_conflict_after_child_completion_and_restart(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_RUNNING, get_online_install_job, persist_online_install,
            )

            self.prepare(base_dir)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
                self.assertEqual(first.status_code, 302)
                child_id = start.call_args.kwargs["install_id"]
                child = get_online_install_job(child_id)
                child.update(running=False, status="已完成", returncode=0)
                ONLINE_INSTALL_RUNNING[child_id].update(child)
                persist_online_install(child)
                ONLINE_INSTALL_RUNNING.clear()
                repeated = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
                self.assertEqual(repeated.status_code, 409)
                self.assertIn(f"/online-scripts/log/{child_id}", repeated.get_data(as_text=True))
                self.assertEqual(start.call_count, 1)
                payload = client.get("/api/online-scripts/log/previous", headers={"X-Token": TOKEN}).get_json()
                self.assertFalse(payload["recovery"]["available"])
                self.assertEqual(payload["recovery"]["href"], f"/online-scripts/log/{child_id}")
                page = client.get("/online-scripts/log/previous", headers={"X-Token": TOKEN}).get_data(as_text=True)
                self.assertNotIn('action="/online-scripts/install-retry/previous"', page)
                self.assertIn("查看重试记录", page)

    def test_retry_refuses_other_active_install_for_same_script(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            self.prepare(base_dir)
            ONLINE_INSTALL_RUNNING["active"] = {"id": "active", "script_id": "demo", "running": True}
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = app.test_client().post(
                    "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            self.assertIn("/online-scripts/log/active", response.get_data(as_text=True))
            start.assert_not_called()

    def test_invalid_saved_task_indexes_do_not_fall_back_to_importing_all(self):
        for indexes in (["bad"], ["0"], ["3"], [1.5], [True], [], "2"):
            with self.subTest(indexes=indexes), isolated_app() as (app, base_dir):
                self.prepare(base_dir, selected_task_indexes=indexes)
                with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                    response = app.test_client().post(
                        "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                    )
                self.assertEqual(response.status_code, 409)
                self.assertIn("/online-scripts/install-select/demo", response.get_data(as_text=True))
                start.assert_not_called()
                payload = app.test_client().get(
                    "/api/online-scripts/log/previous", headers={"X-Token": TOKEN},
                ).get_json()
                self.assertFalse(payload["recovery"]["available"])
                self.assertEqual(payload["recovery"]["href"], "/online-scripts/install-select/demo")

    def test_changed_task_source_requires_new_selection(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _info = self.prepare(base_dir)
            item["task_cron"].reverse()
            save_online_script_cache([item])
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = app.test_client().post(
                    "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                )
            self.assertEqual(response.status_code, 409)
            start.assert_not_called()
            page = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN}).get_data(as_text=True)
            self.assertIn('href="/online-scripts/install-select/demo"', page)

    def test_unknown_status_remains_visible_when_recovery_snapshot_cannot_be_saved(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir, running=True, status="运行中")
            with patch("fls_manager.online_scripts.constants.persist_online_install", side_effect=OSError("disk full")):
                payload = app.test_client().get(
                    "/api/online-scripts/log/previous", headers={"X-Token": TOKEN},
                ).get_json()
            self.assertEqual(payload["status"], "结果未知")
            self.assertFalse(payload["running"])
            self.assertIn("disk full", payload["error"])

    def test_old_unreconciled_install_does_not_replace_newest_completed_attempt(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import latest_online_install_for_script, persist_online_install

            _item, info = self.prepare(base_dir, running=True, status="运行中")
            info.update(id="newest", running=False, status="已完成", returncode=0, start_time=2,
                        updated_at="2026-11-14 00:00:00")
            persist_online_install(info)
            latest_id, latest = latest_online_install_for_script("demo")
            self.assertEqual(latest_id, "newest")
            self.assertEqual(latest["status"], "已完成")

    def test_retry_persistence_failure_preserves_original_and_allows_resubmission(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.persist_online_install", side_effect=OSError("disk full")), patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
            ) as start:
                response = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 500)
                self.assertIn("disk full", response.get_data(as_text=True))
                start.assert_not_called()
            self.assertFalse(get_online_install_job("previous").get("retried_by"))
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 302)
                start.assert_called_once()

    def test_retry_thread_start_failure_links_child_and_child_can_be_retried(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=RuntimeError("thread quota")):
                response = client.post("/online-scripts/install-retry/previous", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 500)
            child_id = get_online_install_job("previous")["retried_by"]
            self.assertEqual(get_online_install_job(child_id)["status"], "启动失败")
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                response = client.post(f"/online-scripts/install-retry/{child_id}", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 302)
            start.assert_called_once()

    def test_retry_requires_csrf_and_live_session_before_state_mutation(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="retry-csrf")
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                denied = client.post("/online-scripts/install-retry/previous")
                self.assertEqual(denied.status_code, 400)
                start.assert_not_called()
                page = client.get("/online-scripts/log/previous").get_data(as_text=True)
                self.assertIn('name="csrf_token" value="retry-csrf"', page)
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(
                    "/online-scripts/install-retry/previous",
                    headers={"X-CSRF-Token": "retry-csrf", "X-Requested-With": "FLS-Ajax"},
                )
                self.assertEqual(expired.status_code, 401)
                start.assert_not_called()
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="retry-csrf")
                accepted = client.post(
                    "/online-scripts/install-retry/previous", data={"csrf_token": "retry-csrf"},
                    headers={"X-Requested-With": "FLS-Ajax"},
                )
                self.assertEqual(accepted.status_code, 302)
                start.assert_called_once()

    def test_concurrent_retry_claim_registers_only_one_child(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            entered = threading.Event()
            release = threading.Event()
            responses = []

            def blocked(**_kwargs):
                entered.set()
                self.assertTrue(release.wait(3))

            def request_retry():
                responses.append(app.test_client().post(
                    "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                ))

            with patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=blocked) as start:
                thread = threading.Thread(target=request_retry)
                thread.start()
                try:
                    self.assertTrue(entered.wait(3))
                    duplicate = app.test_client().post(
                        "/online-scripts/install-retry/previous", headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(duplicate.status_code, 409)
                finally:
                    release.set()
                    thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                start.assert_called_once()
            self.assertEqual([response.status_code for response in responses], [302])

    def test_stable_install_request_replay_returns_original_log_without_starting_twice(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job
            from fls_manager.online_scripts.source import save_online_script_cache

            item, _ = self.prepare(base_dir)
            save_online_script_cache([item])
            request_id = "a" * 32
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": "", "import_task": "1", "task_indexes": "1"},
                    headers={"X-Token": TOKEN},
                )
                repeated = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": "", "import_task": "1", "task_indexes": "1"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 302)
            self.assertEqual(repeated.status_code, 302)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(first.headers["Location"], repeated.headers["Location"])
            self.assertEqual(get_online_install_job(request_id)["id"], request_id)

    def test_stable_install_request_option_conflict_has_zero_start_side_effect(self):
        with isolated_app() as (app, base_dir):
            item, _ = self.prepare(base_dir)
            from fls_manager.online_scripts.source import save_online_script_cache
            save_online_script_cache([item])
            request_id = "b" * 32
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": "", "import_task": "1", "task_indexes": "1"},
                    headers={"X-Token": TOKEN},
                )
                conflict = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": "", "import_task": "1", "task_indexes": "2"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 302)
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(start.call_count, 1)

    def test_stable_install_request_thread_start_failure_is_queryable_without_retrying(self):
        with isolated_app() as (app, base_dir):
            item, _ = self.prepare(base_dir)
            from fls_manager.online_scripts.source import save_online_script_cache
            from fls_manager.online_scripts.constants import get_online_install_job
            save_online_script_cache([item])
            request_id = "c" * 32
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=RuntimeError("thread unavailable")) as start:
                first = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
                repeated = client.post(
                    "/online-scripts/install/demo",
                    data={"request_id": request_id, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 500)
            self.assertEqual(repeated.status_code, 302)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(get_online_install_job(request_id)["status"], "启动失败")

    def test_api_install_assigns_id_and_replays_without_starting_twice(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            body = {"proxy_id": "", "import_task": True, "task_indexes": ["1"]}
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/api/online-scripts/install/demo", json=body,
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 200)
                payload = first.get_json()
                self.assertTrue(payload["ok"])
                self.assertRegex(payload["request_id"], r"^[a-f0-9]{32}$")
                self.assertEqual(payload["install_id"], payload["request_id"])

                repeated = client.post(
                    "/api/online-scripts/install/demo",
                    json={**body, "request_id": payload["request_id"]},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 200)
                self.assertTrue(repeated.get_json()["replayed"])
                self.assertEqual(repeated.get_json()["log_url"], payload["log_url"])
                start.assert_called_once()
            self.assertEqual(get_online_install_job(payload["install_id"])["selected_task_indexes"], ["1"])

    def test_api_install_option_conflict_and_invalid_selection_have_zero_side_effect(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            request_id = "d" * 32
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                first = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": request_id, "import_task": True, "task_indexes": ["1"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 200)
                conflict = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": request_id, "import_task": True, "task_indexes": ["2"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(conflict.status_code, 409)
                invalid_id = "e" * 32
                invalid = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": invalid_id, "import_task": True, "task_indexes": ["9"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(invalid.status_code, 400)
                excluded = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": "f" * 32, "import_task": True,
                        "select_mode": "all", "excluded_task_indexes": ["9"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(excluded.status_code, 400)
                mixed = client.post(
                    "/api/online-scripts/install/demo", json={
                        "request_id": "a" * 32, "task_indexes": ["1"],
                    }, headers={"X-Token": TOKEN},
                )
                self.assertEqual(mixed.status_code, 400)
                start.assert_called_once()
            self.assertIsNone(get_online_install_job(invalid_id))
            self.assertIsNone(get_online_install_job("f" * 32))
            self.assertIsNone(get_online_install_job("a" * 32))

    def test_api_install_existing_target_requires_confirmation_then_force_uses_same_id(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("original", encoding="utf-8")
            client = app.test_client()
            request_id = "1" * 32
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                confirmation = client.post(
                    "/api/online-scripts/install/demo", json={"request_id": request_id},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(confirmation.status_code, 409)
                self.assertTrue(confirmation.get_json()["confirm_required"])
                self.assertIsNone(get_online_install_job(request_id))
                forced = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id, "force": True},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(forced.status_code, 200)
                self.assertEqual(forced.get_json()["request_id"], request_id)
                start.assert_called_once()
            self.assertEqual(get_online_install_job(request_id)["force"], True)
            self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_api_install_start_failure_is_queryable_without_retrying(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            request_id = "2" * 32
            with patch(
                "fls_manager.routes.online_scripts.install.start_install_thread",
                side_effect=RuntimeError("thread unavailable"),
            ) as start:
                first = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 500)
                self.assertEqual(first.get_json()["status"], "启动失败")
                self.assertIn("thread unavailable", first.get_json()["msg"])
                repeated = client.post(
                    "/api/online-scripts/install/demo",
                    json={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 200)
                self.assertTrue(repeated.get_json()["replayed"])
                start.assert_called_once()
            self.assertEqual(get_online_install_job(request_id)["status"], "启动失败")

    def test_api_install_requires_csrf_and_live_session_without_mutating_state(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job

            self.prepare(base_dir)
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="api-install-csrf")
            with patch("fls_manager.routes.online_scripts.install.start_install_thread") as start:
                denied = client.post(
                    "/api/online-scripts/install/demo", json={"request_id": "3" * 32},
                )
                self.assertEqual(denied.status_code, 400)
                self.assertIn("CSRF", denied.get_json()["msg"])
                self.assertIsNone(get_online_install_job("3" * 32))
                forbidden = client.post(
                    "/api/online-scripts/install/demo?token=wrong",
                    json={"request_id": "4" * 32},
                    headers={"X-CSRF-Token": "api-install-csrf"},
                )
                self.assertEqual(forbidden.status_code, 403)
                self.assertIsNone(get_online_install_job("4" * 32))
                with client.session_transaction() as session:
                    session["token_expire_at"] = 1
                expired = client.post(
                    "/api/online-scripts/install/demo", json={"request_id": "5" * 32},
                    headers={"X-CSRF-Token": "api-install-csrf"},
                )
                self.assertEqual(expired.status_code, 401)
                start.assert_not_called()

    def test_api_task_retry_reuses_downloaded_target_without_reinstalling(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import get_online_install_job, persist_online_install
            from fls_manager.online_scripts.install import _fingerprint_path

            item, info = self.prepare(base_dir)
            info["id"] = "a" * 32
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("downloaded", encoding="utf-8")
            info.update(
                running=False,
                status="失败",
                phase="importing_tasks",
                target_path=str(target),
                target_fingerprint=_fingerprint_path(target),
                error="任务导入失败",
            )
            persist_online_install(info)
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.install.import_task_if_needed", return_value=(True, "recovered")) as importer, patch(
                "fls_manager.online_scripts.install.download_online_script_logged"
            ) as download:
                recovered = client.post(
                    "/api/online-scripts/install-task-retry/" + info["id"],
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(recovered.status_code, 200)
                self.assertTrue(recovered.get_json()["ok"])
                self.assertEqual(recovered.get_json()["phase"], "completed")
                importer.assert_called_once()
                download.assert_not_called()
                repeated = client.post(
                    "/api/online-scripts/install-task-retry/" + info["id"],
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(repeated.status_code, 409)
                importer.assert_called_once()
            self.assertEqual(get_online_install_job(info["id"])["status"], "已完成")

    def test_api_task_retry_refuses_later_target_changes_and_requires_csrf(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import persist_online_install
            from fls_manager.online_scripts.install import _fingerprint_path

            _item, info = self.prepare(base_dir)
            info["id"] = "b" * 32
            target = base_dir / "scripts" / "demo.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("downloaded", encoding="utf-8")
            info.update(running=False, status="失败", phase="importing_tasks", target_path=str(target), target_fingerprint=_fingerprint_path(target))
            persist_online_install(info)
            client = app.test_client()
            denied = client.post("/api/online-scripts/install-task-retry/" + info["id"])
            self.assertEqual(denied.status_code, 400)
            target.write_text("edited later", encoding="utf-8")
            with patch("fls_manager.routes.online_scripts.install.import_task_if_needed") as importer:
                changed = client.post(
                    "/api/online-scripts/install-task-retry/" + info["id"],
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(changed.status_code, 409)
            self.assertIn("后来修改", changed.get_json()["msg"])
            importer.assert_not_called()
            payload = client.get(
                "/api/online-scripts/log/" + info["id"], headers={"X-Token": TOKEN},
            ).get_json()
            self.assertFalse(payload["recovery"]["available"])
            self.assertIn("后来修改", payload["recovery"]["reason"])

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise install selection AJAX")
    def test_install_selection_ajax_handles_confirmation_and_redirects_to_original_log(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            client = app.test_client()
            html = client.get("/online-scripts/install-select/demo", headers={"X-Token": TOKEN}).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S) if "flsInstallSubmitApi" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm=require('node:vm');const assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const request={value:'a'.repeat(32)}; const importInput={checked:true};
const form={dataset:{},listeners:{},querySelector:s=>s.includes('request_id')?request:s.includes('import_task')?importInput:null,
 getAttribute:()=>'/online-scripts/install/demo', querySelectorAll:()=>[], addEventListener(n,f){this.listeners[n]=f;}};
const status={hidden:true,textContent:''},login={hidden:true,href:''}; let calls=[];
class FormDataMock{constructor(){this.values={request_id:request.value};}delete(k){delete this.values[k];}set(k,v){this.values[k]=v;}}
    const context={window:{FLS_INSTALL_SCRIPT_ID:'demo',confirm:()=>true,location:{pathname:'/online-scripts/install-select/demo',search:''}},
 document:{getElementById:id=>id==='onlineInstallSelectForm'?form:id==='onlineInstallApiError'?status:login,
  querySelector:()=>null,querySelectorAll:()=>[]},
     FormData:FormDataMock,sessionStorage:{setItem(){}},confirm:()=>true,
 flsGetCsrfToken:()=> 'install-csrf', flsReadJsonResponse:async r=>{if(!r.ok){const e=new Error(r.jsonData.msg);e.status=r.status;e.payload=r.jsonData;throw e;}return r.jsonData;},
 fetch:async(url,opts)=>{calls.push({url,opts});const value=calls.length===1?{confirm_required:true,msg:'target exists'}:{ok:true,install_id:'a'.repeat(32)};return {ok:calls.length!==1,status:calls.length===1?409:200,json:async()=>value};},
 encodeURIComponent,alert:()=>{}
    };vm.createContext(context);vm.runInContext(input.reader+input.script,context);
(async()=>{await context.flsInstallSubmitApi('/online-scripts/install/demo',form,{disabled:false});
 assert.equal(calls.length,2);assert.equal(calls[0].url,'/api/online-scripts/install/demo');assert.equal(calls[1].opts.body.values.force,'1');
 assert.equal(calls[0].opts.headers['X-CSRF-Token'],'install-csrf');assert.equal(context.window.location.href,'/online-scripts/log/'+ 'a'.repeat(32));})().catch(e=>{console.error(e);process.exitCode=1;});
"""], input=json.dumps({"script":script,"reader":reader}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise install recovery")
    def test_install_selection_refresh_recovers_original_request_without_new_post(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            html = app.test_client().get(
                "/online-scripts/install-select/demo", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "flsInstallRecoverPending" in script
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[
                source.index("async function flsReadJsonResponse("):source.index(
                    "async function flsReadTextResponse("
                )
            ]
            result = subprocess.run(
                ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const installId = 'a'.repeat(32);

async function runCase(pending, response, expected) {
  const elements = Object.fromEntries([
    'onlineInstallRecoveryStatus', 'onlineInstallRecoveryLog',
    'onlineInstallRecoveryRetry', 'onlineInstallApiLogin'
  ].map(id => [id, {hidden: true, disabled: false, textContent: '', href: '', onclick: null}]));
  const calls = [];
      const storage = {
        value: pending === undefined ? null : JSON.stringify(pending),
        getItem() { return this.value; },
        setItem(_key, value) { this.value = value; },
      };
      const form = {addEventListener() {}, querySelector: () => null, querySelectorAll: () => []};
      const context = {
    window: {FLS_INSTALL_SCRIPT_ID: 'demo', location: {pathname: '/online-scripts/install-select/demo', search: ''}},
    location: {pathname: '/online-scripts/install-select/demo', search: ''},
        document: {
          getElementById: id => id === 'onlineInstallSelectForm' ? form : elements[id],
      querySelectorAll: () => [],
      querySelector: () => null,
    },
        sessionStorage: storage,
        flsResponseErrorDiagnostic: (response, body) => {
          let payload = null;
          try { payload = JSON.parse(body); } catch (_) {}
          return {payload, message: payload && (payload.msg || payload.error || payload.message) || ''};
        },
        fetch: async (url, options) => {
      calls.push({url, options});
      if (response.throw) throw new Error(response.throw);
      return {
        ok: response.ok,
        status: response.status,
        text: async () => response.body,
      };
    },
    encodeURIComponent,
    flsApplyExcludedToVisible: () => {},
    setTimeout,
  };
  vm.createContext(context);
  vm.runInContext(input.reader + input.script, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, expected.calls);
  if (expected.calls) {
    assert.equal(calls[0].url, '/api/online-scripts/log/' + installId + '?lines=80');
    assert.equal(calls[0].options.method, undefined);
    assert.equal(calls[0].options.credentials, 'same-origin');
  }
  assert.equal(elements.onlineInstallRecoveryLog.hidden, expected.logHidden);
      assert.equal(elements.onlineInstallRecoveryRetry.hidden, expected.retryHidden);
      assert.equal(elements.onlineInstallApiLogin.hidden, expected.loginHidden);
      assert.match(elements.onlineInstallRecoveryStatus.textContent, expected.status);
      assert.equal(storage.value, pending === undefined ? null : JSON.stringify(pending));
      if (expected.logHidden === false) {
        assert.equal(elements.onlineInstallRecoveryLog.href, '/online-scripts/log/' + installId);
      }
      if (expected.retry) {
        await elements.onlineInstallRecoveryRetry.onclick();
        assert.equal(calls.length, expected.calls + 1);
        assert.equal(calls[1].url, calls[0].url);
        assert.equal(calls[1].options.method, undefined);
      }
}

(async () => {
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {ok: true, status: 200, body: JSON.stringify({running: true, status: '安装中', phase: 'downloading'})},
        {calls: 1, logHidden: false, retryHidden: false, loginHidden: true, status: /安装中.*downloading/, retry: true}
  );
  await runCase(
    {request_id: installId, script_id: 'demo'},
    {ok: true, status: 200, body: JSON.stringify({running: false, status: '结果未知', phase: 'unknown'})},
    {calls: 1, logHidden: false, retryHidden: false, loginHidden: true, status: /结果未知/}
  );
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {ok: false, status: 502, body: JSON.stringify({msg: '上游暂不可用'})},
    {calls: 1, logHidden: true, retryHidden: false, loginHidden: true, status: /上游暂不可用/}
  );
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {ok: true, status: 200, body: '<html>gateway timeout</html>'},
    {calls: 1, logHidden: true, retryHidden: false, loginHidden: true, status: /响应解析失败/}
  );
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {ok: false, status: 403, body: JSON.stringify({msg: '登录已过期'})},
    {calls: 1, logHidden: true, retryHidden: false, loginHidden: false, status: /登录已过期/}
  );
  await runCase(
    {install_id: installId, script_id: 'demo'},
    {throw: 'network disconnected'},
    {calls: 1, logHidden: true, retryHidden: false, loginHidden: true, status: /network disconnected/}
  );
  await runCase(
    '{not-json',
    {ok: true, status: 200, body: JSON.stringify({running: true})},
    {calls: 0, logHidden: true, retryHidden: true, loginHidden: true, status: /^$/}
  );
  await runCase(
    {install_id: '../escape', script_id: 'demo'},
    {ok: true, status: 200, body: JSON.stringify({running: true})},
    {calls: 0, logHidden: true, retryHidden: true, loginHidden: true, status: /^$/}
  );
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
                input=json.dumps({"script": script, "reader": reader}),
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise log polling")
    def test_polling_preserves_logs_and_stops_on_permission_or_missing_errors(self):
        with isolated_app() as (app, base_dir):
            self.prepare(base_dir)
            unauthorized = app.test_client().get("/api/online-scripts/log/previous")
            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(unauthorized.get_json()["msg"], "登录已过期")
            html = app.test_client().get("/online-scripts/log/previous", headers={"X-Token": TOKEN}).get_data(as_text=True)
            self.assertIn('id="installPollLogin" class="btn btn-primary" hidden>重新登录</a>', html)
            self.assertIn('id="installPollLogs" class="btn btn-orange" href="/logs" hidden>查看日志管理</a>', html)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                          if "async function loadLog()" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
  for(const status of [401, 403, 404, 409, 500, 200]) {
    const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
      'installStopAction', 'installRecoveryAction', 'installPollError', 'installPollLogin', 'installPollLogs', 'installPollRetry', 'log', 'flsLogNewTip']
      .map(id => [id, {textContent: '', hidden: false, dataset: {}, style: {}}]));
    elements.log.textContent = 'preserved output';
    const context = {
      window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
      document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id]},
      location: {pathname: '/online-scripts/log/previous', search: '?back=%2Fpull'},
      fetch: async () => ({ok: status === 200, status,
        json: async () => {
          if(status === 200) throw new Error('not JSON');
          return {msg: 'backend diagnostic'};
        }}),
      setInterval: () => 1, clearInterval() {},
    };
    vm.createContext(context);
    vm.runInContext(input.reader + input.script, context);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements.log.textContent, 'preserved output');
    assert.equal(elements.installPollError.hidden, false);
    const permanent = [401, 403, 404].includes(status);
    assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, null);
    const denied = [401, 403].includes(status);
    assert.equal(elements.installPollLogin.hidden, !denied);
    assert.equal(elements.installPollLogs.hidden, status !== 404);
    assert.equal(elements.installPollRetry.hidden, status === 404);
    assert.equal(elements.installPollRetry.disabled, false);
    if(denied) assert.equal(elements.installPollLogin.href,
      '/login?next=%2Fonline-scripts%2Flog%2Fprevious%3Fback%3D%252Fpull');
    if(status === 404) assert.equal(elements.installPollLogs.href, '/logs');
    if(permanent) assert.equal(elements.installRecoveryAction.hidden, true);
    assert.match(elements.installPollError.textContent, status === 200 ? /响应解析失败/ : /backend diagnostic/);
    if(status !== 404) {
      context.fetch = async () => ({ok: true, status: 200, json: async () => ({
        running: true, status: '安装中', log: 'running output',
      })});
      await elements.installPollRetry.onclick();
      assert.equal(elements.installStopAction.hidden, false);
      assert.equal(elements.installRecoveryAction.hidden, true);
      assert.equal(elements.installPollError.hidden, true);
      assert.equal(elements.installPollLogin.hidden, true);
      assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, 1);
      context.fetch = async () => ({ok: true, status: 200, json: async () => ({
        running: false, status: '结果未知', log: 'recovered output',
        recovery: {available: false, reason: 'already retried', href: '/online-scripts/log/child', label: 'child'},
      })});
      await elements.installPollRetry.onclick();
      assert.equal(elements.installPollError.hidden, true);
      assert.equal(elements.installPollLogin.hidden, true);
      assert.equal(elements.log.textContent, 'recovered output');
      assert.match(elements.installRecoveryAction.innerHTML, /online-scripts\/log\/child/);
      assert.equal(elements.installRecoveryAction.hidden, false);
      assert.equal(context.window.__FLS_ACTIVE_LOG_INTERVAL__, null);
    }
  }
  {
    const elements = Object.fromEntries(['installStatus', 'installError', 'installReturncode',
      'installStopAction', 'installRecoveryAction', 'installPollError', 'installPollLogin', 'installPollLogs', 'installPollRetry', 'log']
      .map(id => [id, {textContent: 'preserved', hidden: false, dataset: {}, style: {}}]));
    let resolve;
    let requests = 0;
    let intervals = 0;
    const context = {
      window: {innerHeight: 800, scrollY: 0, addEventListener() {}, scrollTo() {}},
      document: {documentElement: {scrollHeight: 800}, getElementById: id => elements[id]},
      fetch: () => { requests++; return new Promise(done => { resolve = done; }); },
      setInterval: () => { intervals++; return 1; }, clearInterval() {},
    };
    vm.createContext(context);
    vm.runInContext(input.reader + input.script, context);
    await elements.installPollRetry.onclick();
    assert.equal(requests, 1, 'manual recovery must not overlap an active request');
    elements.installPollError = {};
    resolve({ok: true, status: 200, json: async () => ({running: true, log: 'old page output'})});
    await new Promise(done => setImmediate(done));
    assert.equal(elements.log.textContent, 'preserved');
    assert.equal(intervals, 0, 'detached page must not start polling again');
  }
  console.log('log polling error paths passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""], input=json.dumps({"script": script, "reader": reader}), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
