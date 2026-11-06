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
    """Replays scripted turns in call order, like Rust's `ScriptedProvider`.


    id = "scripted"

    With `summary_prompt`, a request whose system text is that prompt (a
    compaction summary call) gets the fixture's `SUMMARY:` turn wherever it
    comes, and other requests skip that turn: Python may compact at another
    call than Rust, whose fixture has the summary turn at Rust's call.
    """

    id = "scripted"

    def __init__(
        self, turns: list[dict], context_window: int = 128_000, summary_prompt: str | None = None
    ) -> None:
        self._summary = None
        self._prompt = summary_prompt
        if summary_prompt is not None:
            self._summary = next(t for t in turns if _text(t).startswith("SUMMARY:"))
            turns = [t for t in turns if not _text(t).startswith("SUMMARY:")]
        self._turns = turns
        self._index = 0
        self._window = context_window

    def context_window(self, model: str) -> int:
        return self._window

    async def stream(self, req):
        from linch import ProviderError, Usage

        system = [getattr(block, "text", "") for block in (req.system or [])]
        if self._prompt is not None and self._prompt in system:
            turn = self._summary
        elif self._index >= len(self._turns):
            raise ProviderError(f"scripted provider is out of turns after {self._index}")
        else:
            turn = self._turns[self._index]
            self._index += 1
        for event in turn.get("events", []):
            if event["type"] == "message_end":
                event = {**event, "usage": Usage(**event["usage"]), "provider_metadata": None}
            yield event


def _text(turn: dict) -> str:
    return "".join(e.get("text", "") for e in turn["events"] if e["type"] == "text_delta")


def echo_tool(spec: dict, reply: str | None = None):
    """Echo returns its `value`; with `reply` (the live `get_weather`) it
    answers that template with `{city}` filled in."""
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
            if reply is not None:
                return ToolResult(content=reply.replace("{city}", input.get("city", "there")))
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
    failed = [e for e in events if type(e).__name__ == "ToolCallEndEvent" and e.is_error]
    if failed:
        raise RuntimeError(f"{failed[0].tool_name} failed: {str(failed[0].result)[:300]}")
    result = next(e for e in events if isinstance(e, ResultEvent))
    if result.subtype != "success":
        errors = [repr(e)[:300] for e in events if type(e).__name__ == "ErrorEvent"]
        raise RuntimeError(f"run ended {result.subtype} ({result.stop_reason}): {errors}")
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


def reset_workspace(job: Job) -> None:
    """Put the workspace's `src/` back as `run_all.py` built it (untimed)."""
    import shutil

    src = Path(job["workspace"]) / "src"
    shutil.rmtree(src)
    shutil.copytree(job["pristine"], src)


async def timed_runs(job: Job, agent, before=None) -> Clock:
    clock = Clock()
    for _ in range(job["warmup"]):
        if before is not None:
            before()
        await run_once(agent)
    for _ in range(job["iterations"]):
        if before is not None:
            before()
        clock.start()
        clock.last = await run_once(agent)
        clock.stop()
    return clock


async def case_tools(job: Job) -> Result:
    from linch import workspace_tools

    turns = load_turns(job["fixture"])
    runs = job["warmup"] + job["iterations"]
    extras = {"BashProcess", "TaskCreate", "TaskList", "TaskGet", "TaskUpdate"}
    agent = build_agent(
        job,
        FixtureProvider(turns * runs),
        tools=workspace_tools(exclude=extras),
        cwd=job["workspace"],
    )
    try:
        clock = await timed_runs(job, agent, lambda: reset_workspace(job))
    finally:
        await agent.close()
    return ok(clock, job["calls"], "call", peak_rss_mb())


async def case_mcp(job: Job) -> Result:
    from linch import FeatureFlags, McpStdioServerConfig, ToolRegistry

    turns = load_turns(job["fixture"])
    runs = job["warmup"] + job["iterations"]
    server = McpStdioServerConfig(type="stdio", command=job["mcp_server_bin"], args=[])
    agent = build_agent(
        job,
        FixtureProvider(turns * runs),
        tools=ToolRegistry(),
        mcp_servers={"bench": server},
        features=FeatureFlags(mcp=True),
    )
    try:
        await agent.connect_mcp()
        clock = await timed_runs(job, agent)
    finally:
        await agent.close()
    return ok(clock, job["calls"], "call", peak_rss_mb())


async def case_compaction(job: Job) -> Result:
    from linch import CompactionEvent, DefaultCompaction

    turns = load_turns(job["fixture"])
    runs = job["warmup"] + job["iterations"]
    provider = FixtureProvider(
        turns * runs,
        context_window=job["py_context_window"],
        summary_prompt=job["summary_prompt"],
    )
    agent = build_agent(job, provider, compaction=DefaultCompaction(prompt=job["summary_prompt"]))
    clock = Clock()
    compactions = []
    try:
        for i in range(runs):
            if i >= job["warmup"]:
                clock.start()
            events = await run_once(agent)
            if i >= job["warmup"]:
                clock.stop()
            compactions.append(sum(isinstance(e, CompactionEvent) for e in events))
    finally:
        await agent.close()
    if min(compactions) < 1:
        raise RuntimeError(f"a run did not compact: {compactions}")
    turns_per_run = len(turns) - 1  # the summary turn is not a loop turn
    note = f"{compactions[-1]} compaction(s)/run"
    return ok(clock, turns_per_run, "turn", peak_rss_mb(), note=note)


async def case_concurrency(job: Job) -> Result:
    turns = load_turns(job["fixture"])
    runs = job["warmup"] + job["iterations"]
    agents = [build_agent(job, FixtureProvider(turns * runs)) for _ in range(job["sessions"])]
    clock = Clock()
    try:
        for i in range(runs):
            if i >= job["warmup"]:
                clock.start()
            await asyncio.gather(*(run_once(agent) for agent in agents))
            if i >= job["warmup"]:
                clock.stop()
    finally:
        for agent in agents:
            await agent.close()
    return ok(clock, job["sessions"], "run", peak_rss_mb())


async def case_startup(job: Job) -> Result:
    """One cold process: import, build, first run (`run_all.py` times the process)."""
    import time

    started = time.perf_counter()
    import linch  # noqa: F401

    imported = time.perf_counter()
    agent = build_agent(job, FixtureProvider(load_turns(job["fixture"])))
    built = time.perf_counter()
    try:
        await run_once(agent)
    finally:
        await agent.close()
    done = time.perf_counter()
    return {
        "status": "ok",
        "phases_ms": {
            "import": 1000 * (imported - started),
            "build": 1000 * (built - imported),
            "first_run": 1000 * (done - built),
        },
    }


def decode_provider(job: Job):
    """The vendor's adapter pointed at the loopback replay server."""
    import os

    import linch

    vendor, url, key = job["vendor"], job["base_url"], "bench-dummy-key"
    if vendor == "deepseek":
        options = linch.DeepSeekProviderOptions(api_key=key, base_url=url)
        return linch.DeepSeekProvider(options)
    if vendor == "openai-chat":
        options = linch.OpenAIChatProviderOptions(api_key=key, base_url=url)
        return linch.OpenAIChatCompletionsProvider(options)
    if vendor == "openai-responses":
        options = linch.OpenAIResponsesProviderOptions(api_key=key, base_url=url)
        return linch.OpenAIResponsesProvider(options)
    if vendor == "anthropic":
        options = linch.AnthropicProviderOptions(api_key=key, base_url=url)
        return linch.AnthropicProvider(options)
    # GeminiProviderOptions has no base URL; google-genai reads this one.
    os.environ["GOOGLE_GEMINI_BASE_URL"] = job["gemini_base_url"]
    return linch.GeminiProvider(linch.GeminiProviderOptions(api_key=key))


async def case_decode(job: Job) -> Result:
    from linch import ToolRegistry

    agent = build_agent(job, decode_provider(job), tools=ToolRegistry())
    try:
        clock = await timed_runs(job, agent)
    finally:
        await agent.close()
    text = next(e.final_text for e in clock.last if type(e).__name__ == "ResultEvent") or ""
    if len(text) != job["expect_chars"]:
        raise RuntimeError(f"decoded {len(text)} characters, expected {job['expect_chars']}")
    return ok(
        clock, job["stream_events"], "event", peak_rss_mb(), bytes_per_iter=job["stream_bytes"]
    )


async def case_worker(job: Job) -> Result:
    """Enqueue N one-turn runs; a Worker in this process drains them."""
    import asyncpg

    from linch import ToolRegistry
    from linch.worker import Worker, enqueue_run, install

    jobs, runs = job["jobs"], job["warmup"] + job["iterations"]
    pool = await asyncpg.create_pool(job["pg_dsn"], min_size=2, max_size=10)
    provider = FixtureProvider(load_turns(job["fixture"]) * jobs * runs)

    async def factory(work):
        return build_agent(
            job,
            provider,
            tools=ToolRegistry(),
            run_store=work.run_store,
            session_store=work.session_store,
        )

    clock = Clock()
    try:
        await install(pool)
        worker = Worker(pool, factory, worker_id="bench")
        serving = asyncio.create_task(worker.run())
        try:
            for i in range(runs):
                timed = i >= job["warmup"]
                if timed:
                    clock.start()
                ids = [await enqueue_run(pool, f"i{i}-j{j}", "go") for j in range(jobs)]
                await drain(pool, ids)
                if timed:
                    clock.stop()
        finally:
            worker.stop()
            await serving
    finally:
        await pool.close()
    return ok(clock, jobs, "job", peak_rss_mb(), runs_total=jobs * runs)


async def drain(pool, ids: list[str]) -> None:
    """Wait until every run in `ids` is terminal; fail if one did not complete."""
    import time

    live = ("pending", "running", "waiting_permission", "suspended")
    deadline = time.monotonic() + 120
    while True:
        row = await pool.fetchrow(
            "SELECT count(*) FILTER (WHERE status = ANY($2::text[])) AS left, "
            "count(*) FILTER (WHERE status <> 'completed' AND NOT status = ANY($2::text[])) "
            "AS failed FROM runs WHERE id = ANY($1::text[])",
            ids,
            list(live),
        )
        if row["failed"]:
            raise RuntimeError(f"{row['failed']} queued runs did not complete")
        if row["left"] == 0:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"{row['left']} queued runs still live after 120 s")
        await asyncio.sleep(0.002)


async def case_workflow(job: Job) -> Result:
    """A workflow of N journaled steps; the journal is the run store."""
    from linch import InMemorySessionStore, ToolRegistry
    from linch.ext.workflow import run_workflow

    run_store, _ = stores(job)
    agent = build_agent(
        job,
        FixtureProvider([]),
        tools=ToolRegistry(),
        run_store=run_store,
        session_store=InMemorySessionStore(),
    )
    steps = job["steps"]

    async def flow(wf):
        total = 0
        for i in range(steps):
            total += await wf.step("step", lambda i=i: i)
        return total

    clock = Clock()
    runs = job["warmup"] + job["iterations"]
    try:
        for i in range(runs):
            timed = i >= job["warmup"]
            if timed:
                clock.start()
            total = await run_workflow(agent, flow, run_id=f"wf-{i}")
            if timed:
                clock.stop()
            if total != sum(range(steps)):
                raise RuntimeError(f"workflow returned {total!r}")
    finally:
        await close_all(agent, run_store)
    return ok(clock, steps, "step", peak_rss_mb(), runs_total=runs)


async def case_evals(job: Job) -> Result:
    from linch.ext.evals import EvalCase, run_eval, text_contains

    with open(job["fixture"]) as handle:
        fixture = json.load(handle)
    cases = [EvalCase(prompt=c["prompt"], expected=c["expected"]) for c in fixture["cases"]]
    runs = job["warmup"] + job["iterations"]
    agent = build_agent(job, FixtureProvider(fixture["turns"] * runs))
    clock = Clock()
    try:
        for i in range(runs):
            timed = i >= job["warmup"]
            if timed:
                clock.start()
            result = await run_eval(agent, cases, [text_contains("{expected}")])
            if timed:
                clock.stop()
            if result.passed != result.total:
                raise RuntimeError(f"{result.passed} of {result.total} cases passed")
    finally:
        await agent.close()
    return ok(clock, len(cases), "case", peak_rss_mb())


async def case_live(job: Job) -> Result:
    """The W5 live prompts on DeepSeek; the key is read from the environment."""
    import os

    from linch import (
        AssistantEvent,
        DeepSeekProvider,
        DeepSeekProviderOptions,
        ToolRegistry,
    )

    options = DeepSeekProviderOptions(
        api_key=os.environ["DEEPSEEK_API_KEY"], thinking="disabled", effort=None
    )
    tools = ToolRegistry()
    tools.register(echo_tool(job["tool"], reply=job["tool_reply"]))
    agent = build_agent(
        job,
        DeepSeekProvider(options),
        tools=tools,
        max_turns=job["spec"]["max_turns"],
        max_output_tokens=job["spec"]["max_output_tokens"],
    )
    clock = Clock()
    turns = 0
    try:
        for prompt in job["prompts"]:
            clock.start()
            events = await run_once(agent, prompt)
            clock.stop()
            turns += sum(isinstance(e, AssistantEvent) for e in events)
    finally:
        await agent.close()
    per_run = turns / max(len(job["prompts"]), 1)
    return ok(clock, per_run, "turn", peak_rss_mb(), turns=turns)


CASES = {
    "decode": case_decode,
    "worker": case_worker,
    "workflow": case_workflow,
    "evals": case_evals,
    "live": case_live,
    "loop": case_loop,
    "pg_loop": case_loop,
    "tools": case_tools,
    "mcp": case_mcp,
    "compaction": case_compaction,
    "concurrency": case_concurrency,
    "startup": case_startup,
}


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
