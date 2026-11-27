import json
import os
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.test_backup_dependency_process import process_exited
from tests.test_ui_route_components import ROOT, TOKEN, isolated_app


class InstallProcessTests(unittest.TestCase):
    def wait_file(self, path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"command did not create {path.name}")

    def assert_exited(self, pid):
        deadline = time.monotonic() + 10
        while not process_exited(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(process_exited(pid), f"process {pid} did not exit")

    def tree_command(self, marker, *, wait=True):
        script = (
            "import json,os,pathlib,subprocess,sys,time\n"
            "child=subprocess.Popen([sys.executable,'-u','-c','import time;time.sleep(60)'])\n"
            "print('installation-output',flush=True)\n"
            f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
        )
        if wait:
            script += "time.sleep(60)\n"
        return [sys.executable, "-u", "-c", script]

    def open_process(self, base, command):
        from fls_manager.install_process import InstallProcess
        from fls_manager.log_access import open_log_file

        log = open_log_file(base / "log" / "deps-install-demo-test.log", "ab", buffering=0)
        try:
            process = InstallProcess(command, stdout=log, stderr=subprocess.STDOUT, cwd=str(base))
        except BaseException:
            log.close()
            raise
        return process, log

    def test_completed_parent_reaps_inherited_output_children_before_returning(self):
        with isolated_app() as (_, base):
            from fls_manager.backup_lease import release_process_lease, try_process_lease

            marker = base / "tree.json"
            process, log = self.open_process(base, self.tree_command(marker, wait=False))
            workspace = process.result_path.parent
            try:
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual(process.poll(), 0)
                self.assertFalse(workspace.exists())
                for pid in json.loads(marker.read_text()):
                    self.assert_exited(pid)
                self.assertIn(b"installation-output", Path(log.name).read_bytes())
                self.assertIsNone(try_process_lease(log.log_lease_path))
                log.close()
                fd = try_process_lease(base / ".backup_native_restore.lock")
                try:
                    self.assertIsNotNone(fd)
                finally:
                    release_process_lease(fd)
            finally:
                process.stop()
                process.wait(timeout=10)
                log.close()

    def test_terminate_and_kill_use_owner_channel_without_signalling_supervisor(self):
        with isolated_app() as (_, base):
            marker = base / "tree.json"
            process, log = self.open_process(base, self.tree_command(marker))
            try:
                self.wait_file(marker)
                with patch.object(process._process, "terminate") as terminate, patch.object(
                    process._process, "kill"
                ) as kill:
                    process.terminate()
                    process.kill()
                    self.assertNotEqual(process.wait(timeout=10), 0)
                    terminate.assert_not_called()
                    kill.assert_not_called()
                for pid in json.loads(marker.read_text()):
                    self.assert_exited(pid)
            finally:
                process.stop()
                process.wait(timeout=10)
                log.close()

    def test_start_failure_stops_gated_supervisor_and_cleans_workspace(self):
        with isolated_app() as (_, base):
            from fls_manager.directory_process import DirectoryProcess

            marker = base / "must-not-start"
            seen = []

            def fail_start(process):
                seen.append(process)
                raise OSError("start gate unavailable")

            with patch.object(DirectoryProcess, "start", fail_start):
                with self.assertRaisesRegex(OSError, "start gate unavailable"):
                    self.open_process(base, [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"])
            self.assertEqual(len(seen), 1)
            self.assertIsNotNone(seen[0]._process.poll())
            self.assertFalse(marker.exists())
            self.assertFalse(seen[0].result_path.parent.exists())

    def test_workspace_cleanup_failure_does_not_report_terminal_success(self):
        with isolated_app() as (_, base):
            process, log = self.open_process(base, [sys.executable, "-c", "print('done')"])
            try:
                with patch.object(process._workspace, "cleanup", side_effect=OSError("workspace busy")):
                    with self.assertRaisesRegex(OSError, "workspace busy"):
                        process.wait(timeout=10)
                self.assertTrue(process.result_path.parent.exists())
                self.assertFalse(log.closed)
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertFalse(process.result_path.parent.exists())
            finally:
                process.stop()
                process.wait(timeout=10)
                log.close()

    def test_concurrent_waiters_share_result_and_cleanup_workspace_once(self):
        with isolated_app() as (_, base):
            process, log = self.open_process(base, [sys.executable, "-c", "print('done')"])
            try:
                workspace = process._workspace
                with patch.object(workspace, "cleanup", wraps=workspace.cleanup) as cleanup:
                    with ThreadPoolExecutor(max_workers=2) as workers:
                        results = list(workers.map(lambda _: process.wait(timeout=10), range(2)))
                    self.assertEqual(results, [0, 0])
                    cleanup.assert_called_once()
            finally:
                process.stop()
                process.wait(timeout=10)
                log.close()

    def test_log_close_failure_retains_live_state_until_poll_or_cancel_can_finish(self):
        for cancelling in (False, True):
            with self.subTest(cancelling=cancelling), isolated_app() as (_, base):
                from fls_manager.install_records import persist_install_job
                from fls_manager.routes import deps
                from fls_manager.state import DEPS_RUNNING

                process, log = self.open_process(base, [sys.executable, "-c", "print('done')"])
                info = {
                    "id": "close-failure", "request_id": "close-failure", "kind": "dependency",
                    "package": "demo", "log_file": log.name, "status": "running", "running": True,
                    "finished": False, "process": process, "log_fp": log, "updated_at": "now",
                    "cancel_request_id": "cancel-close" if cancelling else "",
                    "cancel_status": "requested" if cancelling else "",
                }
                DEPS_RUNNING[info["id"]] = info
                persist_install_job(info)
                try:
                    self.assertEqual(process.wait(timeout=10), 0)
                    with patch.object(log, "close", side_effect=OSError("log still open")):
                        with self.assertRaisesRegex(OSError, "log still open"):
                            if cancelling:
                                deps._finish_install_cancel(info, 0, "completed")
                            else:
                                deps.deps_install_result(info["id"])
                    self.assertTrue(info["running"])
                    self.assertFalse(info["finished"])
                    self.assertIs(info["log_fp"], log)
                    self.assertIs(info["process"], process)
                    result = deps.deps_install_result(info["id"])
                    self.assertEqual(result["status"], "succeeded")
                    self.assertTrue(log.closed)
                    self.assertIsNone(info["log_fp"])
                    if cancelling:
                        self.assertEqual(info["cancel_status"], "completed")
                finally:
                    process.stop()
                    process.wait(timeout=10)
                    log.close()

    def test_restore_owner_rejects_command_before_start_gate(self):
        with isolated_app() as (_, base):
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            from fls_manager.install_process import InstallProcess

            marker = base / "must-not-start"
            lease = try_process_lease(base / ".backup_native_restore.lock")
            try:
                self.assertIsNotNone(lease)
                with open(base / "output", "ab") as output:
                    with self.assertRaisesRegex(RuntimeError, "supervisor failed to start"):
                        InstallProcess(
                            [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"],
                            stdout=output, stderr=subprocess.STDOUT, cwd=str(base),
                        )
                self.assertFalse(marker.exists())
            finally:
                release_process_lease(lease)

    def test_owner_crash_preserves_supervisor_log_lease_until_tree_exits(self):
        with isolated_app() as (_, base):
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            from fls_manager.log_access import log_file_access

            marker = base / "tree.json"
            ready = base / "owner-ready.json"
            log_path = base / "log" / "deps-install-demo-orphan.log"
            script = (
                "import json,os,pathlib,sys,time\n"
                "from fls_manager.install_process import InstallProcess\n"
                "from fls_manager.log_access import open_log_file\n"
                f"log=open_log_file({str(log_path)!r},'ab',buffering=0)\n"
                f"process=InstallProcess({self.tree_command(marker)!r},stdout=log,stderr=-2,cwd={str(base)!r})\n"
                "log.close()\n"
                f"pathlib.Path({str(ready)!r}).write_text(json.dumps({{'pid':process.pid}}))\n"
                "sys.stdin.buffer.read(1)\n"
                "os._exit(0)\n"
            )
            with open(base / "owner-errors", "wb") as errors:
                owner = subprocess.Popen([sys.executable, "-u", "-c", script], cwd=ROOT,
                                         stdin=subprocess.PIPE, stdout=errors, stderr=errors)
                try:
                    self.wait_file(ready)
                    self.wait_file(marker)
                    # The owner already closed its log; only the supervisor holds this lease.
                    with log_file_access(log_path) as access:
                        lease_path = access.lock_path
                    self.assertIsNone(try_process_lease(lease_path))
                    owner.stdin.write(b"x")
                    owner.stdin.flush()
                    self.assertEqual(owner.wait(timeout=10), 0)
                    for pid in json.loads(marker.read_text()):
                        self.assert_exited(pid)
                    self.assert_exited(json.loads(ready.read_text())["pid"])
                    for path in (lease_path, base / ".backup_native_restore.lock"):
                        fd = try_process_lease(path)
                        try:
                            self.assertIsNotNone(fd)
                        finally:
                            release_process_lease(fd)
                finally:
                    if owner.poll() is None:
                        owner.kill()
                    owner.wait(timeout=10)
                    owner.stdin.close()

    def test_dependency_and_runtime_routes_cancel_supervised_tree_and_replay(self):
        kinds = ("dependency",) if os.name == "nt" else ("dependency", "runtime")
        for kind in kinds:
            with self.subTest(kind=kind), isolated_app() as (app, base):
                from fls_manager.install_process import InstallProcess
                from fls_manager.routes import deps, runtime
                from fls_manager.state import DEPS_RUNNING

                marker = base / "tree.json"
                module = deps if kind == "dependency" else runtime
                endpoint = "/api/deps/install" if kind == "dependency" else "/api/install/runtime/node"
                body = {"request_id": "supervised-install", "name": "demo"}

                def spawn(_command, **options):
                    return InstallProcess(self.tree_command(marker), **options)

                with patch.object(module, "InstallProcess", side_effect=spawn), patch.object(
                    runtime, "runtime_install_command", return_value="echo harmless"
                ):
                    response = app.test_client().post(endpoint, json=body, headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                process = DEPS_RUNNING[body["request_id"]]["process"]
                try:
                    self.wait_file(marker)
                    cancel = app.test_client().post(
                        "/api/deps/install-cancel/supervised-install",
                        json={"request_id": "stop-install"}, headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(cancel.status_code, 200, cancel.get_data(as_text=True))
                    replay = app.test_client().post(
                        "/api/deps/install-cancel/supervised-install",
                        json={"request_id": "stop-install"}, headers={"X-Token": TOKEN},
                    )
                    self.assertEqual(replay.status_code, 200)
                    self.assertEqual(cancel.json["cancel_status"], "cancelled")
                    self.assertEqual(replay.json["cancel_status"], "cancelled")
                    for pid in json.loads(marker.read_text()):
                        self.assert_exited(pid)
                    self.assertIsNone(DEPS_RUNNING[body["request_id"]]["log_fp"])
                    self.assertFalse(process.result_path.parent.exists())
                finally:
                    process.stop()
                    process.wait(timeout=10)
                    log = DEPS_RUNNING[body["request_id"]].get("log_fp")
                    if log is not None:
                        log.close()

    def test_install_uninstall_and_retry_routes_replay_one_supervisor(self):
        scenarios = [
            ("dependency", "install", False), ("dependency", "install", True),
            ("dependency", "uninstall", False), ("dependency", "uninstall", True),
        ]
        if os.name != "nt":
            scenarios.extend((("runtime", "install", False), ("runtime", "install", True)))
        for kind, operation, retry in scenarios:
            with self.subTest(kind=kind, operation=operation, retry=retry), isolated_app() as (app, base):
                from fls_manager.install_process import InstallProcess
                from fls_manager.install_records import get_install_job, persist_install_job
                from fls_manager.routes import deps, runtime
                from fls_manager.state import DEPS_RUNNING

                release = base / "release-command"
                command = [sys.executable, "-u", "-c", (
                    "import pathlib,time\n"
                    f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n"
                    "print('completed',flush=True)\n"
                )]
                package = "node" if kind == "runtime" else "demo"
                body = {"request_id": "new-install", "name": package}
                if retry:
                    persist_install_job({
                        "id": "previous-install", "request_id": "previous-install", "kind": kind,
                        "operation": operation, "package": package,
                        "log_file": str(base / "log" / "previous-install.log"),
                        "running": False, "finished": True, "status": "failed",
                        "returncode": 1, "error": "previous failure", "updated_at": "now",
                    })
                    endpoint = "/api/deps/install-retry/previous-install"
                    module = deps
                elif kind == "runtime":
                    endpoint = "/api/install/runtime/node"
                    module = runtime
                else:
                    endpoint = "/api/deps/" + operation
                    module = deps

                def spawn(_command, **options):
                    return InstallProcess(command, **options)

                process = None
                try:
                    with patch.object(module, "InstallProcess", side_effect=spawn) as start, patch.object(
                        deps, "_installed_packages", return_value=[{"name": "demo", "version": "1"}]
                    ), patch.object(runtime, "runtime_install_command", return_value="echo harmless"):
                        response = app.test_client().post(endpoint, json=body, headers={"X-Token": TOKEN})
                        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                        process = DEPS_RUNNING[body["request_id"]]["process"]
                        replay = app.test_client().post(endpoint, json=body, headers={"X-Token": TOKEN})
                        self.assertEqual(replay.status_code, 200, replay.get_data(as_text=True))
                        start.assert_called_once()
                        self.assertIsInstance(process, InstallProcess)
                    release.touch()
                    self.assertEqual(process.wait(timeout=10), 0)
                    history = app.test_client().get("/api/deps/install-history", headers={"X-Token": TOKEN})
                    self.assertEqual(history.status_code, 200)
                    record = get_install_job(body["request_id"])
                    self.assertEqual(record["status"], "succeeded")
                    self.assertEqual(record["operation"], operation)
                    if retry:
                        self.assertEqual(record["retry_of"], "previous-install")
                        self.assertEqual(get_install_job("previous-install")["retried_by"], body["request_id"])
                    self.assertFalse(process.result_path.parent.exists())
                finally:
                    if process is not None:
                        process.stop()
                        process.wait(timeout=10)
                    for info in DEPS_RUNNING.values():
                        log = info.get("log_fp")
                        if log is not None:
                            log.close()
