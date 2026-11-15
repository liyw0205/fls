#!/usr/bin/env python3
"""Bounded local benchmark for the high-frequency FLS read and mutation paths.

Run this file through tools/run_performance_benchmark.sh so the global validation
lock is acquired before the fixture or application is created.
"""

import argparse
import contextlib
import json
import os
import resource
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROWS = 10_000
MAX_ROWS = 10_000
DEFAULT_TASKS = 100
DEFAULT_ITERATIONS = 12
MAX_ITERATIONS = 40
TOKEN = "performance-benchmark-token"
P95_THRESHOLDS_MS = {
    "dashboard": 50.0,
    "dashboard_runtime": 20.0,
    "list_shallow": 5.0,
    "pagination_deep": 8.0,
    "search": 15.0,
    "detail": 5.0,
    "status": 5.0,
    "toggle": 15.0,
    "export": 125.0,
}
RSS_THRESHOLD_MB = 128.0


def _purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def _shutdown_fls_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass


def _write_fixture(base_dir, row_count=DEFAULT_ROWS, task_count=DEFAULT_TASKS):
    """Create only the bounded synthetic fixture owned by this benchmark."""
    if row_count != DEFAULT_ROWS:
        raise ValueError(f"row_count must be exactly {DEFAULT_ROWS}")
    if not 1 <= task_count <= DEFAULT_TASKS:
        raise ValueError(f"task_count must be between 1 and {DEFAULT_TASKS}")

    data_dir = Path(base_dir) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    for index in range(task_count):
        task_id = f"bench-task-{index:04d}"
        tasks.append({
            "id": task_id,
            "name": f"Synthetic Task {index:04d}",
            "remark": "bounded performance fixture",
            "command": "echo synthetic",
            "cron": "",
            "config_path": "",
            "collection_id": "",
            "enabled": True,
            "env": {},
            "proxy_id": "",
            "notify": {"mode": "none", "ids": []},
            "random_delay": {"mode": "none", "seconds": 0},
            "retry": {"attempts": 0, "interval_seconds": 60},
            "run_count": 0,
            "pinned": False,
            "created_at": "2026-11-15 00:00:00",
            "updated_at": "2026-11-15 00:00:00",
        })

    statuses = ("success", "failed", "timeout", "stopped", "unknown")
    history = []
    for index in range(row_count):
        task_index = index % task_count
        task_id = f"bench-task-{task_index:04d}"
        history.append({
            "id": f"bench-history-{index:06d}",
            "task_id": task_id,
            "task_name": f"Synthetic Task {task_index:04d}",
            "command": "echo synthetic",
            "status": statuses[index % len(statuses)],
            "start_at": f"2026-11-15 00:{index // 60:02d}:{index % 60:02d}",
            "end_at": f"2026-11-15 00:{index // 60:02d}:{(index % 60 + 1) % 60:02d}",
            "duration_seconds": index % 17,
            "return_code": 0 if index % len(statuses) == 0 else 1,
            "source": "benchmark",
            "retry_attempt": index % 3,
            "max_retries": 2,
            "message": f"synthetic message {index:06d}",
            "log_file": "",
        })

    (data_dir / "tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False), encoding="utf-8"
    )
    (data_dir / "task_history.json").write_text(
        json.dumps(history, ensure_ascii=False), encoding="utf-8"
    )
    return {"history_rows": len(history), "task_rows": len(tasks)}


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * (percentile / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _rss_mb(value):
    # Linux reports ru_maxrss in KiB; keep a fallback for macOS runners.
    divisor = 1024 if sys.platform.startswith("linux") else 1024 * 1024
    return float(value) / divisor


def _measure(client, name, method, path, iterations):
    for _ in range(2):
        response = getattr(client, method)(path, headers={"X-Token": TOKEN})
        response.get_data()

    durations = []
    response_sizes = []
    errors = 0
    statuses = {}
    before_cpu = resource.getrusage(resource.RUSAGE_SELF)
    before_wall = time.perf_counter()
    for _ in range(iterations):
        started = time.perf_counter()
        try:
            response = getattr(client, method)(path, headers={"X-Token": TOKEN})
            body = response.get_data()
            status_code = int(response.status_code)
        except Exception:
            errors += 1
            durations.append((time.perf_counter() - started) * 1000)
            response_sizes.append(0)
            continue
        durations.append((time.perf_counter() - started) * 1000)
        response_sizes.append(len(body))
        statuses[str(status_code)] = statuses.get(str(status_code), 0) + 1
        if status_code >= 400:
            errors += 1

    wall_seconds = max(time.perf_counter() - before_wall, 1e-9)
    after_cpu = resource.getrusage(resource.RUSAGE_SELF)
    cpu_seconds = max(
        0.0,
        (after_cpu.ru_utime - before_cpu.ru_utime)
        + (after_cpu.ru_stime - before_cpu.ru_stime),
    )
    return {
        "path": path,
        "method": method.upper(),
        "samples": len(durations),
        "p50_ms": round(_percentile(durations, 50), 3),
        "p95_ms": round(_percentile(durations, 95), 3),
        "p99_ms": round(_percentile(durations, 99), 3),
        "throughput_req_s": round(iterations / wall_seconds, 3),
        "errors": errors,
        "error_rate": round(errors / max(iterations, 1), 6),
        "cpu_seconds": round(cpu_seconds, 6),
        "cpu_percent_of_wall": round(cpu_seconds / wall_seconds * 100, 2),
        "rss_peak_mb": round(_rss_mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss), 3),
        "response_bytes_avg": round(statistics.mean(response_sizes), 1) if response_sizes else 0,
        "response_bytes_max": max(response_sizes, default=0),
        "http_statuses": statuses,
        "scenario": name,
    }


def _run_playwright(app, base_dir):
    """Run two bounded browser checks without requiring Playwright in CI."""
    try:
        from playwright.sync_api import sync_playwright
        from werkzeug.serving import make_server
    except Exception as exc:
        return {"status": "skipped", "reason": f"Playwright unavailable: {exc}"}

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                return {"status": "skipped", "reason": f"Chromium unavailable: {exc}"}
            try:
                context = browser.new_context(extra_http_headers={"X-Token": TOKEN})
                page = context.new_page()
                with page.expect_response(
                    lambda response: response.url.endswith("/api/dashboard/runtime")
                ) as runtime_response:
                    page.goto(f"{url}/", wait_until="domcontentloaded")
                runtime = runtime_response.value
                dashboard_ok = page.locator("#flsDashboardSystemCpu").count() == 1
                runtime_ok = (
                    runtime.status == 200
                    and "system_cpu_percent" in runtime.json()
                )
                page.goto(f"{url}/history?page=1", wait_until="domcontentloaded")
                page.locator("#history-search").fill("Synthetic Task 0001")
                page.locator("#history-filters button[type='submit']").first.click()
                page.wait_for_load_state("domcontentloaded")
                history_ok = page.locator("#history-results").count() == 1
                page.goto(f"{url}/log/bench-task-0001", wait_until="domcontentloaded")
                detail_ok = page.locator("#task-log-history").count() == 1
                return {
                    "status": (
                        "passed"
                        if dashboard_ok and runtime_ok and history_ok and detail_ok
                        else "failed"
                    ),
                    "dashboard_cpu_rendered": dashboard_ok,
                    "dashboard_runtime_contract": runtime_ok,
                    "history_search_rendered": history_ok,
                    "detail_rendered": detail_ok,
                }
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def validate_thresholds(report):
    failures = []
    for item in report.get("measurements", []):
        scenario = item.get("scenario")
        if item.get("errors", 0):
            failures.append(f"{scenario}: errors={item['errors']}")
        if float(item.get("rss_peak_mb", 0)) > RSS_THRESHOLD_MB:
            failures.append(f"{scenario}: rss_peak_mb={item['rss_peak_mb']}")
        threshold = P95_THRESHOLDS_MS.get(scenario)
        if threshold is not None and float(item.get("p95_ms", 0)) > threshold:
            failures.append(f"{scenario}: p95_ms={item['p95_ms']} > {threshold}")
    if failures:
        raise RuntimeError("performance regression thresholds failed: " + "; ".join(failures))
    return True


def run_benchmark(mode="optimized", row_count=DEFAULT_ROWS, iterations=DEFAULT_ITERATIONS, run_browser=False, check=False, only=None):
    if row_count != DEFAULT_ROWS or row_count > MAX_ROWS:
        raise ValueError(f"This benchmark is fixed at exactly {DEFAULT_ROWS} rows")
    if not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError(f"iterations must be between 1 and {MAX_ITERATIONS}")
    if mode not in ("baseline", "optimized"):
        raise ValueError("mode must be baseline or optimized")
    valid_scenarios = (
        None,
        "dashboard",
        "dashboard_runtime",
        "list_shallow",
        "pagination_deep",
        "search",
        "detail",
        "status",
        "toggle",
        "export",
    )
    if only not in valid_scenarios:
        raise ValueError("only must name a known benchmark scenario")

    old_env = {
        key: os.environ.get(key)
        for key in ("FLS_BASE_DIR", "FLS_TOKEN", "FLS_SECRET_KEY", "FLS_DISABLE_HISTORY_INDEX")
    }
    try:
        with tempfile.TemporaryDirectory(prefix="fls-performance-10k-") as temp_dir:
            os.environ["FLS_BASE_DIR"] = temp_dir
            os.environ["FLS_TOKEN"] = TOKEN
            os.environ["FLS_SECRET_KEY"] = "performance-benchmark-secret"
            if mode == "baseline":
                os.environ["FLS_DISABLE_HISTORY_INDEX"] = "1"
            else:
                os.environ.pop("FLS_DISABLE_HISTORY_INDEX", None)
            _purge_fls_modules()
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from fls_manager.app import create_app
            from fls_manager.models import explain_task_history_query

            fixture = _write_fixture(temp_dir, row_count=row_count)
            # Keep scheduler diagnostics out of the machine-readable benchmark result.
            with contextlib.redirect_stdout(sys.stderr):
                app = create_app()
                app.config.update(TESTING=True)
                client = app.test_client()
                routes = [
                    ("dashboard", "get", "/"),
                    ("dashboard_runtime", "get", "/api/dashboard/runtime"),
                    ("list_shallow", "get", "/history?page=1"),
                    ("pagination_deep", "get", "/history?page=250"),
                    ("search", "get", "/history?q=Synthetic%20Task%200001&page=1"),
                    ("detail", "get", "/log/bench-task-0001"),
                    ("status", "get", "/api/status"),
                    ("toggle", "post", "/api/task/action/toggle/bench-task-0001"),
                    ("export", "get", "/history/export"),
                ]
                if only:
                    routes = [route for route in routes if route[0] == only]
                measurements = [
                    _measure(client, name, method, path, iterations)
                    for name, method, path in routes
                ]
                explain = {
                    "status": explain_task_history_query(status="failed"),
                    "task": explain_task_history_query(task_id="bench-task-0001"),
                    "search": explain_task_history_query(q="Synthetic Task 0001"),
                    "export": explain_task_history_query(),
                    "dashboard_abnormal": explain_task_history_query(
                        statuses=("failed", "timeout", "start_failed", "unknown"),
                    ),
                }
                browser = _run_playwright(app, temp_dir) if run_browser else {"status": "not_requested"}
            result = {
                "benchmark": "fls-high-frequency-routes",
                "mode": mode,
                "only": only,
                "fixture": fixture,
                "iterations": iterations,
                "temporary_fixture": temp_dir,
                "lock": "flock -w 600 /tmp/nonebot-project-validation.lock",
                "measurements": measurements,
                "explain_query_plan": explain,
                "playwright": browser,
                "thresholds": {
                    "p95_ms": P95_THRESHOLDS_MS,
                    "rss_peak_mb": RSS_THRESHOLD_MB,
                    "errors": 0,
                },
            }
            if check and mode == "optimized":
                validate_thresholds(result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result
    finally:
        _shutdown_fls_scheduler()
        _purge_fls_modules()
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "optimized"), default="optimized")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--playwright", action="store_true")
    parser.add_argument("--check", action="store_true", help="enforce optimized p95/RSS/error thresholds")
    parser.add_argument(
        "--only",
        choices=(
            "dashboard",
            "dashboard_runtime",
            "list_shallow",
            "pagination_deep",
            "search",
            "detail",
            "status",
            "toggle",
            "export",
        ),
        help="measure one scenario while keeping the standard bounded fixture",
    )
    args = parser.parse_args(argv)
    run_benchmark(args.mode, args.rows, args.iterations, args.playwright, args.check, args.only)


if __name__ == "__main__":
    main()
