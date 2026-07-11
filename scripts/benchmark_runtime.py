from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from linch import Agent, InMemoryRunStore  # noqa: E402
from linch.events import (  # noqa: E402
    PartialAssistantEvent,
    ResultEvent,
    SystemEvent,
    ToolCallEndEvent,
)
from linch.memory import InMemoryKeywordMemoryStore, MemoryItem  # noqa: E402
from linch.run_store import SqliteRunStore  # noqa: E402
from linch.sessions import InMemorySessionStore  # noqa: E402
from linch.tools import ToolContext, ToolRegistry, ToolResult  # noqa: E402
from linch.types import Usage  # noqa: E402

# These are fixed-runner gates from docs/ROADMAP.md. Cases without a stable
# absolute target still participate in --check through their correctness checks.
DEFAULT_THRESHOLDS_MS: dict[str, float] = {
    "sqlite_trivial_operation": 10.0,
    "durable_sqlite_8_read_tools": 25.0,
    "durable_sqlite_32_read_tools": 70.0,
    "run_store_50_event_writes": 250.0,
}


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
    sqlite_counter = 0

    def sqlite_store() -> SqliteRunStore:
        nonlocal sqlite_counter
        sqlite_counter += 1
        return SqliteRunStore(workdir / f"durable-{sqlite_counter}.db")

    sqlite_operation_store = SqliteRunStore(workdir / "trivial.db")
    sqlite_operation_run_id: str | None = None
    event_write_store = SqliteRunStore(workdir / "event-writes.db")

    async def sqlite_operation() -> Sample:
        nonlocal sqlite_operation_run_id
        if sqlite_operation_run_id is None:
            run = await sqlite_operation_store.create_run("benchmark-session")
            sqlite_operation_run_id = run.id
        started = time.perf_counter()
        loaded = await sqlite_operation_store.load_run(sqlite_operation_run_id)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if loaded is None or loaded.id != sqlite_operation_run_id:
            raise AssertionError("SQLite trivial operation returned the wrong run")
        return Sample(elapsed_ms=elapsed_ms)

    memory_store_cell: list[InMemoryKeywordMemoryStore | None] = [None]

    async def memory_100k_search() -> Sample:
        # 100k-entry keyword-memory heartbeat (WS3 online bounded heap): observed,
        # not gated. The heap bounds retained results to `limit` (memory) and keeps
        # tie-order identical to nlargest; it does not make a 100k pure-Python scan
        # sub-20ms (an inverted index was out of scope). Tokens are cached at
        # construction, so this measures the scan + heap, not tokenizing.
        store = memory_store_cell[0]
        if store is None:
            words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
            items = [
                MemoryItem(
                    id=f"m{i}",
                    content=f"{words[i % 8]} {words[(i + 1) % 8]} {words[(i + 3) % 8]}",
                )
                for i in range(100_000)
            ]
            store = InMemoryKeywordMemoryStore(items)
            memory_store_cell[0] = store
        started = time.perf_counter()
        results = await store.search("alpha beta", limit=10)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if len(results) != 10:
            raise AssertionError(f"expected 10 memory hits, got {len(results)}")
        scores = [result.score or 0.0 for result in results]
        if scores != sorted(scores, reverse=True):
            raise AssertionError("memory search results were not ranked by descending score")
        return Sample(elapsed_ms=elapsed_ms, event_count=len(results))

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
        run = await event_write_store.create_run("benchmark-session")
        started = time.perf_counter()
        sequences = [
            await event_write_store.append_event(
                run.id,
                SystemEvent(
                    session_id="benchmark-session",
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
            raise AssertionError(f"SQLite event sequences were not contiguous: {sequences!r}")
        persisted = await event_write_store.load_events(run.id)
        persisted_models = [
            row.event.model if isinstance(row.event, SystemEvent) else None for row in persisted
        ]
        if [row.seq for row in persisted] != sequences:
            raise AssertionError("SQLite persisted event sequences in the wrong order")
        if persisted_models != [f"write-{index}" for index in range(50)]:
            raise AssertionError("SQLite persisted event payloads in the wrong order")
        return Sample(elapsed_ms=elapsed_ms, event_count=len(persisted))

    return {
        case.name: case
        for case in (
            _no_tool_case(),
            _tool_case(name="parallel_8_read_tools", count=8),
            _tool_case(
                name="durable_memory_8_read_tools",
                count=8,
                run_store_factory=InMemoryRunStore,
            ),
            _tool_case(name="durable_sqlite_8_read_tools", count=8, run_store_factory=sqlite_store),
            _tool_case(
                name="durable_sqlite_32_read_tools", count=32, run_store_factory=sqlite_store
            ),
            _tool_case(
                name="scheduler_maximal_16_read_tools",
                count=16,
                tool_batching_strategy="maximal",
            ),
            BenchmarkCase("memory_100k_search", memory_100k_search),
            BenchmarkCase("session_churn_25", session_churn),
            BenchmarkCase(
                "sqlite_trivial_operation", sqlite_operation, close=sqlite_operation_store.close
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
    return parser


async def async_main(argv: Sequence[str] | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="linch-runtime-bench-") as raw_workdir:
        all_cases = build_cases(Path(raw_workdir))
        parser = build_parser(tuple(all_cases))
        args = parser.parse_args(argv)
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
