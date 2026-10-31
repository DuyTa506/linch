#!/usr/bin/env python3
"""Multi-process scale/load harness for the production worker topology.

The file intentionally has no Linch changes in it.  It is also usable as a
small worker/enqueuer/proxy process so that measurements exercise process
boundaries rather than an in-process simulation.  Heavy invocations should be
wrapped by the caller with ``flock /tmp/wp8-cpu.lock``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SOCKET = "/run/user/1000/python_PostgresServer/28a76638b8/.s.PGSQL.5432"
TERMINAL = {"completed", "failed", "aborted"}
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    at = (len(values) - 1) * p
    lo, hi = math.floor(at), math.ceil(at)
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (at - lo)


def _parse_ts(raw: Any) -> float:
    if isinstance(raw, datetime):
        return raw.timestamp()
    text = str(raw).replace("Z", "+00:00")
    value = datetime.fromisoformat(text)
    return value.replace(tzinfo=UTC).timestamp() if value.tzinfo is None else value.timestamp()


def _proc_cpu(pid: int) -> tuple[float, float] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        return (int(fields[13]) + int(fields[14])) / ticks, time.monotonic()
    except (FileNotFoundError, IndexError, ValueError, OSError):
        return None


def _postgres_pids() -> list[int]:
    pids: list[int] = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            if entry.joinpath("comm").read_text().strip().startswith("postgres"):
                pids.append(int(entry.name))
        except (FileNotFoundError, OSError, ValueError):
            continue
    return pids


async def _cpu_monitor(
    pids: list[int], stop: asyncio.Event, window_start: float, window_end: float
) -> dict[str, float]:
    """Sample process CPU with 100% meaning one fully used logical CPU."""
    tracked = set(pids)
    samples: dict[int, list[tuple[float, float]]] = {pid: [] for pid in tracked}
    while not stop.is_set() and time.monotonic() <= window_end:
        # Postgres launches a backend per connection after the initial
        # snapshot.  Discover those PIDs throughout the window so connection
        # bursts are included in the reported database CPU.
        tracked.update(_postgres_pids())
        for pid in tracked:
            samples.setdefault(pid, [])
            sample = _proc_cpu(pid)
            if sample is not None:
                samples[pid].append(sample)
        await asyncio.sleep(0.25)
    tracked.update(_postgres_pids())
    for pid in tracked:
        sample = _proc_cpu(pid)
        if sample is not None:
            samples.setdefault(pid, [])
            samples[pid].append(sample)
    window_seconds = max(1e-6, window_end - window_start)
    values: dict[str, float] = {}
    for pid, points in samples.items():
        in_window = [point for point in points if window_start <= point[1] <= window_end]
        if len(in_window) < 2:
            continue
        start, end = in_window[0], in_window[-1]
        values[str(pid)] = max(0.0, (end[0] - start[0]) / window_seconds * 100)
    return values


async def _pg_window(
    pool: Any, window_start: float, window_end: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    await asyncio.sleep(max(0.0, window_start - time.monotonic()))
    before = await _pg_stats(pool)
    samples: list[dict[str, Any]] = []
    while time.monotonic() < window_end:
        remaining = window_end - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(1.0, remaining))
        samples.append(await _pg_stats(pool))
    after = samples[-1] if samples else await _pg_stats(pool)
    wait_counts: dict[tuple[Any, Any], int] = {}
    connections = []
    for sample in samples:
        connections.append(int(sample["connections_in_use"]))
        for event in sample["wait_events"]:
            key = (event.get("wait_event_type"), event.get("wait_event"))
            wait_counts[key] = wait_counts.get(key, 0) + int(event.get("n", 0))
    after["wait_event_sample_counts"] = [
        {"wait_event_type": key[0], "wait_event": key[1], "samples": count}
        for key, count in wait_counts.items()
    ]
    after["connections_max"] = max(connections, default=after["connections_in_use"])
    after["connections_mean"] = (
        sum(connections) / len(connections) if connections else after["connections_in_use"]
    )
    return before, after


def _tcp_args(dsn: str, port: int) -> dict[str, Any]:
    parsed = urlparse(dsn)
    return {
        "host": "127.0.0.1",
        "port": port,
        "user": parsed.username or "postgres",
        "password": parsed.password or "",
        "database": (parsed.path or "/postgres").lstrip("/") or "postgres",
    }


async def _pool(
    dsn: str,
    port: int,
    schema: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    application_name: str | None = None,
):
    import asyncpg

    settings = {"search_path": schema}
    if application_name:
        settings["application_name"] = application_name
    return await asyncpg.create_pool(
        **_tcp_args(dsn, port),
        min_size=min_size,
        max_size=max_size,
        server_settings=settings,
    )


async def _direct_pool(
    dsn: str, schema: str, *, min_size: int = 1, max_size: int = 10, application_name: str
):
    import asyncpg

    return await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        server_settings={"search_path": schema, "application_name": application_name},
    )


class TimedProvider:
    """Offline provider: 300 ms first token, 20 deltas over 700 ms per turn."""

    id = "load-test"

    def __init__(self, first_token_s: float, delta_window_s: float) -> None:
        self.first_token_s = first_token_s
        self.delta_window_s = delta_window_s

    def context_window(self, model: str) -> int:
        return 128_000

    def capabilities(self, model: str) -> Any:
        from linch.providers.base import ProviderCapabilities

        return ProviderCapabilities(context_window=128_000)

    async def stream(self, req: Any):
        import json as _json

        # A fresh Agent is built when a run is resumed after worker loss.  The
        # request history, rather than process-local state, pins the scripted
        # turn so a retry cannot repeat an earlier tool call under a new epoch.
        turn = sum(getattr(message, "role", None) == "assistant" for message in req.messages)
        yield {"type": "message_start", "model": req.model}
        await asyncio.sleep(self.first_token_s)
        names = [
            ("RAG", {"query": "scale readiness"}),
            ("SQL", {"query": "SELECT 1"}),
            ("save_note", {"note": "load-test note"}),
        ]
        if turn < len(names):
            name, payload = names[turn]
            yield {"type": "tool_use_start", "id": f"tool_{turn}", "name": name}
            encoded = _json.dumps(payload)
            for part in _parts(encoded, 20):
                await asyncio.sleep(self.delta_window_s / 20)
                yield {"type": "tool_use_input_delta", "id": f"tool_{turn}", "json_delta": part}
            yield {"type": "tool_use_end", "id": f"tool_{turn}"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": _usage()}
            return
        text = "completed load test"
        for part in _parts(text, 20):
            await asyncio.sleep(self.delta_window_s / 20)
            yield {"type": "text_delta", "text": part}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": _usage()}


def _parts(value: str, count: int) -> list[str]:
    step = max(1, math.ceil(len(value) / count))
    result = [value[i : i + step] for i in range(0, len(value), step)]
    return result + [""] * max(0, count - len(result))


def _usage() -> Any:
    from linch.types import Usage

    return Usage(input_tokens=120, output_tokens=20)


def _tools(pool: Any, tool_delay_low: float, tool_delay_high: float) -> Any:
    from linch import ToolContext, ToolResult, empty_tools, tool

    async def _pause() -> None:
        await asyncio.sleep(random.uniform(tool_delay_low, tool_delay_high))

    @tool(name="RAG", scope="read")
    async def rag(query: str, ctx: ToolContext) -> ToolResult:
        await _pause()
        return ToolResult(content=f"retrieved:{query}")

    @tool(name="SQL", scope="read")
    async def sql(query: str, ctx: ToolContext) -> ToolResult:
        await _pause()
        return ToolResult(content=f"row:{query}")

    @tool(name="save_note", scope="write")
    async def save_note(note: str, ctx: ToolContext) -> ToolResult:
        await _pause()
        key = ctx.idempotency_key.rsplit(":", 1)
        run_id, tool_id = (key[0], key[1]) if len(key) == 2 else (ctx.run_id, ctx.idempotency_key)
        await pool.execute(
            "INSERT INTO load_save_note_attempts(run_id, tool_call_id, note) VALUES($1,$2,$3)",
            run_id,
            tool_id,
            note,
        )
        await pool.execute(
            "INSERT INTO load_save_notes(run_id, tool_call_id, note) VALUES($1,$2,$3) "
            "ON CONFLICT (run_id, tool_call_id) DO NOTHING",
            run_id,
            tool_id,
            note,
        )
        return ToolResult(content="saved")

    return empty_tools(rag, sql, save_note)


async def _install_load_tables(pool: Any) -> None:
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS load_save_notes("
        "run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, note TEXT NOT NULL, "
        "PRIMARY KEY(run_id,tool_call_id))"
    )
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS load_save_note_attempts("
        "id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, "
        "note TEXT NOT NULL, attempted_at TIMESTAMPTZ DEFAULT clock_timestamp())"
    )
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS load_claims("
        "run_id TEXT NOT NULL, claimed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
        "owner TEXT NOT NULL, epoch BIGINT NOT NULL)"
    )
    await pool.execute(
        "CREATE OR REPLACE FUNCTION load_claim_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN IF NEW.lease_owner IS NOT NULL AND "
        "(OLD.lease_owner IS DISTINCT FROM NEW.lease_owner OR "
        "OLD.lease_epoch IS DISTINCT FROM NEW.lease_epoch) THEN "
        "INSERT INTO load_claims(run_id,owner,epoch) "
        "VALUES(NEW.id,NEW.lease_owner,NEW.lease_epoch); "
        "END IF; RETURN NEW; END $$"
    )
    await pool.execute("DROP TRIGGER IF EXISTS load_claim_audit_trigger ON runs")
    await pool.execute(
        "CREATE TRIGGER load_claim_audit_trigger AFTER UPDATE OF lease_owner,lease_epoch ON runs "
        "FOR EACH ROW EXECUTE FUNCTION load_claim_audit()"
    )
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS load_event_audit("
        "run_id TEXT NOT NULL, seq BIGINT NOT NULL, appended_at TEXT NOT NULL, "
        "actor TEXT NOT NULL, owner TEXT, epoch BIGINT, event_type TEXT NOT NULL)"
    )
    await pool.execute(
        "CREATE OR REPLACE FUNCTION load_event_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "DECLARE current_owner TEXT; current_epoch BIGINT; BEGIN "
        "SELECT lease_owner,lease_epoch INTO current_owner,current_epoch "
        "FROM runs WHERE id=NEW.run_id; "
        "INSERT INTO load_event_audit(run_id,seq,appended_at,actor,owner,epoch,event_type) "
        "VALUES(NEW.run_id,NEW.seq,NEW.appended_at,current_setting('application_name',true),"
        "current_owner,current_epoch,coalesce(NEW.event::jsonb->'event'->>'type','unknown')); "
        "RETURN NEW; END $$"
    )
    await pool.execute("DROP TRIGGER IF EXISTS load_event_audit_trigger ON run_events")
    await pool.execute(
        "CREATE TRIGGER load_event_audit_trigger AFTER INSERT ON run_events "
        "FOR EACH ROW EXECUTE FUNCTION load_event_audit()"
    )


async def _lag_monitor(
    stop: asyncio.Event,
    out: list[float],
    interval: float = 0.05,
    samples: list[dict[str, float]] | None = None,
) -> None:
    expected = asyncio.get_running_loop().time() + interval
    while not stop.is_set():
        await asyncio.sleep(max(0, expected - asyncio.get_running_loop().time()))
        now = asyncio.get_running_loop().time()
        lag = max(0.0, now - expected)
        out.append(lag)
        if samples is not None:
            samples.append({"wall_time": time.time(), "lag_ms": lag * 1000})
        expected += interval


async def _pool_monitor(
    pool: Any, stop: asyncio.Event, out: list[int], interval: float = 0.1
) -> None:
    """Sample asyncpg connections borrowed by this worker's pool.

    ``pg_stat_activity`` only sees queries that reached the server.  A pool can
    be full while its borrowers are waiting in the proxy, so this local view
    is needed to distinguish pool saturation from server activity.
    """
    while not stop.is_set():
        try:
            out.append(max(0, int(pool.get_size()) - int(pool.get_idle_size())))
        except (AttributeError, TypeError, ValueError):
            pass
        await asyncio.sleep(interval)


async def worker_role(args: argparse.Namespace) -> None:
    from linch import Agent
    from linch.run_stores.postgres import PostgresRunStore
    from linch.sessions.postgres import PostgresSessionStore
    from linch.worker import Worker, install

    pool = await _pool(
        args.dsn,
        args.port,
        args.schema,
        min_size=2,
        max_size=args.pool_size,
        application_name=args.worker_id,
    )
    await install(pool)
    lag: list[float] = []
    lag_samples: list[dict[str, float]] = []
    pool_in_use: list[int] = []
    stopped = asyncio.Event()
    renew_failures = 0
    stop_requested = False
    metrics_path = Path(args.metrics)

    def write_metrics(*, partial: bool) -> None:
        payload = {
            "pid": os.getpid(),
            "worker_id": args.worker_id,
            "event_loop_lag_p99_ms": (_pct([x * 1000 for x in lag], 0.99) or 0),
            "event_loop_lag_max_ms": (max(lag) * 1000 if lag else 0),
            "lag_samples": lag_samples,
            "lease_renew_failures": renew_failures,
            "graceful_stop": stop_requested,
            "partial": partial,
            "pool_size": int(pool.get_size()),
            "pool_in_use_max": max(pool_in_use or [0]),
            "pool_in_use_mean": sum(pool_in_use or [0]) / len(pool_in_use or [0]),
        }
        temporary = metrics_path.with_name(f".{metrics_path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(payload))
            os.replace(temporary, metrics_path)
        except OSError:
            with contextlib.suppress(OSError):
                temporary.unlink()

    lag_task = asyncio.create_task(_lag_monitor(stopped, lag, samples=lag_samples))
    pool_task = asyncio.create_task(_pool_monitor(pool, stopped, pool_in_use))
    checkpoint_task: asyncio.Task[None] | None = None
    original_renew = PostgresRunStore.renew

    async def renew(store: Any, lease: Any) -> Any:
        nonlocal renew_failures
        try:
            return await original_renew(store, lease)
        except Exception:
            renew_failures += 1
            raise

    PostgresRunStore.renew = renew

    def request_stop() -> None:
        nonlocal stop_requested
        stop_requested = True
        worker.stop()

    async def factory(job: Any) -> Agent:
        return Agent(
            model="load-test",
            provider=TimedProvider(args.first_token, args.delta_window),
            tools=_tools(job.pool, args.tool_low, args.tool_high),
            permissions={"mode": "skip-dangerous"},
            session_store=PostgresSessionStore("", pool=job.pool),
            run_store=job.run_store,
            max_turns=4,
        )

    worker = Worker(
        pool,
        factory,
        worker_id=args.worker_id,
        queues={"default": args.concurrency},
        lease_ttl_s=args.lease_ttl,
        stall_timeout_s=args.stall_timeout,
        poll_interval_s=0.05,
        retry_delay_s=0.1,
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, request_stop)

    async def checkpoint_loop() -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=1.0)
            except TimeoutError:
                write_metrics(partial=True)

    write_metrics(partial=True)
    checkpoint_task = asyncio.create_task(checkpoint_loop())
    try:
        await worker.run()
    finally:
        stopped.set()
        tasks: list[asyncio.Task[Any]] = [lag_task, pool_task]
        if checkpoint_task is not None:
            tasks.append(checkpoint_task)
        await asyncio.gather(*tasks)
        write_metrics(partial=not stop_requested)
        await pool.close()


async def _read_stream(pool: Any, rid: str, interval: float, result: dict[str, Any]) -> None:
    """Follow a run by sequence until its terminal result tail is stable."""

    from linch.run_stores.postgres import PostgresRunStore

    store = PostgresRunStore("", pool=pool)
    seq = 0
    result_seq: int | None = None
    result_events = 0
    stable_polls = 0
    terminal_tail_polls = max(3, int(result.get("terminal_tail_polls", 20)))
    ok = True
    result["subscriptions"] = int(result.get("subscriptions", 0)) + 1
    result["active_readers"] = int(result.get("active_readers", 0)) + 1
    result["peak_readers"] = max(int(result.get("peak_readers", 0)), int(result["active_readers"]))
    try:
        while True:
            rows = await store.load_events(rid, after_seq=seq)
            now = time.time()
            for row in rows:
                if row.seq != seq + 1:
                    ok = result["in_order"] = False
                seq = row.seq
                result["events"] += 1
                result["append_read_lag_ms"].append(
                    max(0.0, (now - _parse_ts(row.appended_at)) * 1000)
                )
                event = row.event
                event_type = (
                    event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
                )
                if event_type == "result":
                    result_events += 1
                    result_seq = row.seq
            status = await pool.fetchval("SELECT status FROM runs WHERE id=$1", rid)
            if status in TERMINAL:
                stable_polls = 0 if rows else stable_polls + 1
                exact_tail = result_events == 1 and result_seq == seq and seq > 0
                if exact_tail and stable_polls >= 2:
                    result["completions"] = int(result.get("completions", 0)) + 1
                    result.setdefault("completed_run_ids", set()).add(rid)
                    return
                if stable_polls >= terminal_tail_polls:
                    result["result_event_failures"] = (
                        int(result.get("result_event_failures", 0)) + 1
                    )
                    reason = (
                        "missing result event"
                        if result_events == 0
                        else "duplicate result events"
                        if result_events != 1
                        else "result event is not final"
                    )
                    raise RuntimeError(f"run {rid} terminal with {reason}")
            else:
                stable_polls = 0
            await asyncio.sleep(interval)
    except Exception:
        ok = False
        raise
    finally:
        result["active_readers"] = max(0, int(result.get("active_readers", 1)) - 1)
        if not ok:
            result["reader_failures"] = int(result.get("reader_failures", 0)) + 1


async def _reader_loop(
    queue: asyncio.Queue[str | None] | None,
    pool: Any,
    interval: float,
    result: dict[str, Any],
    *,
    fan_state: dict[str, Any] | None = None,
) -> None:
    """Run a sampled queue reader or a latest-run fan-in subscription."""

    def record_error(exc: Exception) -> None:
        result["reader_errors"] = int(result.get("reader_errors", 0)) + 1
        errors = result.setdefault("reader_errors_by_type", {})
        name = type(exc).__name__
        errors[name] = int(errors.get(name, 0)) + 1

    if fan_state is not None:
        condition: asyncio.Condition = fan_state["condition"]
        last_rid: str | None = None
        while True:
            async with condition:
                while not fan_state["stop"] and (
                    fan_state["latest"] is None or fan_state["latest"] == last_rid
                ):
                    await condition.wait()
                if fan_state["stop"]:
                    return
                rid = str(fan_state["latest"])
                last_rid = rid
            try:
                await _read_stream(pool, rid, interval, result)
            except Exception as exc:
                record_error(exc)
                last_rid = None
                await asyncio.sleep(interval)
        return

    if queue is None:
        raise ValueError("queue is required when fan_state is absent")
    while True:
        rid = await queue.get()
        try:
            if rid is None:
                return
            try:
                await _read_stream(pool, rid, interval, result)
            except Exception as exc:
                record_error(exc)
        finally:
            queue.task_done()


async def enqueue_role(args: argparse.Namespace) -> None:
    from linch.worker import enqueue_run, install

    pool = await _pool(
        args.dsn,
        args.port,
        args.schema,
        min_size=2,
        max_size=args.pool_size,
        application_name=f"api-load-{os.getpid()}",
    )
    reader_tasks: list[asyncio.Task[None]] = []
    inflight: set[asyncio.Task[None]] = set()
    configured_readers = max(1, int(getattr(args, "max_stream_readers", 100) or 0))
    raw_fan_in = getattr(args, "stream_fan_in", 0)
    fan_in = configured_readers if raw_fan_in is True else max(0, int(raw_fan_in or 0))
    reader_count = fan_in or configured_readers
    reader_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=reader_count * 2)
    fan_state: dict[str, Any] | None = None
    stream: dict[str, Any] = {
        "in_order": True,
        "events": 0,
        "append_read_lag_ms": [],
        "reader_failures": 0,
        "reader_errors": 0,
        "reader_errors_by_type": {},
        "reader_dropped": 0,
        "active_readers": 0,
        "peak_readers": 0,
        "subscriptions": 0,
        "completions": 0,
        "completed_run_ids": set(),
        "result_event_failures": 0,
        "terminal_tail_polls": 20,
    }
    try:
        await install(pool)
        if fan_in:
            fan_state = {"condition": asyncio.Condition(), "latest": None, "stop": False}
            reader_tasks = [
                asyncio.create_task(
                    _reader_loop(
                        None,
                        pool,
                        args.stream_poll,
                        stream,
                        fan_state=fan_state,
                    )
                )
                for _ in range(reader_count)
            ]
        else:
            reader_tasks = [
                asyncio.create_task(_reader_loop(reader_queue, pool, args.stream_poll, stream))
                for _ in range(reader_count)
            ]
        rng = random.Random(args.seed)
        loop = asyncio.get_running_loop()
        started = loop.time()
        wall_started = time.time()
        steady_start_wall = wall_started + args.warmup
        steady_end_wall = steady_start_wall + args.duration
        window_path = Path(
            getattr(args, "window", str(Path(args.runs).with_suffix(".window.json")))
        )
        enqueue_timeout = float(getattr(args, "enqueue_timeout", 10.0))
        window_path.write_text(
            json.dumps(
                {
                    "started_wall": wall_started,
                    "steady_start_wall": steady_start_wall,
                    "steady_end_wall": steady_end_wall,
                    "duration": args.duration,
                }
            )
        )
        deadline = started + args.warmup + args.duration
        next_at = started

        async def submit(rid: str, scheduled_wall: float) -> None:
            record: dict[str, Any] = {
                "run_id": rid,
                "scheduled_ts": scheduled_wall,
                "steady": steady_start_wall <= scheduled_wall < steady_end_wall,
                "enqueue_attempted": True,
                "enqueue_started_ts": time.time(),
            }
            try:
                async with asyncio.timeout(enqueue_timeout):
                    await enqueue_run(
                        pool,
                        f"load-session-{rid}",
                        "exercise the tools",
                        run_id=rid,
                    )
                record["enqueue_ok"] = True
                record["enqueue_ts"] = time.time()
                if fan_state is not None:
                    async with fan_state["condition"]:
                        fan_state["latest"] = rid
                        fan_state["condition"].notify_all()
                elif args.stream_sample > 0 and rng.random() < args.stream_sample:
                    try:
                        reader_queue.put_nowait(rid)
                    except asyncio.QueueFull:
                        stream["reader_dropped"] += 1
            except Exception as exc:
                record["enqueue_ok"] = False
                record["enqueue_ts"] = time.time()
                record["error"] = type(exc).__name__
                record["enqueue_error"] = str(exc)
            with Path(args.runs).open("a") as output:
                output.write(json.dumps(record) + "\n")

        while next_at < deadline and args.rate > 0:
            next_at += rng.expovariate(args.rate)
            await asyncio.sleep(max(0.0, next_at - loop.time()))
            if next_at >= deadline:
                break
            rid = str(uuid4())
            task = asyncio.create_task(submit(rid, wall_started + (next_at - started)))
            inflight.add(task)
            task.add_done_callback(inflight.discard)
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        if fan_state is not None:
            drain_deadline = loop.time() + max(5.0, args.stream_poll * 30)
            while stream["active_readers"] and loop.time() < drain_deadline:
                await asyncio.sleep(min(args.stream_poll, 0.1))
        else:
            await reader_queue.join()
    finally:
        if fan_state is not None:
            async with fan_state["condition"]:
                fan_state["stop"] = True
                fan_state["condition"].notify_all()
        else:
            for _ in reader_tasks:
                with contextlib.suppress(asyncio.QueueFull):
                    reader_queue.put_nowait(None)
        if reader_tasks:
            await asyncio.gather(*reader_tasks, return_exceptions=True)
        for task in inflight:
            if not task.done():
                task.cancel()
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        completed_run_ids = stream.get("completed_run_ids", set())
        Path(args.stream_metrics).write_text(
            json.dumps(
                {
                    "in_order": stream["in_order"],
                    "terminal": stream["completions"] > 0
                    and stream["reader_failures"] == 0
                    and stream["reader_errors"] == 0,
                    "events": stream["events"],
                    "readers": len(reader_tasks),
                    "fan_in_target": fan_in,
                    "fan_in_satisfied": bool(fan_in and stream["peak_readers"] >= fan_in),
                    "active_readers": stream["active_readers"],
                    "peak_readers": stream["peak_readers"],
                    "peak_concurrent_readers": stream["peak_readers"],
                    "subscriptions": stream["subscriptions"],
                    "completions": stream["completions"],
                    "completed_runs": len(completed_run_ids),
                    "result_event_exact": stream["result_event_failures"] == 0
                    and stream["completions"] > 0,
                    "result_event_failures": stream["result_event_failures"],
                    "reader_errors": stream["reader_errors"],
                    "reader_errors_by_type": stream["reader_errors_by_type"],
                    "reader_dropped": stream["reader_dropped"],
                    "append_read_lag_p99_ms": _pct(stream["append_read_lag_ms"], 0.99),
                    "append_read_lag_max_ms": max(stream["append_read_lag_ms"], default=None),
                }
            )
        )
        await pool.close()


class _Proxy:
    def __init__(self, socket: str, delay_s: float, control_port: int) -> None:
        self.socket, self.delay, self.control_port = socket, delay_s, control_port
        self.dropping = False
        self.clients: set[asyncio.StreamWriter] = set()
        self.server: asyncio.AbstractServer | None = None

    async def relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.PriorityQueue[tuple[float, int, bytes]] = asyncio.PriorityQueue()
        seq = 0

        async def read_side() -> None:
            nonlocal seq
            while data := await reader.read(65536):
                await queue.put((asyncio.get_running_loop().time() + self.delay, seq, data))
                seq += 1
            await queue.put((asyncio.get_running_loop().time() + self.delay, seq, b""))

        async def write_side() -> None:
            while True:
                due, _, data = await queue.get()
                await asyncio.sleep(max(0, due - asyncio.get_running_loop().time()))
                if not data:
                    return
                writer.write(data)
                await writer.drain()

        try:
            await asyncio.gather(read_side(), write_side())
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.dropping:
            writer.close()
            await writer.wait_closed()
            return
        self.clients.add(writer)
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(self.socket)
            await asyncio.gather(self.relay(reader, up_writer), self.relay(up_reader, writer))
        except (ConnectionError, OSError):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        finally:
            self.clients.discard(writer)

    async def control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            command = (await reader.readline()).decode().strip().split()
            if command and command[0] == "drop":
                seconds = float(command[1]) if len(command) > 1 else 3.0
                self.dropping = True
                for client in list(self.clients):
                    client.close()
                await asyncio.sleep(seconds)
                self.dropping = False
                writer.write(b"ok\n")
                await writer.drain()
        finally:
            writer.close()

    async def run(self, port: int) -> None:
        self.server = await asyncio.start_server(self.client, "127.0.0.1", port)
        control = await asyncio.start_server(self.control, "127.0.0.1", self.control_port)
        actual = self.server.sockets[0].getsockname()[1]
        print(
            json.dumps({"ready": actual, "control": control.sockets[0].getsockname()[1]}),
            flush=True,
        )
        async with self.server, control:
            await asyncio.gather(self.server.serve_forever(), control.serve_forever())


async def proxy_role(args: argparse.Namespace) -> None:
    await _Proxy(args.socket, args.rtt / 2000, args.control_port).run(args.port)


@dataclass(slots=True)
class Point:
    workers: int
    concurrency: int
    rate: float
    rtt: float
    warmup: float
    duration: float
    # Eight connections per worker leaves room under the local 100-connection
    # server limit for the API, monitor, and probe pools at M=8.
    pool_size: int = 8
    first_token: float = 0.3
    delta_window: float = 0.7
    tool_low: float = 0.03
    tool_high: float = 0.08
    stream_sample: float = 0.1
    stream_poll: float = 0.05
    stream_fan_in: bool = False
    chaos: str | None = None
    max_stream_readers: int = 100
    lease_ttl: float = 2.0
    stall_timeout: float = 4.0
    enqueue_timeout: float = 10.0


async def _pg_stats(pool: Any) -> dict[str, Any]:
    db = await pool.fetchrow(
        "SELECT xact_commit,tup_inserted FROM pg_stat_database WHERE datname=current_database()"
    )
    wal = await pool.fetchrow("SELECT wal_bytes FROM pg_stat_wal")
    active = await pool.fetchval(
        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state <> 'idle'"
    )
    waits = await pool.fetch(
        "SELECT wait_event_type,wait_event,count(*) n FROM pg_stat_activity "
        "WHERE datname=current_database() GROUP BY 1,2"
    )
    return {
        "commits": int(db["xact_commit"]),
        "rows_inserted": int(db["tup_inserted"]),
        "wal_bytes": int(wal["wal_bytes"]),
        "connections_in_use": int(active),
        "wait_events": [dict(row) for row in waits],
    }


async def _calibrate_rtt(pool: Any, count: int = 20) -> dict[str, float | None]:
    samples: list[float] = []
    for _ in range(count):
        started = time.perf_counter()
        await pool.fetchval("SELECT 1")
        samples.append((time.perf_counter() - started) * 1000)
    return {"p50_ms": _pct(samples, 0.5), "p95_ms": _pct(samples, 0.95)}


async def _summarize(
    pool: Any,
    run_file: Path,
    point: Point,
    window: dict[str, float],
    pg_window: tuple[dict[str, Any], dict[str, Any]],
    worker_metrics: list[Path],
    stream_file: Path,
    cpu: dict[str, Any],
    calibration: dict[str, Any],
    drain_time: float | None,
) -> dict[str, Any]:
    records = [json.loads(line) for line in run_file.read_text().splitlines() if line.strip()]
    attempted_ids = {r["run_id"] for r in records}
    record_by_id = {r["run_id"]: r for r in records}
    acknowledged_ids = {r["run_id"] for r in records if r.get("enqueue_ok") is True}
    enqueue_error_ids = {
        r["run_id"] for r in records if r.get("enqueue_ok") is False or "error" in r
    }
    enqueue_error_types: dict[str, int] = {}
    for record in records:
        if "error" in record:
            kind = str(record["error"])
            enqueue_error_types[kind] = enqueue_error_types.get(kind, 0) + 1
    steady_start_wall = window["steady_start_wall"]
    steady_end_wall = window["steady_end_wall"]
    steady_duration = max(window["duration"], 1e-9)
    rows = await pool.fetch(
        "SELECT id,status,created_at,updated_at,meta FROM runs WHERE id=ANY($1::text[])",
        list(attempted_ids),
    )
    ids = [str(row["id"]) for row in rows]
    durable_ids = set(ids)
    steady_id_set = {
        rid for rid, row in record_by_id.items() if row.get("steady") and rid in durable_ids
    }
    enqueue_ambiguous = durable_ids & enqueue_error_ids
    enqueue_rejected = enqueue_error_ids - durable_ids
    terminal = [r for r in rows if r["status"] in TERMINAL]

    def _arrival_ts(run_id: str) -> float:
        record = record_by_id[run_id]
        return float(record.get("enqueue_started_ts", record.get("enqueue_ts", time.time())))

    lat = [
        (_parse_ts(row["updated_at"]) - _arrival_ts(row["id"])) * 1000
        for row in terminal
        if row["id"] in steady_id_set
    ]
    waits: list[float] = []
    if ids:
        first = await pool.fetch(
            "SELECT run_id,min(claimed_at) claimed_at FROM load_claims "
            "WHERE run_id=ANY($1::text[]) GROUP BY run_id",
            ids,
        )
        waits = [
            (_parse_ts(row["claimed_at"]) - _arrival_ts(row["run_id"])) * 1000
            for row in first
            if row["run_id"] in steady_id_set
        ]
    pg_before, pg_after = pg_window
    metrics: list[dict[str, Any]] = []
    for path in worker_metrics:
        if not path.exists():
            continue
        item = json.loads(path.read_text())
        raw_samples = item.pop("lag_samples", [])
        steady_lag = [
            float(sample["lag_ms"])
            for sample in raw_samples
            if steady_start_wall <= float(sample.get("wall_time", 0)) <= steady_end_wall
        ]
        item["event_loop_lag_p99_ms"] = _pct(steady_lag, 0.99)
        item["event_loop_lag_max_ms"] = max(steady_lag, default=None)
        item["steady_lag_samples"] = len(steady_lag)
        metrics.append(item)
    worker_ids = {str(item.get("worker_id")) for item in metrics}
    expected_workers = {f"w-{index}" for index in range(point.workers)}
    missing_worker_metrics = sorted(expected_workers - worker_ids)
    graceful_stop_workers = sum(bool(item.get("graceful_stop")) for item in metrics)
    stream = json.loads(stream_file.read_text()) if stream_file.exists() else {}
    scale = steady_duration
    completed = sum(r["status"] == "completed" for r in terminal)
    steady_terminal = [row for row in terminal if row["id"] in steady_id_set]
    steady_completed = [
        row
        for row in terminal
        if row["status"] == "completed"
        and steady_start_wall <= _parse_ts(row["updated_at"]) <= steady_end_wall
    ]
    turns_steady = await pool.fetchval(
        "SELECT count(*) FROM run_events "
        "WHERE appended_at::timestamptz >= to_timestamp($1) "
        "AND appended_at::timestamptz < to_timestamp($2) "
        "AND (event::jsonb->'event'->>'type')='assistant'",
        steady_start_wall,
        steady_end_wall,
    )
    turns_steady = int(turns_steady or 0)
    late_completions = sum(
        _parse_ts(row["updated_at"]) > steady_end_wall for row in steady_terminal
    )
    terminal_events = await pool.fetch(
        "SELECT run_id,count(*) n FROM run_events "
        "WHERE run_id=ANY($1::text[]) AND (event::jsonb->'event'->>'type')='result' "
        "GROUP BY run_id",
        ids,
    )
    result_counts = {row["run_id"]: int(row["n"]) for row in terminal_events}
    duplicate_terminal = sum(max(0, count - 1) for count in result_counts.values())
    seq_rows = await pool.fetch(
        "SELECT run_id,count(*) n,min(seq) first_seq,max(seq) last_seq "
        "FROM run_events WHERE run_id=ANY($1::text[]) GROUP BY run_id",
        ids,
    )
    event_sequence_contiguous = len(seq_rows) == len(ids) and all(
        int(row["last_seq"]) - int(row["first_seq"]) + 1 == int(row["n"]) for row in seq_rows
    )
    exact = (
        len(rows) == len(ids)
        and len(terminal) == len(ids)
        and all(result_counts.get(rid, 0) == 1 for rid in ids)
    )
    duplicate_notes = await pool.fetchval(
        "SELECT coalesce(sum(n - 1),0) FROM (SELECT run_id,tool_call_id,count(*) n "
        "FROM load_save_note_attempts GROUP BY run_id,tool_call_id) counts"
    )
    queue_remaining = await pool.fetchval("SELECT count(*) FROM pgqueuer")
    errors = await pool.fetch(
        "SELECT coalesce(event::jsonb->'event'->'error'->>'type','unknown') kind,count(*) n "
        "FROM run_events WHERE run_id=ANY($1::text[]) AND (event::jsonb->'event'->>'type')='error' "
        "GROUP BY 1",
        ids,
    )
    failed_ids = {row["id"] for row in rows if row["status"] == "failed"}
    failed_error_ids = {
        row["run_id"]
        for row in await pool.fetch(
            "SELECT DISTINCT run_id FROM load_event_audit "
            "WHERE run_id=ANY($1::text[]) AND event_type='error'",
            ids,
        )
    }
    no_error_event_failed = len(failed_ids - failed_error_ids)
    no_events_after_result = await pool.fetchval(
        "WITH result AS (SELECT run_id,max(seq) result_seq FROM load_event_audit "
        "WHERE run_id=ANY($1::text[]) AND event_type='result' GROUP BY run_id) "
        "SELECT count(*) FROM load_event_audit a JOIN result r ON r.run_id=a.run_id "
        "WHERE a.seq>r.result_seq",
        ids,
    )
    owner_conflicts = await pool.fetchval(
        "SELECT count(*) FROM (SELECT run_id,seq,count(DISTINCT actor) n "
        "FROM load_event_audit WHERE run_id=ANY($1::text[]) GROUP BY run_id,seq "
        "HAVING count(DISTINCT actor)>1) conflicts",
        ids,
    )
    owner_missing = await pool.fetchval(
        "SELECT count(*) FROM load_event_audit WHERE run_id=ANY($1::text[]) "
        "AND (owner IS NULL OR actor IS NULL OR actor='')",
        ids,
    )
    actor_owner_mismatches = await pool.fetchval(
        "SELECT count(*) FROM load_event_audit WHERE run_id=ANY($1::text[]) "
        "AND owner IS NOT NULL AND actor IS NOT NULL AND actor<>owner",
        ids,
    )
    actor_owner_mismatches = int(actor_owner_mismatches or 0)
    nonterminal_without_job = await pool.fetchval(
        "SELECT count(*) FROM runs r WHERE r.id=ANY($1::text[]) "
        "AND r.status NOT IN ('completed','failed','aborted') AND NOT EXISTS ("
        "SELECT 1 FROM pgqueuer q WHERE convert_from(q.payload,'UTF8')::jsonb->>'run_id'=r.id)",
        ids,
    )
    error_types: dict[str, int] = {str(row["kind"]): int(row["n"]) for row in errors}
    for row in rows:
        raw_meta = row["meta"]
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
        for error in meta.get("errors", []) if isinstance(meta, dict) else []:
            if isinstance(error, dict):
                kind = str(error.get("type", "unknown"))
                error_types[kind] = error_types.get(kind, 0) + 1
    return {
        "workers": point.workers,
        "concurrency": point.concurrency,
        "target_rate": point.rate,
        "rtt_ms": point.rtt,
        "observed_select_rtt_ms": calibration,
        "steady_start_wall": window["steady_start_wall"],
        "steady_end_wall": steady_end_wall,
        "chaos": {
            "mode": point.chaos,
            "recovery_time_s": drain_time if point.chaos else None,
        },
        "enqueued": len(ids),
        "submitted": len(ids),
        "enqueue_attempts": len(attempted_ids),
        "enqueue_acknowledged": len(acknowledged_ids),
        "enqueue_rejected": len(enqueue_rejected),
        "enqueue_ambiguous_commits": len(enqueue_ambiguous),
        "enqueue_errors_by_type": enqueue_error_types,
        "terminal": len(terminal),
        "completed": completed,
        "steady_submitted": len(steady_id_set),
        "steady_terminal": len(steady_terminal),
        "steady_completed": len(steady_completed),
        "late_completions": late_completions,
        "steady_turns": turns_steady,
        "failed": sum(r["status"] == "failed" for r in terminal),
        "aborted": sum(r["status"] == "aborted" for r in terminal),
        "non_terminal": len(ids) - len(terminal),
        "terminal_exactly_once": exact,
        "duplicate_terminal": duplicate_terminal,
        "terminal_event_duplicates": duplicate_terminal,
        "event_sequence_contiguous": event_sequence_contiguous,
        "duplicate_save_notes": int(duplicate_notes or 0),
        "runs_per_s": len(steady_completed) / scale,
        "turns_per_s": turns_steady / scale,
        "terminal_latency_ms": {
            "p50": _pct(lat, 0.5),
            "p95": _pct(lat, 0.95),
            "p99": _pct(lat, 0.99),
        },
        "queue_wait_ms": {"p50": _pct(waits, 0.5), "p99": _pct(waits, 0.99)},
        "worker_metrics": metrics,
        "missing_worker_metrics": missing_worker_metrics,
        "graceful_stop_workers": graceful_stop_workers,
        "cpu_percent": cpu,
        "stream": stream,
        "postgres": {
            "commits_per_s": (pg_after["commits"] - pg_before["commits"]) / scale,
            "rows_inserted_per_s": (pg_after["rows_inserted"] - pg_before["rows_inserted"]) / scale,
            "wal_bytes_per_s": (pg_after["wal_bytes"] - pg_before["wal_bytes"]) / scale,
            "connections_in_use": pg_after["connections_in_use"],
            "connections_max": pg_after.get("connections_max", pg_after["connections_in_use"]),
            "connections_mean": pg_after.get("connections_mean", pg_after["connections_in_use"]),
            "wait_events": pg_after["wait_events"],
            "wait_event_sample_counts": pg_after.get("wait_event_sample_counts", []),
        },
        "commits_per_turn": (pg_after["commits"] - pg_before["commits"]) / max(1, turns_steady),
        "errors_by_type": error_types,
        "failed_runs_with_error_event": len(failed_ids & failed_error_ids),
        "failed_runs_without_error_event": no_error_event_failed,
        "nonterminal_without_job": int(nonterminal_without_job or 0),
        "event_audit": {
            "owner_conflicts": int(owner_conflicts or 0),
            "owner_missing": int(owner_missing or 0),
            "actor_owner_mismatches": actor_owner_mismatches,
            "actor_owner_mismatch": actor_owner_mismatches,
            "no_events_after_result": int(no_events_after_result or 0),
        },
        "queue_remaining": int(queue_remaining or 0),
        "lease_renew_failures": sum(m.get("lease_renew_failures", 0) for m in metrics),
    }


async def _wait_drain(pool: Any, run_file: Path, timeout: float) -> float | None:
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run_file.exists():
            rows = [json.loads(line) for line in run_file.read_text().splitlines() if line.strip()]
            ids = [row["run_id"] for row in rows]
            if not ids:
                return time.monotonic() - started
            pending = await pool.fetchval(
                "SELECT count(*) FROM runs WHERE id=ANY($1::text[]) "
                "AND status NOT IN ('completed','failed','aborted')",
                ids,
            )
            if not pending:
                return time.monotonic() - started
        await asyncio.sleep(0.25)
    return None


async def _wait_active_owner(pool: Any, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await pool.fetchrow(
            "SELECT id,lease_owner,lease_epoch FROM runs "
            "WHERE lease_owner IS NOT NULL ORDER BY updated_at LIMIT 1"
        )
        if row:
            return dict(row)
        await asyncio.sleep(0.1)
    return None


async def _wait_live_owner(pool: Any, timeout: float) -> dict[str, Any] | None:
    """Select a worker that currently owns a nonterminal, unexpired run."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = await pool.fetchrow(
            "SELECT id,lease_owner,lease_epoch FROM runs "
            "WHERE status NOT IN ('completed','failed','aborted') "
            "AND lease_owner IS NOT NULL AND lease_expires_at > clock_timestamp() "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        if row:
            return dict(row)
        await asyncio.sleep(0.1)
    return None


def _child_command(role: str, **flags: Any) -> list[str]:
    command = [sys.executable, str(Path(__file__)), "--role", role]
    for name, value in flags.items():
        if value is None or value is False:
            continue
        flag = f"--{name.replace('_', '-')}"
        if value is True:
            command.append(flag)
        else:
            command.extend([flag, str(value)])
    return command


async def _wait_process(proc: subprocess.Popen[str], timeout: float) -> bool:
    """Wait for a child with a hard deadline; SIGKILL a stuck child."""

    if proc.poll() is not None:
        return True
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait), max(0.01, timeout))
        return True
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.to_thread(proc.wait), 5)
        return False


async def _stop_children(processes: list[subprocess.Popen[str]], timeout: float) -> bool:
    for proc in processes:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + timeout
    stopped = True
    for proc in processes:
        remaining = max(0.05, deadline - time.monotonic())
        stopped = await _wait_process(proc, remaining) and stopped
    return stopped


async def _finish_task(task: asyncio.Task[Any] | None, timeout: float) -> Any:
    if task is None:
        return None
    try:
        return await asyncio.wait_for(task, max(0.01, timeout))
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return None


def _cpu_breakdown(
    process_cpu: dict[str, float], worker_pids: list[int], proxy_pid: int
) -> dict[str, Any]:
    """Partition sampled CPU, including Postgres PIDs discovered mid-window."""

    worker_keys = {str(pid) for pid in worker_pids}
    proxy_key = str(proxy_pid)
    postgres = {
        pid: value
        for pid, value in process_cpu.items()
        if pid not in worker_keys and pid != proxy_key
    }
    return {
        "processes": process_cpu,
        "workers": {str(pid): process_cpu.get(str(pid), 0.0) for pid in sorted(worker_pids)},
        "postgres": postgres,
        "proxy": process_cpu.get(proxy_key, 0.0),
        "pid_metadata": {
            "worker_pids": sorted(worker_pids),
            "proxy_pid": proxy_pid,
            "postgres_pids": sorted(int(pid) for pid in postgres if pid.isdigit()),
        },
    }


async def run_point(point: Point, dsn: str, *, quick: bool = False) -> dict[str, Any]:
    import asyncpg

    from linch.worker import install

    schema = f"load_{uuid4().hex}"
    tmp = Path(tempfile.mkdtemp(prefix="linch-load-"))
    admin: Any = None
    begin: Any = None
    probe: Any = None
    proxy: subprocess.Popen[str] | None = None
    enqueuer: subprocess.Popen[str] | None = None
    proxy_stdout: Any = None
    proxy_log_handle: Any = None
    log_handles: list[Any] = []
    workers: list[subprocess.Popen[str]] = []
    worker_files: list[Path] = []
    worker_logs: list[Path] = []
    enqueue_file = tmp / "runs.jsonl"
    stream_file = tmp / "streams.json"
    cpu_stop = asyncio.Event()
    cpu_task: asyncio.Task[dict[str, float]] | None = None
    pg_task: asyncio.Task[tuple[dict[str, Any], dict[str, Any]]] | None = None
    chaos: dict[str, Any] = {}
    calibration: dict[str, Any] = {}
    try:
        admin = await asyncpg.connect(dsn)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        child_env = dict(os.environ)
        child_env["PYTHONPATH"] = f"{ROOT / 'src'}:{child_env.get('PYTHONPATH', '')}"

        proxy_log_handle = (tmp / "proxy.stderr.log").open("w")
        proxy = subprocess.Popen(
            _child_command(
                "proxy",
                socket=SOCKET,
                rtt=point.rtt,
                port=0,
                control_port=0,
            ),
            stdout=subprocess.PIPE,
            stderr=proxy_log_handle,
            text=True,
            env=child_env,
        )
        proxy_stdout = proxy.stdout
        if proxy_stdout is None:
            raise RuntimeError("proxy did not expose readiness output")
        try:
            ready_line = await asyncio.wait_for(asyncio.to_thread(proxy_stdout.readline), 10)
            ready = json.loads(ready_line)
        except Exception as exc:
            raise RuntimeError(f"proxy readiness failed: {exc}") from exc

        begin = await _direct_pool(
            dsn,
            schema,
            min_size=1,
            max_size=max(2, point.pool_size),
            application_name="wp18-monitor",
        )
        await install(begin)
        await _install_load_tables(begin)

        for index in range(point.workers):
            metric = tmp / f"worker-{index}.json"
            worker_log = tmp / f"worker-{index}.stderr.log"
            worker_files.append(metric)
            worker_logs.append(worker_log)
            handle = worker_log.open("w")
            log_handles.append(handle)
            workers.append(
                subprocess.Popen(
                    _child_command(
                        "worker",
                        dsn=dsn,
                        port=ready["ready"],
                        schema=schema,
                        worker_id=f"w-{index}",
                        concurrency=point.concurrency,
                        pool_size=point.pool_size,
                        metrics=metric,
                        first_token=point.first_token,
                        delta_window=point.delta_window,
                        tool_low=point.tool_low,
                        tool_high=point.tool_high,
                        lease_ttl=point.lease_ttl,
                        stall_timeout=point.stall_timeout,
                    ),
                    stdout=subprocess.DEVNULL,
                    stderr=handle,
                    text=True,
                    env=child_env,
                )
            )
        await asyncio.sleep(1.0 if not quick else 0.3)

        probe = await _pool(
            dsn,
            int(ready["ready"]),
            schema,
            min_size=1,
            max_size=2,
            application_name="wp18-rtt-probe",
        )
        calibration = {
            "direct": await _calibrate_rtt(begin),
            "proxy": await _calibrate_rtt(probe),
        }

        enqueuer_log = tmp / "enqueuer.stderr.log"
        enqueuer_handle = enqueuer_log.open("w")
        log_handles.append(enqueuer_handle)
        enqueuer = subprocess.Popen(
            _child_command(
                "enqueue",
                dsn=dsn,
                port=ready["ready"],
                schema=schema,
                rate=point.rate,
                warmup=point.warmup,
                duration=point.duration,
                runs=enqueue_file,
                stream_metrics=stream_file,
                stream_sample=point.stream_sample,
                stream_poll=point.stream_poll,
                pool_size=point.pool_size,
                seed=18,
                max_stream_readers=point.max_stream_readers,
                stream_fan_in=point.stream_fan_in,
                enqueue_timeout=point.enqueue_timeout,
            ),
            stdout=subprocess.DEVNULL,
            stderr=enqueuer_handle,
            text=True,
            env=child_env,
        )
        window_path = enqueue_file.with_suffix(".window.json")
        window_deadline = time.monotonic() + max(10.0, point.warmup + 5.0)
        while not window_path.exists() and time.monotonic() < window_deadline:
            await asyncio.sleep(0.05)
        window = (
            json.loads(window_path.read_text())
            if window_path.exists()
            else {
                "steady_start_wall": time.time() + point.warmup,
                "steady_end_wall": time.time() + point.warmup + point.duration,
                "duration": point.duration,
            }
        )
        wall_delta = window["steady_start_wall"] - time.time()
        steady_start = time.monotonic() + wall_delta
        steady_end = steady_start + point.duration
        postgres_pids = _postgres_pids()
        await asyncio.sleep(0.05)
        postgres_pids = sorted(set(postgres_pids + _postgres_pids()))
        cpu_pids = [worker.pid for worker in workers] + postgres_pids + [proxy.pid]
        pg_task = asyncio.create_task(_pg_window(begin, steady_start, steady_end))
        cpu_task = asyncio.create_task(_cpu_monitor(cpu_pids, cpu_stop, steady_start, steady_end))

        chaos_at = max(0.5, point.warmup + point.duration / 3)
        if point.chaos in {"kill", "drain", "blip"}:
            await asyncio.sleep(chaos_at)
        if point.chaos in {"kill", "drain"} and workers:
            active = await _wait_live_owner(begin, 5)
            target = workers[0]
            if active:
                owner = str(active["lease_owner"])
                if owner.startswith("w-"):
                    with contextlib.suppress(ValueError, IndexError):
                        target = workers[int(owner[2:])]
                chaos.update(
                    {
                        "run_id": str(active["id"]),
                        "owner": owner,
                        "epoch": int(active["lease_epoch"]),
                        "target_pid": target.pid,
                    }
                )
            else:
                chaos["error"] = "no_active_lease"
            stop_at = time.time()
            if point.chaos == "kill":
                chaos["kill_started_wall"] = stop_at
                with contextlib.suppress(ProcessLookupError):
                    target.kill()
                await _wait_process(target, 5.0)
                chaos["killed_returncode"] = target.returncode
            else:
                chaos["drain_started_wall"] = stop_at
                with contextlib.suppress(ProcessLookupError):
                    target.send_signal(signal.SIGTERM)
                await asyncio.sleep(0.2)
                if begin is not None and chaos.get("owner"):
                    chaos["post_stop_claims"] = int(
                        await begin.fetchval(
                            "SELECT count(*) FROM load_claims WHERE owner=$1 "
                            "AND claimed_at > to_timestamp($2)",
                            chaos["owner"],
                            stop_at + 0.2,
                        )
                        or 0
                    )
        elif point.chaos == "blip":
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", int(ready["control"])), 5
                )
                writer.write(b"drop 3\n")
                await writer.drain()
                await asyncio.wait_for(reader.readline(), 8)
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 5)
                chaos["proxy_drop_s"] = 3
            except Exception as exc:
                chaos["error"] = f"{type(exc).__name__}: {exc}"

        enqueuer_deadline = max(10.0, point.warmup + point.duration + 20.0)
        if enqueuer is not None and not await _wait_process(enqueuer, enqueuer_deadline):
            chaos["enqueuer_timeout"] = True
        drain_time = await _wait_drain(
            begin, enqueue_file, max(5.0, point.duration if not quick else 8.0)
        )

        if chaos.get("run_id") and begin is not None:
            deadline = time.monotonic() + max(5.0, point.stall_timeout * 2 + point.lease_ttl)
            while time.monotonic() < deadline:
                row = await begin.fetchrow(
                    "SELECT max(epoch) epoch,min(claimed_at) claimed_at "
                    "FROM load_claims WHERE run_id=$1 AND epoch>$2",
                    chaos["run_id"],
                    chaos["epoch"],
                )
                if row and row["epoch"] is not None:
                    chaos["recovered_epoch"] = int(row["epoch"])
                    chaos["recovery_time_s"] = max(
                        0.0,
                        _parse_ts(row["claimed_at"]) - chaos["kill_started_wall"],
                    )
                    chaos["fenced_recovery"] = chaos["recovered_epoch"] > chaos["epoch"]
                    chaos["epoch_increased"] = chaos["fenced_recovery"]
                    owner_violations = await begin.fetchval(
                        "SELECT count(*) FROM load_event_audit WHERE run_id=$1 "
                        "AND (owner IS NULL OR actor IS NULL OR actor<>owner)",
                        chaos["run_id"],
                    )
                    chaos["owner_audit"] = not owner_violations
                    break
                await asyncio.sleep(0.1)
            if "recovered_epoch" not in chaos:
                chaos["recovery_time_s"] = None

        cpu_stop.set()
        process_cpu = await _finish_task(cpu_task, max(10.0, point.duration + 5.0))
        pg_window = await _finish_task(pg_task, max(10.0, point.duration + 5.0))
        if process_cpu is None:
            process_cpu = {}
        if pg_window is None:
            pg_window = (await _pg_stats(begin), await _pg_stats(begin))
        await _stop_children(workers, 10.0)
        if point.chaos == "drain":
            drain_target = next(
                (worker for worker in workers if worker.pid == chaos.get("target_pid")),
                None,
            )
            chaos["drain_returncode"] = (
                drain_target.returncode if drain_target is not None else None
            )
            chaos["drain_finished"] = chaos["drain_returncode"] == 0
        result = await _summarize(
            begin,
            enqueue_file,
            point,
            window,
            pg_window,
            worker_files,
            stream_file,
            _cpu_breakdown(process_cpu, [worker.pid for worker in workers], proxy.pid),
            calibration,
            drain_time,
        )
        result["processes"] = {
            "worker_returncodes": [worker.returncode for worker in workers],
            "enqueuer_returncode": enqueuer.returncode if enqueuer else None,
            "stderr_logs": {
                str(path): path.read_text()[-4000:]
                for path in [*worker_logs, tmp / "enqueuer.stderr.log", tmp / "proxy.stderr.log"]
                if path.exists() and path.stat().st_size
            },
        }
        result["chaos"].update(chaos)
        return result
    finally:
        cpu_stop.set()
        await _finish_task(cpu_task, max(5.0, point.duration + 5.0))
        if pg_task is not None and not pg_task.done():
            pg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(pg_task, 5)
        await _stop_children(workers, 10.0)
        if enqueuer is not None:
            await _wait_process(enqueuer, 10.0)
        if begin is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(begin.close(), 5)
        if probe is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(probe.close(), 5)
        if proxy is not None:
            if proxy.poll() is None:
                proxy.terminate()
            await _wait_process(proxy, 10.0)
        if proxy_stdout is not None:
            proxy_stdout.close()
        if proxy_log_handle is not None:
            proxy_log_handle.close()
        for handle in log_handles:
            handle.close()
        if admin is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'), 5
                )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(admin.close(), 5)


def _make_point(**values: Any) -> Point:
    """Construct a point from fields known by the worker half."""

    fields = getattr(Point, "__dataclass_fields__", {})
    return Point(**{name: value for name, value in values.items() if name in fields})


def _describe_point(point: Point) -> dict[str, Any]:
    fields = getattr(Point, "__dataclass_fields__", {})
    return {name: getattr(point, name) for name in fields}


def _summary_line(result: dict[str, Any], index: int, total: int) -> str:
    if "error" in result:
        point = result.get("point", {})
        return (
            f"[{index}/{total}] ERROR M={point.get('workers')} "
            f"R={point.get('rate')}: {result['error']}"
        )
    latency = result.get("terminal_latency_ms", {}).get("p99")
    return (
        f"[{index}/{total}] M={result.get('workers')} C={result.get('concurrency')} "
        f"R={result.get('target_rate')} RTT={result.get('rtt_ms')}ms "
        f"runs/s={result.get('runs_per_s', 0):.2f} p99={latency}ms "
        f"terminal={result.get('terminal')}/{result.get('enqueued')}"
    )


def _write_report(report: dict[str, Any], output: str | None, *, emit: bool = True) -> None:
    if output:
        Path(output).write_text(json.dumps(report, indent=2, default=str) + "\n")
    if emit:
        print(json.dumps(report, separators=(",", ":"), default=str), flush=True)


def _acceptance(
    result: dict[str, Any],
    *,
    require_completed: bool = False,
    require_stream_readers: int = 0,
    require_chaos_evidence: bool = False,
) -> bool:
    if "error" in result or result.get("non_terminal", 1) != 0:
        return False
    if not result.get("terminal_exactly_once") or result.get("duplicate_terminal", 1):
        return False
    if result.get("duplicate_save_notes", 1) or result.get("queue_remaining", 1):
        return False
    audit = result.get("event_audit", {})
    if any(
        int(audit.get(name, 0) or 0)
        for name in (
            "owner_conflicts",
            "owner_missing",
            "actor_owner_mismatches",
            "actor_owner_mismatch",
            "no_events_after_result",
        )
    ):
        return False
    if result.get("nonterminal_without_job", 0) or result.get("failed_runs_without_error_event", 0):
        return False
    if "event_sequence_contiguous" in result and not result["event_sequence_contiguous"]:
        return False
    stream = result.get("stream", {})
    if stream and not stream.get("in_order", False):
        return False
    if require_stream_readers:
        peak = int(stream.get("peak_concurrent_readers", 0) or 0)
        if peak < require_stream_readers or not stream.get("terminal") or not stream.get("events"):
            return False
    if result.get("failed", 0):
        errors = result.get("error_event_count", result.get("error_events"))
        if errors is None and isinstance(result.get("errors_by_type"), dict):
            errors = sum(int(value) for value in result["errors_by_type"].values())
        if errors is not None and int(errors) < int(result["failed"]):
            return False
    if require_chaos_evidence:
        chaos = result.get("chaos", {})
        active = chaos.get("active_lease")
        if not isinstance(active, dict) or active.get("error"):
            return False
        old_epoch = active.get("epoch")
        new_epoch = chaos.get("recovery_epoch", active.get("recovered_epoch"))
        if active.get("fenced_recovery") is False:
            return False
        if old_epoch is None or new_epoch is None or int(new_epoch) <= int(old_epoch):
            return False
        if chaos.get("epoch_increased") is False or chaos.get("owner_audit") is False:
            return False
    if require_completed and (
        result.get("completed", 0) != result.get("enqueued", 0)
        or result.get("failed", 0)
        or result.get("aborted", 0)
    ):
        return False
    return True


def _point_from(args: argparse.Namespace, **overrides: Any) -> Point:
    values: dict[str, Any] = {
        "workers": args.workers,
        "concurrency": args.concurrency,
        "rate": args.rate,
        "rtt": args.rtt,
        "warmup": args.warmup,
        "duration": args.duration,
        "pool_size": args.pool_size,
        "first_token": args.first_token,
        "delta_window": args.delta_window,
        "tool_low": args.tool_low,
        "tool_high": args.tool_high,
        "stream_sample": args.stream_sample,
        "stream_poll": args.stream_poll,
        "max_stream_readers": args.stream_readers,
        "stream_fan_in": args.stream_fan_in,
        "lease_ttl": args.lease_ttl,
        "stall_timeout": args.stall_timeout,
        "enqueue_timeout": args.enqueue_timeout,
        "chaos": None,
    }
    values.update(overrides)
    return _make_point(**values)


async def _run_points(
    points: list[Point],
    dsn: str,
    args: argparse.Namespace,
    *,
    experiment: str,
    quick: bool = False,
) -> tuple[dict[str, Any], bool]:
    results: list[dict[str, Any]] = []
    accepted = True
    for index, point in enumerate(points, 1):
        try:
            result = await run_point(point, dsn, quick=quick)
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}", "point": _describe_point(point)}
        results.append(result)
        print(_summary_line(result, index, len(points)), file=sys.stderr, flush=True)
        fan_in = getattr(point, "stream_fan_in", 0)
        required_streams = (
            int(getattr(point, "max_stream_readers", 100)) if fan_in is True else int(fan_in or 0)
        )
        strict_chaos = point.chaos == "kill"
        accepted = (
            _acceptance(
                result,
                require_completed=experiment == "quick" and point.chaos is None,
                require_stream_readers=required_streams,
                require_chaos_evidence=strict_chaos,
            )
            and accepted
        )
        if args.output:
            _write_report(
                {"experiment": experiment, "points": results, "complete": index == len(points)},
                args.output,
                emit=False,
            )
    report = {
        "experiment": experiment,
        "points": results,
        "accepted": accepted,
        "point_count": len(points),
    }
    _write_report(report, args.output)
    return report, accepted


def _capacity_rates(args: argparse.Namespace) -> list[float]:
    if args.rates:
        rates = [float(value) for value in args.rates.split(",") if value.strip()]
        if not rates or any(rate <= 0 for rate in rates):
            raise SystemExit("--rates must contain positive numbers")
        return rates
    if args.ramp <= 1:
        raise SystemExit("--ramp must be greater than 1")
    current = max(args.rate, args.baseline_rate, 0.1)
    rates: list[float] = []
    for _ in range(max(1, args.max_points)):
        if current > max(args.max_rate, args.rate, args.baseline_rate, 0.1) * (1 + 1e-9):
            break
        rates.append(current)
        current *= args.ramp
    return rates


async def _capacity(args: argparse.Namespace, dsn: str) -> tuple[dict[str, Any], bool]:
    results: list[dict[str, Any]] = []
    knees: dict[str, Any] = {}
    rates = _capacity_rates(args)
    for workers in (1, 2, 4, 8):
        baseline: float | None = None
        baseline_wait: float | None = None
        backlog_streak = 0
        last_stable: float | None = None
        first_overloaded: float | None = None
        stop_reason = "max_rate_reached"
        tested: list[dict[str, Any]] = []
        for rate in rates:
            point = _point_from(
                args,
                workers=workers,
                concurrency=args.concurrency,
                rate=rate,
                rtt=1,
                stream_sample=max(args.stream_sample, 0.02),
                chaos=None,
            )
            try:
                result = await run_point(point, dsn)
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}", "point": _describe_point(point)}
            tested.append(result)
            results.append(result)
            print(_summary_line(result, len(results), 0), file=sys.stderr, flush=True)
            if args.output:
                _write_report(
                    {"experiment": "capacity", "points": results, "complete": False},
                    args.output,
                    emit=False,
                )
            if "error" in result:
                stop_reason = "point_error"
                break
            p99 = result.get("terminal_latency_ms", {}).get("p99")
            wait99 = result.get("queue_wait_ms", {}).get("p99")
            if baseline is None and p99 is not None:
                baseline, baseline_wait, last_stable = (
                    float(p99),
                    (float(wait99) if wait99 is not None else None),
                    rate,
                )
                continue
            backlog = bool(result.get("non_terminal")) or result.get("queue_remaining", 0) > 0
            backlog_streak = backlog_streak + 1 if backlog else 0
            latency_knee = p99 is not None and baseline is not None and p99 > 2 * baseline
            if latency_knee or backlog_streak >= 2:
                first_overloaded, stop_reason = rate, "latency_or_queue_knee"
                break
            last_stable = rate
        knees[str(workers)] = {
            "knee_rate": first_overloaded,
            "last_stable_rate": last_stable,
            "first_overloaded_rate": first_overloaded,
            "stop_reason": stop_reason,
            "baseline_p99_ms": baseline,
            "baseline_queue_wait_p99_ms": baseline_wait,
            "backlog_streak_at_stop": backlog_streak,
            "tested_rates": [r.get("target_rate") for r in tested if "error" not in r],
        }
    accepted = all(_acceptance(result) for result in results)
    report = {
        "experiment": "capacity",
        "points": results,
        "knees": knees,
        "accepted": accepted,
        "ramp": {"rates": rates, "ramp": args.ramp, "max_rate": args.max_rate},
    }
    _write_report(report, args.output)
    return report, accepted


def _quick_points(args: argparse.Namespace) -> list[Point]:
    if os.getenv("LINCH_LOAD_TEST_SMOKE"):
        return [
            _point_from(
                args,
                workers=2,
                concurrency=8,
                rate=2.0,
                rtt=1,
                warmup=0.2,
                duration=1.5,
                pool_size=8,
                first_token=0.01,
                delta_window=0.02,
                tool_low=0.005,
                tool_high=0.01,
                stream_sample=0.25,
                chaos=None,
            )
        ]
    common = {
        "concurrency": args.concurrency,
        "warmup": min(args.warmup, 3.0),
        "duration": min(args.duration, 15.0),
    }
    specs = [
        (2, 1.5, 1, 0.25, None),
        (4, 1.5, 1, 0.0, "kill"),
        (4, 1.5, 1, 0.0, "blip"),
        (4, 1.5, 1, 0.0, "drain"),
        (4, 1.5, 1, 1.0, None),
    ]
    points: list[Point] = []
    for workers, rate, rtt, sample, chaos in specs:
        overrides = {**common, "workers": workers, "rate": max(args.rate, rate), "rtt": rtt}
        overrides.update({"stream_sample": sample, "chaos": chaos})
        if chaos is None and sample == 1.0:
            overrides.update({"max_stream_readers": 100, "stream_fan_in": True})
        points.append(_point_from(args, **overrides))
    return points


def _experiment_points(args: argparse.Namespace) -> list[Point]:
    if args.experiment == "point":
        return [_point_from(args, chaos=None if args.chaos == "all" else args.chaos)]
    if args.experiment == "network":
        rate = args.knee_rate * args.fraction if args.knee_rate else args.rate
        return [_point_from(args, workers=4, rate=rate, rtt=rtt) for rtt in (0, 1, 2)]
    if args.experiment == "chaos":
        modes = ("kill", "blip", "drain") if args.chaos == "all" else (args.chaos,)
        return [
            _point_from(args, workers=4, rtt=1, stream_sample=0.0, chaos=mode) for mode in modes
        ]
    return [
        _point_from(
            args,
            workers=4,
            rtt=1,
            stream_sample=1.0,
            max_stream_readers=args.stream_readers,
            stream_fan_in=True,
            chaos=None,
        )
    ]


async def main_async(args: argparse.Namespace) -> int:
    dsn = args.dsn or os.getenv("AGENT_KIT_TEST_PG_DSN")
    if not dsn:
        raise SystemExit("AGENT_KIT_TEST_PG_DSN (or --dsn) is required")
    if args.quick or args.experiment == "quick":
        points = _quick_points(args)
        _, accepted = await _run_points(points, dsn, args, experiment="quick", quick=True)
        return 0 if accepted else 1
    if args.experiment == "capacity":
        _, accepted = await _capacity(args, dsn)
        return 0 if accepted else 1
    points = _experiment_points(args)
    _, accepted = await _run_points(points, dsn, args, experiment=args.experiment)
    return 0 if accepted else 1


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "experiment",
        nargs="?",
        choices=("capacity", "network", "chaos", "streams", "point", "quick"),
        default="quick",
    )
    p.add_argument("--quick", action="store_true")
    p.add_argument("--dsn")
    p.add_argument("--output", help="write the complete JSON report to this file")
    p.add_argument("--rate", type=float, default=1.0)
    p.add_argument("--rates", help="comma-separated capacity rates")
    p.add_argument("--baseline-rate", type=float, default=0.5)
    p.add_argument("--max-rate", type=float, default=64.0)
    p.add_argument("--max-points", type=int, default=8)
    p.add_argument("--ramp", type=float, default=2.0)
    p.add_argument("--knee-rate", type=float)
    p.add_argument("--fraction", type=float, default=0.7)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--warmup", type=float, default=15.0)
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--chaos", choices=("all", "kill", "blip", "drain"), default="all")
    p.add_argument("--role", choices=("proxy", "worker", "enqueue"))
    p.add_argument("--socket", default=SOCKET)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--control-port", type=int, default=0)
    p.add_argument("--rtt", type=float, default=0.0)
    p.add_argument("--schema", default="public")
    p.add_argument("--worker-id", default="worker")
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--pool-size", type=int, default=8)
    p.add_argument("--lease-ttl", type=float, default=2.0)
    p.add_argument("--stall-timeout", type=float, default=4.0)
    p.add_argument("--enqueue-timeout", type=float, default=10.0)
    p.add_argument("--metrics", default="worker-metrics.json")
    p.add_argument("--runs", default="runs.jsonl")
    p.add_argument("--stream-metrics", default="stream-metrics.json")
    p.add_argument("--stream-sample", type=float, default=0.1)
    p.add_argument("--stream-poll", type=float, default=0.05)
    p.add_argument("--stream-readers", type=int, default=100)
    p.add_argument("--max-stream-readers", type=int)
    p.add_argument("--stream-fan-in", action="store_true")
    p.add_argument("--first-token", type=float, default=0.3)
    p.add_argument("--delta-window", type=float, default=0.7)
    p.add_argument("--tool-low", type=float, default=0.03)
    p.add_argument("--tool-high", type=float, default=0.08)
    p.add_argument("--seed", type=int, default=18)
    return p


def main() -> None:
    args = parser().parse_args()
    if args.max_stream_readers is not None:
        args.stream_readers = args.max_stream_readers
    args.max_stream_readers = args.stream_readers
    if args.role == "proxy":
        asyncio.run(proxy_role(args))
    elif args.role == "worker":
        asyncio.run(worker_role(args))
    elif args.role == "enqueue":
        asyncio.run(enqueue_role(args))
    else:
        raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
