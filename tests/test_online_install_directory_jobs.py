import contextlib
import json
import os
import signal
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_backup_activity_lease as activity_tests
from tests import test_backup_data_access as access_tests
from tests import test_directory_process as process_tests
from tests import test_script_directory_jobs as script_tests
from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineInstallDirectoryJobTests(unittest.TestCase):
    def item(self):
        return {
            "id": "demo", "name": "Demo", "type": "raw", "link_name": "demo.py",
            "link": "https://example.invalid/demo.py", "install": "",
        }

    def submit(self, client):
        return client.post("/api/online-scripts/install/demo", json={
            "request_id": "a" * 32, "force": True,
        }, headers={"X-Token": TOKEN})

    def prepare(self):
        from fls_manager.online_scripts.source import save_online_script_cache

        save_online_script_cache([self.item()])

    def assert_blocked(self):
        script_tests.ScriptDirectoryJobTests().assert_restore_blocked()

    def assert_available(self):
        script_tests.ScriptDirectoryJobTests().assert_restore_available()

    def routes(self):
        for prefix in ("/api", ""):
            for action in ("install", "install-start-retry", "install-task-retry", "install-task-confirm",
                           "install-reload", "install-retry", "install-stop"):
                yield "post", prefix + "/online-scripts/" + action + "/" + "a" * 32
            yield "get", prefix + "/online-scripts/log/" + "a" * 32
            yield "get", prefix + "/online-scripts/install-history"

    def test_restore_and_pending_io_reject_all_install_routes_before_access_or_mutation(self):
        for mode in ("legacy", "native", "journal", "io"):
            with self.subTest(mode=mode), isolated_app() as (app, base), contextlib.ExitStack() as stack:
                from fls_manager.online_scripts import constants
                from fls_manager.routes.online_scripts import install, logs

                self.prepare()
                before = constants.ONLINE_INSTALL_JOBS_FILE.read_bytes() if constants.ONLINE_INSTALL_JOBS_FILE.exists() else None
                if mode in ("legacy", "native"):
                    stack.enter_context(activity_tests.BackupActivityLeaseTests().holder(mode))
                elif mode == "io":
                    stack.enter_context(access_tests.BackupDataAccessTests().paused_process(
                        "with directory_access(exclusive=True):\n"
                        " print('ready',flush=True)\n sys.stdin.readline()\n",
                    ))
                else:
                    from fls_manager.backup_commit import JOURNAL_NAME

                    directory = base / JOURNAL_NAME
                    directory.mkdir()
                    journal = directory / ("b" * 32 + ".json")
                    journal.write_bytes(b"{broken")
                lookups = [stack.enter_context(patch.object(module, name)) for module, name in (
                    (install, "get_online_script"), (install, "get_online_install_job"),
                    (install, "start_install_thread"), (logs, "get_online_install_job"),
                    (logs, "list_online_install_jobs"),
                )]
                for method, route in self.routes():
                    response = getattr(app.test_client(), method)(route, headers={"X-Token": TOKEN})
                    self.assertEqual(response.status_code, 503, (route, response.get_data(as_text=True)))
                    if route.startswith("/api/"):
                        self.assertEqual(response.json["code"], "panel_storage_busy")
                for lookup in lookups:
                    lookup.assert_not_called()
                after = constants.ONLINE_INSTALL_JOBS_FILE.read_bytes() if constants.ONLINE_INSTALL_JOBS_FILE.exists() else None
                self.assertEqual(after, before)
                self.assertEqual(constants.ONLINE_INSTALL_RUNNING, {})
                self.assertEqual(list((base / "scripts").iterdir()), [])
                self.assertEqual(list((base / "log").glob("online-script-install-*")), [])
                if mode == "journal":
                    self.assertEqual(journal.read_bytes(), b"{broken")

    def test_receipt_and_thread_handoff_keep_restore_blocked_until_worker_cleanup(self):
        with isolated_app() as (app, base):
            from fls_manager.online_scripts import install
            from fls_manager.routes.online_scripts import install as route

            self.prepare()
            queued = []
            original_persist = route.persist_online_install

            def persist(info):
                self.assert_blocked()
                return original_persist(info)

            def queue(worker):
                self.assert_blocked()
                queued.append(worker)

            with patch.object(route, "persist_online_install", side_effect=persist), patch.object(
                threading.Thread, "start", autospec=True, side_effect=queue,
            ):
                accepted = self.submit(app.test_client())
            self.assertEqual(accepted.status_code, 200, accepted.json)
            self.assertEqual(len(queued), 1)
            self.assert_blocked()
            try:
                with patch.object(install, "download_online_script_logged", return_value=base / "scripts" / "demo.py"):
                    queued[0].run()
            finally:
                self.assert_available()
            result = app.test_client().get("/api/online-scripts/log/" + "a" * 32, headers={"X-Token": TOKEN})
            self.assertEqual(result.json["status"], "已完成", result.json)

    def test_save_thread_constructor_and_start_failures_release_every_lease(self):
        for failure in ("receipt", "constructor", "start", "start_and_receipt"):
            with self.subTest(failure=failure), isolated_app() as (app, base), contextlib.ExitStack() as stack:
                from fls_manager.routes.online_scripts import install as route

                self.prepare()
                if failure == "receipt":
                    stack.enter_context(patch.object(route, "persist_online_install", side_effect=OSError("full")))
                else:
                    if failure == "constructor":
                        stack.enter_context(patch.object(threading, "Thread", side_effect=RuntimeError("quota")))
                    else:
                        stack.enter_context(patch.object(threading.Thread, "start", side_effect=RuntimeError("quota")))
                    if failure == "start_and_receipt":
                        original = route.persist_online_install
                        calls = []

                        def save_first(info):
                            calls.append(True)
                            if len(calls) > 1:
                                raise OSError("full")
                            return original(info)

                        stack.enter_context(patch.object(route, "persist_online_install", side_effect=save_first))
                response = self.submit(app.test_client())
                self.assertEqual(response.status_code, 500, response.json)
                self.assertEqual(list((base / "scripts").iterdir()), [])
                self.assert_available()

    def test_http_close_outputs_tasks_terminal_log_and_finally_keep_worker_lease(self):
        for stage in ("request", "close", "outputs", "tasks", "terminal_log", "finally"):
            with self.subTest(stage=stage), isolated_app() as (app, base), contextlib.ExitStack() as stack:
                from fls_manager.online_scripts import install
                from fls_manager import paths, storage

                self.prepare()
                entered, release = threading.Event(), threading.Event()
                workers = []
                original_start = install.start_install_thread

                def pause():
                    entered.set()
                    if not release.wait(10):
                        raise RuntimeError("test did not release install stage")

                def start(**kwargs):
                    workers.append(original_start(**kwargs))
                    return workers[-1]

                class Response:
                    headers = {"Content-Length": "12"}

                    def raise_for_status(self):
                        pass

                    def iter_content(self, **_kwargs):
                        yield b"print('ok')\n"

                    def close(self):
                        if stage == "close":
                            pause()

                def get(*_args, **_kwargs):
                    if stage == "request":
                        pause()
                    return Response()

                stack.enter_context(patch("fls_manager.routes.online_scripts.install.start_install_thread", side_effect=start))
                stack.enter_context(patch.object(install, "requests", SimpleNamespace(get=get)))
                if stage == "outputs":
                    original_capture = install.capture_install_outputs

                    def capture(*args, **kwargs):
                        pause()
                        return original_capture(*args, **kwargs)

                    stack.enter_context(patch.object(install, "capture_install_outputs", side_effect=capture))
                elif stage == "tasks":
                    item = self.item()
                    item["task_cron"] = [{"name": "Imported", "command": "echo ok", "cron": ""}]
                    from fls_manager.online_scripts.source import save_online_script_cache

                    save_online_script_cache([item])
                    original_import = install.import_task_if_needed

                    def import_tasks(*args, **kwargs):
                        pause()
                        return original_import(*args, **kwargs)

                    stack.enter_context(patch.object(install, "import_task_if_needed", side_effect=import_tasks))
                elif stage == "terminal_log":
                    original_log = install.append_log

                    def log(path, value):
                        if "===== 全部完成:" in value:
                            pause()
                        return original_log(path, value)

                    stack.enter_context(patch.object(install, "append_log", side_effect=log))
                elif stage == "finally":
                    class Stopping(set):
                        def discard(self, value):
                            pause()
                            return super().discard(value)

                    stack.enter_context(patch.object(install, "ONLINE_INSTALL_STOPPING", Stopping()))
                responses, errors = [], []

                def submit():
                    try:
                        responses.append(app.test_client().post("/api/online-scripts/install/demo", json={
                            "request_id": "a" * 32, "force": True, "import_task": stage == "tasks",
                            "task_indexes": ["1"] if stage == "tasks" else [],
                        }, headers={"X-Token": TOKEN}))
                    except BaseException as exc:
                        errors.append(exc)

                request_worker = threading.Thread(target=submit)
                request_worker.start()
                try:
                    self.assertTrue(entered.wait(10), stage)
                    self.assert_blocked()
                    storage.write_json(paths.DATA_DIR / "unrelated.json", {"ok": True})
                    self.assertTrue(storage.read_json(paths.DATA_DIR / "unrelated.json", {})["ok"])
                finally:
                    release.set()
                    request_worker.join(10)
                    self.assertFalse(request_worker.is_alive())
                    for worker in workers:
                        worker.join(10)
                        self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0].status_code, 200, responses[0].json)
                self.assertEqual((base / "scripts" / "demo.py").read_bytes(), b"print('ok')\n")
                self.assertEqual(list((base / "scripts").glob(".fls-download-*")), [])
                self.assert_available()

    def test_real_commands_preserve_exit_and_clean_descendants_before_releasing_restore(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts.install import InstallCommandError, run_logged_command

            for code in (0, 17):
                with self.subTest(code=code):
                    marker = base / ("children-" + str(code) + ".json")
                    command = (
                        "import json,os,pathlib,subprocess,sys\n"
                        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                        f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                        "print('fixture output',flush=True)\n"
                        f"sys.exit({code})\n"
                    )
                    if code:
                        with self.assertRaises(InstallCommandError) as caught:
                            run_logged_command([sys.executable, "-c", command], base / "scripts", base / "command.log")
                        self.assertEqual(caught.exception.returncode, code)
                    else:
                        run_logged_command([sys.executable, "-c", command], base / "scripts", base / "command.log")
                    for pid in json.loads(marker.read_text()):
                        process_tests.DirectoryProcessTests().assert_exited(pid)
                    self.assertIn("fixture output", (base / "command.log").read_text())
                    self.assert_available()
            with self.assertRaises(InstallCommandError) as caught:
                run_logged_command("exit 7", base / "scripts", base / "command.log", shell=True)
            self.assertEqual(caught.exception.returncode, 7)
            self.assert_available()

    def test_real_stop_and_timeout_keep_actual_exit_and_reap_entire_command_tree(self):
        for mode in ("stop", "timeout"):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager.online_scripts import install
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

                marker = base / "children.json"
                info = {
                    "id": "install", "script_id": "demo", "script_name": "Demo", "running": True,
                    "status": "运行中", "phase": "running_command", "log_file": str(base / "install.log"),
                }
                ONLINE_INSTALL_RUNNING["install"] = info
                command = (
                    "import json,os,pathlib,signal,subprocess,sys,time\n"
                    + ("signal.signal(signal.SIGTERM,signal.SIG_IGN)\n" if mode == "timeout" else "")
                    + "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                    + f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                    + "time.sleep(60)\n"
                )
                errors = []

                def run():
                    try:
                        install.run_logged_command([sys.executable, "-c", command], base / "scripts",
                                                   base / "install.log", install_id="install")
                    except Exception as exc:
                        errors.append(exc)

                worker = threading.Thread(target=run)
                with patch.object(install, "ONLINE_INSTALL_TIMEOUT_SECONDS", 1 if mode == "timeout" else 60):
                    worker.start()
                    try:
                        deadline = time.monotonic() + 10
                        while not marker.exists() and time.monotonic() < deadline and worker.is_alive():
                            time.sleep(0.01)
                        self.assertTrue(marker.exists(), errors)
                        self.assert_blocked()
                        if mode == "stop":
                            stopped = app.test_client().post("/api/online-scripts/install-stop/install", headers={"X-Token": TOKEN})
                            self.assertEqual(stopped.status_code, 200, stopped.json)
                        worker.join(10)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(len(errors), 1)
                        self.assertIsInstance(errors[0], install.InstallStopped if mode == "stop" else install.InstallTimeout)
                        if os.name == "posix":
                            self.assertEqual(errors[0].returncode, -signal.SIGTERM if mode == "stop" else -signal.SIGKILL)
                        else:
                            self.assertNotEqual(errors[0].returncode, 0)
                        for pid in json.loads(marker.read_text()):
                            process_tests.DirectoryProcessTests().assert_exited(pid)
                    finally:
                        proc = info.get("process")
                        if proc is not None:
                            proc.stop()
                        worker.join(10)
                self.assert_available()

    def test_unreadable_supervisor_result_is_unknown_without_reexecuting_command(self):
        with isolated_app() as (app, base):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            marker = base / "executions"
            info = {
                "id": "install", "script_id": "demo", "script_name": "Demo", "running": True,
                "status": "准备中", "log_file": str(base / "install.log"),
            }
            ONLINE_INSTALL_RUNNING["install"] = info
            original_process = install.DirectoryProcess
            original_command = install.run_logged_command

            def spawn(*args, **kwargs):
                proc = original_process(*args, **kwargs)
                original_wait = proc.wait

                def wait(timeout=None):
                    original_wait(timeout=timeout)
                    raise RuntimeError("unreadable supervisor receipt")

                proc.wait = wait
                return proc

            def command(*_args, **kwargs):
                code = f"from pathlib import Path\np=Path({str(marker)!r})\np.write_bytes(p.read_bytes()+b'x' if p.exists() else b'x')"
                return original_command([sys.executable, "-c", code], **kwargs)

            item = dict(self.item(), install="fixture command")
            with patch.object(install, "DirectoryProcess", side_effect=spawn), patch.object(
                install, "run_logged_command", side_effect=command,
            ), patch.object(install, "download_online_script_logged", return_value=base / "scripts" / "demo.py"):
                install.install_worker("install", item)
            self.assertEqual(info["status"], "结果未知")
            self.assertEqual(info["phase"], "unknown")
            self.assertFalse(info["running"])
            self.assertIsNone(info["returncode"])
            for _ in range(2):
                queried = app.test_client().get("/api/online-scripts/log/install", headers={"X-Token": TOKEN})
                self.assertEqual(queried.json["status"], "结果未知", queried.json)
            self.assertEqual(marker.read_bytes(), b"x")
            self.assert_available()

    def test_owner_crash_reaps_online_command_tree_before_restore_becomes_available(self):
        with isolated_app() as (_, base):
            marker = base / "children.json"
            command = (
                "import json,os,pathlib,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                f"pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
                "time.sleep(60)\n"
            )
            owner = (
                "import json,pathlib,sys,threading,time\n"
                "from fls_manager.online_scripts import install\n"
                "processes=[]\noriginal=install.DirectoryProcess\n"
                "def spawn(*args,**kwargs):\n"
                " process=original(*args,**kwargs)\n processes.append(process)\n return process\n"
                "install.DirectoryProcess=spawn\n"
                f"threading.Thread(target=install.run_logged_command,args=([sys.executable,'-c',{command!r}],"
                f"pathlib.Path({str(base / 'scripts')!r}),pathlib.Path({str(base / 'command.log')!r})),daemon=True).start()\n"
                "deadline=time.monotonic()+10\n"
                f"while not pathlib.Path({str(marker)!r}).exists() and time.monotonic()<deadline:time.sleep(0.01)\n"
                f"children=json.loads(pathlib.Path({str(marker)!r}).read_text())\n"
                "print('OWNER_READY '+json.dumps({'supervisor':processes[0].pid,'children':children}),flush=True)\n"
                "sys.stdin.readline()\n"
            )
            from fls_manager.backup_lease import release_process_lease, try_process_lease

            helper = process_tests.DirectoryProcessTests()
            with helper.paused_owner(owner) as (process, ready):
                self.assert_blocked()
                process.kill()
                process.wait(timeout=10)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    fd = try_process_lease(base / ".backup_native_restore.lock")
                    if fd is not None:
                        try:
                            for pid in ready["children"]:
                                self.assertTrue(process_tests.process_exited(pid), "lease released before command tree exited")
                        finally:
                            release_process_lease(fd)
                        break
                    time.sleep(0.01)
                else:
                    self.fail("online command supervisor did not release the restore lease")
                for pid in ready["children"] + [ready["supervisor"]]:
                    helper.assert_exited(pid)
            self.assert_available()
