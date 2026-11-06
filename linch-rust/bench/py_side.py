"""Bench side "py": Python Linch (master), run as `python -I py_side.py JOB.json`.

Prints one JSON result line (see `common.py`). `linch` must be importable
from the interpreter (the bench venv); `--pg-admin` subcommands are used by
`run_all.py` to create, count and drop the per-run Postgres schemas.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import Clock, Job, Result, load_turns, main, ok, peak_rss_mb  # noqa: E402

READS = ("load", "list", "get", "ready", "close", "connect")


class FixtureProvider:
    """Replays scripted turns in call order, like Rust's `ScriptedProvider`."""

    id = "scripted"

    def __init__(self, turns: list[dict], context_window: int = 128_000) -> None:
        self._turns = turns
        self._index = 0
        self._window = context_window

    def context_window(self, model: str) -> int:
        return self._window

    async def stream(self, req):
        from linch import ProviderError, Usage

        if self._index >= len(self._turns):
            raise ProviderError(f"scripted provider is out of turns after {self._index}")
        turn = self._turns[self._index]
        self._index += 1
        for event in turn.get("events", []):
            if event["type"] == "message_end":
                event = {**event, "usage": Usage(**event["usage"]), "provider_metadata": None}
            yield event


def echo_tool(spec: dict):
    from linch import ToolResult

    class Echo:
        name = spec["name"]
        description = spec["description"]
        input_schema = spec["input_schema"]
        scope = "read"
        parallel = True

        def validate(self, raw):
            return raw

        def summarize(self, input):
            return self.name

        async def execute(self, input, ctx) -> ToolResult:
            return ToolResult(content=input["value"])

    return Echo()


def count_writes(store, counter: dict) -> list:
    """Wrap the store's write coroutines to count calls; returns the undo list."""
    undo = []
    for name in dir(store):
        if name.startswith("_") or name.startswith(READS):
            continue
        method = getattr(store, name)
        if not asyncio.iscoroutinefunction(method):
            continue

        def wrap(method=method, name=name):
            async def counted(*args, **kwargs):
                counter[name] = counter.get(name, 0) + 1
                return await method(*args, **kwargs)

            return counted

        setattr(store, name, wrap())
        undo.append(name)
    return undo


def stores(job: Job):
    if job.get("stores") == "postgres":
        from linch import PostgresRunStore
        from linch.sessions.postgres import PostgresSessionStore

        return PostgresRunStore(job["pg_dsn"]), PostgresSessionStore(job["pg_dsn"])
    from linch import InMemoryRunStore, InMemorySessionStore

    return InMemoryRunStore(), InMemorySessionStore()


def build_agent(job: Job, provider, tools=None, run_store=None, session_store=None, **extra):
    from linch import Agent, SystemPromptConfig, ToolRegistry

    if tools is None:
        tools = ToolRegistry()
        tools.register(echo_tool(job["tool"]))
    if run_store is None:
        run_store, session_store = stores(job)
    return Agent(
        model=job["model"],
        provider=provider,
        tools=tools,
        session_store=session_store,
        run_store=run_store,
        system_prompt_config=SystemPromptConfig(replace_defaults=True, append=job["system"]),
        permissions={"mode": "skip-dangerous"},
        result_offload=None,
        max_retries=0,
        max_tool_concurrency=job.get("max_tool_concurrency"),
        **extra,
    )


async def run_once(agent, prompt: str = "go") -> list:
    from linch import ResultEvent

    session = await agent.session()
    events = [event async for event in session.run(prompt)]
    result = next(e for e in events if isinstance(e, ResultEvent))
    if result.subtype != "success":
        raise RuntimeError(f"run ended {result.subtype}: {getattr(result, 'error', None)}")
    return events


async def close_all(agent, *stores_) -> None:
    await agent.close()
    for store in stores_:
        close = getattr(store, "close", None)
        if close is not None:
            await close()


async def case_loop(job: Job) -> Result:
    turns = load_turns(job["fixture"])
    iters, warmup = job["iterations"], job["warmup"]
    run_store, session_store = stores(job)
    provider = FixtureProvider(turns * (warmup + iters + 1))
    agent = build_agent(job, provider, run_store=run_store, session_store=session_store)
    clock = Clock()
    run_writes: dict = {}
    session_writes: dict = {}
    try:
        for _ in range(warmup):
            await run_once(agent)
        for _ in range(iters):
            clock.start()
            await run_once(agent)
            clock.stop()
        # One more run, not timed, with the store writes counted.
        undo = count_writes(run_store, run_writes), count_writes(session_store, session_writes)
        await run_once(agent)
        for store, names in zip((run_store, session_store), undo, strict=True):
            for name in names:
                delattr(store, name)
    finally:
        await close_all(agent, run_store, session_store)
    return ok(
        clock,
        len(turns),
        "turn",
        peak_rss_mb(),
        writes_per_run={"run": run_writes, "session": session_writes},
        runs_total=warmup + iters + 1,
    )


CASES = {"loop": case_loop, "pg_loop": case_loop}


async def pg_admin(action: str, dsn: str, schema: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        if action == "create":
            await conn.execute(f'CREATE SCHEMA "{schema}"')
        elif action == "drop":
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        elif action == "stats":
            row = await conn.fetchrow(
                "SELECT coalesce(sum(n_tup_ins), 0) AS ins, coalesce(sum(n_tup_upd), 0) AS upd, "
                "coalesce(sum(n_tup_del), 0) AS del FROM pg_stat_user_tables WHERE schemaname = $1",
                schema,
            )
            print(json.dumps({key: int(row[key]) for key in ("ins", "upd", "del")}))
    finally:
        await conn.close()


if __name__ == "__main__":
    if sys.argv[1] == "--pg-admin":
        asyncio.run(pg_admin(sys.argv[2], sys.argv[3], sys.argv[4]))
    else:
        main(CASES, asyncio.run)
