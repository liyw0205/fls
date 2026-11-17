import json
import subprocess
import unittest
from pathlib import Path

from tests.test_ui_route_components import TOKEN, isolated_app


class LogPollRecoveryTests(unittest.TestCase):
    def test_debug_about_and_file_pages_keep_log_and_expose_recovery_region(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.routes.about.state import ABOUT_JOBS
            from fls_manager.routes.scripts.debug import SCRIPT_DEBUG_RUNNING

            log_dir = base_dir / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "live.log").write_text("existing log\n", encoding="utf-8")
            debug_log = log_dir / "debug.log"
            debug_log.write_text("debug log\n", encoding="utf-8")
            about_log = log_dir / "about.log"
            about_log.write_text("about log\n", encoding="utf-8")

            SCRIPT_DEBUG_RUNNING["d1"] = {
                "id": "d1", "script": "demo.py", "rel": "demo.py",
                "log_file": str(debug_log), "running": True, "status": "running",
                "process": object(), "pid": 1, "returncode": None,
                "error": "", "created_at": "now", "updated_at": "now",
            }
            ABOUT_JOBS["j1"] = {
                "id": "j1", "title": "fixture", "action": "fixture",
                "log_file": str(about_log), "running": True, "status": "running",
                "returncode": None, "updated_at": "now",
            }

            client = app.test_client()
            pages = (
                "/scripts/debug-log/d1",
                "/about/job-log/j1",
                "/logfile/live.log",
            )
            for route in pages:
                with self.subTest(route=route):
                    html = client.get(route, headers={"X-Token": TOKEN}).get_data(as_text=True)
                    self.assertIn('id="logRecovery"', html)
                    self.assertIn("flsLogPollFailed", html)
                    self.assertIn("flsLogPollRecovered", html)
                    self.assertNotIn('document.getElementById("log").textContent = "日志读取失败', html)

    def test_common_log_poll_recovery_helper_is_bounded_and_auth_aware(self):
        source = Path("fls_manager/static/fls.js").read_text(encoding="utf-8")
        self.assertIn("function flsLogPollFailed(error, loader, label)", source)
        self.assertIn("现有日志已保留", source)
        self.assertIn('status === 401 || status === 403', source)
        self.assertIn("clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__)", source)
        self.assertIn("立即重试", source)

    def test_common_helper_preserves_log_and_offers_retry_or_login(self):
        source = Path("fls_manager/static/fls.js").read_text(encoding="utf-8")
        helper = source[source.index("function flsLogPollRecovered") : source.index('document.addEventListener("submit"')]
        script = f"""
const helper = {json.dumps(helper)};
const recovery = {{hidden:true, children:[], replaceChildren(){{this.children=[];}}, append(...items){{this.children.push(...items);}}}};
global.window = {{__FLS_ACTIVE_LOG_INTERVAL__: 42}};
global.location = {{pathname:"/logs/live", search:"?back=/logs"}};
global.document = {{
  getElementById(id) {{ return id === "logRecovery" ? recovery : null; }},
  createElement(tag) {{ return {{tagName:tag, addEventListener(name, callback){{this.callback=callback;}}, setAttribute(){{}}}}; }}
}};
global.clearInterval = value => {{ if(value !== 42 && value !== 9) throw new Error("wrong interval"); global.window.__FLS_ACTIVE_LOG_INTERVAL__ = null; }};
global.setInterval = () => 7;
eval(helper);
let attempts = 0;
flsLogPollFailed({{status:502}}, () => attempts++, "日志文件");
if (recovery.hidden || recovery.children.length !== 2 || recovery.children[1].tagName !== "button") throw new Error("retry control missing");
recovery.children[1].callback();
if (attempts !== 1 || window.__FLS_ACTIVE_LOG_INTERVAL__ !== null) throw new Error("retry did not stop polling");
window.__FLS_ACTIVE_LOG_INTERVAL__ = 9;
flsLogPollFailed({{status:401}}, () => attempts++, "日志文件");
if (recovery.children[1].tagName !== "a" || recovery.children[1].href.indexOf("/login?next=") !== 0) throw new Error("login control missing");
console.log("ok");
"""
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
