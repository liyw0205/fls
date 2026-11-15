#!/usr/bin/env bash
set -euo pipefail

if [ "${FLS_SEARCH_COMPARE_LOCKED:-0}" != "1" ]; then
    exec flock -w 600 /tmp/nonebot-project-validation.lock \
        env FLS_SEARCH_COMPARE_LOCKED=1 bash "$0" "$@"
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASELINE_REF="${FLS_SEARCH_BASELINE_REF:-v20261116.1}"
ITERATIONS=40
PYTHON="/home/nonebot-shared-venv/bin/python"
TMP_DIR="$(mktemp -d /tmp/fls-search-compare.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

command -v git >/dev/null 2>&1 || { printf 'git is required\n' >&2; exit 1; }
[ -x "$PYTHON" ] || { printf 'shared Python is unavailable: %s\n' "$PYTHON" >&2; exit 1; }
git -C "$ROOT" rev-parse --verify "$BASELINE_REF^{commit}" >/dev/null

BASELINE_DIR="$TMP_DIR/baseline"
mkdir -p "$BASELINE_DIR"
git -C "$ROOT" archive "$BASELINE_REF" | tar -x -C "$BASELINE_DIR"
cp "$ROOT/tools/benchmark_performance.py" "$BASELINE_DIR/tools/benchmark_performance.py"

printf '[before] revision=%s\n' "$(git -C "$ROOT" rev-parse --short "$BASELINE_REF")" >&2
"$PYTHON" "$BASELINE_DIR/tools/benchmark_performance.py" \
    --mode optimized --only search_suite --iterations "$ITERATIONS" --check > "$TMP_DIR/before.json"

printf '[after] working-tree implementation\n' >&2
"$PYTHON" "$ROOT/tools/benchmark_performance.py" \
    --mode optimized --only search_suite --iterations "$ITERATIONS" --check > "$TMP_DIR/after.json"

"$PYTHON" - "$TMP_DIR/before.json" "$TMP_DIR/after.json" "$BASELINE_REF" <<'PY'
import json
import sys
from pathlib import Path


before = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
after = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
baseline_ref = sys.argv[3]

if before["fixture"] != after["fixture"] or before["fixture"]["history_rows"] != 10000:
    raise SystemExit("comparison fixtures differ or are not exactly 10,000 history rows")
if before["fixture_sha256"] != after["fixture_sha256"]:
    raise SystemExit("comparison fixture SHA-256 values differ")

before_cases = before["search_correctness"]["cases"]
after_cases = after["search_correctness"]["cases"]
if not before["search_correctness"]["all_matched"] or not after["search_correctness"]["all_matched"]:
    raise SystemExit("a search implementation differed from its JSON source fallback")
if before_cases.keys() != after_cases.keys():
    raise SystemExit("search correctness cases differ")
for name in before_cases:
    if before_cases[name]["result_sha256"] != after_cases[name]["result_sha256"]:
        raise SystemExit(f"baseline and optimized search results differ: {name}")

before_measurements = {item["scenario"]: item for item in before["measurements"]}
after_measurements = {item["scenario"]: item for item in after["measurements"]}
if before_measurements.keys() != after_measurements.keys():
    raise SystemExit("benchmark scenarios differ")

scenarios = []
for name, old in before_measurements.items():
    new = after_measurements[name]
    contract_equal = all(
        old[key] == new[key]
        for key in ("http_statuses", "response_bytes_avg", "response_bytes_max", "response_body_sha256")
    )
    if old["errors"] or new["errors"]:
        raise SystemExit(f"benchmark HTTP errors for {name}: {old['errors']} -> {new['errors']}")
    if not contract_equal:
        raise SystemExit(f"response contract differs between baseline and current: {name}")
    scenarios.append({
        "scenario": name,
        "before": {key: old[key] for key in (
            "p50_ms", "p95_ms", "p99_ms", "throughput_req_s", "rss_peak_mb",
            "cpu_seconds", "cpu_percent_of_wall", "response_bytes_avg",
            "response_bytes_max", "errors",
        )},
        "after": {key: new[key] for key in (
            "p50_ms", "p95_ms", "p99_ms", "throughput_req_s", "rss_peak_mb",
            "cpu_seconds", "cpu_percent_of_wall", "response_bytes_avg",
            "response_bytes_max", "errors",
        )},
        "response_contract_equal": contract_equal,
    })

summary = {
    "baseline_ref": baseline_ref,
    "baseline_revision": baseline_ref,
    "fixture_sha256": before["fixture_sha256"],
    "fixture": before["fixture"],
    "iterations": before["iterations"],
    "correctness": {
        name: {
            "total": before_cases[name]["total"],
            "result_sha256": before_cases[name]["result_sha256"],
            "baseline_and_current_equal": True,
        }
        for name in before_cases
    },
    "index_cost": {
        "temporary_fixture_disk_bytes_before": before["temporary_fixture_disk_bytes"],
        "temporary_fixture_disk_bytes_after": after["temporary_fixture_disk_bytes"],
        "history_index_bytes_before": before["history_index_bytes"],
        "history_index_bytes_after": after["history_index_bytes"],
        "history_index_build_ms_before": before["history_index_build_ms"],
        "history_index_build_ms_after": after["history_index_build_ms"],
        "ngram_rows_before": before["history_index_gram_rows"],
        "ngram_rows_after": after["history_index_gram_rows"],
    },
    "search_diagnostics_before": before["search_diagnostics"],
    "search_diagnostics_after": after["search_diagnostics"],
    "explain_before": before["explain_query_plan"],
    "explain_after": after["explain_query_plan"],
    "scenarios": scenarios,
    "lock": before["lock"],
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
PY
