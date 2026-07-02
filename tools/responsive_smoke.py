#!/usr/bin/env python3
"""Lightweight responsive/page-structure smoke test.

Run with ``python -B tools/responsive_smoke.py`` to avoid writing
``__pycache__`` while importing the Flask app.
"""

import contextlib
import os
import sys
import tempfile
import traceback
from html.parser import HTMLParser
from pathlib import Path

sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "responsive-smoke-token"

PAGES = (
    ("/", "page-dashboard"),
    ("/tasks", "page-tasks"),
    ("/task/new", "page-tasks"),
    ("/logs", "page-logs"),
    ("/pull", "page-pull"),
    ("/pull/new", "page-pull"),
    ("/online-scripts", "page-online_scripts"),
    ("/env", "page-env"),
    ("/notify", "page-notify"),
    ("/proxy", "page-proxy"),
    ("/config", "page-config"),
    ("/panel/status", "page-status"),
)


class PageProbe(HTMLParser):
    def __init__(self):
        super().__init__()
        self.has_viewport = False
        self.has_css = False
        self.has_js = False
        self.body_classes = set()

    def handle_starttag(self, tag, attrs):
        attr = {str(k).lower(): str(v or "") for k, v in attrs}

        if tag == "meta" and attr.get("name", "").lower() == "viewport":
            self.has_viewport = True

        if tag == "link":
            rel = attr.get("rel", "").lower().split()
            href = attr.get("href", "")
            if "stylesheet" in rel and "/static/fls.css?v=" in href:
                self.has_css = True

        if tag == "script" and "/static/fls.js?v=" in attr.get("src", ""):
            self.has_js = True

        if tag == "body":
            self.body_classes.update(attr.get("class", "").split())


def pass_item(name):
    return name, []


def fail_item(name, message):
    return name, [message]


@contextlib.contextmanager
def temporary_fls_env():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-responsive-smoke-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "responsive-smoke-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        try:
            yield Path(temp_dir)
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def load_app():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    purge_fls_modules()

    from fls_manager.app import create_app

    return create_app()


def shutdown_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None

    if scheduler is None:
        return

    with contextlib.suppress(Exception):
        scheduler.shutdown(wait=False)


def check_page(client, path, expected_body_class):
    name = f"page {path}"
    errors = []

    try:
        response = client.get(path, headers={"X-Token": TOKEN})
    except Exception as exc:
        return fail_item(name, f"request raised {type(exc).__name__}: {exc}")

    if response.status_code >= 500:
        errors.append(f"returned HTTP {response.status_code}")
    elif response.status_code != 200:
        errors.append(f"expected HTTP 200, got {response.status_code}")

    html = response.get_data(as_text=True)
    probe = PageProbe()

    try:
        probe.feed(html)
    except Exception as exc:
        errors.append(f"HTML parse failed: {type(exc).__name__}: {exc}")

    if not probe.has_viewport:
        errors.append("missing viewport meta")

    if not probe.has_css:
        errors.append("missing /static/fls.css?v= stylesheet")

    if not probe.has_js:
        errors.append("missing /static/fls.js?v= script")

    page_classes = {cls for cls in probe.body_classes if cls.startswith("page-")}
    if not page_classes:
        errors.append("missing body page-* class")
    elif expected_body_class not in page_classes:
        errors.append(
            "expected body class {}, got {}".format(
                expected_body_class,
                ", ".join(sorted(page_classes)),
            )
        )

    return name, errors


def check_static_asset(client, path, required_tokens):
    name = f"static {path}"
    errors = []

    try:
        response = client.get(path)
    except Exception as exc:
        return fail_item(name, f"request raised {type(exc).__name__}: {exc}")

    if response.status_code >= 500:
        errors.append(f"returned HTTP {response.status_code}")
    elif response.status_code != 200:
        errors.append(f"expected HTTP 200, got {response.status_code}")

    text = response.get_data(as_text=True)

    for token in required_tokens:
        if token not in text:
            errors.append(f"missing token: {token}")

    return name, errors


def run_smoke():
    results = []

    with temporary_fls_env() as base_dir:
        try:
            app = load_app()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            return [fail_item("load Flask app", detail or tb)]

        results.append(pass_item(f"temporary FLS_BASE_DIR {base_dir}"))

        try:
            client = app.test_client()

            for path, expected_body_class in PAGES:
                results.append(check_page(client, path, expected_body_class))

            results.append(
                check_static_asset(
                    client,
                    "/static/fls.css",
                    ("fls-phone", "fls-tablet", "fls-mobile", "901px", "1180px"),
                )
            )
            results.append(
                check_static_asset(
                    client,
                    "/static/fls.js",
                    ("fls-phone", "fls-tablet", "fls-desktop", "detectFlsMobile"),
                )
            )
        finally:
            shutdown_scheduler()

    return results


def print_results(results):
    failures = 0

    for name, errors in results:
        if errors:
            failures += 1
            print(f"FAIL {name}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"PASS {name}")

    if failures:
        print(f"\nFAIL responsive smoke: {failures} check(s) failed")
    else:
        print("\nPASS responsive smoke")

    return failures


def main():
    failures = print_results(run_smoke())
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
