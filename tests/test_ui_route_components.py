import contextlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
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
            self.assertIn('<div class="card-title">新建文件 / 文件夹</div>', html)
            self.assertIn("当前目录：scripts 根目录", html)
            self.assertIn('style="color:#6b7280;"', html)
            self.assertIn("暂无操作", html)

    def test_scripts_page_renders_command_example_code_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/pull",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">脚本管理</div>', html)
            self.assertIn('<div class="card-title">文件列表</div>', html)
            self.assertIn('<div class="card-title">任务命令示例</div>', html)
            self.assertIn('<div class="code">', html)
            self.assertIn("task 1.py<br>", html)
            self.assertIn("task folder/main.py<br>", html)
            self.assertIn("task /root/fls/scripts/demo.sh arg1 arg2", html)

    def test_scripts_view_renders_header_card_and_escapes_content(self):
        with isolated_app() as (app, base_dir):
            script_rel = 'dir-<x>/demo-<x>.py'
            script_path = base_dir / "scripts" / script_rel
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("print('<x>')\n", encoding="utf-8")

            response = app.test_client().get(
                "/scripts/view",
                query_string={"path": script_rel},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn(
                '<div class="card-title">查看 / 编辑文件：demo-&lt;x&gt;.py</div>',
                html,
            )
            self.assertIn("路径：", html)
            self.assertIn("dir-&lt;x&gt;/demo-&lt;x&gt;.py", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn("保存文件", html)
            self.assertIn("调试运行", html)
            self.assertIn("改名", html)
            self.assertIn('class="fls-code-editor"', html)
            self.assertIn("print(&#x27;&lt;x&gt;&#x27;)", html)
            self.assertIn("暂无保存操作", html)
            self.assertNotIn("<x>", html)

    def test_scripts_rename_renders_header_card_and_escapes_path(self):
        with isolated_app() as (app, base_dir):
            script_rel = 'rename-<x>.sh'
            script_path = base_dir / "scripts" / script_rel
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text("echo ok\n", encoding="utf-8")

            response = app.test_client().get(
                "/scripts/rename",
                query_string={"path": script_rel},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">改名</div>', html)
            self.assertIn("当前路径：", html)
            self.assertIn("rename-&lt;x&gt;.sh", html)
            self.assertIn('name="new_name" required value="rename-&lt;x&gt;.sh"', html)
            self.assertIn("保存改名", html)
            self.assertIn('href="/pull"', html)
            self.assertIn("暂无操作", html)
            self.assertNotIn("<x>", html)

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

    def test_pull_fetch_get_renders_header_card_and_form_shell(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/pull/fetch",
                query_string={"p": "dir-<x>"},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">拉取脚本 / 仓库</div>', html)
            self.assertIn("当前目录：dir-&lt;x&gt;", html)
            self.assertIn('name="current_rel" value="dir-&lt;x&gt;"', html)
            self.assertIn('name="pull_type"', html)
            self.assertIn('name="url"', html)
            self.assertIn('name="proxy_id"', html)
            self.assertIn("开始拉取", html)
            self.assertIn("返回脚本管理", html)
            self.assertIn("暂无操作", html)
            self.assertNotIn("<x>", html)

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

    def test_pull_import_get_renders_header_card_and_form_shell(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/pull/import",
                query_string={"p": "dir-<x>"},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">导入脚本 / 压缩包</div>', html)
            self.assertIn("当前目录：dir-&lt;x&gt;", html)
            self.assertIn('enctype="multipart/form-data"', html)
            self.assertIn('name="current_rel" value="dir-&lt;x&gt;"', html)
            self.assertIn('type="file" name="file"', html)
            self.assertIn('name="save_as"', html)
            self.assertIn("开始导入", html)
            self.assertIn("返回脚本管理", html)
            self.assertIn("暂无操作", html)
            self.assertNotIn("<x>", html)

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

    def test_deps_refresh_renders_header_card_and_table_card(self):
        with isolated_app() as (app, _base_dir):
            with patch(
                "fls_manager.routes.deps.refresh_dependency_cache",
                return_value={
                    "time": '<time & "x">',
                    "packages": {
                        '<pkg & "x">': '1<2',
                        "bad": '不可用：<bad & "x">',
                    },
                },
            ):
                response = app.test_client().get(
                    "/deps/refresh",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">刷新依赖完成</div>', html)
            self.assertIn("刷新时间：&lt;time &amp; &quot;x&quot;&gt;", html)
            self.assertIn('<div class="card-title">核心依赖检测</div>', html)
            self.assertIn("<th>依赖</th>", html)
            self.assertIn("&lt;pkg &amp; &quot;x&quot;&gt;", html)
            self.assertIn("1&lt;2", html)
            self.assertIn('<span class="badge red">异常</span>', html)
            self.assertIn("不可用：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertIn('href="/deps"', html)
            self.assertNotIn("<time", html)
            self.assertNotIn("<bad", html)

    def test_deps_uninstall_renders_header_card_and_escapes_output(self):
        with isolated_app() as (app, _base_dir):
            with patch(
                "fls_manager.routes.deps.pip_cmd",
                return_value=SimpleNamespace(stdout='removed <pkg & "x">\n'),
            ) as pip_mock:
                response = app.test_client().get(
                    "/deps/uninstall",
                    query_string={"name": '<pkg & "x">'},
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            pip_mock.assert_called_once_with(["uninstall", "-y", '<pkg & "x">'])
            self.assertIn('<div class="card-title">卸载结果</div>', html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/deps"', html)
            self.assertIn('<pre class="log">removed &lt;pkg &amp; &quot;x&quot;&gt;\n</pre>', html)
            self.assertNotIn('<pkg & "x">', html)

    def test_env_import_renders_header_card_and_table_card(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "tasks.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "task-env",
                            "name": '<Task & "x">',
                            "command": "task demo.py",
                            "env": {
                                '<ENV & "x">': 'value <x>',
                                "NEW_ENV": "new",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (data_dir / "global_env.json").write_text(
                json.dumps({'<ENV & "x">': "old"}),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/env/import",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">从任务变量导入到全局变量</div>', html)
            self.assertIn("允许覆盖已有全局变量", html)
            self.assertIn('<div class="card-title">可导入变量</div>', html)
            self.assertIn("<th>选择</th>", html)
            self.assertIn("&lt;Task &amp; &quot;x&quot;&gt;", html)
            self.assertIn("&lt;ENV &amp; &quot;x&quot;&gt;", html)
            self.assertIn("value &lt;x&gt;", html)
            self.assertIn('<span class="badge orange">将覆盖</span>', html)
            self.assertIn('<span class="badge green">新增</span>', html)
            self.assertIn('name="overwrite" value="1"', html)
            self.assertIn("导入所选变量", html)
            self.assertIn('href="/env"', html)
            self.assertNotIn("<Task", html)
            self.assertNotIn("<ENV", html)

    def test_env_view_all_renders_header_card_and_escapes_textarea(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "global_env.json").write_text(
                json.dumps({'<ENV & "x">': "value <x>"}),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/env/view",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">查看全部全局变量</div>', html)
            self.assertIn("保存后会整体覆盖", html)
            self.assertIn('name="env_text"', html)
            self.assertIn(
                "&lt;ENV &amp; &quot;x&quot;&gt;=&quot;value &lt;x&gt;&quot;",
                html,
            )
            self.assertIn("保存全部", html)
            self.assertIn('href="/env"', html)
            self.assertNotIn("<ENV", html)

    def test_env_new_renders_header_card_and_keeps_validation_text(self):
        with isolated_app() as (app, _base_dir):
            client = app.test_client()
            response = client.get(
                "/env/new",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">新增全局变量</div>', html)
            self.assertIn("全局变量会对所有任务生效", html)
            self.assertIn('name="key"', html)
            self.assertIn('name="value"', html)
            self.assertIn("保存", html)
            self.assertIn('href="/env"', html)

            invalid = client.post(
                "/env/new",
                data={"key": "", "value": "x"},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(invalid.get_data(as_text=True), "变量名不能为空")

    def test_env_edit_renders_header_card_and_escapes_values(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            key = '<ENV & "x">'
            (data_dir / "global_env.json").write_text(
                json.dumps({key: "value <x>"}),
                encoding="utf-8",
            )

            response = app.test_client().get(
                f"/env/edit/{quote(key, safe='')}",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">编辑全局变量</div>', html)
            self.assertIn("修改变量名会先移除原变量", html)
            self.assertIn('value="&lt;ENV &amp; &quot;x&quot;&gt;"', html)
            self.assertIn('value="value &lt;x&gt;"', html)
            self.assertIn("保存", html)
            self.assertIn('href="/env"', html)
            self.assertNotIn("<ENV", html)
            self.assertNotIn("<x>", html)

    def test_proxy_new_renders_header_card_and_realtime_shell(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/proxy/new",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">新增代理</div>', html)
            self.assertIn("代理可用于任务运行、脚本拉取和 GitHub 加速", html)
            self.assertIn('action="/proxy/new"', html)
            self.assertIn('name="name"', html)
            self.assertIn('id="proxyType"', html)
            self.assertIn('name="quality_urls"', html)
            self.assertIn("保存代理", html)
            self.assertIn("testProxyRealtime", html)
            self.assertIn("qualityProxyRealtime", html)
            self.assertIn('id="proxyRealtimeResult"', html)
            self.assertIn('href="/proxy"', html)

    def test_proxy_edit_renders_header_card_and_escapes_values(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "proxies.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "proxy-x",
                            "name": '<Proxy & "x">',
                            "type": "http",
                            "host": "proxy<host>",
                            "port": "8080",
                            "username": 'user & "x"',
                            "password": "pass <x>",
                            "url": "https://gh.example/<x>",
                            "enabled": False,
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/proxy/edit/proxy-x",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">编辑代理</div>', html)
            self.assertIn("当前代理：<b>&lt;Proxy &amp; &quot;x&quot;&gt;</b>", html)
            self.assertIn('action="/proxy/edit/proxy-x"', html)
            self.assertIn('name="id" value="proxy-x"', html)
            self.assertIn('name="name" value="&lt;Proxy &amp; &quot;x&quot;&gt;"', html)
            self.assertIn('value="http" selected', html)
            self.assertIn('name="host" value="proxy&lt;host&gt;"', html)
            self.assertIn('name="username" value="user &amp; &quot;x&quot;"', html)
            self.assertIn('name="password" value="pass &lt;x&gt;"', html)
            self.assertIn('name="url" value="https://gh.example/&lt;x&gt;"', html)
            self.assertIn('id="proxyRealtimeResult"', html)
            self.assertNotIn("<Proxy", html)
            self.assertNotIn("<host>", html)
            self.assertNotIn("<x>", html)

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

    def test_config_page_renders_task_type_table_card(self):
        with isolated_app() as (app, base_dir):
            config_file = base_dir / "data" / "config.json"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(
                json.dumps(
                    {
                        "admin_token": TOKEN,
                        "task_types": {
                            "py": True,
                            "sh": False,
                            "js": False,
                        },
                    }
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/config",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">task 可执行脚本类型</div>', html)
            self.assertIn("<th>类型</th>", html)
            self.assertIn("<th>启用</th>", html)
            self.assertIn("<td><b>Python .py / .pyw</b></td>", html)
            self.assertIn('name="type_py" value="1" checked', html)
            self.assertIn('name="type_sh" value="1"', html)
            self.assertNotIn('name="type_sh" value="1" checked', html)
            self.assertIn("保存配置", html)
            self.assertIn("flsToggleSecurityBox", html)

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
            self.assertIn('<div class="card-title">任务命令规则</div>', html)
            self.assertIn("使用 <b>task</b> 开头时", html)
            self.assertIn("task demo.ts<br>", html)
            self.assertIn("python3 /root/test.py", html)
            self.assertIn('<div class="card-title">Cron 说明</div>', html)
            self.assertIn("*/10 * * * *  每 10 分钟<br>", html)
            self.assertIn('<div class="card-title">进程查看示例</div>', html)
            self.assertIn("ps -eo pid,ppid,comm,args | grep fls", html)

    def test_about_refresh_log_no_git_renders_header_card(self):
        with isolated_app() as (app, _base_dir):
            with patch(
                "fls_manager.routes.about.version.git_available",
                return_value=False,
            ):
                response = app.test_client().post(
                    "/about/refresh-log",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">刷新失败</div>', html)
            self.assertIn("系统未安装 git。", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/about"', html)

    def test_about_refresh_log_not_repo_renders_header_card(self):
        with isolated_app() as (app, _base_dir):
            with patch(
                "fls_manager.routes.about.version.git_available",
                return_value=True,
            ), patch(
                "fls_manager.routes.about.version.is_git_repo",
                return_value=False,
            ):
                response = app.test_client().post(
                    "/about/refresh-log",
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">刷新失败</div>', html)
            self.assertIn("当前目录不是 Git 仓库", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/about"', html)

    def test_about_update_invalid_version_renders_header_card_and_escapes_value(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().post(
                "/about/update-version",
                data={"version": '<bad & "x">'},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">更新失败</div>', html)
            self.assertIn("版本号非法：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/about"', html)
            self.assertNotIn("<bad", html)

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

    def test_online_source_json_renders_header_card_and_escapes_cache(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                '[{"id":"demo","name":"<Demo & x>","type":"raw"}]',
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/online-scripts/source",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">脚本源 JSON</div>', html)
            self.assertIn("这里显示当前本地缓存的脚本源 JSON", html)
            self.assertIn("task_cron.var", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/online-scripts"', html)
            self.assertIn('<div class="card-title">查看 / 修改缓存 JSON</div>', html)
            self.assertIn('name="json_text"', html)
            self.assertIn("&lt;Demo &amp; x&gt;", html)
            self.assertIn("保存脚本源 JSON", html)
            self.assertNotIn("<Demo", html)

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

    def test_online_install_select_renders_header_card_and_keeps_task_shell(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "select-demo",
                            "name": '<Install & "x">',
                            "type": "raw",
                            "link": "https://example.invalid/select.py",
                            "link_name": 'select-<x>.py',
                            "task_cron": [
                                {
                                    "name": "任务一",
                                    "cron": "0 1 * * *",
                                    "command": "task select.py",
                                    "remark": "备注",
                                }
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/online-scripts/install-select/select-demo",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn(
                "选择任务并安装：&lt;Install &amp; &quot;x&quot;&gt;",
                html,
            )
            self.assertIn("脚本 ID：select-demo", html)
            self.assertIn("保存名：select-&lt;x&gt;.py", html)
            self.assertIn('<b id="selectedTaskCount">1</b>', html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/online-scripts"', html)
            self.assertIn('id="onlineInstallSelectForm"', html)
            self.assertIn('name="select_mode" value="all"', html)
            self.assertIn("选择要导入的任务", html)
            self.assertIn("flsInstallGoTaskPage", html)
            self.assertNotIn("<Install", html)

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

    def test_script_debug_log_missing_renders_header_card(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/scripts/debug-log/missing-debug?back=/pull",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">脚本调试日志</div>', html)
            self.assertIn("调试记录不存在或面板已重启", html)
            self.assertIn("script-debug-*.log", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/pull"', html)
            self.assertIn('href="/logs?back=/pull"', html)

    def test_script_debug_log_existing_renders_header_card_and_log_shell(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.routes.scripts.debug import SCRIPT_DEBUG_RUNNING

            SCRIPT_DEBUG_RUNNING["debug-x"] = {
                "id": "debug-x",
                "script": '/tmp/script-<x>.py',
                "rel": 'script-<x>.py',
                "log_file": 'script-debug-<x>.log',
                "running": True,
                "process": None,
                "pid": '<123>',
                "returncode": None,
                "error": "",
            }

            response = app.test_client().get(
                "/scripts/debug-log/debug-x?back=/pull",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">脚本调试日志</div>', html)
            self.assertIn('<b id="debugStatus">运行中</b>', html)
            self.assertIn("PID：&lt;123&gt;", html)
            self.assertIn("/tmp/script-&lt;x&gt;.py", html)
            self.assertIn("script-debug-&lt;x&gt;.log", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/scripts/debug-stop/debug-x?back=/pull"', html)
            self.assertIn('href="/pull"', html)
            self.assertIn('<pre class="log" id="log">加载中...</pre>', html)
            self.assertIn('id="flsLogNewTip"', html)
            self.assertIn("loadScriptDebugLog", html)
            self.assertNotIn("<x>", html)

    def test_deps_install_log_missing_renders_header_card_and_log_shell(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().get(
                "/deps/install-log/missing-install?back=/deps",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">安装日志：未知</div>', html)
            self.assertIn('<b id="installStatus">已结束</b>', html)
            self.assertIn("当前进程已结束，无法定位日志", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/deps"', html)
            self.assertIn('href="/deps/refresh"', html)
            self.assertIn('<pre class="log" id="log">加载中...</pre>', html)
            self.assertIn("loadLog", html)

    def test_deps_install_log_existing_renders_header_card_and_log_shell(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.state import DEPS_RUNNING

            class FakeProc:
                def poll(self):
                    return None

            DEPS_RUNNING["deps-x"] = {
                "process": FakeProc(),
                "package": '<Pkg & "x">',
                "log_file": 'deps-install-<x>.log',
                "log_fp": None,
                "finished": False,
                "returncode": None,
            }

            response = app.test_client().get(
                "/deps/install-log/deps-x?back=/deps",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("安装日志：&lt;Pkg &amp; &quot;x&quot;&gt;", html)
            self.assertIn('<b id="installStatus">安装中</b>', html)
            self.assertIn("deps-install-&lt;x&gt;.log", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/deps"', html)
            self.assertIn('href="/deps/refresh"', html)
            self.assertIn('<pre class="log" id="log">加载中...</pre>', html)
            self.assertIn("loadLog", html)
            self.assertNotIn("<Pkg", html)
            self.assertNotIn("<x>", html)

    def test_backup_import_success_renders_header_card(self):
        with isolated_app() as (app, base_dir):
            archive = io.BytesIO()

            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                payload = b'{"restored": true}\n'
                info = tarfile.TarInfo("data/config.json")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

            archive.seek(0)

            with patch(
                "fls_manager.routes.backup.restore.reload_scheduler",
            ) as reload_mock:
                response = app.test_client().post(
                    "/backup/import",
                    data={
                        "file": (archive, "backup.tar.gz"),
                        "restore_items": "data",
                    },
                    headers={"X-Token": TOKEN},
                    content_type="multipart/form-data",
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            reload_mock.assert_called_once_with()
            self.assertIn('<div class="card-title">备份导入完成</div>', html)
            self.assertIn("已恢复：配置 data", html)
            self.assertIn("依赖恢复：未恢复依赖", html)
            self.assertIn("日志：-", html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('href="/backup"', html)
            self.assertIn('href="/logs"', html)
            self.assertEqual(
                (base_dir / "data" / "config.json").read_text(encoding="utf-8"),
                '{"restored": true}\n',
            )


if __name__ == "__main__":
    unittest.main()
