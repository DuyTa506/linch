from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

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


@pytest.mark.parametrize("count", [0, 1, 30])
async def test_below_threshold_request_is_byte_identical(count):
    agent = _agent(_tools(count))
    session = await agent.session()
    assert len(agent.tools.list()) == count
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


async def test_default_defers_native_and_mcp_tools_except_truthy_always_load():
    tools = _tools(30)
    pinned = _tools(1).get("lookup_0")
    original = tools.get("lookup_0")
    tools.replace(
        SimpleNamespace(
            name=original.name,
            description=original.description,
            input_schema=original.input_schema,
            scope=original.scope,
            parallel=False,
            validate=original.validate,
            execute=original.execute,
            summarize=original.summarize,
            always_load="yes",
        )
    )
    pinned.name = "mcp__crm__find"
    tools.register(pinned)
    agent = _agent(tools)
    session = await agent.session()
    request = _request(session)
    assert _names(request) == ["lookup_0", "tool_search"]
    description = request.tools[-1]["description"]
    assert "lookup_29" in description and "mcp__crm__find" in description
    assert "lookup_0," not in description
    assert "Look up customer" not in description
    await agent.tool_search.dispose()
    assert _names(_request(session)) == [tool.name for tool in tools.list()]
    await agent.close()


async def test_search_returns_schema_and_loads_on_next_request():
    agent = _agent(
        _tools(),
        [ToolUseTurn("tool_search", {"query": "select:lookup_2,lookup_4"}), TextTurn("done")],
    )
    session = await agent.session()
    events = await _run(session)
    results = [event for event in events if event.type == "tool_call_end"]
    assert not results[0].is_error
    schemas = json.loads(results[0].result)["tools"]
    assert [schema["name"] for schema in schemas] == ["lookup_2", "lookup_4"]
    assert "value" in schemas[0]["input_schema"]["properties"]
    assert _names(_request(session)) == ["lookup_2", "lookup_4", "tool_search"]
    await agent.close()


async def test_deferred_call_tells_model_to_search_first():
    agent, _, events = await _run_agent(
        _tools(), [ToolUseTurn("lookup_2", {"value": "x"}), TextTurn("done")]
    )
    result = next(event for event in events if event.type == "tool_call_end")
    assert result.is_error
    assert "tool_search" in result.result and "first" in result.result
    await agent.close()


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_threshold_is_rejected(value):
    from linch import ToolSearchPlugin

    with pytest.raises(ValueError, match="non-negative integers"):
        ToolSearchPlugin(max_tools=value)


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


@pytest.mark.parametrize("count", [50, 51])
async def test_skill_threshold_and_body_loading(count):
    turns = (
        [
            ToolUseTurn("tool_search", {"query": "select:skill-2"}, tool_id="search"),
            ToolUseTurn("Skill", {"skill": "skill-2"}, tool_id="skill"),
            TextTurn("done"),
        ]
        if count > 50
        else [TextTurn("done")]
    )
    agent = _agent(_tools(0), turns)
    _add_skills(agent, count)
    session = await agent.session()
    events = await _run(session)
    reminder = session.full_history[0].content[0].text
    if count == 50:
        assert "Skill description 49" in "\n".join(block.text for block in _request(session).system)
        assert "Skill description 49" not in reminder
        assert "tool_search" not in _names(_request(session))
        enabled = asdict(_request(session))
        await agent.tool_search.dispose()
        assert asdict(_request(session)) == enabled
    else:
        assert "Skill description" not in reminder
        assert "skill-50" in reminder
        results = [event for event in events if event.type == "tool_call_end"]
        search = json.loads(results[0].result)
        assert search["skills"] == [{"name": "skill-2", "description": "Skill description 2"}]
        assert "SECRET BODY" not in results[0].result
        assert "SECRET BODY 2" in results[1].result
        assert _names(_request(session)) == ["Skill", "tool_search"]
    await agent.close()


async def test_loaded_names_survive_new_run_reload_and_compaction():
    from linch import InMemorySessionStore
    from linch.types import Message, TextBlock

    store = InMemorySessionStore()
    agent = _agent(
        _tools(),
        [
            ToolUseTurn("tool_search", {"query": "select:lookup_3"}),
            TextTurn("done"),
            TextTurn("later"),
        ],
        session_store=store,
    )
    session = await agent.session(id="persist")
    await _run(session)
    await _run(session, "later")
    assert "lookup_3" in _names(_request(session))
    session.session_log.record_projection(
        [Message(role="user", content=[TextBlock(text="summary")])]
    )
    assert "lookup_3" in _names(_request(session))
    restarted = _agent(_tools(), session_store=store)
    reloaded = await restarted.session(id="persist")
    assert _names(_request(reloaded)) == ["lookup_3", "tool_search"]
    other = await restarted.session(id="other")
    assert _names(_request(other)) == ["tool_search"]
    await agent.close()
    await restarted.close()


async def test_keywords_score_name_and_description_and_no_match_loads_nothing():
    tools = _tools()
    tools.get("lookup_7").description = "Search invoices by account"
    agent = _agent(
        tools,
        [
            ToolUseTurn("tool_search", {"query": "INVOICES account"}),
            TextTurn("done"),
            ToolUseTurn("tool_search", {"query": "no-such-word"}),
            TextTurn("done"),
        ],
    )
    session = await agent.session()
    events = await _run(session)
    result = next(event for event in events if event.type == "tool_call_end")
    assert [s["name"] for s in json.loads(result.result)["tools"]] == ["lookup_7"]
    events = await _run(session)
    result = next(event for event in events if event.type == "tool_call_end")
    assert json.loads(result.result) == {"tools": [], "skills": []}
    assert _names(_request(session)) == ["lookup_7", "tool_search"]
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


async def test_search_only_loads_tools_from_the_next_request_not_the_same_batch():
    from linch import ToolUseBlock
    from linch.abort import AbortContext
    from linch.scheduler import execute_tool_calls

    agent = _agent(_tools())
    session = await agent.session()
    _request(session)
    calls = [
        ToolUseBlock(id="search", name="tool_search", input={"query": "select:lookup_2"}),
        ToolUseBlock(id="lookup", name="lookup_2", input={"value": "x"}),
    ]
    events = [e async for e in execute_tool_calls(calls, agent, session, AbortContext())]
    lookup = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "lookup_2")
    assert lookup.is_error and "select:lookup_2 first" in lookup.result
    assert _names(_request(session)) == ["lookup_2", "tool_search"]
    await agent.close()


@pytest.mark.parametrize("cut", ["tool_call_end", "assistant"])
async def test_loaded_names_survive_crash_and_resume(cut):
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
        if (cut == "tool_call_end" and event.type == cut) or (
            cut == "assistant" and event.type == cut and assistant_count == 2
        ):
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


async def test_dispose_during_run_restores_direct_calls():
    class DisableSearch:
        async def on_after_provider_call(self, ctx):
            await ctx.session.agent.tool_search.dispose()

    agent, _, events = await _run_agent(
        _tools(),
        [
            ToolUseTurn("lookup_2", {"value": "found"}),
            TextTurn("done"),
        ],
        hooks=[DisableSearch()],
    )
    result = next(e for e in events if e.type == "tool_call_end")
    assert not result.is_error and result.result == "found"
    await agent.close()


async def test_catalog_can_cross_threshold_in_both_directions():
    agent = _agent(_tools(30))
    session = await agent.session()
    assert len(_request(session).tools) == 30
    extra = _tools(1).get("lookup_0")
    extra.name = "added_tool"
    agent.tools.register(extra)
    assert _names(_request(session)) == ["tool_search"]
    agent.tools.unregister("added_tool")
    assert _names(_request(session)) == [f"lookup_{i}" for i in range(30)]
    await agent.close()


async def test_terminal_tool_cannot_bypass_discovery():
    agent, _, events = await _run_agent(
        _tools(),
        [
            ToolUseTurn("lookup_2", {"value": "premature"}, tool_id="deferred"),
            ToolUseTurn("tool_search", {"query": "select:lookup_2"}, tool_id="search"),
            ToolUseTurn("lookup_2", {"value": "loaded"}, tool_id="final"),
        ],
        final_tool_name="lookup_2",
    )
    assert events[-1].structured_output == {"value": "loaded"}
    deferred = next(e for e in events if e.type == "tool_call_end" and e.tool_use_id == "deferred")
    assert deferred.is_error and "tool_search" in deferred.result
    await agent.close()


async def test_synthetic_terminal_tool_is_outside_the_native_search_catalog():
    agent, _, events = await _run_agent(
        _tools(), [ToolUseTurn("submit", {"answer": "done"})], final_tool_name="submit"
    )
    assert events[-1].structured_output == {"answer": "done"}
    await agent.close()


async def test_dispose_during_run_restores_terminal_calls():
    class DisableSearch:
        async def on_after_provider_call(self, ctx):
            await ctx.session.agent.tool_search.dispose()

    agent, _, events = await _run_agent(
        _tools(),
        [
            ToolUseTurn("lookup_2", {"value": "found"}),
        ],
        final_tool_name="lookup_2",
        hooks=[DisableSearch()],
    )
    assert events[-1].structured_output == {"value": "found"}
    await agent.close()


@pytest.mark.parametrize("raw", [{}, {"__invalid_json": True, "raw": "{"}])
async def test_malformed_deferred_calls_are_rejected_before_validation(raw):
    agent, _, events = await _run_agent(_tools(), [ToolUseTurn("lookup_2", raw), TextTurn("done")])
    result = next(e for e in events if e.type == "tool_call_end")
    assert result.is_error
    assert (
        result.result == "Tool 'lookup_2' is deferred; call tool_search with select:lookup_2 first"
    )
    await agent.close()


async def test_loaded_ask_user_keeps_its_single_normalization_step():
    from linch import AskUserTool

    tools = _tools()
    tools.register(AskUserTool(lambda request, ctx: {"accepted": True, "answers": {"q": "A"}}))
    questions = {
        "questions": [
            {
                "id": "q",
                "header": "Choice",
                "question": "Pick?",
                "options": [
                    {"label": "A", "description": "First"},
                    {"label": "B", "description": "Second"},
                ],
            }
        ]
    }
    agent, _, events = await _run_agent(
        tools,
        [
            ToolUseTurn("tool_search", {"query": "select:AskUser"}, tool_id="search"),
            ToolUseTurn("AskUser", questions, tool_id="ask"),
            TextTurn("done"),
        ],
    )
    result = next(e for e in events if e.type == "tool_call_end" and e.tool_name == "AskUser")
    assert not result.is_error
    assert json.loads(result.result)["answers"] == {"q": "A"}
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


async def test_resume_keeps_pre_search_snapshot_for_the_unfinished_batch():
    class BatchProvider(ScriptedProvider):
        async def stream(self, req):
            async for event in super().stream(req):
                if event["type"] == "message_end" and self._index == 1:
                    yield {"type": "tool_use_start", "id": "deferred", "name": "lookup_2"}
                    yield {
                        "type": "tool_use_input_delta",
                        "id": "deferred",
                        "json_delta": '{"value":"x"}',
                    }
                    yield {"type": "tool_use_end", "id": "deferred"}
                yield event

    store, runs = memory_session_store(), memory_run_store()
    agent = _agent(_tools(), session_store=store, run_store=runs)
    agent.provider = BatchProvider(
        [
            ToolUseTurn("tool_search", {"query": "select:lookup_2"}, tool_id="search"),
            TextTurn("done"),
        ]
    )
    session = await agent.session(id="snapshot")
    iterator = session.run("go")
    async for event in iterator:
        if event.type == "system":
            run_id = event.run_id
        if event.type == "tool_call_end" and event.tool_name == "tool_search":
            break
    await iterator.aclose()
    restarted = _agent(_tools(), session_store=store, run_store=runs)
    restarted.provider = agent.provider
    resumed = await restarted.session(id="snapshot")
    events = [e async for e in resumed.resume(run_id)]
    deferred = next(e for e in events if e.type == "tool_call_end" and e.tool_use_id == "deferred")
    assert deferred.is_error and "tool_search" in deferred.result
    assert _names(_request(resumed)) == ["lookup_2", "tool_search"]
    await agent.close()
    await restarted.close()


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


async def test_host_catalog_persists_selected_profile_plugin_tool():
    from linch import Profile, RunOptions
    from tests.support.tools import StubTool

    class LocalTool:
        name, version = "local-tool", "1"
        provides = ()
        requires = ("run_config",)

        def setup(self, ctx, config=None):
            return ctx.run_config.tools.register(StubTool("profile_only"))

    agent = _agent(
        _tools(),
        [
            ToolUseTurn("tool_search", {"query": "select:profile_only"}, tool_id="search"),
            ToolUseTurn("profile_only", {}, tool_id="local"),
            TextTurn("done"),
        ],
    )
    session = await agent.session()
    profile = Profile("local", "1", plugins={"tool": (LocalTool(), {})})
    events = [event async for event in session.run("go", RunOptions(profile=profile))]
    local = next(
        event for event in events if event.type == "tool_call_end" and event.tool_use_id == "local"
    )
    assert not local.is_error
    assert "profile_only" in session.meta["linch.tool_search"]["tools"]
    await agent.close()
