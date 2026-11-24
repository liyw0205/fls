import unittest

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task


class NativeCollectionApiTests(unittest.TestCase):
    def test_list_is_bounded_searchable_and_does_not_expose_task_secrets(self):
        with isolated_app() as (app, _):
            from fls_manager import models

            models.save_collections([
                {"id": "group-a", "name": "签到", "remark": "每日运行"},
                {"id": "group-b", "name": "监控", "remark": ""},
            ])
            models.save_tasks([
                sample_task("task-a", name="每日签到", remark="晨间", collection_id="group-a",
                            env={"SECRET": "must-not-leak"}),
                sample_task("task-b", collection_id="group-b"),
                sample_task("task-c", collection_id=""),
            ])
            client = app.test_client()
            response = client.get("/api/collections?q=晨间&limit=1", headers={"X-Token": TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            payload = response.get_json()
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["id"], "group-a")
            self.assertEqual(payload["items"][0]["member_count"], 1)
            self.assertEqual(payload["pages"], 1)
            self.assertNotIn("must-not-leak", response.get_data(as_text=True))

    def test_member_and_available_views_are_paginated_and_return_safe_revisions(self):
        with isolated_app() as (app, _):
            from fls_manager import models
            from fls_manager.collection_delete import collection_version

            collection = {"id": "group-a", "name": "签到", "remark": "备注"}
            models.save_collections([collection])
            models.save_tasks([
                sample_task("a", name="签到 A", collection_id="group-a", env={"SECRET": "private"}),
                sample_task("b", name="签到 B", collection_id="group-a", pinned=True),
                sample_task("c", name="其他任务", collection_id="group-b"),
            ])
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            members = client.get("/api/collection/group-a?limit=1", headers=headers)
            self.assertEqual(members.status_code, 200)
            member_payload = members.get_json()
            self.assertEqual(member_payload["collection"]["member_count"], 2)
            self.assertEqual(member_payload["total"], 2)
            self.assertEqual(len(member_payload["items"]), 1)
            self.assertEqual(member_payload["member_ids"], ["a", "b"])
            self.assertEqual(member_payload["items"][0]["id"], "b")
            self.assertIn("created_at", member_payload["items"][0])
            self.assertEqual(members.headers.get("Cache-Control"), "no-store")
            self.assertEqual(member_payload["collection_version"], collection_version(models.get_collection("group-a")))
            self.assertNotIn("private", members.get_data(as_text=True))

            available = client.get("/api/collection/group-a?view=available&q=其他", headers=headers).get_json()
            self.assertEqual([row["id"] for row in available["items"]], ["c"])
            self.assertEqual(available["items"][0]["collection_id"], "group-b")

    def test_native_session_can_read_and_invalid_paging_is_rejected(self):
        with isolated_app() as (app, _):
            from fls_manager import models

            models.save_collections([{"id": "group-a", "name": "签到"}])
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(token=TOKEN, token_expire_at=9999999999, security_verified=True,
                               csrf_token="native-csrf")
            response = client.get("/api/collections?page=nope")
            self.assertEqual(response.status_code, 400)
            self.assertFalse(response.get_json()["ok"])

            unauthenticated = app.test_client().get("/api/collections")
            self.assertEqual(unauthenticated.status_code, 401)


if __name__ == "__main__":
    unittest.main()
