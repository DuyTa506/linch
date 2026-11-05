from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from linch import Agent, InMemoryRunStore  # noqa: E402
from linch.events import (  # noqa: E402
    AssistantEvent,
    ErrorEvent,
    PartialAssistantEvent,
    ResultEvent,
    SystemEvent,
    ToolCallEndEvent,
)
from linch.run_store import (  # noqa: E402
    _json_safe_stored_event,
    checkpoint_to_dict,
)
from linch.sessions import InMemorySessionStore  # noqa: E402
from linch.tools import ToolContext, ToolRegistry, ToolResult  # noqa: E402
from linch.types import Usage, message_to_dict  # noqa: E402

# Memory cases validate correctness; no database-specific latency baseline applies.
DEFAULT_THRESHOLDS_MS: dict[str, float] = {}


class DeltaProvider:
    id = "delta-bench"

    def __init__(self, *, deltas: int = 200, tool_names: list[str] | None = None) -> None:
        self.deltas = deltas
        self.tool_names = list(tool_names or [])
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self.calls == 1 and self.tool_names:
            for index, name in enumerate(self.tool_names, start=1):
                tool_id = f"call-{index}"
                yield {"type": "tool_use_start", "id": tool_id, "name": name}
                for chunk in ('{"value":"', name, '"}'):
                    yield {"type": "tool_use_input_delta", "id": tool_id, "json_delta": chunk}
                yield {"type": "tool_use_end", "id": tool_id}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            return
        for _ in range(self.deltas):
            yield {"type": "text_delta", "text": "x"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


class BenchTool:
    description = "Benchmark read tool."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    scope = "read"
    parallel = True

    def __init__(self, name: str, *, delay: float = 0.005) -> None:
        self.name = name
        self.delay = delay

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(self.delay)
        return ToolResult(content=f"{self.name}:ok")


def registry(names: list[str]) -> ToolRegistry:
    tools = ToolRegistry()
    for name in names:
        tools.register(BenchTool(name))
    return tools


@dataclass(slots=True, frozen=True)
class Sample:
    elapsed_ms: float
    event_count: int | None = None


@dataclass(slots=True, frozen=True)
class BenchmarkResult:
    name: str
    samples_ms: list[float]
    median_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    event_counts: list[int]
    correct: bool = True
    threshold_ms: float | None = None
    passed: bool = True


@dataclass(slots=True, frozen=True)
class BenchmarkCase:
    name: str
    measure: Callable[[], Awaitable[Sample]]
    close: Callable[[], Awaitable[None]] | None = None


def percentile(values: Sequence[float], percentile_value: float) -> float:
    """Return a nearest-rank percentile, including sensible singleton behavior."""
    if not values:
        raise ValueError("cannot calculate a percentile of no samples")
    if not 0 < percentile_value <= 100:
        raise ValueError("percentile must be greater than 0 and at most 100")
    ordered = sorted(values)
    rank = math.ceil((percentile_value / 100) * len(ordered))
    return float(ordered[rank - 1])


def _validate_agent_events(events: list[object], *, deltas: int, tools: int) -> None:
    text_events = [
        event
        for event in events
        if isinstance(event, PartialAssistantEvent) and event.delta.get("kind") == "text"
    ]
    tool_events = [event for event in events if isinstance(event, ToolCallEndEvent)]
    results = [event for event in events if isinstance(event, ResultEvent)]
    if len(text_events) != deltas:
        raise AssertionError(f"expected {deltas} text deltas, got {len(text_events)}")
    if len(tool_events) != tools:
        raise AssertionError(f"expected {tools} completed tools, got {len(tool_events)}")
    if any(event.is_error for event in tool_events):
        raise AssertionError("a benchmark tool returned an error")
    if len(results) != 1 or results[0].subtype != "success":
        raise AssertionError("benchmark run did not produce exactly one successful result")
    if results[0].final_text != "x" * deltas:
        raise AssertionError("benchmark result text does not match provider output")


async def _measure_agent(agent: Agent, prompt: str, *, deltas: int, tools: int) -> Sample:
    try:
        session = await agent.session()
        started = time.perf_counter()
        events = [event async for event in session.run(prompt)]
        elapsed_ms = (time.perf_counter() - started) * 1000
        _validate_agent_events(events, deltas=deltas, tools=tools)
        return Sample(elapsed_ms=elapsed_ms, event_count=len(events))
    finally:
        await agent.close()


def _no_tool_case() -> BenchmarkCase:
    async def measure() -> Sample:
        return await _measure_agent(
            Agent(
                model="gpt-5",
                provider=DeltaProvider(deltas=200),
                session_store=InMemorySessionStore(),
                permissions={"mode": "skip-dangerous"},
                result_offload=None,
                include_partial_messages=True,
            ),
            "stream",
            deltas=200,
            tools=0,
        )

    return BenchmarkCase("no_tool_stream_200_deltas", measure)


def _tool_case(
    *,
    name: str,
    count: int,
    run_store_factory: Callable[[], object] | None = None,
    tool_batching_strategy: str = "greedy",
) -> BenchmarkCase:
    async def measure() -> Sample:
        tool_names = [f"Read{i}" for i in range(count)]
        return await _measure_agent(
            Agent(
                model="gpt-5",
                provider=DeltaProvider(deltas=10, tool_names=tool_names),
                tools=registry(tool_names),
                session_store=InMemorySessionStore(),
                run_store=run_store_factory() if run_store_factory is not None else None,
                permissions={"mode": "skip-dangerous"},
                max_tool_concurrency=count,
                tool_batching_strategy=tool_batching_strategy,
                result_offload=None,
                include_partial_messages=True,
            ),
            "use tools",
            deltas=10,
            tools=count,
        )

    return BenchmarkCase(name, measure)


def build_cases(workdir: Path) -> dict[str, BenchmarkCase]:
    operation_store = InMemoryRunStore()
    operation_run_id: str | None = None
    event_write_store = InMemoryRunStore()

    async def memory_operation() -> Sample:
        nonlocal operation_run_id
        if operation_run_id is None:
            run = await operation_store.create_run("benchmark-session")
            operation_run_id = run.id
        started = time.perf_counter()
        loaded = await operation_store.load_run(operation_run_id)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if loaded is None or loaded.id != operation_run_id:
            raise AssertionError("Memory operation returned the wrong run")
        return Sample(elapsed_ms=elapsed_ms)

    async def session_churn() -> Sample:
        # Lifecycle churn (WS1): create/run/release many sessions on one agent
        # without leaking registry entries or work.
        agent = Agent(
            model="gpt-5",
            provider=DeltaProvider(deltas=5),
            session_store=InMemorySessionStore(),
            permissions={"mode": "skip-dangerous"},
            result_offload=None,
        )
        try:
            started = time.perf_counter()
            for _ in range(25):
                session = await agent.session()
                events = [event async for event in session.run("go")]
                if not any(
                    isinstance(event, ResultEvent) and event.subtype == "success"
                    for event in events
                ):
                    raise AssertionError("session churn run did not complete successfully")
                await agent.release_session(session)
            elapsed_ms = (time.perf_counter() - started) * 1000
            return Sample(elapsed_ms=elapsed_ms, event_count=25)
        finally:
            await agent.close()

    async def event_writes() -> Sample:
        run = await event_write_store.create_run(f"benchmark-session-{uuid4()}")
        started = time.perf_counter()
        sequences = [
            await event_write_store.append_event(
                run.id,
                SystemEvent(
                    session_id=run.session_id,
                    run_id=run.id,
                    model=f"write-{index}",
                    tools=[],
                    permission_mode="skip-dangerous",
                    cwd=".",
                ),
            )
            for index in range(50)
        ]
        elapsed_ms = (time.perf_counter() - started) * 1000

        if sequences != list(range(1, 51)):
            raise AssertionError(f"Memory event sequences were not contiguous: {sequences!r}")
        persisted = await event_write_store.load_events(run.id)
        persisted_models = [
            row.event.model if isinstance(row.event, SystemEvent) else None for row in persisted
        ]
        if [row.seq for row in persisted] != sequences:
            raise AssertionError("Memory persisted event sequences in the wrong order")
        if persisted_models != [f"write-{index}" for index in range(50)]:
            raise AssertionError("Memory persisted event payloads in the wrong order")
        return Sample(elapsed_ms=elapsed_ms, event_count=len(persisted))

    return {
        case.name: case
        for case in (
            _no_tool_case(),
            _tool_case(name="parallel_8_read_tools", count=8),
            _tool_case(
                name="checkpoint_memory_8_read_tools",
                count=8,
                run_store_factory=InMemoryRunStore,
            ),
            _tool_case(
                name="checkpoint_memory_32_read_tools", count=32, run_store_factory=InMemoryRunStore
            ),
            _tool_case(
                name="scheduler_maximal_16_read_tools",
                count=16,
                tool_batching_strategy="maximal",
            ),
            BenchmarkCase("session_churn_25", session_churn),
            BenchmarkCase(
                "memory_trivial_operation", memory_operation, close=operation_store.close
            ),
            BenchmarkCase("run_store_50_event_writes", event_writes, close=event_write_store.close),
        )
    }


async def run_case(
    case: BenchmarkCase,
    *,
    warmups: int,
    samples: int,
    threshold_ms: float | None,
) -> BenchmarkResult:
    for _ in range(warmups):
        await case.measure()

    measured = [await case.measure() for _ in range(samples)]
    times = [sample.elapsed_ms for sample in measured]
    event_counts = [sample.event_count for sample in measured if sample.event_count is not None]
    p95_ms = percentile(times, 95)
    return BenchmarkResult(
        name=case.name,
        samples_ms=times,
        median_ms=statistics.median(times),
        p95_ms=p95_ms,
        min_ms=min(times),
        max_ms=max(times),
        event_counts=event_counts,
        threshold_ms=threshold_ms,
        passed=threshold_ms is None or p95_ms <= threshold_ms,
    )


async def run_suite(
    cases: Sequence[BenchmarkCase],
    *,
    warmups: int,
    samples: int,
    thresholds: dict[str, float],
) -> list[BenchmarkResult]:
    try:
        return [
            await run_case(
                case,
                warmups=warmups,
                samples=samples,
                threshold_ms=thresholds.get(case.name),
            )
            for case in cases
        ]
    finally:
        for case in cases:
            if case.close is not None:
                await case.close()


def format_human(results: Sequence[BenchmarkResult]) -> str:
    lines: list[str] = []
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        event_text = (
            f", events={min(result.event_counts)}-{max(result.event_counts)}"
            if result.event_counts
            else ""
        )
        threshold_text = (
            f", p95_limit={result.threshold_ms:.1f}ms" if result.threshold_ms is not None else ""
        )
        lines.append(
            f"{result.name}: median={result.median_ms:.3f}ms, "
            f"p95={result.p95_ms:.3f}ms, min={result.min_ms:.3f}ms, "
            f"max={result.max_ms:.3f}ms{event_text}{threshold_text} [{status}]"
        )
    return "\n".join(lines)


def format_json(results: Sequence[BenchmarkResult], *, warmups: int, samples: int) -> str:
    # Schema v2: metrics are keyed by case name (a named-metric map) rather than a
    # positional array, so consumers can look a case up without scanning and new
    # cases don't shift array indices. Readers should tolerate unknown case keys.
    payload = {
        "schema_version": 2,
        "warmups": warmups,
        "samples": samples,
        "metrics": {result.name: asdict(result) for result in results},
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# Scale cases are opt-in and each CLI case runs in a fresh process. The fake
# provider is deliberately history-driven: sharing it cannot mix session turns.
@dataclass(slots=True, frozen=True)
class ScaleCase:
    name: str
    backend: str
    sessions: int
    turns: int


def build_scale_cases() -> dict[str, ScaleCase]:
    return {
        case.name: case
        for backend in ("memory", "postgres")
        for sessions, turns in ((200, 4), (1000, 4), (1, 50))
        for case in [ScaleCase(f"scale_{backend}_{sessions}x{turns}", backend, sessions, turns)]
    }


def scale_case_skip_reason(case: ScaleCase) -> str | None:
    if case.backend != "postgres":
        return None
    if not os.environ.get("AGENT_KIT_TEST_PG_DSN"):
        return "AGENT_KIT_TEST_PG_DSN not set"
    try:
        import asyncpg  # noqa: F401
    except ModuleNotFoundError:
        return "asyncpg is not installed"
    return None


_SCHEMA_PREFIX = re.compile(r"linch_scale_[0-9a-f]{32}_\Z")
_SCHEMA_SUFFIX = re.compile(r"[a-z0-9_]+\Z")
_SCHEMA_NAME = re.compile(r"linch_scale_[0-9a-f]{32}_[a-z0-9_]+\Z")


def _new_scale_schema_prefix() -> str:
    return f"linch_scale_{uuid4().hex}_"


def _owned_scale_schema(name: str, prefix: str) -> bool:
    return (
        bool(_SCHEMA_PREFIX.fullmatch(prefix))
        and name.startswith(prefix)
        and bool(_SCHEMA_SUFFIX.fullmatch(name[len(prefix) :]))
    )


def _base_dsn(raw_dsn: str) -> str:
    parts = urlsplit(raw_dsn)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "search_path"
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _sweep_postgres_schemas(raw_dsn: str, prefix: str | None = None) -> None:
    import asyncpg

    if prefix is not None and not _SCHEMA_PREFIX.fullmatch(prefix):
        raise ValueError(f"invalid benchmark schema prefix: {prefix!r}")
    connection = await asyncpg.connect(_base_dsn(raw_dsn))
    try:
        names = await connection.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE $1",
            f"{prefix}%" if prefix is not None else "linch_scale_%",
        )
        for row in names:
            name = str(row["nspname"])
            if _SCHEMA_NAME.fullmatch(name) and (prefix is None or name.startswith(prefix)):
                await connection.execute(f'DROP SCHEMA "{name}" CASCADE')
    finally:
        await connection.close()


def _scale_schema_name(prefix: str, case: ScaleCase) -> str:
    suffix = re.sub(r"[^a-z0-9_]+", "_", f"{case.backend}_{case.sessions}x{case.turns}".lower())
    name = f"{prefix}{suffix}"
    if not _owned_scale_schema(name, prefix) or len(name) > 63:
        raise ValueError(f"invalid benchmark schema name: {name!r}")
    return name


async def _fresh_postgres_dsn(raw_dsn: str, prefix: str, case: ScaleCase) -> str:
    import asyncpg

    schema = _scale_schema_name(prefix, case)
    connection = await asyncpg.connect(_base_dsn(raw_dsn))
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
    finally:
        await connection.close()
    parts = urlsplit(_base_dsn(raw_dsn))
    query = [*parse_qsl(parts.query, keep_blank_values=True), ("search_path", schema)]
    return urlunsplit(parts._replace(query=urlencode(query)))


class ScaleProvider:
    id = "scale-bench"

    def __init__(self, *, turns: int, delay: float = 0.002) -> None:
        self.turns, self.delay = turns, delay

    def context_window(self, model: str) -> int:
        return 1_000_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        turn = sum(message.role == "assistant" for message in req.messages)
        await asyncio.sleep(self.delay)
        yield {"type": "message_start", "model": req.model}
        if turn == self.turns - 1:
            yield {"type": "text_delta", "text": "scale:ok"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
            return
        names = (
            ["rag_search", "sql_query"]
            if turn == 0
            else ["save_note"]
            if turn == self.turns - 2
            else ["rag_search"]
        )
        for name in names:
            tool_id = f"{turn}-{name}"
            yield {"type": "tool_use_start", "id": tool_id, "name": name}
            yield {
                "type": "tool_use_input_delta",
                "id": tool_id,
                "json_delta": json.dumps({"value": f"{turn}:{name}"}),
            }
            yield {"type": "tool_use_end", "id": tool_id}
        yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}


class ScaleTool:
    description = "Offline scale benchmark tool."
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    def __init__(self, name: str, active: dict[str, int], overlaps: set[str]) -> None:
        self.name, self.active, self.overlaps = name, active, overlaps
        self.scope = "write" if name == "save_note" else "read"
        self.parallel = self.scope == "read"

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        first_read = input["value"].startswith("0:") and self.scope == "read"
        if first_read:
            self.active[ctx.session_id] = self.active.get(ctx.session_id, 0) + 1
            if self.active[ctx.session_id] == 2:
                self.overlaps.add(ctx.session_id)
        try:
            await asyncio.sleep(0.002)
            return ToolResult(content=f"{self.name}:{input['value']}:ok")
        finally:
            if first_read:
                self.active[ctx.session_id] -= 1


class ScaleStoreMetrics:
    def __init__(self) -> None:
        self.turn = ContextVar("scale_turn", default=-1)
        self.capture_payload_bytes = os.environ.get("LINCH_SCALE_PROFILE_LEAN") != "1"
        self.writes: Counter[tuple[str, str, int]] = Counter()
        self.payloads: dict[tuple[int, str], list[int]] = defaultdict(list)
        self.event_payloads: dict[int, list[int]] = defaultdict(list)
        self.session_payloads: dict[int, list[int]] = defaultdict(list)
        self.sql_round_trips: Counter[tuple[str, int]] = Counter()
        self.transaction_commands: Counter[tuple[str, int]] = Counter()
        self.pool_reset_commands: Counter[tuple[str, int]] = Counter()
        self.protocol_exchanges: Counter[tuple[str, int, str]] = Counter()
        self.protocol_transaction_commands: Counter[tuple[str, int]] = Counter()
        self.protocol_failures: Counter[tuple[str, int, str]] = Counter()

    def reset_round_trip_metrics(self) -> None:
        for counter in (
            self.sql_round_trips,
            self.transaction_commands,
            self.pool_reset_commands,
            self.protocol_exchanges,
            self.protocol_transaction_commands,
            self.protocol_failures,
        ):
            counter.clear()

    def record_protocol_exchange(self, store: str, method: str, query: object = None) -> None:
        turn = self.turn.get()
        self.protocol_exchanges[store, turn, method] += 1
        if method == "query" and str(query or "").lstrip().upper().startswith(
            ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT")
        ):
            self.protocol_transaction_commands[store, turn] += 1

    def summaries(self, *, turns: int) -> tuple[dict[str, Any], dict[str, Any]]:
        stores: dict[str, Counter[str]] = defaultdict(Counter)
        by_turn: dict[int, Counter[str]] = defaultdict(Counter)
        for (store, method, turn), count in self.writes.items():
            stores[store][method] += count
            by_turn[turn][f"{store}.{method}"] += count
        writes = sum(self.writes.values())
        payload_turns: dict[int, list[int]] = defaultdict(list)
        phases: dict[str, list[int]] = defaultdict(list)
        for (turn, phase), sizes in self.payloads.items():
            payload_turns[turn].extend(sizes)
            phases[phase].extend(sizes)

        def sizes_summary(sizes: list[int]) -> dict[str, Any]:
            return {
                "count": len(sizes),
                "total": sum(sizes),
                "min": min(sizes),
                "max": max(sizes),
                "mean": statistics.mean(sizes),
            }

        total_bytes = sum(sum(sizes) for sizes in payload_turns.values())

        def byte_summary(values: dict[int, list[int]]) -> dict[str, Any]:
            by_turn = [
                {"turn": turn, **sizes_summary(sizes)} for turn, sizes in sorted(values.items())
            ]
            total = sum(row["total"] for row in by_turn)
            return {
                "total": total,
                "per_provider_turn": total / turns if turns else None,
                "by_turn": by_turn,
                "encoding": "UTF-8 JSON payloads before the store call",
            }

        return (
            {
                "total": writes,
                "per_provider_turn": writes / turns if turns else None,
                "by_store": {name: dict(methods) for name, methods in stores.items()},
                "by_turn": [
                    {"turn": turn, "total": sum(methods.values()), "methods": dict(methods)}
                    for turn, methods in sorted(by_turn.items())
                ],
                "unit": "successful external mutation API calls, not SQL statements or rows",
                "attribution": "-1: setup; then latest provider_pending checkpoint turn; "
                "checkpoint calls use their own turn; cleanup uses final turn",
            },
            {
                "total": total_bytes,
                "per_provider_turn": total_bytes / turns if turns else None,
                "by_turn": [
                    {"turn": turn, **sizes_summary(sizes)}
                    for turn, sizes in sorted(payload_turns.items())
                ],
                "by_phase": {phase: sizes_summary(sizes) for phase, sizes in phases.items()},
                "encoding": "checkpoint_to_dict, json.dumps(allow_nan=False), UTF-8; "
                "includes terminal checkpoint payloads; -1 is setup",
                "event_bytes": byte_summary(self.event_payloads),
                "session_append_bytes": byte_summary(self.session_payloads),
            },
        )

    def round_trip_summary(self, *, turns: int) -> dict[str, Any]:
        stores = sorted(
            {store for store, _ in self.sql_round_trips}
            | {store for store, _, _ in self.protocol_exchanges}
        )

        def total(counter: Counter[Any], store: str) -> int:
            return sum(value for key, value in counter.items() if key[0] == store)

        def summary(store: str) -> dict[str, Any]:
            statement_calls = total(self.sql_round_trips, store)
            txns = total(self.transaction_commands, store)
            resets = total(self.pool_reset_commands, store)
            protocol_txns = total(self.protocol_transaction_commands, store)
            failures = total(self.protocol_failures, store)
            protocol_total = total(self.protocol_exchanges, store)
            return {
                "round_trips": statement_calls,
                "application_statements": statement_calls - txns - resets,
                "transaction_commands": txns,
                "pool_reset_commands": resets,
                "protocol_exchanges": protocol_total,
                "protocol_exchanges_per_provider_turn": protocol_total / turns if turns else None,
                "protocol_transaction_commands": protocol_txns,
                "protocol_failures": failures,
                "per_provider_turn": statement_calls / turns if turns else None,
            }

        return {"by_store": {store: summary(store) for store in stores}}


def _instrument_connection_protocol(
    connection: Any, metrics: ScaleStoreMetrics, store: str
) -> None:
    protocol = connection._protocol
    if getattr(protocol, "_wp8_instrumented", False):
        return

    class ProtocolProxy:
        __slots__ = ("inner", "_wp8_instrumented")

        def __init__(self, inner: Any) -> None:
            self.inner = inner
            self._wp8_instrumented = True

        async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
            query = (
                args[1]
                if method == "prepare" and len(args) > 1
                else args[0]
                if method == "query" and args
                else getattr(kwargs.get("state", args[0] if args else None), "query", None)
            )
            metrics.record_protocol_exchange(store, method, query)
            if method in {"bind_execute", "bind_execute_many", "execute"}:
                metrics.sql_round_trips[store, metrics.turn.get()] += 1
            elif method == "query" and str(query or "").lstrip().upper().startswith(
                ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT")
            ):
                metrics.sql_round_trips[store, metrics.turn.get()] += 1
                metrics.transaction_commands[store, metrics.turn.get()] += 1
            try:
                return await getattr(self.inner, method)(*args, **kwargs)
            except BaseException:
                metrics.protocol_failures[store, metrics.turn.get(), method] += 1
                raise

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    def wrapper(method: str) -> Callable[..., Awaitable[Any]]:
        async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
            return await self._call(method, *args, **kwargs)

        return call

    for method in (
        "prepare",
        "bind_execute",
        "bind_execute_many",
        "execute",
        "query",
        "close_statement",
    ):
        setattr(ProtocolProxy, method, wrapper(method))
    connection._protocol = ProtocolProxy(protocol)


def _instrument_connection_class(metrics: ScaleStoreMetrics, store: str) -> Any:
    import asyncpg

    class InstrumentedConnection(asyncpg.Connection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            _instrument_connection_protocol(self, metrics, store)

        async def reset(self, *args: Any, **kwargs: Any) -> Any:
            turn = metrics.turn.get()
            metrics.pool_reset_commands[store, turn] += 1
            metrics.sql_round_trips[store, turn] += 1
            return await super().reset(*args, **kwargs)

    return InstrumentedConnection


class MeasuredStore:
    """Benchmark-only accounting wrapper; it preserves the store API surface."""

    _mutations = {
        "session": {
            "create",
            "create_if_absent",
            "append_messages",
            "enqueue_inbox",
            "commit_inbox",
            "save_provider_snapshot",
            "update_meta",
            "set_invoked_skills",
            "delete",
            "create_task",
            "update_task",
            "delete_task",
            "claim_task",
            "release_task",
        },
        "run": {
            "create_run",
            "save_checkpoint",
            "save_checkpoint_with_events",
            "append_event",
            "append_events",
            "mark_completed",
            "mark_failed",
            "save",
            "delete",
            "prune",
        },
    }

    def __init__(self, inner: Any, name: str, metrics: ScaleStoreMetrics) -> None:
        self.inner, self.name, self.metrics = inner, name, metrics

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.inner, name)
        if name not in self._mutations[self.name]:
            return method

        async def measured(*args: Any, **kwargs: Any) -> Any:
            checkpoint = None
            turn = self.metrics.turn.get()
            if self.name == "run" and name in {
                "save_checkpoint",
                "save_checkpoint_with_events",
                "mark_completed",
                "mark_failed",
            }:
                checkpoint = args[1] if len(args) > 1 else kwargs.get("checkpoint")
                if checkpoint is not None:
                    turn = (
                        -1
                        if checkpoint.phase in {"started", "user_appended"}
                        else checkpoint.turn_index
                    )
                    if checkpoint.phase == "provider_pending":
                        self.metrics.turn.set(turn)
            result = await method(*args, **kwargs)
            self.metrics.writes[self.name, name, turn] += 1
            if (
                self.name == "session"
                and name == "append_messages"
                and self.metrics.capture_payload_bytes
            ):
                messages = args[1] if len(args) > 1 else kwargs.get("messages", [])
                self.metrics.session_payloads[turn].extend(
                    len(json.dumps(message_to_dict(message), allow_nan=False).encode())
                    for message in messages
                )
            if (
                self.name == "run"
                and name
                in {"append_event", "append_events", "save_checkpoint_with_events", "mark_failed"}
                and self.metrics.capture_payload_bytes
            ):
                if name == "mark_failed":
                    events = kwargs.get("events", [])
                elif name == "save_checkpoint_with_events":
                    events = args[2] if len(args) > 2 else kwargs.get("events", [])
                else:
                    events = args[1] if len(args) > 1 else kwargs.get("events", [])
                    events = [events] if name == "append_event" else events
                if events:
                    self.metrics.event_payloads[turn].extend(
                        len(json.dumps(_json_safe_stored_event(event), allow_nan=False).encode())
                        for event in events
                    )
            if self.name == "run" and name in {
                "create_run",
                "save_checkpoint",
                "save_checkpoint_with_events",
                "mark_completed",
                "mark_failed",
            }:
                # Write methods return no record; the argument is what was stored
                # (mark_* set its phase before persisting).
                persisted = result.checkpoint if name == "create_run" else checkpoint
                if persisted is not None and self.metrics.capture_payload_bytes:
                    size = len(json.dumps(checkpoint_to_dict(persisted), allow_nan=False).encode())
                    self.metrics.payloads[turn, persisted.phase].append(size)
            return result

        return measured


class LagSampler:
    def __init__(self, *, interval: float = 0.01) -> None:
        self.interval = interval
        self.samples: list[float] = []
        self.deadline = 0.0
        self.peak_rss = _rss_current()
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.deadline = time.perf_counter() + self.interval
        self.task = asyncio.create_task(self._run())
        await asyncio.sleep(0)

    def _capture(self) -> None:
        self.peak_rss = max(self.peak_rss, _rss_current())
        now = time.perf_counter()
        if now >= self.deadline:
            self.samples.append((now - self.deadline) * 1000)
            self.deadline = now + self.interval

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(max(0, self.deadline - time.perf_counter()))
            self._capture()

    async def stop(self) -> dict[str, Any]:
        self._capture()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        return {
            "interval": self.interval * 1000,
            "samples": len(self.samples),
            "p99": percentile(self.samples, 99) if self.samples else 0.0,
            "max": max(self.samples, default=0.0),
        }


def _rss_high_water() -> int:
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _rss_current() -> int:
    try:
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return _rss_high_water()


def scale_rss_summary(
    *, baseline: int, initial_hwm: int, sampled_peak: int, final_hwm: int, sessions: int
) -> dict[str, Any]:
    peak = max(sampled_peak, final_hwm if final_hwm > initial_hwm else baseline)
    growth = max(0, peak - baseline)
    return {
        "baseline_current_bytes": baseline,
        "initial_high_water_bytes": initial_hwm,
        "peak_high_water_bytes": final_hwm,
        "sampled_peak_rss_bytes": sampled_peak,
        "peak_rss_bytes": peak,
        "peak_delta_bytes": growth,
        "peak_delta_bytes_per_session": growth / sessions,
        "import_high_water_floor_bytes": max(0, initial_hwm - baseline),
        "method": "10ms /proc current RSS samples including final RSS; use ru_maxrss "
        "when it exceeds initial HWM; below import HWM brief unsampled peaks can be missed",
    }


async def run_scale_case(
    case: ScaleCase,
    workdir: Path,
    *,
    progress_path: Path | None = None,
    postgres_prefix: str | None = None,
) -> dict[str, Any]:
    if case.backend not in {"memory", "postgres"} or case.sessions < 1 or case.turns < 4:
        raise ValueError(
            "scale cases need memory or postgres, positive sessions, and at least 4 turns"
        )
    reason = scale_case_skip_reason(case)
    if reason is not None:
        raise RuntimeError(reason)
    baseline_rss, initial_hwm = _rss_current(), _rss_high_water()
    metrics = ScaleStoreMetrics()
    session_pool: Any | None = None
    run_pool: Any | None = None
    prefix = postgres_prefix
    raw_dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN")

    if case.backend == "memory":
        session_store: Any = InMemorySessionStore()
        run_store: Any = InMemoryRunStore()
    else:
        import asyncpg

        from linch.run_stores.postgres import PostgresRunStore
        from linch.sessions.postgres import PostgresSessionStore

        if prefix is None:
            await _sweep_postgres_schemas(raw_dsn or "")
            prefix = _new_scale_schema_prefix()
        dsn = await _fresh_postgres_dsn(raw_dsn or "", prefix, case)
        try:
            from linch.storage import _pg as postgres_runtime
        except ImportError:
            postgres_runtime = None
        reset = getattr(postgres_runtime, "_reset_connection", None)
        common = {
            "min_size": 1,
            "max_size": 10,
            "connection_class": _instrument_connection_class(metrics, "session"),
        }
        if reset is not None:
            common["reset"] = reset
        session_pool = await asyncpg.create_pool(dsn, **common)
        run_kwargs = dict(common)
        run_kwargs["connection_class"] = _instrument_connection_class(metrics, "run")
        run_pool = await asyncpg.create_pool(dsn, **run_kwargs)
        session_store = PostgresSessionStore(dsn, pool=session_pool)
        run_store = PostgresRunStore(dsn, pool=run_pool)
        await session_store._ensure()
        await run_store._ensure()
        metrics.reset_round_trip_metrics()

    active: dict[str, int] = {}
    overlaps: set[str] = set()
    tools = ToolRegistry()
    for name in ("rag_search", "sql_query", "save_note"):
        tools.register(ScaleTool(name, active, overlaps))
    agent: Agent | None = None
    sampler: LagSampler | None = None
    result: dict[str, Any] = {
        **asdict(case),
        "pid": os.getpid(),
        "completed": 0,
        "failed": 0,
        "errors": [],
        "observed_provider_turns": 0,
        "observed_tool_calls": 0,
        "timed_out": False,
    }
    latencies: list[float] = []
    started = time.perf_counter()
    last_progress = 0.0

    def progress(*, force: bool = False) -> None:
        nonlocal last_progress
        now = time.perf_counter()
        if progress_path is None or (not force and now - last_progress < 0.25):
            return
        partial = {**result, "elapsed_s": now - started}
        partial["store_writes"], partial["checkpoint_bytes"] = metrics.summaries(
            turns=result["observed_provider_turns"]
        )
        path = progress_path.with_suffix(".tmp")
        path.write_text(json.dumps(partial, allow_nan=False))
        path.replace(progress_path)
        last_progress = now

    async def run_one() -> None:
        metrics.turn.set(-1)
        began = time.perf_counter()
        turns, calls, results = 0, Counter(), []
        try:
            session = await agent.session()  # type: ignore[union-attr]
            async for event in session.run("offline scale benchmark"):
                if isinstance(event, AssistantEvent):
                    turns += 1
                    result["observed_provider_turns"] += 1
                elif isinstance(event, ToolCallEndEvent):
                    calls[event.tool_name] += 1
                    result["observed_tool_calls"] += 1
                    if event.is_error:
                        raise AssertionError(f"tool error: {event.tool_name}")
                elif isinstance(event, ErrorEvent):
                    raise AssertionError(f"run error: {event.error}")
                elif isinstance(event, ResultEvent):
                    results.append(event)
                progress()
            expected = {"rag_search": case.turns - 2, "sql_query": 1, "save_note": 1}
            if turns != case.turns or calls != expected:
                raise AssertionError(f"wrong workload: turns={turns}, tools={dict(calls)}")
            if (
                len(results) != 1
                or results[0].subtype != "success"
                or results[0].final_text != "scale:ok"
                or session.id not in overlaps
            ):
                raise AssertionError("invalid final result or first reads did not overlap")
            latencies.append((time.perf_counter() - began) * 1000)
            result["completed"] += 1
        except Exception as exc:
            result["failed"] += 1
            if len(result["errors"]) < 20:
                result["errors"].append(f"{type(exc).__name__}: {exc}")
        progress()

    try:
        agent = Agent(
            model="gpt-5",
            provider=ScaleProvider(turns=case.turns),
            tools=tools,
            session_store=MeasuredStore(session_store, "session", metrics),
            run_store=MeasuredStore(run_store, "run", metrics),
            permissions={"mode": "skip-dangerous"},
            max_tool_concurrency=2,
            max_turns=case.turns + 1,
            result_offload=None,
        )
        sampler = LagSampler()
        await sampler.start()
        progress(force=True)
        started = time.perf_counter()
        await asyncio.gather(*(run_one() for _ in range(case.sessions)))
        elapsed = time.perf_counter() - started
        lag = await sampler.stop()
        sampled_peak = sampler.peak_rss
        sampler = None
        final_hwm = _rss_high_water()
        result.update(
            correct=result["completed"] == case.sessions and result["failed"] == 0,
            elapsed_s=elapsed,
            runs_per_second=result["completed"] / elapsed,
            run_latency_ms={
                "p50": percentile(latencies, 50) if latencies else None,
                "p99": percentile(latencies, 99) if latencies else None,
                "samples": len(latencies),
                "scope": "session creation plus run; successful runs only",
            },
            parallel_first_read_pairs=len(overlaps),
            event_loop_lag_ms=lag,
            rss=scale_rss_summary(
                baseline=baseline_rss,
                initial_hwm=initial_hwm,
                sampled_peak=sampled_peak,
                final_hwm=final_hwm,
                sessions=case.sessions,
            ),
        )
        result["store_writes"], result["checkpoint_bytes"] = metrics.summaries(
            turns=result["observed_provider_turns"]
        )
        result["store_bytes"] = {
            "checkpoint": result["checkpoint_bytes"],
            "events": result["checkpoint_bytes"]["event_bytes"],
            "session_append": result["checkpoint_bytes"]["session_append_bytes"],
        }
        result["store_round_trips"] = metrics.round_trip_summary(
            turns=result["observed_provider_turns"]
        )
        progress(force=True)
        return result
    finally:
        if sampler is not None:
            await sampler.stop()
        if agent is not None:
            await agent.close()
        if prefix and raw_dsn:
            try:
                await _sweep_postgres_schemas(raw_dsn, prefix)
            except Exception:
                if case.backend == "postgres":
                    raise


async def run_scale_subprocess(case: ScaleCase, *, timeout_s: float = 600) -> dict[str, Any]:
    raw_dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN")
    prefix = None
    if case.backend == "postgres" and raw_dsn:
        await _sweep_postgres_schemas(raw_dsn)
        prefix = _new_scale_schema_prefix()
    with tempfile.TemporaryDirectory(prefix="linch-scale-") as raw:
        progress = Path(raw) / "progress.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--scale-child",
            json.dumps(asdict(case)),
            "--scale-progress",
            str(progress),
        ]
        if prefix:
            command.extend(["--scale-prefix", prefix])
        started = time.perf_counter()
        timed_out = False
        stdout_path, stderr_path = Path(raw) / "stdout", Path(raw) / "stderr"
        with stdout_path.open("wb") as output, stderr_path.open("wb") as errors:
            process = subprocess.Popen(command, stdout=output, stderr=errors)
            try:
                deadline = started + timeout_s
                while process.poll() is None:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        timed_out = True
                        break
                    await asyncio.sleep(min(0.01, remaining))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        try:
            stdout, stderr = stdout_path.read_bytes(), stderr_path.read_bytes()
            if not timed_out and process.returncode == 0:
                result = json.loads(stdout)
            else:
                result = (
                    json.loads(progress.read_text())
                    if progress.exists()
                    else {
                        **asdict(case),
                        "completed": 0,
                        "failed": 0,
                        "errors": [],
                        "observed_provider_turns": 0,
                        "observed_tool_calls": 0,
                    }
                )
                message = (
                    f"timeout after {timeout_s}s"
                    if timed_out
                    else (
                        f"child exited {process.returncode}: "
                        f"{stderr.decode(errors='replace')[-2000:]}"
                    )
                )
                result.update(correct=False, timed_out=timed_out)
                result["errors"].append(message)
            result.update(
                child_command=command,
                child_wall_s=time.perf_counter() - started,
                timeout_s=timeout_s,
                pid=process.pid,
            )
            return result
        finally:
            if raw_dsn and prefix:
                await asyncio.shield(_sweep_postgres_schemas(raw_dsn, prefix))


def _scale_machine() -> dict[str, Any]:
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "machine": platform.machine(),
    }


async def _scale_main(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.scale_child:
        case = ScaleCase(**json.loads(args.scale_child))
        progress = Path(args.scale_progress)
        result = await run_scale_case(
            case,
            progress.parent / "stores",
            progress_path=progress,
            postgres_prefix=args.scale_prefix,
        )
        print(json.dumps(result, allow_nan=False))
        return 0
    if args.scale_timeout <= 0 or not math.isfinite(args.scale_timeout):
        parser.error("--scale-timeout must be finite and positive")
    if args.cases or args.check or args.threshold:
        parser.error("--scale uses --scale-case and does not use legacy thresholds")
    cases = build_scale_cases()
    results = []
    for name in args.scale_cases or list(cases):
        case = cases[name]
        reason = scale_case_skip_reason(case)
        if reason is not None:
            results.append({**asdict(case), "skipped": True, "skip_reason": reason})
        else:
            results.append(await run_scale_subprocess(case, timeout_s=args.scale_timeout))
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 3,
                    "suite": "scale",
                    "machine": _scale_machine(),
                    "command": shlex.join([sys.executable, *sys.argv]),
                    "metrics": {result["name"]: result for result in results},
                },
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        )
    else:
        for result in results:
            if result.get("skipped"):
                print(f"{result['name']}: skipped ({result['skip_reason']})")
            else:
                print(
                    f"{result['name']}: {result['completed']}/{result['sessions']} completed, "
                    f"correct={result['correct']}, timeout={result['timed_out']}"
                )
                print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if all(result.get("skipped", False) or result["correct"] for result in results) else 1


def _threshold(value: str) -> tuple[str, float]:
    try:
        name, raw_limit = value.rsplit("=", 1)
        limit = float(raw_limit)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("threshold must have the form CASE=MILLISECONDS") from exc
    if not name or limit < 0:
        raise argparse.ArgumentTypeError("threshold needs a case name and non-negative limit")
    return name, limit


def build_parser(case_names: Sequence[str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Linch's offline runtime benchmarks.")
    parser.add_argument(
        "--warmups", type=int, default=2, help="warmup samples per case (default: 2)"
    )
    parser.add_argument(
        "--samples", type=int, default=10, help="measured samples per case (default: 10)"
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=case_names,
        dest="cases",
        help="run only this case; repeat to select multiple cases",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit nonzero if a case exceeds its fixed-runner p95 threshold",
    )
    parser.add_argument(
        "--threshold",
        action="append",
        default=[],
        type=_threshold,
        metavar="CASE=MS",
        help="override or add a p95 threshold (used with --check)",
    )
    parser.add_argument("--scale", action="store_true", help="run six isolated scale cases only")
    parser.add_argument(
        "--scale-case",
        action="append",
        choices=tuple(build_scale_cases()),
        dest="scale_cases",
        help="select a scale case (requires --scale)",
    )
    parser.add_argument(
        "--scale-timeout",
        type=float,
        default=600,
        help="hard timeout in seconds per scale child (default: 600)",
    )
    parser.add_argument("--scale-child", help=argparse.SUPPRESS)
    parser.add_argument("--scale-progress", help=argparse.SUPPRESS)
    parser.add_argument("--scale-prefix", help=argparse.SUPPRESS)
    return parser


async def async_main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--scale" in argv or "--scale-child" in argv:
        parser = build_parser(())
        return await _scale_main(parser.parse_args(argv), parser)
    with tempfile.TemporaryDirectory(prefix="linch-runtime-bench-") as raw_workdir:
        all_cases = build_cases(Path(raw_workdir))
        parser = build_parser(tuple(all_cases))
        args = parser.parse_args(argv)
        if args.scale_cases:
            parser.error("--scale-case requires --scale")
        if args.warmups < 0:
            parser.error("--warmups must be non-negative")
        if args.samples < 1:
            parser.error("--samples must be at least 1")

        selected_names = args.cases or list(all_cases)
        unknown_thresholds = [name for name, _ in args.threshold if name not in all_cases]
        if unknown_thresholds:
            parser.error(f"unknown threshold case: {unknown_thresholds[0]}")
        if args.threshold and not args.check:
            parser.error("--threshold requires --check")
        thresholds = dict(DEFAULT_THRESHOLDS_MS) if args.check else {}
        thresholds.update(dict(args.threshold))
        results = await run_suite(
            [all_cases[name] for name in selected_names],
            warmups=args.warmups,
            samples=args.samples,
            thresholds=thresholds,
        )
        print(
            format_json(results, warmups=args.warmups, samples=args.samples)
            if args.json
            else format_human(results)
        )
        return 0 if all(result.passed for result in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
