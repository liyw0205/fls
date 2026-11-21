import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class OnlineSourceRefreshTests(unittest.TestCase):
    def response(self, body="[]", status=200):
        response = Mock()
        response.status_code = status
        response.text = body
        response.headers = {"Content-Type": "application/json"}
        return response

    def test_source_response_is_closed_and_cache_publish_is_atomic(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import source

            response = self.response('[{"id":"demo","name":"Demo","type":"raw","link":"https://example.invalid/demo.sh"}]')
            with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=response))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ):
                items = source.fetch_online_scripts()
            self.assertEqual(items[0]["id"], "demo")
            response.close.assert_called_once()
            cache = base_dir / "data" / "online_scripts_cache.json"
            self.assertEqual(json.loads(cache.read_text())[0]["id"], "demo")

            failed = self.response("not-json")
            with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=failed))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ):
                with self.assertRaisesRegex(RuntimeError, "不是合法 JSON"):
                    source.fetch_online_scripts()
            failed.close.assert_called_once()
            self.assertEqual(json.loads(cache.read_text())[0]["id"], "demo")

    def test_invalid_or_duplicate_remote_entries_fail_before_cache_publish(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import get_online_refresh_state

            source.save_online_script_cache([{"id": "stable", "name": "Stable", "type": "raw", "link": "x"}])
            cache = base_dir / "data" / "online_scripts_cache.json"
            original = cache.read_bytes()
            payloads = (
                '[{"id":"good","name":"Good","type":"raw","link":"x"}, {"id":"bad","name":""}]',
                '[{"id":"same","name":"One","type":"raw","link":"x"}, {"id":"same","name":"Two","type":"raw","link":"y"}]',
            )
            for payload in payloads:
                with self.subTest(payload=payload):
                    response = self.response(payload)
                    with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=response))), patch.object(
                        source, "github_proxy_url", side_effect=lambda url, *_args: url,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "未发布"):
                            source.fetch_online_scripts()
                    response.close.assert_called_once()
                    self.assertEqual(cache.read_bytes(), original)

            response = self.response(payloads[0])
            with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=response))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ):
                source.refresh_worker("invalid-source", log_file=str(base_dir / "invalid.log"))
            self.assertEqual(get_online_refresh_state()["status"], "失败")
            self.assertIn("未发布", get_online_refresh_state()["error"])
            self.assertEqual(cache.read_bytes(), original)

    def test_task_link_response_is_closed_and_error_does_not_discard_main_source(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import source

            main = self.response('[{"id":"demo","name":"Demo","type":"raw","link":"https://example.invalid/demo.sh","task_link":"https://example.invalid/tasks.json"}]')
            task = self.response("bad json")
            with patch.object(source, "requests", SimpleNamespace(get=Mock(side_effect=[main, task]))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ):
                items = source.fetch_online_scripts()
            self.assertEqual(items[0]["id"], "demo")
            main.close.assert_called_once()
            task.close.assert_called_once()
            self.assertEqual(json.loads((base_dir / "data" / "online_scripts_cache.json").read_text())[0]["id"], "demo")

    def test_task_link_failure_is_persisted_as_partial_diagnostic_without_discarding_main_source(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import source

            main = self.response(
                '[{"id":"demo","name":"Demo","type":"raw",'
                '"link":"https://example.invalid/demo.sh",'
                '"task_link":"https://example.invalid/tasks.json"}]'
            )
            task = self.response("bad json")
            failures = []
            with patch.object(source, "requests", SimpleNamespace(get=Mock(side_effect=[main, task]))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ):
                items = source.fetch_online_scripts(task_link_failures=failures)

            self.assertEqual(items[0]["id"], "demo")
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["script_id"], "demo")
            self.assertIn("不是合法 JSON", failures[0]["error"])
            self.assertEqual(json.loads((base_dir / "data" / "online_scripts_cache.json").read_text())[0]["id"], "demo")
            main.close.assert_called_once()
            task.close.assert_called_once()

    def test_source_refresh_collects_task_link_failures_past_inline_limit(self):
        with isolated_app() as (_app, _base_dir):
            from fls_manager.online_scripts import source

            source_items = [
                {
                    "id": f"script-{number:03d}",
                    "name": f"Script {number:03d}",
                    "type": "raw",
                    "link": f"https://example.invalid/{number:03d}.sh",
                    "task_link": f"https://example.invalid/tasks/{number:03d}.json",
                }
                for number in range(105)
            ]
            main = self.response(json.dumps(source_items))
            failures = []
            with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=main))), patch.object(
                source, "github_proxy_url", side_effect=lambda url, *_args: url,
            ), patch.object(source, "fetch_task_link_tasks", side_effect=RuntimeError("upstream timeout")):
                items = source.fetch_online_scripts(task_link_failures=failures)

            self.assertEqual(len(items), 105)
            self.assertEqual(len(failures), 105)
            self.assertEqual(failures[-1]["script_id"], "script-104")
            main.close.assert_called_once()

    def test_refresh_failure_details_are_bounded_paged_authenticated_and_restartable(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            request_id = "a" * 32
            failures = [
                {
                    "script_id": f"script-{number:03d}",
                    "name": f"Script {number:03d}",
                    "task_link": f"https://example.invalid/tasks/{number:03d}.json",
                    "error": f"upstream timeout {number}",
                    "proxy_id": "",
                }
                for number in range(105)
            ]
            constants.update_online_refresh_state(
                id="b" * 32, request_id=request_id, proxy_id="", running=False,
                status="部分完成", message="105 个外部任务源加载失败", error="",
                cache_published=True, task_link_failures=failures, updated_at="2026-11-21 10:00:00",
                log_file="",
            )
            state = constants.get_online_refresh_state()
            self.assertEqual(state["task_link_failure_count"], 105)
            self.assertEqual(len(state["task_link_failures"]), 100)
            detail_file = base_dir / "data" / "online_script_refresh_failures" / f"{request_id}.json"
            self.assertLessEqual(detail_file.stat().st_size, constants.ONLINE_REFRESH_TASK_FAILURE_FILE_LIMIT)

            client = app.test_client()
            first = client.get(
                f"/api/online-scripts/refresh-task-link-failures/{request_id}?offset=0&limit=100",
                headers={"X-Token": TOKEN},
            )
            second = client.get(
                f"/api/online-scripts/refresh-task-link-failures/{request_id}?offset=100&limit=100",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(first.json["total"], 105)
            self.assertEqual(len(first.json["items"]), 100)
            self.assertTrue(first.json["has_more"])
            self.assertEqual(second.json["items"][0]["script_id"], "script-100")
            self.assertEqual(second.json["items"][-1]["script_id"], "script-104")
            self.assertFalse(second.json["has_more"])
            self.assertEqual(first.json["digest"], second.json["digest"])
            self.assertIn(client.get(
                f"/api/online-scripts/refresh-task-link-failures/{request_id}?offset=0&limit=101",
                headers={"X-Token": TOKEN},
            ).status_code, (400, 503))
            self.assertIn(client.get(
                f"/api/online-scripts/refresh-task-link-failures/{request_id}?offset=0",
            ).status_code, (401, 302))

            page = client.get(
                f"/online-scripts?refresh_request_id={request_id}&failure_offset=100",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn("script-104", html)
            self.assertIn(f"/online-scripts/refresh-task-link/{request_id}/task-link/script-104", html)
            self.assertIn(f'name="origin_refresh_request_id" value="{request_id}"', html)
            self.assertIn("失败源 101-105 / 105", html)
            self.assertIn("failure_offset=0#online-status", html)

            restarted = subprocess.run(
                [sys.executable, "-c", (
                    "import json; from fls_manager.online_scripts.constants import "
                    "get_online_refresh_task_link_failures; "
                    f"print(json.dumps(get_online_refresh_task_link_failures('{request_id}', 100, 100)))"
                )],
                cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(json.loads(restarted.stdout)["items"][-1]["script_id"], "script-104")

            detail_file.write_text("not-json", encoding="utf-8")
            unavailable = client.get(
                f"/api/online-scripts/refresh-task-link-failures/{request_id}?offset=100&limit=100",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(unavailable.status_code, 503)
            self.assertIn("失败明细损坏", unavailable.json["error"])

    def test_refresh_failure_detail_storage_error_prevents_cache_publication(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import constants, source

            request_id = "c" * 32
            original = [{"id": "old", "name": "Old", "type": "raw", "link": "old.sh"}]
            source.save_online_script_cache(original)
            cache_before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
            constants.update_online_refresh_state(
                id="d" * 32, request_id=request_id, proxy_id="", running=True,
                status="运行中", message="", error="", cache_published=False,
                task_link_failures=[], updated_at="before", log_file="",
            )

            def fake_fetch(**kwargs):
                kwargs["task_link_failures"].extend({
                    "script_id": f"script-{number:03d}",
                    "name": f"Script {number:03d}",
                    "task_link": f"https://example.invalid/tasks/{number:03d}.json",
                    "error": "upstream timeout",
                    "proxy_id": "",
                } for number in range(105))
                return [{"id": "new", "name": "New", "type": "raw", "link": "new.sh"}]

            with patch.object(source, "fetch_online_scripts", side_effect=fake_fetch), patch.object(
                constants, "_write_online_refresh_task_failures_file", side_effect=OSError("detail store read-only"),
            ):
                source.refresh_worker("d" * 32, log_file=str(base_dir / "refresh.log"))

            self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), cache_before)
            self.assertEqual(source.load_online_script_cache()[0]["id"], "old")
            state = constants.get_online_refresh_state()
            self.assertEqual(state["status"], "失败")
            self.assertFalse(state["cache_published"])
            self.assertIn("detail store read-only", state["error"])

    def test_refresh_failure_task_link_retry_is_bound_idempotent_and_csrf_protected(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.routes.online_scripts import task_link

            refresh_request_id = "e" * 32
            task_request_id = constants.online_refresh_task_link_request_id(refresh_request_id, "demo")
            task_link_url = "https://example.invalid/demo-tasks.json"
            source.save_online_script_cache([
                {"id": "demo", "name": "Demo", "type": "raw", "link": "demo.sh", "task_link": task_link_url, "task_cron": []},
                {"id": "other", "name": "Other", "type": "raw", "link": "other.sh", "task_link": "https://example.invalid/other.json", "task_cron": []},
            ])
            constants.update_online_refresh_state(
                id="f" * 32, request_id=refresh_request_id, proxy_id="", running=False,
                status="部分完成", message="1 个任务源失败", error="", cache_published=True,
                task_link_failures=[{
                    "script_id": "demo", "name": "Demo", "task_link": task_link_url,
                    "error": "upstream timeout", "proxy_id": "",
                }], updated_at="2026-11-21 11:00:00", log_file="",
            )
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="refresh-source-csrf")
            headers = {"X-CSRF-Token": "refresh-source-csrf"}
            endpoint = f"/api/online-scripts/refresh-task-link/{refresh_request_id}/task-link/demo"
            anonymous = app.test_client()
            with anonymous.session_transaction() as session:
                session["csrf_token"] = "anonymous-source-csrf"
            with patch.object(task_link, "fetch_task_link_tasks", side_effect=[
                RuntimeError("upstream timeout"),
                [{"name": "Recovered", "cron": "", "command": "run recovered.sh"}],
            ]) as fetch:
                denied = anonymous.post(
                    endpoint, json={"request_id": "1" * 32}, headers={"X-CSRF-Token": "anonymous-source-csrf"},
                )
                csrf_denied = client.post(endpoint, json={"request_id": "1" * 32})
                wrong_script = client.post(
                    f"/api/online-scripts/refresh-task-link/{refresh_request_id}/task-link/other",
                    json={"request_id": "2" * 32}, headers=headers,
                )
                wrong_origin = client.post(
                    endpoint,
                    json={"request_id": "3" * 32, "origin_refresh_request_id": "4" * 32},
                    headers=headers,
                )
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(csrf_denied.status_code, 400)
                self.assertEqual(wrong_script.status_code, 409)
                self.assertEqual(wrong_origin.status_code, 409)
                fetch.assert_not_called()

                first = client.post(
                    endpoint,
                    json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                    headers=headers,
                )
                retry_page = client.get(
                    f"/online-scripts?refresh_request_id={refresh_request_id}",
                    headers={"X-Token": TOKEN},
                ).get_data(as_text=True)
                self.assertIn(f'name="request_id" value="{task_request_id}"', retry_page)
                recovered = client.post(
                    endpoint,
                    json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                    headers=headers,
                )
                replay = client.post(
                    endpoint,
                    json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                    headers=headers,
                )
                generic_replay = client.post(
                    "/api/online-scripts/task-link/demo", json={"request_id": task_request_id}, headers=headers,
                )
            self.assertEqual(first.status_code, 502)
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(generic_replay.status_code, 409)
            self.assertEqual(fetch.call_count, 2)
            receipt = source.get_task_link_request(task_request_id)
            self.assertEqual(receipt["origin_refresh_request_id"], refresh_request_id)

            page = client.get(
                f"/online-scripts?refresh_request_id={refresh_request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn("Demo：已恢复，新增 1 个任务，当前共 1 个任务", html)
            self.assertNotIn(f"/online-scripts/refresh-task-link/{refresh_request_id}/task-link/demo", html)
            paged = client.get(
                f"/api/online-scripts/refresh-task-link-failures/{refresh_request_id}?offset=0",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(paged.json["items"][0]["task_link_request_id"], task_request_id)
            self.assertEqual(paged.json["items"][0]["recovery"]["state"], "completed")
            refreshed_status = client.get(
                f"/api/online-scripts/refresh-status/{refresh_request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(refreshed_status.status_code, 200)
            self.assertEqual(refreshed_status.json["task_link_failures"][0]["recovery"]["state"], "completed")

    def test_refresh_worker_exposes_partial_task_link_retry_actions(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import get_online_refresh_state, update_online_refresh_state

            refresh_request_id = "9" * 32
            update_online_refresh_state(
                id="partial-refresh", request_id=refresh_request_id, proxy_id="", running=True,
                status="运行中", message="", error="", cache_published=False,
                task_link_failures=[], updated_at="before", log_file="",
            )

            def fake_fetch(**kwargs):
                kwargs["task_link_failures"].append({
                    "script_id": "demo",
                    "name": "Demo",
                    "task_link": "https://example.invalid/tasks.json",
                    "error": "任务源暂时不可用",
                })
                return [{
                    "id": "demo", "name": "Demo", "type": "raw", "link": "x",
                    "task_link": "https://example.invalid/tasks.json",
                }]

            with patch.object(source, "fetch_online_scripts", side_effect=fake_fetch):
                source.refresh_worker("partial-refresh", log_file=str(base_dir / "refresh.log"))

            state = get_online_refresh_state()
            self.assertEqual(state["status"], "部分完成")
            self.assertTrue(state["cache_published"])
            self.assertEqual(state["task_link_failures"][0]["script_id"], "demo")
            payload = app.test_client().get(
                "/api/online-scripts/refresh-status", headers={"X-Token": TOKEN},
            ).get_json()
            self.assertEqual(payload["status"], "部分完成")
            self.assertEqual(payload["task_link_failures"][0]["script_id"], "demo")
            self.assertEqual(
                payload["task_link_failures"][0]["task_link_request_id"],
                source.online_refresh_task_link_request_id(refresh_request_id, "demo"),
            )
            page = app.test_client().get(
                "/online-scripts", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            self.assertIn("任务源暂时不可用", page)
            self.assertIn(f"/online-scripts/refresh-task-link/{refresh_request_id}/task-link/demo", page)
            self.assertIn('class="inline-form fls-online-task-link-form"', page)
            self.assertIn('data-script-id="demo"', page)
            self.assertIn('name="csrf_token"', page)
            self.assertIn('name="proxy_id"', page)

    def test_refresh_failure_recovery_survives_task_history_pruning_restart_and_cache_change(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.routes.online_scripts import task_link

            refresh_request_id = "6" * 32
            item = {
                "id": "demo", "name": "Demo", "type": "raw", "link": "demo.sh",
                "task_link": "https://example.invalid/demo-tasks.json", "task_cron": [],
            }
            source.save_online_script_cache([item])
            constants.update_online_refresh_state(
                id="7" * 32, request_id=refresh_request_id, proxy_id="", running=False,
                status="部分完成", message="1 个任务源失败", error="", cache_published=True,
                task_link_failures=[{
                    "script_id": "demo", "name": "Demo", "task_link": item["task_link"],
                    "error": "upstream timeout", "proxy_id": "",
                }], updated_at="2026-11-21 12:00:00", log_file="",
            )
            (base_dir / "data" / "online_script_refresh_failures" / f"{refresh_request_id}.json").unlink()
            task_request_id = constants.online_refresh_task_link_request_id(refresh_request_id, "demo")
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="stable-refresh-csrf")
            headers = {"X-CSRF-Token": "stable-refresh-csrf"}
            endpoint = f"/api/online-scripts/refresh-task-link/{refresh_request_id}/task-link/demo"
            recovered_tasks = [{"name": "Recovered", "cron": "", "command": "run recovered.sh"}]
            with patch.object(task_link, "fetch_task_link_tasks", return_value=recovered_tasks) as fetch:
                submitted = client.post(
                    endpoint,
                    json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                    headers=headers,
                )
                self.assertEqual(submitted.status_code, 200)
                self.assertFalse(submitted.json.get("replayed", False))
                recovery_file = (
                    base_dir / "data" / "online_script_refresh_failures"
                    / f"{refresh_request_id}.recovery.json"
                )
                self.assertTrue(recovery_file.is_file())

                for number in range(205):
                    request_id = f"{number + 1:032x}"
                    if request_id == task_request_id:
                        request_id = f"{number + 1001:032x}"
                    source.persist_task_link_request({
                        "request_id": request_id, "script_id": f"other-{number}", "proxy_id": "",
                        "expected_version": "", "state": "completed", "added": 0, "total": 0,
                        "error": "", "updated_at": f"2099-01-01 00:00:{number:04d}",
                        "expected_tasks_fingerprint": "", "published_tasks_fingerprint": "",
                        "confirmation": "", "confirmation_fingerprint": "",
                        "origin_refresh_request_id": "",
                    })
                self.assertIsNone(source.get_task_link_request(task_request_id))

                changed_item = {**item, "task_cron": [{"name": "Later", "cron": "", "command": "later"}]}
                source.save_online_script_cache([changed_item])
                replay = client.post(
                    endpoint,
                    json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                    headers=headers,
                )
                self.assertEqual(replay.status_code, 200)
                self.assertTrue(replay.json["replayed"])
                self.assertEqual(replay.json["added"], 1)
                self.assertEqual(fetch.call_count, 1)

            restarted = subprocess.run(
                [sys.executable, "-c", (
                    "import json; from fls_manager.online_scripts.constants import "
                    "get_online_refresh_task_link_recovery; "
                    f"print(json.dumps(get_online_refresh_task_link_recovery('{refresh_request_id}', 'demo')))"
                )],
                cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(json.loads(restarted.stdout)["request_id"], task_request_id)

            page = client.get(
                f"/online-scripts?refresh_request_id={refresh_request_id}", headers={"X-Token": TOKEN},
            )
            html = page.get_data(as_text=True)
            self.assertIn("Demo：已恢复，新增 1 个任务，当前共 1 个任务", html)
            self.assertNotIn('data-script-id="demo"', html)

            recovery_file.write_text("broken", encoding="utf-8")
            recovery_backup_file = recovery_file.with_name(
                f"{refresh_request_id}.recovery.bak.json"
            )
            recovery_backup_file.write_text("broken backup", encoding="utf-8")
            with patch.object(task_link, "fetch_task_link_tasks") as fetch_after_corruption:
                blocked = client.post(
                    endpoint,
                    json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                    headers=headers,
                )
            self.assertEqual(blocked.status_code, 503)
            fetch_after_corruption.assert_not_called()
            damaged_page = client.get(
                f"/online-scripts?refresh_request_id={refresh_request_id}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            self.assertIn("恢复状态不可读取", damaged_page)
            self.assertNotIn('data-script-id="demo"', damaged_page)

    def test_refresh_failure_recovery_write_failure_confirms_after_cache_merge_without_refetch(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.routes.online_scripts import task_link

            refresh_request_id = "8" * 32
            item = {
                "id": "demo", "name": "Demo", "type": "raw", "link": "demo.sh",
                "task_link": "https://example.invalid/demo-tasks.json", "task_cron": [],
            }
            source.save_online_script_cache([item])
            constants.update_online_refresh_state(
                id="9" * 32, request_id=refresh_request_id, proxy_id="", running=False,
                status="部分完成", message="1 个任务源失败", error="", cache_published=True,
                task_link_failures=[{
                    "script_id": "demo", "name": "Demo", "task_link": item["task_link"],
                    "error": "upstream timeout", "proxy_id": "",
                }], updated_at="2026-11-21 12:30:00", log_file="",
            )
            task_request_id = constants.online_refresh_task_link_request_id(refresh_request_id, "demo")
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="recovery-write-csrf")
            headers = {"X-CSRF-Token": "recovery-write-csrf"}
            endpoint = f"/api/online-scripts/refresh-task-link/{refresh_request_id}/task-link/demo"
            tasks = [{"name": "Recovered", "cron": "", "command": "run recovered.sh"}]
            with patch.object(task_link, "fetch_task_link_tasks", return_value=tasks) as fetch, patch.object(
                task_link, "persist_online_refresh_task_link_recovery", side_effect=OSError("recovery store read-only"),
            ):
                uncertain = client.post(
                    endpoint,
                    json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                    headers=headers,
                )
            self.assertEqual(uncertain.status_code, 503)
            self.assertFalse(uncertain.json["retry_allowed"])
            self.assertEqual(source.get_task_link_request(task_request_id)["state"], "unknown")
            blocked = client.post(
                endpoint,
                json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                headers=headers,
            )
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(fetch.call_count, 1)

            confirmed = client.post(
                f"/api/online-scripts/task-link-confirm/{task_request_id}", headers=headers,
            )
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json["confirmation"], "merged")
            replay = client.post(
                endpoint,
                json={"request_id": task_request_id, "origin_refresh_request_id": refresh_request_id},
                headers=headers,
            )
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(fetch.call_count, 1)
            recovery = constants.get_online_refresh_task_link_recovery(refresh_request_id, "demo")
            self.assertEqual(recovery["state"], "completed")

    def test_refresh_failure_recovery_mirrors_and_repairs_single_damaged_copy(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants, source

            request_id = "a" * 32
            item = {"id": "demo", "name": "Demo", "type": "raw", "link": "demo.sh",
                    "task_link": "https://example.invalid/tasks.json", "task_cron": []}
            source.save_online_script_cache([item])
            constants.update_online_refresh_state(
                id="b" * 32, request_id=request_id, proxy_id="", running=False, status="部分完成",
                message="1 个任务源失败", error="", cache_published=True,
                task_link_failures=[{"script_id": "demo", "name": "Demo", "task_link": item["task_link"],
                                    "error": "upstream timeout", "proxy_id": ""}],
                updated_at="before", log_file="",
            )
            receipt = constants.persist_online_refresh_task_link_recovery(
                request_id, "demo", proxy_id="", added=1, total=1,
            )
            primary = base_dir / "data" / "online_script_refresh_failures" / f"{request_id}.recovery.json"
            backup = base_dir / "data" / "online_script_refresh_failures" / f"{request_id}.recovery.bak.json"
            self.assertEqual(primary.read_bytes(), backup.read_bytes())
            task_request_id = receipt["request_id"]

            for damaged in (primary, backup):
                with self.subTest(damaged=damaged.name):
                    good = backup if damaged == primary else primary
                    damaged.write_text("broken", encoding="utf-8")
                    response = app.test_client().get(
                        f"/api/online-scripts/refresh-task-link-failures/{request_id}?offset=0",
                        headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json["maintenance"]["repaired"])
                    self.assertEqual(response.json["items"][0]["recovery"]["request_id"], task_request_id)
                    self.assertEqual(primary.read_bytes(), backup.read_bytes())
                    self.assertEqual(json.loads(primary.read_text())["items"]["demo"]["state"], "completed")
                    self.assertEqual(good.read_bytes(), primary.read_bytes())

            backup.write_text("damaged backup", encoding="utf-8")
            page = app.test_client().get(
                f"/online-scripts?refresh_request_id={request_id}", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            self.assertIn("恢复回执副本已自动修复", page)
            self.assertIn("重新读取恢复状态", page)

    def test_refresh_failure_recovery_prefers_newer_backup_after_primary_publish_failure(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import constants, source

            request_id = "c" * 32
            failures = [
                {"script_id": script_id, "name": script_id, "task_link": f"https://example.invalid/{script_id}.json",
                 "error": "upstream timeout", "proxy_id": ""}
                for script_id in ("first", "second")
            ]
            constants.update_online_refresh_state(
                id="d" * 32, request_id=request_id, proxy_id="", running=False, status="部分完成",
                message="2 个任务源失败", error="", cache_published=True, task_link_failures=failures,
                updated_at="before", log_file="",
            )
            constants.persist_online_refresh_task_link_recovery(
                request_id, "first", proxy_id="", added=1, total=1,
            )
            primary = base_dir / "data" / "online_script_refresh_failures" / f"{request_id}.recovery.json"
            backup = base_dir / "data" / "online_script_refresh_failures" / f"{request_id}.recovery.bak.json"
            original_replace = constants.os.replace

            def fail_primary_publish(src, dst):
                if Path(dst) == primary:
                    raise OSError("primary recovery publication denied")
                return original_replace(src, dst)

            with patch.object(constants.os, "replace", side_effect=fail_primary_publish):
                with self.assertRaisesRegex(OSError, "primary recovery publication denied"):
                    constants.persist_online_refresh_task_link_recovery(
                        request_id, "second", proxy_id="", added=2, total=2,
                    )

            damaged_primary = json.loads(primary.read_text(encoding="utf-8"))
            published_backup = json.loads(backup.read_text(encoding="utf-8"))
            self.assertEqual(damaged_primary["generation"], 1)
            self.assertEqual(published_backup["generation"], 2)
            self.assertEqual(set(published_backup["items"]), {"first", "second"})

            recovered = constants._read_online_refresh_task_recoveries_file(request_id)
            self.assertTrue(recovered.repaired)
            self.assertEqual(recovered.generation, 2)
            self.assertEqual(set(recovered), {"first", "second"})
            self.assertEqual(primary.read_bytes(), backup.read_bytes())

    def test_refresh_failure_recovery_backup_publish_limits_and_pruning(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import constants

            request_id = "2" * 32
            recovery_id = constants.online_refresh_task_link_request_id(request_id, "demo")
            item = {
                "demo": {
                    "request_id": recovery_id, "state": "completed", "proxy_id": "",
                    "added": 1, "total": 1, "updated_at": "now",
                },
            }
            directory = base_dir / "data" / "online_script_refresh_failures"
            primary = directory / f"{request_id}.recovery.json"
            backup = directory / f"{request_id}.recovery.bak.json"
            constants._write_online_refresh_task_recoveries_file(request_id, item, 1)
            original_pair = (primary.read_bytes(), backup.read_bytes())

            original_replace = constants.os.replace

            def fail_backup_publish(src, dst):
                if Path(dst) == backup:
                    raise OSError("backup recovery publication denied")
                return original_replace(src, dst)

            with patch.object(constants.os, "replace", side_effect=fail_backup_publish):
                with self.assertRaisesRegex(OSError, "backup recovery publication denied"):
                    constants._write_online_refresh_task_recoveries_file(request_id, item, 2)
            self.assertEqual((primary.read_bytes(), backup.read_bytes()), original_pair)
            self.assertEqual(list(directory.glob("*.recovery.tmp")), [])

            with patch.object(constants, "ONLINE_REFRESH_TASK_FAILURE_TOTAL_LIMIT", 0):
                with self.assertRaises(constants.OnlineRefreshTaskFailureLimit):
                    constants._write_online_refresh_task_recoveries_file(request_id, item, 2)
            self.assertEqual((primary.read_bytes(), backup.read_bytes()), original_pair)

            other_request_id = "3" * 32
            other_recovery_id = constants.online_refresh_task_link_request_id(other_request_id, "demo")
            constants._write_online_refresh_task_recoveries_file(other_request_id, {
                "demo": {**item["demo"], "request_id": other_recovery_id},
            }, 1)
            constants._prune_online_refresh_task_failures_files({request_id})
            self.assertTrue(primary.is_file())
            self.assertTrue(backup.is_file())
            self.assertFalse((directory / f"{other_request_id}.recovery.json").exists())
            self.assertFalse((directory / f"{other_request_id}.recovery.bak.json").exists())
            constants._prune_online_refresh_task_failures_files(set())
            self.assertFalse(primary.exists())
            self.assertFalse(backup.exists())

    def test_refresh_failure_recovery_fails_closed_when_copies_conflict_or_both_are_invalid(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import constants

            request_id = "e" * 32
            directory = base_dir / "data" / "online_script_refresh_failures"
            directory.mkdir(parents=True)
            primary = directory / f"{request_id}.recovery.json"
            backup = directory / f"{request_id}.recovery.bak.json"
            recovery_id = constants.online_refresh_task_link_request_id(request_id, "demo")
            first = {"demo": {"request_id": recovery_id, "state": "completed", "proxy_id": "",
                              "added": 1, "total": 1, "updated_at": "one"}}
            second = {"demo": {"request_id": recovery_id, "state": "completed", "proxy_id": "",
                               "added": 2, "total": 2, "updated_at": "two"}}
            primary.write_text(json.dumps({"version": 1, "generation": 3, "request_id": request_id, "items": first}))
            backup.write_text(json.dumps({"version": 1, "generation": 3, "request_id": request_id, "items": second}))
            with self.assertRaisesRegex(RuntimeError, "冲突"):
                constants._read_online_refresh_task_recoveries_file(request_id)

            primary.write_text("broken-primary")
            backup.write_text('{"version":2,"generation":4,"request_id":"' + request_id + '","items":{}}')
            with self.assertRaisesRegex(RuntimeError, "副本均无效"):
                constants._read_online_refresh_task_recoveries_file(request_id)

    def test_refresh_failure_recovery_migrates_legacy_copy_and_serializes_concurrent_updates(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import constants

            request_id = "f" * 32
            scripts = ("alpha", "beta", "gamma", "delta")
            failures = [{
                "script_id": script_id, "name": script_id,
                "task_link": f"https://example.invalid/{script_id}.json",
                "error": "upstream timeout", "proxy_id": "",
            } for script_id in scripts]
            constants.update_online_refresh_state(
                id="1" * 32, request_id=request_id, proxy_id="", running=False, status="部分完成",
                message="4 个任务源失败", error="", cache_published=True,
                task_link_failures=failures, updated_at="before", log_file="",
            )
            directory = base_dir / "data" / "online_script_refresh_failures"
            primary = directory / f"{request_id}.recovery.json"
            backup = directory / f"{request_id}.recovery.bak.json"
            legacy_item = {
                "request_id": constants.online_refresh_task_link_request_id(request_id, "alpha"),
                "state": "completed", "proxy_id": "", "added": 1, "total": 1,
                "updated_at": "legacy",
            }
            primary.write_text(json.dumps({"version": 1, "request_id": request_id, "items": {"alpha": legacy_item}}))

            migrated = constants._read_online_refresh_task_recoveries_file(request_id)
            self.assertTrue(migrated.repaired)
            self.assertEqual(migrated.generation, 0)
            self.assertEqual(primary.read_bytes(), backup.read_bytes())
            constants.persist_online_refresh_task_link_recovery(
                request_id, "beta", proxy_id="", added=2, total=2,
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda script_id: constants.persist_online_refresh_task_link_recovery(
                        request_id, script_id, proxy_id="", added=3, total=3,
                    ),
                    ("gamma", "delta"),
                ))
            self.assertEqual(len(results), 2)
            recovered = constants._read_online_refresh_task_recoveries_file(request_id)
            self.assertEqual(recovered.generation, 3)
            self.assertEqual(set(recovered), set(scripts))
            self.assertEqual(primary.read_bytes(), backup.read_bytes())

            restarted = subprocess.run(
                [sys.executable, "-c", (
                    "import json; from fls_manager.online_scripts.constants import "
                    "_read_online_refresh_task_recoveries_file; "
                    f"data=_read_online_refresh_task_recoveries_file('{request_id}'); "
                    "print(json.dumps({'generation':data.generation,'count':len(data),'repaired':data.repaired}))"
                )],
                cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(restarted.returncode, 0, restarted.stderr)
            self.assertEqual(json.loads(restarted.stdout), {"generation": 3, "count": 4, "repaired": False})

            target = base_dir / "recovery-target.json"
            target.write_text("untouched", encoding="utf-8")
            backup.unlink()
            backup.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "符号链接"):
                constants._read_online_refresh_task_recoveries_file(request_id)
            self.assertEqual(target.read_text(encoding="utf-8"), "untouched")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the partial refresh UI")
    def test_refresh_polling_renders_task_link_retry_action(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            scripts = re.findall(r"<script>(.*?)</script>", response.get_data(as_text=True), re.S)
            script = next(script for script in scripts if "async function updateOnlineRefreshStatus(" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const retryButton = {onclick: null, __flsOnlineTaskLinkBound: false};
const retryForm = {querySelector: () => null, getAttribute: () => 'demo'};
retryButton.form = retryForm;
const elements = {
  onlineRefreshStatusCard: {style: {}},
  onlineRefreshStatusText: {innerHTML: ''},
  onlineRefreshTaskLinkFailures: {innerHTML: '', querySelectorAll: () => [retryButton]},
  onlineRefreshLogLink: {hidden: true, href: ''},
  onlineRefreshBtn: {disabled: true, textContent: '正在同步中...'},
  onlineRefreshHistoryError: {hidden: true, textContent: '', children:[], appendChild(node){this.children.push(node);}},
  onlineRefreshLogin: {hidden: true, href: ''},
  onlineRefreshQuery: {hidden: true, disabled: false},
  onlineRefreshPersistence: {innerHTML: ''},
};
const storage = {data:{}, get value(){return this.getItem('fls-online-refresh-request');},
  set value(value){this.setItem('fls-online-refresh-request', value);},
  getItem(key){return this.data[key] || null;}, setItem(key,value){this.data[key]=value;}, removeItem(key){delete this.data[key];}};
let submitHandlers=0;
const context = {
  window: {},
  location: {pathname: '/online-scripts', search: ''},
  document: {getElementById: id => elements[id], querySelectorAll: () => [],
    createElement: name => ({tagName:name,href:'',textContent:''}), createTextNode: text => ({textContent:text}),
    addEventListener: name => {if(name==='submit') submitHandlers++;}},
  sessionStorage: storage,
  flsGetCsrfToken: () => 'refresh-repair-csrf',
  flsResponseErrorDiagnostic: (_response, body) => ({payload: null, message: body}),
  fetch: async () => ({ok: true, status: 200, json: async () => ({
    id: 'c'.repeat(32), request_id: 'd'.repeat(32), running: false,
    status: '部分完成', message: '1 个外部任务源加载失败，请逐项重试', error: '',
    updated_at: 'now', log_file: '/tmp/refresh.log',
    task_link_failures: [{script_id: 'demo', name: 'Demo', error: 'upstream timeout'}],
  })}),
  setTimeout: () => {},
};
vm.createContext(context);
vm.runInContext(input.reader + input.script, context);
setImmediate(async () => {
  assert.equal(elements.onlineRefreshStatusCard.style.display, 'block');
  assert.match(elements.onlineRefreshStatusText.innerHTML, /部分完成/);
  assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /task-link\/demo/);
  assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /refresh-task-link\/d{32}\/task-link\/demo/);
  assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /data-api-action='\/api\/online-scripts\/refresh-task-link\/d{32}\/task-link\/demo'/);
  assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /origin_refresh_request_id/);
  assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /name='csrf_token'/);
  assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /upstream timeout/);
  assert.equal(typeof retryButton.onclick, 'function');
  assert.equal(retryButton.__flsOnlineTaskLinkBound, true);
  assert.equal(elements.onlineRefreshBtn.disabled, false);
  assert.equal(elements.onlineRefreshLogLink.hidden, false);
  for(const status of [401, 403, 409, 500, 503]) {
    context.fetch = async () => ({ok: false, status, text: async () => 'temporary status failure'});
    await context.updateOnlineRefreshStatus();
    assert.match(elements.onlineRefreshStatusText.innerHTML, new RegExp('HTTP ' + status));
    assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /task-link\/demo/);
    assert.equal(elements.onlineRefreshLogLink.hidden, false);
    assert.equal(elements.onlineRefreshQuery.hidden, false);
    assert.equal(elements.onlineRefreshQuery.disabled, false);
    assert.equal(elements.onlineRefreshLogin.hidden, status !== 401 && status !== 403);
  }
  const requestId = 'e'.repeat(32);
  storage.value = JSON.stringify({request_id: requestId, proxy_id: '', server_id: 'f'.repeat(32)});
  context.fetch = async () => ({ok: false, status: 404, text: async () => 'refresh request missing'});
  await context.updateOnlineRefreshStatus();
  assert.match(elements.onlineRefreshStatusText.innerHTML, /原刷新记录不存在或已被替换/);
  assert.equal(JSON.parse(storage.value).request_id, requestId);
  for(const body of ['<html>login</html>', '[]', JSON.stringify({running:false, request_id:'a'.repeat(32)})]) {
    context.fetch = async () => ({ok: true, status: 200, text: async () => body});
    await context.updateOnlineRefreshStatus();
    assert.equal(elements.onlineRefreshQuery.hidden, false);
    assert.equal(JSON.parse(storage.value).request_id, requestId);
    assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /task-link\/demo/);
  }
  let resolveQuery, queryCount = 0;
  context.fetch = async (url, options) => {
    queryCount++;
    assert.equal(url, '/api/online-scripts/refresh-status/' + requestId);
    assert.equal(options.method, undefined);
    return new Promise(resolve => {resolveQuery = resolve;});
  };
  const query = elements.onlineRefreshQuery.onclick();
  await elements.onlineRefreshQuery.onclick();
  assert.equal(queryCount, 1);
  assert.equal(elements.onlineRefreshQuery.disabled, true);
  resolveQuery({ok:true, status:200, json:async()=>({
    id:'f'.repeat(32), request_id:requestId, running:false, status:'失败', error:'original failure',
    task_link_failures:[{script_id:'demo', error:'upstream timeout'}],
  })});
  await query;
  assert.equal(elements.onlineRefreshQuery.hidden, true);
  assert.equal(elements.onlineRefreshQuery.disabled, false);
  assert.equal(elements.onlineRefreshLogin.hidden, true);
  assert.equal(elements.onlineRefreshLogLink.href, '/online-scripts/refresh-log/' + requestId);
  assert.match(elements.onlineRefreshTaskLinkFailures.innerHTML, /task-link\/demo/);
  context.fetch = async () => ({ok:false, status:404, json:async()=>({error:'request missing'})});
  await elements.onlineRefreshQuery.onclick();
  const inputId = {value:requestId}, proxy = {value:''}, button = {disabled:false};
  const form = {action:'/online-scripts/refresh', querySelector(selector){
    if(selector.includes('request_id')) return inputId;
    if(selector.includes('proxy_id')) return proxy;
    return button;
  }};
  let posts = 0;
  context.FormData = class {constructor(form){this.form = form;}};
  context.fetch = async (url, options) => {
    if(options.method === 'POST') {
      posts++;
      assert.equal(url, '/api/online-scripts/refresh');
      assert.notEqual(inputId.value, requestId);
      assert.match(inputId.value, /^[a-f0-9]{32}$/);
      return {ok:true, status:202, json:async()=>({ok:true, id:'b'.repeat(32)})};
    }
    assert.equal(url, '/api/online-scripts/refresh-status/' + inputId.value);
    return {ok:true, status:200, json:async()=>({id:'b'.repeat(32), request_id:inputId.value, running:true, status:'运行中'})};
  };
  context.window.confirm = () => false;
  await context.submitOnlineRefreshForm(form);
  assert.equal(posts, 0);
  assert.equal(JSON.parse(storage.value).request_id, requestId);
  context.window.confirm = () => true;
  await context.submitOnlineRefreshForm(form);
  assert.equal(posts, 1);
  assert.equal(JSON.parse(storage.value).request_id, inputId.value);
  const retryId = '9'.repeat(32), originalId = '8'.repeat(32);
  const retryInput = {value:retryId};
  const retryForm = {action:'/online-scripts/refresh-retry/' + originalId,
    getAttribute(){return originalId;}, querySelector(selector){
      if(selector.includes('retry_request_id')) return retryInput;
      if(selector.includes('proxy_id')) return proxy;
      return button;
    }
  };
  context.fetch = async (url, options) => {
    if(options.method === 'POST') {
      posts++;
      assert.equal(url, '/api/online-scripts/refresh-retry/' + originalId);
      throw new Error('response lost after retry accepted');
    }
    assert.equal(url, '/api/online-scripts/refresh-status/' + retryId);
    return {ok:true, status:200, json:async()=>({id:'b'.repeat(32), request_id:retryId, running:true, status:'运行中'})};
  };
  await context.submitOnlineRefreshRetryForm(retryForm);
  assert.equal(posts, 2);
  assert.equal(JSON.parse(storage.value).request_id, retryId);
  assert.equal(elements.onlineRefreshQuery.hidden, false);
  await elements.onlineRefreshQuery.onclick();
  assert.equal(posts, 2);
  assert.equal(elements.onlineRefreshQuery.hidden, true);
  assert.equal(JSON.parse(storage.value).request_id, retryId);
  let retryAttempts = 0;
  context.fetch = async (url, options) => {
    assert.equal(url, '/api/online-scripts/refresh-retry/' + originalId);
    assert.equal(options.method, 'POST');
    retryAttempts++;
    return {ok:false, status:500, text:async()=>JSON.stringify({error:'thread quota exhausted'})};
  };
  await context.submitOnlineRefreshRetryForm(retryForm);
  assert.equal(retryAttempts, 1);
  assert.equal(retryInput.value, retryId);
  assert.equal(JSON.parse(storage.getItem('fls-online-refresh-retry:' + originalId)).request_id, retryId);
  assert.match(elements.onlineRefreshHistoryError.textContent, /thread quota exhausted/);
  context.fetch = async () => ({ok:false, status:409, text:async()=>JSON.stringify({
    retry_not_started:true, error:'refresh retry chain reached capacity', request_id:retryId,
  })});
  context.flsResponseErrorDiagnostic = (_response, body) => {
    try { const payload=JSON.parse(body); return {payload, message:payload.error || body}; }
    catch(_error) { return {payload:null, message:body}; }
  };
  await context.submitOnlineRefreshRetryForm(retryForm);
  assert.equal(storage.getItem('fls-online-refresh-retry:' + originalId), null);
  assert.equal(storage.getItem('fls-online-refresh-request'), null);
  assert.notEqual(retryInput.value, retryId);
  assert.equal(elements.onlineRefreshQuery.hidden, true);
  assert.match(elements.onlineRefreshHistoryError.textContent, /此次重试未启动/);
  assert.equal(elements.onlineRefreshHistoryError.children.at(-1).href, '/online-scripts#online-status');
  const recoveryId = '7'.repeat(32);
  const recoverButton = {disabled:false};
  const recoverForm = {dataset:{}, getAttribute:()=>recoveryId, querySelector:()=>recoverButton};
  storage.value = JSON.stringify({request_id:recoveryId, proxy_id:'original-proxy', server_id:'6'.repeat(32)});
  context.renderOnlineRefreshPersistence({request_id:recoveryId, persistence_error:'history write denied'});
  assert.match(elements.onlineRefreshPersistence.innerHTML,/data-api-action='\/api\/online-scripts\/refresh-recover\//);
  for(const status of [401,403,409,500,503,200]) {
    context.fetch = async (url, options) => {
      assert.equal(url,'/api/online-scripts/refresh-recover/'+recoveryId);
      assert.equal(options.method,'POST');
      assert.equal(options.headers['X-CSRF-Token'],'refresh-repair-csrf');
      return {ok:status===200, status, text:async()=>'<html>unavailable</html>'};
    };
    await context.submitOnlineRefreshRecoverForm(recoverForm);
    assert.match(elements.onlineRefreshStatusText.textContent,/回执修复未确认/);
    assert.equal(recoverButton.disabled,false);
    assert.equal(recoverForm.dataset.pending,undefined);
    assert.equal(elements.onlineRefreshQuery.hidden,false);
    assert.equal(elements.onlineRefreshLogin.hidden,status!==401 && status!==403);
    assert.equal(JSON.parse(storage.value).request_id,recoveryId);
    assert.equal(JSON.parse(storage.value).proxy_id,'original-proxy');
  }
  let finishRepair, repairPosts=0;
  context.fetch = async (url, options) => {
    if(options.method==='POST') {
      repairPosts++;
      return new Promise(resolve=>{finishRepair=resolve;});
    }
    if(url.includes('refresh-history')) return {ok:true,status:200,json:async()=>({ok:true,items:[]})};
    assert.equal(url,'/api/online-scripts/refresh-status/'+recoveryId);
    return {ok:true,status:200,json:async()=>({id:'6'.repeat(32),request_id:recoveryId,running:false,status:'结果未知',error:'original diagnosis'})};
  };
  const repair = context.submitOnlineRefreshRecoverForm(recoverForm);
  await context.submitOnlineRefreshRecoverForm(recoverForm);
  assert.equal(repairPosts,1);
  assert.equal(recoverButton.disabled,true);
  finishRepair({ok:true,status:200,json:async()=>({ok:true,request_id:recoveryId})});
  await repair;
  assert.equal(recoverButton.disabled,false);
  assert.equal(elements.onlineRefreshPersistence.innerHTML,'');
  context.fetch=async()=>new Promise(resolve=>{finishRepair=resolve;});
  const lateRepair=context.submitOnlineRefreshRecoverForm(recoverForm);
  storage.value=JSON.stringify({request_id:'5'.repeat(32),proxy_id:''});
  elements.onlineRefreshStatusText.textContent='new request';
  finishRepair({ok:false,status:500,text:async()=>'old repair failed'});
  await lateRepair;
  assert.equal(elements.onlineRefreshStatusText.textContent,'new request');
  assert.equal(JSON.parse(storage.value).request_id,'5'.repeat(32));
  let finishOldQuery;
  context.fetch=async()=>new Promise(resolve=>{finishOldQuery=resolve;});
  const oldPageQuery=elements.onlineRefreshQuery.onclick();
  context.fetch=async url=>({ok:true,status:200,json:async()=>url.includes('refresh-history')
    ? {ok:true,items:[]}
    : {id:'4'.repeat(32),request_id:'5'.repeat(32),running:false,status:'结果未知',error:'current page diagnosis'}});
  vm.runInContext(input.script,context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(submitHandlers,5);
  assert.match(elements.onlineRefreshStatusText.innerHTML,/current page diagnosis/);
  const currentPageText=elements.onlineRefreshStatusText.innerHTML;
  finishOldQuery({ok:true,status:200,json:async()=>({id:'4'.repeat(32),request_id:'5'.repeat(32),running:true,status:'运行中',error:'stale page diagnosis'})});
  await oldPageQuery;
  assert.equal(elements.onlineRefreshStatusText.innerHTML,currentPageText);
  assert.equal(elements.onlineRefreshQuery.disabled,false);
  console.log('partial refresh retry action passed');
});
'''],
                input=json.dumps({"script": script, "reader": reader}),
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise refresh submission recovery")
    def test_refresh_form_ajax_submission_keeps_request_after_response_loss(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('class="action-row fls-online-refresh-form"', html)
            self.assertIn('name="request_id"', html)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "submitOnlineRefreshForm" in script
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(["node", "-e", r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const requestId = 'a'.repeat(32);
const inputId = {value: requestId};
const proxy = {value: 'proxy-a'};
const button = {disabled: false};
const form = {action: '/online-scripts/refresh', querySelector(selector) {
  if(selector.includes('request_id')) return inputId;
  if(selector.includes('proxy_id')) return proxy;
  return button;
}};
button.form = form;
const elements = {
  onlineRefreshStatusCard: {style: {}},
  onlineRefreshStatusText: {innerHTML: '', textContent: ''},
  onlineRefreshTaskLinkFailures: {innerHTML: ''},
  onlineRefreshBtn: button,
  onlineRefreshLogin: {hidden: true, href: ''},
};
const storage = {value: null, getItem(){return this.value;}, setItem(_key, value){this.value = value;}, removeItem(){this.value = null;}};
let calls = [];
let statusReads = 0;
let resolveInitialStatus;
const context = {
  window: {location: {pathname: '/online-scripts', search: ''}},
  location: {pathname: '/online-scripts', search: ''},
  document: {
    getElementById: id => elements[id],
    querySelectorAll: selector => selector.includes('fls-online-refresh-form') ? [button] : [],
    addEventListener: () => {},
  },
  sessionStorage: storage,
  encodeURIComponent,
  flsGetCsrfToken: () => 'csrf-refresh',
  flsResponseErrorDiagnostic: (_response, body) => ({payload: JSON.parse(body), message: JSON.parse(body).msg || ''}),
  FormData: class { constructor(value){this.value = value;} },
  fetch: async (url, options) => {
    calls.push({url, options});
    if(url === '/api/online-scripts/refresh-status' || url.startsWith('/api/online-scripts/refresh-status/')) {
      statusReads += 1;
      if(statusReads === 1) return new Promise(resolve => {resolveInitialStatus = resolve;});
      return {ok: true, status: 200, text: async () => JSON.stringify({id: 'b'.repeat(32), request_id: requestId, running: statusReads > 1, status: statusReads > 1 ? '运行中' : '', message: '', error: '', task_link_failures: []})};
    }
    assert.equal(url, '/api/online-scripts/refresh');
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['X-CSRF-Token'], 'csrf-refresh');
    return {ok: true, status: 202, text: async () => JSON.stringify({ok: true, id: 'b'.repeat(32), status: '运行中'})};
  },
  setTimeout: () => {},
};
vm.createContext(context);
vm.runInContext(input.reader + input.script, context);
(async()=>{
  await new Promise(resolve => setImmediate(resolve));
  await context.submitOnlineRefreshForm(form, {preventDefault(){}, stopImmediatePropagation(){}});
  assert.equal(calls.filter(item => item.url === '/api/online-scripts/refresh').length, 1);
  assert.equal(button.disabled, true);
  const pending = JSON.parse(storage.value);
  assert.equal(pending.request_id, requestId);
  assert.equal(pending.server_id, 'b'.repeat(32));
  assert.ok(calls.some(item => item.url === '/api/online-scripts/refresh-status/' + 'a'.repeat(32)));
  resolveInitialStatus({ok:true, status:200, text:async()=>JSON.stringify({running:false, status:'旧状态', message:'', error:''})});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(button.disabled, true);
  assert.match(elements.onlineRefreshStatusText.innerHTML, /正在后台同步/);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''], input=json.dumps({"script": script, "reader": reader}), text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_refresh_worker_persists_success_and_failure_without_losing_cache(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import ONLINE_REFRESH_STATE_FILE, get_online_refresh_state

            source.save_online_script_cache([{"id": "old", "name": "Old", "type": "raw", "link": "x"}])
            with patch.object(source, "fetch_online_scripts", return_value=[{"id": "new"}]):
                source.refresh_worker("r-success", log_file=str(base_dir / "success.log"))
            self.assertEqual(get_online_refresh_state()["status"], "已完成")
            self.assertEqual(json.loads((base_dir / "data" / "online_scripts_cache.json").read_text())[0]["id"], "new")
            self.assertEqual(json.loads(ONLINE_REFRESH_STATE_FILE.read_text())["id"], "r-success")

            source.save_online_script_cache([{"id": "stable", "name": "Stable", "type": "raw", "link": "x"}])
            with patch.object(source, "fetch_online_scripts", side_effect=TimeoutError("upstream timeout")):
                source.refresh_worker("r-failure", log_file=str(base_dir / "failure.log"))
            state = get_online_refresh_state()
            self.assertEqual(state["status"], "失败")
            self.assertIn("upstream timeout", state["error"])
            self.assertEqual(json.loads((base_dir / "data" / "online_scripts_cache.json").read_text())[0]["id"], "stable")
            payload = app.test_client().get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN}).json
            self.assertEqual(payload["status"], "失败")

    def test_cache_publish_then_status_write_failure_is_unknown_and_queryable(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import get_online_refresh_state

            source.save_online_script_cache([{"id": "old", "name": "Old", "type": "raw", "link": "x"}])
            real_update = source.update_online_refresh_state
            calls = 0

            def update(**changes):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("state disk full")
                return real_update(**changes)

            with patch.object(
                source,
                "fetch_online_scripts",
                return_value=[{"id": "new", "name": "New", "type": "raw", "link": "x"}],
            ), patch.object(source, "update_online_refresh_state", side_effect=update):
                source.refresh_worker("published-unknown", log_file=str(base_dir / "refresh.log"))

            state = get_online_refresh_state()
            self.assertEqual(state["status"], "结果未知")
            self.assertFalse(state["running"])
            self.assertTrue(state["cache_published"])
            self.assertIn("缓存已发布", state["error"])
            self.assertEqual(source.load_online_script_cache()[0]["id"], "new")
            self.assertEqual(json.loads((base_dir / "data" / "online_script_refresh.json").read_text())["status"], "结果未知")
            payload = app.test_client().get(
                "/api/online-scripts/refresh-status", headers={"X-Token": TOKEN},
            ).get_json()
            self.assertEqual(payload["status"], "结果未知")
            self.assertTrue(payload["cache_published"])
            page = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN}).get_data(as_text=True)
            self.assertIn("缓存已发布", page)

    def test_cache_publish_crash_can_be_confirmed_after_process_restart(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.online_scripts.constants import update_online_refresh_state

            request_id = "b" * 32
            refresh_id = "a" * 32
            original = [{"id": "old", "name": "Old", "type": "raw", "link": "x"}]
            published = [{"id": "new", "name": "New", "type": "raw", "link": "x"}]
            source.save_online_script_cache(original)
            update_online_refresh_state(
                id=refresh_id, request_id=request_id, running=True, status="运行中",
                expected_cache_fingerprint=source.online_script_cache_fingerprint(),
                source_fingerprint=source.online_script_source_fingerprint(), updated_at="before",
            )
            real_save = source.save_online_script_cache
            fetch = Mock(return_value=published)

            def publish_then_terminate(items):
                real_save(items)
                raise SystemExit("simulated worker termination after cache replacement")

            with patch.object(source, "fetch_online_scripts", fetch), patch.object(
                source, "save_online_script_cache", side_effect=publish_then_terminate,
            ):
                with self.assertRaisesRegex(SystemExit, "simulated worker termination"):
                    source.refresh_worker(refresh_id, log_file=str(base_dir / "refresh.log"))

            persisted = json.loads(constants.ONLINE_REFRESH_STATE_FILE.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "发布中")
            self.assertEqual(persisted["published_cache_fingerprint"], source.online_script_cache_fingerprint())
            constants.ONLINE_REFRESH_STATE.clear()
            constants.ONLINE_REFRESH_STATE.update(constants._refresh_snapshot({}))

            status = app.test_client().get(
                f"/api/online-scripts/refresh-status/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["status"], "结果未知")
            self.assertTrue(status.json["running"] is False)
            confirmed = app.test_client().post(
                f"/api/online-scripts/refresh-confirm/{request_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json["confirmation"], "published")
            self.assertEqual(confirmed.json["status"], "已确认发布")
            self.assertEqual([item["id"] for item in source.load_online_script_cache()], ["new"])
            fetch.assert_called_once()

    def test_terminal_success_remains_recoverable_when_history_publication_fails(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.online_scripts.constants import get_online_refresh_state, update_online_refresh_state
            from fls_manager.online_scripts.source import online_script_cache_fingerprint, online_script_source_fingerprint

            update_online_refresh_state(
                id="a" * 32, request_id="b" * 32, running=True, status="运行中",
                expected_cache_fingerprint=online_script_cache_fingerprint(),
                source_fingerprint=online_script_source_fingerprint(), updated_at="before",
            )
            items = [{"id": "published", "name": "Published", "type": "raw", "link": "https://example.invalid/new.py"}]
            real_replace = constants.os.replace
            publications = []

            def fail_terminal_history(src_path, dst):
                if Path(dst) == constants.ONLINE_REFRESH_HISTORY_FILE:
                    candidate = json.loads(Path(src_path).read_text(encoding="utf-8"))
                    publications.append(candidate.get("b" * 32, {}).get("status"))
                    if candidate.get("b" * 32, {}).get("status") == "已完成":
                        raise OSError("terminal history publication denied")
                return real_replace(src_path, dst)

            with patch.object(source, "fetch_online_scripts", return_value=items) as fetch, patch.object(
                constants.os, "replace", side_effect=fail_terminal_history,
            ):
                source.refresh_worker("a" * 32, log_file=str(base_dir / "log" / "terminal.log"))
            self.assertEqual(fetch.call_count, 1)
            self.assertIn("已完成", publications)
            self.assertEqual(json.loads(constants.ONLINE_REFRESH_STATE_FILE.read_text())["status"], "已完成")
            history_before = json.loads(constants.ONLINE_REFRESH_HISTORY_FILE.read_text())
            self.assertEqual(history_before["b" * 32]["status"], "发布中")
            visible = get_online_refresh_state()
            self.assertEqual(visible["status"], "已完成")
            self.assertFalse(visible["running"])
            self.assertIn("terminal history publication denied", visible["persistence_error"])
            self.assertTrue(visible["persistence_recovery"]["api_action"])
            self.assertEqual([item["id"] for item in source.load_online_script_cache()], ["published"])

            repaired = app.test_client().post(
                visible["persistence_recovery"]["api_action"], headers={"X-Token": TOKEN},
            )
            self.assertEqual(repaired.status_code, 200)
            self.assertEqual(repaired.json["status"], "已完成")
            self.assertFalse(repaired.json["running"])
            self.assertEqual(json.loads(constants.ONLINE_REFRESH_HISTORY_FILE.read_text())["b" * 32]["status"], "已完成")
            self.assertEqual(fetch.call_count, 1)

    def test_failed_worker_final_history_write_error_is_not_left_running(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.online_scripts.constants import get_online_refresh_state, update_online_refresh_state
            from fls_manager.online_scripts.source import online_script_cache_fingerprint, online_script_source_fingerprint

            update_online_refresh_state(
                id="a" * 32, request_id="b" * 32, running=True, status="运行中",
                expected_cache_fingerprint=online_script_cache_fingerprint(),
                source_fingerprint=online_script_source_fingerprint(), updated_at="before",
            )
            real_replace = constants.os.replace
            publications = []

            def fail_failed_history(src_path, dst):
                if Path(dst) == constants.ONLINE_REFRESH_HISTORY_FILE:
                    candidate = json.loads(Path(src_path).read_text(encoding="utf-8"))
                    publications.append(candidate.get("b" * 32, {}).get("status"))
                    if candidate.get("b" * 32, {}).get("status") == "失败":
                        raise OSError("failed history publication denied")
                return real_replace(src_path, dst)

            with patch.object(source, "fetch_online_scripts", side_effect=TimeoutError("upstream timeout")), patch.object(
                constants.os, "replace", side_effect=fail_failed_history,
            ):
                source.refresh_worker("a" * 32, log_file=str(base_dir / "log" / "failed.log"))
            self.assertIn("失败", publications)
            visible = get_online_refresh_state()
            self.assertEqual(visible["status"], "失败")
            self.assertFalse(visible["running"])
            self.assertIn("upstream timeout", visible["error"])
            self.assertIn("failed history publication denied", visible["persistence_error"])
            repaired = app.test_client().post(
                visible["persistence_recovery"]["api_action"], headers={"X-Token": TOKEN},
            )
            self.assertEqual(repaired.status_code, 200)
            self.assertEqual(repaired.json["status"], "失败")
            self.assertFalse(repaired.json["running"])
            self.assertEqual(json.loads(constants.ONLINE_REFRESH_HISTORY_FILE.read_text())["b" * 32]["status"], "失败")

    def test_refresh_worker_publishes_real_fetched_cache_only_once(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import get_online_refresh_state

            response = self.response('[{"id":"demo","name":"Demo","type":"raw","link":"https://example.invalid/demo.sh"}]')
            with patch.object(source, "requests", SimpleNamespace(get=Mock(return_value=response))), patch.object(
                source, "save_online_script_cache", wraps=source.save_online_script_cache,
            ) as save:
                source.refresh_worker("single-publish", log_file=str(base_dir / "refresh.log"))
            save.assert_called_once()
            self.assertEqual(source.load_online_script_cache()[0]["id"], "demo")
            self.assertEqual(get_online_refresh_state()["status"], "已完成")

    def test_refresh_worker_keeps_refresh_lock_before_storage_during_concurrent_status_read(self):
        with isolated_app() as (_app, base_dir):
            from contextlib import contextmanager

            from fls_manager.online_scripts import constants, source
            from fls_manager.online_scripts.source import (
                online_script_cache_fingerprint,
                online_script_source_fingerprint,
            )

            class TrackingRLock:
                def __init__(self):
                    self.lock = threading.RLock()
                    self.local = threading.local()

                def __enter__(self):
                    self.lock.acquire()
                    self.local.depth = getattr(self.local, "depth", 0) + 1
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    self.local.depth -= 1
                    self.lock.release()

                def owned_by_current_thread(self):
                    return getattr(self.local, "depth", 0) > 0

            refresh_lock = TrackingRLock()
            constants.update_online_refresh_state(
                id="a" * 32,
                request_id="b" * 32,
                running=True,
                status="运行中",
                expected_cache_fingerprint=online_script_cache_fingerprint(),
                source_fingerprint=online_script_source_fingerprint(),
                updated_at="before",
            )

            real_storage_transaction = source.storage_transaction
            publish_entered = threading.Event()
            allow_publish = threading.Event()
            query_started = threading.Event()
            violations = []
            query_results = []

            @contextmanager
            def checked_storage_transaction():
                if threading.current_thread().name == "refresh-worker":
                    if not refresh_lock.owned_by_current_thread():
                        violations.append("refresh worker acquired storage before refresh lock")
                        raise RuntimeError(violations[-1])
                    with real_storage_transaction():
                        publish_entered.set()
                        if not allow_publish.wait(timeout=5):
                            raise TimeoutError("cache publication gate timed out")
                        yield
                    return
                with real_storage_transaction():
                    yield

            def read_status():
                query_started.set()
                query_results.append(constants.get_online_refresh_state())

            with patch.object(constants, "ONLINE_REFRESH_LOCK", refresh_lock), patch.object(
                source, "ONLINE_REFRESH_LOCK", refresh_lock,
            ), patch.object(source, "storage_transaction", checked_storage_transaction), patch.object(
                source,
                "fetch_online_scripts",
                return_value=[{"id": "demo", "name": "Demo", "type": "raw", "link": "https://example.invalid/demo.sh"}],
            ):
                worker = threading.Thread(
                    name="refresh-worker",
                    target=source.refresh_worker,
                    args=("a" * 32, "", str(base_dir / "log" / "concurrent-refresh.log")),
                )
                worker.start()
                self.assertTrue(publish_entered.wait(timeout=3))
                reader = threading.Thread(name="refresh-status-reader", target=read_status)
                reader.start()
                self.assertTrue(query_started.wait(timeout=1))
                allow_publish.set()
                worker.join(timeout=5)
                reader.join(timeout=5)

            self.assertFalse(worker.is_alive(), "refresh worker stalled while publishing cache")
            self.assertFalse(reader.is_alive(), "status query stalled behind refresh publication")
            self.assertEqual(violations, [])
            self.assertEqual(len(query_results), 1)
            self.assertEqual(query_results[0]["request_id"], "b" * 32)
            self.assertEqual(constants.get_online_refresh_state()["status"], "已完成")
            self.assertEqual(source.load_online_script_cache()[0]["id"], "demo")

    def test_api_refresh_serializes_concurrent_starts_and_reports_start_failure(self):
        with isolated_app() as (app, _base_dir):
            import fls_manager.routes.online_scripts.refresh as refresh

            gate = threading.Event()
            started = threading.Event()
            completed = threading.Event()

            def blocked(refresh_id, *_args, **_kwargs):
                started.set()
                def finish():
                    gate.wait(timeout=3)
                    refresh.update_online_refresh_state(
                        id=refresh_id,
                        running=False,
                        status="已完成",
                        message="同步在线脚本源成功，共 0 条",
                        error="",
                        updated_at="2026-11-14 00:00:00",
                        log_file="",
                    )
                    completed.set()

                threading.Thread(target=finish, daemon=True).start()

            with patch.object(refresh, "start_refresh_thread", side_effect=blocked):
                first = app.test_client().post("/api/online-scripts/refresh", headers={"X-Token": TOKEN})
                self.assertEqual(first.status_code, 202)
                self.assertTrue(started.wait(1))
                second = app.test_client().post("/api/online-scripts/refresh", headers={"X-Token": TOKEN})
                self.assertEqual(second.status_code, 409)
                gate.set()
                self.assertTrue(completed.wait(1))

            with patch.object(refresh, "start_refresh_thread", side_effect=RuntimeError("thread quota exhausted")):
                result = app.test_client().post("/api/online-scripts/refresh", headers={"X-Token": TOKEN})
            self.assertEqual(result.status_code, 500)
            self.assertIn("thread quota exhausted", result.json["error"])
            self.assertEqual(app.test_client().get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN}).json["status"], "启动失败")

    def test_refresh_request_id_replays_terminal_job_and_queries_original_state(self):
        with isolated_app() as (app, _base_dir):
            import fls_manager.routes.online_scripts.refresh as refresh
            from fls_manager.online_scripts.constants import update_online_refresh_state

            request_id = "a" * 32
            client = app.test_client()
            with patch.object(refresh, "start_refresh_thread") as start:
                first = client.post(
                    "/api/online-scripts/refresh",
                    data={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 202)
                self.assertEqual(first.json["request_id"], request_id)
                update_online_refresh_state(
                    running=False, status="部分完成",
                    message="同步在线脚本源部分完成", error="", cache_published=True,
                    task_link_failures=[], updated_at="now", log_file="",
                )
                replay = client.post(
                    "/api/online-scripts/refresh",
                    data={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                status = client.get(
                    "/api/online-scripts/refresh-status/" + request_id,
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["replayed"])
            self.assertEqual(replay.json["id"], first.json["id"])
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json["request_id"], request_id)
            self.assertEqual(status.json["status"], "部分完成")
            start.assert_called_once()

    def test_refresh_request_id_unknown_and_binding_conflict_do_not_start_worker(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import save_proxies
            from fls_manager.online_scripts.constants import update_online_refresh_state
            import fls_manager.routes.online_scripts.refresh as refresh

            save_proxies([{"id": "chosen", "name": "Chosen", "type": "http",
                           "host": "127.0.0.1", "port": "8080", "enabled": True}])
            request_id = "b" * 32
            client = app.test_client()
            with patch.object(refresh, "start_refresh_thread") as start:
                first = client.post(
                    "/api/online-scripts/refresh",
                    data={"request_id": request_id, "proxy_id": "chosen"},
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(first.status_code, 202)
                update_online_refresh_state(
                    running=False, status="结果未知", message="", error="缓存已发布但回执未知",
                    cache_published=True, task_link_failures=[], updated_at="now", log_file="",
                )
                unknown = client.post(
                    "/api/online-scripts/refresh",
                    data={"request_id": request_id, "proxy_id": "chosen"},
                    headers={"X-Token": TOKEN},
                )
                conflict = client.post(
                    "/api/online-scripts/refresh",
                    data={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                invalid = client.post(
                    "/api/online-scripts/refresh",
                    data={"request_id": "bad"}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(unknown.status_code, 409)
            self.assertIn("缓存已发布", unknown.json["error"])
            self.assertEqual(conflict.status_code, 409)
            self.assertIn("绑定", conflict.json["error"])
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(invalid.json["request_id"], "")
            start.assert_called_once()

    def test_refresh_history_pruning_keeps_retry_parent_discoverable(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            parent_id = "a" * 32
            retry_id = "b" * 32
            records = {}

            def record(request_id, updated_at, *, status="已完成", retry_of=""):
                return {
                    "id": request_id,
                    "request_id": request_id,
                    "proxy_id": "",
                    "running": False,
                    "status": status,
                    "message": "",
                    "error": "",
                    "cache_published": status == "已完成",
                    "task_link_failures": [],
                    "updated_at": updated_at,
                    "log_file": "",
                    "retry_of": retry_of,
                    "expected_cache_fingerprint": "c" * 64,
                    "published_cache_fingerprint": "d" * 64 if status == "已完成" else "",
                    "source_fingerprint": "e" * 64,
                    "confirmation": "",
                    "confirmation_fingerprint": "",
                }

            records[parent_id] = record(parent_id, "2020-01-01", status="失败")
            records[retry_id] = record(retry_id, "2020-01-02", retry_of=parent_id)
            for number in range(1, 199):
                request_id = f"{number:032x}"
                records[request_id] = record(request_id, f"2026-01-{number:03d}")
            self.assertEqual(len(records), 200)
            constants.ONLINE_REFRESH_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            constants.ONLINE_REFRESH_HISTORY_FILE.write_text(
                json.dumps(records), encoding="utf-8",
            )

            constants.persist_online_refresh(record("c" * 32, "2026-11-21"))

            client = app.test_client()
            parent = client.get(
                "/api/online-scripts/refresh-history?request_id=" + parent_id,
                headers={"X-Token": TOKEN},
            )
            retry = client.get(
                "/api/online-scripts/refresh-history?request_id=" + retry_id,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(parent.status_code, 200)
            self.assertEqual(parent.json["items"][0]["request_id"], parent_id)
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(retry.json["items"][0]["retry_of"], parent_id)
            saved = constants._read_online_refresh_history_file()
            self.assertLessEqual(len(saved), 200)

    def test_refresh_retry_at_chain_capacity_is_rejected_before_claim_or_worker(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.routes.online_scripts import refresh

            source.save_online_script_cache([{
                "id": "stable", "name": "Stable", "type": "raw", "link": "x",
            }])
            cache_fingerprint = source.online_script_cache_fingerprint()
            source_fingerprint = source.online_script_source_fingerprint()
            records = {}
            previous_id = ""
            for number in range(1, constants.ONLINE_REFRESH_HISTORY_LIMIT + 1):
                request_id = f"{number:032x}"
                status = "失败" if number == constants.ONLINE_REFRESH_HISTORY_LIMIT else "已完成"
                records[request_id] = {
                    "id": request_id,
                    "request_id": request_id,
                    "proxy_id": "",
                    "running": False,
                    "status": status,
                    "message": "",
                    "error": "upstream timeout" if status == "失败" else "",
                    "cache_published": status == "已完成",
                    "task_link_failures": [],
                    "updated_at": f"2026-01-{number:03d}",
                    "log_file": "",
                    "retry_of": previous_id,
                    "expected_cache_fingerprint": cache_fingerprint,
                    "published_cache_fingerprint": cache_fingerprint if status == "已完成" else "",
                    "source_fingerprint": source_fingerprint,
                    "confirmation": "",
                    "confirmation_fingerprint": "",
                }
                previous_id = request_id
            constants.ONLINE_REFRESH_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            original_bytes = json.dumps(records).encode("utf-8")
            constants.ONLINE_REFRESH_HISTORY_FILE.write_bytes(original_bytes)
            state_before = dict(constants.ONLINE_REFRESH_STATE)

            client = app.test_client()
            retry_id = "f" * 32
            with patch.object(refresh, "start_refresh_thread") as start:
                response = client.post(
                    f"/api/online-scripts/refresh-retry/{previous_id}",
                    json={"retry_request_id": retry_id, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
            self.assertTrue(response.json["retry_not_started"])
            self.assertIn("未启动此次重试", response.json["error"])
            start.assert_not_called()

            form_client = app.test_client()
            with form_client.session_transaction() as session:
                session.update(
                    token=TOKEN, token_expire_at=4102444800, security_verified=True,
                    csrf_token="refresh-chain-csrf",
                )
            anonymous = app.test_client()
            with anonymous.session_transaction() as session:
                session["csrf_token"] = "anonymous-csrf"
            with patch.object(refresh, "start_refresh_thread") as form_start:
                unauthorized = anonymous.post(
                    f"/online-scripts/refresh-retry/{previous_id}",
                    data={"retry_request_id": "e" * 32, "proxy_id": "", "csrf_token": "anonymous-csrf"},
                )
                csrf_denied = form_client.post(
                    f"/online-scripts/refresh-retry/{previous_id}",
                    data={"retry_request_id": "e" * 32, "proxy_id": ""},
                )
                html = form_client.post(
                    f"/online-scripts/refresh-retry/{previous_id}",
                    data={"retry_request_id": "e" * 32, "proxy_id": "", "csrf_token": "refresh-chain-csrf"},
                )
                form_start.assert_not_called()
            self.assertEqual(unauthorized.status_code, 302)
            self.assertIn("/login", unauthorized.headers.get("Location", ""))
            self.assertEqual(csrf_denied.status_code, 400)
            self.assertEqual(html.status_code, 409)
            self.assertIn("未启动此次重试", html.get_data(as_text=True))
            self.assertIn('href="/online-scripts#online-status"', html.get_data(as_text=True))
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), original_bytes)
            self.assertNotIn(retry_id, constants._read_online_refresh_history_file())
            self.assertNotIn("e" * 32, constants._read_online_refresh_history_file())
            self.assertEqual(source.online_script_cache_fingerprint(), cache_fingerprint)
            self.assertEqual(constants.ONLINE_REFRESH_STATE, state_before)
            self.assertNotIn("persistence_error", constants.ONLINE_REFRESH_STATE)

            fresh_request_id = "d" * 32
            with patch.object(refresh, "start_refresh_thread") as fresh_start:
                fresh = client.post(
                    "/api/online-scripts/refresh",
                    json={"request_id": fresh_request_id, "proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(fresh.status_code, 202, fresh.get_data(as_text=True))
            fresh_start.assert_called_once()
            self.assertEqual(constants.ONLINE_REFRESH_STATE["request_id"], fresh_request_id)
            self.assertNotIn("persistence_error", constants.ONLINE_REFRESH_STATE)

    def test_original_refresh_replays_terminal_receipt_before_reading_changed_inputs(self):
        for status in ("已完成", "部分完成", "失败", "结果未知"):
            with self.subTest(status=status), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import constants, source
                from fls_manager.online_scripts.logs import online_refresh_log_file
                from fls_manager.routes.online_scripts import refresh

                request_id, refresh_id = "a" * 32, "b" * 32
                log_file = online_refresh_log_file(refresh_id)
                log_file.write_text("original refresh diagnosis\n", encoding="utf-8")
                constants.update_online_refresh_state(
                    request_id=request_id, id=refresh_id, proxy_id="deleted-proxy", running=False,
                    status=status, error="original diagnosis", message="original result",
                    log_file=str(log_file), updated_at="before",
                )
                state_before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
                history_before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
                client = app.test_client()
                with patch.object(refresh, "validate_proxy_id", side_effect=ValueError("proxy was deleted")) as proxy, patch.object(
                    refresh, "online_script_cache_fingerprint", side_effect=OSError("cache unreadable"),
                ) as cache, patch.object(
                    refresh, "online_script_source_fingerprint", side_effect=OSError("source unreadable"),
                ) as config, patch.object(refresh, "start_refresh_thread") as start:
                    response = client.post(
                        "/api/online-scripts/refresh", json={"request_id": request_id, "proxy_id": "deleted-proxy"},
                        headers={"X-Token": TOKEN},
                    )
                    html_response = client.post(
                        "/online-scripts/refresh", data={"request_id": request_id, "proxy_id": "deleted-proxy"},
                        headers={"X-Token": TOKEN},
                    )
                self.assertEqual(response.status_code, 200 if status in ("已完成", "部分完成") else 409)
                self.assertTrue(response.json["replayed"])
                self.assertEqual(response.json["request_id"], request_id)
                self.assertEqual(response.json["id"], refresh_id)
                self.assertEqual(response.json["status"], status)
                self.assertEqual(response.json["error"], "original diagnosis")
                self.assertEqual(response.json["state_url"], f"/api/online-scripts/refresh-status/{request_id}")
                self.assertEqual(response.json["log_url"], f"/online-scripts/refresh-log/{request_id}")
                self.assertEqual(html_response.status_code, response.status_code)
                self.assertIn("已查询原同步请求", html_response.get_data(as_text=True))
                self.assertIn("original diagnosis", html_response.get_data(as_text=True))
                self.assertIn(response.json["log_url"], html_response.get_data(as_text=True))
                proxy.assert_not_called()
                cache.assert_not_called()
                config.assert_not_called()
                start.assert_not_called()
                self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), state_before)
                self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)

    def test_original_refresh_can_replay_history_while_another_refresh_is_running(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import refresh

            original = "a" * 32
            constants.update_online_refresh_state(
                request_id=original, id="b" * 32, running=False, status="已完成", updated_at="old",
            )
            constants.update_online_refresh_state(
                request_id="c" * 32, id="d" * 32, running=True, status="运行中", updated_at="new",
            )
            before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
            with patch.object(refresh, "start_refresh_thread") as start:
                replay = app.test_client().post(
                    "/api/online-scripts/refresh", json={"request_id": original}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json["id"], "b" * 32)
            self.assertEqual(replay.json["request_id"], original)
            self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), before)
            start.assert_not_called()

    def test_original_refresh_concurrent_submission_claims_one_job(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import refresh

            barrier = threading.Barrier(2)
            real_fingerprint = refresh.online_script_cache_fingerprint
            responses, errors = [], []

            def fingerprint():
                barrier.wait(timeout=5)
                return real_fingerprint()

            def submit():
                try:
                    responses.append(app.test_client().post(
                        "/api/online-scripts/refresh", json={"request_id": "a" * 32}, headers={"X-Token": TOKEN},
                    ))
                except Exception as exc:
                    errors.append(exc)

            with patch.object(refresh, "online_script_cache_fingerprint", side_effect=fingerprint), patch.object(
                refresh, "start_refresh_thread",
            ) as start:
                threads = [threading.Thread(target=submit, daemon=True) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertEqual(sorted(response.status_code for response in responses), [202, 409])
                self.assertEqual(len({response.json["id"] for response in responses}), 1)
                start.assert_called_once()

    def test_original_refresh_replay_checks_auth_csrf_and_binding_without_mutation(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants
            from fls_manager.routes.online_scripts import refresh

            constants.update_online_refresh_state(
                request_id="a" * 32, id="b" * 32, proxy_id="original", running=True, status="运行中", updated_at="before",
            )
            constants.ONLINE_REFRESH_STATE.clear()
            before = constants.ONLINE_REFRESH_STATE_FILE.read_bytes()
            history_before = constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="original-csrf")
            anonymous = app.test_client()
            with anonymous.session_transaction() as session:
                session["csrf_token"] = "without-login"
            with patch.object(refresh, "start_refresh_thread") as start:
                denied = anonymous.post(
                    "/api/online-scripts/refresh", json={"request_id": "a" * 32},
                    headers={"X-CSRF-Token": "without-login"},
                )
                self.assertEqual(denied.status_code, 401)
                csrf_denied = client.post("/api/online-scripts/refresh", json={"request_id": "a" * 32})
                self.assertEqual(csrf_denied.status_code, 400)
                mismatch = client.post(
                    "/api/online-scripts/refresh", json={"request_id": "a" * 32, "proxy_id": "wrong"},
                    headers={"X-CSRF-Token": "original-csrf"},
                )
                self.assertEqual(mismatch.status_code, 409)
                self.assertIn("绑定", mismatch.json["error"])
                invalid = client.post(
                    "/api/online-scripts/refresh", json={"request_id": "c" * 32, "proxy_id": "missing"},
                    headers={"X-CSRF-Token": "original-csrf"},
                )
                self.assertEqual(invalid.status_code, 400)
                start.assert_not_called()
            self.assertEqual(constants.ONLINE_REFRESH_STATE_FILE.read_bytes(), before)
            self.assertEqual(constants.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)
            self.assertEqual(constants.ONLINE_REFRESH_STATE, {})
            with patch.object(refresh, "start_refresh_thread") as start:
                original_form = client.post(
                    "/online-scripts/refresh",
                    data={"request_id": "a" * 32, "proxy_id": "original", "csrf_token": "original-csrf"},
                )
            self.assertEqual(original_form.status_code, 409)
            self.assertIn("已查询原同步请求", original_form.get_data(as_text=True))
            self.assertIn("/online-scripts/refresh-log/" + "a" * 32, original_form.get_data(as_text=True))
            start.assert_not_called()


    def test_refresh_request_status_rejects_unknown_or_replaced_identifiers(self):
        with isolated_app() as (app, _base_dir):
            client = app.test_client()
            invalid = client.get(
                "/api/online-scripts/refresh-status/not-a-request",
                headers={"X-Token": TOKEN},
            )
            missing = client.get(
                "/api/online-scripts/refresh-status/" + "c" * 32,
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(missing.status_code, 404)
            self.assertIn("不存在", missing.json["error"])

    def test_form_and_api_start_failures_remain_retryable_even_when_state_write_fails(self):
        for path, success_status in (("/online-scripts/refresh", 302), ("/api/online-scripts/refresh", 202)):
            for failure in ("reserve", "thread", "thread-and-state"):
                with self.subTest(path=path, failure=failure), isolated_app() as (app, base_dir):
                    from fls_manager.online_scripts import constants, source
                    import fls_manager.routes.online_scripts.refresh as refresh

                    source.save_online_script_cache([{"id": "stable", "name": "Stable", "type": "raw", "link": "x"}])
                    from fls_manager.models import save_proxies
                    save_proxies([{"id": "chosen", "name": "Chosen", "type": "http",
                                   "host": "127.0.0.1", "port": "8080", "enabled": True}])
                    cache = base_dir / "data" / "online_scripts_cache.json"
                    original = cache.read_bytes()
                    real_persist = constants.persist_online_refresh
                    writes = 0

                    def persist(info):
                        nonlocal writes
                        writes += 1
                        if failure == "reserve" or (failure == "thread-and-state" and writes == 2):
                            raise OSError("state disk full")
                        return real_persist(info)

                    client = app.test_client()
                    headers = {"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"}
                    with patch.object(constants, "persist_online_refresh", side_effect=persist), patch.object(
                        refresh, "start_refresh_thread", side_effect=RuntimeError("thread quota exhausted"),
                    ) as start:
                        response = client.post(path, data={"proxy_id": "chosen"}, headers=headers)
                    self.assertEqual(response.status_code, 500)
                    if failure == "reserve":
                        start.assert_not_called()
                    else:
                        start.assert_called_once()
                    detail = response.json["error"] if response.is_json else response.get_data(as_text=True)
                    if failure != "thread":
                        self.assertIn("state disk full", detail)
                    if failure != "reserve":
                        self.assertIn("thread quota exhausted", detail)
                        self.assertEqual(constants.get_online_refresh_state()["status"], "启动失败")
                    if not response.is_json:
                        self.assertNotIn("Location", response.headers)
                        self.assertIn('action="/online-scripts/refresh"', detail)
                        self.assertIn('name="csrf_token"', detail)
                    self.assertFalse(constants.get_online_refresh_state()["running"])
                    self.assertEqual(cache.read_bytes(), original)
                    original_request_id = constants.ONLINE_REFRESH_STATE.get("request_id")
                    with patch.object(refresh, "start_refresh_thread") as retry_start:
                        retried = client.post(path, headers=headers)
                    self.assertEqual(retried.status_code, success_status)
                    retry_start.assert_called_once()
                    self.assertTrue(constants.get_online_refresh_state()["running"])
                    if failure == "thread-and-state":
                        previous = constants.get_online_refresh_request(original_request_id)
                        self.assertEqual(previous["status"], "启动失败")
                        self.assertFalse(previous["running"])
                        self.assertIn("thread quota exhausted", previous["error"])

    def test_refresh_retry_start_failure_keeps_terminal_state_and_reuses_same_request_id(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants, source
            from fls_manager.routes.online_scripts import refresh

            original_id = "a" * 32
            retry_id = "b" * 32
            source.save_online_script_cache([{"id": "stable", "name": "Stable", "type": "raw", "link": "x"}])
            constants.update_online_refresh_state(
                id="c" * 32, request_id=original_id, running=False, status="失败", error="upstream timeout",
                expected_cache_fingerprint=source.online_script_cache_fingerprint(),
                source_fingerprint=source.online_script_source_fingerprint(), updated_at="before",
            )
            real_persist = constants.persist_online_refresh

            def fail_startup_receipt(info):
                if info.get("request_id") == retry_id and info.get("status") == "启动失败":
                    raise OSError("retry status disk full")
                return real_persist(info)

            client = app.test_client()
            endpoint = f"/api/online-scripts/refresh-retry/{original_id}"
            payload = {"retry_request_id": retry_id, "proxy_id": ""}
            with patch.object(constants, "persist_online_refresh", side_effect=fail_startup_receipt), patch.object(
                refresh, "start_refresh_thread", side_effect=RuntimeError("thread quota exhausted"),
            ) as first_start:
                failed = client.post(endpoint, json=payload, headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            self.assertEqual(failed.json["request_id"], retry_id)
            self.assertIn("thread quota exhausted", failed.json["error"])
            self.assertIn("retry status disk full", failed.json["error"])
            first_start.assert_called_once()

            state = constants.get_online_refresh_state()
            self.assertEqual(state["request_id"], retry_id)
            self.assertEqual(state["retry_of"], original_id)
            self.assertEqual(state["status"], "启动失败")
            self.assertFalse(state["running"])
            self.assertIn("retry status disk full", state["persistence_error"])
            self.assertEqual(state["persistence_recovery"]["api_action"], f"/api/online-scripts/refresh-recover/{retry_id}")

            starter_entered = threading.Event()
            release_starter = threading.Event()
            responses = []

            def blocked_start(*_args):
                starter_entered.set()
                if not release_starter.wait(timeout=5):
                    raise RuntimeError("retry test starter was not released")

            first_client = app.test_client()
            concurrent_client = app.test_client()
            with patch.object(refresh, "start_refresh_thread", side_effect=blocked_start) as retry_start:
                worker = threading.Thread(
                    target=lambda: responses.append(first_client.post(endpoint, json=payload, headers={"X-Token": TOKEN})),
                    daemon=True,
                )
                worker.start()
                try:
                    self.assertTrue(starter_entered.wait(timeout=3))
                    replayed = concurrent_client.post(endpoint, json=payload, headers={"X-Token": TOKEN})
                    self.assertEqual(replayed.status_code, 200)
                    self.assertTrue(replayed.json["replayed"])
                    self.assertTrue(replayed.json["running"])
                    self.assertEqual(replayed.json["request_id"], retry_id)
                    retry_start.assert_called_once()
                finally:
                    release_starter.set()
                    worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
            self.assertEqual(len(responses), 1)
            retried = responses[0]
            self.assertEqual(retried.status_code, 202)
            self.assertEqual(retried.json["request_id"], retry_id)
            self.assertEqual(retried.json["retry_of"], original_id)
            self.assertEqual(constants.get_online_refresh_state()["request_id"], retry_id)
            self.assertTrue(constants.get_online_refresh_state()["running"])

    def test_form_and_api_share_conflict_admission_without_starting_a_second_worker(self):
        for first_path, next_path in (
            ("/online-scripts/refresh", "/api/online-scripts/refresh"),
            ("/api/online-scripts/refresh", "/online-scripts/refresh"),
        ):
            with self.subTest(first_path=first_path), isolated_app() as (app, _base_dir):
                import fls_manager.routes.online_scripts.refresh as refresh

                client = app.test_client()
                with patch.object(refresh, "start_refresh_thread") as start:
                    first = client.post(first_path, headers={"X-Token": TOKEN})
                    response = client.post(next_path, headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"})
                self.assertIn(first.status_code, (202, 302))
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("Location", response.headers)
                detail = response.json["error"] if response.is_json else response.get_data(as_text=True)
                self.assertIn("等待当前任务结束", detail)
                if not response.is_json:
                    self.assertIn('href="/online-scripts#online-status"', detail)
                start.assert_called_once()

    def test_refresh_error_form_retains_proxy_and_csrf_and_can_start_again(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import save_proxies
            import fls_manager.routes.online_scripts.refresh as refresh

            save_proxies([{"id": "chosen", "name": "Chosen", "type": "http",
                           "host": "127.0.0.1", "port": "8080", "enabled": True}])
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                               security_verified=True, csrf_token="refresh-csrf")
            headers = {"X-Requested-With": "FLS-Ajax"}
            with patch.object(refresh, "start_refresh_thread", side_effect=RuntimeError("thread quota exhausted")):
                failed = client.post(
                    "/online-scripts/refresh", data={"proxy_id": "chosen", "csrf_token": "refresh-csrf"}, headers=headers,
                )
            self.assertEqual(failed.status_code, 500)
            html = failed.get_data(as_text=True)
            self.assertIn('<option value="chosen" selected>', html)
            self.assertRegex(html, r'name="request_id" value="[a-f0-9]{32}"')
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
            with patch.object(refresh, "start_refresh_thread") as start:
                recovered = client.post(
                    "/online-scripts/refresh", data={"proxy_id": "chosen", "csrf_token": csrf}, headers=headers,
                )
            self.assertEqual(recovered.status_code, 302)
            self.assertEqual(start.call_args.args[1], "chosen")

    def test_refresh_rejects_invalid_proxy_before_reservation_or_worker(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.models import save_proxies
            import fls_manager.routes.online_scripts.refresh as refresh

            save_proxies([
                {"id": "disabled", "name": "Disabled", "type": "http",
                 "host": "127.0.0.1", "port": "8080", "enabled": False},
                {"id": "no-address", "name": "No address", "type": "http", "enabled": True},
            ])
            client = app.test_client()
            for path in ("/online-scripts/refresh", "/api/online-scripts/refresh"):
                for proxy_id in ("missing", "disabled", "no-address"):
                    with self.subTest(path=path, proxy_id=proxy_id):
                        with patch.object(refresh, "reserve_online_refresh") as reserve, patch.object(
                            refresh, "start_refresh_thread",
                        ) as start:
                            response = client.post(
                                path,
                                data={"proxy_id": proxy_id},
                                headers={"X-Token": TOKEN, "X-Requested-With": "FLS-Ajax"},
                            )
                        self.assertEqual(response.status_code, 400)
                        self.assertNotIn("Location", response.headers)
                        detail = response.get_json().get("error", "") if response.is_json else response.get_data(as_text=True)
                        self.assertIn("代理", detail)
                        reserve.assert_not_called()
                        start.assert_not_called()

    def test_refresh_status_returns_json_diagnostic_when_restart_recovery_cannot_persist(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants

            constants.ONLINE_REFRESH_STATE_FILE.write_text(json.dumps({
                "id": "stale", "running": True, "status": "运行中", "message": "正在同步",
                "error": "", "log_file": str(base_dir / "stale.log"),
            }), encoding="utf-8")
            client = app.test_client()
            with patch.object(constants, "persist_online_refresh", side_effect=OSError("state read-only")):
                failed = client.get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            self.assertFalse(failed.json["ok"])
            self.assertIn("state read-only", failed.json["error"])
            recovered = client.get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN})
            self.assertEqual(recovered.status_code, 200)
            self.assertFalse(recovered.json["running"])
            self.assertEqual(recovered.json["status"], "结果未知")

    def test_refresh_form_and_api_reject_csrf_expired_and_forbidden_requests_before_reservation(self):
        for path in ("/online-scripts/refresh", "/api/online-scripts/refresh"):
            with self.subTest(path=path), isolated_app() as (app, _base_dir):
                import fls_manager.routes.online_scripts.refresh as refresh

                client = app.test_client()
                headers = {"X-Requested-With": "FLS-Ajax"}
                with client.session_transaction() as session:
                    session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                   security_verified=True, csrf_token="refresh-csrf")
                with patch.object(refresh, "reserve_online_refresh") as reserve, patch.object(refresh, "start_refresh_thread") as start:
                    denied = client.post(path, headers=headers)
                    self.assertEqual(denied.status_code, 400)
                    with client.session_transaction() as session:
                        session["token_expire_at"] = 1
                    expired = client.post(path, data={"csrf_token": "refresh-csrf"}, headers=headers)
                    self.assertEqual(expired.status_code, 401)
                    with client.session_transaction() as session:
                        session.update(token=TOKEN, token_expire_at=int(time.time()) + 3600,
                                       security_verified=True, csrf_token="refresh-csrf")
                    with patch("fls_manager.auth.query_token_enabled", return_value=False):
                        forbidden = client.post(
                            path, query_string={"token": TOKEN}, data={"csrf_token": "refresh-csrf"}, headers=headers,
                        )
                    self.assertEqual(forbidden.status_code, 403)
                    reserve.assert_not_called()
                    start.assert_not_called()

    def test_restart_running_refresh_becomes_unknown_and_page_shows_it(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_REFRESH_STATE_FILE, get_online_refresh_state

            ONLINE_REFRESH_STATE_FILE.write_text(json.dumps({
                "id": "stale", "running": True, "status": "运行中", "message": "正在同步",
                "error": "", "updated_at": "2026-11-14 00:00:00", "log_file": str(base_dir / "stale.log"),
            }), encoding="utf-8")
            state = get_online_refresh_state()
            self.assertFalse(state["running"])
            self.assertEqual(state["status"], "结果未知")
            self.assertIn("面板重启后", state["error"])
            page = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("结果未知", page.get_data(as_text=True))

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the polling script")
    def test_refresh_polling_surfaces_http_error_and_restores_retry_button(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            scripts = re.findall(r"<script>(.*?)</script>", response.get_data(as_text=True), re.S)
            script = next(script for script in scripts if "async function updateOnlineRefreshStatus(" in script)
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
(async () => {
for(const status of [401, 403, 409, 500, 200]) {
 const elements = {
  onlineRefreshStatusCard: {style: {}},
  onlineRefreshStatusText: {innerHTML: ''},
  onlineRefreshBtn: {disabled: true, textContent: '正在同步中...'},
  onlineRefreshLogin: {hidden: true, href: ''},
  onlineRefreshQuery: {hidden: true, disabled: false},
};
const context = {
  window: {},
  location: {pathname: '/online-scripts', search: ''},
  document: {getElementById: id => elements[id]},
  fetch: async () => ({ok: status === 200, status, json: async () => {
    if(status === 200) throw new Error('not JSON');
    return {msg: 'backend diagnostic'};
  }}),
};
vm.createContext(context);
vm.runInContext(input.reader + input.script, context);
await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.onlineRefreshStatusCard.style.display, 'block');
  assert.ok(elements.onlineRefreshStatusText.innerHTML.includes('HTTP ' + status));
  assert.match(elements.onlineRefreshStatusText.innerHTML, status === 200 ? /响应解析失败/ : /backend diagnostic/);
  if(status === 401 || status === 403) assert.match(elements.onlineRefreshStatusText.innerHTML, /重新登录/);
  else assert.match(elements.onlineRefreshStatusText.innerHTML, /重新查询同步状态/);
  assert.equal(elements.onlineRefreshBtn.disabled, false);
  assert.equal(elements.onlineRefreshBtn.textContent, '同步在线脚本源');
  assert.equal(elements.onlineRefreshQuery.hidden, false);
  assert.equal(elements.onlineRefreshQuery.disabled, false);
  assert.equal(typeof elements.onlineRefreshQuery.onclick, 'function');
  assert.equal(elements.onlineRefreshLogin.hidden, !(status === 401 || status === 403));
  if(status === 401 || status === 403) assert.match(elements.onlineRefreshLogin.href, /\/login\?next=/);
}
  console.log('refresh polling error recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
                input=json.dumps({"script": script, "reader": reader}),
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise automatic status recovery")
    def test_refresh_status_retries_only_the_original_read_and_stops_at_boundaries(self):
        with isolated_app() as (app, _base_dir):
            request_id = "b" * 32
            server_id = "c" * 32
            html = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN}).get_data(as_text=True)
            script = next(
                script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "async function updateOnlineRefreshStatus(options)" in script
            )
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const requestId='b'.repeat(32),serverId='c'.repeat(32);
const settle=async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};
const response=(status,body)=>({ok:status===200,status,headers:{get:()=> 'application/json'},text:async()=>JSON.stringify(body)});
const temporary=status=>response(status,{error:'temporary gateway failure'});
function setup(queue){
 const elements={
  onlineRefreshStatusCard:{style:{}},onlineRefreshStatusText:{innerHTML:''},
  onlineRefreshBtn:{disabled:true,textContent:'正在同步中...'},
  onlineRefreshLogin:{hidden:true,href:''},onlineRefreshQuery:{hidden:true,disabled:false},
  onlineRefreshLogLink:{hidden:true,href:''},onlineRefreshTaskLinkFailures:{innerHTML:''},
  onlineRefreshPersistence:{innerHTML:''},onlineRefreshHistory:{innerHTML:'',querySelectorAll:()=>[]},
  onlineRefreshHistoryError:{hidden:true,textContent:''},
 };
 const storage={data:{'fls-online-refresh-request':JSON.stringify({request_id:requestId,proxy_id:'',server_id:serverId})},
  getItem(key){return this.data[key]||null;},setItem(key,value){this.data[key]=value;},removeItem(key){delete this.data[key];}};
 const timers=[];let pagehide=null,pageshow=null,gets=0,posts=0;const urls=[],methods=[];
 const context={window:{addEventListener:(name,fn)=>{if(name==='pagehide')pagehide=fn;if(name==='pageshow')pageshow=fn;}},
  location:{pathname:'/online-scripts',search:''},document:{getElementById:id=>elements[id],querySelectorAll:()=>[],
   addEventListener:()=>{},createElement:name=>({tagName:name,href:'',textContent:''}),createTextNode:text=>({textContent:text})},
  sessionStorage:storage,encodeURIComponent,URL,Math,Number,Array,String,Boolean,JSON,
  fetch:async(url,options)=>{
   if(String(url).includes('/refresh-history')) return response(200,{ok:true,items:[]});
   urls.push(String(url));methods.push(options&&options.method);
   if(options&&String(options.method||'GET').toUpperCase()==='POST')posts++;
   gets++;
   const item=queue.shift();if(item===undefined)throw new Error('unexpected status read '+url);
   if(item.network)throw new TypeError('network offline');return item;
  },
  setTimeout:(fn,delay)=>{const timer={fn,delay};timers.push(timer);return timer;},
  clearTimeout:timer=>{const index=timers.indexOf(timer);if(index>=0)timers.splice(index,1);},
  flsResponseErrorDiagnostic:(_response,body)=>{let payload=null;try{payload=JSON.parse(body);}catch(_){}return {payload,message:payload&&(payload.error||payload.msg)||''};},
 };
 vm.createContext(context);vm.runInContext(input.reader+input.script,context);
 return {context,elements,storage,timers,gets:()=>gets,posts:()=>posts,urls,methods,pagehide:()=>pagehide&&pagehide(),
  pageshow:event=>pageshow&&pageshow(event),
  runTimer:async()=>{const timer=timers.shift();assert.ok(timer);await timer.fn();await settle();return timer.delay;}};
}
const running=response(200,{ok:true,id:serverId,request_id:requestId,running:true,status:'运行中',message:'still running',error:'',task_link_failures:[]});
const terminal=response(200,{ok:true,id:serverId,request_id:requestId,running:false,status:'失败',message:'',error:'original failure',task_link_failures:[]});
(async()=>{
 const recovered=setup([temporary(503),{network:true},temporary(408),temporary(429),running,terminal]);
 await settle();
 assert.equal(recovered.gets(),1);assert.equal(recovered.timers[0].delay,2000);
 assert.match(recovered.elements.onlineRefreshStatusText.innerHTML,/1\/5/);
 assert.equal(recovered.elements.onlineRefreshQuery.hidden,false);
 const first=recovered.context.updateOnlineRefreshStatus();
 const duplicate=recovered.context.updateOnlineRefreshStatus();
 await Promise.all([first,duplicate]);await settle();
 assert.equal(recovered.gets(),2,'concurrent manual reads must be single-flight');
 assert.equal(recovered.timers[0].delay,2000,'manual read resets the backoff');
 assert.match(recovered.elements.onlineRefreshStatusText.innerHTML,/1\/5/);
 assert.equal(await recovered.runTimer(),2000);
 assert.equal(recovered.gets(),3);assert.equal(recovered.timers[0].delay,4000);
 assert.equal(await recovered.runTimer(),4000);
 assert.equal(recovered.gets(),4);assert.equal(recovered.timers[0].delay,8000);
 assert.equal(await recovered.runTimer(),8000);
 assert.equal(recovered.gets(),5);assert.equal(recovered.elements.onlineRefreshStatusText.innerHTML.includes('HTTP 429'),false);
 assert.equal(recovered.timers[0].delay,2000,'a valid running response resets the failure count');
 assert.equal(await recovered.runTimer(),2000);
 assert.equal(recovered.gets(),6);assert.equal(recovered.timers.length,0,'terminal response clears polling');
 assert.equal(recovered.elements.onlineRefreshStatusText.innerHTML.includes('original failure'),true);
 assert.equal(recovered.posts(),0,'status recovery must never resubmit synchronization');
 assert.deepEqual(recovered.urls,Array(6).fill('/api/online-scripts/refresh-status/'+requestId));
 assert.ok(recovered.methods.every(method=>method===undefined),'status recovery must remain GET-only');

 for(const status of [401,403,404]){
  const body=status===404?{error:'original request missing',request_id:requestId}:{error:'access denied'};
  const stopped=setup([response(status,body)]);await settle();
  assert.equal(stopped.gets(),1);assert.equal(stopped.timers.length,0);
  assert.equal(stopped.elements.onlineRefreshLogin.hidden,!(status===401||status===403));
  assert.equal(JSON.parse(stopped.storage.getItem('fls-online-refresh-request')).request_id,requestId);
  if(status===404){
   assert.equal(stopped.context.onlineRefreshMissingRequest,true);
   assert.match(stopped.elements.onlineRefreshBtn.textContent,/明确重新同步/);
  }
 }

 const bounded=setup([...Array.from({length:6},()=>temporary(503)),running,terminal]);await settle();
 const delays=[];
 while(bounded.timers.length) delays.push(await bounded.runTimer());
 assert.deepEqual(delays,[2000,4000,8000,16000,30000]);
 assert.equal(bounded.gets(),6);assert.match(bounded.elements.onlineRefreshStatusText.innerHTML,/次数已达上限/);
 assert.equal(bounded.timers.length,0);
 await bounded.context.updateOnlineRefreshStatus();await settle();
 assert.equal(bounded.gets(),7,'manual recovery remains available after the retry bound');
 assert.equal(bounded.timers[0].delay,2000);
 bounded.pagehide();
 assert.equal(bounded.timers.length,0,'pagehide clears the scheduled status read');
 bounded.pageshow({persisted:true});await settle();
 assert.equal(bounded.gets(),8,'restoring a cached page resumes the original status read');
 assert.equal(bounded.timers.length,0);
 assert.equal(bounded.posts(),0);
 console.log('online refresh status retry/recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''],
                input=json.dumps({"script": script, "reader": reader}),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise history-read recovery")
    def test_refresh_history_read_failure_preserves_rows_and_has_single_flight_get_retry(self):
        with isolated_app() as (app, _base_dir):
            html = app.test_client().get("/online-scripts", headers={"X-Token": TOKEN}).get_data(as_text=True)
            script = next(script for script in re.findall(r"<script>(.*?)</script>", html, re.S)
                          if "async function updateOnlineRefreshHistory()" in script)
            start = script.index("function hideOnlineRefreshHistoryReadActions()")
            end = script.index("\nfunction randomOnlineRefreshRequestId()", start)
            history_script = script[start:end]
            binding_start = script.index("var onlineRefreshQuery = document.getElementById(\"onlineRefreshQuery\")")
            binding_end = script.index("\nupdateOnlineRefreshStatus();", binding_start)
            binding = script[binding_start:binding_end]
            static = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            reader = static[static.index("async function flsReadJsonResponse("):static.index("async function flsReadTextResponse(")]
            result = subprocess.run(
                ["node", "-e", r'''
const vm=require('node:vm'),assert=require('node:assert/strict');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const settle=async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};
const response=(status,body,type='application/json')=>({ok:status>=200&&status<300,status,
 headers:{get:()=>type},text:async()=>typeof body==='string'?body:JSON.stringify(body)});
function setup(queue){
 const row='<div id="old-history">previous valid history</div>';
 const elements={onlineRefreshHistory:{innerHTML:row},onlineRefreshHistoryError:{hidden:true,textContent:''},
  onlineRefreshHistoryRetry:{hidden:true,disabled:false},onlineRefreshHistoryLogin:{hidden:true,href:''},onlineRefreshQuery:{hidden:true}};
 const reads=[];
 const context={window:{},location:{pathname:'/online-scripts',search:'?refresh_request_id='+ 'a'.repeat(32)},
  document:{getElementById:id=>elements[id]},onlineRefreshHistoryControl:{pending:false,generation:0},
  fetch:async(url,options)=>{reads.push({url:String(url),options});const next=queue.shift();return next&&next.promise?await next.promise:next;},
  flsReadJsonResponse:async response=>{
   if(!response.ok){let body='';try{body=await response.text();}catch(_){}
    let payload=null;try{payload=JSON.parse(body);}catch(_){}
    const error=new Error('HTTP '+response.status+(payload&&payload.error?': '+payload.error:''));
    error.status=response.status;error.payload=payload;throw error;}
   try{return JSON.parse(await response.text());}catch(_){const error=new Error('响应解析失败：'+response.status);error.status=response.status;throw error;}
  },
  escapeHtml:value=>String(value==null?'':value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])),
  bindOnlineRefreshRetryForms:()=>{},bindOnlineRefreshConfirmForms:()=>{},updateOnlineRefreshStatus:async()=>{},
  encodeURIComponent,Array,String,Number,Boolean,JSON,Math,
 };
 vm.createContext(context);vm.runInContext(input.historyScript,context);vm.runInContext(input.binding,context);
 return {context,elements,reads,row};
}
(async()=>{
 let release;
 const pending=new Promise(resolve=>release=resolve);
 const concurrent=setup([{promise:pending}]);
 const first=concurrent.context.updateOnlineRefreshHistory();
 const duplicate=concurrent.context.updateOnlineRefreshHistory();
 assert.equal(concurrent.reads.length,1,'concurrent history reads must be single-flight');
 assert.equal(concurrent.elements.onlineRefreshHistoryRetry.disabled,true);
 release(response(503,{error:'temporary history outage'}));
 await Promise.all([first,duplicate]);await settle();
 assert.equal(concurrent.elements.onlineRefreshHistory.innerHTML,concurrent.row,'failed GET must preserve rendered history');
 assert.match(concurrent.elements.onlineRefreshHistoryError.textContent,/HTTP 503.*temporary history outage/);
 assert.equal(concurrent.elements.onlineRefreshHistoryRetry.hidden,false);
 assert.equal(concurrent.elements.onlineRefreshHistoryRetry.disabled,false);
 assert.equal(concurrent.elements.onlineRefreshHistoryLogin.hidden,true);
 assert.equal(typeof concurrent.elements.onlineRefreshHistoryRetry.onclick,'function');

 concurrent.reads.length=0;
 const recovery=setup([response(503,{error:'temporary history outage'}),response(200,{ok:true,items:[
  {request_id:'b'.repeat(32),status:'已完成',updated_at:'now',task_link_failure_count:0}
 ]})]);
 await recovery.context.updateOnlineRefreshHistory();
 const retry=recovery.elements.onlineRefreshHistoryRetry.onclick();
 const duplicateRetry=recovery.context.updateOnlineRefreshHistory();
 await Promise.all([retry,duplicateRetry]);await settle();
 assert.equal(recovery.reads.length,2);
 assert.ok(recovery.reads.every(read=>read.url==='/api/online-scripts/refresh-history?limit=8'));
 assert.ok(recovery.reads.every(read=>read.options.cache==='no-store'&&read.options.credentials==='same-origin'));
 assert.match(recovery.elements.onlineRefreshHistory.innerHTML,/refresh-log\/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/);
 assert.equal(recovery.elements.onlineRefreshHistoryError.hidden,true);
 assert.equal(recovery.elements.onlineRefreshHistoryRetry.hidden,true);
 assert.equal(recovery.elements.onlineRefreshHistoryLogin.hidden,true);
 assert.ok(recovery.reads.every(read=>!read.options.method||read.options.method==='GET'));

 for(const invalid of [
  response(503,'<html>proxy unavailable</html>','text/html'),
  response(200,'not-json'),
  response(200,{ok:true,items:{}}),
  response(200,{ok:true,items:[{}]}),
  response(200,{ok:false,error:'invalid response',items:[]}),
 ]){
  const broken=setup([invalid]);await broken.context.updateOnlineRefreshHistory();
  assert.equal(broken.elements.onlineRefreshHistory.innerHTML,broken.row);
  assert.equal(broken.elements.onlineRefreshHistoryRetry.hidden,false);
  assert.equal(broken.elements.onlineRefreshHistoryError.hidden,false);
 }

 for(const status of [401,403]){
  const denied=setup([response(status,{error:'authentication required'})]);
  await denied.context.updateOnlineRefreshHistory();
  assert.equal(denied.elements.onlineRefreshHistory.innerHTML,denied.row);
  assert.equal(denied.elements.onlineRefreshHistoryRetry.hidden,false);
  assert.equal(denied.elements.onlineRefreshHistoryLogin.hidden,false);
  assert.equal(denied.elements.onlineRefreshHistoryLogin.href,
   '/login?next=%2Fonline-scripts%3Frefresh_request_id%3D'+'a'.repeat(32));
 }
 console.log('online refresh history read recovery passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''],
                input=json.dumps({"historyScript": history_script, "binding": binding}),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise the response reader")
    def test_response_readers_preserve_bounded_non_json_http_diagnostic(self):
        source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
        diagnostic = source[source.index("function flsResponseErrorDiagnostic("):source.index("async function flsReadJsonResponse(")]
        reader = source[source.index("async function flsReadJsonResponse("):source.index("async function flsReadTextResponse(")]
        text_reader = source[source.index("async function flsReadTextResponse("):source.index('document.addEventListener("submit"')]
        result = subprocess.run(
            ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const context = {};
vm.createContext(context);
vm.runInContext(input.diagnostic+input.reader+input.textReader, context);
(async () => {
  for(const status of [401, 403, 409, 500, 502, 503]){
    let reads = 0;
    const response = {ok:false, status, headers:{get:()=> 'text/plain'},
      text:async()=>{reads++; return 'upstream diagnostic '+status;}};
    let error;
    try { await context.flsReadJsonResponse(response); } catch(e) { error = e; }
    assert.equal(error.status, status);
    assert.match(error.message, new RegExp('HTTP '+status));
    assert.match(error.message, /upstream diagnostic/);
    assert.equal(reads, 1);
  }
  let htmlError;
  try {
    await context.flsReadJsonResponse({ok:false,status:502,headers:{get:()=> 'text/html'},
      text:async()=> '<html><body><h1>Gateway unavailable</h1><script>private marker</script></body></html>'});
  } catch(e) { htmlError = e; }
  assert.match(htmlError.message, /Gateway unavailable/);
  assert.doesNotMatch(htmlError.message, /private marker|<script/i);
  let longError;
  try {
    await context.flsReadJsonResponse({ok:false,status:502,headers:{get:()=> 'text/plain'},
      text:async()=> 'x'.repeat(2000)});
  } catch(e) { longError = e; }
  assert.equal(longError.message.length, 510);
  const success = await context.flsReadJsonResponse({ok:true,status:200,headers:{get:()=> 'application/json'},
    text:async()=> '{"ok":true}'});
  assert.equal(JSON.stringify(success), '{"ok":true}');
  let textError;
  try {
    await context.flsReadTextResponse({ok:false,status:503,headers:{get:()=> 'text/plain'},
      text:async()=> 'task log temporarily unavailable'});
  } catch(e) { textError = e; }
  assert.equal(textError.status, 503);
  assert.match(textError.message, /task log temporarily unavailable/);
  console.log('JSON response diagnostics passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
            input=json.dumps({"diagnostic": diagnostic, "reader": reader, "textReader": text_reader}),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
