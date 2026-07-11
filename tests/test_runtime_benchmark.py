from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_runtime.py"
    spec = importlib.util.spec_from_file_location("benchmark_runtime_script", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_percentile_uses_nearest_rank():
    benchmark = _load_script()

    assert benchmark.percentile([4.0], 95) == 4.0
    assert benchmark.percentile(list(range(1, 21)), 95) == 19.0
    with pytest.raises(ValueError, match="no samples"):
        benchmark.percentile([], 95)


@pytest.mark.asyncio
async def test_run_case_discards_warmups_and_checks_p95():
    benchmark = _load_script()
    values = iter([100.0, 1.0, 3.0, 2.0])

    async def measure():
        return benchmark.Sample(next(values), event_count=7)

    result = await benchmark.run_case(
        benchmark.BenchmarkCase("fake", measure),
        warmups=1,
        samples=3,
        threshold_ms=2.5,
    )

    assert result.samples_ms == [1.0, 3.0, 2.0]
    assert result.median_ms == 2.0
    assert result.p95_ms == 3.0
    assert result.event_counts == [7, 7, 7]
    assert result.passed is False


def test_json_output_is_versioned_and_contains_distributions():
    benchmark = _load_script()
    result = benchmark.BenchmarkResult(
        name="fake",
        samples_ms=[1.0, 2.0],
        median_ms=1.5,
        p95_ms=2.0,
        min_ms=1.0,
        max_ms=2.0,
        event_counts=[3, 3],
    )

    payload = json.loads(benchmark.format_json([result], warmups=2, samples=2))

    # Schema v2: metrics are a name-keyed map, not a positional array.
    assert payload["schema_version"] == 2
    assert payload["warmups"] == 2
    assert payload["samples"] == 2
    assert "results" not in payload
    assert payload["metrics"]["fake"]["samples_ms"] == [1.0, 2.0]
    assert payload["metrics"]["fake"]["correct"] is True


@pytest.mark.asyncio
async def test_no_tool_case_runs_offline_and_validates_events(tmp_path):
    benchmark = _load_script()
    case = benchmark.build_cases(tmp_path)["no_tool_stream_200_deltas"]

    result = await benchmark.run_case(case, warmups=0, samples=1, threshold_ms=None)

    assert result.correct is True
    assert len(result.event_counts) == 1
    assert result.event_counts[0] > 200


@pytest.mark.asyncio
async def test_trivial_sqlite_case_runs_offline(tmp_path):
    benchmark = _load_script()
    case = benchmark.build_cases(tmp_path)["sqlite_trivial_operation"]

    try:
        result = await benchmark.run_case(case, warmups=1, samples=2, threshold_ms=None)
    finally:
        await case.close()

    assert result.correct is True
    assert len(result.samples_ms) == 2
    assert result.event_counts == []


@pytest.mark.asyncio
async def test_run_store_50_event_writes_validates_persisted_order(tmp_path):
    benchmark = _load_script()
    case = benchmark.build_cases(tmp_path)["run_store_50_event_writes"]

    try:
        result = await benchmark.run_case(case, warmups=1, samples=1, threshold_ms=250.0)
    finally:
        await case.close()

    assert benchmark.DEFAULT_THRESHOLDS_MS[case.name] == 250.0
    assert result.correct is True
    assert result.event_counts == [50]
    assert len(result.samples_ms) == 1


def test_new_cases_registered(tmp_path):
    benchmark = _load_script()
    cases = benchmark.build_cases(tmp_path)

    assert "memory_100k_search" in cases
    assert "scheduler_maximal_16_read_tools" in cases
    assert "session_churn_25" in cases
    # The 100k memory scan is observational (no absolute gate); it validates
    # ranking correctness, not a sub-20ms latency the scan can't hit in CPython.
    assert "memory_100k_search" not in benchmark.DEFAULT_THRESHOLDS_MS
    # Existing SQLite gates are retained.
    assert benchmark.DEFAULT_THRESHOLDS_MS["durable_sqlite_8_read_tools"] == 25.0
    assert benchmark.DEFAULT_THRESHOLDS_MS["durable_sqlite_32_read_tools"] == 70.0


def test_parser_accepts_threshold_override():
    benchmark = _load_script()
    parser = benchmark.build_parser(("fake",))

    args = parser.parse_args(
        ["--warmups", "0", "--samples", "1", "--case", "fake", "--threshold", "fake=2.5"]
    )

    assert args.warmups == 0
    assert args.samples == 1
    assert args.cases == ["fake"]
    assert args.threshold == [("fake", 2.5)]
