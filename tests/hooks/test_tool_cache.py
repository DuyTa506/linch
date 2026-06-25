"""ToolCacheHook — opt-in per-run memoization of read-scope tool calls.

Proves the cache (1) actually saves work on duplicate calls, and (2) stays
correct: distinct inputs aren't conflated, writes invalidate prior reads,
write/exec tools and errors are never cached, and it is off unless configured.
"""

from __future__ import annotations

import pytest

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


# ── unit: phase ordering / leaks (drive the hook directly) ────────────────────


class _FakeTool:
    def __init__(self, scope: str) -> None:
        self.scope = scope


def _pre(tid: str, name: str, scope: str, inp: dict):
    from linch.hooks.contexts import PreToolUseContext

    return PreToolUseContext(
        session=None,
        run_id="r",
        turn_index=0,
        tool_use_id=tid,
        tool_name=name,
        input=inp,
        tool=_FakeTool(scope),
    )


def _post(tid: str, name: str, inp: dict, result):
    from linch.hooks.contexts import PostToolUseContext

    return PostToolUseContext(
        session=None,
        run_id="r",
        turn_index=0,
        tool_use_id=tid,
        tool_name=name,
        input=inp,
        result=result,
    )


async def test_same_turn_read_then_write_invalidates_the_cached_read() -> None:
    # Regression for the phase-ordering bug: the scheduler runs the whole
    # PreToolUse pass over a turn BEFORE any tool executes, then all PostToolUse.
    # A [Search, Write] turn must NOT leave a stale Search cached.
    from linch.hooks.tool_cache import ToolCacheConfig, ToolCacheHook
    from linch.tools import ToolResult

    hook = ToolCacheHook(ToolCacheConfig())

    # Pre pass over both blocks, then post pass over both (scheduler order).
    assert await hook.on_pre_tool_use(_pre("s1", "Search", "read", {"q": "x"})) is None
    assert await hook.on_pre_tool_use(_pre("w1", "Write", "write", {"f": "f"})) is None
    await hook.on_post_tool_use(_post("s1", "Search", {"q": "x"}, ToolResult(content="OLD")))
    await hook.on_post_tool_use(_post("w1", "Write", {"f": "f"}, ToolResult(content="wrote")))

    # Next turn: an identical Search must MISS (not be served the pre-write value).
    res = await hook.on_pre_tool_use(_pre("s2", "Search", "read", {"q": "x"}))
    assert res is None  # cache invalidated by the write's PostToolUse


async def test_agent_stop_evicts_bucket_on_any_termination() -> None:
    # on_stop fires only on success terminals; on_agent_stop fires on every
    # terminal (incl. error/abort/budget), so eviction must hang off it.
    from linch.hooks.contexts import AgentStopContext
    from linch.hooks.tool_cache import ToolCacheConfig, ToolCacheHook
    from linch.tools import ToolResult

    hook = ToolCacheHook(ToolCacheConfig())
    await hook.on_pre_tool_use(_pre("s1", "Search", "read", {"q": "x"}))
    await hook.on_post_tool_use(_post("s1", "Search", {"q": "x"}, ToolResult(content="v")))
    assert "r" in hook._runs

    await hook.on_agent_stop(AgentStopContext(session=None, run_id="r", turn_index=0))
    assert "r" not in hook._runs


def test_invalid_config_is_rejected() -> None:
    from linch.hooks.tool_cache import ToolCacheHook

    with pytest.raises(ValueError):
        ToolCacheHook(ToolCacheConfig(max_entries=0))
    with pytest.raises(ValueError):
        ToolCacheHook(ToolCacheConfig(max_value_bytes=-1))


async def test_oversized_results_are_not_cached() -> None:
    # Large results are left to the offload subsystem; caching them would pin
    # full payloads and re-offload on each hit. Above max_value_bytes → re-runs.
    search, calls = _read_tool()
    agent = _agent(
        _twice("Search", {"query": "x"}),
        [search],
        tool_cache=ToolCacheConfig(max_value_bytes=2),  # "r1:x" exceeds 2 bytes
    )
    await _run(agent)
    assert calls["n"] == 2  # not cached → executed both times


async def test_unmatched_pending_misses_are_bounded() -> None:
    # A backgrounded read records a pending entry but never fires PostToolUse;
    # pending must be LRU-bounded so it can't grow without limit over a long run.
    from linch.hooks.tool_cache import ToolCacheConfig, ToolCacheHook

    hook = ToolCacheHook(ToolCacheConfig(max_entries=4))
    for i in range(20):
        await hook.on_pre_tool_use(_pre(f"bg{i}", "Search", "read", {"q": i}))  # no post
    assert len(hook._runs["r"].pending) <= 4


async def test_resolve_without_tool_result_blocks_instead_of_executing() -> None:
    from linch.hooks import HookResult

    calls = {"n": 0}

    @tool(name="Danger", scope="write")
    def danger(x: str) -> str:
        calls["n"] += 1
        return "ran"

    class BadResolve:
        def on_pre_tool_use(self, ctx):
            return HookResult(action="resolve")  # tool_result left None

    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="Danger", tool_input={"x": "y"}, tool_id="t1"),
            TextTurn(text="done"),
        ]
    )
    agent = _agent(provider, [danger], permissions={"mode": "skip-dangerous"})
    agent.hooks = [BadResolve()]
    await _run(agent)
    assert calls["n"] == 0  # malformed resolve blocks; the tool never runs


async def test_resolve_reports_the_mutated_input_in_telemetry() -> None:
    # When an earlier PreToolUse hook rewrites the input and the cache serves a
    # hit keyed on that rewritten input, the ToolCallStartEvent must show the
    # mutated input (what the served result corresponds to), not the original.
    from linch.events import ToolCallStartEvent
    from linch.hooks import HookResult

    search, calls = _read_tool()

    class Rewrite:
        def on_pre_tool_use(self, ctx):
            if ctx.tool_name == "Search":
                return HookResult.mutate(input={"query": "X"})
            return None

    agent = _agent(_twice("Search", {"query": "x"}), [search], tool_cache=ToolCacheConfig())
    agent.hooks = [Rewrite(), *agent.hooks]  # mutate first, cache appended last

    inputs = []
    session = await agent.session()
    async for ev in session.run("go"):
        if isinstance(ev, ToolCallStartEvent) and ev.tool_name == "Search":
            inputs.append(ev.input)

    assert calls["n"] == 1  # 2nd call served from cache (keyed on mutated input)
    assert inputs == [{"query": "X"}, {"query": "X"}]  # both report the mutated input


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
