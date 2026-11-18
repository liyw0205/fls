import json
import re
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class DependencyInstallLogRecoveryTests(unittest.TestCase):
    def test_install_request_replay_is_idempotent_for_dependency_and_runtime(self):
        for runtime in (False, True):
            with self.subTest(runtime=runtime), isolated_app() as (app, base_dir):
                from fls_manager.state import DEPS_RUNNING

                proc = SimpleNamespace(poll=lambda: None)
                endpoint = "/install/runtime/node" if runtime else "/deps/install"
                data = {"request_id": "replay-install-1", "name": "demo-package"}
                if runtime:
                    data = {"request_id": "replay-runtime-1"}
                module = "runtime" if runtime else "deps"
                with patch(
                    f"fls_manager.routes.{module}.subprocess.Popen", return_value=proc,
                ) as popen, patch(
                    "fls_manager.routes.runtime.runtime_install_command",
                    return_value="echo runtime",
                ):
                    first = app.test_client().post(endpoint, data=data, headers={"X-Token": TOKEN})
                    second = app.test_client().post(endpoint, data=data, headers={"X-Token": TOKEN})
                self.assertEqual(first.status_code, 302)
                self.assertEqual(second.status_code, 302)
                self.assertEqual(first.headers["Location"], second.headers["Location"])
                self.assertEqual(popen.call_count, 1)
                install_id = first.headers["Location"].split("/deps/install-log/", 1)[1].split("?", 1)[0]
                self.assertIn(install_id, DEPS_RUNNING)
                self.assertEqual(DEPS_RUNNING[install_id]["request_id"], install_id)

    def test_dependency_install_json_api_returns_stable_replay_state(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.state import DEPS_RUNNING

            proc = SimpleNamespace(poll=lambda: None)
            with patch("fls_manager.routes.deps.subprocess.Popen", return_value=proc) as popen:
                first = app.test_client().post(
                    "/api/deps/install",
                    json={"request_id": "api-install-1", "name": "demo-package"},
                    headers={"X-Token": TOKEN},
                )
                replay = app.test_client().post(
                    "/api/deps/install",
                    json={"request_id": "api-install-1", "name": "demo-package"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertTrue(first.json["ok"])
            self.assertEqual(first.json["request_id"], "api-install-1")
            self.assertEqual(replay.status_code, 200)
            self.assertTrue(replay.json["ok"])
            self.assertEqual(replay.json["request_id"], first.json["request_id"])
            self.assertEqual(popen.call_count, 1)
            self.assertIn("api-install-1", DEPS_RUNNING)

    def test_runtime_install_json_api_replays_without_starting_second_process(self):
        with isolated_app() as (app, _base_dir):
            proc = SimpleNamespace(poll=lambda: None)
            with patch("fls_manager.routes.runtime.runtime_install_command", return_value="echo runtime"), patch(
                "fls_manager.routes.runtime.subprocess.Popen", return_value=proc,
            ) as popen:
                first = app.test_client().post(
                    "/api/install/runtime/node",
                    json={"request_id": "api-runtime-1"},
                    headers={"X-Token": TOKEN},
                )
                replay = app.test_client().post(
                    "/api/install/runtime/node",
                    json={"request_id": "api-runtime-1"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json["request_id"], "api-runtime-1")
            self.assertEqual(popen.call_count, 1)

    def test_json_replay_reports_persisted_terminal_state(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.install_records import persist_install_job

            persist_install_job({
                "id": "finished-install", "request_id": "finished-install",
                "kind": "dependency", "package": "demo-package", "log_file": str(_base_dir / "log" / "deps-install-demo-finished-install.log"),
                "running": False, "finished": True, "status": "succeeded",
                "returncode": 0, "error": "", "start_time": 1,
                "updated_at": "2026-11-18 17:00:00",
            })
            persist_install_job({
                "id": "failed-runtime", "request_id": "failed-runtime",
                "kind": "runtime", "package": "node", "log_file": str(_base_dir / "log" / "system-install-node-failed-runtime.log"),
                "running": False, "finished": True, "status": "failed",
                "returncode": 7, "error": "package manager failed", "start_time": 1,
                "updated_at": "2026-11-18 17:01:00",
            })
            client = app.test_client()
            with patch("fls_manager.routes.deps.subprocess.Popen") as dep_popen, patch(
                "fls_manager.routes.runtime.subprocess.Popen"
            ) as runtime_popen:
                succeeded = client.post(
                    "/api/deps/install", json={"request_id": "finished-install", "name": "demo-package"},
                    headers={"X-Token": TOKEN},
                )
                failed = client.post(
                    "/api/install/runtime/node", json={"request_id": "failed-runtime"},
                    headers={"X-Token": TOKEN},
                )
            self.assertEqual(succeeded.status_code, 200)
            self.assertEqual(succeeded.json["status"], "succeeded")
            self.assertFalse(succeeded.json["running"])
            self.assertEqual(succeeded.json["returncode"], 0)
            self.assertEqual(failed.status_code, 200)
            self.assertEqual(failed.json["status"], "failed")
            self.assertFalse(failed.json["running"])
            self.assertEqual(failed.json["returncode"], 7)
            dep_popen.assert_not_called()
            runtime_popen.assert_not_called()

    def test_restart_running_install_becomes_unknown_and_history_is_queryable(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job
            from fls_manager.state import DEPS_RUNNING

            install_id = "restart-dependency"
            log_file = base_dir / "log" / f"deps-install-demo-{install_id}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("install output\n", encoding="utf-8")
            persist_install_job({
                "id": install_id, "request_id": install_id, "kind": "dependency",
                "package": "demo", "log_file": str(log_file), "running": True,
                "status": "running", "returncode": None, "error": "",
                "start_time": 1, "updated_at": "2026-11-18 15:00:00",
            })
            DEPS_RUNNING.clear()
            client = app.test_client()
            self.assertEqual(client.get("/api/deps/install-history").status_code, 401)
            invalid_kind = client.get(
                "/api/deps/install-history?kind=other", headers={"X-Token": TOKEN},
            )
            self.assertEqual(invalid_kind.status_code, 400)
            result = client.get(
                f"/api/deps/install-log/{install_id}", headers={"X-Token": TOKEN},
            )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json["status"], "unknown")
            self.assertFalse(result.json["running"])
            self.assertIn("install output", result.json["log"])
            history = client.get(
                "/api/deps/install-history?kind=dependency&limit=10",
                headers={"X-Token": TOKEN},
            )
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json["items"][0]["id"], install_id)
            self.assertEqual(history.json["items"][0]["status"], "unknown")
            page = client.get("/deps/install-history?kind=dependency", headers={"X-Token": TOKEN})
            self.assertEqual(page.status_code, 200)
            self.assertIn(f"/deps/install-log/{install_id}", page.get_data(as_text=True))
            self.assertTrue(INSTALL_JOBS_FILE.exists())

    def test_install_history_rejects_corrupt_or_symlink_receipt_without_overwrite(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE

            client = app.test_client()
            INSTALL_JOBS_FILE.write_text("not-json", encoding="utf-8")
            original = INSTALL_JOBS_FILE.read_bytes()
            bad = client.get("/api/deps/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(bad.status_code, 500)
            self.assertEqual(INSTALL_JOBS_FILE.read_bytes(), original)

            target = base_dir.parent / f"{base_dir.name}-install-record-target.json"
            target.write_text("{}", encoding="utf-8")
            INSTALL_JOBS_FILE.unlink()
            INSTALL_JOBS_FILE.symlink_to(target)
            linked = client.get("/api/deps/install-history", headers={"X-Token": TOKEN})
            self.assertEqual(linked.status_code, 500)
            self.assertIn("符号链接", linked.json["msg"])
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")
            target.unlink()

    def test_install_log_rejects_outside_and_symlink_paths_without_reading_them(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.install_records import INSTALL_JOBS_FILE

            outside = base_dir.parent / f"{base_dir.name}-outside-deps.log"
            outside.write_text("outside dependency secret\n", encoding="utf-8")
            record = {
                "id": "unsafe-dependency", "request_id": "unsafe-dependency",
                "kind": "dependency", "package": "demo", "log_file": str(outside),
                "start_time": 1, "running": False, "status": "failed",
                "returncode": 1, "error": "failed", "updated_at": "2026-11-18 16:00:00",
                "retry_of": "", "retried_by": "",
            }
            INSTALL_JOBS_FILE.write_text(json.dumps({record["id"]: record}), encoding="utf-8")
            client = app.test_client()
            response = client.get(
                "/api/deps/install-log/unsafe-dependency", headers={"X-Token": TOKEN},
            )
            self.assertEqual(response.status_code, 500)
            self.assertIn("受控日志目录", response.json["error"])
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside dependency secret\n")

            target = base_dir / "real-dependency.log"
            target.write_text("target\n", encoding="utf-8")
            link = base_dir / "log" / "deps-install-demo-unsafe-dependency.log"
            link.symlink_to(target)
            record["log_file"] = str(link)
            INSTALL_JOBS_FILE.write_text(json.dumps({record["id"]: record}), encoding="utf-8")
            linked = client.get(
                "/api/deps/install-log/unsafe-dependency", headers={"X-Token": TOKEN},
            )
            self.assertEqual(linked.status_code, 500)
            self.assertIn("受控日志目录", linked.json["error"])
            self.assertEqual(target.read_text(encoding="utf-8"), "target\n")
            outside.unlink()

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
  for(const body of ['null', '{}', '{"running":"false","log":"bad"}']) {
    const invalid = makeContext([
      response(200, JSON.stringify({running: true, status_label: '安装中', log: 'preserved'}), 'application/json'),
      response(200, body, 'application/json')
    ]);
    await tick();
    await [...invalid.timers.values()][0].callback();
    assert.equal(invalid.elements.log.textContent, 'preserved');
    assert.equal(invalid.elements.installStatus.textContent, '安装中');
    assert.equal(invalid.elements.installPollRetry.hidden, false);
    assert.equal(invalid.timers.size, 0);
  }
  {
    const stale = makeContext([
      response(200, JSON.stringify({running: true, status_label: '安装中', log: 'current'}), 'application/json')
    ]);
    await tick();
    let resolve;
    let requests = 0;
    stale.context.fetch = () => { requests++; return new Promise(done => { resolve = done; }); };
    const pending = [...stale.timers.values()][0].callback();
    await stale.elements.installPollRetry.listeners.click();
    assert.equal(requests, 1);
    stale.elements.installPollError = {};
    stale.context.window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
    stale.timers.clear();
    resolve(response(200, JSON.stringify({running: true, log: 'old page result'}), 'application/json'));
    await pending;
    assert.equal(stale.elements.log.textContent, 'current');
    assert.equal(stale.timers.size, 0);
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
