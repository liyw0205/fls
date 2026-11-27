import contextlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import test_backup_activity_lease as activity_tests
from tests import test_backup_data_access as access_tests
from tests import test_online_install_workers as install_workers
from tests import test_script_directory_jobs as directory_tests
from tests.test_ui_route_components import TOKEN, isolated_app


WORKER = r'''
import json, os, sys, threading, time
from fls_manager.app import create_app
from fls_manager.paths import BASE_DIR
from fls_manager.online_scripts import constants, tasks
from fls_manager.routes.online_scripts import install_select as routes
from fls_manager.online_scripts.task_import_coordination import TASK_IMPORT_IDENTITIES
from fls_manager.directory_jobs import directory_job
label, mode = sys.argv[1:]
started, release = (BASE_DIR / (label + suffix) for suffix in ('.started', '.release'))
def pause():
    started.touch()
    deadline = time.monotonic() + 45
    while not release.exists():
        if time.monotonic() > deadline: raise RuntimeError('import barrier expired')
        time.sleep(.01)
if mode == 'prepared':
    original = routes.claim_online_task_import_request
    def claim(record):
        result = original(record)
        if result is None: pause()
        return result
    routes.claim_online_task_import_request = claim
elif mode in ('running', 'publication'):
    original = routes.import_task_if_needed
    def importer(*args, **kwargs):
        if mode == 'running': pause()
        else:
            publish = kwargs['before_publish']
            def before_publish(evidence):
                publish(evidence)
                pause()
            kwargs['before_publish'] = before_publish
        return original(*args, **kwargs)
    routes.import_task_if_needed = importer
elif mode == 'reload':
    routes.reload_scheduler = pause
elif mode == 'confirm':
    original = routes.task_import_publication_state
    def confirmation(record):
        pause()
        return original(record)
    routes.task_import_publication_state = confirmation
app = create_app()
tasks.reload_scheduler = lambda: None
headers = {'X-Token': os.environ['FLS_TOKEN']}
pending = None
result = []
held = {}
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
            pending = threading.Thread(target=lambda command=dict(command): result.append(request(
                dict(command, method='post'))), daemon=True)
            pending.start()
            payload = [200, {'accepted':True}]
        elif method == 'result':
            pending.join(15)
            payload = result.pop() if result else [500, {'error':'request did not finish'}]
        elif method == 'claim':
            lease = directory_job()
            lease.__enter__()
            with constants.task_import_receipt_transaction():
                owner = TASK_IMPORT_IDENTITIES.claim(data['request_id'])
            held[data['request_id']] = (owner, lease)
            payload = [200, {'claimed':True}]
        elif method == 'save':
            constants.persist_online_task_import_request(data)
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


class OnlineTaskImportWorkerTests(unittest.TestCase):
    request_id = 'a' * 32

    def worker(self, base, label, mode='normal'):
        return install_workers.OnlineInstallWorkerTests().worker(
            base, label, mode, worker_code=WORKER)

    def prepare(self):
        from fls_manager.online_scripts.source import save_online_script_cache

        save_online_script_cache([{
            'id':'demo', 'name':'Demo', 'type':'raw', 'link':'https://example.invalid/demo.py',
            'task_cron':[{'name':'Task', 'command':'echo fixture', 'cron':''}],
        }])

    def record(self, state='unknown', request_id=None):
        from fls_manager.online_scripts import tasks
        from fls_manager.routes.online_scripts.install_select import get_online_script

        return {
            'request_id':request_id or self.request_id, 'script_id':'demo',
            'expected_version':tasks.online_task_import_version(get_online_script('demo')),
            'selected_task_indexes':['1'], 'enable_task':False, 'state':state,
            'message':'original diagnosis', 'scheduler_reload_error':'', 'updated_at':'old',
        }

    def start(self, rpc, action='import-tasks/demo'):
        return rpc('begin', '/api/online-scripts/' + action, {
            'request_id':self.request_id, 'task_indexes':['1'],
        })

    def wait_started(self, base, label):
        install_workers.OnlineInstallWorkerTests().wait_started(base, label)

    def assert_blocked(self):
        directory_tests.ScriptDirectoryJobTests().assert_restore_blocked()

    def assert_available(self):
        directory_tests.ScriptDirectoryJobTests().assert_restore_available()

    def test_every_import_route_rejects_restore_pending_journal_and_io_before_access(self):
        for mode in ('legacy', 'native', 'journal', 'io'):
            with self.subTest(mode=mode), isolated_app() as (app, base), contextlib.ExitStack() as stack:
                from fls_manager.backup_commit import JOURNAL_NAME
                from fls_manager.online_scripts import constants
                from fls_manager.routes.online_scripts import catalog_api
                from fls_manager.routes.online_scripts import install_select as routes

                self.prepare()
                if mode in ('legacy', 'native'):
                    stack.enter_context(activity_tests.BackupActivityLeaseTests().holder(mode))
                elif mode == 'io':
                    stack.enter_context(access_tests.BackupDataAccessTests().paused_process(
                        "with directory_access(exclusive=True):\n print('ready',flush=True)\n sys.stdin.readline()\n"))
                else:
                    directory = base / JOURNAL_NAME
                    directory.mkdir()
                    journal = directory / ('b' * 32 + '.json')
                    journal.write_bytes(b'{broken')
                reads = [stack.enter_context(patch.object(routes, name)) for name in (
                    'get_online_script', 'get_online_task_import_request',
                    'list_online_task_import_requests', 'import_task_if_needed', 'reload_scheduler')]
                reads.extend(stack.enter_context(patch.object(catalog_api, name)) for name in (
                    'get_online_script', 'load_proxies', 'validate_online_task_import'))
                for prefix in ('/api', ''):
                    for action in ('import-tasks/demo', 'import-tasks-confirm/' + self.request_id,
                                   'import-tasks-reload/' + self.request_id, 'import-tasks-archive/' + self.request_id):
                        response = app.test_client().post(prefix + '/online-scripts/' + action, headers={'X-Token':TOKEN})
                        self.assertEqual(response.status_code, 503, (mode, action))
                    for action in ('import-tasks-status/' + self.request_id, 'import-tasks-history'):
                        response = app.test_client().get(prefix + '/online-scripts/' + action, headers={'X-Token':TOKEN})
                        self.assertEqual(response.status_code, 503, (mode, action))
                response = app.test_client().get('/online-scripts/install-select/demo', headers={'X-Token':TOKEN})
                self.assertEqual(response.status_code, 503)
                response = app.test_client().get('/api/online-scripts/install-options/demo', headers={'X-Token':TOKEN})
                self.assertEqual(response.status_code, 503)
                for read in reads: read.assert_not_called()
                self.assertFalse(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.exists())
                self.assertEqual(constants.ONLINE_TASK_IMPORT_ACTIVE, set())
                if mode == 'journal': self.assertEqual(journal.read_bytes(), b'{broken')

    def test_other_worker_observes_live_prepared_and_running_import_and_rejects_recovery(self):
        for mode in ('prepared', 'running', 'publication'):
            with self.subTest(mode=mode), isolated_app() as (_, base):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts import constants

                self.prepare()
                with self.worker(base, 'owner', mode) as (_, first), self.worker(base, 'reader') as (_, second):
                    self.start(first)
                    self.wait_started(base, 'owner')
                    self.assert_blocked()
                    before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
                    code, state = second('get', '/api/online-scripts/import-tasks-status/' + self.request_id)
                    self.assertEqual(code, 200, state)
                    self.assertTrue(state['active'])
                    self.assertEqual(state['state'], 'prepared' if mode == 'prepared' else 'running')
                    code, history = second('get', '/api/online-scripts/import-tasks-history?recovery=1')
                    self.assertEqual(code, 200)
                    self.assertTrue(history['items'][0]['active'])
                    code, replay = second('post', '/api/online-scripts/import-tasks/demo', {
                        'request_id':self.request_id, 'task_indexes':['1']})
                    self.assertEqual(code, 409, replay)
                    code, conflict = second('post', '/api/online-scripts/import-tasks/demo', {
                        'request_id':self.request_id, 'task_indexes':['1'], 'enable_task':True})
                    self.assertEqual(code, 409, conflict)
                    for action in ('confirm', 'reload', 'archive'):
                        code, payload = second('post', '/api/online-scripts/import-tasks-' + action + '/' + self.request_id,
                                               {'acknowledge':'archive'})
                        self.assertEqual(code, 409, payload)
                    self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                    self.assertEqual(load_tasks(), [])
                    (base / 'owner.release').touch()
                    self.assertEqual(first('result')[0], 200)
                    code, replay = second('post', '/api/online-scripts/import-tasks/demo', {
                        'request_id':self.request_id, 'task_indexes':['1']})
                    self.assertEqual(code, 200, replay)
                    self.assertTrue(replay['replayed'])
                    self.assertEqual(len(load_tasks()), 1)
                self.assert_available()

    def test_owner_hard_exit_recovers_original_prepared_or_unknown_without_duplicate_publication(self):
        for mode in ('prepared', 'publication'):
            with self.subTest(mode=mode), isolated_app() as (_, base):
                from fls_manager.models import load_tasks

                self.prepare()
                with self.worker(base, 'owner', mode) as (owner, first), self.worker(base, 'reader') as (_, second):
                    self.start(first)
                    self.wait_started(base, 'owner')
                    owner.kill()
                    owner.wait(10)
                    code, state = second('get', '/api/online-scripts/import-tasks-status/' + self.request_id)
                    self.assertEqual(code, 200, state)
                    self.assertFalse(state['active'])
                    self.assertEqual(state['state'], 'failed' if mode == 'prepared' else 'unknown')
                    self.assertEqual(load_tasks(), [])
                    if mode == 'publication':
                        code, payload = second('post', '/api/online-scripts/import-tasks/demo', {
                            'request_id':self.request_id, 'task_indexes':['1']})
                        self.assertEqual(code, 409, payload)
                        code, payload = second('post', '/api/online-scripts/import-tasks-confirm/' + self.request_id)
                        self.assertEqual(code, 200, payload)
                        self.assertEqual(payload['confirmation'], 'not_published')
                    code, payload = second('post', '/api/online-scripts/import-tasks/demo', {
                        'request_id':self.request_id, 'task_indexes':['1']})
                    self.assertEqual(code, 200, payload)
                    self.assertEqual(len(load_tasks()), 1)
                self.assert_available()

    def test_confirmation_and_scheduler_recovery_claim_block_cross_worker_actions_until_release(self):
        for mode in ('reload', 'confirm'):
            with self.subTest(mode=mode), isolated_app() as (_, base):
                from fls_manager.online_scripts import constants, tasks

                self.prepare()
                record = self.record('scheduler_pending' if mode == 'reload' else 'unknown')
                if mode == 'confirm':
                    record.update(publication_tracking=True, task_baseline_captured=True,
                                  task_baseline=tasks.task_import_file_signature())
                constants.persist_online_task_import_request(record)
                with self.worker(base, 'owner', mode) as (_, first), self.worker(base, 'reader') as (_, second):
                    self.start(first, 'import-tasks-' + mode + '/' + self.request_id)
                    self.wait_started(base, 'owner')
                    self.assert_blocked()
                    code, state = second('get', '/api/online-scripts/import-tasks-status/' + self.request_id)
                    self.assertEqual(code, 200, state)
                    self.assertTrue(state['active'])
                    for action in ('confirm', 'reload', 'archive'):
                        code, payload = second('post', '/api/online-scripts/import-tasks-' + action + '/' + self.request_id,
                                               {'acknowledge':'archive'})
                        self.assertEqual(code, 409, payload)
                    (base / 'owner.release').touch()
                    code, result = first('result')
                    self.assertEqual(code, 200, result)
                    code, state = second('get', '/api/online-scripts/import-tasks-status/' + self.request_id)
                    self.assertEqual(code, 200, state)
                    self.assertFalse(state['active'])
                    self.assertEqual(state['state'], 'completed' if mode == 'reload' else 'failed')
                self.assert_available()

    def test_missing_receipt_in_another_worker_reserves_capacity_and_blocks_reclaim(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts import constants

            self.prepare()
            for index in range(199):
                constants.persist_online_task_import_request(self.record('failed', f'{index:032x}'))
            before = constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes()
            with self.worker(base, 'owner') as (_, first), self.worker(base, 'reader') as (_, second):
                self.assertEqual(first('claim', data={'request_id':self.request_id})[0], 200)
                code, payload = second('get', '/api/online-scripts/import-tasks-status/' + self.request_id)
                self.assertEqual(code, 503, payload)
                for request_id in (self.request_id, 'f' * 32):
                    code, payload = second('post', '/api/online-scripts/import-tasks/demo', {
                        'request_id':request_id, 'task_indexes':['1']})
                    self.assertEqual(code, 503 if request_id == self.request_id else 409, payload)
                self.assertEqual(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes(), before)
                first('save', data=self.record('running'))
                code, payload = second('get', '/api/online-scripts/import-tasks-status/' + self.request_id)
                self.assertEqual(code, 200, payload)
                self.assertTrue(payload['active'])
                self.assertEqual(len(json.loads(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes())), 200)
            self.assert_available()

    def test_initial_receipt_has_identity_and_save_failure_releases_it_without_task_mutation(self):
        for after in (False, True):
            with self.subTest(after=after), isolated_app() as (app, _):
                from fls_manager.models import load_tasks
                from fls_manager.online_scripts import constants

                self.prepare()
                original = constants.mutate_json
                observed = []
                def fail(path, default, update, **kwargs):
                    if path != constants.ONLINE_TASK_IMPORT_REQUESTS_FILE:
                        return original(path, default, update, **kwargs)
                    def inspect(data):
                        value = update(data)
                        observed.append(self.request_id in constants.task_import_active_ids())
                        self.assert_blocked()
                        return value
                    if after:
                        original(path, default, inspect, **kwargs)
                    else:
                        inspect({})
                    raise OSError('injected receipt failure')
                with patch.object(constants, 'mutate_json', side_effect=fail):
                    response = app.test_client().post('/api/online-scripts/import-tasks/demo', json={
                        'request_id':self.request_id, 'task_indexes':['1']}, headers={'X-Token':TOKEN})
                self.assertEqual(response.status_code, 500, response.json)
                self.assertEqual(observed, [True])
                self.assertEqual(load_tasks(), [])
                self.assertEqual(constants.TASK_IMPORT_IDENTITIES.owners, {})
                self.assert_available()

    def test_independent_workers_merge_receipts_without_lost_updates(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts import constants

            self.prepare()
            records = [[self.record('failed', f'{group * 20 + index:032x}') for index in range(20)]
                       for group in range(2)]
            with self.worker(base, 'first') as (_, first), self.worker(base, 'second') as (_, second):
                def save(rpc, items):
                    for item in items:
                        self.assertEqual(rpc('save', data=item)[0], 200)
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(save, rpc, items) for rpc, items in zip((first, second), records)]
                    for future in futures: future.result(timeout=20)
            saved = json.loads(constants.ONLINE_TASK_IMPORT_REQUESTS_FILE.read_bytes())
            self.assertEqual(set(saved), {item['request_id'] for items in records for item in items})

    def test_overlapping_imports_preserve_both_task_files_and_unrelated_config_access(self):
        with isolated_app() as (_, base):
            from fls_manager.models import load_tasks
            from fls_manager.online_scripts import constants, source

            self.prepare()
            items = source.load_online_script_cache()
            items.append(dict(items[0], id='other', name='Other'))
            source.save_online_script_cache(items)
            with self.worker(base, 'first', 'publication') as (_, first), self.worker(base, 'second') as (_, second):
                self.start(first)
                self.wait_started(base, 'first')
                second('begin', '/api/online-scripts/import-tasks/other', {
                    'request_id':'b' * 32, 'task_indexes':['1']})
                constants.write_json(base / 'data' / 'unrelated.json', {'fixture':True})
                (base / 'first.release').touch()
                self.assertEqual(first('result')[0], 200)
                code, result = second('result')
                self.assertEqual(code, 200, result)
                self.assertEqual({item['online_script_id'] for item in load_tasks()}, {'demo', 'other'})
                self.assertEqual(json.loads((base / 'data' / 'unrelated.json').read_bytes()), {'fixture':True})

    def test_missing_task_file_confirmation_keeps_retry_bound_to_absence(self):
        with isolated_app() as (app, _):
            from fls_manager.models import TASK_FILE, save_tasks
            from fls_manager.online_scripts import constants, tasks

            self.prepare()
            record = self.record()
            record.update(publication_tracking=True, task_baseline_captured=True,
                          task_baseline=tasks.task_import_file_signature())
            self.assertFalse(TASK_FILE.exists())
            constants.persist_online_task_import_request(record)
            client = app.test_client()
            response = client.post('/api/online-scripts/import-tasks-confirm/' + self.request_id, headers={'X-Token':TOKEN})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['confirmation'], 'not_published')
            self.assertTrue(constants.get_online_task_import_request(self.request_id)['retry_allowed'])
            save_tasks([])
            before = TASK_FILE.read_bytes()
            response = client.post('/api/online-scripts/import-tasks/demo', json={
                'request_id':self.request_id, 'task_indexes':['1']}, headers={'X-Token':TOKEN})
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(TASK_FILE.read_bytes(), before)

    def test_missing_task_file_created_after_retry_claim_prevents_publication(self):
        with isolated_app() as (app, base):
            from fls_manager.models import TASK_FILE, save_tasks
            from fls_manager.online_scripts import constants, tasks

            self.prepare()
            record = self.record()
            record.update(publication_tracking=True, task_baseline_captured=True,
                          task_baseline=tasks.task_import_file_signature())
            self.assertFalse(TASK_FILE.exists())
            constants.persist_online_task_import_request(record)
            response = app.test_client().post(
                '/api/online-scripts/import-tasks-confirm/' + self.request_id, headers={'X-Token':TOKEN})
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['confirmation'], 'not_published')
            with self.worker(base, 'owner', 'prepared') as (_, first):
                self.start(first)
                self.wait_started(base, 'owner')
                claimed = constants.get_online_task_import_request(self.request_id)
                self.assertTrue(claimed['retry_source_captured'])
                self.assertEqual(claimed['retry_source'], '')
                save_tasks([])
                before = TASK_FILE.read_bytes()
                (base / 'owner.release').touch()
                code, payload = first('result')
                self.assertEqual(code, 409, payload)
                self.assertEqual(TASK_FILE.read_bytes(), before)
                state = constants.get_online_task_import_request(self.request_id)
                self.assertEqual(state['state'], 'unknown')
                self.assertFalse(state['retry_allowed'])
