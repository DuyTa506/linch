from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest
from test_runtime_benchmark import _load_script


def test_scale_suite_is_explicit_and_separate_from_default_cases(tmp_path):
    benchmark = _load_script()
    args = benchmark.build_parser(("legacy",)).parse_args(["--scale", "--json"])
    assert args.scale is True
    assert args.scale_timeout == 600
    cases = benchmark.build_scale_cases()
    assert {(case.backend, case.sessions, case.turns) for case in cases.values()} == {
        ("memory", 200, 4),
        ("memory", 1000, 4),
        ("memory", 1, 50),
        ("postgres", 200, 4),
        ("postgres", 1000, 4),
        ("postgres", 1, 50),
    }
    assert not set(cases).intersection(benchmark.build_cases(tmp_path))


def test_postgres_scale_contract_and_metrics_are_explicit(monkeypatch):
    benchmark = _load_script()
    monkeypatch.delenv("AGENT_KIT_TEST_PG_DSN", raising=False)
    case = benchmark.ScaleCase("scale_postgres_1x50", "postgres", 1, 50)
    assert benchmark.scale_case_skip_reason(case) == "AGENT_KIT_TEST_PG_DSN not set"
    metrics = benchmark.ScaleStoreMetrics()
    metrics.writes["run", "save_checkpoint", 0] = 2
    metrics.payloads[0, "provider_pending"].append(17)
    metrics.event_payloads[0].append(12)
    metrics.session_payloads[0].append(11)
    metrics.sql_round_trips["run", 0] = 4
    metrics.transaction_commands["run", 0] = 1
    metrics.pool_reset_commands["run", 0] = 1
    metrics.protocol_exchanges["run", 0, "bind_execute"] = 2
    writes, payloads = metrics.summaries(turns=1)
    summary = metrics.round_trip_summary(turns=1)["by_store"]["run"]
    assert writes["by_store"]["run"]["save_checkpoint"] == 2
    assert payloads["total"] == 17
    assert payloads["event_bytes"]["total"] == 12
    assert payloads["session_append_bytes"]["total"] == 11
    assert summary["application_statements"] == 2
    assert summary["transaction_commands"] == 1
    assert summary["pool_reset_commands"] == 1
    assert summary["protocol_exchanges"] == 2


def test_scale_schema_names_are_owned_and_bounded():
    benchmark = _load_script()
    prefix = benchmark._new_scale_schema_prefix()
    case = benchmark.ScaleCase("scale_postgres_1000x4", "postgres", 1000, 4)
    name = benchmark._scale_schema_name(prefix, case)
    assert benchmark._owned_scale_schema(name, prefix)
    assert len(name) <= 63
    assert not benchmark._owned_scale_schema(prefix[:-1] + "other", prefix)
    with pytest.raises(ValueError):
        benchmark._scale_schema_name("linch_scale_bad_", case)


@pytest.mark.asyncio
async def test_scale_provider_derives_each_session_turn_from_request_history():
    benchmark = _load_script()
    provider = benchmark.ScaleProvider(turns=4, delay=0)

    async def response(turn):
        request = SimpleNamespace(
            model="gpt-5",
            messages=[SimpleNamespace(role="assistant") for _ in range(turn)],
        )
        return [item async for item in provider.stream(request)]

    first, other_first = await asyncio.gather(response(0), response(0))
    assert first == other_first
    assert [item["name"] for item in first if item["type"] == "tool_use_start"] == [
        "rag_search",
        "sql_query",
    ]
    for turn, names in ((1, ["rag_search"]), (2, ["save_note"]), (3, [])):
        events = await response(turn)
        assert [item["name"] for item in events if item["type"] == "tool_use_start"] == names
    assert (await response(3))[-2] == {"type": "text_delta", "text": "scale:ok"}


@pytest.mark.parametrize("backend", ["memory"])
@pytest.mark.asyncio
async def test_scale_smoke_uses_one_shared_store_and_validates_five_runs(tmp_path, backend):
    benchmark = _load_script()
    progress = tmp_path / "progress.json"
    result = await benchmark.run_scale_case(
        benchmark.ScaleCase(f"smoke_{backend}", backend, sessions=5, turns=4),
        tmp_path / backend,
        progress_path=progress,
    )

    assert result["correct"] is True
    assert result["completed"] == 5
    assert result["failed"] == 0
    assert result["errors"] == []
    assert result["observed_provider_turns"] == 20
    assert result["observed_tool_calls"] == 20
    assert result["parallel_first_read_pairs"] == 5
    assert result["run_latency_ms"]["p50"] > 0
    assert result["run_latency_ms"]["p99"] >= result["run_latency_ms"]["p50"]
    assert result["runs_per_second"] > 0
    assert result["store_writes"]["by_store"]["session"]["create"] == 5
    assert result["store_writes"]["by_store"]["run"]["create_run"] == 5
    assert result["store_writes"]["by_store"]["run"]["save_checkpoint_with_events"] >= 5
    assert result["store_writes"]["total"] == sum(
        row["total"] for row in result["store_writes"]["by_turn"]
    )
    checkpoint = result["checkpoint_bytes"]
    assert checkpoint["total"] > 0
    assert checkpoint["total"] == sum(row["total"] for row in checkpoint["by_turn"])
    assert checkpoint["by_phase"]["completed"]["count"] == 5
    assert [row["turn"] for row in checkpoint["by_turn"]] == [-1, 0, 1, 2, 3]
    assert result["event_loop_lag_ms"]["interval"] == 10
    assert result["event_loop_lag_ms"]["samples"] > 0
    assert result["rss"]["peak_delta_bytes_per_session"] >= 0
    assert result["rss"]["initial_high_water_bytes"] > 0
    assert json.loads(progress.read_text())["completed"] == 5


@pytest.mark.asyncio
async def test_scale_subprocess_hard_deadline_reports_observed_work():
    benchmark = _load_script()
    started = time.monotonic()
    result = await asyncio.wait_for(
        benchmark.run_scale_subprocess(
            benchmark.ScaleCase("timeout_smoke", "memory", sessions=1, turns=4), timeout_s=0.001
        ),
        timeout=0.5,
    )
    assert time.monotonic() - started < 5
    assert result["timed_out"] is True
    assert result["correct"] is False
    assert result["completed"] == 0
    assert "timeout" in result["errors"][0].lower()


@pytest.mark.asyncio
async def test_measured_store_preserves_capabilities_and_counts_terminal_payloads(pg_dsn):
    benchmark = _load_script()
    import asyncpg

    from linch import PostgresRunStore, RunCheckpoint
    from linch.types import Message, ProviderRequest, Usage

    metrics = benchmark.ScaleStoreMetrics()
    inner = PostgresRunStore(pg_dsn)
    connection = await asyncpg.connect(pg_dsn)
    store = benchmark.MeasuredStore(inner, "run", metrics)
    try:
        run = await store.create_run(
            "s", initial_checkpoint=RunCheckpoint("started", "héllo", 0, Usage())
        )
        raw = await connection.fetchval("SELECT checkpoint FROM runs WHERE id = $1", run.id)
        initial_bytes = len(raw.encode("utf-8"))
        assert metrics.payloads[-1, "started"] == [initial_bytes]
        checkpoint = RunCheckpoint("user_appended", "héllo", 0, Usage())
        await store.save_checkpoint(run.id, checkpoint)
        raw = await connection.fetchval("SELECT checkpoint FROM runs WHERE id = $1", run.id)
        first_bytes = len(raw.encode("utf-8"))
        request = ProviderRequest(
            model="gpt-5", system=[], messages=[Message("user", [])], tools=[]
        )
        snapshot = await store.save(run.id, 1, request)
        assert (await store.load(snapshot.id)).id == snapshot.id
        await store.delete(snapshot.id, run_id=run.id)
        assert await store.prune(run.id) == 0
        await store.mark_completed(run.id, checkpoint)
        raw = await connection.fetchval("SELECT checkpoint FROM runs WHERE id = $1", run.id)
        terminal_bytes = len(raw.encode("utf-8"))
        other = await store.create_run("failed")
        await store.mark_failed(other.id, checkpoint)
        raw = await connection.fetchval("SELECT checkpoint FROM runs WHERE id = $1", other.id)
        failed_bytes = len(raw.encode("utf-8"))
        await store.mark_failed(other.id)
        with pytest.raises(AttributeError):
            _ = store.not_a_capability
        with pytest.raises(KeyError):
            await store.save_checkpoint("missing", checkpoint)
    finally:
        await store.close()
        await connection.close()

    writes, payloads = metrics.summaries(turns=2)
    assert writes["by_store"]["run"] == {
        "create_run": 2,
        "save_checkpoint": 1,
        "save": 1,
        "delete": 1,
        "prune": 1,
        "mark_completed": 1,
        "mark_failed": 2,
    }
    assert writes["total"] == 9
    assert payloads["total"] == initial_bytes + first_bytes + terminal_bytes + 2 * failed_bytes
    assert payloads["by_phase"]["completed"]["total"] == terminal_bytes
    assert payloads["by_phase"]["failed"]["total"] == 2 * failed_bytes
    assert not hasattr(benchmark.MeasuredStore(object(), "run", metrics), "append_events")


@pytest.mark.asyncio
async def test_lag_sampler_records_final_blocking_burst():
    benchmark = _load_script()
    sampler = benchmark.LagSampler(interval=0.01)
    await sampler.start()
    time.sleep(0.04)
    result = await sampler.stop()
    assert result["max"] >= 25
    assert result["p99"] >= 25
    assert result["samples"] >= 1


@pytest.mark.asyncio
async def test_scale_subprocess_has_fresh_pid_and_reports_tiny_case():
    benchmark = _load_script()
    result = await benchmark.run_scale_subprocess(
        benchmark.ScaleCase("child_smoke", "memory", sessions=1, turns=4), timeout_s=30
    )
    assert result["correct"] is True
    assert result["pid"] != os.getpid()
    assert result["completed"] == 1
    assert result["timed_out"] is False


@pytest.mark.asyncio
async def test_postgres_protocol_and_schema_cleanup_are_classified(pg_dsn):
    benchmark = _load_script()
    import asyncpg

    prefix = benchmark._new_scale_schema_prefix()
    other_prefix = benchmark._new_scale_schema_prefix()
    owned, orphan = prefix + "orphan", other_prefix + "orphan"
    malformed = prefix + "bad-name"
    keep = "linch_scale_deadbeef_unrelated"
    connection = await asyncpg.connect(pg_dsn)
    try:
        for name in (owned, orphan, malformed, keep):
            await connection.execute(f'CREATE SCHEMA "{name}"')
    finally:
        await connection.close()
    metrics = benchmark.ScaleStoreMetrics()
    pool = await asyncpg.create_pool(
        pg_dsn,
        min_size=1,
        max_size=1,
        connection_class=benchmark._instrument_connection_class(metrics, "run"),
    )
    try:
        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1")
            async with connection.transaction():
                await connection.fetchval("SELECT 2")
        await benchmark._sweep_postgres_schemas(pg_dsn, prefix)
        check = await asyncpg.connect(pg_dsn)
        try:
            assert not await check.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = $1)", owned
            )
            for name in (orphan, malformed, keep):
                assert await check.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = $1)", name
                )
        finally:
            await check.close()
        summary = metrics.round_trip_summary(turns=1)["by_store"]["run"]
        assert summary["protocol_transaction_commands"] >= 2
        assert summary["pool_reset_commands"] == 1
        assert summary["protocol_exchanges"] >= 3
    finally:
        connection = await asyncpg.connect(pg_dsn)
        try:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{orphan}" CASCADE')
            await connection.execute(f'DROP SCHEMA IF EXISTS "{malformed}" CASCADE')
            await connection.execute(f'DROP SCHEMA IF EXISTS "{keep}" CASCADE')
        finally:
            await connection.close()
        await pool.close()


@pytest.mark.asyncio
async def test_scale_cli_runs_only_scale_cases(tmp_path, monkeypatch, capsys):
    benchmark = _load_script()
    tiny = benchmark.ScaleCase("tiny", "memory", sessions=1, turns=4)
    monkeypatch.setattr(benchmark, "build_scale_cases", lambda: {tiny.name: tiny})

    def legacy_cases_must_not_run(workdir):
        raise AssertionError("--scale invoked the legacy suite")

    monkeypatch.setattr(benchmark, "build_cases", legacy_cases_must_not_run)
    assert await benchmark.async_main(["--scale", "--scale-case", "tiny", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["suite"] == "scale"
    assert payload["schema_version"] == 3
    assert list(payload["metrics"]) == ["tiny"]
    assert payload["machine"]["python"]
    assert payload["command"]


@pytest.mark.parametrize(
    ("final_hwm", "expected_peak", "expected_delta"), [(200, 150, 50), (220, 220, 120)]
)
def test_rss_delta_uses_sampled_growth_below_import_high_water(
    final_hwm, expected_peak, expected_delta
):
    benchmark = _load_script()
    result = benchmark.scale_rss_summary(
        baseline=100, initial_hwm=200, sampled_peak=150, final_hwm=final_hwm, sessions=5
    )
    assert result["peak_rss_bytes"] == expected_peak
    assert result["peak_delta_bytes"] == expected_delta
    assert result["peak_delta_bytes_per_session"] == expected_delta / 5
    assert result["import_high_water_floor_bytes"] == 100


@pytest.mark.asyncio
async def test_scale_filter_requires_explicit_scale_opt_in(capsys):
    benchmark = _load_script()
    with pytest.raises(SystemExit) as error:
        await benchmark.async_main(["--scale-case", "scale_memory_200x4"])
    assert error.value.code == 2
    assert "requires --scale" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_long_scale_provider_has_unique_inputs_and_fifty_turn_termination():
    benchmark = _load_script()
    provider = benchmark.ScaleProvider(turns=50, delay=0)
    inputs = []
    for turn in range(50):
        request = SimpleNamespace(
            model="gpt-5", messages=[SimpleNamespace(role="assistant") for _ in range(turn)]
        )
        events = [item async for item in provider.stream(request)]
        inputs.extend(
            item["json_delta"] for item in events if item["type"] == "tool_use_input_delta"
        )
        assert events[-1]["stop_reason"] == ("end_turn" if turn == 49 else "tool_use")
    assert len(inputs) == len(set(inputs)) == 50


@pytest.mark.asyncio
async def test_cancelling_scale_parent_reaps_child(monkeypatch):
    import subprocess

    benchmark = _load_script()
    processes = []
    original = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", tracked_popen)
    task = asyncio.create_task(
        benchmark.run_scale_subprocess(
            benchmark.ScaleCase("cancel_smoke", "memory", sessions=5, turns=4), timeout_s=30
        )
    )
    for _ in range(200):
        if processes:
            break
        await asyncio.sleep(0.001)
    assert processes, "no child was created"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    try:
        assert processes[0].poll() is not None
    finally:
        # Do not leave a child behind when exercising the RED implementation.
        if processes[0].poll() is None:
            processes[0].kill()
            await asyncio.to_thread(processes[0].wait, timeout=2)
