import contextlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_backup_dependency_process import process_exited
from tests import test_online_install_scheduler_recovery as scheduler_tests
from tests.test_ui_route_components import TOKEN, isolated_app


WORKER = r'''
import json, os, pathlib, sys, threading, time
from types import SimpleNamespace
from fls_manager.app import create_app
from fls_manager.paths import BASE_DIR
from fls_manager.online_scripts import constants, install
from fls_manager.routes.online_scripts import install as routes
label, mode = sys.argv[1:]
started, release = (BASE_DIR / (label + suffix) for suffix in ('.started', '.release'))
def pause():
    started.touch()
    deadline = time.monotonic() + 45
    while not release.exists():
        if time.monotonic() > deadline: raise RuntimeError('worker barrier expired')
        time.sleep(.01)
class Response:
    headers = {'Content-Length':'8'}
    def raise_for_status(self): pass
    def iter_content(self, chunk_size):
        yield b'body'
        if mode == 'download': pause()
        yield b'body'
    def close(self): pass
install.requests = SimpleNamespace(get=lambda *a, **k: Response())
if mode in ('command', 'stop-failure'):
    original_command = install.run_logged_command
    def command(*args, **kwargs):
        marker = BASE_DIR / (label + '.children.json')
        code = (
            'import json,os,pathlib,subprocess,sys,time\n'
            'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
            f'pathlib.Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n'
            f'pathlib.Path({str(started)!r}).touch()\n'
            f'while not pathlib.Path({str(release)!r}).exists():time.sleep(.01)\n'
        )
        return original_command([sys.executable, '-u', '-c', code], **kwargs)
    install.run_logged_command = command
    if mode == 'stop-failure':
        terminate = install.terminate_install_process
        attempts = []
        def stop(proc):
            attempts.append(1)
            return (False, 'injected stop failure') if len(attempts) == 1 else terminate(proc)
        install.terminate_install_process = stop
if mode == 'tasks':
    original_import = routes.import_task_if_needed
    def importer(*args, **kwargs):
        pause()
        return original_import(*args, **kwargs)
    routes.import_task_if_needed = importer
if mode == 'reload':
    def reload(): pause()
    routes.reload_scheduler = reload
if mode == 'confirm':
    original_confirmation = routes.task_import_publication_state
    def confirmation(*args, **kwargs):
        pause()
        return original_confirmation(*args, **kwargs)
    routes.task_import_publication_state = confirmation
app = create_app()
client = app.test_client()
headers = {'X-Token':os.environ['FLS_TOKEN']}
try:
    for line in sys.stdin:
        command = json.loads(line)
        method, path, body = command['method'], command.get('path'), command.get('data')
        if method == 'quit': break
        if method == 'claim':
            with constants.ONLINE_INSTALL_LOCK:
                owner = constants.ONLINE_INSTALL_LOCK.claim(body['id'])
            payload = [200, {'id':body['id']}]
        elif method == 'cache':
            info = constants.get_online_install_job(body['id'])
            constants.ONLINE_INSTALL_RUNNING[body['id']] = dict(info)
            if body.get('diagnostic'):
                constants.ONLINE_INSTALL_RUNNING[body['id']]['recovery_persistence_error'] = 'stale write failure'
            payload = [200, {'status':info['status']}]
        elif method == 'progress':
            with constants.ONLINE_INSTALL_LOCK:
                info = constants.ONLINE_INSTALL_RUNNING[body['id']]
                info.update(status='运行中', updated_at='progress fixture')
                constants.persist_online_install(info)
                payload = [200, {'status':info['status'], 'stop_requested':info.get('stop_requested')}]
        else:
            response = getattr(client, method)(path, json=body, headers=headers)
            payload = [response.status_code, response.get_json(silent=True)]
        print('FLS_RESULT ' + json.dumps(payload), flush=True)
finally:
    release.touch()
    for worker in threading.enumerate():
        if worker.name.startswith('fls-online-install-'): worker.join(12)
'''


class OnlineInstallWorkerTests(unittest.TestCase):
    @contextlib.contextmanager
    def worker(self, base, label, mode='download', *, worker_code=WORKER):
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [sys.executable, '-u', '-c', worker_code, label, mode], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8',
                env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            )
            results = queue.Queue()

            def read():
                for line in process.stdout:
                    if line.startswith('FLS_RESULT '):
                        results.put(json.loads(line[len('FLS_RESULT '):]))

            reader = threading.Thread(target=read, daemon=True)
            reader.start()

            def rpc(method, path=None, data=None):
                process.stdin.write(json.dumps({'method':method, 'path':path, 'data':data}) + '\n')
                process.stdin.flush()
                try:
                    return results.get(timeout=15)
                except queue.Empty as exc:
                    errors.seek(0)
                    raise AssertionError(errors.read().decode('utf-8', errors='replace')) from exc

            try:
                self.assertEqual(rpc('get', '/api/online-scripts/install-history')[0], 200)
                yield process, rpc
            except BaseException as exc:
                errors.seek(0)
                raise AssertionError(str(exc) + '\n' + errors.read().decode('utf-8', errors='replace')) from exc
            finally:
                (base / (label + '.release')).touch()
                if process.poll() is None:
                    process.stdin.write('{"method":"quit"}\n')
                    process.stdin.flush()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                process.wait(timeout=10)
                reader.join(5)
                process.stdin.close()
                process.stdout.close()
                self.assertFalse(reader.is_alive())

    def prepare(self, command=False):
        from fls_manager.online_scripts.source import save_online_script_cache

        save_online_script_cache([
            {'id':key, 'name':key, 'type':'raw', 'link':'https://example.invalid/' + key + '.py',
             'link_name':key + '.py', 'install':'fixture command' if command else ''}
            for key in ('demo', 'other')
        ])

    def wait_started(self, base, label):
        deadline = time.monotonic() + 15
        while not (base / (label + '.started')).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((base / (label + '.started')).exists())

    def state(self, rpc, install_id, expected=None):
        deadline = time.monotonic() + 15
        while True:
            code, result = rpc('get', '/api/online-scripts/log/' + install_id + '?state_only=1')
            self.assertEqual(code, 200, result)
            if expected is None or result['status'] in expected:
                return result
            if time.monotonic() > deadline:
                self.fail(str(result))
            time.sleep(.02)

    def submit(self, rpc, request_id, script='demo', **options):
        return rpc('post', '/api/online-scripts/install/' + script,
                   {'request_id':request_id, 'force':True, **options})

    def assert_tree_exited(self, base, label):
        children = json.loads((base / (label + '.children.json')).read_text(encoding='utf-8'))
        deadline = time.monotonic() + 15
        while not all(process_exited(pid) for pid in children) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(all(process_exited(pid) for pid in children), children)

    def test_other_worker_observes_replays_conflicts_and_cancels_original_download(self):
        with isolated_app() as (_, base):
            self.prepare()
            with self.worker(base, 'a') as (_, owner), self.worker(base, 'b') as (_, observer):
                install_id = 'a' * 32
                self.assertEqual(self.submit(owner, install_id)[0], 200)
                self.wait_started(base, 'a')
                for _ in range(2):
                    self.assertTrue(self.state(observer, install_id)['running'])
                replay = self.submit(observer, install_id)
                self.assertEqual(replay[0], 200, replay)
                self.assertTrue(replay[1]['replayed'])
                self.assertEqual(self.submit(observer, install_id, force=False)[0], 409)
                self.assertEqual(self.submit(observer, 'b' * 32)[0], 409)
                stopped = observer('post', '/api/online-scripts/install-stop/' + install_id)
                self.assertEqual(stopped[0], 200, stopped)
                self.assertTrue(stopped[1]['running'])
                self.assertEqual(self.state(observer, install_id)['status'], '停止中')
                progress = owner('progress', data={'id':install_id})
                self.assertEqual(progress[1], {'status':'停止中', 'stop_requested':True})
                (base / 'a.release').touch()
                self.state(observer, install_id, {'已停止'})
                self.assertFalse((base / 'scripts' / 'demo.py').exists())
                self.assertFalse((base / 'b.started').exists())

    def test_concurrent_same_request_starts_once_and_distinct_progress_does_not_disappear(self):
        with isolated_app() as (_, base):
            self.prepare()
            with self.worker(base, 'a') as (_, first), self.worker(base, 'b') as (_, second):
                barrier = threading.Barrier(2)
                responses, errors = [], []

                def submit(rpc):
                    try:
                        barrier.wait(5)
                        responses.append(self.submit(rpc, 'a' * 32))
                    except Exception as exc:
                        errors.append(exc)

                workers = [threading.Thread(target=submit, args=(rpc,)) for rpc in (first, second)]
                for worker in workers: worker.start()
                for worker in workers: worker.join(15)
                self.assertFalse(any(worker.is_alive() for worker in workers))
                self.assertEqual(errors, [])
                self.assertEqual([reply[0] for reply in responses], [200, 200], responses)
                self.assertEqual(sum(bool(reply[1].get('replayed')) for reply in responses), 1, responses)
                deadline = time.monotonic() + 10
                while not any((base / (label + '.started')).exists() for label in ('a', 'b')) and time.monotonic() < deadline:
                    time.sleep(.01)
                active = [label for label in ('a', 'b') if (base / (label + '.started')).exists()]
                self.assertEqual(len(active), 1)
                other = second if active == ['a'] else first
                self.assertEqual(self.submit(other, 'b' * 32, script='other')[0], 200)
                self.wait_started(base, 'b' if active == ['a'] else 'a')
                for rpc in (first, second):
                    history = rpc('get', '/api/online-scripts/install-history')
                    self.assertEqual(history[0], 200, history)
                    self.assertEqual({item['id'] for item in history[1]['items']}, {'a' * 32, 'b' * 32})
                    self.assertTrue(all(item['running'] for item in history[1]['items']))
                for label in ('a', 'b'): (base / (label + '.release')).touch()
                for install_id in ('a' * 32, 'b' * 32): self.state(first, install_id, {'已完成'})

    def test_remote_stop_failure_can_retry_and_reaps_real_command_tree(self):
        with isolated_app() as (_, base):
            self.prepare(command=True)
            with self.worker(base, 'a', 'stop-failure') as (_, owner), self.worker(base, 'b') as (_, observer):
                install_id = 'a' * 32
                self.assertEqual(self.submit(owner, install_id)[0], 200)
                self.wait_started(base, 'a')
                self.assertEqual(observer('post', '/api/online-scripts/install-stop/' + install_id)[0], 200)
                failed = self.state(observer, install_id, {'停止失败'})
                self.assertTrue(failed['running'])
                self.assertEqual(self.submit(observer, 'b' * 32)[0], 409)
                self.assertEqual(observer('post', '/api/online-scripts/install-stop/' + install_id)[0], 200)
                self.state(observer, install_id, {'已停止'})
                self.assert_tree_exited(base, 'a')

    def test_owner_crash_preserves_supervisor_identity_until_command_tree_exits(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_LOCK

            self.prepare(command=True)
            with self.worker(base, 'a', 'command') as (process, owner), self.worker(base, 'b') as (_, observer):
                install_id = 'a' * 32
                self.assertEqual(self.submit(owner, install_id)[0], 200)
                self.wait_started(base, 'a')
                process.kill()
                process.wait(timeout=10)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with ONLINE_INSTALL_LOCK, ONLINE_INSTALL_LOCK.probe(install_id) as active:
                        if not active:
                            self.assert_tree_exited(base, 'a')
                            break
                    observed = self.state(observer, install_id)
                    if observed['status'] == '结果未知': self.assert_tree_exited(base, 'a')
                    else: self.assertTrue(observed['running'])
                    time.sleep(.01)
                else:
                    self.fail('supervisor did not release the install identity')
                self.state(observer, install_id, {'结果未知'})

    def test_remote_task_recovery_has_one_claim_and_terminal_cache_refreshes(self):
        with isolated_app() as (_, base):
            install_id = scheduler_tests.OnlineInstallSchedulerRecoveryTests().pending_task_import(base)
            with self.worker(base, 'a', 'tasks') as (_, owner), self.worker(base, 'b') as (_, observer):
                self.assertEqual(observer('cache', data={'id':install_id, 'diagnostic':True})[0], 200)
                responses = []
                operation = threading.Thread(target=lambda: responses.append(owner('post', '/api/online-scripts/install-task-retry/' + install_id)))
                operation.start()
                try:
                    self.wait_started(base, 'a')
                    duplicate = observer('post', '/api/online-scripts/install-task-retry/' + install_id)
                    self.assertEqual(duplicate[0], 409, duplicate)
                    self.assertTrue(self.state(observer, install_id)['running'])
                    self.assertEqual(observer('post', '/api/online-scripts/install-task-confirm/' + install_id)[0], 409)
                finally:
                    (base / 'a.release').touch()
                    operation.join(15)
                self.assertFalse(operation.is_alive())
                self.assertEqual(responses[0][0], 200, responses)
                self.state(observer, install_id, {'已完成'})
                history = observer('get', '/api/online-scripts/install-history')
                self.assertEqual(history[1]['items'][0]['status'], '已完成')
                from fls_manager.models import load_tasks
                self.assertEqual(len(load_tasks()), 1)

    def test_legacy_identity_preserves_active_history_and_becomes_unknown_only_after_owner_exit(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts.constants import persist_online_install
            from fls_manager.online_scripts.logs import online_install_log_file

            install_id = 'historical-install_1'
            log_file = online_install_log_file(install_id, 'demo')
            log_file.write_text('legacy fixture\n', encoding='utf-8')
            with self.worker(base, 'a') as (process, owner):
                self.assertEqual(owner('claim', data={'id':install_id})[0], 200)
                persist_online_install({'id':install_id, 'script_id':'demo', 'script_name':'demo',
                                        'log_file':str(log_file), 'running':True, 'status':'运行中', 'start_time':1})
                with self.worker(base, 'b') as (_, observer):
                    self.assertTrue(self.state(observer, install_id)['running'])
                    stopped = observer('post', '/api/online-scripts/install-stop/' + install_id)
                    self.assertEqual(stopped[0], 200, stopped)
                    self.assertEqual(self.state(observer, install_id)['status'], '停止中')
                    process.kill()
                    process.wait(timeout=10)
                    self.state(observer, install_id, {'结果未知'})

    def test_two_workers_retry_parent_once_and_preserve_bidirectional_relation(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts.constants import persist_online_install, _load_persisted_online_installs

            self.prepare()
            parent = 'c' * 32
            persist_online_install({'id':parent, 'script_id':'demo', 'running':False, 'status':'失败',
                                    'phase':'failed', 'import_task':False, 'start_time':1})
            with self.worker(base, 'a') as (_, first), self.worker(base, 'b') as (_, second):
                barrier = threading.Barrier(2)
                responses, errors = [], []

                def retry(rpc):
                    try:
                        barrier.wait(5)
                        responses.append(rpc('post', '/api/online-scripts/install-retry/' + parent, {'force':True}))
                    except Exception as exc:
                        errors.append(exc)

                threads = [threading.Thread(target=retry, args=(rpc,)) for rpc in (first, second)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(15)
                self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(errors, [])
                self.assertTrue(all(reply[0] in (200, 409) for reply in responses), responses)
                successes = [reply for reply in responses if reply[0] == 200]
                self.assertTrue(successes, responses)
                jobs = _load_persisted_online_installs()
                children = [row for row in jobs.values() if row.get('retry_of') == parent]
                self.assertEqual(len(children), 1, jobs)
                child = children[0]['id']
                self.assertEqual(jobs[parent]['retried_by'], child)
                for rpc in (first, second):
                    replay = rpc('post', '/api/online-scripts/install-retry/' + parent, {'force':True})
                    self.assertEqual(replay[0], 200, replay)
                    self.assertEqual(replay[1]['install_id'], child)
                for label in ('a', 'b'): (base / (label + '.release')).touch()
                self.state(first, child, {'已完成'})

    def test_remote_stop_write_failure_sends_no_signal_and_corrupt_history_is_preserved(self):
        with isolated_app() as (app, base):
            from fls_manager import storage
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_JOBS_FILE

            self.prepare(command=True)
            with self.worker(base, 'a', 'command') as (_, owner):
                install_id = 'a' * 32
                self.assertEqual(self.submit(owner, install_id)[0], 200)
                self.wait_started(base, 'a')
                original = storage._write_json_unlocked

                def fail_stop(path, data):
                    if path == ONLINE_INSTALL_JOBS_FILE and data[install_id].get('stop_requested'):
                        raise OSError('stop receipt read only')
                    return original(path, data)

                before = ONLINE_INSTALL_JOBS_FILE.read_bytes()
                with patch.object(storage, '_write_json_unlocked', side_effect=fail_stop):
                    stopped = app.test_client().post('/api/online-scripts/install-stop/' + install_id, headers={'X-Token':TOKEN})
                self.assertEqual(stopped.status_code, 500, stopped.json)
                self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), before)
                self.assertTrue(self.state(owner, install_id)['running'])
                for pid in json.loads((base / 'a.children.json').read_text(encoding='utf-8')):
                    self.assertFalse(process_exited(pid))
                stopped = app.test_client().post('/api/online-scripts/install-stop/' + install_id, headers={'X-Token':TOKEN})
                self.assertEqual(stopped.status_code, 200, stopped.json)
                self.state(owner, install_id, {'已停止'})
                self.assert_tree_exited(base, 'a')
                terminal = ONLINE_INSTALL_JOBS_FILE.read_bytes()
                ONLINE_INSTALL_JOBS_FILE.write_bytes(b'{broken')
                failed = app.test_client().post('/api/online-scripts/install-stop/' + install_id, headers={'X-Token':TOKEN})
                self.assertEqual(failed.status_code, 500, failed.json)
                self.assertEqual(ONLINE_INSTALL_JOBS_FILE.read_bytes(), b'{broken')
                ONLINE_INSTALL_JOBS_FILE.write_bytes(terminal)

    def test_scheduler_recovery_claim_blocks_other_workers_and_refreshes_old_terminal_cache(self):
        with isolated_app() as (app, base):
            install_id = scheduler_tests.OnlineInstallSchedulerRecoveryTests().partial_install(app, base)
            with self.worker(base, 'a', 'reload') as (_, owner), self.worker(base, 'b') as (_, observer):
                self.assertEqual(observer('cache', data={'id':install_id})[0], 200)
                replies = []
                operation = threading.Thread(target=lambda: replies.append(owner('post', '/api/online-scripts/install-reload/' + install_id)))
                operation.start()
                try:
                    self.wait_started(base, 'a')
                    for action in ('install-reload', 'install-task-retry', 'install-task-confirm', 'install-retry'):
                        duplicate = observer('post', '/api/online-scripts/' + action + '/' + install_id, {})
                        self.assertEqual(duplicate[0], 409, (action, duplicate))
                finally:
                    (base / 'a.release').touch()
                    operation.join(15)
                self.assertFalse(operation.is_alive())
                self.assertEqual(replies[0][0], 200, replies)
                self.state(observer, install_id, {'已完成'})

    def test_confirmation_claim_excludes_second_worker_until_receipt_publication(self):
        with isolated_app() as (app, base):
            install_id = scheduler_tests.OnlineInstallSchedulerRecoveryTests().unconfirmed_task_import(app, base)
            with self.worker(base, 'a', 'confirm') as (_, owner), self.worker(base, 'b') as (_, observer):
                replies = []
                operation = threading.Thread(target=lambda: replies.append(owner('post', '/api/online-scripts/install-task-confirm/' + install_id)))
                operation.start()
                try:
                    self.wait_started(base, 'a')
                    duplicate = observer('post', '/api/online-scripts/install-task-confirm/' + install_id)
                    self.assertEqual(duplicate[0], 409, duplicate)
                finally:
                    (base / 'a.release').touch()
                    operation.join(15)
                self.assertFalse(operation.is_alive())
                self.assertEqual(replies[0][0], 200, replies)
                confirmed = observer('post', '/api/online-scripts/install-task-confirm/' + install_id)
                self.assertEqual(confirmed[0], 200, confirmed)
                self.assertTrue(confirmed[1]['replayed'])

    def test_local_running_cache_without_identity_is_not_an_active_worker(self):
        with isolated_app() as (_, base):
            from fls_manager.online_scripts.constants import (
                ONLINE_INSTALL_LOCK, ONLINE_INSTALL_RUNNING, get_online_install_job, persist_online_install,
            )

            install_id = 'a' * 32
            with ONLINE_INSTALL_LOCK:
                owner = ONLINE_INSTALL_LOCK.claim(install_id)
                info = {'id':install_id, 'script_id':'demo', 'running':True,
                        'status':'运行中', 'process':None, 'log_file':str(base / 'log' / 'fixture.log')}
                ONLINE_INSTALL_RUNNING[install_id] = info
                persist_online_install(info)
                self.assertTrue(get_online_install_job(install_id)['running'])
            owner.close()
            state = get_online_install_job(install_id)
            self.assertEqual(state['status'], '结果未知')
            self.assertFalse(state['running'])
            self.assertIs(ONLINE_INSTALL_RUNNING[install_id], info)

    def test_remote_stop_log_warning_after_owner_completes_preserves_terminal_receipt(self):
        with isolated_app() as (app, base):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import _load_persisted_online_install

            self.prepare()
            with self.worker(base, 'a') as (_, owner):
                install_id = 'a' * 32
                self.assertEqual(self.submit(owner, install_id)[0], 200)
                self.wait_started(base, 'a')

                def fail_log(*args, **kwargs):
                    (base / 'a.release').touch()
                    self.state(owner, install_id, {'已停止'})
                    raise OSError('stop log unavailable after completion')

                with patch.object(install, 'append_log', side_effect=fail_log):
                    stopped = app.test_client().post('/api/online-scripts/install-stop/' + install_id, headers={'X-Token':TOKEN})
                self.assertEqual(stopped.status_code, 200, stopped.json)
                self.assertFalse(stopped.json['running'])
                actual = _load_persisted_online_install(install_id)
                self.assertEqual(actual['status'], '已停止')
                self.assertFalse(actual['running'])
                self.assertIn('stop log unavailable', actual['stop_warning'])


if __name__ == '__main__':
    unittest.main()
