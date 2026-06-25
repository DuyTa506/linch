"""ToolCacheHook — opt-in per-run memoization of read-scope tool calls.

Proves the cache (1) actually saves work on duplicate calls, and (2) stays
correct: distinct inputs aren't conflated, writes invalidate prior reads,
write/exec tools and errors are never cached, and it is off unless configured.
"""

from __future__ import annotations

from linch import Agent, ToolCacheConfig
from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
from linch.sessions import InMemorySessionStore
from linch.tools import ToolRegistry, tool


def _read_tool(name: str = "Search"):
    calls = {"n": 0}

    @tool(name=name, scope="read")
    def fn(query: str) -> str:
        calls["n"] += 1
        return f"r{calls['n']}:{query}"

    return fn, calls


def _write_tool(name: str = "Mutate"):
    calls = {"n": 0}

    @tool(name=name, scope="write")
    def fn(path: str) -> str:
        calls["n"] += 1
        return f"wrote {path}"

    return fn, calls


def _agent(provider, tools, *, tool_cache=None, permissions=None) -> Agent:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return Agent(
        model="m",
        provider=provider,
        session_store=InMemorySessionStore(),
        tools=reg,
        tool_cache=tool_cache,
        permissions=permissions,
    )


def _twice(tool_name: str, tool_input: dict) -> ScriptedProvider:
    return ScriptedProvider(
        [
            ToolUseTurn(tool_name=tool_name, tool_input=tool_input, tool_id="t1"),
            ToolUseTurn(tool_name=tool_name, tool_input=tool_input, tool_id="t2"),
            TextTurn(text="done"),
        ]
    )


async def _run(agent: Agent, prompt: str = "go") -> None:
    session = await agent.session()
    async for _ in session.run(prompt):
        pass


# ── efficiency ────────────────────────────────────────────────────────────────


async def test_cache_serves_duplicate_read_without_re_executing() -> None:
    search, calls = _read_tool()
    agent = _agent(_twice("Search", {"query": "x"}), [search], tool_cache=ToolCacheConfig())
    await _run(agent)
    assert calls["n"] == 1  # second identical call served from cache


async def test_disabled_by_default_executes_every_call() -> None:
    search, calls = _read_tool()
    agent = _agent(_twice("Search", {"query": "x"}), [search])  # no tool_cache
    await _run(agent)
    assert calls["n"] == 2


# ── correctness ───────────────────────────────────────────────────────────────


async def test_distinct_inputs_are_not_conflated() -> None:
    search, calls = _read_tool()
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="Search", tool_input={"query": "a"}, tool_id="t1"),
            ToolUseTurn(tool_name="Search", tool_input={"query": "b"}, tool_id="t2"),
            TextTurn(text="done"),
        ]
    )
    agent = _agent(provider, [search], tool_cache=ToolCacheConfig())
    await _run(agent)
    assert calls["n"] == 2  # different inputs → no false hit


async def test_write_invalidates_cached_read() -> None:
    search, scalls = _read_tool()
    mutate, _ = _write_tool()
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="Search", tool_input={"query": "x"}, tool_id="t1"),
            ToolUseTurn(tool_name="Mutate", tool_input={"path": "/f"}, tool_id="t2"),
            ToolUseTurn(tool_name="Search", tool_input={"query": "x"}, tool_id="t3"),
            TextTurn(text="done"),
        ]
    )
    agent = _agent(
        provider,
        [search, mutate],
        tool_cache=ToolCacheConfig(),
        permissions={"mode": "skip-dangerous"},
    )
    await _run(agent)
    assert scalls["n"] == 2  # write between the two reads invalidates the cache


async def test_write_scope_tool_is_never_cached() -> None:
    mutate, calls = _write_tool()
    agent = _agent(
        _twice("Mutate", {"path": "/f"}),
        [mutate],
        # Even explicitly allow-listed, a write-scope tool must not be cached.
        tool_cache=ToolCacheConfig(allow={"Mutate"}),
        permissions={"mode": "skip-dangerous"},
    )
    await _run(agent)
    assert calls["n"] == 2


async def test_error_results_are_not_cached() -> None:
    calls = {"n": 0}

    @tool(name="Search", scope="read")
    def search(query: str) -> str:
        calls["n"] += 1
        raise RuntimeError("boom")

    agent = _agent(_twice("Search", {"query": "x"}), [search], tool_cache=ToolCacheConfig())
    await _run(agent)
    assert calls["n"] == 2  # failed call not cached → retried


async def test_allowlist_excludes_unlisted_read_tool() -> None:
    search, calls = _read_tool()
    agent = _agent(
        _twice("Search", {"query": "x"}),
        [search],
        tool_cache=ToolCacheConfig(allow={"Other"}),
    )
    await _run(agent)
    assert calls["n"] == 2  # Search not in allow set → not cached


# ── plumbing: the resolve action ──────────────────────────────────────────────


async def test_resolve_action_short_circuits_dispatch() -> None:
    from linch.hooks import HookDispatcher, HookResult
    from linch.hooks.contexts import PreToolUseContext
    from linch.tools import ToolResult

    served = ToolResult(content="cached")
    later_ran = {"v": False}

    class Server:
        def on_pre_tool_use(self, ctx):
            return HookResult.resolve(tool_result=served)

    class Later:
        def on_pre_tool_use(self, ctx):
            later_ran["v"] = True
            return None

    dispatcher = HookDispatcher([Server(), Later()])
    ctx = PreToolUseContext(
        session=None, run_id="r", turn_index=0, tool_use_id="t", tool_name="X", input={}
    )
    res = await dispatcher.dispatch("PreToolUse", ctx)

    assert res.result.action == "resolve"
    assert res.result.tool_result is served
    assert later_ran["v"] is False  # short-circuited before the next hook
