import contextlib
import json
import os
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

    def wait(self, timeout=None):
        return self.return_code

    def poll(self):
        return self.return_code


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


if __name__ == "__main__":
    unittest.main()
