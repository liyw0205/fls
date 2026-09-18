import contextlib
import os
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "frontend-method-contract-token"


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


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-frontend-method-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "frontend-method-contract-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()
        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from fls_manager.app import create_app

            yield create_app()
        finally:
            shutdown_scheduler()
            purge_fls_modules()
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class ControlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.forms = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        elif tag == "form":
            self.forms.append(
                (
                    (attrs.get("method") or "GET").upper(),
                    attrs.get("action") or "",
                )
            )


class FrontendMethodContractTests(unittest.TestCase):
    pages = (
        "/",
        "/tasks",
        "/history",
        "/env",
        "/proxy",
        "/pull",
        "/online-scripts",
        "/backup",
        "/deps",
        "/logs",
        "/notify",
        "/panel/status",
        "/config",
        "/about",
        "/collections",
    )

    def test_rendered_controls_use_methods_supported_by_routes(self):
        with isolated_app() as app:
            client = app.test_client()
            headers = {"X-Token": TOKEN}

            # Seed the pages whose action controls only appear with an item.
            client.post(
                "/proxy/new",
                data={"name": "fixture-proxy", "type": "http", "host": "127.0.0.1", "port": "9"},
                headers=headers,
            )
            client.post(
                "/notify/new",
                data={"name": "fixture-notify", "channel": "bark"},
                headers=headers,
            )

            for page in self.pages:
                with self.subTest(page=page):
                    response = client.get(page, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    parser = ControlParser()
                    parser.feed(response.get_data(as_text=True))

                    for href in parser.links:
                        if not href or href.startswith(("#", "http:", "https:", "otpauth:")):
                            continue
                        path = urlsplit(href).path or "/"
                        adapter = app.url_map.bind("localhost")
                        try:
                            endpoint, values = adapter.match(path, method="GET")
                        except Exception as exc:
                            self.fail(f"{page} link {href!r} does not accept GET: {exc}")
                        self.assertTrue(endpoint)

                    for method, action in parser.forms:
                        target = action or page
                        path = urlsplit(target).path or "/"
                        adapter = app.url_map.bind("localhost")
                        try:
                            endpoint, values = adapter.match(path, method=method)
                        except Exception as exc:
                            self.fail(
                                f"{page} form {method} {target!r} does not match a route: {exc}"
                            )
                        self.assertTrue(endpoint)

    def test_mutating_controls_are_explicit_post_forms(self):
        with isolated_app() as app:
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            client.post(
                "/proxy/new",
                data={"name": "fixture-proxy", "type": "http", "host": "127.0.0.1", "port": "9"},
                headers=headers,
            )
            client.post(
                "/notify/new",
                data={"name": "fixture-notify", "channel": "bark"},
                headers=headers,
            )

            proxy_html = client.get("/proxy", headers=headers).get_data(as_text=True)
            notify_html = client.get("/notify", headers=headers).get_data(as_text=True)
            with patch(
                "fls_manager.routes.status.runtime_items",
                return_value=[
                    {
                        "name": "Fixture runtime",
                        "suffix": ".fixture",
                        "command": "fixture-runtime",
                        "version": "",
                        "install_url": "/install/runtime/fixture",
                    }
                ],
            ):
                status_html = client.get("/panel/status", headers=headers).get_data(as_text=True)

            self.assertIn('method="post" action="/proxy/toggle/', proxy_html)
            self.assertNotIn('href="/proxy/toggle/', proxy_html)
            self.assertIn('method="post" action="/notify/test/', notify_html)
            self.assertIn('method="post" action="/notify/toggle/', notify_html)
            self.assertNotIn('href="/notify/test/', notify_html)
            self.assertNotIn('href="/notify/toggle/', notify_html)
            self.assertIn('method="post" action="/install/runtime/', status_html)

    def test_legacy_get_side_effect_routes_remain_compatible(self):
        with isolated_app() as app:
            client = app.test_client()
            headers = {"X-Token": TOKEN}
            self.assertNotEqual(client.get("/proxy/toggle/missing", headers=headers).status_code, 405)
            self.assertNotEqual(client.get("/notify/toggle/missing", headers=headers).status_code, 405)


if __name__ == "__main__":
    unittest.main()
