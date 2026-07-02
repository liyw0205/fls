import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "route-component-token"


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-route-component-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "route-component-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        purge_fls_modules()

        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))

            from fls_manager.app import create_app

            yield create_app(), Path(temp_dir)
        finally:
            shutdown_scheduler()
            purge_fls_modules()

            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def shutdown_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None

    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


class UiRouteComponentTests(unittest.TestCase):
    def test_scripts_new_renders_info_message_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/pull/new",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#6b7280;"', html)
            self.assertIn("暂无操作", html)

    def test_online_script_doc_error_renders_escaped_message_card(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "demo",
                            "name": "Demo",
                            "type": "raw",
                            "link": "https://example.invalid/demo.py",
                            "doc_link": "https://example.invalid/<doc>.md",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with patch(
                "fls_manager.routes.online_scripts.docs.requests.get",
                side_effect=RuntimeError('<bad & "x">'),
            ):
                response = app.test_client().get(
                    "/online-scripts/doc/demo?mode=render",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("文档加载失败：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertNotIn("<bad", html)


if __name__ == "__main__":
    unittest.main()
