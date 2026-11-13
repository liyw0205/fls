import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "runtime-status-token"


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(prefix="fls-runtime-status-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "runtime-status-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        for name in list(sys.modules):
            if name == "fls_manager" or name.startswith("fls_manager."):
                sys.modules.pop(name, None)
        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from fls_manager.app import create_app

            yield create_app()
        finally:
            state = sys.modules.get("fls_manager.state")
            scheduler = getattr(state, "scheduler", None) if state else None
            if scheduler is not None:
                with contextlib.suppress(Exception):
                    scheduler.shutdown(wait=False)
            for name in list(sys.modules):
                if name == "fls_manager" or name.startswith("fls_manager."):
                    sys.modules.pop(name, None)
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class RuntimeDetectionTests(unittest.TestCase):
    def test_probe_requires_zero_exit_and_nonempty_version(self):
        with isolated_app():
            from fls_manager.routes.status import probe_runtime

            with patch(
                "fls_manager.routes.status.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="v1\nextra details\n"),
            ) as run:
                result = probe_runtime(["custom-node", "--version"])
            self.assertEqual(result, {"status": "available", "version": "v1", "detail": ""})
            self.assertEqual(run.call_args.kwargs["timeout"], 3)
            self.assertEqual(run.call_args.args[0], ["custom-node", "--version"])

            with patch(
                "fls_manager.routes.status.subprocess.run",
                return_value=SimpleNamespace(returncode=7, stdout="loader <failure>"),
            ):
                result = probe_runtime(["node", "--version"])
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["version"], "")
            self.assertIn("退出码 7", result["detail"])
            self.assertIn("loader <failure>", result["detail"])

            with patch(
                "fls_manager.routes.status.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="  \n"),
            ):
                self.assertEqual(probe_runtime(["node", "--version"])["status"], "error")

            with patch(
                "fls_manager.routes.status.subprocess.run",
                return_value=SimpleNamespace(returncode=9, stdout="x" * 1000),
            ):
                result = probe_runtime(["node", "--version"])
            self.assertTrue(result["detail"].endswith("x" * 240))
            self.assertLess(len(result["detail"]), 300)

    def test_probe_distinguishes_missing_timeout_and_execution_error(self):
        with isolated_app():
            from fls_manager.routes.status import probe_runtime

            self.assertEqual(probe_runtime(None)["status"], "missing")
            with patch(
                "fls_manager.routes.status.subprocess.run",
                side_effect=FileNotFoundError("runtime not found"),
            ):
                result = probe_runtime(["missing-runtime", "--version"])
            self.assertEqual(result["status"], "missing")
            self.assertIn("missing-runtime", result["detail"])

            with patch(
                "fls_manager.routes.status.subprocess.run",
                side_effect=FileNotFoundError("interpreter not found"),
            ), patch("fls_manager.routes.status.shutil.which", return_value="/opt/broken-runtime"):
                result = probe_runtime(["broken-runtime", "--version"])
            self.assertEqual(result["status"], "error")
            self.assertIn("文件存在", result["detail"])

            with patch(
                "fls_manager.routes.status.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["slow-runtime"], 3),
            ):
                result = probe_runtime(["slow-runtime", "--version"])
            self.assertEqual(result["status"], "timeout")
            self.assertIn("3 秒", result["detail"])

            with patch(
                "fls_manager.routes.status.subprocess.run",
                side_effect=PermissionError("permission denied"),
            ):
                result = probe_runtime(["locked-runtime", "--version"])
            self.assertEqual(result["status"], "error")
            self.assertIn("permission denied", result["detail"])

    def test_runtime_probes_are_bounded_ordered_and_use_task_interpreters(self):
        with isolated_app():
            from fls_manager.routes import status

            active = 0
            peak = 0
            lock = threading.Lock()
            calls = []
            first_batch = threading.Barrier(status.RUNTIME_PROBE_WORKERS)

            def probe(command):
                nonlocal active, peak
                if command is None:
                    return {"status": "missing", "version": "", "detail": "未找到 tsx / ts-node"}
                with lock:
                    active += 1
                    peak = max(peak, active)
                    calls.append(command)
                    is_first_batch = len(calls) <= status.RUNTIME_PROBE_WORKERS
                if is_first_batch:
                    first_batch.wait(timeout=2)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return {"status": "available", "version": command[0], "detail": ""}

            with patch.object(status, "PYTHON_BIN", "/opt/custom/python"), patch.object(
                status, "BASH_BIN", "/opt/custom/bash"
            ), patch.object(status, "NODE_BIN", "/opt/custom/node"), patch.object(
                status.shutil, "which", return_value=None
            ), patch.object(status, "probe_runtime", side_effect=probe):
                items = status.runtime_items()

            self.assertLessEqual(peak, status.RUNTIME_PROBE_WORKERS)
            self.assertEqual(peak, status.RUNTIME_PROBE_WORKERS)
            self.assertEqual([item["key"] for item in items], [
                "python", "bash", "node", "typescript", "php", "ruby", "perl", "lua", "java"
            ])
            self.assertEqual(items[0]["command"], "/opt/custom/python")
            self.assertEqual(items[0]["status"], "available")
            self.assertEqual(items[0]["version"], "/opt/custom/python")
            self.assertIn(["/opt/custom/python", "--version"], calls)
            self.assertIn(["/opt/custom/bash", "--version"], calls)
            self.assertIn(["/opt/custom/node", "--version"], calls)
            self.assertEqual(items[3]["status"], "missing")
            self.assertFalse(any("probe" in item for item in items))

    def test_status_page_does_not_offer_install_for_probe_errors(self):
        with isolated_app() as app:
            from fls_manager.routes import status

            items = [
                {"name": "Error runtime", "suffix": ".err", "command": "runtime", "status": "error",
                 "version": "退出码 7：loader <failure>", "install_url": "/install/runtime/error"},
                {"name": "Slow runtime", "suffix": ".slow", "command": "slow", "status": "timeout",
                 "version": "超过 3 秒", "install_url": "/install/runtime/slow"},
            ]
            with patch.object(status, "runtime_items", return_value=items):
                html = app.test_client().get("/panel/status", headers={"X-Token": TOKEN}).get_data(as_text=True)

            self.assertIn("检测失败", html)
            self.assertIn("检测超时", html)
            self.assertIn("loader &lt;failure&gt;", html)
            self.assertNotIn('action="/install/runtime/error"', html)
            self.assertNotIn('action="/install/runtime/slow"', html)
            self.assertIn('href="/panel/status"', html)

    def test_only_confirmed_missing_runtimes_offer_install(self):
        with isolated_app() as app:
            from fls_manager.routes import status

            with patch.object(status, "runtime_items", return_value=[
                {"name": "Missing runtime", "suffix": ".missing", "command": "missing", "status": "missing",
                 "version": "未找到可执行文件", "install_url": "/install/runtime/missing"},
                {"name": "Ready runtime", "suffix": ".ready", "command": "ready", "status": "available",
                 "version": "v1", "install_url": "/install/runtime/ready"},
            ]):
                html = app.test_client().get("/panel/status", headers={"X-Token": TOKEN}).get_data(as_text=True)

            self.assertIn('action="/install/runtime/missing"', html)
            self.assertIn("安装Missing runtime", html)
            self.assertIn("已安装", html)
            self.assertNotIn('action="/install/runtime/ready"', html)

    def test_page_classifies_real_probe_results_and_rechecks_after_recovery(self):
        with isolated_app() as app:
            from fls_manager.routes import status

            client = app.test_client()
            with patch.object(status, "PYTHON_BIN", sys.executable), patch.object(
                status.shutil, "which", return_value=None
            ), patch.object(status.subprocess, "run", return_value=SimpleNamespace(
                returncode=7, stdout="loader <fault>"
            )) as run:
                html = client.get("/panel/status", headers={"X-Token": TOKEN}).get_data(as_text=True)
                self.assertEqual(html.count('role="status">检测失败</span>'), 7)
                self.assertEqual(html.count('role="status">已安装</span>'), 1)
                self.assertEqual(html.count('method="post" action="/install/runtime/'), 1)
                self.assertIn('action="/install/runtime/typescript"', html)
                self.assertIn("loader &lt;fault&gt;", html)
                self.assertNotIn("loader <fault>", html)
                run.return_value = SimpleNamespace(returncode=0, stdout="v1 recovered")
                html = client.get("/panel/status", headers={"X-Token": TOKEN}).get_data(as_text=True)
                self.assertEqual(html.count('role="status">已安装</span>'), 8)
                self.assertNotIn("loader &lt;fault&gt;", html)
                self.assertEqual(run.call_count, 14)

    def test_timeout_stops_a_real_version_process(self):
        with isolated_app():
            from fls_manager.routes import status

            started = time.monotonic()
            with patch.object(status, "RUNTIME_PROBE_TIMEOUT", 0.1):
                result = status.probe_runtime([sys.executable, "-c", "import time; time.sleep(10)"])
            self.assertEqual(result["status"], "timeout")
            self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
