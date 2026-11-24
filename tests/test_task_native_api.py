import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task


class NativeTaskApiTests(unittest.TestCase):
    def test_task_list_is_paginated_filterable_and_excludes_environment(self):
        with isolated_app() as (app, _):
            from fls_manager import models

            models.save_tasks([
                sample_task("task-a", name="签到 A", collection_id="group-a", pinned=True,
                            env={"SECRET": "must-not-leak"}, updated_at="2026-01-01"),
                sample_task("task-b", name="监控 B", collection_id="group-a", enabled=False,
                            updated_at="2026-01-02"),
                sample_task("task-c", name="签到 C", collection_id="group-b", updated_at="2026-01-03"),
            ])
            client = app.test_client()
            with patch("fls_manager.task_list_api.running_task_snapshot", return_value={"task-c": {"pid": 123}}):
                response = client.get(
                    "/api/tasks?q=签到&collection_id=group-a&limit=1",
                    headers={"X-Token": TOKEN},
                )
                running = client.get("/api/tasks?status=running", headers={"X-Token": TOKEN}).get_json()
            with patch("fls_manager.task_list_api.running_task_snapshot", return_value={"task-c": {"pid": 123}}):
                disabled = client.get("/api/tasks?status=disabled", headers={"X-Token": TOKEN}).get_json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            payload = response.get_json()
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["pages"], 1)
            self.assertEqual(payload["items"][0]["id"], "task-a")
            self.assertTrue(payload["items"][0]["pinned"])
            self.assertEqual(payload["items"][0]["collection_id"], "group-a")
            self.assertNotIn("must-not-leak", response.get_data(as_text=True))

            self.assertEqual([row["id"] for row in running["items"]], ["task-c"])
            self.assertEqual([row["id"] for row in disabled["items"]], ["task-b"])

    def test_invalid_task_list_filters_and_unauthenticated_reads_are_rejected(self):
        with isolated_app() as (app, _):
            client = app.test_client()
            for query in ("page=x", "status=unknown", "sort=env", "order=sideways"):
                with self.subTest(query=query):
                    response = client.get("/api/tasks?" + query, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.get_json()["ok"])
            self.assertEqual(client.get("/api/tasks").status_code, 401)

    def test_explicit_pin_state_replays_same_request_without_reapplying(self):
        with isolated_app() as (app, _):
            from fls_manager import models

            models.save_tasks([sample_task("task-a", pinned=False)])
            client = app.test_client()
            data = {"pinned": True, "request_id": "a" * 32}
            first = client.post("/api/task/action/pin/task-a", json=data, headers={"X-Token": TOKEN})
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.get_json()["pinned"])
            self.assertTrue(models.get_task("task-a")["pinned"])

            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                replay = client.post("/api/task/action/pin/task-a", json=data, headers={"X-Token": TOKEN})
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.get_json(), first.get_json())
                mutate.assert_not_called()

            unpin = client.post(
                "/api/task/action/pin/task-a",
                json={"pinned": False, "request_id": "b" * 32},
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(unpin.status_code, 200)
            self.assertFalse(unpin.get_json()["pinned"])
            self.assertFalse(models.get_task("task-a")["pinned"])

    def test_explicit_pin_preserves_limit_and_rejects_invalid_contract(self):
        with isolated_app() as (app, _):
            from fls_manager import models

            models.save_tasks([
                sample_task(f"pinned-{index}", pinned=True) for index in range(5)
            ] + [sample_task("task-six", pinned=False)])
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            too_many = client.post(
                "/api/task/action/pin/task-six",
                json={"pinned": True, "request_id": "c" * 32},
                headers=headers,
            )
            self.assertEqual(too_many.status_code, 400)
            self.assertFalse(models.get_task("task-six")["pinned"])
            self.assertEqual(
                client.post("/api/task/action/pin/task-six", json={"pinned": "true"}, headers=headers).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/api/task/action/pin/task-six",
                    json={"pinned": True},
                    headers=headers,
                ).status_code,
                400,
            )

    def test_bulk_collection_removal_checks_expected_membership_and_replays(self):
        with isolated_app() as (app, _):
            from fls_manager import models

            models.save_tasks([
                sample_task("task-a", collection_id="group-a"),
                sample_task("task-b", collection_id="group-b"),
            ])
            client = app.test_client()
            data = {
                "action": "clear_collection",
                "task_ids": ["task-a", "task-b"],
                "request_id": "e" * 32,
                "expected_collections": {"task-a": "group-a", "task-b": "group-b"},
            }
            first = client.post("/api/task/bulk-action", json=data, headers={"X-Token": TOKEN})
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.get_json()["completed_ids"], ["task-a", "task-b"])
            self.assertEqual(models.get_task("task-a")["collection_id"], "")
            with patch("fls_manager.routes.api.mutate_tasks") as mutate:
                replay = client.post("/api/task/bulk-action", json=data, headers={"X-Token": TOKEN})
                self.assertEqual(replay.get_json(), first.get_json())
                mutate.assert_not_called()

            models.save_tasks([
                sample_task("task-a", collection_id="group-other"),
                sample_task("task-b", collection_id="group-b"),
            ])
            stale = client.post(
                "/api/task/bulk-action",
                json={
                    **data,
                    "request_id": "f" * 32,
                    "expected_collections": {"task-a": "group-a", "task-b": "group-b"},
                },
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(models.get_task("task-a")["collection_id"], "group-other")


if __name__ == "__main__":
    unittest.main()
