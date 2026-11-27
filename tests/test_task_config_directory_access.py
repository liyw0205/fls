import io
import threading
import unittest
from unittest.mock import Mock, patch

from tests import test_script_directory_access as directory_tests
from tests import test_task_config_recovery as config_tests
from tests.test_ui_route_components import TOKEN, isolated_app


class TaskConfigDirectoryAccessTests(unittest.TestCase):
    def test_all_restore_states_reject_config_before_task_or_file_access(self):
        helper = directory_tests.ScriptDirectoryAccessTests()
        for mode in ('legacy', 'native', 'journal', 'io'):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                target = config_tests.TaskConfigRecoveryTests().prepare(base)
                with helper.blocked(mode, base), patch(
                    'fls_manager.routes.tasks.config_file.get_task',
                    side_effect=AssertionError('task read during restore'),
                ), patch('fls_manager.routes.tasks.config_file._save_config_atomic') as save:
                    for method, arguments in (
                        ('get', {}),
                        ('post', {'data': {'content': 'draft', 'config_revision': 'a' * 64}}),
                    ):
                        with self.subTest(method=method):
                            response = getattr(app.test_client(), method)(
                                '/task/config/config', headers={'X-Token': TOKEN}, **arguments,
                            )
                            self.assertEqual(response.status_code, 503, response.get_data(as_text=True))
                            self.assertEqual(response.json['code'], 'panel_storage_busy')
                            self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    save.assert_not_called()
                    self.assertEqual(target.read_bytes(), b'key: original\n')
                helper.assert_restore_available()

    def test_busy_after_admission_propagates_without_misleading_file_error(self):
        for stage in ('get_task', 'task_config_safe_path', '_read_config_snapshot', '_save_config_atomic'):
            with self.subTest(stage=stage), isolated_app() as (app, base):
                from fls_manager.data_access import PanelStorageBusy

                target = config_tests.TaskConfigRecoveryTests().prepare(base)
                client = app.test_client()
                form = config_tests.TaskConfigRecoveryTests().form(client)
                with patch('fls_manager.routes.tasks.config_file.' + stage, side_effect=PanelStorageBusy):
                    response = client.post('/task/config/config', data={
                        **form.fields, 'content': 'draft',
                    }, headers={'X-Token': TOKEN})
                self.assertEqual(response.status_code, 503, response.get_data(as_text=True))
                self.assertEqual(response.json['code'], 'panel_storage_busy')
                self.assertEqual(target.read_bytes(), b'key: original\n')
                self.assertEqual(list(target.parent.glob('.fls-config-*.tmp')), [])

    def test_restore_lease_covers_snapshot_publication_and_final_temporary_cleanup(self):
        helper = directory_tests.ScriptDirectoryAccessTests()
        for stage in ('read', 'replace', 'cleanup'):
            with self.subTest(stage=stage), isolated_app() as (app, base):
                from fls_manager.routes.tasks import config_file

                target = config_tests.TaskConfigRecoveryTests().prepare(base)
                client = app.test_client()
                form = config_tests.TaskConfigRecoveryTests().form(client)
                entered, release = threading.Event(), threading.Event()
                responses, failures = [], []
                module, name = {
                    'read': (config_file, '_read_config_snapshot'),
                    'replace': (config_file.os, 'replace'),
                    'cleanup': (type(target), 'unlink'),
                }[stage]
                original = getattr(module, name)

                def pause(*args, **kwargs):
                    if stage != 'cleanup' or args[0].name.startswith('.fls-config-'):
                        entered.set()
                        if not release.wait(15):
                            raise AssertionError('config operation was not released')
                    return original(*args, **kwargs)

                def request_config():
                    try:
                        if stage == 'read':
                            response = app.test_client().get('/task/config/config', headers={'X-Token': TOKEN})
                        else:
                            response = app.test_client().post('/task/config/config', data={
                                **form.fields, 'content': 'saved\r\n',
                            }, headers={'X-Token': TOKEN})
                        responses.append(response)
                    except BaseException as exc:
                        failures.append(exc)

                with patch.object(module, name, autospec=True, side_effect=pause):
                    worker = threading.Thread(target=request_config)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(15))
                        helper.assert_restore_blocked()
                    finally:
                        release.set()
                        worker.join(20)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0].status_code, 200, responses[0].get_data(as_text=True))
                self.assertEqual(target.read_bytes(), b'key: original\n' if stage == 'read' else b'saved\r\n')
                self.assertEqual(list(target.parent.glob('.fls-config-*.tmp')), [])
                helper.assert_restore_available()

    def test_oversized_snapshot_and_multibyte_draft_preserve_original_and_no_temporary_file(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.tasks import config_file

            target = config_tests.TaskConfigRecoveryTests().prepare(base)
            client = app.test_client()
            form = config_tests.TaskConfigRecoveryTests().form(client)
            # Isolate the editor's byte limit from Werkzeug's independent form parser limit.
            with patch.object(config_file, 'MAX_CONFIG_BYTES', 1024):
                for draft in ('a' * 1025, '\u4e2d' * 342):
                    with self.subTest(bytes=len(draft.encode('utf-8'))), patch.object(
                        config_file.os, 'replace', side_effect=AssertionError('oversized draft was published'),
                    ):
                        response = client.post('/task/config/config', data={
                            **form.fields, 'content': draft,
                        }, headers={'X-Token': TOKEN})
                    self.assertEqual(response.status_code, 413)
                    self.assertEqual(config_tests.ConfigFormParser(response.get_data(as_text=True)).textareas['content'], draft)
                    self.assertEqual(target.read_bytes(), b'key: original\n')
                    self.assertEqual(list(target.parent.glob('.fls-config-*.tmp')), [])
                target.write_bytes(b'x' * 1025)
                response = client.get('/task/config/config', headers={'X-Token': TOKEN})
                self.assertEqual(response.status_code, 413)
                self.assertNotIn('x' * 100, response.get_data(as_text=True))

    def test_snapshot_bounds_actual_read_and_closes_handle_when_file_grows_after_stat(self):
        with isolated_app() as (_, base):
            from fls_manager.routes.tasks import config_file

            target = config_tests.TaskConfigRecoveryTests().prepare(base)
            handle = io.BytesIO(b'x' * (config_file.MAX_CONFIG_BYTES + 2))
            read = Mock(wraps=handle.read)
            handle.read = read
            with patch.object(config_file.os, 'fdopen', return_value=handle), patch.object(
                config_file.os, 'open', return_value=123,
            ), patch.object(config_file.os, 'fstat', return_value=target.stat()):
                with self.assertRaises(config_file.TaskConfigLimitError):
                    config_file._read_config_snapshot(target, 'conf/app.yml')
            read.assert_called_once_with(config_file.MAX_CONFIG_BYTES + 1)
            self.assertTrue(handle.closed)
