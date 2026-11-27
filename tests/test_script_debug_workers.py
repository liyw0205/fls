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
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.test_ui_route_components import TOKEN, isolated_app
from tests.test_backup_dependency_process import process_exited


WORKER = r'''
import json, os, sys, threading
from fls_manager.app import create_app
from fls_manager.routes.scripts import debug
debug.debug_command_info = lambda task: {
    'cmd':[sys.executable, '-u', str(debug.script_safe_path(task['command'][5:]))],
    'cwd':os.environ['FLS_BASE_DIR'], 'shell':False,
}
app = create_app()
client = app.test_client()
headers = {'X-Token':os.environ['FLS_TOKEN']}
for line in sys.stdin:
    command = json.loads(line)
    if command['method'] == 'quit':break
    response = getattr(client, command['method'])(command['path'], json=command.get('data'), headers=headers)
    print('FLS_RESULT ' + json.dumps([response.status_code, response.json]), flush=True)
for worker in threading.enumerate():
    if worker.name.startswith('fls-script-debug-'):worker.join(10)
'''


class ScriptDebugWorkerTests(unittest.TestCase):
    @contextlib.contextmanager
    def worker(self, base):
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen([sys.executable, '-u', '-c', WORKER], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=errors, text=True, encoding='utf-8',
                                    env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            results = queue.Queue()

            def read():
                for line in proc.stdout:
                    if line.startswith('FLS_RESULT '):
                        results.put(json.loads(line[len('FLS_RESULT '):]))

            reader = threading.Thread(target=read, daemon=True)
            reader.start()

            def rpc(method, path=None, data=None):
                proc.stdin.write(json.dumps(dict(method=method, path=path, data=data)) + '\n')
                proc.stdin.flush()
                try:
                    return results.get(timeout=15)
                except queue.Empty as exc:
                    errors.seek(0)
                    raise AssertionError(errors.read().decode('utf-8', errors='replace')) from exc

            try:
                self.assertEqual(rpc('get', '/api/scripts/debug-runs')[0], 200)
                yield proc, rpc
            finally:
                (base / 'release').touch()
                if proc.poll() is None:
                    proc.stdin.write('{"method":"quit"}\n')
                    proc.stdin.flush()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                proc.wait(timeout=10)
                reader.join(5)
                self.assertFalse(reader.is_alive())
                proc.stdin.close()
                proc.stdout.close()

    def script(self, base, *, child=False):
        code = (
            'import pathlib, time, os, signal, subprocess, sys\n'
            f'base = pathlib.Path({str(base)!r})\n'
            'with (base / "executions").open("a") as f:f.write("started\\n")\n'
        )
        if child:
            child_code = ('import pathlib,time,os,signal\n'
                          'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                          f'pathlib.Path({str(base / "child-ready")!r}).touch()\n'
                          'time.sleep(60)\n')
            code += f'p = subprocess.Popen([sys.executable, "-u", "-c", {child_code!r}])\n'
            code += '(base / "child-pid").write_text(str(p.pid))\n'
            code += 'while not (base / "child-ready").exists():time.sleep(.01)\n'
        code += '(base / "started").touch()\nwhile not (base / "release").exists():time.sleep(.01)\n'
        (base / 'scripts' / 'demo.py').write_text(code, encoding='utf-8')

    def wait_for(self, predicate, *, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.02)
        self.fail('debug condition did not become true')

    def wait_done(self, rpc, debug_id):
        def done():
            status, body = rpc('get', '/api/scripts/debug-runs/' + debug_id)
            self.assertEqual(status, 200, body)
            return body['run'] if not body['run']['active'] else None
        return self.wait_for(done)

    def test_two_workers_replay_query_and_durable_remote_stop(self):
        with isolated_app() as (_, base):
            self.script(base)
            with self.worker(base) as (_, a), self.worker(base) as (_, b):
                body = dict(path='demo.py', request_id='a' * 32)
                with ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(lambda rpc: rpc('post', '/api/scripts/debug-runs', body), (a, b)))
                self.assertEqual(sorted(result[0] for result in results), [200, 202], results)
                self.wait_for(lambda: (base / 'started').exists())
                self.assertEqual((base / 'executions').read_text().splitlines(), ['started'])
                owner_index = next(index for index, result in enumerate(results) if result[0] == 202)
                remote = (b, a)[owner_index]
                debug_id = results[0][1]['run']['id']
                self.assertEqual(remote('get', '/api/scripts/debug-requests/' + 'a' * 32)[1]['run']['status'], 'running')
                response = remote('post', f'/api/scripts/debug-runs/{debug_id}/stop', {})
                self.assertEqual(response[0], 202, response)
                self.assertTrue(response[1]['run']['active'])
                self.assertEqual(response[1]['run']['status'], 'stopping')
                final = self.wait_done(remote, debug_id)
                self.assertEqual(final['status'], 'stopped')
                self.assertFalse(final['running'])

    def test_owner_crash_retains_restore_exclusion_until_process_tree_closes(self):
        with isolated_app() as (_, base):
            self.script(base, child=True)
            from fls_manager.backup_lease import release_process_lease, try_process_lease

            with self.worker(base) as (owner, a), self.worker(base) as (_, b):
                accepted = a('post', '/api/scripts/debug-runs', dict(path='demo.py', request_id='b' * 32))
                self.assertEqual(accepted[0], 202, accepted)
                self.wait_for(lambda: (base / 'started').exists())
                lease_path = base / '.backup_native_restore.lock'
                self.assertIsNone(try_process_lease(lease_path))
                owner.kill()
                owner.wait(timeout=10)
                # Linux grants the child a TERM grace period; Windows closes its Job Object.
                final = self.wait_done(b, 'b' * 32)
                self.assertEqual(final['status'], 'unknown')
                self.assertTrue(process_exited(int((base / 'child-pid').read_text())))
                fd = self.wait_for(lambda: try_process_lease(lease_path))
                release_process_lease(fd)
                replay = b('post', '/api/scripts/debug-runs', dict(path='demo.py', request_id='b' * 32))
                self.assertEqual(replay[0], 200, replay)
                self.assertEqual(replay[1]['run']['status'], 'unknown')
                self.assertEqual((base / 'executions').read_text().splitlines(), ['started'])

    def test_parent_exit_closes_child_tree_before_terminal_receipt(self):
        with isolated_app() as (_, base):
            self.script(base, child=True)
            with self.worker(base) as (_, rpc):
                response = rpc('post', '/api/scripts/debug-runs', dict(path='demo.py', request_id='c' * 32))
                self.assertEqual(response[0], 202, response)
                self.wait_for(lambda: (base / 'started').exists())
                (base / 'release').touch()
                final = self.wait_done(rpc, 'c' * 32)
                self.assertEqual(final['status'], 'succeeded')
                self.assertEqual(final['returncode'], 0)
                self.assertTrue(process_exited(int((base / 'child-pid').read_text())))

    def test_get_preserves_receipts_and_missing_active_receipt_blocks_new_admission(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.scripts import debug
            from fls_manager.script_debug_coordination import DEBUG_IDENTITIES
            from fls_manager.paths import SCRIPT_DEBUG_RUNS_FILE

            client = app.test_client()
            headers = {'X-Token': TOKEN}
            with debug.SCRIPT_DEBUG_LOCK:
                owner = DEBUG_IDENTITIES.claim('d' * 32)
            try:
                with patch.object(debug, 'write_json', side_effect=AssertionError('GET wrote receipt')):
                    response = client.get('/api/scripts/debug-requests/' + 'd' * 32, headers=headers)
                self.assertEqual(response.status_code, 404)
                self.assertFalse(SCRIPT_DEBUG_RUNS_FILE.exists())
                (base / 'scripts' / 'demo.py').touch()
                with patch.object(debug.subprocess, 'Popen') as popen:
                    blocked = client.post('/api/scripts/debug-runs', json=dict(path='demo.py', request_id='e' * 32), headers=headers)
                self.assertEqual(blocked.status_code, 503, blocked.json)
                popen.assert_not_called()
                self.assertTrue(DEBUG_IDENTITIES.lease_path('d' * 32).exists())
            finally:
                owner.close()

    def test_stop_save_failure_sends_no_signal_and_old_owner_cannot_replace_new_result(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.scripts import debug
            from fls_manager.script_debug_coordination import DEBUG_IDENTITIES
            from fls_manager.paths import SCRIPT_DEBUG_RUNS_FILE

            info = dict(id='f' * 32, request_id='f' * 32, managed=True, rel='demo.py', status='running', running=True)
            with debug.SCRIPT_DEBUG_LOCK:
                owner = DEBUG_IDENTITIES.claim(info['id'])
                debug.persist_script_debug(info)
            original = SCRIPT_DEBUG_RUNS_FILE.read_bytes()
            try:
                with patch.object(debug, 'write_json', side_effect=OSError('disk full')), patch.object(debug, 'terminate_debug_process') as signal:
                    response = app.test_client().post('/api/scripts/debug-runs/' + info['id'] + '/stop', json={}, headers={'X-Token': TOKEN})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(SCRIPT_DEBUG_RUNS_FILE.read_bytes(), original)
                signal.assert_not_called()
                newer = dict(info, status='succeeded', running=False, returncode=0, error='new result')
                SCRIPT_DEBUG_RUNS_FILE.write_text(json.dumps([newer]), encoding='utf-8')
                self.assertFalse(debug.persist_script_debug(dict(info, status='unknown', error='late diagnostic')))
                self.assertEqual(json.loads(SCRIPT_DEBUG_RUNS_FILE.read_text())[0]['error'], 'new result')
            finally:
                owner.close()

    def test_handoff_failure_cleans_started_tree_and_releases_both_leases(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.scripts import debug
            from fls_manager.script_debug_coordination import DEBUG_IDENTITIES
            from fls_manager.backup_lease import release_process_lease, try_process_lease

            self.script(base)
            with patch.object(debug, 'debug_command_info', return_value=dict(cmd=[sys.executable, str(base / 'scripts' / 'demo.py')], cwd=str(base), shell=False)), patch.object(debug, 'start_directory_job', side_effect=RuntimeError('handoff failed')):
                response = app.test_client().post('/api/scripts/debug-runs', json=dict(path='demo.py', request_id='1' * 32), headers={'X-Token': TOKEN})
            self.assertEqual(response.status_code, 202, response.json)
            self.assertFalse(response.json['run']['active'])
            self.assertFalse(DEBUG_IDENTITIES.owners)
            fd = try_process_lease(base / '.backup_native_restore.lock')
            self.assertIsNotNone(fd)
            release_process_lease(fd)

    def test_identity_precedes_first_receipt_and_save_failure_never_spawns(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.scripts import debug
            from fls_manager.script_debug_coordination import DEBUG_IDENTITIES, debug_active

            self.script(base)

            def fail(info):
                self.assertTrue(debug_active(info['id']))
                raise OSError('receipt failed')

            with patch.object(debug, 'persist_script_debug', side_effect=fail), patch.object(debug.subprocess, 'Popen') as popen:
                result = app.test_client().post('/api/scripts/debug-runs', json=dict(path='demo.py', request_id='2' * 32), headers={'X-Token': TOKEN})
            self.assertEqual(result.status_code, 503, result.json)
            popen.assert_not_called()
            self.assertFalse(DEBUG_IDENTITIES.owners)
            self.assertFalse(debug_active('2' * 32))

    def test_log_close_failure_keeps_identity_and_restore_lease_until_closed(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.scripts import debug
            from fls_manager.script_debug_coordination import debug_active
            from fls_manager.backup_lease import release_process_lease, try_process_lease

            self.script(base)
            entered, release = threading.Event(), threading.Event()
            actual_open = debug.open_log_file
            wrapped = []

            class Log:
                def __init__(self, stream):
                    self.stream, self.calls = stream, 0

                @property
                def closed(self):
                    return self.stream.closed

                def write(self, data):
                    return self.stream.write(data)

                def fileno(self):
                    return self.stream.fileno()

                @property
                def log_lease_path(self):
                    return self.stream.log_lease_path

                def close(self):
                    self.calls += 1
                    if self.calls == 1:
                        entered.set()
                        if not release.wait(10):
                            raise AssertionError('log close barrier expired')
                        raise OSError('log close failed once')
                    self.stream.close()

            def open_log(path, *args, **kwargs):
                stream = actual_open(path, *args, **kwargs)
                wrapped.append(Log(stream))
                return wrapped[-1]

            try:
                with patch.object(debug, 'open_log_file', side_effect=open_log), patch.object(debug, 'debug_command_info', return_value=dict(cmd=[sys.executable, '-c', 'print("done")'], cwd=str(base), shell=False)):
                    result = app.test_client().post('/api/scripts/debug-runs', json=dict(path='demo.py', request_id='3' * 32), headers={'X-Token': TOKEN})
                    self.assertEqual(result.status_code, 202, result.json)
                    self.assertTrue(entered.wait(10))
                    self.assertTrue(debug_active('3' * 32))
                    self.assertIsNone(try_process_lease(base / '.backup_native_restore.lock'))
                    release.set()
                    self.wait_for(lambda: not debug_active('3' * 32))
                    self.assertTrue(wrapped[0].closed)
                    self.assertEqual(debug.get_script_debug_run('3' * 32)['status'], 'unknown')
                    fd = try_process_lease(base / '.backup_native_restore.lock')
                    self.assertIsNotNone(fd)
                    release_process_lease(fd)
            finally:
                release.set()

    def test_all_debug_entries_reject_directory_replacement_and_bad_history_is_preserved(self):
        with isolated_app() as (app, base):
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            from fls_manager.paths import SCRIPT_DEBUG_RUNS_FILE

            client = app.test_client()
            headers = {'X-Token': TOKEN}
            fd = try_process_lease(base / '.backup_native_restore.lock')
            self.assertIsNotNone(fd)
            try:
                for method, path in (
                    ('get', '/scripts/debug/demo.py'), ('post', '/scripts/debug-stop/' + 'a' * 32),
                    ('get', '/scripts/debug-log/' + 'a' * 32), ('get', '/api/scripts/debug-log/' + 'a' * 32),
                    ('get', '/api/scripts/debug-runs'), ('post', '/api/scripts/debug-runs'),
                    ('get', '/api/scripts/debug-runs/' + 'a' * 32), ('post', '/api/scripts/debug-runs/' + 'a' * 32 + '/stop'),
                    ('get', '/api/scripts/debug-requests/' + 'a' * 32),
                ):
                    with self.subTest(path=path):
                        self.assertEqual(getattr(client, method)(path, json={}, headers=headers).status_code, 503)
            finally:
                release_process_lease(fd)
            SCRIPT_DEBUG_RUNS_FILE.write_text('{broken', encoding='utf-8')
            self.assertEqual(client.get('/api/scripts/debug-runs', headers=headers).status_code, 503)
            self.assertEqual(SCRIPT_DEBUG_RUNS_FILE.read_text(), '{broken')
