import json
import re
import shutil
import subprocess
import unittest
from types import SimpleNamespace

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class DependencyInstallLogRecoveryTests(unittest.TestCase):
    def test_install_log_api_keeps_auth_boundary_and_running_state(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.state import DEPS_RUNNING

            log_file = base_dir / "log" / "deps-install-demo-demo-id.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("already installed output\n", encoding="utf-8")
            DEPS_RUNNING["demo-id"] = {
                "kind": "dependency",
                "package": "demo",
                "log_file": str(log_file),
                "process": SimpleNamespace(poll=lambda: None),
                "finished": False,
                "returncode": None,
                "log_fp": None,
            }

            client = app.test_client()
            denied = client.get("/api/deps/install-log/demo-id")
            self.assertEqual(denied.status_code, 401)
            self.assertEqual(denied.get_json()["msg"], "登录已过期")

            response = client.get(
                "/api/deps/install-log/demo-id", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["running"])
            self.assertEqual(response.get_json()["log"], "already installed output")

            page = client.get(
                "/deps/install-log/demo-id", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            for control_id in ("installPollError", "installPollRetry", "installPollLogin"):
                self.assertIn(f'id="{control_id}"', page)
            self.assertIn('id="installPollLogin" class="btn btn-primary" hidden>重新登录</a>', page)

    @unittest.skipUnless(shutil.which("node"), "Node is needed to exercise log polling")
    def test_polling_preserves_output_and_recovers_from_http_and_auth_errors(self):
        with isolated_app() as (app, _base_dir):
            html = app.test_client().get(
                "/deps/install-log/demo-id", headers={"X-Token": TOKEN},
            ).get_data(as_text=True)
            script = next(
                item for item in re.findall(r"<script>(.*?)</script>", html, re.S)
                if "async function loadLog()" in item
            )
            source = (ROOT / "fls_manager" / "static" / "fls.js").read_text(encoding="utf-8")
            helpers = source[
                source.index("function flsResponseErrorDiagnostic("):
                source.index("async function flsReadTextResponse(")
            ]
            result = subprocess.run(
                ["node", "-e", r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));

function makeContext(responses) {
  const elements = Object.fromEntries([
    'installPollError', 'installPollRetry', 'installPollLogin', 'installStatus',
    'installReturncode', 'installError', 'log'
  ].map(id => [id, {
    textContent: '', hidden: id === 'installPollError' || id === 'installPollRetry' || id === 'installPollLogin',
    href: '', listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; }
  }]));
  elements.log.textContent = 'previously read output';
  elements.installStatus.textContent = '安装中';
  let nextTimer = 1;
  const timers = new Map();
  const context = {
    window: {scrollTo() {}},
    document: {documentElement: {scrollHeight: 100}, getElementById: id => elements[id]},
    location: {pathname: '/deps/install-log/demo-id', search: '?back=%2Fdeps'},
    fetch: async () => responses.shift(),
    setInterval(callback, delay) { const id = nextTimer++; timers.set(id, {callback, delay}); return id; },
    clearInterval(id) { timers.delete(id); }
  };
  vm.createContext(context);
  vm.runInContext(input.helpers + input.script, context);
  return {context, elements, timers};
}

function response(status, body, contentType) {
  return {ok: status >= 200 && status < 300, status,
    headers: {get: () => contentType}, text: async () => body};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const transient = makeContext([
    response(200, JSON.stringify({running: true, status_label: '安装中', returncode: null,
      error: '', log: 'previously read output'}), 'application/json'),
    response(502, 'upstream temporarily unavailable', 'text/plain'),
    response(200, JSON.stringify({running: true, status_label: '安装中', returncode: null,
      error: '', log: 'latest output'}), 'application/json')
  ]);
  await tick();
  assert.equal(transient.elements.log.textContent, 'previously read output');
  const transientTimer = [...transient.timers.values()][0];
  await transientTimer.callback();
  assert.equal(transient.elements.log.textContent, 'previously read output');
  assert.equal(transient.elements.installStatus.textContent, '安装中');
  assert.match(transient.elements.installPollError.textContent, /upstream temporarily unavailable/);
  assert.equal(transient.elements.installPollRetry.hidden, false);
  assert.equal(transient.elements.installPollLogin.hidden, true);
  assert.equal(transient.timers.size, 0);
  await transient.elements.installPollRetry.listeners.click();
  assert.equal(transient.elements.installPollError.hidden, true);
  assert.equal(transient.elements.installPollRetry.hidden, true);
  assert.equal(transient.elements.log.textContent, 'latest output');
  assert.equal(transient.timers.size, 1);

  for(const status of [401, 403]) {
    const message = status === 401 ? '登录已过期' : '无权读取安装日志';
    const auth = makeContext([
      response(200, JSON.stringify({running: true, status_label: '安装中', returncode: null,
        error: '', log: 'previously read output'}), 'application/json'),
      response(status, JSON.stringify({ok: false, msg: message}), 'application/json')
    ]);
    await tick();
    const authTimer = [...auth.timers.values()][0];
    await authTimer.callback();
    assert.equal(auth.elements.log.textContent, 'previously read output');
    assert.equal(auth.elements.installStatus.textContent, '安装中');
    assert.equal(auth.elements.installPollRetry.hidden, true);
    assert.equal(auth.elements.installPollLogin.hidden, false);
    assert.equal(auth.elements.installPollLogin.href,
      '/login?next=%2Fdeps%2Finstall-log%2Fdemo-id%3Fback%3D%252Fdeps');
    assert.equal(auth.timers.size, 0);
  }
  console.log('dependency install log recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
                input=json.dumps({"script": script, "helpers": helpers}),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
