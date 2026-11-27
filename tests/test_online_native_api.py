import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app


def sample_script(script_id, **updates):
    item = {
        "id": script_id,
        "name": f"Script {script_id}",
        "type": "raw",
        "link": "https://example.invalid/demo.sh?api_key=source-secret",
        "link_name": "demo.sh",
        "install": "sh install.sh",
        "doc_link": "https://example.invalid/README.md",
        "task_cron": [{
            "name": "Demo task",
            "cron": "0 * * * *",
            "command": "python demo.py --api_key=task-secret",
            "var": {"API_TOKEN": "env-secret", "REGION": "us-east"},
        }],
    }
    item.update(updates)
    return item


class OnlineNativeApiTests(unittest.TestCase):
    def test_catalog_is_authenticated_paginated_and_uses_a_safe_dto(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import catalog_api

            scripts = [sample_script("alpha"), sample_script("beta", name="Needle")]
            with patch.object(catalog_api, "load_online_script_cache", return_value=scripts), \
                    patch.object(catalog_api, "get_online_script", side_effect=lambda script_id: next(
                        (item for item in scripts if item["id"] == script_id), None,
                    )):
                client = app.test_client()
                unauthorized = client.get("/api/online-scripts/catalog")
                page = client.get(
                    "/api/online-scripts/catalog?page=1&limit=1",
                    headers={"X-Token": TOKEN},
                )
                detail = client.get(
                    "/api/online-scripts/detail/alpha", headers={"X-Token": TOKEN},
                )
                filtered = client.get(
                    "/api/online-scripts/catalog?q=Needle",
                    headers={"X-Token": TOKEN},
                )
                invalid = client.get(
                    "/api/online-scripts/catalog?limit=1000",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.headers.get("Cache-Control"), "no-store")
            self.assertEqual(page.json["total"], 2)
            self.assertEqual(page.json["limit"], 1)
            self.assertEqual(len(page.json["items"]), 1)
            self.assertEqual(detail.json["item"]["tasks"][0]["variable_names"], ["API_TOKEN", "REGION"])
            self.assertEqual(filtered.json["items"][0]["name"], "Needle")
            serialized = page.get_data(as_text=True)
            for secret in ("source-secret", "task-secret", "env-secret", "us-east"):
                self.assertNotIn(secret, serialized)
            self.assertEqual(invalid.status_code, 400)

    def test_install_options_redacts_variable_values_and_reports_task_validation(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import catalog_api

            item = sample_script("alpha")
            with patch.object(catalog_api, "get_online_script", return_value=item), \
                    patch.object(catalog_api, "load_proxies", return_value=[{
                        "id": "proxy-1", "name": "Proxy", "type": "github", "enabled": True,
                        "url": "https://user:password@example.invalid", "password": "proxy-secret",
                    }]):
                response = app.test_client().get(
                    "/api/online-scripts/install-options/alpha?q=Demo",
                    headers={"X-Token": TOKEN},
                )
                secret_search = app.test_client().get(
                    "/api/online-scripts/install-options/alpha?q=env-secret",
                    headers={"X-Token": TOKEN},
                )
                value_search = app.test_client().get(
                    "/api/online-scripts/install-options/alpha?q=us-east",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["total"], 1)
            task = response.json["items"][0]
            self.assertEqual(task["index"], 1)
            self.assertEqual(task["variable_names"], ["API_TOKEN", "REGION"])
            self.assertTrue(task["valid"])
            self.assertEqual(secret_search.json["total"], 0)
            self.assertEqual(value_search.json["total"], 0)
            serialized = response.get_data(as_text=True)
            for secret in ("env-secret", "us-east", "proxy-secret", "password@example.invalid"):
                self.assertNotIn(secret, serialized)
            self.assertEqual(response.json["proxies"], [{"id": "proxy-1", "name": "Proxy", "type": "github"}])

    def test_markdown_document_endpoint_is_bounded_authenticated_and_native_friendly(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import catalog_api

            response_body = SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "text/markdown; charset=utf-8", "Content-Length": "13"},
                encoding="utf-8",
                url="https://example.invalid/README.md",
                iter_content=lambda chunk_size: iter([b"# Demo\nhello"]),
                raise_for_status=lambda: None,
                close=lambda: None,
            )
            with patch.object(catalog_api, "get_online_script", return_value=sample_script("alpha")), \
                    patch.object(catalog_api.requests, "get", return_value=response_body) as request:
                client = app.test_client()
                unauthorized = client.get("/api/online-scripts/doc/alpha")
                response = client.get(
                    "/api/online-scripts/doc/alpha", headers={"X-Token": TOKEN},
                )

            self.assertEqual(unauthorized.status_code, 401)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(response.json["format"], "markdown")
            self.assertEqual(response.json["content"], "# Demo\nhello")
            self.assertEqual(request.call_args.kwargs["timeout"], (5, 25))

    def test_document_size_limit_and_invalid_source_url_are_rejected(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import catalog_api

            oversized = SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "text/plain"},
                encoding="utf-8",
                url="https://example.invalid/doc.txt",
                iter_content=lambda chunk_size: iter([b"x" * (catalog_api.DOC_MAX_BYTES + 1)]),
                raise_for_status=lambda: None,
                close=lambda: None,
            )
            client = app.test_client()
            with patch.object(catalog_api, "get_online_script", return_value=sample_script("alpha")), \
                    patch.object(catalog_api.requests, "get", return_value=oversized):
                too_large = client.get("/api/online-scripts/doc/alpha", headers={"X-Token": TOKEN})
            with patch.object(catalog_api, "get_online_script", return_value=sample_script(
                "alpha", doc_link="file:///etc/passwd",
            )), patch.object(catalog_api.requests, "get") as request:
                invalid = client.get("/api/online-scripts/doc/alpha", headers={"X-Token": TOKEN})

            self.assertEqual(too_large.status_code, 413)
            self.assertEqual(invalid.status_code, 400)
            request.assert_not_called()

    def test_install_status_exposes_byte_progress_and_history_omits_task_values(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import logs

            install = {
                "id": "install-1",
                "script_id": "alpha",
                "script_name": "Alpha",
                "running": True,
                "status": "运行中",
                "phase": "downloading",
                "returncode": None,
                "error": "",
                "scheduler_reload_pending": True,
                "scheduler_reload_error": "api_key=reload-secret",
                "stop_warning": "Authorization: Bearer stop-secret",
                "download_progress": {
                    "state": "downloading", "kind": "raw", "downloaded_bytes": 5,
                    "total_bytes": 10, "updated_at": "2026-11-25 12:00:00",
                },
                "selected_tasks": [{"name": "Alpha", "var": {"API_TOKEN": "env-secret"}}],
                "task_snapshot": [{"env": {"API_TOKEN": "env-secret"}}],
                "target_path": "/private/path",
                "log_file": "/private/log/install-1.log",
            }
            client = app.test_client()
            with patch.object(logs, "get_online_install_job", return_value=install), \
                    patch.object(logs, "_install_recovery", return_value={"available": False}):
                status = client.get(
                    "/api/online-scripts/log/install-1?state_only=1",
                    headers={"X-Token": TOKEN},
                )
            with patch.object(logs, "list_online_install_jobs", return_value=([install], 1, "a" * 64)):
                history = client.get(
                    "/api/online-scripts/install-history?limit=1",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.headers.get("Cache-Control"), "no-store")
            self.assertEqual(status.json["download_progress"]["downloaded_bytes"], 5)
            self.assertEqual(status.json["download_progress"]["total_bytes"], 10)
            self.assertEqual(status.json["download_progress"]["percent"], 50)
            self.assertNotIn("reload-secret", status.get_data(as_text=True))
            self.assertNotIn("stop-secret", status.get_data(as_text=True))
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json["items"][0]["download_progress"]["percent"], 50)
            self.assertNotIn("selected_tasks", history.json["items"][0])
            self.assertNotIn("task_snapshot", history.json["items"][0])
            self.assertNotIn("target_path", history.json["items"][0])
            self.assertNotIn("env-secret", history.get_data(as_text=True))

    def test_raw_download_tracks_known_and_unknown_content_lengths(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.online_scripts import install as online_install
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            class Response:
                status_code = 200
                encoding = "utf-8"

                def __init__(self, content_length):
                    self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

                def raise_for_status(self):
                    return None

                def iter_content(self, chunk_size):
                    yield b"abc"
                    yield b"defg"

                def close(self):
                    return None

            scripts_dir = base_dir / "scripts"
            log_dir = base_dir / "log"
            log_dir.mkdir(exist_ok=True)
            client = app.test_client()
            for install_id, length, expected_total, expected_percent in (
                ("raw-known", 7, 7, 100),
                ("raw-unknown", None, None, None),
                ("raw-mismatched-length", 10, None, None),
            ):
                info = {
                    "id": install_id, "script_id": install_id, "script_name": install_id,
                    "log_file": str(log_dir / f"online-install-{install_id}.log"),
                    "running": True, "status": "运行中", "phase": "downloading",
                    "returncode": None, "error": "", "updated_at": "2026-11-25 12:00:00",
                }
                ONLINE_INSTALL_RUNNING[install_id] = info
                with patch.object(online_install, "SCRIPT_DIR", scripts_dir), \
                        patch.object(online_install.requests, "get", return_value=Response(length)):
                    target = online_install.download_online_script_logged(
                        {"id": install_id, "name": install_id, "type": "raw", "link": "https://example.invalid/demo.sh",
                         "link_name": f"{install_id}.sh"},
                        "", info["log_file"], force=True, install_id=install_id,
                    )
                state = client.get(
                    f"/api/online-scripts/log/{install_id}?state_only=1",
                    headers={"X-Token": TOKEN},
                )
                self.assertEqual(target.read_bytes(), b"abcdefg")
                self.assertEqual(state.status_code, 200)
                self.assertEqual(state.json["download_progress"]["downloaded_bytes"], 7)
                self.assertEqual(state.json["download_progress"]["total_bytes"], expected_total)
                self.assertEqual(state.json["download_progress"]["percent"], expected_percent)
                ONLINE_INSTALL_RUNNING.pop(install_id, None)

    def test_new_file_publication_falls_back_when_hard_links_are_unavailable(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import install as online_install

            target = base_dir / "published.sh"
            first_temp = base_dir / "first.part"
            first_temp.write_bytes(b"first")
            with patch.object(online_install.os, "link", None, create=True):
                online_install._publish_new_file(first_temp, target)

                second_temp = base_dir / "second.part"
                second_temp.write_bytes(b"second")
                with self.assertRaises(FileExistsError):
                    online_install._publish_new_file(second_temp, target)

            self.assertEqual(target.read_bytes(), b"first")
            self.assertTrue(second_temp.exists())

    def test_retry_api_requires_overwrite_confirmation_and_replays_the_child(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.online_scripts import install as install_routes

            parent_id = "a" * 32
            child_id = "b" * 32
            parent = {
                "id": parent_id, "script_id": "alpha", "script_name": "Alpha",
                "running": False, "status": "失败", "phase": "failed",
                "import_task": False, "enable_task": False, "selected_task_indexes": [],
                "proxy_id": "", "force": False, "retried_by": "",
                "scheduler_reload_pending": False,
            }
            child = {
                **parent, "id": child_id, "retry_of": parent_id,
                "running": True, "status": "运行中", "phase": "downloading",
            }
            client = app.test_client()
            with patch.object(install_routes, "get_online_install_job", return_value=parent), \
                    patch.object(install_routes, "online_install_retry_relation", return_value={}), \
                    patch.object(install_routes, "active_online_install_for_script", return_value=("", None)), \
                    patch.object(install_routes, "get_online_script", return_value=sample_script("alpha")), \
                    patch.object(install_routes, "online_script_target", return_value=SimpleNamespace(exists=lambda: True)), \
                    patch.object(install_routes, "_start_install_job") as start:
                confirmation = client.post(
                    f"/api/online-scripts/install-retry/{parent_id}",
                    json={}, headers={"X-Token": TOKEN},
                )
                self.assertEqual(confirmation.status_code, 409)
                self.assertTrue(confirmation.json["confirm_required"])
                start.assert_not_called()

            redirect = app.response_class(
                "", status=302, headers={"Location": f"/online-scripts/log/{child_id}"},
            )
            with patch.object(install_routes, "get_online_install_job", side_effect=lambda request_id: {
                parent_id: parent, child_id: child,
            }.get(request_id)), \
                    patch.object(install_routes, "online_install_retry_relation", return_value={}), \
                    patch.object(install_routes, "active_online_install_for_script", return_value=("", None)), \
                    patch.object(install_routes, "get_online_script", return_value=sample_script("alpha")), \
                    patch.object(install_routes, "online_script_target", return_value=SimpleNamespace(exists=lambda: True)), \
                    patch.object(install_routes, "_start_install_job", return_value=redirect) as start:
                accepted = client.post(
                    f"/api/online-scripts/install-retry/{parent_id}",
                    json={"force": True}, headers={"X-Token": TOKEN},
                )

            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json["install_id"], child_id)
            self.assertEqual(accepted.json["retry_of"], parent_id)
            self.assertTrue(start.call_args.kwargs["force"])


if __name__ == "__main__":
    unittest.main()
