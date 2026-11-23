import contextlib
import json
import os
import shutil
import subprocess
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
            self.assertIn('/static/favicon-generated.png?v=20260920-1', html)
            self.assertIn('/static/fls.css?v=20260920-3', html)
            self.assertIn('/static/fls.js?v=20260920-3&actions=6', html)
            self.assertIn('/static/fls_theme.css?v=20261124-1&theme=deepseek', html)
            self.assertIn('class="page-dashboard fls-theme-deepseek"', html)
            self.assertIn('/static/fls_density.css?v=20260930-2', html)
            self.assertIn('id="flsUpdateNoticeSlot"', html)

    def test_generated_favicon_is_served_as_static_asset(self):
        with isolated_app() as app:
            response = app.test_client().get("/static/favicon-generated.png")
            data = response.get_data()
            response.close()

            self.assertEqual(response.status_code, 200)
            self.assertIn("image/png", response.content_type)
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_editor_bundle_and_styles_are_available_without_external_network_or_auth(self):
        with isolated_app() as app:
            client = app.test_client()
            for name, content_type, marker in (
                ("codemirror.min.js", "javascript", b"CodeMirror"),
                ("codemirror.css", "text/css", b".CodeMirror"),
                ("LICENSE", "application/octet-stream", b"MIT"),
            ):
                with self.subTest(name=name):
                    response = client.get("/static/vendor/codemirror-5.65.16/" + name)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(content_type, response.content_type)
                    self.assertIn(marker, response.get_data())
                    response.close()

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

    def test_panel_controls_are_on_dashboard_and_theme_selector_is_in_config(self):
        with isolated_app() as app:
            client = app.test_client()

            dashboard = client.get("/", headers={"X-Token": TOKEN})
            dashboard_html = dashboard.get_data(as_text=True)
            self.assertEqual(dashboard.status_code, 200)
            self.assertIn('id="dashboard-panel-control"', dashboard_html)
            self.assertIn('action="/about/restart-panel"', dashboard_html)
            self.assertIn('action="/about/stop-panel"', dashboard_html)
            self.assertNotIn('<h2 class="section-title">面板控制</h2>', dashboard_html)
            self.assertNotIn("重启或停止面板会影响当前访问", dashboard_html)
            self.assertNotIn("查看任务摘要、最近活动和面板运行状态。", dashboard_html)
            self.assertLess(
                dashboard_html.index('id="dashboard-panel-control"'),
                dashboard_html.index('id="dashboard-summary"'),
            )

            about = client.get("/about", headers={"X-Token": TOKEN})
            about_html = about.get_data(as_text=True)
            self.assertEqual(about.status_code, 200)
            self.assertNotIn('action="/about/restart-panel"', about_html)
            self.assertNotIn('action="/about/stop-panel"', about_html)

            config = client.get("/config", headers={"X-Token": TOKEN})
            config_html = config.get_data(as_text=True)
            self.assertEqual(config.status_code, 200)
            self.assertIn('id="config-theme"', config_html)
            self.assertIn('<option value="deepseek" selected>deepseek</option>', config_html)
            self.assertIn('<option value="neumorphism">新拟物派</option>', config_html)
            self.assertIn('<option value="editorial">编辑杂志风</option>', config_html)
            self.assertIn('<option value="glassmorphism">玻璃拟态</option>', config_html)
            self.assertNotIn('默认主题', config_html)

    def test_theme_defaults_migrate_legacy_default_and_keep_theme_rules_scoped(self):
        with isolated_app() as app:
            from fls_manager.config import DEFAULT_CONFIG, normalize_config_data

            self.assertEqual(DEFAULT_CONFIG["theme"], "deepseek")
            self.assertEqual(normalize_config_data({})["theme"], "deepseek")
            self.assertEqual(normalize_config_data({"theme": "default"})["theme"], "neumorphism")
            self.assertEqual(normalize_config_data({"theme": "unknown"})["theme"], "deepseek")

            css = (ROOT / "fls_manager" / "static" / "fls_theme.css").read_text(encoding="utf-8")
            legacy_theme = css.split("Neumorphism theme refresh", 1)[1].split("DeepSeek-inspired theme", 1)[0]
            self.assertNotIn("\n.sidebar {", legacy_theme)
            self.assertIn("body.fls-theme-neumorphism .sidebar", legacy_theme)
            deepseek_theme = css.split("DeepSeek-inspired theme", 1)[1]
            self.assertIn("--primary:#3964fe", deepseek_theme)
            self.assertIn("@media (prefers-color-scheme:dark)", deepseek_theme)
            self.assertIn("--bg:#151517", deepseek_theme)

    def test_shell_layering_tokens_are_present(self):
        css = (ROOT / "fls_manager" / "static" / "fls.css").read_text(encoding="utf-8")
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")

        self.assertIn("overflow-x:clip", css)
        self.assertIn("position:sticky", css)
        self.assertIn("body.fls-menu-open .fls-form-float-actions", css)
        self.assertIn("document.body.classList.add(\"fls-menu-open\")", js)
        self.assertIn('e.key === "Escape"', js)
        self.assertNotIn("flsOriginalButtonsInViewport", js)
        self.assertNotIn("flsUpdateFloatingFormVisibility", js)
        self.assertNotIn("hide-near-original", js)

    def test_form_errors_render_server_response_without_getting_post_url(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")

        self.assertIn("async function handleFormErrorResponse", js)
        self.assertIn('errorDoc.querySelector(".content")', js)
        self.assertIn("await replaceHtmlText(body, res.url || fetchUrl, false)", js)
        self.assertIn("res.status === 409", js)
        self.assertIn("重新登录后重试", js)
        self.assertNotIn("location.href = fetchUrl", js)

    def test_ajax_payload_readers_check_http_status_before_json_or_text(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")

        self.assertIn("async function flsReadJsonResponse(response)", js)
        self.assertIn("async function flsReadTextResponse(response)", js)
        self.assertIn("if(!response.ok)", js)
        self.assertIn("error.payload = payload", js)
        self.assertNotIn("await res.json()", js)

        for relative in (
            "ui/tables.py",
            "routes/tasks/collections.py",
            "routes/scripts/debug.py",
            "routes/about/jobs.py",
            "routes/online_scripts/logs.py",
            "routes/online_scripts/pages.py",
            "routes/deps.py",
            "routes/dashboard.py",
            "routes/proxy/_common.py",
            "routes/proxy/pages.py",
            "routes/backup/page.py",
            "routes/logs/pages.py",
            "routes/logs/files.py",
            "routes/tasks/logs.py",
        ):
            source = (ROOT / "fls_manager" / relative).read_text(encoding="utf-8")
            with self.subTest(relative=relative):
                self.assertNotIn("await res.json()", source)
                self.assertNotIn("await res.text()", source)

        backup = (ROOT / "fls_manager" / "routes" / "backup" / "page.py").read_text(encoding="utf-8")
        self.assertIn("if(payload && payload.job_id)", backup)

    def test_density_styles_preserve_table_details_and_mobile_targets(self):
        css = (ROOT / "fls_manager" / "static" / "fls_density.css").read_text(encoding="utf-8")

        self.assertIn("--fls-section-gap", css)
        self.assertIn("#tasksTable .task-detail-column", css)
        self.assertIn(".task-desktop-details", css)
        self.assertIn("#pullDesktopTable .script-tree-actions .row-actions", css)
        self.assertIn("--fls-control-height:44px", css)

    def test_panel_control_uses_custom_confirmation_modal(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
        css = (ROOT / "fls_manager" / "static" / "fls_theme.css").read_text(encoding="utf-8")

        self.assertIn("flsConfirmPanelControl", js)
        self.assertIn("确认停止面板", js)
        self.assertIn("确认重启面板", js)
        self.assertIn(".fls-panel-confirm-dialog", css)

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

    def test_update_info_api_exposes_completed_refresh_failure_as_retryable(self):
        with isolated_app() as app:
            from fls_manager.routes.about.state import set_update_log_state

            set_update_log_state(
                checking=False,
                available=False,
                current_version="def5678",
                checked_at="2026-09-19 12:00:00",
                error="git fetch 失败",
            )
            response = app.test_client().get(
                "/api/about/update-info?wait=1",
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()["ok"], False)
            self.assertEqual(response.get_json()["error"], "git fetch 失败")

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise update recovery rendering")
    def test_update_notice_renders_retry_button_and_retries_from_cached_error(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
        start = js.index("function flsRenderUpdateNotice")
        end = js.index("async function flsOpenUpdateLogModal", start)
        renderer = js[start:end]
        result = subprocess.run(
            ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.hidden = false; this.textContent = ''; this.dataset = {}; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); return item; }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  addEventListener(name, handler) { if(name === 'click') this.onclick = handler; }
}
const slot = new Node('div');
let retries = 0;
const context = {
  window: {__FLS_UPDATE_NOTICE_RESOLVED__: true},
  document: {getElementById: id => id === 'flsUpdateNoticeSlot' ? slot : null,
    createElement: tag => new Node(tag),
    createTextNode: text => String(text)},
  flsMarkButtonBusy: () => true,
  flsRestoreButton: () => {},
  flsCheckUpdateNoticeCache: async () => { retries += 1; },
};
(async () => {
  vm.createContext(context);
  vm.runInContext(input.renderer, context);
  context.flsRenderUpdateNotice({current_version:'def5678', error:'HTTP 503: git fetch 失败'});
  assert.equal(slot.children.length, 2);
  const error = slot.children[1];
  assert.match(error.textContent, /更新检查失败：HTTP 503/);
  assert.equal(error.children.length, 1);
  await error.children[0].onclick();
  assert.equal(retries, 1);
  assert.equal(context.window.__FLS_UPDATE_NOTICE_RESOLVED__, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
            input=json.dumps({"renderer": renderer}),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise update log error recovery")
    def test_update_log_modal_preserves_retry_and_login_actions_for_http_errors(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
        start = js.index("async function flsOpenUpdateLogModal")
        end = js.index("const FLS_UPDATE_NOTICE_CACHE_KEY", start)
        modal = js[start:end]
        result = subprocess.run(
            ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Node {
  constructor(tag){ this.tagName=tag.toUpperCase(); this.children=[]; this.classList={remove(){}}; this.textContent=''; this.href=''; }
  append(...items){ this.children.push(...items); }
  appendChild(item){ this.children.push(item); return item; }
  addEventListener(name, handler){ if(name === 'click') this.onclick = handler; }
  setAttribute(){}
}
let opened = [];
let retried = 0;
const context = {
  document: {createElement: tag => new Node(tag)},
  location: {pathname:'/about', search:''},
  flsOpenModalContent: (title, content, trigger) => { opened.push({title, content, trigger}); },
  flsMarkButtonBusy: () => true,
  flsRestoreButton: () => {},
  flsReadTextResponse: async response => { const e = new Error('upstream diagnostic'); e.status = response.status; throw e; },
  fetch: async () => ({status:503}),
  DOMParser: function(){},
};
vm.createContext(context);
vm.runInContext(input.modal, context);
(async () => {
  const openUpdateLogModal = context.flsOpenUpdateLogModal;
  context.flsOpenUpdateLogModal = async () => { retried += 1; };
  const trigger = new Node('button');
  await openUpdateLogModal({currentTarget:trigger});
  assert.equal(opened.length, 1);
  assert.equal(opened[0].title, '更新日志不可用');
  const content = opened[0].content;
  assert.match(content.textContent, /HTTP 503/);
  const actions = content.children[1];
  assert.equal(actions.children.length, 1);
  await actions.children[0].onclick();
  assert.equal(retried, 1);

  opened = [];
  context.flsReadTextResponse = async response => { const e = new Error('login expired'); e.status = 401; throw e; };
  await openUpdateLogModal({currentTarget:trigger});
  assert.equal(opened[0].content.children[1].children[0].textContent, '重新登录');
  assert.match(opened[0].content.children[1].children[0].href, /\/login\?next=/);
  console.log('update log error recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
            input=json.dumps({"modal": modal}),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise AJAX navigation recovery")
    def test_ajax_navigation_keeps_page_and_exposes_http_retry_or_login(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
        start = js.index("    function flsRenderAjaxLoadError")
        end = js.index('    document.addEventListener("click"', start)
        navigation = js[start:end]
        result = subprocess.run(
            ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Node {
  constructor(tag){ this.tagName=tag.toUpperCase(); this.children=[]; this.textContent=''; this.className=''; this.dataset={}; this.href=''; this.style={}; }
  append(...items){ this.children.push(...items); }
  appendChild(item){ this.children.push(item); return item; }
  prepend(item){ this.children.unshift(item); }
  replaceChildren(...items){ this.children = items; }
  setAttribute(name, value){ if(name === 'data-fls-ajax-load-error') this.dataset.flsAjaxLoadError = value; }
  addEventListener(name, handler){ if(name === 'click') this.onclick = handler; }
  querySelector(selector){ return selector === '[data-fls-ajax-load-error]' ? this.children.find(item => item.dataset && item.dataset.flsAjaxLoadError) || null : null; }
}
const content = new Node('main');
let status = 503;
let navigated = '';
const context = {
  document: {querySelector: selector => selector === '.content' ? content : null, createElement: tag => new Node(tag)},
  location: {pathname:'/tasks', search:'', href:''},
  flsIsCacheablePage: () => false,
  setLoading: () => {},
  flsMarkButtonBusy: () => true,
  flsRestoreButton: () => {},
  flsReadTextResponse: async response => { const e = new Error('gateway diagnostic'); e.status = response.status; throw e; },
  fetch: async () => ({ok:false, status}),
};
vm.createContext(context);
vm.runInContext(input.navigation, context);
(async () => {
  await context.ajaxLoad('/tasks', true);
  assert.equal(navigated, '');
  assert.match(content.children[0].children[0].textContent, /HTTP 503/);
  assert.equal(content.children[0].children[1].children[0].textContent, '重新加载页面');
  status = 401;
  await context.ajaxLoad('/tasks', true);
  assert.equal(content.children[0].children[1].children[0].textContent, '重新登录');
  assert.match(content.children[0].children[1].children[0].href, /\/login\?next=/);
  console.log('ajax navigation recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
            input=json.dumps({"navigation": navigation}),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
