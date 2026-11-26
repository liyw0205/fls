import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests import test_native_script_repo_pull_api as repo_tests
from tests.test_ui_route_components import TOKEN, isolated_app


WORKER = r'''
import faulthandler, importlib, io, json, os, pathlib, subprocess, sys, threading, time
from types import SimpleNamespace
from fls_manager.app import create_app
from fls_manager.paths import BASE_DIR
kind, label = sys.argv[1:]
module = importlib.import_module('fls_manager.routes.scripts.' + {
    'pull':'native_pull', 'repo-pull':'native_repo_pull', 'import':'native_import',
}[kind])
started, release = (BASE_DIR / (label + suffix) for suffix in ('.started', '.release'))
faulthandler.dump_traceback_later(12, repeat=True)
def pause():
    started.touch()
    deadline = time.monotonic() + 40
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('test barrier expired')
        time.sleep(.01)
if kind == 'pull':
    class Response:
        headers = {'Content-Length':'8'}
        def raise_for_status(self): pass
        def iter_content(self, chunk_size):
            yield b'body'
            pause()
            yield b'body'
        def close(self): pass
    module.requests = SimpleNamespace(get=lambda *a, **k: Response())
elif kind == 'repo-pull':
    def command(url, target, proxy_id, progress):
        target = str(pathlib.Path(target).resolve())
        trace = BASE_DIR / (label + '.git-trace')
        result = BASE_DIR / (label + '.git-result')
        code = (
            'import pathlib,subprocess,sys,time\n'
            f'pathlib.Path({str(started)!r}).touch()\n'
            f'while not pathlib.Path({str(release)!r}).exists():time.sleep(.01)\n'
            f'pathlib.Path({str(trace)!r}).write_text("released")\n'
            f'result=subprocess.run(["git","clone","--no-hardlinks",{str(BASE_DIR / "source")!r},{target!r}],capture_output=True,timeout=10)\n'
            f'pathlib.Path({str(result)!r}).write_bytes(str(result.returncode).encode()+b"\\n"+result.stderr)\n'
            'sys.exit(result.returncode)\n'
        )
        return [sys.executable, '-u', '-c', code]
    module.build_repo_pull_command = command
else:
    original = module._copy_upload
    def copy(source, target, cancel, on_progress):
        def progress(count):
            on_progress(count)
            pause()
        return original(source, target, cancel, progress)
    module._copy_upload = copy
    if label == 'receiving':
        original_open = os.open
        def open_upload(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            if str(path).endswith('.upload') and flags & os.O_CREAT:
                pause()
            return fd
        os.open = open_upload
app = create_app()
client = app.test_client()
headers = {'X-Token':os.environ['FLS_TOKEN']}
try:
    for line in sys.stdin:
        command = json.loads(line)
        if command['method'] == 'quit': break
        if command['method'] == 'submit':
            body = command['data']
            if kind == 'import':
                response = client.post('/api/scripts/import', data={
                    'file':(io.BytesIO(b'bodybody'), 'test.py'), **body,
                }, headers=headers)
            else:
                response = client.post('/api/scripts/' + kind, json={
                    'url':'https://example.invalid/source', **body,
                }, headers=headers)
        else:
            response = getattr(client, command['method'])(command['path'], headers=headers)
        print('FLS_RESULT ' + json.dumps([response.status_code, response.json]), flush=True)
finally:
    release.touch()
    for worker in threading.enumerate():
        if worker.name.startswith(('fls-script-', 'fls-repo-')):
            worker.join(10)
'''


class ScriptJobWorkerTests(unittest.TestCase):
    @contextlib.contextmanager
    def worker(self, base, kind, label):
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [sys.executable, '-u', '-c', WORKER, kind, label],
                stdin=subprocess.PIPE, stdout=output, stderr=errors, text=True,
                env=dict(os.environ, PYTHONIOENCODING='utf-8'),
            )
            offset = 0

            def rpc(method, path=None, data=None):
                nonlocal offset
                process.stdin.write(json.dumps({'method': method, 'path': path, 'data': data}) + '\n')
                process.stdin.flush()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    output.seek(offset)
                    line = output.readline()
                    if line and line.endswith(b'\n'):
                        offset = output.tell()
                        if line.startswith(b'FLS_RESULT '):
                            return json.loads(line[len(b'FLS_RESULT '):])
                    elif process.poll() is not None:
                        break
                    else:
                        time.sleep(.01)
                errors.seek(0)
                raise AssertionError('worker RPC failed: ' + errors.read().decode(errors='replace'))

            try:
                self.assertEqual(rpc('get', '/api/scripts/' + kind + '-history')[0], 200)
                yield process, rpc
            except BaseException:
                errors.seek(0)
                print('WORKER_DIAGNOSTICS', kind, label, errors.read().decode(errors='replace'))
                for diagnostic in base.glob('*.git-*'):
                    print(diagnostic.name, diagnostic.read_text(errors='replace'))
                for diagnostic in (base / 'scripts').rglob('.process-*.json'):
                    print(diagnostic.name, diagnostic.read_text(errors='replace'))
                raise
            finally:
                (base / (label + '.release')).touch()
                if process.poll() is None:
                    process.stdin.write('{"method":"quit"}\n')
                    process.stdin.flush()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                process.communicate(timeout=10)

    def wait_started(self, base, label):
        deadline = time.monotonic() + 15
        while not (base / (label + '.started')).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((base / (label + '.started')).exists())

    def wait_status(self, rpc, kind, job_id, expected):
        deadline = time.monotonic() + 15
        latest = None
        while time.monotonic() < deadline:
            status, payload = rpc('get', '/api/scripts/' + kind + '/' + job_id)
            self.assertEqual(status, 200, payload)
            latest = payload['job']
            if latest['status'] in expected:
                return latest
            time.sleep(.02)
        self.fail(f'worker did not reach {expected}: {latest}')

    def setup_repo(self, base, kind):
        if kind == 'repo-pull':
            repo_tests.NativeScriptRepoPullApiTests()._source_repo(base)

    def artifacts(self, base, job_id):
        return sorted(path for path in base.rglob('*') if job_id in path.name and path.suffix != '.lock')

    def test_other_worker_preserves_active_artifacts_replays_and_cancels_original_job(self):
        for kind in ('pull', 'repo-pull', 'import'):
            with self.subTest(kind=kind), isolated_app() as (_, base):
                self.setup_repo(base, kind)
                with self.worker(base, kind, 'a') as (_, owner), self.worker(base, kind, 'b') as (_, observer):
                    body = {'request_id': 'a' * 32, 'path': 'repo' if kind == 'repo-pull' else 'test.py'}
                    status, accepted = owner('submit', data=body)
                    self.assertEqual(status, 202, accepted)
                    job_id = accepted['job']['id']
                    self.wait_started(base, 'a')
                    self.wait_status(owner, kind, job_id, {'downloading', 'cloning', 'processing'})
                    artifacts = self.artifacts(base, job_id)
                    self.assertTrue(artifacts)
                    for _ in range(2):
                        active = self.wait_status(observer, kind, job_id, {'downloading', 'cloning', 'processing'})
                        self.assertNotEqual(active['status'], 'unknown')
                        self.assertEqual(self.artifacts(base, job_id), artifacts)
                    history = observer('get', '/api/scripts/' + kind + '-history')
                    self.assertEqual(history[1]['items'][0]['id'], job_id)
                    replay_status, replay = observer('submit', data=body)
                    self.assertEqual(replay_status, 200, replay)
                    self.assertEqual(replay['job']['id'], job_id)
                    self.assertTrue(replay['job']['replayed'])
                    cancelled = observer('post', '/api/scripts/' + kind + '/' + job_id + '/cancel')
                    self.assertEqual(cancelled[0], 202, cancelled)
                    (base / 'a.release').touch()
                    self.wait_status(observer, kind, job_id, {'cancelled'})
                    self.wait_status(owner, kind, job_id, {'cancelled'})
                    self.assertEqual(self.artifacts(base, job_id), [])
                    self.assertFalse((base / 'scripts' / body['path']).exists())

    def test_two_workers_keep_both_receipts_when_each_finishes_from_an_old_cache(self):
        for kind in ('pull', 'repo-pull', 'import'):
            with self.subTest(kind=kind), isolated_app() as (_, base):
                self.setup_repo(base, kind)
                with self.worker(base, kind, 'a') as (_, first), self.worker(base, kind, 'b') as (_, second):
                    ids = []
                    for rpc, label in ((first, 'a'), (second, 'b')):
                        status, result = rpc('submit', data={'request_id': label * 32, 'path': label + '.py'})
                        self.assertEqual(status, 202, result)
                        ids.append(result['job']['id'])
                        self.wait_started(base, label)
                    if kind != 'pull':
                        rejected = first('submit', data={'request_id': 'c' * 32, 'path': 'overflow.py'})
                        self.assertEqual(rejected[0], 429, rejected)
                        self.assertEqual(rejected[1]['code'], 'too_many_active_jobs')
                    (base / 'a.release').touch()
                    self.wait_status(second, kind, ids[0], {'complete'})
                    self.wait_status(first, kind, ids[1], {'cloning', 'processing', 'downloading'})
                    (base / 'b.release').touch()
                    self.wait_status(first, kind, ids[1], {'complete'})
                    for rpc in (first, second):
                        history = rpc('get', '/api/scripts/' + kind + '-history')
                        self.assertEqual({job['id'] for job in history[1]['items']}, set(ids))
                        self.assertTrue(all(job['status'] == 'complete' for job in history[1]['items']))

    def test_dead_owner_is_recovered_without_cleaning_another_workers_stage(self):
        for kind in ('pull', 'repo-pull', 'import'):
            with self.subTest(kind=kind), isolated_app() as (_, base):
                self.setup_repo(base, kind)
                with self.worker(base, kind, 'a') as (process, first), self.worker(base, kind, 'b') as (_, second):
                    first_job = first('submit', data={'request_id': 'a' * 32, 'path': 'a.py'})[1]['job']['id']
                    self.wait_started(base, 'a')
                    second_job = second('submit', data={'request_id': 'b' * 32, 'path': 'b.py'})[1]['job']['id']
                    self.wait_started(base, 'b')
                    self.wait_status(second, kind, second_job, {'downloading', 'cloning', 'processing'})
                    active_artifacts = self.artifacts(base, second_job)
                    process.kill()
                    process.wait(timeout=10)
                    self.wait_status(second, kind, first_job, {'unknown'})
                    self.assertEqual(self.artifacts(base, first_job), [])
                    self.assertEqual(self.artifacts(base, second_job), active_artifacts)
                    (base / 'b.release').touch()
                    self.wait_status(second, kind, second_job, {'complete'})

    def test_simultaneous_request_id_admission_starts_only_one_worker(self):
        for kind in ('pull', 'repo-pull', 'import'):
            with self.subTest(kind=kind), isolated_app() as (_, base):
                self.setup_repo(base, kind)
                with self.worker(base, kind, 'a') as (_, first), self.worker(base, kind, 'b') as (_, second):
                    barrier = threading.Barrier(2)
                    results, errors = [], []
                    def submit(rpc):
                        try:
                            barrier.wait(timeout=5)
                            results.append(rpc('submit', data={'request_id': 'a' * 32, 'path': 'same.py'}))
                        except BaseException as exc:
                            errors.append(exc)
                    workers = [threading.Thread(target=submit, args=(rpc,)) for rpc in (first, second)]
                    for worker in workers:
                        worker.start()
                    for worker in workers:
                        worker.join(20)
                        self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(sorted(result[0] for result in results), [200, 202], results)
                    self.assertEqual(len({result[1]['job']['id'] for result in results}), 1)
                    job_id = results[0][1]['job']['id']
                    (base / 'a.release').touch()
                    (base / 'b.release').touch()
                    self.wait_status(first, kind, job_id, {'complete'})

    def test_orphan_upload_is_cleaned_only_after_receiving_identity_is_released(self):
        with isolated_app() as (app, base):
            from fls_manager.routes.scripts.native_import import SCRIPT_IMPORT_LOCK

            owner = SCRIPT_IMPORT_LOCK.claim('f' * 32)
            receiving = base / 'data' / ('.script-import-' + 'f' * 32 + '.upload')
            receiving.write_bytes(b'upload in progress')
            try:
                response = app.test_client().get('/api/scripts/import-history', headers={'X-Token': TOKEN})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(receiving.read_bytes(), b'upload in progress')
            finally:
                owner.close()
            response = app.test_client().get('/api/scripts/import-history', headers={'X-Token': TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(receiving.exists())

    def test_other_worker_cannot_remove_upload_before_admission_receipt_exists(self):
        with isolated_app() as (_, base):
            with self.worker(base, 'import', 'receiving') as (_, receiver), self.worker(base, 'import', 'b') as (_, observer):
                results, errors = [], []
                def submit():
                    try:
                        results.append(receiver('submit', data={'request_id': 'a' * 32, 'path': 'received.py'}))
                    except BaseException as exc:
                        errors.append(exc)
                worker = threading.Thread(target=submit)
                worker.start()
                try:
                    self.wait_started(base, 'receiving')
                    uploads = list((base / 'data').glob('.script-import-*.upload'))
                    self.assertEqual(len(uploads), 1)
                    self.assertEqual(observer('get', '/api/scripts/import-history')[1]['items'], [])
                    self.assertTrue(uploads[0].exists())
                finally:
                    (base / 'receiving.release').touch()
                    worker.join(20)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(results[0][0], 202, results)
                self.wait_status(observer, 'import', results[0][1]['job']['id'], {'complete'})
                self.assertEqual((base / 'scripts' / 'received.py').read_bytes(), b'bodybody')
                self.assertEqual(list((base / 'data').glob('.script-import-*.upload')), [])


if __name__ == '__main__':
    unittest.main()
