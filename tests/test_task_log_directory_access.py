import io
import threading
import unittest
from unittest.mock import patch

from tests.test_bulk_workflows import TOKEN, isolated_app, sample_task
from tests import test_script_directory_access as directory_tests


class TaskLogDirectoryAccessTests(unittest.TestCase):
    def test_restore_states_block_all_task_and_system_log_io(self):
        helper = directory_tests.ScriptDirectoryAccessTests()
        for mode in ('legacy', 'native', 'journal', 'io'):
            with self.subTest(mode=mode), isolated_app() as (app, base):
                from fls_manager import models
                models.save_tasks([sample_task('a')])
                target = base / 'log' / 'task.log'
                target.write_text('original\n', encoding='utf-8')
                with helper.blocked(mode, base):
                    for method, path in (
                        ('get', '/log/a'), ('get', '/api/log/a'), ('get', '/api/task/a/log'),
                        ('get', '/logfile/task.log'), ('get', '/api/logfile/task.log'),
                        ('get', '/api/logfile/task.log/download'), ('get', '/api/logs'), ('get', '/logs'),
                        ('post', '/logfile/delete/task.log'), ('post', '/api/logs/groups/delete'),
                    ):
                        with self.subTest(path=path):
                            response = getattr(app.test_client(), method)(path, headers={'X-Token': TOKEN})
                            self.assertEqual(response.status_code, 503, response.get_data(as_text=True))
                            self.assertEqual(response.json['code'], 'panel_storage_busy')
                    self.assertEqual(target.read_text(), 'original\n')
                helper.assert_restore_available()

    def test_growing_download_reads_only_limit_plus_one_and_closes_before_release(self):
        with isolated_app() as (app, base):
            helper = directory_tests.ScriptDirectoryAccessTests()
            target = base / 'log' / 'growing.log'
            target.write_bytes(b'small')
            reads = []

            class GrowingFile(io.BytesIO):
                def read(self, size=-1):
                    helper.assert_restore_blocked()
                    reads.append(size)
                    if size != 8 * 1024 * 1024 + 1:
                        raise AssertionError('unbounded download read')
                    return b'x' * size

                def close(self):
                    helper.assert_restore_blocked()
                    super().close()

            stream = GrowingFile()
            original = type(target).open

            def opened(path, *args, **kwargs):
                return stream if path == target else original(path, *args, **kwargs)

            with patch.object(type(target), 'open', autospec=True, side_effect=opened):
                response = app.test_client().get('/api/logfile/growing.log/download', headers={'X-Token': TOKEN})
            self.assertEqual(response.status_code, 413)
            self.assertEqual(reads, [8 * 1024 * 1024 + 1])
            self.assertTrue(stream.closed)
            helper.assert_restore_available()

    def test_final_log_close_keeps_identity_and_restore_excluded(self):
        with isolated_app() as (_, base):
            from fls_manager.task_execution_coordination import TaskExecution, execution_snapshot
            helper = directory_tests.ScriptDirectoryAccessTests()
            execution = TaskExecution('a', 'a' * 32)
            entered, release = threading.Event(), threading.Event()
            failures = []

            class SlowClose:
                closed = False

                def close(self):
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('log close was not released')
                    self.closed = True

            execution.log_fp = SlowClose()
            execution.terminal = 'success'

            def finish():
                try:
                    execution.release()
                except BaseException as exc:
                    failures.append(exc)

            worker = threading.Thread(target=finish)
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                helper.assert_restore_blocked()
                self.assertTrue(execution_snapshot(['a'])['a']['active'])
                self.assertEqual(execution_snapshot(['a'])['a']['status'], 'starting')
            finally:
                release.set()
                worker.join(15)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(execution_snapshot(['a'])['a']['status'], 'success')
            self.assertFalse(execution_snapshot(['a'])['a']['active'])
            helper.assert_restore_available()


if __name__ == '__main__':
    unittest.main()
