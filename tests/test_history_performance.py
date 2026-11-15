import contextlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def isolated_models():
    old_base = os.environ.get("FLS_BASE_DIR")
    old_disable = os.environ.get("FLS_DISABLE_HISTORY_INDEX")
    with tempfile.TemporaryDirectory(prefix="fls-history-performance-test-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ.pop("FLS_DISABLE_HISTORY_INDEX", None)
        for name in list(sys.modules):
            if name == "fls_manager" or name.startswith("fls_manager."):
                sys.modules.pop(name, None)
        try:
            yield Path(temp_dir)
        finally:
            state = sys.modules.get("fls_manager.state")
            scheduler = getattr(state, "scheduler", None) if state else None
            if scheduler is not None:
                with contextlib.suppress(Exception):
                    scheduler.shutdown(wait=False)
            for name in list(sys.modules):
                if name == "fls_manager" or name.startswith("fls_manager."):
                    sys.modules.pop(name, None)
            if old_base is None:
                os.environ.pop("FLS_BASE_DIR", None)
            else:
                os.environ["FLS_BASE_DIR"] = old_base
            if old_disable is None:
                os.environ.pop("FLS_DISABLE_HISTORY_INDEX", None)
            else:
                os.environ["FLS_DISABLE_HISTORY_INDEX"] = old_disable


def load_benchmark_module():
    path = ROOT / "tools" / "benchmark_performance.py"
    spec = importlib.util.spec_from_file_location("fls_benchmark_performance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HistoryPerformanceTests(unittest.TestCase):
    def test_unfiltered_full_history_read_skips_nonselective_index(self):
        with isolated_models():
            from fls_manager import models, paths

            rows = [
                {"id": "h-1", "task_id": "task-a", "message": "first"},
                None,
                {"id": "h-2", "task_id": "task-b", "message": "second"},
            ]
            paths.TASK_HISTORY_FILE.write_text(json.dumps(rows), encoding="utf-8")

            with patch.object(models, "_open_current_task_history_index") as open_index:
                items, total = models.query_task_history(limit=None)

            self.assertEqual(total, 2)
            self.assertEqual([item["id"] for item in items], ["h-1", "h-2"])
            open_index.assert_not_called()
            self.assertFalse(paths.TASK_HISTORY_INDEX_FILE.exists())

            plan = models.explain_task_history_query()
            self.assertTrue(any("SCAN history_rows" in detail for detail in plan))

    def test_index_queries_match_json_order_and_invalidate_after_write(self):
        with isolated_models():
            from fls_manager import models, paths

            rows = [
                {
                    "id": f"h-{index}",
                    "task_id": "task-a" if index < 3 else "task-b",
                    "task_name": "Alpha" if index < 3 else "Beta",
                    "command": "echo synthetic",
                    "status": "failed" if index % 2 else "success",
                    "message": f"message-{index}",
                }
                for index in range(6)
            ]
            paths.TASK_HISTORY_FILE.write_text(json.dumps(rows), encoding="utf-8")

            items, total = models.query_task_history(task_id="task-a", limit=2)
            self.assertEqual(total, 3)
            self.assertEqual([item["id"] for item in items], ["h-0", "h-1"])
            self.assertTrue(any("history_rows_task_seq" in plan for plan in models.explain_task_history_query(task_id="task-a")))

            models.add_task_history({
                "id": "h-new",
                "task_id": "task-a",
                "task_name": "Alpha",
                "status": "failed",
                "message": "new",
            })
            items, total = models.query_task_history(task_id="task-a", limit=1)
            self.assertEqual(total, 4)
            self.assertEqual(items[0]["id"], "h-new")

    def test_corrupt_or_disabled_index_falls_back_without_changing_contract(self):
        with isolated_models():
            from fls_manager import models, paths

            rows = [{"id": "h1", "task_id": "task-a", "status": "failed", "message": "needle"}]
            paths.TASK_HISTORY_FILE.write_text(json.dumps(rows), encoding="utf-8")
            self.assertEqual(models.query_task_history(q="needle")[1], 1)
            paths.TASK_HISTORY_INDEX_FILE.write_bytes(b"not sqlite")
            items, total = models.query_task_history(q="needle")
            self.assertEqual((items[0]["id"], total), ("h1", 1))

            os.environ["FLS_DISABLE_HISTORY_INDEX"] = "1"
            items, total = models.query_task_history(status="failed")
            self.assertEqual((items[0]["id"], total), ("h1", 1))

    def test_search_uses_ngram_index_and_matches_json_fallback(self):
        with isolated_models():
            from fls_manager import models, paths

            rows = [
                {
                    "id": "h-needle",
                    "task_id": "task-a",
                    "task_name": "Alpha Task",
                    "command": "echo Synthetic Needle",
                    "status": "success",
                    "message": "literal%value_literal",
                    "source": "manual",
                },
                {
                    "id": "h-case",
                    "task_id": "task-b",
                    "task_name": "beta",
                    "command": "echo needle suffix",
                    "status": "failed",
                    "message": "other",
                    "source": "retry",
                },
                {
                    "id": "h-short",
                    "task_id": "task-c",
                    "task_name": "ab",
                    "command": "echo short",
                    "status": "success",
                    "message": "short",
                    "source": "manual",
                },
            ]
            paths.TASK_HISTORY_FILE.write_text(json.dumps(rows), encoding="utf-8")

            for query in ("Needle", "synthetic", "literal%v", "literal_v", "ab"):
                indexed_items, indexed_total = models.query_task_history(q=query, limit=None)
                os.environ["FLS_DISABLE_HISTORY_INDEX"] = "1"
                fallback_items, fallback_total = models.query_task_history(q=query, limit=None)
                os.environ.pop("FLS_DISABLE_HISTORY_INDEX", None)
                self.assertEqual(indexed_total, fallback_total, query)
                self.assertEqual(indexed_items, fallback_items, query)

            plan = models.explain_task_history_query(q="Needle")
            self.assertTrue(any("history_ngrams_ngram_seq" in detail for detail in plan))
            self.assertFalse(any("SCAN history_rows" in detail for detail in plan))

    def test_search_index_rebuilds_when_ngram_table_is_missing(self):
        with isolated_models():
            from fls_manager import models, paths

            rows = [{
                "id": "h1",
                "task_id": "task-a",
                "task_name": "Alpha",
                "status": "success",
                "message": "needle in message",
            }]
            paths.TASK_HISTORY_FILE.write_text(json.dumps(rows), encoding="utf-8")
            self.assertEqual(models.query_task_history(q="needle")[1], 1)

            with models.TASK_HISTORY_INDEX_LOCK:
                connection = sqlite3.connect(str(paths.TASK_HISTORY_INDEX_FILE))
                try:
                    connection.execute("DROP TABLE history_ngrams")
                    connection.commit()
                finally:
                    connection.close()

            items, total = models.query_task_history(q="needle")
            self.assertEqual((items[0]["id"], total), ("h1", 1))
            self.assertTrue(any("history_ngrams_ngram_seq" in detail for detail in models.explain_task_history_query(q="needle")))

    def test_concurrent_reads_and_writes_are_bounded_and_error_free(self):
        with isolated_models():
            from fls_manager import models, paths

            paths.TASK_HISTORY_FILE.write_text("[]", encoding="utf-8")
            errors = []

            def reader():
                try:
                    for _ in range(20):
                        models.query_task_history(q="message", limit=5)
                except Exception as exc:
                    errors.append(exc)

            def writer():
                try:
                    for index in range(20):
                        models.add_task_history({
                            "id": f"concurrent-{index}",
                            "task_id": "task-a",
                            "status": "success",
                            "message": f"message-{index}",
                        })
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=reader) for _ in range(3)]
            threads.append(threading.Thread(target=writer))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(models.query_task_history()[1], 20)

    def test_benchmark_has_fixed_ten_thousand_row_bound_and_lock_wrapper(self):
        benchmark = load_benchmark_module()
        self.assertEqual(benchmark.DEFAULT_ROWS, 10_000)
        self.assertEqual(benchmark.MAX_ROWS, 10_000)
        self.assertEqual(benchmark.P95_THRESHOLDS_MS["export"], 125.0)
        report = {
            "measurements": [{
                "scenario": "export",
                "errors": 0,
                "rss_peak_mb": 64,
                "p95_ms": 124.9,
            }],
        }
        self.assertTrue(benchmark.validate_thresholds(report))
        report["measurements"][0]["p95_ms"] = 125.1
        with self.assertRaises(RuntimeError):
            benchmark.validate_thresholds(report)
        with tempfile.TemporaryDirectory(prefix="fls-benchmark-fixture-test-") as temp_dir:
            self.assertEqual(benchmark._write_fixture(temp_dir)["history_rows"], 10_000)
            with self.assertRaises(ValueError):
                benchmark._write_fixture(temp_dir, row_count=1_000_000)
        wrapper = (ROOT / "tools" / "run_performance_benchmark.sh").read_text(encoding="utf-8")
        self.assertIn("flock -w 600 /tmp/nonebot-project-validation.lock", wrapper)
        self.assertEqual(benchmark._percentile([1, 2, 3, 4], 50), 2.5)


if __name__ == "__main__":
    unittest.main()
