import contextlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "high-side-effect-token"


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-high-side-effect-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "high-side-effect-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()

        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))

            from fls_manager.app import create_app

            yield create_app(), Path(temp_dir)
        finally:
            cleanup_side_effect_state()
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


def cleanup_side_effect_state():
    state = sys.modules.get("fls_manager.state")
    deps = getattr(state, "DEPS_RUNNING", {}) if state else {}

    for info in list(deps.values()):
        log_fp = info.get("log_fp") if isinstance(info, dict) else None
        if log_fp is not None:
            with contextlib.suppress(Exception):
                log_fp.close()

    if state is not None:
        deps.clear()

    backup_common = sys.modules.get("fls_manager.routes.backup._common")
    if backup_common is not None:
        backup_jobs = getattr(backup_common, "BACKUP_JOBS", None)
        if backup_jobs is not None:
            backup_jobs.clear()

    about_state = sys.modules.get("fls_manager.routes.about.state")
    if about_state is not None:
        about_jobs = getattr(about_state, "ABOUT_JOBS", None)
        if about_jobs is not None:
            about_jobs.clear()

    scheduler = getattr(state, "scheduler", None) if state else None
    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def add_tar_file(tar, name, content):
    info = tarfile.TarInfo(name)
    info.size = len(content)
    tar.addfile(info, io.BytesIO(content))


class DependencyInstallIsolationTests(unittest.TestCase):
    def test_deps_install_registers_process_and_redirects_without_real_pip(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.state import DEPS_RUNNING

            proc = SimpleNamespace(pid=4321, poll=lambda: None)
            with patch(
                "fls_manager.routes.deps.subprocess.Popen",
                return_value=proc,
            ) as popen:
                response = app.test_client().post(
                    "/deps/install",
                    data={"name": "demo-package"},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            location = urlsplit(response.headers["Location"])
            self.assertEqual(location.path.rsplit("/", 1)[0], "/deps/install-log")
            self.assertEqual(parse_qs(location.query), {"back": ["/deps"]})

            install_id = location.path.rsplit("/", 1)[1]
            self.assertIn(install_id, DEPS_RUNNING)
            info = DEPS_RUNNING[install_id]
            self.assertEqual(info["package"], "demo-package")
            self.assertIs(info["process"], proc)
            self.assertFalse(info["finished"])
            self.assertTrue(info["log_file"].startswith(str(base_dir / "log")))
            self.assertIn("pip install demo-package", Path(info["log_file"]).read_text(encoding="utf-8"))

            popen.assert_called_once()
            self.assertEqual(
                popen.call_args.args[0][1:],
                ["-m", "pip", "install", "demo-package"],
            )
            self.assertEqual(popen.call_args.kwargs["cwd"], str(base_dir))

    def test_runtime_install_registers_process_without_running_package_manager(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.state import DEPS_RUNNING

            proc = SimpleNamespace(pid=9876, poll=lambda: None)
            with patch(
                "fls_manager.routes.runtime.runtime_install_command",
                return_value="echo runtime-install",
            ) as build_command:
                with patch(
                    "fls_manager.routes.runtime.subprocess.Popen",
                    return_value=proc,
                ) as popen:
                    response = app.test_client().get(
                        "/install/runtime/node",
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 302)
            location = urlsplit(response.headers["Location"])
            self.assertEqual(location.path.rsplit("/", 1)[0], "/deps/install-log")
            self.assertEqual(parse_qs(location.query), {"back": ["/panel/status"]})
            self.assertEqual(build_command.call_args.args, ("node",))

            install_id = location.path.rsplit("/", 1)[1]
            self.assertIn(install_id, DEPS_RUNNING)
            info = DEPS_RUNNING[install_id]
            self.assertEqual(info["package"], "node")
            self.assertIs(info["process"], proc)
            self.assertTrue(info["log_file"].startswith(str(base_dir / "log")))
            popen.assert_called_once_with(
                ["sh", "-lc", "echo runtime-install"],
                stdout=info["log_fp"],
                stderr=subprocess.STDOUT,
                cwd=str(base_dir),
                env=os.environ.copy(),
            )


class NotificationIsolationTests(unittest.TestCase):
    def test_notify_test_renders_mock_result_without_network(self):
        with isolated_app() as (app, _base_dir):
            item = {
                "id": "notify-1",
                "name": "Mock webhook",
                "channel": "webhook",
                "enabled": True,
                "config": {},
            }

            with patch(
                "fls_manager.routes.notify.test.get_notify_item",
                return_value=item,
            ):
                with patch(
                    "fls_manager.routes.notify.test.send_one",
                    return_value=(True, "mock sent"),
                ) as send_one:
                    response = app.test_client().get(
                        "/notify/test/notify-1",
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("通知测试结果", html)
            self.assertIn("mock sent", html)
            send_one.assert_called_once()
            self.assertIs(send_one.call_args.args[0], item)
            self.assertEqual(send_one.call_args.args[1], "FLS 通知测试")

    def test_notify_new_test_persists_item_and_uses_mock_sender(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            with patch(
                "fls_manager.routes.notify.pages.send_one",
                return_value=(True, "mock sent"),
            ) as send_one:
                response = app.test_client().post(
                    "/notify/new",
                    data={
                        "name": "Mock bark",
                        "channel": "bark",
                        "BARK_PUSH": "fixture-token",
                        "action": "test",
                    },
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 302)
            items = read_json(base_dir / "data" / "config.json")["notify_items"]
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["name"], "Mock bark")
            self.assertEqual(items[0]["config"]["BARK_PUSH"], "fixture-token")
            self.assertEqual(items[0]["channel"], "bark")
            send_one.assert_called_once()
            self.assertEqual(send_one.call_args.args[0], items[0])
            self.assertEqual(send_one.call_args.args[1], "FLS 通知测试")
            self.assertTrue(paths.CONFIG_FILE.exists())


class ProxyQualityIsolationTests(unittest.TestCase):
    def test_saved_proxy_quality_uses_mock_detector_without_network(self):
        with isolated_app() as (app, _base_dir):
            proxy = {
                "id": "proxy-1",
                "name": "Fixture proxy",
                "type": "http",
                "host": "proxy.local",
                "port": "8080",
            }
            result = [
                {
                    "url": "https://a.example",
                    "ok": True,
                    "status_code": 200,
                    "elapsed": "1 ms",
                }
            ]

            with patch(
                "fls_manager.routes.proxy.api.get_proxy_for_test",
                return_value=proxy,
            ):
                with patch(
                    "fls_manager.routes.proxy.api.quality_proxy_object",
                    return_value=result,
                ) as detect_quality:
                    response = app.test_client().get(
                        "/api/proxy/quality/proxy-1?urls=https://a.example",
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.get_json(),
                {"ok": True, "name": "Fixture proxy", "items": result},
            )
            detect_quality.assert_called_once_with(proxy, ["https://a.example"])

    def test_proxy_quality_form_uses_mock_detector_without_network(self):
        with isolated_app() as (app, _base_dir):
            result = [
                {
                    "url": "https://a.example",
                    "ok": False,
                    "status_code": "-",
                    "elapsed": "mock failure",
                }
            ]

            with patch(
                "fls_manager.routes.proxy.api.quality_proxy_object",
                return_value=result,
            ) as detect_quality:
                response = app.test_client().post(
                    "/api/proxy/quality-form",
                    data={
                        "name": "Fixture proxy",
                        "type": "http",
                        "host": "proxy.local",
                        "port": "8080",
                        "quality_urls": "a.example",
                    },
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"ok": True, "items": result})
            detector_proxy = detect_quality.call_args.args[0]
            self.assertEqual(detector_proxy["host"], "proxy.local")
            self.assertEqual(detector_proxy["port"], "8080")
            self.assertEqual(detect_quality.call_args.args[1], ["https://a.example"])


class AboutAuxiliaryIsolationTests(unittest.TestCase):
    def test_network_time_sync_uses_mock_clock_and_reloads_scheduler(self):
        from datetime import datetime, timedelta, timezone

        with isolated_app() as (app, _base_dir):
            network_time = datetime(2026, 9, 18, 1, 2, 3, tzinfo=timezone.utc)
            calibration = {
                "timezone_text": "UTC+8",
                "panel_time_offset_seconds": 0,
            }

            with patch(
                "fls_manager.routes.about.time_sync.fetch_network_utc_time",
                return_value=network_time,
            ) as fetch_time:
                with patch(
                    "fls_manager.routes.about.time_sync.set_panel_time_calibration",
                    return_value=calibration,
                ) as set_calibration:
                    with patch(
                        "fls_manager.routes.about.time_sync.reload_scheduler"
                    ) as reload_scheduler:
                        response = app.test_client().post(
                            "/about/time-sync",
                            data={"mode": "beijing"},
                            headers={"X-Token": TOKEN},
                        )

            self.assertEqual(response.status_code, 200)
            self.assertIn("北京时间校准完成", response.get_data(as_text=True))
            fetch_time.assert_called_once_with()
            set_calibration.assert_called_once_with(
                offset_hours=8,
                virtual_now=datetime(
                    2026,
                    9,
                    18,
                    9,
                    2,
                    3,
                    tzinfo=timezone(timedelta(hours=8), "UTC+8"),
                ),
            )
            reload_scheduler.assert_called_once_with()

    def test_about_refresh_log_registers_mock_job_without_git_network(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.about.helpers import refresh_log_worker

            with patch(
                "fls_manager.routes.about.version.git_available",
                return_value=True,
            ):
                with patch(
                    "fls_manager.routes.about.version.is_git_repo",
                    return_value=True,
                ):
                    with patch(
                        "fls_manager.routes.about.version.start_about_job",
                        return_value="job-refresh",
                    ) as start_job:
                        response = app.test_client().post(
                            "/about/refresh-log",
                            headers={"X-Token": TOKEN},
                        )

            self.assertEqual(response.status_code, 302)
            location = urlsplit(response.headers["Location"])
            self.assertEqual(location.path, "/about/job-log/job-refresh")
            self.assertEqual(parse_qs(location.query), {"back": ["/about"]})
            start_job.assert_called_once_with(
                action="refresh-log",
                title="刷新更新日志",
                target=refresh_log_worker,
            )

    def test_about_update_version_registers_mock_job_without_git_mutation(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.about.helpers import update_version_worker

            with patch(
                "fls_manager.routes.about.version.git_available",
                return_value=True,
            ):
                with patch(
                    "fls_manager.routes.about.version.is_git_repo",
                    return_value=True,
                ):
                    with patch(
                        "fls_manager.routes.about.version.start_about_job",
                        return_value="job-update",
                    ) as start_job:
                        response = app.test_client().post(
                            "/about/update-version",
                            data={"version": "abcdef1234567"},
                            headers={"X-Token": TOKEN},
                        )

            self.assertEqual(response.status_code, 302)
            location = urlsplit(response.headers["Location"])
            self.assertEqual(location.path, "/about/job-log/job-update")
            self.assertEqual(parse_qs(location.query), {"back": ["/about"]})
            start_job.assert_called_once_with(
                action="update-version",
                title="更新版本 abcdef123456",
                target=update_version_worker,
                args=("abcdef1234567",),
            )


class BackupIsolationTests(unittest.TestCase):
    def test_backup_create_api_registers_job_without_starting_worker(self):
        with isolated_app() as (app, _base_dir):
            with patch(
                "fls_manager.routes.backup.api.start_backup_job",
                return_value="job-fixture",
            ) as start_backup_job:
                response = app.test_client().post(
                    "/api/backup/create",
                    data={"items": ["data", "scripts"]},
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"ok": True, "job_id": "job-fixture"})
            start_backup_job.assert_called_once_with(["data", "scripts"])

    def test_backup_import_restores_isolated_dirs_and_mocks_dependency_install(self):
        with isolated_app() as (app, base_dir):
            from fls_manager import paths

            paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
            paths.SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
            backup_dir = paths.DATA_DIR / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            (paths.DATA_DIR / "old.json").write_text("old", encoding="utf-8")
            (paths.SCRIPT_DIR / "old.py").write_text("old", encoding="utf-8")
            (backup_dir / "keep.tar.gz").write_bytes(b"keep")

            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                add_tar_file(tar, "payload/data/config.json", b'{"restored": true}')
                add_tar_file(tar, "payload/scripts/demo.py", b"print('restored')\n")
                add_tar_file(tar, "payload/dependencies.txt", b"fixture-package==1.0\n")
            archive.seek(0)

            with patch(
                "fls_manager.routes.backup.restore.install_dependencies",
                return_value=(True, str(base_dir / "log" / "restore-deps.log")),
            ) as install_dependencies:
                with patch(
                    "fls_manager.routes.backup.restore.reload_scheduler"
                ) as reload_scheduler:
                    response = app.test_client().post(
                        "/backup/import",
                        data={
                            "file": (archive, "fixture.tar.gz"),
                            "restore_items": ["data", "scripts"],
                            "restore_deps": "1",
                        },
                        headers={"X-Token": TOKEN},
                        content_type="multipart/form-data",
                    )

            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("备份导入完成", html)
            self.assertIn("依赖恢复成功", html)
            self.assertEqual(
                json.loads((paths.DATA_DIR / "config.json").read_text(encoding="utf-8")),
                {"restored": True},
            )
            self.assertEqual(
                (paths.SCRIPT_DIR / "demo.py").read_text(encoding="utf-8"),
                "print('restored')\n",
            )
            self.assertTrue((paths.DATA_DIR / "backups" / "keep.tar.gz").exists())
            self.assertFalse((paths.DATA_DIR / "old.json").exists())
            self.assertFalse((paths.SCRIPT_DIR / "old.py").exists())
            install_dependencies.assert_called_once()
            self.assertTrue(str(install_dependencies.call_args.args[0]).endswith("dependencies.txt"))
            reload_scheduler.assert_called_once_with()


class PanelControlIsolationTests(unittest.TestCase):
    def test_systemd_panel_control_uses_detached_transient_unit(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.routes.about import helpers

            script = base_dir / "fls.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")

            def fake_which(name):
                if name == "systemd-run":
                    return "/usr/bin/systemd-run"
                if name == "systemctl":
                    return "/usr/bin/systemctl"
                return None

            with patch.object(helpers, "systemd_fls_unit", return_value="fls.service"):
                with patch.object(helpers.shutil, "which", side_effect=fake_which):
                    command = helpers.build_fls_control_command("restart")

            self.assertEqual(command[0], "/usr/bin/systemd-run")
            self.assertIn("--service-type=oneshot", command)
            self.assertIn("fls.service", command[-1])
            self.assertIn("systemctl restart fls.service", command[-1])
            self.assertNotIn("kill -TERM", command[-1])

    def test_shell_fls_process_scan_does_not_use_broad_match(self):
        script = (ROOT / "fls.sh").read_text(encoding="utf-8")

        self.assertNotIn('pgrep -f "fls-manager.py"', script)
        self.assertNotIn('pgrep -f "fls-manager"', script)
        self.assertIn('pgrep -x "fls-manager"', script)

    def test_panel_restart_posts_status_without_starting_control_thread(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.routes.about.panel_control import delayed_restart_panel

            script = base_dir / "fls.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            thread = MagicMock()

            with patch(
                "fls_manager.routes.about.panel_control.threading.Thread",
                return_value=thread,
            ) as thread_factory:
                with patch(
                    "fls_manager.routes.about.helpers.subprocess.Popen"
                ) as popen:
                    response = app.test_client().post(
                        "/about/restart-panel",
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertIn("正在重启面板", response.get_data(as_text=True))
            thread_factory.assert_called_once_with(
                target=delayed_restart_panel,
                daemon=True,
                name="fls-panel-restart",
            )
            thread.start.assert_called_once_with()
            popen.assert_not_called()

    def test_panel_stop_posts_status_without_starting_control_thread(self):
        with isolated_app() as (app, base_dir):
            from fls_manager.routes.about.panel_control import delayed_stop_panel

            script = base_dir / "fls.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            thread = MagicMock()

            with patch(
                "fls_manager.routes.about.panel_control.threading.Thread",
                return_value=thread,
            ) as thread_factory:
                with patch(
                    "fls_manager.routes.about.helpers.subprocess.Popen"
                ) as popen:
                    response = app.test_client().post(
                        "/about/stop-panel",
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertIn("正在停止面板", response.get_data(as_text=True))
            thread_factory.assert_called_once_with(
                target=delayed_stop_panel,
                daemon=True,
                name="fls-panel-stop",
            )
            thread.start.assert_called_once_with()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
