import unittest

from tests.test_bulk_workflows import TOKEN, isolated_app


class NativeLogApiTests(unittest.TestCase):
    def test_log_list_filters_and_paginates_safe_file_summaries(self):
        with isolated_app() as (app, base_dir):
            log_dir = base_dir / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            older = log_dir / "alpha-old.log"
            newer = log_dir / "beta-new.log"
            older.write_text("===== 启动任务: Alpha =====\nold\n", encoding="utf-8")
            newer.write_text("===== 启动任务: Beta =====\nnew\n", encoding="utf-8")
            client = app.test_client()
            headers = {"X-Token": TOKEN}

            first = client.get("/api/logs?limit=1", headers=headers)
            filtered = client.get("/api/logs?q=alpha", headers=headers)

            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.headers.get("Cache-Control"), "no-store")
            self.assertEqual(first.json["total"], 2)
            self.assertEqual(first.json["items"][0]["filename"], "beta-new.log")
            self.assertEqual(filtered.json["total"], 1)
            self.assertEqual(filtered.json["items"][0]["group"], "Alpha")
            self.assertNotIn(str(base_dir), first.get_data(as_text=True))
            self.assertEqual(client.get("/api/logs").status_code, 401)

    def test_log_json_tail_is_bounded_masked_and_confined_to_log_directory(self):
        with isolated_app() as (app, base_dir):
            log_dir = base_dir / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "task.log").write_text(
                "one\ntoken=super-secret\nthree\n", encoding="utf-8",
            )
            (base_dir / "outside.log").write_text("private outside\n", encoding="utf-8")
            client = app.test_client()
            headers = {"X-Token": TOKEN}

            response = client.get(
                "/api/logfile/task.log?lines=10&format=json", headers=headers,
            )
            download = client.get("/api/logfile/task.log/download", headers=headers)
            invalid = client.get(
                "/api/logfile/task.log?lines=5001&format=json", headers=headers,
            )
            escaped = client.get(
                "/api/logfile/../outside.log?format=json", headers=headers,
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertIn("token=sup*****cret", response.json["log"])
            self.assertNotIn("super-secret", response.json["log"])
            self.assertEqual(download.status_code, 200)
            self.assertIn("attachment", download.headers["Content-Disposition"])
            self.assertIn(b"token=sup*****cret", download.data)
            self.assertNotIn(b"super-secret", download.data)
            self.assertEqual(invalid.status_code, 400)
            self.assertIn(escaped.status_code, (404, 308))


if __name__ == "__main__":
    unittest.main()
