import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "frontend-optimization-token"


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

    with tempfile.TemporaryDirectory(prefix="fls-frontend-optimization-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "frontend-optimization-secret"
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


class FrontendOptimizationTests(unittest.TestCase):
    def test_layout_references_versioned_favicon_and_assets(self):
        with isolated_app() as app:
            response = app.test_client().get("/", headers={"X-Token": TOKEN})
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('/static/favicon.svg?v=20260919-5', html)
            self.assertIn('/static/fls.css?v=20260919-5', html)
            self.assertIn('/static/fls.js?v=20260919-5', html)
            self.assertIn('/static/fls_theme.css?v=20260919-5', html)
            self.assertIn('id="flsUpdateNoticeSlot"', html)

    def test_favicon_is_served_as_static_asset(self):
        with isolated_app() as app:
            response = app.test_client().get("/static/favicon.svg")
            data = response.get_data()
            response.close()

            self.assertEqual(response.status_code, 200)
            self.assertIn("image/svg+xml", response.content_type)
            self.assertIn(b"<title>FLS</title>", data)

    def test_long_pages_expose_section_navigation_targets(self):
        with isolated_app() as app:
            client = app.test_client()
            cases = {
                "/config": ("config-login", "config-save"),
                "/deps": ("deps-install", "deps-installed"),
                "/online-scripts": ("online-search", "online-list"),
                "/about": ("about-intro", "about-rules"),
            }

            for path, targets in cases.items():
                with self.subTest(path=path):
                    response = client.get(path, headers={"X-Token": TOKEN})
                    html = response.get_data(as_text=True)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('class="fls-section-nav"', html)
                    for target in targets:
                        self.assertIn(f'id="{target}"', html)
                        self.assertIn(f'href="#{target}"', html)

    def test_shell_layering_tokens_are_present(self):
        css = (ROOT / "fls_manager" / "static" / "fls.css").read_text(encoding="utf-8")
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")

        self.assertIn("overflow-x:clip", css)
        self.assertIn("position:sticky", css)
        self.assertIn("body.fls-menu-open .fls-form-float-actions", css)
        self.assertIn("document.body.classList.add(\"fls-menu-open\")", js)
        self.assertIn('e.key === "Escape"', js)

    def test_navigation_cache_has_limits_and_explicit_refresh(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")

        self.assertIn("FLS_PAGE_CACHE_MAX", js)
        self.assertIn("FLS_SESSION_CACHE_MAX", js)
        self.assertIn("window.flsRefreshCurrentPage", js)
        self.assertIn("flsHtmlCacheAllowed", js)

    def test_details_use_modal_and_result_panels_float(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
        css = (ROOT / "fls_manager" / "static" / "fls_theme.css").read_text(encoding="utf-8")

        self.assertIn("flsOpenDisclosureModal", js)
        self.assertIn("flsShowFloatingPanel", js)
        self.assertIn(".fls-disclosure-modal", css)
        self.assertIn(".fls-floating-panel", css)
        self.assertIn("--sidebar:#ffffff", css)
        self.assertIn(".fls-update-notice", css)

    def test_update_notice_uses_cached_state_and_reuses_about_update_view(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")

        self.assertIn("/api/about/update-info", js)
        self.assertIn('"/api/about/update-info?wait=1"', js)
        self.assertIn('fetch("/about"', js)
        self.assertIn('querySelector("#about-version")', js)
        self.assertIn('querySelector("#about-updates")', js)
        self.assertIn("flsOpenUpdateLogModal", js)
        self.assertIn("fls-update-notice-new", js)
        self.assertIn("fls-version-current", js)
        self.assertIn("localStorage.getItem(FLS_UPDATE_NOTICE_CACHE_KEY)", js)
        self.assertIn("flsCheckUpdateNoticeCache();", js)
        self.assertIn("window.__FLS_UPDATE_NOTICE_RESOLVED__", js)
        self.assertNotIn("setTimeout(function(){ loadState", js)

    def test_update_info_api_returns_cached_status_and_logs_on_demand(self):
        with isolated_app() as app:
            from fls_manager.routes.about.state import set_update_log_state

            set_update_log_state(
                checking=False,
                available=True,
                version="abc1234",
                current_version="def5678",
                checked_at="2026-09-19 12:00:00",
                error="",
            )
            logs = [{"short": "abc1234", "date": "2026-09-19", "subject": "更新"}]
            with patch(
                "fls_manager.routes.about.version.get_version_info",
                return_value={"logs": logs},
            ):
                response = app.test_client().get(
                    "/api/about/update-info?logs=1",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {
                "ok": True,
                "checking": False,
                "available": True,
                "version": "abc1234",
                "current_version": "def5678",
                "checked_at": "2026-09-19 12:00:00",
                "error": "",
                "logs": logs,
            })

    def test_update_info_api_does_not_read_logs_while_refresh_is_running(self):
        with isolated_app() as app:
            from fls_manager.routes.about.state import set_update_log_state

            set_update_log_state(checking=True, available=False, error="")
            with patch("fls_manager.routes.about.version.get_version_info") as version_info:
                response = app.test_client().get(
                    "/api/about/update-info?logs=1",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["checking"])
            self.assertEqual(response.get_json()["logs"], [])
            version_info.assert_not_called()

    def test_update_info_waits_for_the_current_cache_refresh_once(self):
        with isolated_app() as app:
            with patch("fls_manager.routes.about.version.update_log_state") as update_state:
                update_state.return_value = {
                    "checking": False,
                    "available": False,
                    "version": "",
                    "current_version": "abc1234",
                    "checked_at": "2026-09-19 12:00:00",
                    "error": "",
                }
                response = app.test_client().get(
                    "/api/about/update-info?wait=1",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            update_state.assert_called_once_with(wait_for_completion=True, timeout=10)

    def test_dashboard_uses_theme_stat_classes(self):
        html = (ROOT / "fls_manager" / "routes" / "dashboard.py").read_text(encoding="utf-8")

        self.assertIn("stat-emphasis", html)
        self.assertIn("stat-warning", html)
        self.assertNotIn('style="color:#7c3aed', html)


if __name__ == "__main__":
    unittest.main()
