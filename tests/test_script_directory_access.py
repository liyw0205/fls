import contextlib
import io
import os
import threading
import unittest
from unittest.mock import patch

from tests import test_backup_activity_lease as activity_tests
from tests import test_backup_data_access as access_tests
from tests import test_script_directory_jobs as job_tests
from tests.test_ui_route_components import TOKEN, isolated_app


class ScriptDirectoryAccessTests(unittest.TestCase):
    def assert_restore_available(self):
        job_tests.ScriptDirectoryJobTests().assert_restore_available()

    def assert_restore_blocked(self):
        job_tests.ScriptDirectoryJobTests().assert_restore_blocked()

    def requests(self):
        operation = 'a' * 32
        routes = [
            ('get', '/api/scripts/tree', {}),
            ('get', '/api/scripts/files', {}),
            ('get', '/api/scripts/file?path=demo.py', {}),
            ('get', '/api/scripts/delete/preview?path=demo.py', {}),
            ('get', '/api/scripts/rename/preview?path=demo.py&new_name=renamed.py', {}),
            ('get', '/api/scripts/rename/' + operation, {}),
            ('get', '/pull', {}),
            ('get', '/pull/new', {}),
            ('get', '/pull/fetch', {}),
            ('get', '/pull/import', {}),
            ('get', '/scripts/view?path=demo.py', {}),
            ('get', '/scripts/rename?path=demo.py', {}),
            ('get', '/scripts/rename-recovery/' + operation, {}),
            ('get', '/scripts/download/demo.py', {}),
            ('post', '/api/scripts/file', {'json': {'path': 'new.py', 'kind': 'file', 'content': 'new'}}),
            ('put', '/api/scripts/file', {'json': {'path': 'demo.py', 'content': 'new', 'revision': 'b' * 64}}),
            ('delete', '/api/scripts/file', {'json': {'path': 'demo.py', 'revision': 'b' * 64, 'confirm_references': True}}),
            ('post', '/api/scripts/rename', {'json': {'path': 'demo.py', 'new_name': 'renamed.py',
                                                  'revision': 'b' * 64, 'operation_id': operation, 'references': []}}),
            ('post', '/api/scripts/rename/' + operation + '/recovery', {'json': {'action': 'resume'}}),
            ('post', '/pull/new', {'data': {'item_type': 'file', 'name': 'new.py', 'content': 'new'}}),
            ('post', '/pull/new', {'data': {'item_type': 'dir', 'name': 'new-dir'}}),
            ('post', '/pull/fetch', {'data': {'url': 'https://example.invalid/demo.py', 'filename': 'demo.py'}}),
            ('post', '/pull/fetch', {'data': {'url': 'https://example.invalid/repo.git', 'pull_type': 'repo', 'filename': 'repo'}}),
            ('post', '/pull/import', {'data': {'file': (io.BytesIO(b'new'), 'new.py')}}),
            ('post', '/scripts/view?path=demo.py', {'data': {'content': 'new', 'file_revision': 'b' * 64}}),
            ('post', '/scripts/rename?path=demo.py', {'data': {'new_name': 'renamed.py', 'confirm': '1'}}),
            ('post', '/scripts/rename-recovery/' + operation, {'data': {'action': 'rollback'}}),
            ('post', '/scripts/delete/demo.py', {}),
            ('post', '/scripts/delete/folder', {}),
        ]
        return routes

    @contextlib.contextmanager
    def blocked(self, mode, base):
        if mode in ('legacy', 'native'):
            with activity_tests.BackupActivityLeaseTests().holder(mode):
                yield
        elif mode == 'io':
            with access_tests.BackupDataAccessTests().paused_process(
                "with directory_access(exclusive=True):\n"
                " print('ready',flush=True)\n sys.stdin.readline()\n",
            ):
                yield
        else:
            from fls_manager.backup_commit import JOURNAL_NAME

            directory = base / JOURNAL_NAME
            directory.mkdir()
            journal = directory / ('b' * 32 + '.json')
            journal.write_bytes(b'{broken')
            try:
                yield
            finally:
                self.assertEqual(journal.read_bytes(), b'{broken')
                journal.unlink()

    def test_restore_pending_journal_and_io_conflict_reject_all_file_routes_before_effects(self):
        for mode in ('legacy', 'native', 'journal', 'io'):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                scripts = base / 'scripts'
                (scripts / 'demo.py').write_bytes(b'original')
                (scripts / 'folder').mkdir()
                (scripts / 'folder' / 'nested.py').write_bytes(b'nested')
                self.assert_restore_available()
                with self.blocked(mode, base), patch(
                    'fls_manager.routes.scripts.pull.fetch_file_bytes', side_effect=AssertionError('network started'),
                ), patch('fls_manager.routes.scripts.pull.DirectoryProcess', side_effect=AssertionError('Git started')):
                    before = sorted(str(path.relative_to(base)) for path in base.rglob('*'))
                    for method, url, arguments in self.requests():
                        with self.subTest(method=method, url=url):
                            response = getattr(app.test_client(), method)(url, headers={'X-Token': TOKEN}, **arguments)
                            self.assertEqual(response.status_code, 503, response.get_data(as_text=True)[:300])
                            self.assertEqual(response.json['code'], 'panel_storage_busy')
                            self.assertEqual(response.headers['Cache-Control'], 'no-store')
                            response.close()
                    self.assertEqual(sorted(str(path.relative_to(base)) for path in base.rglob('*')), before)
                    self.assertEqual((scripts / 'demo.py').read_bytes(), b'original')
                    self.assertEqual((scripts / 'folder' / 'nested.py').read_bytes(), b'nested')
                self.assert_restore_available()

    def test_file_read_edit_and_web_fetch_import_hold_lease_until_cleanup_without_config_lock(self):
        for kind in ('read', 'edit', 'fetch', 'import'):
            with self.subTest(kind=kind), isolated_app() as (app, base):
                from fls_manager import storage
                from fls_manager.routes.scripts import native_files, pull

                (base / 'scripts' / 'demo.py').write_bytes(b'original')
                current = app.test_client().get('/api/scripts/file?path=demo.py', headers={'X-Token': TOKEN})
                revision = current.json['file']['revision']
                entered, release = threading.Event(), threading.Event()
                responses, errors = [], []
                if kind == 'read':
                    original = native_files.script_file_snapshot
                elif kind == 'edit':
                    original = native_files.publish_script_file
                elif kind == 'fetch':
                    original = lambda *args, **kwargs: b'fetched'
                else:
                    original = pull.shutil.copy2

                def pause(*args, **kwargs):
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('test operation was not released')
                    return original(*args, **kwargs)

                def request():
                    try:
                        client = app.test_client()
                        if kind == 'read':
                            response = client.get('/api/scripts/file?path=demo.py', headers={'X-Token': TOKEN})
                        elif kind == 'edit':
                            response = client.put('/api/scripts/file', json={
                                'path': 'demo.py', 'content': 'edited', 'revision': revision,
                            }, headers={'X-Token': TOKEN})
                        elif kind == 'fetch':
                            response = client.post('/pull/fetch', data={
                                'url': 'https://example.invalid/fetched.py', 'filename': 'fetched.py',
                            }, headers={'X-Token': TOKEN})
                        else:
                            response = client.post('/pull/import', data={
                                'file': (io.BytesIO(b'imported'), 'imported.py'),
                            }, headers={'X-Token': TOKEN})
                        responses.append(response)
                    except BaseException as exc:
                        errors.append(exc)

                module, name = {
                    'read': (native_files, 'script_file_snapshot'),
                    'edit': (native_files, 'publish_script_file'),
                    'fetch': (pull, 'fetch_file_bytes'),
                    'import': (pull.shutil, 'copy2'),
                }[kind]
                worker = threading.Thread(target=request)
                with patch.object(module, name, side_effect=pause):
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(10))
                        self.assert_restore_blocked()
                        storage.write_json(base / 'data' / 'unrelated.json', {'saved': True})
                        self.assertEqual(storage.read_json(base / 'data' / 'unrelated.json', {}), {'saved': True})
                    finally:
                        release.set()
                        worker.join(15)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0].status_code, 200, responses[0].get_data(as_text=True)[:300])
                if kind != 'read':
                    filename, content = {'edit': ('demo.py', b'edited'), 'fetch': ('fetched.py', b'fetched'),
                                         'import': ('imported.py', b'imported')}[kind]
                    self.assertEqual((base / 'scripts' / filename).read_bytes(), content)
                self.assertEqual(list((base / 'scripts').glob('.fls-*')), [])
                self.assert_restore_available()

    def test_storage_busy_after_admission_is_not_rendered_as_an_ordinary_file_error(self):
        cases = [
            ('get', '/pull', 'pages.load_script_renames', {}),
            ('post', '/pull/new', 'files._guard_file_mutation', {'data': {'name': 'new.py', 'content': 'new'}}),
            ('get', '/scripts/view?path=demo.py', 'files.pending_script_rename', {}),
            ('get', '/scripts/rename?path=demo.py', 'files.get_script_rename', {}),
            ('get', '/scripts/rename-recovery/' + 'a' * 32, 'files.get_script_rename', {}),
            ('post', '/scripts/delete/demo.py', 'files._guard_file_mutation', {}),
            ('post', '/pull/fetch', 'pull.fetch_file_bytes', {'data': {'url': 'https://example.invalid/demo.py'}}),
            ('get', '/api/scripts/rename/' + 'a' * 32, 'native_files.get_script_rename', {}),
            ('post', '/api/scripts/rename/' + 'a' * 32 + '/recovery', 'native_files.get_script_rename',
             {'json': {'action': 'resume'}}),
        ]
        for method, url, target, arguments in cases:
            with self.subTest(method=method, url=url), isolated_app() as (app, base):
                from fls_manager.data_access import PanelStorageBusy

                script = base / 'scripts' / 'demo.py'
                script.write_bytes(b'original')
                with patch('fls_manager.routes.scripts.' + target, side_effect=PanelStorageBusy):
                    response = getattr(app.test_client(), method)(url, headers={'X-Token': TOKEN}, **arguments)
                self.assertEqual(response.status_code, 503, response.get_data(as_text=True)[:300])
                self.assertEqual(response.json['code'], 'panel_storage_busy')
                self.assertEqual(script.read_bytes(), b'original')
                self.assertFalse((base / 'scripts' / 'new.py').exists())
                self.assert_restore_available()

    def test_download_lease_covers_complete_read_early_close_and_unstarted_response(self):
        for finish in ('complete', 'early', 'unstarted'):
            with self.subTest(finish=finish), isolated_app() as (app, base):
                from fls_manager.routes.scripts.files import scripts_download

                content = b'original' * (256 * 1024)
                (base / 'scripts' / 'demo.py').write_bytes(content)
                with app.test_request_context('/scripts/download/demo.py'):
                    response = scripts_download('demo.py')
                self.assert_restore_blocked()
                try:
                    if finish == 'complete':
                        self.assertEqual(b''.join(response.response), content)
                        self.assert_restore_available()
                    elif finish == 'early':
                        self.assertTrue(next(iter(response.response)))
                        self.assert_restore_blocked()
                finally:
                    response.close()
                    response.close()
                self.assert_restore_available()

    def test_busy_during_rename_reload_preserves_original_recovery_record(self):
        with isolated_app() as (app, base):
            from fls_manager.data_access import PanelStorageBusy

            script = base / 'scripts' / 'demo.py'
            script.write_bytes(b'original')
            client = app.test_client()
            headers = {'X-Token': TOKEN}
            preview = client.get('/api/scripts/rename/preview?path=demo.py&new_name=renamed.py', headers=headers)
            operation_id = 'a' * 32
            body = {'path': 'demo.py', 'new_name': 'renamed.py', 'revision': preview.json['preview']['revision'],
                    'operation_id': operation_id, 'references': []}
            with patch('fls_manager.scheduler.reload_scheduler', side_effect=PanelStorageBusy):
                response = client.post('/api/scripts/rename', json=body, headers=headers)
            self.assertEqual(response.status_code, 503, response.json)
            self.assertEqual(response.json['code'], 'panel_storage_busy')
            self.assertFalse(script.exists())
            self.assertEqual((base / 'scripts' / 'renamed.py').read_bytes(), b'original')
            state = client.get('/api/scripts/rename/' + operation_id, headers=headers)
            self.assertEqual(state.json['operation']['status'], 'tasks_saved')
            self.assertFalse(state.json['operation']['finished'])
            resumed = client.post('/api/scripts/rename/' + operation_id + '/recovery', json={'action': 'resume'}, headers=headers)
            self.assertEqual(resumed.status_code, 200, resumed.json)
            self.assertEqual(resumed.json['operation']['status'], 'succeeded')
            self.assert_restore_available()

    def test_download_pins_file_at_admission_and_releases_after_open_or_read_failure(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.scripts import files

            target = base / 'scripts' / 'demo.py'
            target.write_bytes(b'original')
            response = app.test_client().get('/scripts/download/demo.py', headers={'X-Token': TOKEN}, buffered=False)
            try:
                # Consume after the original path disappears; the already-open file owns the bytes.
                if os.name != 'nt':
                    target.rename(target.with_suffix('.previous'))
                    target.write_bytes(b'replacement')
                self.assertEqual(response.get_data(), b'original')
            finally:
                response.close()
            self.assert_restore_available()
            self.assertEqual(app.test_client().get('/scripts/download/missing.py', headers={'X-Token': TOKEN}).status_code, 404)
            self.assert_restore_available()
            with patch('builtins.open', side_effect=PermissionError('unreadable')):
                with app.test_request_context('/scripts/download/demo.py'), self.assertRaises(PermissionError):
                    files.scripts_download('demo.py')
            self.assert_restore_available()
            with target.open('rb') as source, patch('builtins.open', return_value=source):
                with app.test_request_context('/scripts/download/demo.py'):
                    response = files.scripts_download('demo.py')
                with patch.object(source, 'read', side_effect=OSError('read failed')):
                    with self.assertRaises(OSError):
                        next(iter(response.response))
                self.assertTrue(source.closed)
                response.close()
            self.assert_restore_available()


if __name__ == '__main__':
    unittest.main()
