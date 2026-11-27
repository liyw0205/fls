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

from tests.test_bulk_workflows import isolated_app, sample_task
from tests.test_backup_dependency_process import process_exited


WORKER = r'''
import json, os, sys, threading
from fls_manager.app import create_app
from fls_manager import task_runner, scheduler
app = create_app()
client = app.test_client()
headers = {'X-Token':os.environ['FLS_TOKEN']}
for line in sys.stdin:
    command = json.loads(line)
    if command['method'] == 'quit':break
    if command['method'] == 'cron':
        scheduler.scheduler_run_once('a')
        result = [True, 'scheduler callback completed']
    else:
        response = getattr(client, command['method'])(command['path'], json=command.get('data'), headers=headers)
        result = [response.status_code, response.json]
    print('FLS_RESULT ' + json.dumps(result), flush=True)
for worker in threading.enumerate():
    if worker.name.startswith('fls-task-'):worker.join(10)
'''


class TaskExecutionWorkerTests(unittest.TestCase):
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
                    return results.get(timeout=20)
                except queue.Empty as exc:
                    errors.seek(0)
                    raise AssertionError(errors.read().decode('utf-8', errors='replace')) from exc

            try:
                self.assertEqual(rpc('get', '/api/tasks')[0], 200)
                yield proc, rpc
            finally:
                (base / 'release').touch()
                if proc.poll() is None:
                    with contextlib.suppress(Exception):
                        rpc('post', '/api/task/action/stop/a', {'request_id': os.urandom(16).hex()})
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

    def seed(self, base, *, child=False, retry=False, delay=0):
        from fls_manager import models
        code = ('import pathlib,time,os,signal,subprocess,sys\n'
                f'base=pathlib.Path({str(base)!r})\n'
                'with (base/"executions").open("a") as f:f.write("started\\n")\n')
        if child:
            child_code = ('import pathlib,time,signal\n'
                          'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                          f'pathlib.Path({str(base / "child-ready")!r}).touch()\n'
                          'time.sleep(60)\n')
            code += f'p=subprocess.Popen([sys.executable,"-u","-c",{child_code!r}])\n'
            code += '(base/"child-pid").write_text(str(p.pid))\n'
            code += 'while not (base/"child-ready").exists():time.sleep(.01)\n'
        code += '(base/"started").touch()\n'
        code += 'sys.exit(1)\n' if retry else 'while not (base/"release").exists():time.sleep(.01)\n'
        (base / 'scripts' / 'run.py').write_text(code, encoding='utf-8')
        models.save_tasks([sample_task('a', command='task run.py',
                          retry={'attempts': 1 if retry else 0, 'interval_seconds': 5},
                          random_delay={'mode': 'custom' if delay else 'none', 'seconds': delay})])

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail('task execution condition did not become true')

    def done(self, rpc):
        observations = []

        def idle():
            status, body = rpc('get', '/api/task/a')
            self.assertEqual(status, 200, body)
            observation = {
                'runtime': body['runtime'],
                'history': [{key: row.get(key) for key in ('id', 'status', 'source', 'message')}
                            for row in body['history']],
            }
            if not observations or observations[-1] != observation:
                observations.append(observation)
            return body if not body['runtime']['running'] else None
        try:
            return self.wait_for(idle)
        except AssertionError as exc:
            self.fail(f'{exc}; observed task phases: {observations!r}')

    def test_two_workers_reserve_once_and_remote_stop_preserves_history(self):
        with isolated_app() as (_, base):
            self.seed(base, child=True)
            with self.worker(base) as (_, a), self.worker(base) as (_, b):
                with ThreadPoolExecutor(2) as pool:
                    responses = list(pool.map(lambda item: item[1]('post', '/api/task/action/run/a',
                        {'request_id': item[0] * 32}), [('a', a), ('b', b)]))
                self.assertEqual(sorted(row[0] for row in responses), [200, 409], responses)
                self.wait_for(lambda: (base / 'started').exists())
                remote = b if responses[0][0] == 200 else a
                detail = remote('get', '/api/task/a')[1]
                self.assertTrue(detail['runtime']['running'])
                self.assertEqual(detail['history'][0]['status'], 'running')
                log = remote('get', '/api/task/a/log')[1]['log']
                self.assertIn('启动任务', log)
                stopped = remote('post', '/api/task/action/stop/a', {'request_id': 'c' * 32})
                self.assertEqual(stopped[0], 200, stopped)
                self.assertIn('请求停止', stopped[1]['results'][0]['msg'])
                final = self.done(remote)
                self.assertEqual(final['history'][0]['status'], 'stopped')
                self.assertTrue(process_exited(int((base / 'child-pid').read_text())))
                self.assertEqual((base / 'executions').read_text().splitlines(), ['started'])

    def test_owner_crash_keeps_restore_excluded_until_command_tree_exits(self):
        with isolated_app() as (_, base):
            self.seed(base, child=True)
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            from fls_manager.task_execution_coordination import execution_snapshot
            with self.worker(base) as (owner, a), self.worker(base) as (_, b):
                self.assertEqual(a('post', '/api/task/action/run/a', {'request_id': 'd' * 32})[0], 200)
                self.wait_for(lambda: (base / 'started').exists())
                lease = base / '.backup_native_restore.lock'
                self.assertIsNone(try_process_lease(lease))
                owner.kill()
                owner.wait(timeout=10)
                self.done(b)
                self.assertEqual(execution_snapshot(['a'])['a']['status'], 'unknown')
                self.assertTrue(process_exited(int((base / 'child-pid').read_text())))
                fd = self.wait_for(lambda: try_process_lease(lease))
                release_process_lease(fd)

    def test_retry_wait_holds_restore_lease_and_remote_stop_cancels_callback(self):
        with isolated_app() as (_, base):
            self.seed(base, retry=True)
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            from fls_manager.task_execution_coordination import execution_snapshot
            with self.worker(base) as (_, a), self.worker(base) as (_, b):
                self.assertEqual(a('post', '/api/task/action/run/a', {'request_id': 'e' * 32})[0], 200)
                self.wait_for(lambda: execution_snapshot(['a']).get('a', {}).get('status') == 'retry_wait')
                self.assertIsNone(try_process_lease(base / '.backup_native_restore.lock'))
                self.assertEqual(b('post', '/api/task/action/stop/a', {'request_id': 'f' * 32})[0], 200)
                self.done(b)
                fd = self.wait_for(lambda: try_process_lease(base / '.backup_native_restore.lock'))
                release_process_lease(fd)
                self.assertEqual((base / 'executions').read_text().splitlines(), ['started'])

    def test_retry_executes_once_and_cron_uses_same_identity_admission(self):
        with isolated_app() as (_, base):
            self.seed(base, retry=True)
            with self.worker(base) as (_, a), self.worker(base) as (_, b):
                self.assertTrue(a('cron')[0])
                final = self.done(b)
                self.assertEqual(len(final['history']), 2)
                self.assertEqual(final['history'][0]['source'], 'retry')
                self.assertEqual((base / 'executions').read_text().splitlines(), ['started', 'started'])

    def test_admission_failure_before_history_and_broken_receipt_never_launch(self):
        with isolated_app() as (_, base):
            self.seed(base)
            from fls_manager import models, task_runner, task_execution_coordination as coordination
            from fls_manager.backup_lease import release_process_lease, try_process_lease
            fd = try_process_lease(base / '.backup_native_restore.lock')
            try:
                with self.assertRaises(Exception):
                    task_runner.run_task_now('a')
                self.assertFalse(models.load_task_history())
            finally:
                release_process_lease(fd)
            with patch.object(coordination, '_write', side_effect=OSError('receipt full')):
                with self.assertRaises(OSError):
                    task_runner.run_task_now('a')
            self.assertFalse(models.load_task_history())
            with patch.object(task_runner, 'add_task_history', side_effect=OSError('history full')):
                with self.assertRaises(OSError):
                    task_runner.run_task_now('a')
            self.assertFalse((base / 'executions').exists())
            path = coordination._receipt_path('a')
            path.write_bytes(b'{broken')
            with self.assertRaises(Exception):
                task_runner.run_task_now('a')
            self.assertEqual(path.read_bytes(), b'{broken')
            self.assertFalse((base / 'executions').exists())

    def test_remote_stop_during_delay_never_spawns_command(self):
        with isolated_app() as (_, base):
            self.seed(base, delay=120)
            from fls_manager.task_execution_coordination import execution_snapshot
            with self.worker(base) as (_, a), self.worker(base) as (_, b):
                self.assertEqual(a('post', '/api/task/action/run/a', {'request_id': '1' * 32})[0], 200)
                self.wait_for(lambda: execution_snapshot(['a']).get('a', {}).get('status') == 'delaying')
                self.assertEqual(b('post', '/api/task/action/stop/a', {'request_id': '2' * 32})[0], 200)
                final = self.done(b)
                self.assertEqual(final['history'][0]['status'], 'stopped')
                self.assertFalse((base / 'executions').exists())

    def test_different_workers_preserve_both_task_histories_and_counters(self):
        with isolated_app() as (_, base):
            self.seed(base)
            from fls_manager import models
            tasks = models.load_tasks()
            tasks.append(sample_task('b', command='task run.py'))
            models.save_tasks(tasks)
            with self.worker(base) as (_, a), self.worker(base) as (_, b):
                with ThreadPoolExecutor(2) as pool:
                    responses = list(pool.map(lambda item: item[1]('post', '/api/task/action/run/' + item[0],
                        {'request_id': item[0] * 32}), [('a', a), ('b', b)]))
                self.assertEqual([response[0] for response in responses], [200, 200], responses)
                self.wait_for(lambda: (base / 'executions').exists() and
                              len((base / 'executions').read_text().splitlines()) == 2)
                (base / 'release').touch()
                self.done(a)
                self.wait_for(lambda: not b('get', '/api/task/b')[1]['runtime']['running'])
                history = models.load_task_history()
                self.assertEqual({row['task_id'] for row in history}, {'a', 'b'})
                self.assertEqual([row['status'] for row in history], ['success', 'success'])
                self.assertEqual([task['run_count'] for task in models.load_tasks()], [1, 1])

    def test_thread_handoff_and_failed_terminal_history_release_admission(self):
        for failed_history in (False, True):
            with self.subTest(failed_history=failed_history), isolated_app() as (_, base):
                self.seed(base)
                from fls_manager import task_runner, task_execution_coordination as coordination
                from fls_manager.backup_lease import release_process_lease, try_process_lease
                with patch.object(task_runner.threading, 'Thread', side_effect=RuntimeError('thread full')):
                    if failed_history:
                        with patch.object(task_runner, 'finish_task_history', side_effect=OSError('history full')):
                            with self.assertRaises(OSError):
                                task_runner.run_task_now('a')
                    else:
                        self.assertFalse(task_runner.run_task_now('a')[0])
                row = coordination.execution_snapshot(['a'])['a']
                self.assertEqual(row['status'], 'unknown' if failed_history else 'start_failed')
                self.assertFalse(row['active'])
                fd = try_process_lease(base / '.backup_native_restore.lock')
                self.assertIsNotNone(fd)
                release_process_lease(fd)
                self.assertFalse((base / 'executions').exists())

    def test_manual_run_replaces_remote_pending_retry_without_duplicate_callback(self):
        with isolated_app() as (_, base):
            self.seed(base, retry=True)
            from fls_manager.task_execution_coordination import execution_snapshot
            with self.worker(base) as (_, a), self.worker(base) as (_, b):
                self.assertEqual(a('post', '/api/task/action/run/a', {'request_id': '3' * 32})[0], 200)
                self.wait_for(lambda: execution_snapshot(['a']).get('a', {}).get('status') == 'retry_wait')
                cancelled_history_id = execution_snapshot(['a'])['a']['history_id']
                response = b('post', '/api/task/action/run/a', {'request_id': '4' * 32})
                self.assertEqual(response[0], 200, response)

                def new_manual_retry():
                    row = execution_snapshot(['a']).get('a', {})
                    return row if row.get('status') == 'retry_wait' and row['history_id'] != cancelled_history_id else None

                # Observe the replacement's first attempt before waiting for its retry and cleanup.
                replacement = self.wait_for(new_manual_retry)
                intermediate = b('get', '/api/task/a')[1]
                self.assertTrue(intermediate['runtime']['active'])
                self.assertEqual(intermediate['runtime']['history_id'], replacement['history_id'])
                self.assertEqual([row['source'] for row in intermediate['history']], ['manual', 'manual'])
                final = self.done(b)
                self.assertEqual(len(final['history']), 3)
                self.assertEqual([row['source'] for row in final['history']], ['retry', 'manual', 'manual'])
                self.assertEqual((base / 'executions').read_text().splitlines(), ['started'] * 3)


if __name__ == '__main__':
    unittest.main()
