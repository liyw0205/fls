#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK_PATH=/tmp/nonebot-project-validation.lock
PYTHON=/home/nonebot-shared-venv/bin/python
MODE=all
ITERATIONS=40
LOCKED=0

if [ "${1:-}" = --locked ]; then
  if [ "$#" -ne 3 ]; then
    printf '%s\n' 'internal fast-regression invocation error' >&2
    exit 2
  fi
  shift
  MODE=$1
  ITERATIONS=$2
  LOCKED=1
else
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --all) MODE=all ;;
      --tests-only) MODE=tests ;;
      --benchmark-only) MODE=benchmark ;;
      --iterations)
        shift
        if [ "$#" -eq 0 ] || ! [[ "$1" =~ ^[0-9]+$ ]] || [ "$1" -lt 1 ] || [ "$1" -gt 40 ]; then
          printf '%s\n' '--iterations must be between 1 and 40' >&2
          exit 2
        fi
        ITERATIONS=$1
        ;;
      -h|--help)
        cat <<'EOF'
Usage: tools/run_fast_regression.sh [--all|--tests-only|--benchmark-only] [--iterations N]

Runs the bounded task/log correctness regression and/or the task-log before/after
response comparison under /tmp/nonebot-project-validation.lock. The comparison uses
10,000 history rows and a 7.68MB task log, and defaults to 40 requests per implementation.
EOF
        exit 0
        ;;
      *)
        printf 'unknown option: %s\n' "$1" >&2
        exit 2
        ;;
    esac
    shift
  done
fi

if [ "$LOCKED" -ne 1 ]; then
  exec flock -w 600 "$LOCK_PATH" "$0" --locked "$MODE" "$ITERATIONS"
fi

TESTS=(
  "$ROOT/tests/test_storage_notify_proxy.py::SensitiveTextTests"
  "$ROOT/tests/test_storage_notify_proxy.py::LogBoundaryTests"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_api_logfile_rejects_symlink_outside_log_directory"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_api_logfile_invalid_lines_returns_text_error_without_reading_file"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_task_log_invalid_lines_returns_400_without_reading"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_debug_log_invalid_lines_returns_400_without_mutating_record"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_task_debug_and_file_log_apis_return_bounded_masked_tail"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_all_log_apis_reject_invalid_lines_before_reading"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_all_log_apis_keep_defaults_and_clamp_valid_line_counts"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_task_log_page_keeps_history_table_actions_and_safe_back"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_task_log_history_skips_unused_total_count"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_online_install_log_api_returns_stable_polling_fields"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_online_install_log_api_missing_returns_stable_polling_fields"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_online_install_log_api_invalid_lines_returns_json_error"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_about_job_log_invalid_lines_returns_json_error"
  "$ROOT/tests/test_ui_route_components.py::UiRouteComponentTests::test_deps_install_log_invalid_lines_returns_json_error"
)

if [ "$MODE" = all ] || [ "$MODE" = tests ]; then
  printf '%s\n' '== Task/log correctness regression =='
  TIMEFORMAT='fast-regression phase=correctness elapsed=%3R seconds'
  time "$PYTHON" -m pytest -q "${TESTS[@]}"
fi

if [ "$MODE" = all ] || [ "$MODE" = benchmark ]; then
  printf '%s\n' '== Task-log tail before/after response benchmark =='
  TIMEFORMAT='fast-regression phase=benchmark elapsed=%3R seconds'
  time "$PYTHON" "$ROOT/tools/benchmark_performance.py" \
    --compare-task-log-tail --iterations "$ITERATIONS" --check
fi
