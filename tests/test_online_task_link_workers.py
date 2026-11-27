import contextlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests import test_backup_activity_lease as activity_tests
from tests import test_backup_data_access as access_tests
from tests import test_online_install_workers as install_workers
from tests import test_script_directory_jobs as directory_tests
from tests.test_ui_route_components import TOKEN, isolated_app


WORKER = r'''
import json, os, sys, threading, time
from fls_manager.app import create_app
from fls_manager.paths import BASE_DIR
from fls_manager.online_scripts import source
from fls_manager.routes.online_scripts import task_link as routes
from fls_manager.online_scripts.source_coordination import TASK_LINK_IDENTITIES
from fls_manager.directory_jobs import directory_job
label, mode = sys.argv[1:]
started, release = (BASE_DIR / (label + suffix) for suffix in ('.started', '.release'))
def pause():
    started.touch()
    deadline = time.monotonic() + 45
    while not release.exists():
        if time.monotonic() > deadline: raise RuntimeError('task-link barrier expired')
        time.sleep(.01)
def fetch(*args, **kwargs):
    if mode == 'running': pause()
    return [{'name':label, 'cron':'', 'command':'echo ' + label}]
routes.fetch_task_link_tasks = fetch
if mode == 'prepared':
    original = routes.claim_task_link_request
    def claim(record):
        result = original(record)
        if result is None: pause()
        return result
    routes.claim_task_link_request = claim
elif mode == 'confirm':
    original = routes._confirm_task_link_locked
    def confirm(request_id):
        pause()
        return original(request_id)
    routes._confirm_task_link_locked = confirm
elif mode == 'publication':
    original = routes.merge_task_link_tasks
    def merge(*args, **kwargs):
        callback = kwargs['before_publish']
        def before_publish(*evidence):
            callback(*evidence)
            pause()
        kwargs['before_publish'] = before_publish
        return original(*args, **kwargs)
    routes.merge_task_link_tasks = merge
elif mode == 'repair':
    from fls_manager.online_scripts import constants
    original = constants._write_online_refresh_task_recoveries_file
    def write(*args):
        pause()
        return original(*args)
    constants._write_online_refresh_task_recoveries_file = write
app = create_app()
headers = {'X-Token': os.environ['FLS_TOKEN']}
pending, result, held = None, [], {}
def request(command):
    response = getattr(app.test_client(), command['method'])(
        command['path'], json=command.get('data'), headers=headers)
    return [response.status_code, response.get_json(silent=True)]
try:
    for line in sys.stdin:
        command = json.loads(line)
        method, data = command['method'], command.get('data') or {}
        if method == 'quit': break
        if method == 'begin':
            pending = threading.Thread(target=lambda command=dict(command), verb=data.pop('_method', 'post'): result.append(request(
                dict(command, method=verb))), daemon=True)
            pending.start()
            payload = [200, {'accepted':True}]
        elif method == 'result':
            pending.join(15)
            payload = result.pop() if result else [500, {'error':'request did not finish'}]
        elif method == 'claim':
            lease = directory_job()
            lease.__enter__()
            with source.task_link_receipt_transaction():
                owner = TASK_LINK_IDENTITIES.claim(data['request_id'])
            held[data['request_id']] = (owner, lease)
            payload = [200, {'claimed':True}]
        elif method == 'save':
            source.persist_task_link_request(data)
            payload = [200, {'saved':True}]
        elif method == 'recover':
            from fls_manager.online_scripts.constants import persist_online_refresh_task_link_recovery
            persist_online_refresh_task_link_recovery(data['request_id'], data['script_id'], proxy_id='', added=1, total=1)
            payload = [200, {'saved':True}]
        else:
            payload = request(command)
        print('FLS_RESULT ' + json.dumps(payload), flush=True)
finally:
    release.touch()
    if pending: pending.join(15)
    for owner, lease in held.values():
        owner.close()
        lease.__exit__(None, None, None)
'''


class OnlineTaskLinkWorkerTests(unittest.TestCase):
    request_id = 'a' * 32

    def worker(self, base, label, mode='normal'):
        return install_workers.OnlineInstallWorkerTests().worker(base, label, mode, worker_code=WORKER)

    def prepare(self):
        from fls_manager.online_scripts import source

        item = {'id':'demo', 'name':'Demo', 'type':'raw', 'link':'https://example.invalid/demo.py',
                'task_link':'https://example.invalid/tasks.json'}
        source.save_online_script_cache([item])
        return source.load_online_script_cache()[0]

    def record(self, item, *, request_id=None, state='unknown'):
        from fls_manager.online_scripts import source

        return {'request_id':request_id or self.request_id, 'script_id':'demo', 'proxy_id':'',
                'expected_version':source.online_task_link_version(item), 'state':state,
                'expected_tasks_fingerprint':source.online_script_task_fingerprint(item), 'updated_at':'old'}

    def wait_started(self, base, label):
        install_workers.OnlineInstallWorkerTests().wait_started(base, label)

    def test_all_task_link_and_source_editor_routes_guard_restore_before_access(self):
        for mode in ('legacy', 'native', 'journal', 'io'):
            with self.subTest(mode=mode), isolated_app() as (app, base), contextlib.ExitStack() as stack:
                from fls_manager.backup_commit import JOURNAL_NAME
                from fls_manager.routes.online_scripts import task_link, source_json

                self.prepare()
                if mode in ('legacy', 'native'):
                    stack.enter_context(activity_tests.BackupActivityLeaseTests().holder(mode))
                elif mode == 'io':
                    stack.enter_context(access_tests.BackupDataAccessTests().paused_process(
                        "with directory_access(exclusive=True):\n print('ready',flush=True)\n sys.stdin.readline()\n"))
                else:
                    directory = base / JOURNAL_NAME
                    directory.mkdir()
                    (directory / ('b' * 32 + '.json')).write_bytes(b'{broken')
                reads = [stack.enter_context(patch.object(task_link, name)) for name in (
                    'get_online_script', 'get_task_link_request', 'list_task_link_requests', 'fetch_task_link_tasks')]
                reads.append(stack.enter_context(patch.object(source_json, 'read_cache_text')))
                for prefix in ('', '/api'):
                    for action in ('task-link/demo', 'refresh-task-link/' + 'b' * 32 + '/task-link/demo',
                                   'task-link-confirm/' + self.request_id, 'task-link-archive/' + self.request_id):
                        response = app.test_client().post(prefix + '/online-scripts/' + action, headers={'X-Token':TOKEN})
                        self.assertEqual(response.status_code, 503, (mode, action))
                    for action in ('task-link-status/' + self.request_id, 'task-link-history', 'source'):
                        response = app.test_client().get(prefix + '/online-scripts/' + action, headers={'X-Token':TOKEN})
                        self.assertEqual(response.status_code, 503, (mode, action))
                for read in reads: read.assert_not_called()

    def test_live_requests_observed_across_workers_reject_replay_and_recovery(self):
        for mode in ('prepared', 'running', 'publication'):
            with self.subTest(mode=mode), isolated_app() as (_, base):
                self.prepare()
                with self.worker(base, 'owner', mode) as (_, first), self.worker(base, 'reader') as (_, second):
                    first('begin', '/api/online-scripts/task-link/demo', {'request_id':self.request_id})
                    self.wait_started(base, 'owner')
                    directory_tests.ScriptDirectoryJobTests().assert_restore_blocked()
                    code, state = second('get', '/api/online-scripts/task-link-status/' + self.request_id)
                    self.assertEqual(code, 200, state)
                    self.assertTrue(state['active'])
                    self.assertEqual(state['state'], 'prepared' if mode == 'prepared' else 'running')
                    code, history = second('get', '/api/online-scripts/task-link-history?recovery=1')
                    self.assertEqual(code, 200, history)
                    self.assertTrue(history['items'][0]['active'])
                    for action, data in (
                        ('task-link/demo', {'request_id':self.request_id}),
                        ('task-link/demo', {'request_id':self.request_id, 'proxy_id':'changed'}),
                        ('task-link-confirm/' + self.request_id, {}),
                        ('task-link-archive/' + self.request_id, {'acknowledge':'archive'}),
                    ):
                        code, result = second('post', '/api/online-scripts/' + action, data)
                        self.assertEqual(code, 409, result)
                    (base / 'owner.release').touch()
                    self.assertEqual(first('result')[0], 200)
                    code, state = second('get', '/api/online-scripts/task-link-status/' + self.request_id)
                    self.assertEqual(state['state'], 'completed', state)
                    self.assertFalse(state['active'])
                    directory_tests.ScriptDirectoryJobTests().assert_restore_available()

    def test_owner_crash_releases_identity_and_retains_original_unknown(self):
        for mode in ('prepared', 'running', 'publication'):
            with self.subTest(mode=mode), isolated_app() as (_, base):
                self.prepare()
                with self.worker(base, 'owner', mode) as (process, first), self.worker(base, 'reader') as (_, second):
                    first('begin', '/api/online-scripts/task-link/demo', {'request_id':self.request_id})
                    self.wait_started(base, 'owner')
                    process.kill()
                    process.wait(10)
                    code, state = second('get', '/api/online-scripts/task-link-status/' + self.request_id)
                    self.assertEqual(code, 200, state)
                    self.assertFalse(state['active'])
                    self.assertEqual(state['state'], 'failed' if mode == 'prepared' else 'unknown')
                    directory_tests.ScriptDirectoryJobTests().assert_restore_available()
                    if mode != 'prepared':
                        self.assertEqual(second('post', '/api/online-scripts/task-link/demo',
                                                {'request_id':self.request_id})[0], 409)
                        code, confirmed = second('post', '/api/online-scripts/task-link-confirm/' + self.request_id)
                        self.assertEqual(code, 200, confirmed)
                        self.assertEqual(confirmed['confirmation'], 'not_merged')

    def test_confirmation_is_claimed_and_second_worker_cannot_archive_or_retry(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts import source

            item = self.prepare()
            source.persist_task_link_request(self.record(item))
            with self.worker(base, 'owner', 'confirm') as (_, first), self.worker(base, 'reader') as (_, second):
                first('begin', '/api/online-scripts/task-link-confirm/' + self.request_id)
                self.wait_started(base, 'owner')
                code, state = second('get', '/api/online-scripts/task-link-status/' + self.request_id)
                self.assertEqual(code, 200, state)
                self.assertTrue(state['active'])
                for action in ('task-link-confirm/', 'task-link-archive/'):
                    self.assertEqual(second('post', '/api/online-scripts/' + action + self.request_id,
                                            {'acknowledge':'archive'})[0], 409)
                (base / 'owner.release').touch()
                self.assertEqual(first('result')[0], 200)
                code, replay = second('post', '/api/online-scripts/task-link-confirm/' + self.request_id)
                self.assertEqual(code, 200, replay)
                self.assertTrue(replay['replayed'])

    def test_missing_live_receipts_reserve_capacity_across_workers(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts import source

            item = self.prepare()
            records = {f'{index:032x}':source._task_link_request_snapshot(
                self.record(item, request_id=f'{index:032x}')) for index in range(199)}
            source.mutate_json(source.ONLINE_TASK_LINK_REQUESTS_FILE, {}, lambda _:records, strict=True)
            with self.worker(base, 'owner') as (_, first), self.worker(base, 'reader') as (_, second):
                first('claim', data={'request_id':self.request_id})
                code, missing = second('post', '/api/online-scripts/task-link/demo', {'request_id':self.request_id})
                self.assertEqual(code, 503, missing)
                self.assertTrue(missing['receipt_missing'])
                code, full = second('post', '/api/online-scripts/task-link/demo', {'request_id':'f' * 32})
                self.assertEqual(code, 409, full)
                self.assertTrue(full['history_full'])

    def test_parallel_workers_merge_receipts_and_cache_without_lost_updates(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts import source

            item = self.prepare()
            with self.worker(base, 'first') as (_, first), self.worker(base, 'second') as (_, second):
                def publish(rpc, start):
                    for index in range(start, start + 20):
                        code, result = rpc('save', data=self.record(item, request_id=f'{index:032x}', state='completed'))
                        self.assertEqual(code, 200, result)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(publish, rpc, start) for rpc, start in ((first, 1), (second, 21))]
                    for future in futures: future.result(30)
                records = json.loads(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_text(encoding='utf-8'))
                self.assertEqual(len(records), 40)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(rpc, 'post', '/api/online-scripts/task-link/demo', {'request_id':request_id})
                               for rpc, request_id in ((first, 'a' * 32), (second, 'b' * 32))]
                    for future in futures: self.assertEqual(future.result(15)[0], 200)
                self.assertEqual({task['name'] for task in source.load_online_script_cache()[0]['task_cron']}, {'first', 'second'})

    def test_initial_receipt_write_failure_releases_identity_before_and_after_replace(self):
        for after in (False, True):
            with self.subTest(after=after), isolated_app() as (app, _):
                from fls_manager.online_scripts import source
                from fls_manager.routes.online_scripts import task_link

                self.prepare()
                original = source.mutate_json
                def fail(*args, **kwargs):
                    if after: original(*args, **kwargs)
                    else:
                        # Fail after the mutator has claimed the identity, before publication.
                        args[2]({})
                    raise OSError('receipt acknowledgement failed')
                with patch.object(source, 'mutate_json', side_effect=fail), patch.object(task_link, 'fetch_task_link_tasks') as fetch:
                    response = app.test_client().post('/api/online-scripts/task-link/demo',
                        json={'request_id':self.request_id}, headers={'X-Token':TOKEN})
                    self.assertEqual(response.status_code, 500)
                    fetch.assert_not_called()
                with source.task_link_receipt_transaction():
                    self.assertNotIn(self.request_id, source.task_link_active_ids())
                directory_tests.ScriptDirectoryJobTests().assert_restore_available()

    def test_response_close_and_stream_limit_hold_restore_lease_and_preserve_cache(self):
        for phase in ('success', 'limit', 'read_error', 'close_error'):
            with self.subTest(phase=phase), isolated_app() as (app, _):
                from fls_manager.online_scripts import source

                self.prepare()
                before = source.ONLINE_SCRIPT_CACHE_FILE.read_bytes()
                response = Mock(status_code=200, headers={}, encoding='utf-8')
                def chunks(**kwargs):
                    directory_tests.ScriptDirectoryJobTests().assert_restore_blocked()
                    if phase == 'read_error': raise OSError('response read failed')
                    yield b'x' * (4 * 1024 * 1024 + 1) if phase == 'limit' else b'[{"name":"new","command":"echo new"}]'
                def close():
                    directory_tests.ScriptDirectoryJobTests().assert_restore_blocked()
                    if phase == 'close_error': raise OSError('response close failed')
                response.iter_content.side_effect = chunks
                response.close.side_effect = close
                with patch.object(source, 'requests', SimpleNamespace(get=Mock(return_value=response))) as requests:
                    result = app.test_client().post('/api/online-scripts/task-link/demo',
                        json={'request_id':self.request_id}, headers={'X-Token':TOKEN})
                    self.assertEqual(result.status_code, 200 if phase == 'success' else 502, result.json)
                    self.assertTrue(requests.get.call_args.kwargs['stream'])
                response.close.assert_called_once()
                if phase != 'success': self.assertEqual(source.ONLINE_SCRIPT_CACHE_FILE.read_bytes(), before)
                directory_tests.ScriptDirectoryJobTests().assert_restore_available()

    def test_refresh_does_not_overwrite_cache_edited_during_fetch(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts import source
            from fls_manager.online_scripts.constants import update_online_refresh_state, get_online_refresh_state

            item = self.prepare()
            def fetch(**kwargs):
                source.save_online_script_cache([dict(item, name='later edit')])
                return [dict(item, name='old remote data')]
            update_online_refresh_state(id='b' * 32, request_id=self.request_id, running=True, status='运行中')
            with patch.object(source, 'fetch_online_scripts', side_effect=fetch):
                source.refresh_worker('b' * 32)
            self.assertEqual(source.load_online_script_cache()[0]['name'], 'later edit')
            self.assertFalse(get_online_refresh_state()['cache_published'])

    def test_failed_request_cannot_be_rebound_to_another_origin_at_claim(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts import source

            item = self.prepare()
            record = self.record(item, state='failed')
            record['origin_refresh_request_id'] = 'b' * 32
            source.persist_task_link_request(record)
            before = source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes()
            conflict = source.claim_task_link_request(dict(record, state='prepared', origin_refresh_request_id='c' * 32))
            self.assertEqual(conflict['origin_refresh_request_id'], 'b' * 32)
            self.assertEqual(source.ONLINE_TASK_LINK_REQUESTS_FILE.read_bytes(), before)
            self.assertFalse(source.TASK_LINK_IDENTITIES.owners)

    def test_source_editor_revision_is_checked_after_another_worker_publishes(self):
        with isolated_app() as (_, base):
            self.prepare()
            with self.worker(base, 'owner', 'publication') as (_, first), self.worker(base, 'editor') as (_, second):
                code, draft = second('get', '/api/online-scripts/source')
                self.assertEqual(code, 200, draft)
                first('begin', '/api/online-scripts/task-link/demo', {'request_id':self.request_id})
                self.wait_started(base, 'owner')
                with ThreadPoolExecutor(max_workers=1) as pool:
                    save = pool.submit(second, 'put', '/api/online-scripts/source', {
                        'json_text':draft['json_text'], 'cache_revision':draft['cache_revision']})
                    (base / 'owner.release').touch()
                    self.assertEqual(first('result')[0], 200)
                    code, conflict = save.result(15)
                self.assertEqual(code, 409, conflict)
                self.assertEqual(conflict['code'], 'revision_conflict')
                self.assertIn('owner', conflict['current_json_text'])

    def test_failure_query_repairs_copies_without_overwriting_other_worker_recovery(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts import constants

            self.prepare()
            refresh_id = 'c' * 32
            failures = [{'script_id':script_id, 'name':script_id, 'task_link':'https://example.invalid/tasks.json',
                         'proxy_id':'', 'error':'original failure'} for script_id in ('demo', 'other')]
            constants.update_online_refresh_state(id='fixture', request_id=refresh_id, status='部分完成',
                running=False, cache_published=True, task_link_failures=failures)
            constants.persist_online_refresh_task_link_recovery(refresh_id, 'demo', proxy_id='', added=1, total=1)
            constants._online_refresh_task_recoveries_path(refresh_id).write_bytes(b'{broken')
            with self.worker(base, 'owner', 'repair') as (_, first), self.worker(base, 'writer') as (_, second):
                first('begin', '/api/online-scripts/refresh-task-link-failures/' + refresh_id, {'_method':'get'})
                self.wait_started(base, 'owner')
                with ThreadPoolExecutor(max_workers=1) as pool:
                    recovery = pool.submit(second, 'recover', data={'request_id':refresh_id, 'script_id':'other'})
                    (base / 'owner.release').touch()
                    self.assertEqual(first('result')[0], 200)
                    self.assertEqual(recovery.result(15)[0], 200)
                for script_id in ('demo', 'other'):
                    self.assertEqual(constants.get_online_refresh_task_link_recovery(refresh_id, script_id)['state'], 'completed')
