from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.stores import memory_run_store, memory_session_store


def _tools(count=31):
    from linch import FunctionTool, ToolRegistry

    registry = ToolRegistry()
    for i in range(count):
        registry.register(
            FunctionTool(
                lambda value: value,
                name=f"lookup_{i}",
                description=f"Look up customer {i} records.",
                scope="read",
            )
        )
    return registry


def _agent(tools, turns=(), **kwargs):
    options = {"model": "m", "tools": tools, "run_store": None, "result_offload": None}
    options.update(kwargs)
    return make_agent(ScriptedProvider(list(turns)), **options)


def _request(session, context=None):
    from linch import RunOptions
    from linch.loop.request import _build_turn_request

    return _build_turn_request(session, RunOptions(), context=context)


def _names(request):
    return [schema["name"] for schema in request.tools]


async def _run(session, prompt="go"):
    return [event async for event in session.run(prompt)]


async def _run_agent(tools, turns=(), **kwargs):
    agent = _agent(tools, turns, **kwargs)
    session = await agent.session()
    return agent, session, await _run(session)


async def test_below_threshold_request_is_byte_identical():
    agent = _agent(_tools(1))
    session = await agent.session()
    assert len(agent.tools.list()) == 1
    enabled = json.dumps(asdict(_request(session)), sort_keys=True)
    await agent.tool_search.dispose()
    disabled = json.dumps(asdict(_request(session)), sort_keys=True)
    assert enabled == disabled
    assert "tool_search" not in _names(_request(session))
    await agent.close()


async def test_agent_copies_registry_and_late_registration_is_agent_local():
    from linch import FunctionTool

    registry = _tools(1)
    agent = _agent(registry)
    caller_late = FunctionTool(
        lambda value: value,
        name="caller_late",
        description="Registered only on the caller registry after construction.",
        scope="read",
    )
    registry.register(caller_late)
    assert agent.tools.get("caller_late") is None

    agent_late = FunctionTool(
        lambda value: value,
        name="agent_late",
        description="Registered directly on this agent after construction.",
        scope="read",
    )
    agent.tools.register(agent_late)
    assert agent.tools.get("agent_late") is agent_late
    assert registry.get("agent_late") is None

    session = await agent.session()
    assert set(_names(_request(session))) == {"lookup_0", "agent_late"}
    await agent.close()


def test_invalid_threshold_is_rejected():
    from linch import ToolSearchPlugin

    with pytest.raises(ValueError, match="non-negative integers"):
        ToolSearchPlugin(max_tools=-1)


async def test_dispose_before_first_session_and_mount_custom_thresholds():
    from linch import ToolSearchPlugin

    agent = _agent(_tools(2))
    await agent.tool_search.dispose()
    mount = await agent.use(ToolSearchPlugin(max_tools=1))
    session = await agent.session()
    assert _names(_request(session)) == ["tool_search"]
    await mount.dispose()
    await mount.dispose()
    assert _names(_request(session)) == ["lookup_0", "lookup_1"]
    await agent.close()


def _add_skills(agent, count):
    from linch.skills import Skill, SkillFrontmatter, build_skill_listing
    from linch.tools.skill import SkillTool

    agent.skills = {
        f"skill-{i}": Skill(
            name=f"skill-{i}",
            dir=".",
            body=f"SECRET BODY {i}",
            frontmatter=SkillFrontmatter(name=f"skill-{i}", description=f"Skill description {i}"),
        )
        for i in range(count)
    }
    agent.skill_listing_text = build_skill_listing(list(agent.skills.values()))
    agent.tools.register(SkillTool(agent.skills, agent._sessions))


async def test_skill_threshold_and_body_loading():
    turns = [
        ToolUseTurn("tool_search", {"query": "select:skill-2"}, tool_id="search"),
        ToolUseTurn("Skill", {"skill": "skill-2"}, tool_id="skill"),
        TextTurn("done"),
    ]
    agent = _agent(_tools(0), turns)
    _add_skills(agent, 51)
    session = await agent.session()
    events = await _run(session)
    reminder = session.full_history[0].content[0].text
    assert "Skill description" not in reminder
    assert "skill-50" in reminder
    results = [event for event in events if event.type == "tool_call_end"]
    search = json.loads(results[0].result)
    assert search["skills"] == [{"name": "skill-2", "description": "Skill description 2"}]
    assert "SECRET BODY" not in results[0].result
    assert "SECRET BODY 2" in results[1].result
    assert _names(_request(session)) == ["Skill", "tool_search"]
    await agent.close()


async def test_keyword_results_use_token_scores_and_rank_best_matches_first():
    tools = _tools()
    tools.get("lookup_0").description = "accounting"
    tools.get("lookup_1").description = "account"
    tools.get("lookup_2").description = "account invoices"
    agent, _, events = await _run_agent(
        tools,
        [ToolUseTurn("tool_search", {"query": "account invoices"}), TextTurn("done")],
    )
    result = next(e for e in events if e.type == "tool_call_end")
    assert [s["name"] for s in json.loads(result.result)["tools"]] == ["lookup_2", "lookup_1"]
    await agent.close()


async def test_context_selection_cannot_be_widened_by_search():
    from linch import ContextBuildResult, ToolContext

    agent = _agent(_tools(32))
    session = await agent.session()
    context = ContextBuildResult(
        selected_tools=[f"lookup_{i}" for i in range(31)] + ["tool_search"]
    )
    assert _names(_request(session, context)) == ["tool_search"]
    search = agent.tools.get("tool_search")
    result = await search.execute(
        {"query": "select:lookup_31"},
        ToolContext(
            cwd=".",
            session_id=session.id,
            run_id="r",
            session_store=session.store,
        ),
    )
    assert json.loads(result.content)["tools"] == []
    await agent.close()


async def test_loaded_tool_still_runs_validation_hooks_and_permissions():
    from linch import HookResult, ToolRule

    seen = []

    class Hook:
        def on_pre_tool_use(self, ctx):
            if ctx.tool_name == "lookup_2":
                seen.append(ctx.input)
                return HookResult.mutate(input={"value": "rewritten"})

    agent = _agent(
        _tools(),
        [
            ToolUseTurn("tool_search", {"query": "select:lookup_2"}, tool_id="search"),
            ToolUseTurn("lookup_2", {"value": "x"}, tool_id="lookup"),
            TextTurn("done"),
            ToolUseTurn("lookup_2", {}, tool_id="invalid"),
            TextTurn("done"),
        ],
        hooks=[Hook()],
    )
    agent.permission_engine.rules.append(ToolRule("lookup_2", "deny"))
    session = await agent.session()
    events = await _run(session)
    lookup = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "lookup_2")
    assert lookup.is_error and "denied" in lookup.result.lower()
    assert seen == [{"value": "x"}]
    events = await _run(session)
    invalid = next(e for e in events if e.type == "tool_call_end")
    assert invalid.is_error and "value" in invalid.result
    assert len(seen) == 1
    await agent.close()


async def test_loaded_names_survive_crash_and_resume():
    session_store, run_store = memory_session_store(), memory_run_store()
    turns = [
        ToolUseTurn("tool_search", {"query": "select:lookup_2"}, tool_id="search"),
        ToolUseTurn("lookup_2", {"value": "found"}, tool_id="lookup"),
        TextTurn("done"),
    ]
    agent = _agent(_tools(), turns, session_store=session_store, run_store=run_store)
    session = await agent.session(id="resume")
    iterator = session.run("go")
    assistant_count = 0
    run_id = None
    async for event in iterator:
        if event.type == "system":
            run_id = event.run_id
        if event.type == "assistant":
            assistant_count += 1
            if assistant_count == 2:
                break
    await iterator.aclose()
    # Reuse the scripted provider's cursor; the resumed batch must skip old calls.
    restarted = _agent(_tools(), session_store=session_store, run_store=run_store)
    restarted.provider = agent.provider
    resumed = await restarted.session(id="resume")
    events = [event async for event in resumed.resume(run_id)]
    assert events[-1].type == "result" and events[-1].subtype == "success"
    assert "lookup_2" in _names(_request(resumed))
    result = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "lookup_2")
    assert result.result == "found" and not result.is_error
    await agent.close()
    await restarted.close()


async def test_failed_metadata_write_does_not_load_tools():
    from linch import InMemorySessionStore

    class FailingMetaStore(InMemorySessionStore):
        async def update_meta(self, id, meta):
            raise RuntimeError("metadata unavailable")

    agent, session, events = await _run_agent(
        _tools(),
        [
            ToolUseTurn("tool_search", {"query": "select:lookup_2"}),
            TextTurn("done"),
        ],
        session_store=FailingMetaStore(),
    )
    result = next(e for e in events if e.type == "tool_call_end")
    assert result.is_error and "metadata unavailable" in result.result
    assert _names(_request(session)) == ["tool_search"]
    await agent.close()


async def test_synthetic_terminal_tool_is_outside_the_native_search_catalog():
    agent, _, events = await _run_agent(
        _tools(), [ToolUseTurn("submit", {"answer": "done"})], final_tool_name="submit"
    )
    assert events[-1].structured_output == {"answer": "done"}
    await agent.close()


@pytest.mark.parametrize("same_session_id", [False, True])
async def test_agents_sharing_registry_keep_search_state_isolated(same_session_id):
    from linch import Context

    registry = _tools()
    parent = Context()
    a = _agent(
        registry,
        [
            ToolUseTurn("tool_search", {"query": "select:lookup_2"}),
            TextTurn("done"),
        ],
        context=parent,
    )
    b = _agent(registry, context=parent)
    sa = await a.session(id="a")
    sb = await b.session(id="a" if same_session_id else "b")
    _request(sb)
    events = await _run(sa)
    result = next(e for e in events if e.type == "tool_call_end")
    assert not result.is_error
    assert _names(_request(sa)) == ["lookup_2", "tool_search"]
    assert _names(_request(sb)) == ["tool_search"]
    assert registry.get("tool_search") is None
    await a.close()
    await b.close()
    await parent.dispose()


async def test_compaction_reinjects_only_deferred_skill_names():
    from linch.loop.request import _re_inject_skill_context
    from linch.types import Message, TextBlock

    agent = _agent(_tools(0))
    _add_skills(agent, 51)
    session = await agent.session()
    session.session_log.record_projection(
        [Message(role="user", content=[TextBlock(text="summary")])]
    )
    _re_inject_skill_context(session)
    text = "\n".join(
        b.text for m in session.provider_view for b in m.content if isinstance(b, TextBlock)
    )
    assert "skill-50" in text and "tool_search" in text
    assert "Skill description" not in text
    await agent.close()


async def test_deferral_snapshot_survives_catalog_shrinking_during_request():
    from linch import ToolUseBlock
    from linch.abort import AbortContext
    from linch.scheduler import execute_tool_calls

    agent = _agent(_tools())
    session = await agent.session()
    assert _names(_request(session)) == ["tool_search"]
    agent.tools.unregister("lookup_30")
    calls = [ToolUseBlock(id="lookup", name="lookup_2", input={"value": "x"})]
    events = [e async for e in execute_tool_calls(calls, agent, session, AbortContext())]
    result = next(e for e in events if e.type == "tool_call_end")
    assert result.is_error and "select:lookup_2" in result.result
    assert _names(_request(session)) == [f"lookup_{i}" for i in range(30)]
    await agent.close()


async def test_empty_profile_blocks_deferred_call_before_selection():
    from linch import Profile, RunOptions
    from tests.support.tools import StubTool

    seen, before_selection = [], []

    class Probe(StubTool):
        def validate(self, raw):
            seen.append("validate")
            return super().validate(raw)

    class Hook:
        def on_pre_tool_use(self, ctx):
            if ctx.tool_name == "lookup_2":
                seen.append("hook")

    class CaptureBoundary(ScriptedProvider):
        async def stream(self, req):
            if len(self.requests) == 1:
                before_selection.append(list(seen))
            async for event in super().stream(req):
                yield event

    registry = _tools()
    registry.replace(Probe("lookup_2", on_execute=lambda raw, ctx: seen.append("execute")))
    agent = _agent(registry, hooks=[Hook()])
    agent.provider = CaptureBoundary(
        [
            ToolUseTurn("lookup_2", {"value": "x"}, tool_id="blocked"),
            ToolUseTurn("tool_search", {"query": "select:lookup_2"}, tool_id="search"),
            ToolUseTurn("lookup_2", {"value": "x"}, tool_id="selected"),
            TextTurn("done"),
        ]
    )
    evaluate = agent.permission_engine.evaluate

    def record_permission(call):
        if call.tool.name == "lookup_2":
            seen.append("permission")
        return evaluate(call)

    agent.permission_engine.evaluate = record_permission
    session = await agent.session()
    events = [event async for event in session.run("go", RunOptions(profile=Profile("empty", "1")))]
    result = next(event for event in events if event.type == "tool_call_end")
    assert _names(agent.provider.requests[0]) == ["tool_search"]
    assert result.is_error
    assert (
        result.result == "Tool 'lookup_2' is deferred; call tool_search with select:lookup_2 first"
    )
    assert before_selection == [[]]
    selected = next(
        event
        for event in events
        if event.type == "tool_call_end" and event.tool_use_id == "selected"
    )
    assert not selected.is_error
    assert {"validate", "hook", "permission", "execute"} <= set(seen)
    await agent.close()


async def test_profile_local_catalog_shadows_host_guard_and_selects_tool():
    from linch import Profile, RunOptions, ToolSearchPlugin

    agent = _agent(
        _tools(1),
        [
            ToolUseTurn("lookup_0", {"value": "blocked"}, tool_id="blocked"),
            ToolUseTurn("tool_search", {"query": "select:lookup_0"}, tool_id="search"),
            ToolUseTurn("lookup_0", {"value": "selected"}, tool_id="selected"),
            TextTurn("done"),
        ],
    )
    session = await agent.session()
    profile = Profile("search", "1", plugins={"search": (ToolSearchPlugin(max_tools=0), {})})
    host = agent.context.get("request_catalog")
    events = [event async for event in session.run("go", RunOptions(profile=profile))]
    results = [event for event in events if event.type == "tool_call_end"]
    assert _names(agent.provider.requests[0]) == ["tool_search"]
    assert results[0].is_error and "deferred" in results[0].result
    assert not results[1].is_error
    assert results[2].result == "selected"
    assert agent.context.get("request_catalog") is host
    assert agent.tools.get("tool_search") is None
    await agent.close()


async def test_empty_profile_terminal_guard_requires_selection():
    from linch import Profile, RunOptions

    agent = _agent(
        _tools(),
        [
            ToolUseTurn("lookup_2", {"value": "blocked"}, tool_id="blocked"),
            ToolUseTurn("tool_search", {"query": "select:lookup_2"}, tool_id="search"),
            ToolUseTurn("lookup_2", {"value": "selected"}, tool_id="selected"),
        ],
        final_tool_name="lookup_2",
    )
    session = await agent.session()
    events = [event async for event in session.run("go", RunOptions(profile=Profile("empty", "1")))]
    assert events[-1].structured_output == {"value": "selected"}
    assert len(agent.provider.requests) == 3
    assert any(
        event.type == "tool_call_end" and event.tool_use_id == "blocked" and event.is_error
        for event in events
    )
    await agent.close()
