import unittest

from tests.test_bulk_workflows import TOKEN, isolated_app


class NativeHistoryApiTests(unittest.TestCase):
    def test_history_api_filters_paginates_and_masks_sensitive_values(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import models

            models.save_task_history([
                {
                    "id": "history-3", "task_id": "task-1", "task_name": "Alpha",
                    "command": "python alpha.py token=super-secret", "status": "failed",
                    "start_at": "2026-11-25 03:00:00", "message": "failed",
                    "log_file": "/private/path/alpha.log", "env": {"SECRET": "drop"},
                },
                {
                    "id": "history-2", "task_id": "task-2", "task_name": "Alpha again",
                    "command": "python alpha_again.py", "status": "failed",
                    "start_at": "2026-11-25 02:00:00", "message": "retry failed",
                },
                {
                    "id": "history-1", "task_id": "task-3", "task_name": "Beta",
                    "command": "python beta.py", "status": "success",
                    "start_at": "2026-11-25 01:00:00", "message": "ok",
                },
            ])
            client = app.test_client()
            headers = {"X-Token": TOKEN}

            first = client.get("/api/history?q=alpha&status=failed&limit=1", headers=headers)
            second = client.get("/api/history?q=alpha&status=failed&limit=1&page=2", headers=headers)

            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.headers.get("Cache-Control"), "no-store")
            self.assertEqual(first.json["total"], 2)
            self.assertEqual(first.json["pages"], 2)
            self.assertEqual(first.json["items"][0]["id"], "history-3")
            self.assertIn("token=sup*****cret", first.json["items"][0]["command"])
            self.assertNotIn("log_file", first.json["items"][0])
            self.assertNotIn("private/path", first.get_data(as_text=True))
            self.assertNotIn("drop", first.get_data(as_text=True))
            self.assertEqual(second.json["items"][0]["id"], "history-2")

    def test_history_export_is_bounded_authenticated_and_uses_safe_rows(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager import models

            models.save_task_history([{
                "id": "history-1", "task_id": "task-1", "task_name": "Demo",
                "command": "=1+1 token=super-secret", "status": "success",
                "message": "ok", "log_file": "/private/path/task.log",
            }])
            client = app.test_client()
            unauthorized = client.get("/api/history/export")
            response = client.get("/api/history/export", headers={"X-Token": TOKEN})
            body = response.get_data()

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertIn(b"attachment; filename=\"fls-history-", response.headers["Content-Disposition"].encode())
            self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"'=1+1 token=sup*****cret", body)
            self.assertNotIn(b"private/path", body)

    def test_history_api_rejects_invalid_filters_and_unauthenticated_reads(self):
        with isolated_app() as (app, _base_dir):
            client = app.test_client()
            self.assertEqual(client.get("/api/history").status_code, 401)
            for query in ("page=0", "page=bad", "limit=101", "status=invalid", "q=" + "x" * 201):
                with self.subTest(query=query):
                    response = client.get("/api/history?" + query, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json["ok"])


if __name__ == "__main__":
    unittest.main()
