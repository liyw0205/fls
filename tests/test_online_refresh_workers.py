import contextlib
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import test_backup_activity_lease as activity_tests
from tests import test_backup_data_access as access_tests
from tests import test_online_install_workers as workers
from tests import test_script_directory_jobs as directories
from tests.test_ui_route_components import TOKEN, isolated_app


WORKER = r'''
import json, os, sys, threading, time
from fls_manager.app import create_app
from fls_manager.paths import BASE_DIR
from fls_manager.online_scripts import constants, source
from fls_manager.online_scripts.refresh_coordination import REFRESH_IDENTITIES
from fls_manager.routes.online_scripts import refresh as routes
from fls_manager.directory_jobs import directory_job
label, mode = sys.argv[1:]
started, release = (BASE_DIR / (label + suffix) for suffix in ('.started', '.release'))
def pause():
    started.touch()
    deadline = time.monotonic() + 45
    while not release.exists():
        if time.monotonic() > deadline: raise RuntimeError('refresh barrier expired')
        time.sleep(.01)
class Response:
    status_code = 200
    headers = {}
    def iter_content(self, **kwargs):
        if mode == 'http': pause()
        yield b'[]'
    def close(self):
        if mode == 'close': pause()
def fetch(*args, **kwargs):
    with (BASE_DIR / (label + '.calls')).open('a') as stream: stream.write('fetch\n')
    return Response()
source.requests = type('Requests', (), {'get':staticmethod(fetch)})
if mode == 'prepared':
    original = routes.reserve_online_refresh
    def reserve(*args):
        result = original(*args)
        if result and not result.get('_running'): pause()
        return result
    routes.reserve_online_refresh = reserve
elif mode in ('confirm', 'recover'):
    name = '_confirm_online_refresh_result_locked' if mode == 'confirm' else 'recover_online_refresh_receipt'
    original = getattr(routes, name)
    def operation(*args):
        pause()
        return original(*args)
    setattr(routes, name, operation)
elif mode == 'finalize':
    from fls_manager.directory_jobs import DirectoryJobOwner
    original = DirectoryJobOwner.close
    def close(self):
        if self.lock is REFRESH_IDENTITIES: pause()
        return original(self)
    DirectoryJobOwner.close = close
app = create_app()
headers = {'X-Token':os.environ['FLS_TOKEN']}
pending, result, held = None, [], {}
def request(command):
    response = getattr(app.test_client(), command['method'])(command['path'],
        json=command.get('data'), headers=headers)
    return [response.status_code, response.get_json(silent=True)]
try:
    for line in sys.stdin:
        command = json.loads(line)
        method, data = command['method'], command.get('data') or {}
        if method == 'quit': break
        if method == 'begin':
            pending = threading.Thread(target=lambda command=dict(command):result.append(
                request(dict(command, method='post'))), daemon=True)
            pending.start()
            payload = [200, {'accepted':True}]
        elif method == 'result':
            pending.join(15)
            payload = result.pop() if result else [500, {'error':'request did not finish'}]
        elif method == 'claim':
            lease = directory_job()
            lease.__enter__()
            with constants.ONLINE_REFRESH_LOCK:
                owner = REFRESH_IDENTITIES.claim(data['request_id'])
            held[data['request_id']] = owner, lease
            payload = [200, {'claimed':True}]
        elif method == 'diagnose':
            constants.get_online_refresh_state()
            constants.ONLINE_REFRESH_STATE['_diagnostic_disk'] = dict(constants.ONLINE_REFRESH_STATE)
            constants.ONLINE_REFRESH_STATE['persistence_error'] = 'old local failure'
            payload = [200, {'remembered':True}]
        else: payload = request(command)
        print('FLS_RESULT ' + json.dumps(payload), flush=True)
finally:
    release.touch()
    if pending: pending.join(15)
    for worker in threading.enumerate():
        if worker.name == 'fls-online-scripts-refresh': worker.join(15)
    for owner, lease in held.values():
        owner.close()
        lease.__exit__(None, None, None)
'''


class OnlineRefreshWorkerTests(unittest.TestCase):
    request_id = 'a' * 32

    def worker(self, base, label, mode='normal'):
        return workers.OnlineInstallWorkerTests().worker(base, label, mode, worker_code=WORKER)

    def wait_started(self, base, label):
        workers.OnlineInstallWorkerTests().wait_started(base, label)

    def state(self, rpc, *, active=None, request_id=None):
        deadline = time.monotonic() + 15
        while True:
            code, state = rpc('get', '/api/online-scripts/refresh-status/' + (request_id or self.request_id))
            self.assertEqual(code, 200, state)
            if active is None or state['active'] is active:
                return state
            if time.monotonic() > deadline:
                self.fail(str(state))
            time.sleep(.02)

    def seed(self):
        from fls_manager.online_scripts import constants, source

        constants.update_online_refresh_state(id='b' * 32, request_id=self.request_id,
            running=False, status='结果未知', expected_cache_fingerprint=source.online_script_cache_fingerprint(),
            source_fingerprint=source.online_script_source_fingerprint())

    def test_live_refresh_and_terminal_cleanup_remain_active_across_workers(self):
        for mode in ('prepared', 'http', 'close', 'finalize'):
            with self.subTest(mode=mode), isolated_app() as (_, base):
                with self.worker(base, 'owner', mode) as (_, first), self.worker(base, 'reader') as (_, second):
                    first('begin', '/api/online-scripts/refresh', {'request_id':self.request_id})
                    self.wait_started(base, 'owner')
                    directories.ScriptDirectoryJobTests().assert_restore_blocked()
                    state = self.state(second, active=True)
                    self.assertEqual(state['running'], mode != 'finalize')
                    history = second('get', '/api/online-scripts/refresh-history')[1]
                    self.assertTrue(history['items'][0]['active'])
                    log = second('get', '/api/online-scripts/refresh-log/' + self.request_id)
                    self.assertEqual(log[0], 200, log)
                    self.assertFalse(log[1]['terminal'])
                    for path, data in (
                        ('refresh', {'request_id':self.request_id}),
                        ('refresh', {'request_id':'c' * 32}),
                        ('refresh', {'request_id':self.request_id, 'proxy_id':'changed'}),
                        ('refresh-confirm/' + self.request_id, {}),
                        ('refresh-recover/' + self.request_id, {}),
                        ('refresh-retry/' + self.request_id, {'request_id':'d' * 32}),
                    ):
                        code, info = second('post', '/api/online-scripts/' + path, data)
                        self.assertEqual(code, 409, (path, info))
                    (base / 'owner.release').touch()
                    self.assertEqual(first('result')[0], 202)
                    self.assertEqual(self.state(second, active=False)['status'], '已完成')
                    directories.ScriptDirectoryJobTests().assert_restore_available()

    def test_crashed_owner_becomes_unknown_without_refetch(self):
        for mode in ('prepared', 'http', 'close'):
            with self.subTest(mode=mode), isolated_app() as (_, base):
                with self.worker(base, 'owner', mode) as (process, first), self.worker(base, 'reader') as (_, second):
                    first('begin', '/api/online-scripts/refresh', {'request_id':self.request_id})
                    self.wait_started(base, 'owner')
                    self.assertTrue(self.state(second)['active'])
                    process.kill()
                    process.wait(10)
                    state = self.state(second, active=False)
                    self.assertFalse(state['running'])
                    self.assertEqual(state['status'], '结果未知')
                    replay = second('post', '/api/online-scripts/refresh', {'request_id':self.request_id})
                    self.assertEqual(replay[0], 409, replay)
                    self.assertEqual(replay[1]['id'], state['id'])
                    self.assertFalse((base / 'reader.started').exists())
                    directories.ScriptDirectoryJobTests().assert_restore_available()

    def test_confirm_and_repair_claim_independent_identity(self):
        for mode in ('confirm', 'recover'):
            with self.subTest(mode=mode), isolated_app() as (_, base):
                self.seed()
                with self.worker(base, 'owner', mode) as (_, first), self.worker(base, 'reader') as (_, second):
                    first('begin', '/api/online-scripts/refresh-' + mode + '/' + self.request_id)
                    self.wait_started(base, 'owner')
                    self.assertTrue(self.state(second)['active'])
                    for action in ('confirm', 'recover'):
                        self.assertEqual(second('post', '/api/online-scripts/refresh-' + action + '/' + self.request_id)[0], 409)
                    self.assertEqual(second('post', '/api/online-scripts/refresh', {'request_id':'c' * 32})[0], 409)
                    (base / 'owner.release').touch()
                    self.assertEqual(first('result')[0], 200)
                    state = self.state(second, active=False)
                    self.assertEqual(state['status'], '已确认未发布' if mode == 'confirm' else '结果未知')

    def test_live_identity_without_receipt_blocks_new_submission(self):
        with isolated_app() as (_, base):
            with self.worker(base, 'owner') as (_, first), self.worker(base, 'reader') as (_, second):
                first('claim', data={'request_id':self.request_id})
                self.assertEqual(second('post', '/api/online-scripts/refresh', {'request_id':'c' * 32})[0], 409)
                self.assertFalse((base / 'data' / 'online_script_refresh.json').exists())
                directories.ScriptDirectoryJobTests().assert_restore_blocked()

    def test_simultaneous_same_request_fetches_once(self):
        with isolated_app() as (_, base):
            with self.worker(base, 'one') as (_, first), self.worker(base, 'two') as (_, second):
                for rpc in (first, second):
                    rpc('begin', '/api/online-scripts/refresh', {'request_id':self.request_id})
                results = [rpc('result') for rpc in (first, second)]
                self.assertIn(202, [result[0] for result in results])
                self.assertTrue(all(result[0] in (200, 202, 409) for result in results), results)
                self.state(second, active=False)
                calls = sum(len(path.read_text().splitlines()) for path in base.glob('*.calls'))
                self.assertEqual(calls, 1)

    def test_live_retry_binds_parent_child_and_blocks_competing_retry(self):
        with isolated_app() as (_, base):
            self.seed()
            with self.worker(base, 'owner', 'http') as (_, first), self.worker(base, 'reader') as (_, second):
                endpoint = '/api/online-scripts/refresh-retry/' + self.request_id
                child = 'c' * 32
                self.assertEqual(first('post', endpoint, {'request_id':child})[0], 202)
                self.wait_started(base, 'owner')
                replay = second('post', endpoint, {'request_id':child})
                self.assertEqual(replay[0], 200, replay)
                self.assertTrue(replay[1]['replayed'])
                self.assertEqual(second('post', endpoint, {'request_id':'d' * 32})[0], 409)
                self.assertEqual(self.state(second, request_id=child)['retry_of'], self.request_id)
                (base / 'owner.release').touch()
                self.assertEqual(self.state(second, active=False, request_id=child)['status'], '已完成')

    def test_other_workers_new_terminal_overrides_old_local_diagnostic(self):
        with isolated_app() as (_, base):
            self.seed()
            with self.worker(base, 'one') as (_, first), self.worker(base, 'two') as (_, second):
                first('diagnose')
                result = second('post', '/api/online-scripts/refresh-confirm/' + self.request_id)
                self.assertEqual(result[0], 200, result)
                latest = self.state(first)
                self.assertEqual(latest['status'], '已确认未发布')
                self.assertNotIn('persistence_error', latest)

    def test_all_refresh_routes_guard_directory_restore_before_reads(self):
        for mode in ('legacy', 'native', 'journal', 'io'):
            with self.subTest(mode=mode), isolated_app() as (app, base), contextlib.ExitStack() as stack:
                from fls_manager.backup_commit import JOURNAL_NAME
                from fls_manager.routes.online_scripts import refresh, logs, pages

                if mode in ('legacy', 'native'):
                    stack.enter_context(activity_tests.BackupActivityLeaseTests().holder(mode))
                elif mode == 'io':
                    stack.enter_context(access_tests.BackupDataAccessTests().paused_process(
                        "with directory_access(exclusive=True):\n print('ready',flush=True)\n sys.stdin.readline()\n"))
                else:
                    directory = base / JOURNAL_NAME
                    directory.mkdir()
                    (directory / ('b' * 32 + '.json')).write_bytes(b'{broken')
                reads = [stack.enter_context(patch.object(module, 'get_online_refresh_state'))
                         for module in (refresh, logs, pages)]
                for prefix in ('', '/api'):
                    for path in ('refresh', 'refresh-retry/' + self.request_id,
                                 'refresh-recover/' + self.request_id, 'refresh-confirm/' + self.request_id):
                        result = app.test_client().post(prefix + '/online-scripts/' + path, headers={'X-Token':TOKEN})
                        self.assertEqual(result.status_code, 503, (mode, path))
                    result = app.test_client().get(prefix + '/online-scripts/refresh-log/' + self.request_id, headers={'X-Token':TOKEN})
                    self.assertEqual(result.status_code, 503)
                for path in ('/online-scripts', '/api/online-scripts/refresh-options',
                             '/api/online-scripts/refresh-status', '/api/online-scripts/refresh-history',
                             '/api/online-scripts/refresh-status/' + self.request_id,
                             '/api/online-scripts/refresh-task-link-failures/' + self.request_id):
                    self.assertEqual(app.test_client().get(path, headers={'X-Token':TOKEN}).status_code, 503, path)
                for read in reads: read.assert_not_called()

    def test_identity_precedes_receipt_and_worker_handoff_has_no_lease_gap(self):
        with isolated_app() as (app, base):
            from fls_manager.online_scripts import constants, source
            queued = []
            original = constants.persist_online_refresh

            def persist(info):
                directories.ScriptDirectoryJobTests().assert_restore_blocked()
                self.assertTrue(constants.refresh_active(info['request_id']))
                return original(info)

            response = Mock(status_code=200, headers={})
            response.iter_content.return_value = [b'[]']
            with patch.object(constants, 'persist_online_refresh', side_effect=persist), patch.object(
                threading.Thread, 'start', autospec=True, side_effect=queued.append,
            ):
                result = app.test_client().post('/api/online-scripts/refresh', json={'request_id':self.request_id}, headers={'X-Token':TOKEN})
            self.assertEqual(result.status_code, 202, result.json)
            try:
                directories.ScriptDirectoryJobTests().assert_restore_blocked()
                with patch.object(source, 'requests', SimpleNamespace(get=Mock(return_value=response))):
                    queued[0].run()
            finally:
                for owner in list(constants.REFRESH_IDENTITIES.owners.values()): owner.close()
            response.close.assert_called_once()
            directories.ScriptDirectoryJobTests().assert_restore_available()

    def test_constructor_start_and_receipt_failures_release_identity_and_lease(self):
        for failure in ('constructor', 'start', 'receipt', 'busy'):
            with self.subTest(failure=failure), isolated_app() as (app, _):
                from fls_manager.online_scripts import constants
                from fls_manager.data_access import PanelStorageBusy
                from fls_manager.routes.online_scripts import refresh
                with contextlib.ExitStack() as stack:
                    if failure == 'constructor':
                        stack.enter_context(patch.object(threading, 'Thread', side_effect=RuntimeError('constructor denied')))
                    elif failure == 'busy':
                        stack.enter_context(patch.object(refresh, 'start_refresh_thread', side_effect=PanelStorageBusy()))
                    else:
                        stack.enter_context(patch.object(threading.Thread, 'start', side_effect=RuntimeError('start denied')))
                    if failure == 'receipt':
                        stack.enter_context(patch.object(refresh, 'update_online_refresh_state', side_effect=OSError('receipt denied')))
                    result = app.test_client().post('/api/online-scripts/refresh', json={'request_id':self.request_id}, headers={'X-Token':TOKEN})
                self.assertEqual(result.status_code, 503 if failure == 'busy' else 500, result.json)
                self.assertFalse(constants.REFRESH_IDENTITIES.owners)
                directories.ScriptDirectoryJobTests().assert_restore_available()
                self.assertFalse(constants.get_online_refresh_state()['running'])

    def test_cache_changed_after_admission_is_not_fetched_or_overwritten(self):
        with isolated_app() as (app, _):
            from fls_manager.online_scripts import constants, source
            queued = []
            with patch.object(threading.Thread, 'start', autospec=True, side_effect=queued.append):
                result = app.test_client().post('/api/online-scripts/refresh', json={'request_id':self.request_id}, headers={'X-Token':TOKEN})
            self.assertEqual(result.status_code, 202)
            items = [{'id':'new', 'name':'New', 'type':'raw', 'link':'https://example.invalid/new.py'}]
            source.save_online_script_cache(items)
            with patch.object(source, 'requests') as network:
                queued[0].run()
                network.get.assert_not_called()
            self.assertEqual(source.load_online_script_cache()[0]['id'], 'new')
            self.assertEqual(constants.get_online_refresh_state()['status'], '失败')
            directories.ScriptDirectoryJobTests().assert_restore_available()
