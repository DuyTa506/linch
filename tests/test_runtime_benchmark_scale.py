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


def test_postgres_scale_cases_skip_without_dsn(monkeypatch):
    benchmark = _load_script()
    monkeypatch.delenv("AGENT_KIT_TEST_PG_DSN", raising=False)
    case = benchmark.ScaleCase("scale_postgres_1x50", "postgres", 1, 50)
    assert benchmark.scale_case_skip_reason(case) == "AGENT_KIT_TEST_PG_DSN not set"


def test_lean_profile_disables_repeated_payload_encoding(monkeypatch):
    monkeypatch.setenv("LINCH_SCALE_PROFILE_LEAN", "1")
    lean = _load_script()
    assert lean.ScaleStoreMetrics().capture_payload_bytes is False

    monkeypatch.delenv("LINCH_SCALE_PROFILE_LEAN")
    instrumented = _load_script()
    assert instrumented.ScaleStoreMetrics().capture_payload_bytes is True


def test_scale_metrics_separates_payload_bytes_and_sql_round_trips():
    benchmark = _load_script()
    metrics = benchmark.ScaleStoreMetrics()
    metrics.payloads[0, "provider_pending"].append(17)
    metrics.event_payloads[0].extend((5, 7))
    metrics.session_payloads[0].append(11)
    metrics.sql_round_trips["run", 0] = 9
    metrics.transaction_commands["run", 0] = 3
    metrics.pool_acquires["run", 0] = 3
    metrics.pool_releases["run", 0] = 3

    writes, checkpoint = metrics.summaries(turns=1)
    assert writes["total"] == 0
    assert checkpoint["total"] == 17
    assert checkpoint["event_bytes"]["per_provider_turn"] == 12
    assert checkpoint["session_append_bytes"]["per_provider_turn"] == 11
    assert metrics.round_trip_summary(turns=1)["by_store"]["run"] == {
        "round_trips": 9,
        "application_statements": 6,
        "transaction_commands": 3,
        "pool_reset_commands": 0,
        "pool_acquires": 3,
        "pool_releases": 3,
        "protocol_exchanges": 0,
        "protocol_exchanges_per_provider_turn": 0.0,
        "protocol_by_method": {},
        "protocol_transaction_commands": 0,
        "protocol_failures": 0,
        "per_provider_turn": 9.0,
        "definition": "round_trips counts supporting connection command/reset calls; "
        "protocol_exchanges counts attempted asyncpg protocol cycles (query, prepare, "
        "bind_execute, bind_execute_many, execute, and close_statement) after schema "
        "setup; transaction and pool-reset categories are broken out",
    }


def test_scale_metrics_counts_reset_calls_in_supporting_round_trips():
    benchmark = _load_script()
    metrics = benchmark.ScaleStoreMetrics()
    metrics.sql_round_trips["run", 0] = 4
    metrics.transaction_commands["run", 0] = 2
    metrics.pool_reset_commands["run", 0] = 1

    summary = metrics.round_trip_summary(turns=1)["by_store"]["run"]
    assert summary["round_trips"] == 4
    assert summary["application_statements"] == 1
    assert summary["transaction_commands"] == 2
    assert summary["pool_reset_commands"] == 1


@pytest.mark.asyncio
async def test_postgres_protocol_instrumentation_distinguishes_cold_cache_and_boundaries(pg_dsn):
    benchmark = _load_script()
    import asyncpg

    metrics = benchmark.ScaleStoreMetrics()
    pool = await asyncpg.create_pool(
        pg_dsn,
        min_size=1,
        max_size=1,
        connection_class=benchmark._instrument_connection_class(metrics, "run"),
    )
    wrapped = benchmark._CountingPool(
        pool, metrics, "run", benchmark._instrument_connection_protocol
    )
    try:
        metrics.reset_round_trip_metrics()
        async with wrapped.acquire() as connection:
            await connection.fetchval("SELECT $1::integer", 1)
            cold_prepare = sum(
                count
                for (store, _turn, method), count in metrics.protocol_exchanges.items()
                if store == "run" and method == "prepare"
            )
            cold_bind = sum(
                count
                for (store, _turn, method), count in metrics.protocol_exchanges.items()
                if store == "run" and method == "bind_execute"
            )
            await connection.fetchval("SELECT $1::integer", 2)
            warm_prepare = sum(
                count
                for (store, _turn, method), count in metrics.protocol_exchanges.items()
                if store == "run" and method == "prepare"
            )
            warm_bind = sum(
                count
                for (store, _turn, method), count in metrics.protocol_exchanges.items()
                if store == "run" and method == "bind_execute"
            )
            assert cold_prepare >= 1
            assert cold_bind >= 1
            assert warm_prepare == cold_prepare
            assert warm_bind >= cold_bind + 1
            async with connection.transaction():
                await connection.fetchval("SELECT $1::integer", 3)
            await connection.executemany("SELECT $1::integer", [(4,), (5,)])
        assert sum(metrics.protocol_transaction_commands.values()) >= 2
        assert sum(metrics.pool_reset_commands.values()) == 1
        assert (
            sum(
                count
                for (store, _turn, method), count in metrics.protocol_exchanges.items()
                if store == "run" and method == "bind_execute_many"
            )
            >= 1
        )
    finally:
        await pool.close()


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
    assert result["store_writes"]["by_store"]["run"]["mark_completed"] == 5
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
        run = await store.create_run("s")
        checkpoint = RunCheckpoint("user_appended", "héllo", 0, Usage())
        await store.save_checkpoint(run.id, checkpoint)
        raw = await connection.fetchval("SELECT checkpoint FROM runs WHERE id = $1", run.id)
        first_bytes = len(raw.encode("utf-8"))
        request = ProviderRequest(
            model="gpt-5", system=[], messages=[Message("user", [])], tools=[]
        )
        snapshot = await store.save(run.id, 1, request)
        assert (await store.load(snapshot.id)).id == snapshot.id
        await store.delete(snapshot.id)
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
    assert payloads["total"] == first_bytes + terminal_bytes + 2 * failed_bytes
    assert payloads["by_phase"]["completed"]["total"] == terminal_bytes
    assert payloads["by_phase"]["failed"]["total"] == 2 * failed_bytes
    assert not hasattr(benchmark.MeasuredStore(object(), "run", metrics), "append_events")


@pytest.mark.asyncio
async def test_measured_store_wraps_atomic_checkpoint_capability_and_final_payload():
    benchmark = _load_script()
    from types import SimpleNamespace

    from linch import RunCheckpoint
    from linch.events import ToolCallEndEvent
    from linch.types import Usage

    checkpoint = RunCheckpoint("provider_pending", "before", 3, Usage())
    event = ToolCallEndEvent(tool_use_id="fixed-call", tool_name="read")
    calls: list[tuple[str, RunCheckpoint, list[object], str]] = []

    class AtomicStore:
        async def save_checkpoint_with_events(
            self,
            run_id,
            candidate,
            events,
            *,
            status="running",
            prepare_checkpoint=None,
        ):
            calls.append((run_id, candidate, events, status))
            assert prepare_checkpoint is not None
            prepare_checkpoint(42)
            candidate.prompt = "prepared"
            return SimpleNamespace(checkpoint=candidate), [17]

    metrics = benchmark.ScaleStoreMetrics()
    store = benchmark.MeasuredStore(AtomicStore(), "run", metrics)

    result = await store.save_checkpoint_with_events(
        "run-id", checkpoint, [event], prepare_checkpoint=lambda seq: None
    )

    assert result[1] == [17]
    assert calls == [("run-id", checkpoint, [event], "running")]
    assert metrics.writes["run", "save_checkpoint_with_events", 3] == 1
    expected_checkpoint_bytes = len(
        json.dumps(benchmark.checkpoint_to_dict(result[0].checkpoint), allow_nan=False).encode(
            "utf-8"
        )
    )
    assert metrics.payloads[3, "provider_pending"] == [expected_checkpoint_bytes]
    expected_event_bytes = len(
        json.dumps(benchmark._json_safe_stored_event(event), allow_nan=False).encode("utf-8")
    )
    assert metrics.event_payloads[3] == [expected_event_bytes]


def test_measured_store_preserves_optional_atomic_capability_none_and_absence():
    benchmark = _load_script()
    from linch import InMemoryRunStore

    class LegacyStore:
        save_checkpoint_with_events = None

    metrics = benchmark.ScaleStoreMetrics()
    assert (
        benchmark.MeasuredStore(LegacyStore(), "run", metrics).save_checkpoint_with_events is None
    )
    with pytest.raises(AttributeError):
        _ = benchmark.MeasuredStore(InMemoryRunStore(), "run", metrics).save_checkpoint_with_events


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
async def test_postgres_timeout_drops_child_schema_after_create_before_close(
    pg_dsn, tmp_path, monkeypatch
):
    benchmark = _load_script()
    import asyncpg

    created_marker = tmp_path / "created-schema"
    monkeypatch.setenv("LINCH_SCALE_BLOCK_AFTER_CREATE", str(created_marker))

    async def scale_schemas(connection):
        return {
            row["nspname"]
            for row in await connection.fetch(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'linch_scale_%'"
            )
        }

    connection = await asyncpg.connect(pg_dsn)
    try:
        before = await scale_schemas(connection)
        unrelated_schema = await connection.fetchval("SELECT current_schema()")
    finally:
        await connection.close()

    result = await benchmark.run_scale_subprocess(
        benchmark.ScaleCase("timeout_postgres", "postgres", sessions=1, turns=4),
        timeout_s=1.0,
    )
    assert result["timed_out"] is True
    assert created_marker.read_text() == result["postgres_schema"]

    connection = await asyncpg.connect(pg_dsn)
    try:
        after = await scale_schemas(connection)
        unrelated_exists = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = $1)", unrelated_schema
        )
    finally:
        await connection.close()
    assert after == before
    assert unrelated_exists is True


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
