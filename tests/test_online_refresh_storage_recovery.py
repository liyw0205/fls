import json
import re
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineRefreshStorageRecoveryTests(unittest.TestCase):
    def _seed_refresh(self, state, request_id="a" * 32):
        from fls_manager.online_scripts.source import online_script_cache_fingerprint, online_script_source_fingerprint

        state.update_online_refresh_state(
            id="b" * 32, request_id=request_id, running=False, status="失败", error="original failure",
            expected_cache_fingerprint=online_script_cache_fingerprint(),
            source_fingerprint=online_script_source_fingerprint(), updated_at="before",
        )

    def test_partial_claim_publication_keeps_original_request_queryable_without_launching(self):
        for retry in (False, True):
            with self.subTest(retry=retry), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import constants as state
                from fls_manager.online_scripts.source import online_script_cache_fingerprint, online_script_source_fingerprint
                from fls_manager.routes.online_scripts import refresh

                original = "a" * 32
                state.update_online_refresh_state(
                    id="b" * 32, request_id=original, running=False, status="失败", error="original failure",
                    expected_cache_fingerprint=online_script_cache_fingerprint(),
                    source_fingerprint=online_script_source_fingerprint(), updated_at="before",
                )
                real_replace = state.os.replace

                def fail_history(src, dst):
                    if Path(dst) == state.ONLINE_REFRESH_HISTORY_FILE:
                        raise OSError("history publication denied")
                    return real_replace(src, dst)

                client = app.test_client()
                request_id = "c" * 32
                endpoint = f"/api/online-scripts/refresh-retry/{original}" if retry else "/api/online-scripts/refresh"
                values = {"retry_request_id" if retry else "request_id": request_id, "proxy_id": ""}
                headers = {"X-Token": TOKEN}
                cache = base_dir / "data" / "online_scripts_cache.json"
                cache_before = cache.read_bytes() if cache.exists() else None
                with patch.object(state.os, "replace", side_effect=fail_history), patch.object(refresh, "start_refresh_thread") as start:
                    failed = client.post(endpoint, json=values, headers=headers)
                    self.assertEqual(failed.status_code, 500)
                    actual = json.loads(state.ONLINE_REFRESH_STATE_FILE.read_text(encoding="utf-8"))
                    self.assertEqual(actual["request_id"], request_id)
                    status = client.get(f"/api/online-scripts/refresh-status/{request_id}", headers=headers)
                    self.assertEqual(status.status_code, 200)
                    self.assertEqual(status.json["request_id"], request_id)
                    self.assertFalse(status.json["running"])
                    self.assertIn("history publication denied", status.json["persistence_error"])
                    self.assertTrue(status.json["persistence_recovery"]["api_action"])
                    self.assertTrue(Path(status.json["log_file"]).is_file())
                    self.assertIn("未启动抓取", Path(status.json["log_file"]).read_text(encoding="utf-8"))
                    replay = client.post(endpoint, json=values, headers=headers)
                    self.assertIn(replay.status_code, (200, 409))
                    self.assertEqual(replay.json["id"], actual["id"])
                    other = client.post("/api/online-scripts/refresh", json={"request_id": "d" * 32}, headers=headers)
                    self.assertEqual(other.status_code, 500)
                    self.assertEqual(json.loads(state.ONLINE_REFRESH_STATE_FILE.read_text(encoding="utf-8"))["request_id"], request_id)
                    start.assert_not_called()
                with patch.object(refresh, "start_refresh_thread") as start:
                    for new_endpoint, new_values in (
                        ("/api/online-scripts/refresh", {"request_id": "d" * 32}),
                        (f"/api/online-scripts/refresh-retry/{original}", {"retry_request_id": "e" * 32}),
                    ):
                        blocked = client.post(new_endpoint, json=new_values, headers=headers)
                        self.assertEqual(blocked.status_code, 500)
                        self.assertIn("先修复原刷新回执", blocked.json["error"])
                    start.assert_not_called()
                recovered = client.post(status.json["persistence_recovery"]["api_action"], headers=headers)
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.json["request_id"], request_id)
                self.assertFalse(recovered.json["running"])
                history = json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8"))
                self.assertEqual(history[request_id]["id"], actual["id"])
                if cache_before is None:
                    self.assertFalse(cache.exists())
                else:
                    self.assertEqual(cache.read_bytes(), cache_before)
                with patch.object(refresh, "start_refresh_thread") as start:
                    newer = client.post(
                        "/api/online-scripts/refresh", json={"request_id": "d" * 32}, headers=headers,
                    )
                    self.assertEqual(newer.status_code, 202)
                    self.assertEqual(start.call_count, 1)
                archived = client.get(f"/api/online-scripts/refresh-status/{request_id}", headers=headers)
                self.assertEqual(archived.status_code, 200)
                self.assertFalse(archived.json["running"])
                self.assertEqual(archived.json["status"], "结果未知")
                self.assertIn("未启动抓取", archived.json["error"])

    def test_current_state_replace_failure_leaves_old_receipt_and_same_request_can_retry(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants as state
            from fls_manager.routes.online_scripts import refresh

            self._seed_refresh(state)
            state_before = state.ONLINE_REFRESH_STATE_FILE.read_bytes()
            history_before = state.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            cache = base_dir / "data" / "online_scripts_cache.json"
            cache_before = cache.read_bytes() if cache.exists() else None
            real_replace = state.os.replace

            def fail_current(src, dst):
                if Path(dst) == state.ONLINE_REFRESH_STATE_FILE:
                    raise OSError("current state publication denied")
                return real_replace(src, dst)

            client = app.test_client()
            values = {"request_id": "c" * 32, "proxy_id": ""}
            with patch.object(state.os, "replace", side_effect=fail_current), patch.object(
                refresh, "start_refresh_thread",
            ) as start:
                failed = client.post("/api/online-scripts/refresh", json=values, headers={"X-Token": TOKEN})
            self.assertEqual(failed.status_code, 500)
            self.assertEqual(state.ONLINE_REFRESH_STATE_FILE.read_bytes(), state_before)
            self.assertEqual(state.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)
            start.assert_not_called()
            if cache_before is None:
                self.assertFalse(cache.exists())
            else:
                self.assertEqual(cache.read_bytes(), cache_before)

            with patch.object(refresh, "start_refresh_thread") as start:
                retried = client.post("/api/online-scripts/refresh", json=values, headers={"X-Token": TOKEN})
            self.assertEqual(retried.status_code, 202)
            self.assertEqual(retried.json["request_id"], values["request_id"])
            self.assertEqual(start.call_count, 1)
            replay = client.post("/api/online-scripts/refresh", json=values, headers={"X-Token": TOKEN})
            self.assertEqual(replay.status_code, 409)
            self.assertIn("正在同步", replay.json["error"])
            self.assertEqual(start.call_count, 1)

    def test_history_replace_error_after_publication_retains_claim_until_repair(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts import constants as state
            from fls_manager.routes.online_scripts import refresh

            self._seed_refresh(state)
            real_replace = state.os.replace

            def replace_then_fail_history(src, dst):
                real_replace(src, dst)
                if Path(dst) == state.ONLINE_REFRESH_HISTORY_FILE:
                    raise OSError("history fsync confirmation lost")

            client = app.test_client()
            request_id = "c" * 32
            with patch.object(state.os, "replace", side_effect=replace_then_fail_history), patch.object(
                refresh, "start_refresh_thread",
            ) as start:
                failed = client.post(
                    "/api/online-scripts/refresh", json={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 500)
                actual = json.loads(state.ONLINE_REFRESH_STATE_FILE.read_text(encoding="utf-8"))
                self.assertEqual(actual["request_id"], request_id)
                self.assertEqual(json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8"))[request_id]["id"], actual["id"])
                start.assert_not_called()

            status = client.get(f"/api/online-scripts/refresh-status/{request_id}", headers={"X-Token": TOKEN})
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json["running"])
            self.assertEqual(status.json["status"], "结果未知")
            self.assertIn("history fsync confirmation lost", status.json["persistence_error"])
            recovered = client.post(status.json["persistence_recovery"]["api_action"], headers={"X-Token": TOKEN})
            self.assertEqual(recovered.status_code, 200)
            replay = client.post(
                "/api/online-scripts/refresh", json={"request_id": request_id}, headers={"X-Token": TOKEN},
            )
            self.assertEqual(replay.status_code, 409)
            self.assertEqual(replay.json["id"], actual["id"])
            start.assert_not_called()

    def test_receipt_recovery_is_csrf_protected_and_concurrent_replay_is_idempotent(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants as state
            from fls_manager.online_scripts.constants import recover_online_refresh_receipt

            self._seed_refresh(state)
            records = json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8"))
            records.pop("a" * 32)
            state.ONLINE_REFRESH_HISTORY_FILE.write_text(json.dumps(records), encoding="utf-8")
            before = (base_dir / "data" / "online_scripts_cache.json")
            cache_before = before.read_bytes() if before.exists() else None

            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="refresh-csrf")
            endpoint = "/api/online-scripts/refresh-recover/" + "a" * 32
            denied = client.post(endpoint)
            self.assertEqual(denied.status_code, 400)
            self.assertIn("CSRF", denied.json["msg"])
            self.assertNotIn("a" * 32, json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8")))
            recovered = client.post(endpoint, headers={"X-CSRF-Token": "refresh-csrf"})
            self.assertEqual(recovered.status_code, 200)
            self.assertTrue(recovered.json["ok"])
            self.assertIn("a" * 32, json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8")))
            invalid = client.post("/api/online-scripts/refresh-recover/not-a-request", headers={"X-CSRF-Token": "refresh-csrf"})
            self.assertEqual(invalid.status_code, 400)

            barrier = threading.Barrier(4)
            results, errors = [], []

            def recover():
                try:
                    other = app.test_client()
                    barrier.wait(timeout=5)
                    response = other.post(endpoint, headers={"X-Token": TOKEN})
                    results.append(response.status_code)
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=recover) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertIn(200, results)
            self.assertTrue(set(results) <= {200, 409}, results)
            replay = client.post(endpoint, headers={"X-CSRF-Token": "refresh-csrf"})
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(json.loads(state.ONLINE_REFRESH_STATE_FILE.read_text(encoding="utf-8"))["request_id"], "a" * 32)
            if cache_before is None:
                self.assertFalse(before.exists())
            else:
                self.assertEqual(before.read_bytes(), cache_before)
            self.assertIsNone(recover_online_refresh_receipt("f" * 32))

    def test_restart_recovers_half_published_claim_without_starting_worker(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants as state
            from fls_manager.routes.online_scripts import refresh
            self._seed_refresh(state)
            real_replace = state.os.replace

            def fail_history(src, dst):
                if Path(dst) == state.ONLINE_REFRESH_HISTORY_FILE:
                    raise OSError("restart history publication denied")
                return real_replace(src, dst)

            request_id = "c" * 32
            client = app.test_client()
            with patch.object(state.os, "replace", side_effect=fail_history), patch.object(
                refresh, "start_refresh_thread",
            ) as start:
                failed = client.post(
                    "/api/online-scripts/refresh",
                    json={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(failed.status_code, 500)
                start.assert_not_called()

            state.ONLINE_REFRESH_STATE.clear()
            recovered = state.get_online_refresh_state()
            self.assertEqual(recovered["request_id"], request_id)
            self.assertEqual(recovered["status"], "结果未知")
            self.assertFalse(recovered["running"])
            self.assertEqual(json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8"))[request_id]["id"], recovered["id"])
            self.assertTrue(Path(recovered["log_file"]).is_file())
            self.assertIn("未启动抓取", Path(recovered["log_file"]).read_text(encoding="utf-8"))
            with patch.object(refresh, "start_refresh_thread") as restarted:
                replay = client.post(
                    "/api/online-scripts/refresh",
                    json={"request_id": request_id}, headers={"X-Token": TOKEN},
                )
            self.assertEqual(replay.status_code, 409)
            self.assertEqual(restarted.call_count, 0)
            self.assertEqual(state.ONLINE_REFRESH_STATE["request_id"], request_id)

    def test_server_rendered_receipt_recovery_form_preserves_csrf_and_auth_boundary(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants as state

            self._seed_refresh(state)
            records = json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8"))
            records.pop("a" * 32)
            state.ONLINE_REFRESH_HISTORY_FILE.write_text(json.dumps(records), encoding="utf-8")
            endpoint = "/online-scripts/refresh-recover/" + "a" * 32

            unauthenticated = app.test_client()
            with unauthenticated.session_transaction() as session:
                session["csrf_token"] = "csrf-without-login"
            unauthorized = unauthenticated.post(
                "/api/online-scripts/refresh-recover/" + "a" * 32,
                headers={"X-CSRF-Token": "csrf-without-login"},
            )
            self.assertEqual(unauthorized.status_code, 401)
            self.assertNotIn("a" * 32, json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8")))

            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=4102444800, security_verified=True, csrf_token="html-refresh-csrf")
            page = client.get("/online-scripts")
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            self.assertIn(f'action="{endpoint}"', html)
            form = html.split(f'action="{endpoint}"', 1)[1].split("</form>", 1)[0]
            self.assertIn('data-api-action="/api' + endpoint + '"', form)
            token = re.search(r'name="csrf_token" value="([^"]+)"', form)
            self.assertIsNotNone(token)
            denied = client.post(endpoint)
            self.assertEqual(denied.status_code, 400)
            failed_data = json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8"))
            self.assertNotIn("a" * 32, failed_data)
            repaired = client.post(endpoint, data={"csrf_token": token.group(1)})
            self.assertEqual(repaired.status_code, 200)
            self.assertIn("刷新回执已修复", repaired.get_data(as_text=True))
            self.assertIn("不重新抓取或改动缓存", repaired.get_data(as_text=True))
            self.assertIn("a" * 32, json.loads(state.ONLINE_REFRESH_HISTORY_FILE.read_text(encoding="utf-8")))

    def test_corrupt_or_linked_history_blocks_receipt_repair_without_replacing_source(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants as state

            self._seed_refresh(state)
            client = app.test_client()
            raw = b"{broken"
            state.ONLINE_REFRESH_HISTORY_FILE.write_bytes(raw)
            failed = client.post(
                "/api/online-scripts/refresh-recover/" + "a" * 32, headers={"X-Token": TOKEN},
            )
            self.assertEqual(failed.status_code, 500)
            self.assertEqual(state.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), raw)

            state.ONLINE_REFRESH_HISTORY_FILE.unlink()
            outside = base_dir / "outside-history.json"
            outside.write_text("{}", encoding="utf-8")
            state.ONLINE_REFRESH_HISTORY_FILE.symlink_to(outside)
            linked = client.post(
                "/api/online-scripts/refresh-recover/" + "a" * 32, headers={"X-Token": TOKEN},
            )
            self.assertEqual(linked.status_code, 500)
            self.assertEqual(outside.read_text(encoding="utf-8"), "{}")
            self.assertTrue(state.ONLINE_REFRESH_HISTORY_FILE.is_symlink())
            self.assertTrue(base_dir.joinpath("data").exists())


    def test_corrupt_refresh_state_is_reported_and_preserved(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts.constants import ONLINE_REFRESH_STATE_FILE

            ONLINE_REFRESH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            raw = b'{"id":'
            ONLINE_REFRESH_STATE_FILE.write_bytes(raw)
            client = app.test_client()

            status = client.get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN})
            self.assertEqual(status.status_code, 500)
            self.assertIn("未覆盖原文件", status.json["error"])
            self.assertEqual(ONLINE_REFRESH_STATE_FILE.read_bytes(), raw)

            with patch("fls_manager.routes.online_scripts.refresh.start_refresh_thread") as start:
                failed = client.post(
                    "/online-scripts/refresh",
                    data={"proxy_id": ""},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(failed.status_code, 500)
            self.assertIn("未覆盖原文件", failed.get_data(as_text=True))
            start.assert_not_called()
            self.assertEqual(ONLINE_REFRESH_STATE_FILE.read_bytes(), raw)

            page = client.get("/online-scripts", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn("未覆盖原文件", page.get_data(as_text=True))
            self.assertEqual(ONLINE_REFRESH_STATE_FILE.read_bytes(), raw)

    def test_dangling_state_symlink_is_reported_without_following_or_replacing_target(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import constants as state

            self._seed_refresh(state)
            target = base_dir / "missing-outside-state.json"
            history_before = state.ONLINE_REFRESH_HISTORY_FILE.read_bytes()
            state.ONLINE_REFRESH_STATE_FILE.unlink()
            state.ONLINE_REFRESH_STATE_FILE.symlink_to(target)

            response = app.test_client().get(
                "/api/online-scripts/refresh-status", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 500)
            self.assertIn("普通文件", response.json["error"])
            self.assertTrue(state.ONLINE_REFRESH_STATE_FILE.is_symlink())
            self.assertFalse(target.exists())
            self.assertEqual(state.ONLINE_REFRESH_HISTORY_FILE.read_bytes(), history_before)

    def test_repaired_refresh_state_can_start_and_running_worker_is_not_marked_unknown(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_REFRESH_STATE_FILE

            ONLINE_REFRESH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_REFRESH_STATE_FILE.write_text(json.dumps({}), encoding="utf-8")
            client = app.test_client()
            with patch("fls_manager.routes.online_scripts.refresh.start_refresh_thread") as start:
                response = client.post("/api/online-scripts/refresh", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 202)
                self.assertTrue(start.called)
                running = client.get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN})

            self.assertEqual(running.status_code, 200)
            self.assertTrue(running.json["running"])
            self.assertEqual(running.json["status"], "运行中")

            repaired = client.get("/api/online-scripts/refresh-status", headers={"X-Token": TOKEN})
            self.assertTrue(repaired.json["running"])


if __name__ == "__main__":
    unittest.main()
