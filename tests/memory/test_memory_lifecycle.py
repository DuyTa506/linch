"""Memory lifecycle: extraction + gated consolidation (ROADMAP Phase 3.1).

The SDK ships the *lifecycle seam* — a terminal-turn hook that runs a
caller-supplied extractor over the ``full_history`` tail, dedups against existing
entries, and upserts — plus a neutral ``ConsolidationGate`` (time + change-count
+ in-process lock). The extraction *prompt* / what counts as a memory is
embedder policy; Linch only wires the loop.

Opt-in via ``Agent(hooks=[MemoryExtractionHook(...)])``; with no such hook the
loop is byte-identical.
"""

from __future__ import annotations

from typing import Any


def _agent(provider: Any, *, hooks: Any = None, tools: Any = None):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore
    from linch.tools.registry import empty_tools

    return Agent(
        model="test-model",
        provider=provider,
        tools=tools if tools is not None else empty_tools(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        hooks=hooks,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
    )


async def _collect(session: Any, prompt: str = "go") -> list[Any]:
    return [event async for event in session.run(prompt)]


# ── ConsolidationGate ────────────────────────────────────────────────────────


async def test_consolidation_gate_gates_on_changes_and_interval() -> None:
    from linch.memory import ConsolidationGate

    now = [1000.0]
    gate = ConsolidationGate(min_interval_s=60.0, min_changes=2, clock=lambda: now[0])
    runs = 0

    async def consolidate() -> None:
        nonlocal runs
        runs += 1

    # No changes recorded yet → does not run.
    assert await gate.run(consolidate) is False
    assert runs == 0

    # One change is below min_changes.
    gate.record()
    assert await gate.run(consolidate) is False

    # Second change meets the count gate → runs and resets counters.
    gate.record()
    assert await gate.run(consolidate) is True
    assert runs == 1

    # Immediately after, the interval gate blocks even with enough changes.
    gate.record(2)
    assert await gate.run(consolidate) is False
    assert runs == 1

    # Once the interval elapses it runs again.
    now[0] += 60.0
    assert await gate.run(consolidate) is True
    assert runs == 2


# ── MemoryExtractionHook ─────────────────────────────────────────────────────


async def test_extraction_upserts_without_explicit_tool_call() -> None:
    from linch import MemoryExtractionHook
    from linch.evals import ScriptedProvider, TextTurn
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem

    store = InMemoryKeywordMemoryStore()
    seen: list[Any] = []

    async def extractor(ctx: Any) -> list[MemoryItem]:
        seen.append(ctx)
        return [MemoryItem(id="fav-color", content="user's favorite color is blue")]

    hook = MemoryExtractionHook(store, extractor)
    provider = ScriptedProvider([TextTurn(text="done")])
    agent = _agent(provider, hooks=[hook])
    session = await agent.session()

    await _collect(session)

    items = store.list()
    assert [item.id for item in items] == ["fav-color"]
    # The extractor saw the terminal context with the history tail.
    assert len(seen) == 1
    assert seen[0].history and seen[0].store is store


async def test_extraction_dedups_on_rerun() -> None:
    from linch import MemoryExtractionHook
    from linch.evals import ScriptedProvider, TextTurn
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem

    store = InMemoryKeywordMemoryStore()

    # A non-deterministic id each call: only content-dedup can prevent a dup.
    counter = [0]

    async def extractor(ctx: Any) -> list[MemoryItem]:
        counter[0] += 1
        return [MemoryItem(id=f"mem-{counter[0]}", content="the deploy command is make ship")]

    hook = MemoryExtractionHook(store, extractor)

    for _ in range(2):
        provider = ScriptedProvider([TextTurn(text="done")])
        agent = _agent(provider, hooks=[hook])
        session = await agent.session()
        await _collect(session)

    # Two runs, identical fact, distinct ids → dedup keeps exactly one.
    assert len(store.list()) == 1


async def test_extraction_skipped_when_agent_wrote_memory_this_turn() -> None:
    from linch import MemoryExtractionHook
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.memory import InMemoryKeywordMemoryStore, MemoryItem, MemoryUpsertTool
    from linch.tools import ToolRegistry

    store = InMemoryKeywordMemoryStore()
    extractor_calls = [0]

    async def extractor(ctx: Any) -> list[MemoryItem]:
        extractor_calls[0] += 1
        return [MemoryItem(id="auto", content="auto extracted fact")]

    tools = ToolRegistry()
    tools.register(MemoryUpsertTool(store))
    hook = MemoryExtractionHook(store, extractor)
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="UpsertMemory",
                tool_input={"id": "explicit", "content": "the agent wrote this itself"},
            ),
            TextTurn(text="done"),
        ]
    )
    agent = _agent(provider, hooks=[hook], tools=tools)
    session = await agent.session()
    await _collect(session)

    # The agent already wrote memory this turn → auto-extraction is skipped.
    assert extractor_calls[0] == 0
    assert [item.id for item in store.list()] == ["explicit"]


async def test_extraction_triggers_gated_consolidation() -> None:
    from linch import MemoryExtractionHook
    from linch.evals import ScriptedProvider, TextTurn
    from linch.memory import ConsolidationGate, InMemoryKeywordMemoryStore, MemoryItem

    store = InMemoryKeywordMemoryStore()
    consolidations = [0]

    async def extractor(ctx: Any) -> list[MemoryItem]:
        return [MemoryItem(id="f", content="fact number one two three")]

    async def consolidator(s: Any, ctx: Any) -> None:
        consolidations[0] += 1

    gate = ConsolidationGate(min_changes=1)
    hook = MemoryExtractionHook(
        store, extractor, consolidator=consolidator, consolidation_gate=gate
    )
    provider = ScriptedProvider([TextTurn(text="done")])
    agent = _agent(provider, hooks=[hook])
    session = await agent.session()
    await _collect(session)

    assert consolidations[0] == 1


async def test_no_hook_leaves_store_untouched() -> None:
    from linch.evals import ScriptedProvider, TextTurn
    from linch.memory import InMemoryKeywordMemoryStore

    store = InMemoryKeywordMemoryStore()
    provider = ScriptedProvider([TextTurn(text="done")])
    agent = _agent(provider)
    session = await agent.session()
    await _collect(session)

    assert store.list() == []
