import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "route-component-token"


@contextlib.contextmanager
def isolated_app():
    keys = ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-route-component-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["FLS_TOKEN"] = TOKEN
        os.environ["FLS_SECRET_KEY"] = "route-component-secret"
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        purge_fls_modules()

        try:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))

            from fls_manager.app import create_app

            yield create_app(), Path(temp_dir)
        finally:
            shutdown_scheduler()
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


def shutdown_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None

    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


class UiRouteComponentTests(unittest.TestCase):
    def test_scripts_new_renders_info_message_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/pull/new",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#6b7280;"', html)
            self.assertIn("暂无操作", html)

    def test_pull_fetch_renders_error_message_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().post(
                "/pull/fetch",
                data={"url": ""},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">结果</div>', html)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("URL 不能为空", html)

    def test_pull_fetch_success_renders_success_message_card(self):
        with isolated_app() as (app, _base_dir):
            with patch(
                "fls_manager.routes.scripts.pull.fetch_file_bytes",
                return_value=b"print('ok')\n",
            ):
                response = app.test_client().post(
                    "/pull/fetch",
                    data={
                        "url": "https://example.invalid/demo.py",
                        "filename": "demo.py",
                        "pull_type": "file",
                    },
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">结果</div>', html)
            self.assertIn('style="color:#18a058;font-weight:800;"', html)
            self.assertIn("文件拉取成功", html)

    def test_pull_fetch_failure_escapes_message_card(self):
        with isolated_app() as (app, _base_dir):
            with patch(
                "fls_manager.routes.scripts.pull.fetch_file_bytes",
                side_effect=RuntimeError('<bad & "x">'),
            ):
                response = app.test_client().post(
                    "/pull/fetch",
                    data={
                        "url": "https://example.invalid/demo.py",
                        "filename": "demo.py",
                        "pull_type": "file",
                    },
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("拉取失败：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertNotIn("<bad", html)

    def test_pull_import_renders_error_message_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().post(
                "/pull/import",
                data={},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">结果</div>', html)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("请选择要导入的文件", html)

    def test_pull_import_success_renders_success_message_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().post(
                "/pull/import",
                data={"file": (io.BytesIO(b"print('ok')\n"), "demo.py")},
                headers={"X-Token": TOKEN},
                content_type="multipart/form-data",
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">结果</div>', html)
            self.assertIn('style="color:#18a058;font-weight:800;"', html)
            self.assertIn("文件导入成功", html)

    def test_task_config_save_success_renders_message_card(self):
        with isolated_app() as (app, base_dir):
            tasks_file = base_dir / "data" / "tasks.json"
            tasks_file.parent.mkdir(parents=True, exist_ok=True)
            tasks_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "task-config",
                            "name": "配置任务",
                            "command": "task demo.py",
                            "config_path": "conf/app.yml",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().post(
                "/task/config/task-config",
                data={"content": "key: value\n"},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#18a058;font-weight:800;"', html)
            self.assertIn("保存成功", html)
            self.assertEqual(
                (base_dir / "scripts" / "conf" / "app.yml").read_text(
                    encoding="utf-8"
                ),
                "key: value\n",
            )

    def test_task_config_save_failure_escapes_message_card(self):
        with isolated_app() as (app, base_dir):
            tasks_file = base_dir / "data" / "tasks.json"
            tasks_file.parent.mkdir(parents=True, exist_ok=True)
            tasks_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "task-config",
                            "name": "配置任务",
                            "command": "task demo.py",
                            "config_path": "conf/app.yml",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            real_write_text = Path.write_text

            def fail_config_write(path, *args, **kwargs):
                if path.name == "app.yml":
                    raise RuntimeError('<bad & "x">')

                return real_write_text(path, *args, **kwargs)

            with patch("pathlib.Path.write_text", new=fail_config_write):
                response = app.test_client().post(
                    "/task/config/task-config",
                    data={"content": "key: value\n"},
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("保存失败：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertNotIn("<bad", html)

    def test_deps_page_renders_table_card_and_escapes_rows(self):
        with isolated_app() as (app, _base_dir):
            packages = [
                {
                    "name": '<pkg & "x">',
                    "version": "1<2",
                }
            ]

            with patch(
                "fls_manager.routes.deps.pip_cmd",
                return_value=SimpleNamespace(stdout=json.dumps(packages)),
            ):
                response = app.test_client().get(
                    "/deps",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">已安装依赖</div>', html)
            self.assertIn("<th>包名</th>", html)
            self.assertIn("&lt;pkg &amp; &quot;x&quot;&gt;", html)
            self.assertIn("1&lt;2", html)
            self.assertNotIn('<pkg & "x">', html)

    def test_status_page_renders_table_card_with_runtime_table_id(self):
        with isolated_app() as (app, _base_dir):
            runtime_items = [
                {
                    "name": "<Python>",
                    "suffix": ".py <x>",
                    "command": "python",
                    "version": "3.13",
                    "install_url": "/install/runtime/python",
                }
            ]

            with patch(
                "fls_manager.routes.status.runtime_items",
                return_value=runtime_items,
            ):
                response = app.test_client().get(
                    "/panel/status",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">运行环境</div>', html)
            self.assertIn('<table id="runtimeTable">', html)
            self.assertIn("<th>脚本类型</th>", html)
            self.assertIn("&lt;Python&gt;", html)
            self.assertIn(".py &lt;x&gt;", html)
            self.assertNotIn("<Python>", html)

    def test_dashboard_renders_environment_table_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">环境状态</div>', html)
            self.assertIn("<th>项目</th>", html)
            self.assertIn("<th>值</th>", html)
            self.assertIn("当前峰值统计周期", html)
            self.assertIn("<td><b>面板时区</b></td>", html)
            self.assertIn("<td><b>工作目录</b></td>", html)

    def test_about_page_renders_panel_info_table_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/about",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">面板信息</div>', html)
            self.assertIn("<th>项目</th>", html)
            self.assertIn("<th>值</th>", html)
            self.assertIn("<td><b>项目仓库</b></td>", html)
            self.assertIn("https://github.com/liyw0205/fls", html)
            self.assertIn("<td><b>控制脚本</b></td>", html)

    def test_about_job_missing_renders_header_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/about/job-log/missing-job",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">后台任务日志</div>', html)
            self.assertIn("任务记录不存在或面板已重启", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/about"', html)
            self.assertIn('href="/logs?back=/about"', html)

    def test_about_job_existing_renders_header_card_and_log_shell(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.about.state import ABOUT_JOBS

            ABOUT_JOBS["job-x"] = {
                "title": '<更新 & "x">',
                "status": "running <ok>",
                "log_file": 'about-<x>.log',
                "updated_at": "2026-07-05 01:02:03",
                "running": True,
            }

            response = app.test_client().get(
                "/about/job-log/job-x?back=/about",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("后台任务日志：&lt;更新 &amp; &quot;x&quot;&gt;", html)
            self.assertIn("running &lt;ok&gt;", html)
            self.assertIn("about-&lt;x&gt;.log", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/logs?back=/about"', html)
            self.assertIn('href="/logfile/fls-manager-daemon.log?back=/about"', html)
            self.assertIn('<pre class="log" id="log">加载中...</pre>', html)
            self.assertIn("loadAboutJobLog", html)
            self.assertNotIn("<更新", html)

    def test_restart_panel_missing_script_renders_header_card(self):
        with isolated_app() as (app, base_dir):
            missing_script = base_dir / 'missing-<restart>.sh'

            with patch(
                "fls_manager.routes.about.panel_control.fls_control_script",
                return_value=missing_script,
            ):
                response = app.test_client().post(
                    "/about/restart-panel",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 400)
            self.assertIn('<div class="card-title">重启失败</div>', html)
            self.assertIn("未找到 FLS 控制脚本", html)
            self.assertIn("missing-&lt;restart&gt;.sh", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/about"', html)
            self.assertNotIn("<restart>", html)

    def test_restart_panel_success_renders_header_card_without_running_thread(self):
        with isolated_app() as (app, base_dir):
            script = base_dir / "fls.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            created_threads = []

            class FakeThread:
                def __init__(self, *args, **kwargs):
                    self.args = args
                    self.kwargs = kwargs
                    self.started = False
                    created_threads.append(self)

                def start(self):
                    self.started = True

            with patch(
                "fls_manager.routes.about.panel_control.fls_control_script",
                return_value=script,
            ), patch(
                "fls_manager.routes.about.panel_control.threading.Thread",
                new=FakeThread,
            ):
                response = app.test_client().post(
                    "/about/restart-panel",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(created_threads), 1)
            self.assertTrue(created_threads[0].started)
            self.assertEqual(created_threads[0].kwargs.get("name"), "fls-panel-restart")
            self.assertIn('<div class="card-title">正在重启面板</div>', html)
            self.assertIn("控制脚本", html)
            self.assertIn('href="/logfile/fls-manager-daemon.log?back=/about"', html)
            self.assertIn("setTimeout(function(){", html)

    def test_stop_panel_missing_script_renders_header_card(self):
        with isolated_app() as (app, base_dir):
            missing_script = base_dir / 'missing-<stop>.sh'

            with patch(
                "fls_manager.routes.about.panel_control.fls_control_script",
                return_value=missing_script,
            ):
                response = app.test_client().post(
                    "/about/stop-panel",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 400)
            self.assertIn('<div class="card-title">停止失败</div>', html)
            self.assertIn("未找到 FLS 控制脚本", html)
            self.assertIn("missing-&lt;stop&gt;.sh", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/about"', html)
            self.assertNotIn("<stop>", html)

    def test_stop_panel_success_renders_header_card_without_running_thread(self):
        with isolated_app() as (app, base_dir):
            script = base_dir / "fls.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            created_threads = []

            class FakeThread:
                def __init__(self, *args, **kwargs):
                    self.args = args
                    self.kwargs = kwargs
                    self.started = False
                    created_threads.append(self)

                def start(self):
                    self.started = True

            with patch(
                "fls_manager.routes.about.panel_control.fls_control_script",
                return_value=script,
            ), patch(
                "fls_manager.routes.about.panel_control.threading.Thread",
                new=FakeThread,
            ):
                response = app.test_client().post(
                    "/about/stop-panel",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(created_threads), 1)
            self.assertTrue(created_threads[0].started)
            self.assertEqual(created_threads[0].kwargs.get("name"), "fls-panel-stop")
            self.assertIn('<div class="card-title">正在停止面板</div>', html)
            self.assertIn("停止后需要你手动重新启动面板", html)
            self.assertIn('href="/logfile/fls-manager-daemon.log?back=/about"', html)

    def test_notify_test_renders_result_table_card_and_escapes_return(self):
        with isolated_app() as (app, base_dir):
            config_file = base_dir / "data" / "config.json"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(
                json.dumps(
                    {
                        "notify_items": [
                            {
                                "id": "notify-test",
                                "name": '<通知 & "x">',
                                "channel": "webhook",
                                "enabled": True,
                                "config": {},
                            }
                        ],
                        "notify_default_ids": [],
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "fls_manager.routes.notify.test.send_one",
                return_value=(False, '<bad & "x">'),
            ):
                response = app.test_client().get(
                    "/notify/test/notify-test",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">通知测试结果</div>', html)
            self.assertIn("<th>项目</th>", html)
            self.assertIn("<th>值</th>", html)
            self.assertIn("&lt;通知 &amp; &quot;x&quot;&gt;", html)
            self.assertIn("自定义 Webhook", html)
            self.assertIn('<span class="badge red">失败</span>', html)
            self.assertIn("&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertIn('href="/notify"', html)
            self.assertNotIn('<bad & "x">', html)

    def test_online_script_doc_error_renders_escaped_message_card(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "demo",
                            "name": "Demo",
                            "type": "raw",
                            "link": "https://example.invalid/demo.py",
                            "doc_link": "https://example.invalid/<doc>.md",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with patch(
                "fls_manager.routes.online_scripts.docs.requests.get",
                side_effect=RuntimeError('<bad & "x">'),
            ):
                response = app.test_client().get(
                    "/online-scripts/doc/demo?mode=render",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("文档加载失败：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertNotIn("<bad", html)

    def test_online_script_doc_without_link_renders_header_card(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "no-doc",
                            "name": "No Doc",
                            "type": "raw",
                            "link": "https://example.invalid/no-doc.py",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/online-scripts/doc/no-doc",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">脚本文档</div>', html)
            self.assertIn("该脚本未提供 doc_link。", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/online-scripts"', html)

    def test_online_install_invalid_target_renders_header_card(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "bad-target",
                            "name": "Bad Target",
                            "type": "raw",
                            "link": "https://example.invalid/demo.py",
                            "link_name": "../bad.py",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().post(
                "/online-scripts/install/bad-target",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 400)
            self.assertIn('<div class="card-title">目标路径非法</div>', html)
            self.assertIn("目标路径非法", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/online-scripts"', html)

    def test_online_install_existing_target_renders_header_card(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "exists-demo",
                            "name": "Exists Demo",
                            "type": "raw",
                            "link": "https://example.invalid/demo.py",
                            "link_name": 'exists-<x>.py',
                        }
                    ]
                ),
                encoding="utf-8",
            )
            target = base_dir / "scripts" / 'exists-<x>.py'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("print('old')\n", encoding="utf-8")

            response = app.test_client().post(
                "/online-scripts/install/exists-demo",
                data={"proxy_id": "", "import_task": "0"},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">目标已存在，请确认</div>', html)
            self.assertIn("检测到同名文件或文件夹已经存在", html)
            self.assertIn("exists-&lt;x&gt;.py", html)
            self.assertIn('<form method="post" action="/online-scripts/install/exists-demo">', html)
            self.assertIn('name="force" value="1"', html)
            self.assertIn("确认继续", html)
            self.assertIn('href="/online-scripts"', html)
            self.assertNotIn("<x>", html)

    def test_online_install_log_missing_renders_header_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/online-scripts/log/missing-install",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">在线脚本日志</div>', html)
            self.assertIn("安装记录不存在或面板已重启", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/online-scripts"', html)
            self.assertIn('href="/logs?back=/online-scripts"', html)

    def test_online_install_log_existing_renders_header_card_and_log_shell(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            ONLINE_INSTALL_RUNNING["install-x"] = {
                "id": "install-x",
                "script_id": "demo",
                "script_name": '<脚本 & "x">',
                "log_file": 'online-install-<x>.log',
                "running": True,
                "status": "running <ok>",
                "returncode": None,
                "error": "",
            }

            response = app.test_client().get(
                "/online-scripts/log/install-x?back=/online-scripts",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("在线脚本下载安装日志：&lt;脚本 &amp; &quot;x&quot;&gt;", html)
            self.assertIn("running &lt;ok&gt;", html)
            self.assertIn("online-install-&lt;x&gt;.log", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/online-scripts"', html)
            self.assertIn('href="/pull"', html)
            self.assertIn('href="/tasks"', html)
            self.assertIn('action="/online-scripts/install-stop/install-x"', html)
            self.assertIn('<pre class="log" id="log">加载中...</pre>', html)
            self.assertIn("loadLog", html)
            self.assertNotIn("<脚本", html)


if __name__ == "__main__":
    unittest.main()
