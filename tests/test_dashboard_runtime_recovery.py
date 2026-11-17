import json
import re
import subprocess
import unittest

from tests.test_ui_route_components import TOKEN, isolated_app


class DashboardRuntimeRecoveryTests(unittest.TestCase):
    def test_dashboard_exposes_runtime_recovery_controls(self):
        with isolated_app() as (app, _base):
            response = app.test_client().get("/", headers={"X-Token": TOKEN})
            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('id="flsDashboardRuntimeRecovery" hidden', html)
            self.assertIn('id="flsDashboardRuntimeRetry"', html)
            self.assertIn('id="flsDashboardRuntimeLogin"', html)
            self.assertIn("flsDashboardShowRuntimeError", html)
            self.assertIn("flsDashboardRuntimeBlocked", html)

    def test_runtime_http_failure_stops_polling_and_retry_clears_diagnostic(self):
        with isolated_app() as (app, _base):
            html = app.test_client().get("/", headers={"X-Token": TOKEN}).get_data(as_text=True)
            script = next(
                block for block in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "flsDashboardRefreshRuntime" in block
            )
            node_script = r"""
const vm = require('vm');
const source = JSON.parse(process.env.FLS_DASHBOARD_SCRIPT);
const elements = {};
for (const id of [
  'flsDashboardNow', 'flsDashboardUptime', 'flsDashboardSystemCpu',
  'flsDashboardPanelCpuCurrent', 'flsDashboardPanelCpuPeak',
  'flsDashboardRuntimeRecovery', 'flsDashboardRuntimeErrorText',
  'flsDashboardRuntimeRetry', 'flsDashboardRuntimeLogin'
]) elements[id] = {textContent:'', hidden:id === 'flsDashboardRuntimeRecovery', disabled:false, href:'', listener:null};
elements.flsDashboardRuntimeRetry.addEventListener = (name, callback) => { elements.flsDashboardRuntimeRetry.listener = callback; };
const document = {
  hidden: false,
  getElementById: id => elements[id] || null,
  addEventListener: () => {},
  removeEventListener: () => {}
};
let status = 503;
let activeIntervals = 0;
const context = {
  document,
  window: {},
  location: {pathname: '/', search: ''},
  encodeURIComponent,
  Error,
  setInterval: () => ++activeIntervals,
  clearInterval: () => { activeIntervals = 0; },
  fetch: async () => ({
    ok: status === 200,
    status,
    json: async () => status === 200 ? {
      ok: true, current_time: 'now', panel_uptime: 'up',
      system_cpu_percent: '1%', panel_cpu_current: '2%', panel_cpu_peak: '3%'
    } : {ok: false, msg: 'runtime backend unavailable'}
  }),
  flsReadJsonResponse: async response => {
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(payload.msg);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }
};
vm.runInNewContext(source, context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  if (elements.flsDashboardRuntimeRecovery.hidden) throw new Error('failure was hidden');
  if (!elements.flsDashboardRuntimeErrorText.textContent.includes('HTTP 503')) throw new Error('status missing');
  if (activeIntervals !== 0) throw new Error('polling was not stopped');
  status = 401;
  await context.flsDashboardRefreshRuntime();
  if (elements.flsDashboardRuntimeLogin.hidden) throw new Error('login recovery was hidden');
  if (!elements.flsDashboardRuntimeLogin.href.includes('/login?next=')) throw new Error('login recovery link missing');
  status = 200;
  await elements.flsDashboardRuntimeRetry.listener();
  if (!elements.flsDashboardRuntimeRecovery.hidden) throw new Error('recovery stayed visible');
  if (elements.flsDashboardNow.textContent !== 'now') throw new Error('runtime values not refreshed');
  process.stdout.write('ok');
})().catch(error => { console.error(error); process.exit(1); });
"""
            result = subprocess.run(
                ["node", "-e", node_script],
                env={"FLS_DASHBOARD_SCRIPT": json.dumps(script)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "ok")


if __name__ == "__main__":
    unittest.main()
