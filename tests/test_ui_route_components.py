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
    def test_static_js_formats_structured_bulk_action_messages(self):
        js = (ROOT / "fls_manager" / "static" / "fls.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("function flsBulkActionMessage", js)
        self.assertIn("submitted_count", js)
        self.assertIn("stopped_count", js)
        self.assertIn("skipped_count", js)
        self.assertIn("deleted_count", js)
        self.assertIn("failures", js)
        self.assertIn("删除失败", js)

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

    def test_scripts_new_failure_renders_error_message_and_preserves_form(self):
        with isolated_app() as (app, base_dir):
            with patch(
                "fls_manager.routes.scripts.files.script_safe_path",
                side_effect=RuntimeError('<bad & "x">'),
            ):
                response = app.test_client().post(
                    "/pull/new",
                    data={
                        "item_type": "file",
                        "name": "new <file>.py",
                        "content": 'print("<x>")\n',
                    },
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("新建失败：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertIn('value="new &lt;file&gt;.py"', html)
            self.assertIn("print(&quot;&lt;x&gt;&quot;)", html)
            self.assertNotIn("<bad", html)
            self.assertNotIn("new <file>.py", html)
            self.assertFalse((base_dir / "scripts" / "new <file>.py").exists())

    def test_scripts_view_save_failure_preserves_posted_content(self):
        with isolated_app() as (app, base_dir):
            script_file = base_dir / "scripts" / "demo.py"
            script_file.write_text('print("old")\n', encoding="utf-8")

            real_write_text = Path.write_text

            def fail_script_write(path, *args, **kwargs):
                if path.name == "demo.py":
                    raise RuntimeError('<bad & "x">')

                return real_write_text(path, *args, **kwargs)

            with patch("pathlib.Path.write_text", new=fail_script_write):
                response = app.test_client().post(
                    "/scripts/view?path=demo.py",
                    data={"content": 'print("updated <x>")\n'},
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("保存失败：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertIn("print(&quot;updated &lt;x&gt;&quot;)", html)
            self.assertNotIn("<bad", html)
            self.assertEqual(script_file.read_text(encoding="utf-8"), 'print("old")\n')

    def test_scripts_rename_failure_renders_error_message_and_preserves_name(self):
        with isolated_app() as (app, base_dir):
            script_file = base_dir / "scripts" / "demo.py"
            script_file.write_text('print("old")\n', encoding="utf-8")
            new_name = '<renamed & "x">.py'

            real_rename = Path.rename

            def fail_rename(path, *args, **kwargs):
                if path.name == "demo.py":
                    raise RuntimeError('<bad & "x">')

                return real_rename(path, *args, **kwargs)

            with patch("pathlib.Path.rename", new=fail_rename):
                response = app.test_client().post(
                    "/scripts/rename?path=demo.py",
                    data={"new_name": new_name},
                    headers={"X-Token": TOKEN},
                )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("改名失败：&lt;bad &amp; &quot;x&quot;&gt;", html)
            self.assertIn('value="&lt;renamed &amp; &quot;x&quot;&gt;.py"', html)
            self.assertNotIn("<bad", html)
            self.assertNotIn(new_name, html)
            self.assertEqual(script_file.read_text(encoding="utf-8"), 'print("old")\n')
            self.assertFalse((base_dir / "scripts" / new_name).exists())

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
            self.assertIn(
                'formaction="/run/task-config?back=/tasks"',
                html,
            )
            self.assertIn('formmethod="post"', html)
            self.assertNotIn('href="/run/task-config', html)

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

    def test_task_config_missing_task_returns_404_without_write(self):
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
                "/task/config/missing",
                data={"content": "key: value\n"},
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 404)
            self.assertFalse((base_dir / "scripts" / "conf" / "app.yml").exists())
            self.assertEqual(
                json.loads(tasks_file.read_text(encoding="utf-8"))[0]["id"],
                "task-config",
            )

    def test_task_config_without_config_path_renders_edit_prompt(self):
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
                            "config_path": "",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/task/config/task-config?back=https://example.invalid/out",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">任务配置文件</div>', html)
            self.assertIn("该任务没有配置 config_path。", html)
            self.assertIn('href="/tasks"', html)
            self.assertIn('href="/task/edit/task-config?back=/tasks"', html)
            self.assertEqual(list((base_dir / "scripts").iterdir()), [])

    def test_task_config_illegal_path_renders_error_without_write(self):
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
                            "config_path": "../outside.yml",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().post(
                "/task/config/task-config?back=/collections",
                data={"content": "key: value\n"},
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 400)
            self.assertIn('<div class="card-title">配置文件路径非法</div>', html)
            self.assertIn("配置文件路径非法", html)
            self.assertIn('href="/collections"', html)
            self.assertIn('href="/task/edit/task-config?back=/collections"', html)
            self.assertFalse((base_dir / "outside.yml").exists())

    def test_task_edit_form_preserves_back_url_and_retry_fields(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "collections.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "c1",
                            "name": "合集一",
                            "remark": "",
                            "created_at": "2026-07-04 00:00:00",
                            "updated_at": "2026-07-04 00:00:00",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (data_dir / "tasks.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "task-edit",
                            "name": "编辑任务",
                            "command": "task demo.py",
                            "collection_id": "c1",
                            "retry": {
                                "attempts": 2,
                                "interval_seconds": 90,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/task/edit/task-edit?back=/collections%23collection-c1",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('name="back" value="/collections#collection-c1"', html)
            self.assertIn('href="/collections#collection-c1"', html)
            self.assertIn('name="retry_attempts" type="number" min="0" max="5" value="2"', html)
            self.assertIn('name="retry_interval_seconds" type="number" min="5" max="3600" value="90"', html)
            self.assertNotIn('name="retry_count"', html)

    def test_task_new_validation_error_renders_message_card_and_preserves_form(self):
        with isolated_app() as (app, _base_dir):
            response = app.test_client().post(
                "/task/new",
                data={
                    "name": 'Bad <task "x">',
                    "command": "",
                    "cron": "",
                    "back": "/tasks?q=bad",
                    "enabled": "1",
                },
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 400)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("命令不能为空", html)
            self.assertIn(
                'value="Bad &lt;task &quot;x&quot;&gt;"',
                html,
            )
            self.assertIn('name="back" value="/tasks?q=bad"', html)
            self.assertIn('href="/tasks?q=bad"', html)
            self.assertNotIn('Bad <task "x">', html)

    def test_task_new_cron_validation_error_does_not_save_or_reload(self):
        with isolated_app() as (app, base_dir):
            with patch("fls_manager.routes.tasks.pages.save_tasks") as save_tasks:
                with patch("fls_manager.routes.tasks.pages.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/task/new",
                        data={
                            "name": "定时任务",
                            "command": "task demo.py",
                            "cron": "* *",
                            "back": "/tasks?q=cron",
                            "enabled": "1",
                        },
                        headers={"X-Token": TOKEN},
                    )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 400)
            self.assertIn("Cron 不合法：Cron 格式错误", html)
            self.assertIn('name="back" value="/tasks?q=cron"', html)
            self.assertIn('href="/tasks?q=cron"', html)
            save_tasks.assert_not_called()
            reload_scheduler.assert_not_called()
            self.assertFalse((base_dir / "data" / "tasks.json").exists())

    def test_task_new_missing_collection_does_not_save_or_reload(self):
        with isolated_app() as (app, base_dir):
            with patch("fls_manager.routes.tasks.pages.save_tasks") as save_tasks:
                with patch("fls_manager.routes.tasks.pages.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/task/new",
                        data={
                            "name": "合集任务",
                            "command": "task demo.py",
                            "cron": "",
                            "collection_id": "missing-collection",
                            "back": "/collections",
                            "enabled": "1",
                        },
                        headers={"X-Token": TOKEN},
                    )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 400)
            self.assertIn("合集不存在", html)
            self.assertIn('name="back" value="/collections"', html)
            self.assertIn('href="/collections"', html)
            save_tasks.assert_not_called()
            reload_scheduler.assert_not_called()
            self.assertFalse((base_dir / "data" / "tasks.json").exists())

    def test_task_edit_validation_error_keeps_safe_back_and_does_not_save(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            tasks_file = data_dir / "tasks.json"
            tasks_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "task-edit",
                            "name": "原任务",
                            "command": "task demo.py",
                            "collection_id": "",
                            "retry": {
                                "attempts": 1,
                                "interval_seconds": 30,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().post(
                "/task/edit/task-edit?back=https://example.invalid/evil",
                data={
                    "name": 'Cron <bad>',
                    "command": "task demo.py",
                    "cron": "* *",
                    "retry_attempts": "2",
                    "retry_interval_seconds": "45",
                    "enabled": "1",
                },
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)
            task = json.loads(tasks_file.read_text(encoding="utf-8"))[0]

            self.assertEqual(response.status_code, 400)
            self.assertIn('style="color:#dc2626;font-weight:800;"', html)
            self.assertIn("Cron 不合法：Cron 格式错误", html)
            self.assertIn('name="back" value="/tasks"', html)
            self.assertIn('href="/tasks"', html)
            self.assertIn('value="Cron &lt;bad&gt;"', html)
            self.assertNotIn("Cron <bad>", html)
            self.assertEqual(task["name"], "原任务")
            self.assertEqual(task["retry"], {"attempts": 1, "interval_seconds": 30})

    def test_task_edit_missing_collection_keeps_safe_back_and_does_not_save(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "collections.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "c1",
                            "name": "合集一",
                            "remark": "",
                            "created_at": "2026-07-04 00:00:00",
                            "updated_at": "2026-07-04 00:00:00",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            tasks_file = data_dir / "tasks.json"
            tasks_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "task-edit",
                            "name": "原任务",
                            "command": "task demo.py",
                            "collection_id": "c1",
                            "retry": {
                                "attempts": 1,
                                "interval_seconds": 30,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with patch("fls_manager.routes.tasks.pages.save_tasks") as save_tasks:
                with patch("fls_manager.routes.tasks.pages.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/task/edit/task-edit",
                        data={
                            "back": "https://example.invalid/evil",
                            "name": "改名任务",
                            "command": "task other.py",
                            "cron": "",
                            "collection_id": "missing-collection",
                            "retry_attempts": "3",
                            "retry_interval_seconds": "90",
                            "enabled": "1",
                        },
                        headers={"X-Token": TOKEN},
                    )

            html = response.get_data(as_text=True)
            task = json.loads(tasks_file.read_text(encoding="utf-8"))[0]

            self.assertEqual(response.status_code, 400)
            self.assertIn("合集不存在", html)
            self.assertIn('name="back" value="/collections#collection-c1"', html)
            self.assertIn('href="/collections#collection-c1"', html)
            save_tasks.assert_not_called()
            reload_scheduler.assert_not_called()
            self.assertEqual(task["name"], "原任务")
            self.assertEqual(task["collection_id"], "c1")
            self.assertEqual(task["retry"], {"attempts": 1, "interval_seconds": 30})

    def test_task_edit_missing_task_aborts_without_side_effects(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            tasks_file = data_dir / "tasks.json"
            tasks_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "task-edit",
                            "name": "原任务",
                            "command": "task demo.py",
                            "cron": "",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with patch("fls_manager.routes.tasks.pages.save_tasks") as save_tasks:
                with patch("fls_manager.routes.tasks.pages.reload_scheduler") as reload_scheduler:
                    response = app.test_client().post(
                        "/task/edit/missing",
                        data={
                            "name": "新任务名",
                            "command": "task other.py",
                            "cron": "",
                            "enabled": "1",
                        },
                        headers={"X-Token": TOKEN},
                    )

            self.assertEqual(response.status_code, 404)
            save_tasks.assert_not_called()
            reload_scheduler.assert_not_called()
            self.assertEqual(
                json.loads(tasks_file.read_text(encoding="utf-8"))[0]["name"],
                "原任务",
            )

    def test_task_edit_saves_retry_and_sanitizes_external_back_url(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "collections.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "c1",
                            "name": "合集一",
                            "remark": "",
                            "created_at": "2026-07-04 00:00:00",
                            "updated_at": "2026-07-04 00:00:00",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            tasks_file = data_dir / "tasks.json"
            tasks_file.write_text(
                json.dumps(
                    [
                        {
                            "id": "task-edit",
                            "name": "编辑任务",
                            "command": "task demo.py",
                            "collection_id": "c1",
                            "retry": {
                                "attempts": 1,
                                "interval_seconds": 30,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().post(
                "/task/edit/task-edit",
                data={
                    "back": "https://example.invalid/evil",
                    "name": "编辑任务2",
                    "command": "task demo2.py",
                    "collection_id": "c1",
                    "retry_attempts": "4",
                    "retry_interval_seconds": "120",
                    "enabled": "1",
                },
                headers={"X-Token": TOKEN},
            )

            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers.get("Location"), "/collections#collection-c1")

            task = json.loads(tasks_file.read_text(encoding="utf-8"))[0]

            self.assertEqual(task["name"], "编辑任务2")
            self.assertEqual(task["retry"], {"attempts": 4, "interval_seconds": 120})
            self.assertNotIn("retry_count", task)

    def test_tasks_page_keeps_ajax_actions_bulk_toolbar_and_collapsible_command(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "tasks.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "task-list",
                            "name": "普通任务",
                            "command": "task demo.py " + "--flag " * 24,
                            "config_path": "conf/app.yml",
                            "enabled": True,
                            "pinned": False,
                            "retry": {
                                "attempts": 0,
                                "interval_seconds": 60,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/tasks",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("fls-collapsible-code", html)
            self.assertIn("fls-value-preview", html)
            self.assertIn("task-bulk-toolbar", html)
            self.assertIn("task-bulk-btn", html)
            self.assertIn("taskBulkAction('enable')", html)
            self.assertIn("taskBulkAction('delete')", html)
            self.assertIn("flsBulkActionMessage(json", html)
            self.assertIn("task-action-more", html)
            self.assertIn("task-action-more-menu", html)
            self.assertIn("taskAjaxAction('copy','task-list')", html)
            self.assertIn("taskAjaxAction('pin','task-list')", html)
            self.assertIn("taskAjaxAction('stop','task-list')", html)
            self.assertIn('href="/task/edit/task-list?back=/tasks"', html)
            self.assertIn('href="/task/config/task-list?back=/tasks"', html)
            self.assertNotIn('href="/task/pin/task-list', html)
            self.assertNotIn('href="/stop/task-list', html)
            self.assertNotIn('href="/task/delete/task-list', html)

    def test_logs_page_keeps_group_bulk_controls_and_post_delete_forms(self):
        with isolated_app() as (app, base_dir):
            log_dir = base_dir / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "alpha-one.log").write_text(
                "===== 启动任务: Alpha <x> =====\nalpha\n",
                encoding="utf-8",
            )
            (log_dir / "beta.log").write_text(
                "===== 启动任务: Beta =====\nbeta\n",
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/logs",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("log-bulk-toolbar", html)
            self.assertIn("logsSelectAllGroups", html)
            self.assertIn("logsDeleteSelectedGroupsBtn", html)
            self.assertIn("flsLogsToggleAllGroups", html)
            self.assertIn("flsLogsDeleteSelectedGroups", html)
            self.assertIn('fetch("/api/logs/groups/delete"', html)
            self.assertIn('class="log-group-select"', html)
            self.assertIn('data-log-group="Alpha &lt;x&gt;"', html)
            self.assertIn('value="Alpha &lt;x&gt;"', html)
            self.assertIn("任务：Alpha &lt;x&gt;", html)
            self.assertNotIn("Alpha <x>", html)
            self.assertIn(
                "flsLogsDeleteGroups([this.dataset.group]);",
                html,
            )
            self.assertIn(
                '<form class="inline-form" method="post" '
                'action="/logfile/delete/alpha-one.log?back=/logs">',
                html,
            )
            self.assertIn(
                '<a class="btn btn-orange" href="/logfile/alpha-one.log?back=/logs">',
                html,
            )
            self.assertNotIn('href="/logfile/delete/alpha-one.log', html)

    def test_logfile_view_preserves_safe_back_and_sanitizes_external_back(self):
        with isolated_app() as (app, base_dir):
            log_dir = base_dir / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "live.log").write_text("live\n", encoding="utf-8")

            client = app.test_client()

            response = client.get(
                "/logfile/live.log?back=/history",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">日志文件：live.log</div>', html)
            self.assertIn('<a class="btn btn-gray" href="/history">返回</a>', html)
            self.assertIn(
                'action="/logfile/delete/live.log?back=/history"',
                html,
            )
            self.assertIn('fetch("/api/logfile/live.log?lines=1500"', html)
            self.assertNotIn('href="/logfile/delete/live.log', html)

            response = client.get(
                "/logfile/live.log?back=https://example.invalid/evil",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<a class="btn btn-gray" href="/logs">返回</a>', html)
            self.assertIn(
                'action="/logfile/delete/live.log?back=/logs"',
                html,
            )
            self.assertNotIn("example.invalid", html)

    def test_task_log_page_keeps_history_table_actions_and_safe_back(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            log_dir = base_dir / "log"
            data_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)
            history_log = log_dir / "task-history.log"
            history_log.write_text("history\n", encoding="utf-8")
            (data_dir / "tasks.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "task-history",
                            "name": "历史任务 <x>",
                            "command": "task demo.py --name <x>",
                            "config_path": "conf/app.yml",
                            "enabled": True,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (data_dir / "task_history.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "history-1",
                            "task_id": "task-history",
                            "task_name": "历史任务 <x>",
                            "command": "task demo.py --name <x>",
                            "status": "failed",
                            "start_at": "2026-07-04 01:00:00",
                            "duration_seconds": 3,
                            "return_code": 2,
                            "source": "manual",
                            "message": "exit <bad>",
                            "log_file": str(history_log),
                        }
                    ]
                ),
                encoding="utf-8",
            )

            client = app.test_client()

            response = client.get(
                "/log/task-history?back=/history",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn("日志：历史任务 &lt;x&gt;", html)
            self.assertNotIn("历史任务 <x>", html)
            self.assertIn('<div class="card-title">最近运行历史</div>', html)
            self.assertIn('<span class="badge red">失败</span>', html)
            self.assertIn("<td>manual</td>", html)
            self.assertIn("<td>exit &lt;bad&gt;</td>", html)
            self.assertIn(
                'href="/logfile/task-history.log?back=/log/task-history"',
                html,
            )
            self.assertIn(
                'action="/run/task-history?back=/history"',
                html,
            )
            self.assertIn(
                'action="/stop/task-history?back=/history"',
                html,
            )
            self.assertIn(
                'href="/task/config/task-history?back=/history"',
                html,
            )
            self.assertIn('<a class="btn btn-gray" href="/history">返回</a>', html)
            self.assertIn('fetch("/api/log/task-history?lines=1200"', html)
            self.assertNotIn('href="/run/task-history', html)
            self.assertNotIn('href="/stop/task-history', html)

            response = client.get(
                "/log/task-history?back=https://example.invalid/evil",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<a class="btn btn-gray" href="/tasks">返回</a>', html)
            self.assertIn(
                'action="/stop/task-history?back=/tasks"',
                html,
            )
            self.assertNotIn("example.invalid", html)

    def test_history_page_filters_rows_and_escapes_history_fields(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            log_dir = base_dir / "log"
            data_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)
            alpha_log = log_dir / "alpha-history.log"
            alpha_log.write_text("alpha\n", encoding="utf-8")
            (data_dir / "task_history.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "h1",
                            "task_id": "alpha",
                            "task_name": "Alpha <x>",
                            "command": "task alpha.py --arg <x>",
                            "status": "success",
                            "start_at": "2026-07-04 02:00:00",
                            "end_at": "2026-07-04 02:00:03",
                            "duration_seconds": 3,
                            "return_code": 0,
                            "source": "manual",
                            "retry_attempt": 1,
                            "max_retries": 3,
                            "message": "done <ok>",
                            "log_file": str(alpha_log),
                        },
                        {
                            "id": "h2",
                            "task_id": "beta",
                            "task_name": "Beta",
                            "command": "task beta.py",
                            "status": "failed",
                            "source": "cron",
                            "message": "hidden",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/history?q=Alpha%20%3Cx%3E&status=success",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">运行历史</div>', html)
            self.assertIn("当前匹配 <b>1</b> 条", html)
            self.assertIn('name="q" value="Alpha &lt;x&gt;"', html)
            self.assertIn('<option value="success" selected>成功</option>', html)
            self.assertIn("Alpha &lt;x&gt;", html)
            self.assertIn("task alpha.py --arg &lt;x&gt;", html)
            self.assertIn('<span class="badge green">成功</span>', html)
            self.assertIn("<td>manual</td>", html)
            self.assertIn("<td>1/3</td>", html)
            self.assertIn("<td>done &lt;ok&gt;</td>", html)
            self.assertIn(
                'href="/logfile/alpha-history.log?back=/history"',
                html,
            )
            self.assertNotIn("Alpha <x>", html)
            self.assertNotIn("done <ok>", html)
            self.assertNotIn("Beta", html)
            self.assertNotIn("hidden", html)

    def test_dashboard_keeps_recent_and_abnormal_history_summaries(self):
        with isolated_app() as (app, base_dir):
            data_dir = base_dir / "data"
            log_dir = base_dir / "log"
            data_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)
            success_log = log_dir / "success-history.log"
            failed_log = log_dir / "failed-history.log"
            success_log.write_text("success\n", encoding="utf-8")
            failed_log.write_text("failed\n", encoding="utf-8")
            (data_dir / "task_history.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "h1",
                            "task_id": "success",
                            "task_name": "Recent <ok>",
                            "status": "success",
                            "start_at": "2026-07-04 03:00:00",
                            "duration_seconds": 2,
                            "message": "done <ok>",
                            "log_file": str(success_log),
                        },
                        {
                            "id": "h2",
                            "task_id": "failed",
                            "task_name": "Broken <x>",
                            "status": "failed",
                            "start_at": "2026-07-04 03:01:00",
                            "duration_seconds": 5,
                            "message": "boom <bad>",
                            "log_file": str(failed_log),
                        },
                    ]
                ),
                encoding="utf-8",
            )

            response = app.test_client().get(
                "/",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">最近运行</div>', html)
            self.assertIn('<div class="card-title">最近异常</div>', html)
            self.assertIn("Recent &lt;ok&gt;", html)
            self.assertIn("Broken &lt;x&gt;", html)
            self.assertIn('<span class="badge green">成功</span>', html)
            self.assertIn('<span class="badge red">失败</span>', html)
            self.assertIn("<td>done &lt;ok&gt;</td>", html)
            self.assertIn("<td>boom &lt;bad&gt;</td>", html)
            self.assertIn(
                'href="/logfile/success-history.log?back=/"',
                html,
            )
            self.assertIn(
                'href="/logfile/failed-history.log?back=/"',
                html,
            )
            self.assertNotIn("Recent <ok>", html)
            self.assertNotIn("Broken <x>", html)
            self.assertNotIn("boom <bad>", html)

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

    def test_about_page_renders_panel_info_and_code_cards(self):
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

    def test_online_scripts_page_renders_header_card_and_actions(self):
        with isolated_app() as (app, base_dir):
            cache_file = base_dir / "data" / "online_scripts_cache.json"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("[]", encoding="utf-8")

            response = app.test_client().get(
                "/online-scripts",
                headers={"X-Token": TOKEN},
            )

            html = response.get_data(as_text=True)

            self.assertEqual(response.status_code, 200)
            self.assertIn('<div class="card-title">在线脚本</div>', html)
            self.assertIn("默认读取本地缓存", html)
            self.assertIn("task_cron.var", html)
            self.assertIn('style="min-width:0;flex:1 1 360px;"', html)
            self.assertIn('<div class="fls-source-code">', html)
            self.assertIn('<div class="action-row">', html)
            self.assertIn('action="/online-scripts/refresh"', html)
            self.assertIn('id="onlineRefreshBtn"', html)
            self.assertIn('href="/online-scripts/source"', html)
            self.assertIn('href="/config"', html)
            self.assertIn('<div class="card-title">脚本列表，本地缓存</div>', html)

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

    def test_api_proxy_test_saved_missing_returns_404_json_without_network(self):
        with isolated_app() as (app, _base_dir):
            with patch(
                "fls_manager.routes.proxy.api.test_proxy_object"
            ) as test_proxy_object:
                response = app.test_client().get(
                    "/api/proxy/test/missing-proxy",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.content_type, "application/json")
            self.assertEqual(
                response.get_json(),
                {
                    "ok": False,
                    "name": "",
                    "error": "代理不存在",
                },
            )
            test_proxy_object.assert_not_called()

    def test_online_install_log_api_returns_stable_polling_fields(self):
        with isolated_app() as (app, _base_dir):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING

            ONLINE_INSTALL_RUNNING["install-1"] = {
                "id": "install-1",
                "script_id": "demo",
                "script_name": "Demo",
                "log_file": "/tmp/online-script-install-demo.log",
                "running": 1,
                "status": "安装中",
                "returncode": None,
                "error": "",
                "process": object(),
            }

            with patch(
                "fls_manager.routes.online_scripts.logs.tail_file",
                return_value="clone complete\ninstalling dependencies",
            ) as tail_file:
                response = app.test_client().get(
                    "/api/online-scripts/log/install-1?lines=1600",
                    headers={"X-Token": TOKEN},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content_type, "application/json")
            self.assertEqual(
                response.get_json(),
                {
                    "running": True,
                    "status": "安装中",
                    "returncode": None,
                    "error": "",
                    "log_file": "/tmp/online-script-install-demo.log",
                    "log": "clone complete\ninstalling dependencies",
                },
            )
            tail_file.assert_called_once_with(
                "/tmp/online-script-install-demo.log",
                1600,
            )


if __name__ == "__main__":
    unittest.main()
