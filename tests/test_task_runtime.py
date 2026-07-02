import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def isolated_fls_modules():
    keys = ("FLS_BASE_DIR", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-runtime-test-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        try:
            yield Path(temp_dir)
        finally:
            cleanup_runtime_state()
            purge_fls_modules()

            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def cleanup_runtime_state():
    task_runner = sys.modules.get("fls_manager.task_runner")

    if task_runner is not None:
        for info in list(task_runner.RUNNING.values()):
            log_fp = info.get("log_fp") if isinstance(info, dict) else None
            if log_fp is not None:
                with contextlib.suppress(Exception):
                    log_fp.close()

        task_runner.RUNNING.clear()
        task_runner.STOPPED_MANUALLY.clear()

    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None

    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ImmediateThread:
    created = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.name = name
        self.started = False
        ImmediateThread.created.append(self)

    def start(self):
        self.started = True


class FakeProc:
    def __init__(self, return_code=0):
        self.return_code = return_code
        self.pid = 4321

    def wait(self, timeout=None):
        return self.return_code

    def poll(self):
        return self.return_code


class TimeoutProc(FakeProc):
    def wait(self, timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired(cmd="demo", timeout=timeout)

        return self.return_code


class FakeResponse:
    def __init__(self, status_code=200, text="OK", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")

        return self._json_data


class FakeSmtp:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.login_calls = []
        self.sendmail_calls = []
        self.closed = False

    def login(self, *args):
        self.login_calls.append(args)

    def sendmail(self, *args):
        self.sendmail_calls.append(args)

    def close(self):
        self.closed = True


class TaskRuntimeTests(unittest.TestCase):
    def test_increase_run_count_updates_persisted_task(self):
        with isolated_fls_modules():
            from fls_manager import paths, task_runner

            write_json(
                paths.TASK_FILE,
                [
                    {
                        "id": "t1",
                        "name": "demo",
                        "command": "echo ok",
                        "run_count": 2,
                    },
                    {
                        "id": "t2",
                        "name": "other",
                        "command": "echo skip",
                    },
                ],
            )

            with mock.patch.object(task_runner, "now_str", return_value="2026-07-03 10:00:00"):
                task_runner.increase_run_count("t1")

            tasks = read_json(paths.TASK_FILE)

            self.assertEqual(tasks[0]["run_count"], 3)
            self.assertEqual(tasks[0]["last_run_at"], "2026-07-03 10:00:00")
            self.assertEqual(tasks[0]["updated_at"], "2026-07-03 10:00:00")
            self.assertEqual(tasks[1]["run_count"], 0)

    def test_random_delay_and_retry_count_are_bounded(self):
        with isolated_fls_modules():
            from fls_manager import paths, task_runner

            write_json(paths.CONFIG_FILE, {"random_delay_seconds": 30})

            self.assertEqual(
                task_runner.task_random_delay_seconds({"random_delay": {"mode": "none"}}),
                0,
            )
            self.assertEqual(
                task_runner.task_random_delay_seconds({"random_delay": "bad"}),
                0,
            )

            with mock.patch.object(task_runner.random, "randint", return_value=7) as randint:
                value = task_runner.task_random_delay_seconds(
                    {"random_delay": {"mode": "default"}}
                )

            self.assertEqual(value, 7)
            randint.assert_called_once_with(1, 30)

            with mock.patch.object(task_runner.random, "randint", return_value=9) as randint:
                value = task_runner.task_random_delay_seconds(
                    {"random_delay": {"mode": "custom", "seconds": "999"}}
                )

            self.assertEqual(value, 9)
            randint.assert_called_once_with(1, 120)

            self.assertEqual(task_runner.task_retry_count({"retry_count": "bad"}), 0)
            self.assertEqual(task_runner.task_retry_count({"retry_count": -5}), 0)
            self.assertEqual(task_runner.task_retry_count({"retry_count": 99}), 20)
            self.assertEqual(task_runner.task_retry_count({"retry_count": "3"}), 3)

    def test_run_task_now_sets_running_state_without_starting_worker(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            task = {
                "id": "t1",
                "name": "Demo Task",
                "command": "echo ok",
            }
            log_file = Path(os.environ["FLS_BASE_DIR"]) / "log" / "demo.log"
            cmd_info = {
                "cmd": "echo ok",
                "shell": True,
                "cwd": str(Path(os.environ["FLS_BASE_DIR"])),
                "display_cmd": "echo ok",
            }

            ImmediateThread.created = []

            with mock.patch.object(task_runner, "get_task", return_value=task), \
                    mock.patch.object(task_runner, "build_command", return_value=cmd_info), \
                    mock.patch.object(task_runner, "log_file_for_task", return_value=log_file), \
                    mock.patch.object(task_runner.threading, "Thread", ImmediateThread):
                ok, msg = task_runner.run_task_now("t1", source="manual")

            self.assertTrue(ok)
            self.assertEqual(msg, "已提交启动")
            self.assertIn("t1", task_runner.RUNNING)
            self.assertEqual(task_runner.RUNNING["t1"]["status"], "starting")
            self.assertEqual(task_runner.RUNNING["t1"]["pid"], "-")
            self.assertTrue(log_file.exists())
            self.assertEqual(len(ImmediateThread.created), 1)
            self.assertTrue(ImmediateThread.created[0].started)
            self.assertEqual(ImmediateThread.created[0].args[0], "t1")

        cleanup_runtime_state()

    def test_run_task_now_rejects_missing_running_and_bad_command(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            with mock.patch.object(task_runner, "get_task", return_value=None):
                self.assertEqual(
                    task_runner.run_task_now("missing"),
                    (False, "任务不存在"),
                )

            task_runner.RUNNING["t1"] = {"status": "running"}

            with mock.patch.object(task_runner, "get_task", return_value={"id": "t1"}):
                self.assertEqual(
                    task_runner.run_task_now("t1"),
                    (False, "任务已在运行中"),
                )

            task_runner.RUNNING.clear()

            with mock.patch.object(task_runner, "get_task", return_value={"id": "t1"}), \
                    mock.patch.object(task_runner, "build_command", side_effect=ValueError("bad")):
                ok, msg = task_runner.run_task_now("t1")

            self.assertFalse(ok)
            self.assertEqual(msg, "命令解析失败：bad")

    def test_stop_task_now_removes_running_state_and_logs(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            log_file = Path(os.environ["FLS_BASE_DIR"]) / "log" / "stop.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            task_runner.RUNNING["t1"] = {
                "status": "running",
                "process": None,
                "log_file": str(log_file),
            }

            with mock.patch.object(task_runner, "now_str", return_value="2026-07-03 10:01:00"):
                ok, msg = task_runner.stop_task_now("t1")

            self.assertTrue(ok)
            self.assertEqual(msg, "已结束")
            self.assertNotIn("t1", task_runner.RUNNING)
            self.assertIn("t1", task_runner.STOPPED_MANUALLY)
            self.assertIn("手动结束任务", log_file.read_text(encoding="utf-8"))

            self.assertEqual(
                task_runner.stop_task_now("t1"),
                (False, "任务未运行"),
            )

    def test_start_task_worker_builds_environment_and_calls_attempt(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            task_runner.RUNNING["t1"] = {"status": "starting"}
            log_file = Path(os.environ["FLS_BASE_DIR"]) / "log" / "worker.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(log_file, "ab", buffering=0)

            task = {
                "id": "t1",
                "name": "Demo",
                "command": "echo ok",
                "env": {
                    "TASK_ONLY": "1",
                    "SHARED": "task",
                },
                "proxy_id": "p1",
                "retry_count": 2,
            }
            cmd_info = {
                "cmd": "echo ok",
                "shell": True,
                "cwd": str(Path(os.environ["FLS_BASE_DIR"])),
                "display_cmd": "echo ok",
            }
            captured = {}

            def fake_attempt(task_id, task_snapshot, cmd_info_arg, process_name,
                             log_file_arg, log_fp_arg, source, env, attempt,
                             total_attempts):
                captured.update({
                    "task_id": task_id,
                    "task_snapshot": task_snapshot,
                    "cmd_info": cmd_info_arg,
                    "process_name": process_name,
                    "log_file": log_file_arg,
                    "source": source,
                    "env": env,
                    "attempt": attempt,
                    "total_attempts": total_attempts,
                })
                return True

            try:
                with mock.patch.object(
                    task_runner,
                    "load_global_env",
                    return_value={"GLOBAL_ONLY": "g", "SHARED": "global"},
                ), mock.patch.object(
                    task_runner,
                    "apply_proxy_env",
                    side_effect=lambda env, proxy_id: {**env, "HTTP_PROXY": "http://proxy"},
                ), mock.patch.object(
                    task_runner,
                    "task_random_delay_seconds",
                    return_value=0,
                ), mock.patch.object(
                    task_runner,
                    "_start_task_attempt",
                    side_effect=fake_attempt,
                ):
                    task_runner._start_task_worker(
                        "t1",
                        dict(task),
                        cmd_info,
                        "FLS-Demo",
                        str(log_file),
                        log_fp,
                        "manual",
                    )
            finally:
                with contextlib.suppress(Exception):
                    log_fp.close()

            self.assertEqual(captured["task_id"], "t1")
            self.assertEqual(captured["attempt"], 1)
            self.assertEqual(captured["total_attempts"], 3)
            self.assertEqual(captured["env"]["GLOBAL_ONLY"], "g")
            self.assertEqual(captured["env"]["TASK_ONLY"], "1")
            self.assertEqual(captured["env"]["SHARED"], "task")
            self.assertEqual(captured["env"]["HTTP_PROXY"], "http://proxy")
            self.assertEqual(captured["env"]["FLS_TASK_ID"], "t1")
            self.assertEqual(captured["env"]["FLS_TASK_NAME"], "Demo")
            self.assertEqual(captured["env"]["FLS_TASK_PROCESS_NAME"], "FLS-Demo")
            self.assertEqual(captured["source"], "manual")

    def test_start_task_attempt_uses_popen_and_starts_watcher_thread(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            task_runner.RUNNING["t1"] = {"status": "starting"}
            log_file = Path(os.environ["FLS_BASE_DIR"]) / "log" / "attempt.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(log_file, "ab", buffering=0)
            fake_proc = FakeProc(return_code=None)
            cmd_info = {
                "cmd": ["python", "demo.py"],
                "shell": False,
                "cwd": str(Path(os.environ["FLS_BASE_DIR"])),
            }
            ImmediateThread.created = []

            try:
                with mock.patch.object(
                    task_runner.subprocess,
                    "Popen",
                    return_value=fake_proc,
                ) as popen, mock.patch.object(
                    task_runner,
                    "increase_run_count",
                ) as increase_run_count, mock.patch.object(
                    task_runner.threading,
                    "Thread",
                    ImmediateThread,
                ):
                    ok = task_runner._start_task_attempt(
                        "t1",
                        {"id": "t1", "name": "Demo"},
                        cmd_info,
                        "FLS-Demo",
                        str(log_file),
                        log_fp,
                        "manual",
                        {"ENV": "1"},
                        1,
                        3,
                    )
            finally:
                with contextlib.suppress(Exception):
                    log_fp.close()

            self.assertTrue(ok)
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0], ["python", "demo.py"])
            self.assertFalse(popen.call_args.kwargs["shell"])
            self.assertEqual(popen.call_args.kwargs["cwd"], cmd_info["cwd"])
            self.assertIs(popen.call_args.kwargs["stdout"], log_fp)
            self.assertEqual(popen.call_args.kwargs["stderr"], task_runner.subprocess.STDOUT)
            self.assertEqual(popen.call_args.kwargs["env"], {"ENV": "1"})
            if os.name != "nt":
                self.assertIs(popen.call_args.kwargs["preexec_fn"], task_runner.os.setsid)
            self.assertEqual(task_runner.RUNNING["t1"]["process"], fake_proc)
            self.assertEqual(task_runner.RUNNING["t1"]["pid"], 4321)
            self.assertEqual(task_runner.RUNNING["t1"]["status"], "running")
            self.assertEqual(task_runner.RUNNING["t1"]["attempt"], 1)
            self.assertEqual(task_runner.RUNNING["t1"]["total_attempts"], 3)
            increase_run_count.assert_called_once_with("t1")
            self.assertEqual(len(ImmediateThread.created), 1)
            watcher = ImmediateThread.created[0]
            self.assertIs(watcher.target, task_runner.task_finish_watcher)
            self.assertEqual(watcher.args[0], "t1")
            self.assertIs(watcher.args[2], fake_proc)
            self.assertEqual(watcher.args[-2:], (1, 3))
            self.assertTrue(watcher.started)

    def test_finish_watcher_skips_notification_when_task_notify_none(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            log_file = Path(os.environ["FLS_BASE_DIR"]) / "log" / "none.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(log_file, "ab", buffering=0)
            task_runner.RUNNING["t1"] = {
                "status": "running",
                "process": FakeProc(0),
                "log_file": str(log_file),
                "log_fp": log_fp,
            }

            with mock.patch.object(
                task_runner,
                "load_config",
                return_value={"task_timeout_seconds": 0},
            ), mock.patch.object(task_runner, "send_by_ids") as send_by_ids:
                task_runner.task_finish_watcher(
                    "t1",
                    {"id": "t1", "name": "Demo", "notify": {"mode": "none"}},
                    FakeProc(0),
                    str(log_file),
                    log_fp,
                    {"cmd": "echo ok"},
                    "FLS-Demo",
                    "manual",
                    {},
                )

            self.assertNotIn("t1", task_runner.RUNNING)
            send_by_ids.assert_not_called()
            self.assertIn("任务设置为不通知", log_file.read_text(encoding="utf-8"))

    def test_finish_watcher_sends_notification_with_user_log_content(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            log_file = Path(os.environ["FLS_BASE_DIR"]) / "log" / "notify.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(log_file, "ab", buffering=0)
            task_runner.RUNNING["t1"] = {
                "status": "running",
                "process": FakeProc(0),
                "log_file": str(log_file),
                "log_fp": log_fp,
            }

            with mock.patch.object(
                task_runner,
                "load_config",
                return_value={"task_timeout_seconds": 0},
            ), mock.patch.object(
                task_runner,
                "tail_file",
                return_value=(
                    "===== 启动任务: Demo =====\n"
                    "============================================================\n"
                    "script output\n"
                    "===== 任务已结束: now ====="
                ),
            ), mock.patch.object(
                task_runner,
                "send_by_ids",
                return_value=[{"name": "One", "ok": True, "msg": "ok"}],
            ) as send_by_ids:
                task_runner.task_finish_watcher(
                    "t1",
                    {
                        "id": "t1",
                        "name": "Demo",
                        "notify": {"mode": "custom", "ids": ["n1"]},
                    },
                    FakeProc(0),
                    str(log_file),
                    log_fp,
                    {"cmd": "echo ok"},
                    "FLS-Demo",
                    "manual",
                    {},
                )

            self.assertNotIn("t1", task_runner.RUNNING)
            send_by_ids.assert_called_once_with("Demo", "script output", ["n1"])
            self.assertIn("通知结果", log_file.read_text(encoding="utf-8"))

    def test_finish_watcher_retries_failed_attempt_before_notification(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            log_file = Path(os.environ["FLS_BASE_DIR"]) / "log" / "retry.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(log_file, "ab", buffering=0)
            task_runner.RUNNING["t1"] = {
                "status": "running",
                "process": FakeProc(2),
                "log_file": str(log_file),
                "log_fp": log_fp,
            }

            try:
                with mock.patch.object(
                    task_runner,
                    "load_config",
                    return_value={"task_timeout_seconds": 0},
                ), mock.patch.object(
                    task_runner,
                    "_start_task_attempt",
                    return_value=True,
                ) as start_attempt, mock.patch.object(
                    task_runner,
                    "send_by_ids",
                ) as send_by_ids:
                    task_runner.task_finish_watcher(
                        "t1",
                        {"id": "t1", "name": "Demo"},
                        FakeProc(2),
                        str(log_file),
                        log_fp,
                        {"cmd": "echo ok"},
                        "FLS-Demo",
                        "manual",
                        {},
                        attempt=1,
                        total_attempts=2,
                    )
            finally:
                with contextlib.suppress(Exception):
                    log_fp.close()

            self.assertEqual(task_runner.RUNNING["t1"]["status"], "retrying")
            start_attempt.assert_called_once()
            self.assertEqual(start_attempt.call_args.args[8], 2)
            self.assertEqual(start_attempt.call_args.args[9], 2)
            send_by_ids.assert_not_called()

    def test_finish_watcher_timeout_kills_process_without_retry(self):
        with isolated_fls_modules():
            from fls_manager import task_runner

            log_file = Path(os.environ["FLS_BASE_DIR"]) / "log" / "timeout.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_fp = open(log_file, "ab", buffering=0)
            proc = TimeoutProc(return_code=-9)
            task_runner.RUNNING["t1"] = {
                "status": "running",
                "process": proc,
                "log_file": str(log_file),
                "log_fp": log_fp,
            }

            with mock.patch.object(
                task_runner,
                "load_config",
                return_value={"task_timeout_seconds": 5},
            ), mock.patch.object(
                task_runner,
                "force_kill_process",
            ) as force_kill, mock.patch.object(
                task_runner,
                "_start_task_attempt",
            ) as start_attempt, mock.patch.object(
                task_runner,
                "send_by_ids",
            ) as send_by_ids:
                task_runner.task_finish_watcher(
                    "t1",
                    {"id": "t1", "name": "Demo", "notify": {"mode": "none"}},
                    proc,
                    str(log_file),
                    log_fp,
                    {"cmd": "echo ok"},
                    "FLS-Demo",
                    "manual",
                    {},
                    attempt=1,
                    total_attempts=2,
                )

            self.assertNotIn("t1", task_runner.RUNNING)
            force_kill.assert_called_once_with(proc)
            start_attempt.assert_not_called()
            send_by_ids.assert_not_called()
            text = log_file.read_text(encoding="utf-8")
            self.assertIn("任务超时", text)
            self.assertIn("任务设置为不通知", text)


class ProxyRuntimeTests(unittest.TestCase):
    def test_proxy_url_dict_and_environment_injection(self):
        with isolated_fls_modules():
            from fls_manager import paths, proxy

            write_json(
                paths.PROXY_FILE,
                [
                    {
                        "id": "http",
                        "name": "HTTP",
                        "type": "http",
                        "host": "proxy.local",
                        "port": "8080",
                        "username": "user",
                        "password": "pass",
                        "enabled": True,
                    },
                    {
                        "id": "socks",
                        "name": "SOCKS",
                        "type": "socks5",
                        "host": "127.0.0.1",
                        "port": "1080",
                        "enabled": True,
                    },
                    {
                        "id": "github",
                        "name": "GitHub",
                        "type": "github",
                        "url": "https://gh.example/",
                        "enabled": True,
                    },
                    {
                        "id": "disabled",
                        "name": "Disabled",
                        "type": "http",
                        "host": "proxy.local",
                        "port": "9999",
                        "enabled": False,
                    },
                ],
            )

            self.assertEqual(
                proxy.build_proxy_url(proxy.get_proxy("http")),
                "http://user:pass@proxy.local:8080",
            )
            self.assertEqual(
                proxy.requests_proxy_dict("http"),
                {
                    "http": "http://user:pass@proxy.local:8080",
                    "https": "http://user:pass@proxy.local:8080",
                },
            )

            env = proxy.apply_proxy_env({}, "socks")
            self.assertEqual(env["HTTP_PROXY"], "socks5://127.0.0.1:1080")
            self.assertEqual(env["HTTPS_PROXY"], "socks5://127.0.0.1:1080")
            self.assertEqual(env["ALL_PROXY"], "socks5://127.0.0.1:1080")

            self.assertEqual(proxy.build_proxy_url(proxy.get_proxy("github")), "https://gh.example")
            self.assertIsNone(proxy.requests_proxy_dict("github"))
            self.assertEqual(proxy.apply_proxy_env({"A": "1"}, "github"), {"A": "1"})
            self.assertIsNone(proxy.get_proxy("disabled"))

    def test_github_proxy_available_uses_cache_and_records_failure(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            github_proxy = {
                "id": "gh",
                "type": "github",
                "url": "https://gh.example/",
            }
            proxy._GITHUB_PROXY_HEALTH_CACHE.clear()

            with mock.patch.object(
                proxy.time,
                "time",
                side_effect=[100, 110, 200],
            ), mock.patch.object(
                proxy,
                "github_proxy_ping_object",
                side_effect=[{"status_code": 200}, RuntimeError("down")],
            ) as ping:
                self.assertTrue(proxy.github_proxy_available(github_proxy))
                self.assertTrue(proxy.github_proxy_available(github_proxy))
                self.assertFalse(proxy.github_proxy_available(github_proxy))

            self.assertEqual(ping.call_count, 2)
            self.assertEqual(proxy._GITHUB_PROXY_HEALTH_CACHE["gh"], (200, False))

    def test_github_proxy_available_skips_non_github_and_can_bypass_cache(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            github_proxy = {
                "id": "gh",
                "type": "github",
                "url": "https://gh.example/",
            }
            proxy._GITHUB_PROXY_HEALTH_CACHE.clear()
            proxy._GITHUB_PROXY_HEALTH_CACHE["gh"] = (100, True)

            with mock.patch.object(
                proxy,
                "github_proxy_ping_object",
                return_value={"status_code": 200},
            ) as ping:
                self.assertFalse(proxy.github_proxy_available({"type": "http"}))
                ping.assert_not_called()

            with mock.patch.object(proxy.time, "time", return_value=110), \
                    mock.patch.object(
                        proxy,
                        "github_proxy_ping_object",
                        side_effect=RuntimeError("down"),
                    ) as ping:
                self.assertFalse(
                    proxy.github_proxy_available(github_proxy, use_cache=False)
                )

            ping.assert_called_once_with(github_proxy, timeout=5)
            self.assertEqual(proxy._GITHUB_PROXY_HEALTH_CACHE["gh"], (110, False))


class LogCleanupTests(unittest.TestCase):
    def test_cleanup_logs_removes_oversized_and_keeps_recent_per_task(self):
        with isolated_fls_modules():
            from fls_manager import logs, paths

            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)

            def make_log(name, task_name, mtime, content="body"):
                path = paths.LOG_DIR / name
                path.write_text(
                    f"===== 启动任务: {task_name} =====\n{content}\n",
                    encoding="utf-8",
                )
                os.utime(path, (mtime, mtime))
                return path

            old_log = make_log("task-a-old.log", "TaskA", 100)
            mid_log = make_log("task-a-mid.log", "TaskA", 200)
            new_log = make_log("task-a-new.log", "TaskA", 300)
            other_log = make_log("task-b.log", "TaskB", 150)
            huge_log = paths.LOG_DIR / "huge.log"
            huge_log.write_bytes(b"x" * (1024 * 1024 + 1))

            with mock.patch.object(
                logs,
                "load_config",
                return_value={
                    "log_keep_per_task": 2,
                    "log_max_size_mb": 1,
                },
            ):
                logs.cleanup_logs()

            self.assertFalse(huge_log.exists())
            self.assertFalse(old_log.exists())
            self.assertTrue(mid_log.exists())
            self.assertTrue(new_log.exists())
            self.assertTrue(other_log.exists())


class NotifyRuntimeTests(unittest.TestCase):
    def test_task_notify_ids_supports_new_and_legacy_shapes(self):
        with isolated_fls_modules():
            from fls_manager import notify

            self.assertEqual(
                notify.task_notify_ids({"notify": {"mode": "none", "ids": ["n1"]}}),
                ["__none__"],
            )
            self.assertEqual(
                notify.task_notify_ids({"notify": {"mode": "default", "ids": []}}),
                ["__default__"],
            )
            self.assertEqual(
                notify.task_notify_ids({"notify": {"mode": "custom", "ids": ["n1", ""]}}),
                ["n1"],
            )
            self.assertEqual(
                notify.task_notify_ids({"notify_ids": ["old"]}),
                ["old"],
            )
            self.assertEqual(notify.task_notify_ids({}), ["__none__"])

    def test_send_by_ids_uses_enabled_default_and_deduplicates(self):
        with isolated_fls_modules():
            from fls_manager import notify

            items = [
                {"id": "n1", "name": "One", "enabled": True},
                {"id": "n2", "name": "Two", "enabled": True},
            ]

            with mock.patch.object(notify, "enabled_notify_items", return_value=items), \
                    mock.patch.object(notify, "default_notify_ids", return_value=["n2", "n1"]), \
                    mock.patch.object(
                        notify,
                        "get_notify_item",
                        side_effect=lambda item_id: next(
                            (item for item in items if item["id"] == item_id),
                            None,
                        ),
                    ), mock.patch.object(
                        notify,
                        "send_one",
                        return_value=(True, "ok"),
                    ) as send_one, mock.patch("builtins.print"):
                results = notify.send_by_ids("Title", "content", ["__default__", "n1", "n1"])

            self.assertEqual([result["id"] for result in results], ["n1"])
            self.assertEqual(results[0]["name"], "One")
            send_one.assert_called_once_with(items[0], "Title", "content")

            with mock.patch.object(notify, "enabled_notify_items", return_value=items), \
                    mock.patch.object(notify, "send_one") as send_one:
                self.assertEqual(notify.send_by_ids("Title", "content", ["__none__"]), [])
                send_one.assert_not_called()

    def test_send_one_webhook_uses_requests_request(self):
        with isolated_fls_modules():
            from fls_manager import notify

            response = FakeResponse(status_code=202, text="accepted")

            item = {
                "channel": "webhook",
                "enabled": True,
                "config": {
                    "WEBHOOK_URL": "https://hook.example/$title",
                    "WEBHOOK_METHOD": "POST",
                    "WEBHOOK_CONTENT_TYPE": "application/json",
                    "WEBHOOK_HEADERS": "X-Test: yes",
                },
            }

            with mock.patch.object(
                notify.requests,
                "request",
                return_value=response,
            ) as request:
                ok, msg = notify.send_one(item, "Title", "content")

            self.assertTrue(ok)
            self.assertEqual(msg, "202 accepted")
            request.assert_called_once()
            self.assertEqual(request.call_args.kwargs["method"], "POST")
            self.assertEqual(request.call_args.kwargs["url"], "https://hook.example/Title")
            self.assertEqual(
                request.call_args.kwargs["headers"],
                {
                    "X-Test": "yes",
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(
                json.loads(request.call_args.kwargs["data"].decode("utf-8")),
                {
                    "title": "Title",
                    "content": "content",
                },
            )

    def test_send_one_bark_uses_requests_post(self):
        with isolated_fls_modules():
            from fls_manager import notify

            item = {
                "channel": "bark",
                "enabled": True,
                "config": {
                    "BARK_PUSH": "device-token",
                    "BARK_GROUP": "FLS",
                },
            }

            with mock.patch.object(
                notify.requests,
                "post",
                return_value=FakeResponse(json_data={"code": 200}),
            ) as post:
                ok, msg = notify.send_one(item, "Title", "content")

            self.assertTrue(ok)
            self.assertIn("'code': 200", msg)
            post.assert_called_once()
            self.assertEqual(post.call_args.args[0], "https://api.day.app/device-token")
            self.assertEqual(
                post.call_args.kwargs["json"],
                {
                    "title": "Title",
                    "body": "content",
                    "group": "FLS",
                },
            )

    def test_send_one_smtp_uses_smtp_ssl(self):
        with isolated_fls_modules():
            from fls_manager import notify

            smtp = FakeSmtp()
            item = {
                "channel": "smtp",
                "enabled": True,
                "config": {
                    "SMTP_SERVER": "smtp.example.com:465",
                    "SMTP_SSL": "true",
                    "SMTP_EMAIL": "from@example.com",
                    "SMTP_PASSWORD": "secret",
                    "SMTP_NAME": "FLS",
                    "SMTP_TO": "to@example.com",
                },
            }

            with mock.patch.object(
                notify.smtplib,
                "SMTP_SSL",
                return_value=smtp,
            ) as smtp_ssl:
                ok, msg = notify.send_one(item, "Title", "content")

            self.assertTrue(ok)
            self.assertEqual(msg, "ok")
            smtp_ssl.assert_called_once_with("smtp.example.com", 465, timeout=20)
            self.assertEqual(smtp.login_calls, [("from@example.com", "secret")])
            self.assertEqual(smtp.sendmail_calls[0][0], "from@example.com")
            self.assertEqual(smtp.sendmail_calls[0][1], ["to@example.com"])
            self.assertTrue(smtp.closed)


if __name__ == "__main__":
    unittest.main()
