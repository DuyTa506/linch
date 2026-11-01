from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest

from linch import Profile, RunOptions
from tests.support.agents import make_agent as support_make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


def _echo_tool(name: str) -> StubTool:
    seen: list[dict[str, Any]] = []
    tool = StubTool(name, on_execute=lambda raw, _: seen.append(dict(raw)))
    tool.seen = seen
    return tool


class _NamedPlugin:
    """Deterministic plugin fixture with stable identity metadata."""

    provides: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()

    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version

    def setup(self, ctx: Any, config: Any = None) -> Any:
        from linch.kernel import Disposable

        return Disposable()


class _ConfiguredTool(StubTool):
    def __init__(self, label: str) -> None:
        seen: list[dict[str, Any]] = []
        super().__init__(
            "ProfileTool", output=f"tool:{label}", on_execute=lambda raw, _: seen.append(raw)
        )
        self.label = label
        self.seen = seen


class _ConfiguredWrapper:
    resume_policy_id = "tests.profile.wrapper"
    resume_policy_version = "1"

    def __init__(self, label: str) -> None:
        self.label = label
        self.resume_policy_config = {"label": label}

    async def __call__(self, execution: Any, next: Any) -> Any:
        result = await next()
        result.content = f"wrapped:{self.label}:{result.content}"
        return result


class _ScopedProfilePlugin:
    provides = ("profile.service",)
    requires: tuple[str, ...] = ()

    def __init__(self, name: str, events: list[tuple[str, str, Any]]) -> None:
        self.name = name
        self.version = "1"
        self.events = events
        self.tools: list[_ConfiguredTool] = []

    def setup(self, ctx: Any, config: Any = None) -> Any:
        from linch.kernel import Disposable

        label = config["label"]

        def on_service(changed: Any, key: str) -> None:
            self.events.append((label, key, changed.get(key)))

        listener = ctx.on("context/service", on_service)
        service = ctx.register("profile.service", config)
        tool = _ConfiguredTool(label)
        self.tools.append(tool)
        tool_disposer = ctx.run_config.tools.replace(tool)
        wrapper_disposer = ctx.run_config.tool_pipeline.on_execute(_ConfiguredWrapper(label))

        async def cleanup() -> None:
            await wrapper_disposer.dispose()
            await tool_disposer.dispose()
            await service.dispose()
            await listener.dispose()

        return Disposable(cleanup)


class _ScopedProfileProvider:
    id = "scoped-profile-provider"

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()
        self._first_active = 0

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        from linch.types import Usage

        self.requests.append(req)
        system = "\n".join(block.text.lower() for block in req.system)
        label = "support" if "support" in system else "billing"
        has_tool_result = any(
            getattr(block, "type", None) == "tool_result"
            for message in req.messages
            for block in message.content
        )
        if not has_tool_result:
            self._first_active += 1
            if self._first_active == 2:
                self.both_started.set()
            await self.both_started.wait()
            await self.release.wait()
            self._first_active -= 1
        yield {"type": "message_start", "model": req.model}
        if has_tool_result:
            yield {"type": "text_delta", "text": f"{label} answer"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
            return
        yield {"type": "tool_use_start", "id": f"{label}-call", "name": "ProfileTool"}
        yield {
            "type": "tool_use_input_delta",
            "id": f"{label}-call",
            "json_delta": '{"value":"profile"}',
        }
        yield {"type": "tool_use_end", "id": f"{label}-call"}
        yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}


class _CaptureProvider(ScriptedProvider):
    """Small provider that records requests while emitting scripted turns."""

    id = "profile-capture"

    def __init__(self, turns: list[tuple[str, str]], *, barrier: bool = False) -> None:
        scripted = [
            TextTurn(value) if kind == "text" else ToolUseTurn(value, {}) for kind, value in turns
        ]
        super().__init__(scripted)
        self.barrier = barrier
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()
        self._active = 0
        self.max_active = 0

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        turn = self._turns[self._index]
        self._index += 1
        self.requests.append(req)
        if self.barrier:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active >= 2:
                self.both_started.set()
            await self.both_started.wait()
            await self.release.wait()
        try:
            async for event in ScriptedProvider([turn]).stream(req):
                yield event
        finally:
            if self.barrier:
                self._active -= 1


def _agent(provider: Any, tools: Any, *, run_store: Any = None, **kwargs: Any) -> Any:
    permissions = kwargs.pop("permissions", {"mode": "skip-dangerous"})
    return support_make_agent(
        provider,
        model="profile-model",
        tools=tools,
        permissions=permissions,
        run_store=run_store,
        result_offload=None,
        **kwargs,
    )


def test_profile_layers_replace_rows_and_resolve_without_live_objects() -> None:

    search = _NamedPlugin("search", "1")
    replacement = _NamedPlugin("search", "2")
    base_config = {"endpoint": {"region": "us"}}
    base = Profile(
        id="base",
        version="1",
        system_prompt="base role",
        plugins={"search": (search, base_config)},
        tool_allow=("Search", "Read"),
        max_turns=8,
    )
    layer_config = {"endpoint": {"region": "eu"}}
    layer = Profile(
        id="support",
        version="2",
        system_prompt="support role",
        plugins={
            "search": (replacement, layer_config),
            "billing": (_NamedPlugin("billing", "1"), {}),
        },
        tool_deny=("Write",),
        output_schema={"name": "answer", "schema": {"type": "object"}},
    )

    resolved = base.layer(layer).resolve()
    assert resolved["id"] == "support"
    assert resolved["version"] == "2"
    assert resolved["system_prompt"] == "support role"
    assert set(resolved["plugins"]) == {"search", "billing"}
    assert resolved["plugins"]["search"]["config"] == {"endpoint": {"region": "eu"}}
    assert "tool_allow" in resolved and resolved["tool_allow"] == ["Search", "Read"]
    assert resolved["tool_deny"] == ["Write"]
    assert resolved["plugins"]["search"]["plugin"] == {
        "name": getattr(replacement, "name", None),
        "version": getattr(replacement, "version", None),
    }

    # Resolution is a detached, inspectable snapshot; caller-owned config cannot
    # mutate either the frozen profile or an earlier resolved view.
    layer_config["endpoint"]["region"] = "changed"
    resolved["plugins"]["search"]["config"]["endpoint"]["region"] = "mutated"
    assert base_config["endpoint"]["region"] == "us"
    assert base.resolve()["plugins"]["search"]["config"]["endpoint"]["region"] == "us"
    with pytest.raises((AttributeError, TypeError)):
        base.system_prompt = "changed"


@pytest.mark.asyncio
async def test_two_profiles_run_concurrently_on_one_agent_with_distinct_tools_and_prompts() -> None:
    support = _echo_tool("SupportSearch")
    billing = _echo_tool("BillingLookup")
    tools = tool_registry(support, billing)
    provider = _CaptureProvider(
        [("text", "support answer"), ("text", "billing answer")], barrier=True
    )
    agent = _agent(provider, tools)
    support_profile = Profile(
        id="support",
        version="1",
        system_prompt="Answer support questions.",
        tool_allow=("SupportSearch",),
    )
    billing_profile = Profile(
        id="billing",
        version="1",
        system_prompt="Answer billing questions.",
        tool_allow=("BillingLookup",),
    )
    sessions = [await agent.session(id="support"), await agent.session(id="billing")]

    async def run(session: Any, profile: Any, prompt: str) -> list[Any]:
        return [event async for event in session.run(prompt, RunOptions(profile=profile))]

    support_task = asyncio.create_task(run(sessions[0], support_profile, "How do I reset access?"))
    billing_task = asyncio.create_task(run(sessions[1], billing_profile, "Where is my invoice?"))
    await asyncio.wait_for(provider.both_started.wait(), timeout=1)
    provider.release.set()
    support_events, billing_events = await asyncio.gather(support_task, billing_task)

    assert support_events[-1].final_text == "support answer"
    assert billing_events[-1].final_text == "billing answer"
    requests = {request.messages[-1].content[-1].text: request for request in provider.requests}
    assert [schema["name"] for schema in requests["How do I reset access?"].tools] == [
        "SupportSearch"
    ]
    assert [schema["name"] for schema in requests["Where is my invoice?"].tools] == [
        "BillingLookup"
    ]
    assert any(
        "support" in block.text.lower() for block in requests["How do I reset access?"].system
    )
    assert any("billing" in block.text.lower() for block in requests["Where is my invoice?"].system)
    assert provider.max_active == 2
    assert all(session.tools_override is None for session in sessions)
    assert all(session.system_blocks_override is None for session in sessions)
    assert all(session.current_turn_allowed_tools is None for session in sessions)
    assert agent.system_prompt == "" or agent.system_prompt is None


@pytest.mark.asyncio
async def test_profile_filter_intersects_agent_tool_surface_and_applies_output_schema() -> None:
    support = _echo_tool("SupportSearch")
    tools = tool_registry(support)
    provider = _CaptureProvider([("text", '{"answer":"done"}')])
    agent = _agent(provider, tools)
    profile = Profile(
        id="support",
        version="1",
        tool_allow=("SupportSearch", "BillingLookup"),
        tool_deny=("SupportSearch",),
        output_schema={
            "name": "answer",
            "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
        },
    )
    session = await agent.session()
    events = [event async for event in session.run("go", RunOptions(profile=profile))]
    assert events[-1].subtype == "success"
    assert provider.requests[0].tools == []
    assert provider.requests[0].output_schema is not None
    assert provider.requests[0].output_schema.name == "answer"
    assert provider.requests[0].output_schema.schema["type"] == "object"


@pytest.mark.asyncio
async def test_profile_allowlist_cannot_bypass_agent_permission_deny() -> None:
    from linch import ToolCallEndEvent
    from linch.permissions import ToolRule

    tool = _echo_tool("SupportSearch")
    tools = tool_registry(tool)
    provider = _CaptureProvider([("tool", "SupportSearch"), ("text", "done")])
    agent = _agent(
        provider,
        tools,
        permissions={"mode": "skip-dangerous", "rules": [ToolRule("SupportSearch", "deny")]},
    )
    profile = Profile(id="support", version="1", tool_allow=("SupportSearch",))
    session = await agent.session()
    events = [event async for event in session.run("go", RunOptions(profile=profile))]
    denied = next(event for event in events if isinstance(event, ToolCallEndEvent))
    assert denied.is_error is True
    assert tool.seen == []


@pytest.mark.asyncio
async def test_profile_max_turns_is_effective_for_run_without_mutating_agent() -> None:
    from linch import LoopGuardEvent, ResultEvent

    tool = _echo_tool("SupportSearch")
    tools = tool_registry(tool)
    provider = _CaptureProvider([("tool", "SupportSearch"), ("text", "unreached")])
    agent = _agent(provider, tools, loop_guard=None)
    session = await agent.session()
    events = [
        event
        async for event in session.run(
            "go", RunOptions(profile=Profile(id="support", version="1", max_turns=1))
        )
    ]
    assert any(
        isinstance(event, LoopGuardEvent) and event.reason == "max_turns" for event in events
    )
    assert isinstance(events[-1], ResultEvent)
    assert events[-1].subtype == "error"
    assert agent.max_turns == float("inf")


@pytest.mark.asyncio
async def test_profile_contract_pins_id_version_and_content_hash_on_resume() -> None:
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore

    store = InMemoryRunStore()
    provider = _CaptureProvider([("text", "done")])
    agent = _agent(provider, tool_registry(), run_store=store)
    session = await agent.session(id="s")
    profile = Profile(id="support", version="1", system_prompt="support")
    iterator = session.run("go", RunOptions(profile=profile)).__aiter__()
    first = await iterator.__anext__()
    run_id = first.run_id
    await iterator.aclose()
    record = await store.load_run(run_id)
    assert record is not None
    pinned = record.meta["run_contract"]["payload"]["run_options"]["profile"]
    assert pinned["id"] == "support"
    assert pinned["version"] == "1"
    assert pinned["content_hash"].startswith("sha256:")

    provider._turns = [TextTurn("must not dispatch")]
    for changed in (
        Profile(id="support", version="2", system_prompt="support"),
        Profile(id="support", version="1", system_prompt="changed definition"),
    ):
        with pytest.raises(ConfigError, match="profile"):
            _ = [event async for event in session.resume(run_id, RunOptions(profile=changed))]
    assert provider._index == 0

    with pytest.raises(ConfigError, match="profile"):
        _ = [event async for event in session.resume(run_id, RunOptions())]
    assert provider._index == 0


@pytest.mark.asyncio
async def test_profile_budget_is_fresh_per_run_and_explicit_budget_wins() -> None:
    from linch import RunBudget

    provider = _CaptureProvider([("text", "one"), ("text", "two")])
    agent = _agent(provider, tool_registry())
    first, second = await agent.session(id="first"), await agent.session(id="second")
    profile = Profile(id="support", version="1", budget={"max_tokens": 123})

    await _collect(first, RunOptions(profile=profile))
    explicit = RunBudget(max_tokens=7)
    await _collect(second, RunOptions(profile=profile, budget=explicit))

    assert first.active_budget is not second.active_budget
    assert first.active_budget.max_tokens == 123
    assert second.active_budget is explicit
    assert second.active_budget.max_tokens == 7


@pytest.mark.asyncio
async def test_same_profile_resumes_on_a_cold_agent_after_lease_takeover() -> None:
    from linch.run_store import InMemoryRunStore

    store = InMemoryRunStore()
    profile = Profile(id="support", version="1", system_prompt="support")
    first_provider = _CaptureProvider([("text", "interrupted")])
    first = _agent(first_provider, tool_registry(), run_store=store)
    session = await first.session(id="s")
    iterator = session.run("go", RunOptions(profile=profile)).__aiter__()
    first_event = await iterator.__anext__()
    run_id = first_event.run_id
    await iterator.aclose()

    second_store = copy.copy(store)
    await store.claim(run_id, "worker-a", 0.01)
    await asyncio.sleep(0.02)
    new = await second_store.claim(run_id, "worker-b", 60.0)

    second_provider = _CaptureProvider([("text", "resumed")])
    second = _agent(second_provider, tool_registry(), run_store=second_store)
    resumed = await second.session(id="s")
    events = [
        event async for event in resumed.resume(run_id, RunOptions(profile=profile, lease=new))
    ]
    assert events[-1].final_text == "resumed"
    assert second_provider._index == 1


@pytest.mark.asyncio
async def test_profile_plugins_receive_config_and_are_cleaned_up_per_run() -> None:
    from linch.kernel import Disposable

    calls: list[tuple[str, Any]] = []

    class MarkerPlugin:
        name = "marker"
        version = "1"
        provides = ("profile_marker",)
        requires = ()

        def setup(self, ctx: Any, config: Any = None) -> Disposable:
            calls.append(("setup", config))
            registered = ctx.register("profile_marker", config)

            async def cleanup() -> None:
                calls.append(("cleanup", config))
                await registered.dispose()

            return Disposable(cleanup)

    profile = Profile(
        id="support",
        version="1",
        plugins={"marker": (MarkerPlugin(), {"tenant": "acme"})},
    )
    provider = _CaptureProvider([("text", "one"), ("text", "two")])
    agent = _agent(provider, tool_registry())
    first, second = await agent.session(id="one"), await agent.session(id="two")
    await asyncio.gather(
        *(_collect(session, RunOptions(profile=profile)) for session in (first, second))
    )
    assert calls.count(("setup", {"tenant": "acme"})) == 2
    assert calls.count(("cleanup", {"tenant": "acme"})) == 2
    assert agent.context.get("profile_marker") is None
    assert first.tools_override is None and second.tools_override is None
    assert first.system_blocks_override is None and second.system_blocks_override is None


@pytest.mark.asyncio
async def test_profile_plugin_cleanup_runs_when_run_generator_closes_early() -> None:
    from linch.kernel import Disposable

    cleaned: list[str] = []

    class ClosingPlugin:
        name = "closing-marker"
        version = "1"
        provides = ("profile_marker",)
        requires = ()

        def setup(self, ctx: Any, config: Any = None) -> Disposable:
            registration = ctx.register("profile_marker", config)

            async def cleanup() -> None:
                cleaned.append("cleanup")
                await registration.dispose()

            return Disposable(cleanup)

    profile = Profile(
        id="support",
        version="1",
        plugins={"closing-marker": (ClosingPlugin(), {"tenant": "acme"})},
    )
    provider = _CaptureProvider([("text", "done")])
    agent = _agent(provider, tool_registry())
    session = await agent.session()
    iterator = session.run("go", RunOptions(profile=profile)).__aiter__()
    await iterator.__anext__()
    await iterator.aclose()

    assert cleaned == ["cleanup"]
    assert agent.context.get("profile_marker") is None


@pytest.mark.asyncio
async def test_profile_plugin_scope_adds_tool_pipeline_wrapper_and_context_listener() -> None:
    from linch import ToolCallEndEvent

    service_events: list[tuple[str, str, Any]] = []
    support_plugin = _ScopedProfilePlugin("support-plugin", service_events)
    billing_plugin = _ScopedProfilePlugin("billing-plugin", service_events)
    profile_support = Profile(
        id="support",
        version="1",
        system_prompt="support role",
        plugins={"domain": (support_plugin, {"label": "support"})},
        tool_allow=("ProfileTool",),
    )
    profile_billing = Profile(
        id="billing",
        version="1",
        system_prompt="billing role",
        plugins={"domain": (billing_plugin, {"label": "billing"})},
        tool_allow=("ProfileTool",),
    )
    provider = _ScopedProfileProvider()
    agent = _agent(provider, tool_registry())
    support_session = await agent.session(id="support")
    billing_session = await agent.session(id="billing")

    support_task = asyncio.create_task(
        _collect(support_session, RunOptions(profile=profile_support))
    )
    billing_task = asyncio.create_task(
        _collect(billing_session, RunOptions(profile=profile_billing))
    )
    await asyncio.wait_for(provider.both_started.wait(), timeout=1)
    provider.release.set()
    support_events, billing_events = await asyncio.gather(support_task, billing_task)

    support_end = next(event for event in support_events if isinstance(event, ToolCallEndEvent))
    billing_end = next(event for event in billing_events if isinstance(event, ToolCallEndEvent))
    assert support_end.result == "wrapped:support:tool:support"
    assert billing_end.result == "wrapped:billing:tool:billing"
    assert ("support", "profile.service", {"label": "support"}) in service_events
    assert ("billing", "profile.service", {"label": "billing"}) in service_events
    assert support_plugin.tools[0].seen and billing_plugin.tools[0].seen

    # Both mounts are run-local: neither plugin's service, tool, or wrapper
    # survives back on the parent Agent after its profile scope is disposed.
    assert agent.context.get("profile.service") is None
    assert agent.tools.get("ProfileTool") is None
    assert not agent.tool_pipeline.has_listeners()
    assert support_session.tools_override is None
    assert billing_session.tools_override is None
    assert support_session.agent is agent
    assert billing_session.agent is agent


def test_profile_none_is_byte_identical_to_omitted_run_options() -> None:
    from linch.loop.contracts import _build_run_contract
    from linch.run_store import run_contract_to_dict

    agent = _agent(_CaptureProvider([]), tool_registry())
    session = agent._sessions.get("unused")
    assert session is None

    async def build() -> tuple[dict[str, Any], dict[str, Any]]:
        live = await agent.session(id="contract")
        omitted = _build_run_contract(live, "go", RunOptions())
        explicit_none = _build_run_contract(live, "go", RunOptions(profile=None))
        return run_contract_to_dict(omitted), run_contract_to_dict(explicit_none)

    omitted, explicit_none = asyncio.run(build())
    assert omitted == explicit_none
    assert "profile" not in omitted["payload"]["run_options"]


def test_profile_resolve_preserves_plugin_execution_order() -> None:

    profile = Profile(
        id="ordered",
        version="1",
        plugins={
            "z-provider": (_NamedPlugin("provider", "1"), {}),
            "a-dependent": (_NamedPlugin("dependent", "1"), {}),
        },
    )
    resolved = profile.resolve()
    assert list(resolved["plugins"]) == ["z-provider", "a-dependent"]
    assert profile._contract()["id"] == "ordered"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"id": "", "version": "1"}, "profile id"),
        ({"id": "support", "version": ""}, "profile id"),
        ({"id": "support", "version": 1}, "profile id"),
        ({"id": "support", "version": "1", "max_turns": 0}, "max_turns"),
        ({"id": "support", "version": "1", "max_turns": True}, "max_turns"),
        ({"id": "support", "version": "1", "tool_allow": "Read"}, "tool_allow"),
        ({"id": "support", "version": "1", "tool_deny": ("",)}, "tool_deny"),
        ({"id": "support", "version": "1", "system_prompt": 7}, "system_prompt"),
        ({"id": "support", "version": "1", "budget": {"max_tokens": "many"}}, "budget"),
        ({"id": "support", "version": "1", "budget": {"warn_ratio": None}}, "budget"),
        ({"id": "support", "version": "1", "output_schema": {}}, "output_schema"),
        (
            {"id": "support", "version": "1", "output_schema": {"name": "answer", "schema": None}},
            "output_schema",
        ),
    ],
)
def test_profile_rejects_invalid_declarative_values(kwargs: dict[str, Any], message: str) -> None:
    from linch.errors import ConfigError

    with pytest.raises(ConfigError, match=message):
        Profile(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_retained_child_keeps_profile_service_after_parent_scope_ends(
    background: bool,
) -> None:
    from linch.profiles import _profile_scope
    from linch.run_store import InMemoryRunStore
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.subagents.types import AgentDefinition, AgentFrontmatter
    from linch.subagents.workers import WorkerHandle

    cleaned, cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class ServicePlugin:
        name = "retained-service"
        version = "1"
        provides = ("profile.service",)
        requires = ()

        def setup(self, ctx: Any, config: Any = None) -> Any:
            from linch.kernel import Disposable

            ctx.register("profile.service", config)

            async def cleanup() -> None:
                cleanup_started.set()
                await cleanup_release.wait()
                cleaned.set()

            return Disposable(cleanup)

    store = InMemoryRunStore()
    agent = _agent(
        _CaptureProvider([("text", "child done")]),
        tool_registry(),
        run_store=store,
    )
    session = await agent.session(id="parent")
    profile = Profile(
        id="support",
        version="1",
        plugins={"service": (ServicePlugin(), {"tenant": "acme"})},
    )
    definition = AgentDefinition(
        name="retained",
        file_path="<test>",
        source="built-in",
        frontmatter=AgentFrontmatter(name="retained", description="test", tools=[]),
        body="Return the child answer.",
    )
    async with _profile_scope(session, profile, "profile-run"):
        args = RunSubagentArgs(
            parent_session=session,
            parent_agent=session.agent,
            config_binding=session._run_bindings["profile-run"],
            definition=definition,
            prompt="child prompt",
            display_name="retained",
            subagent_run_id="retained-run",
            run_options=RunOptions(),
            retain=True,
        )
        if background:
            from linch.profiles import _schedule_with_binding

            binding = session._run_bindings["profile-run"]
            task = _schedule_with_binding(binding, run_subagent(args))
            session.workers["worker"] = WorkerHandle(
                "worker", "pending", "retained", definition, task=task
            )
        else:
            result = await run_subagent(args)

    if background:
        result = await task
    child = agent._sessions[result.child_session_id]
    assert child.agent is agent
    assert child._config_seed.config.context.get("profile.service") == {"tenant": "acme"}
    releasing = asyncio.create_task(agent.release_session(child.id))
    await asyncio.wait_for(cleanup_started.wait(), 1)
    waited_for_cleanup = not releasing.done()
    cleanup_release.set()
    await releasing
    await asyncio.wait_for(cleaned.wait(), 1)
    assert waited_for_cleanup
    assert child._config_seed is None
    assert agent.context.get("profile.service") is None
    await agent.close()


async def _collect(session: Any, opts: Any) -> list[Any]:
    return [event async for event in session.run("go", opts)]


@pytest.mark.asyncio
async def test_profile_plugin_tracks_removal_of_inherited_requirement() -> None:
    from linch.profiles import _profile_scope

    class DependentPlugin:
        name = "dependent"
        version = "1"
        provides = ("dependent",)
        requires = ("dependency",)

        def setup(self, ctx: Any, config: Any = None) -> Any:
            return ctx.register("dependent", "active")

    agent = _agent(_CaptureProvider([]), tool_registry())
    dependency = agent.context.register("dependency", object())
    session = await agent.session()
    profile = Profile(id="support", version="1", plugins={"dep": (DependentPlugin(), {})})
    async with _profile_scope(session, profile, "profile-run") as config:
        assert config.context.get("dependent") == "active"
        await dependency.dispose()
        assert config.context.get("dependent") is None
    await agent.close()


@pytest.mark.asyncio
async def test_profile_filter_preserves_host_ruleset_path_root() -> None:
    from linch import empty_tools
    from linch.permissions import PathRule, PermissionLayer, PermissionRuleSet
    from linch.permissions.engine import PendingToolCall
    from linch.profiles import _profile_scope

    write = _echo_tool("Write")
    agent = _agent(_CaptureProvider([]), empty_tools(write, _echo_tool("Other")))
    agent.permission_engine.rule_set = PermissionRuleSet(
        [PermissionLayer("host", [PathRule(["secret/**"], "deny")])],
        project_root="/tenant-policy",
    )
    call = PendingToolCall("call", write, {"file_path": "/tenant-policy/secret/key"})
    assert agent.permission_engine.evaluate(call).decision == "deny"
    session = await agent.session()
    async with _profile_scope(
        session, Profile(id="support", version="1", tool_allow=("Write",)), "profile-run"
    ) as config:
        assert config.permission_engine.evaluate(call).decision == "deny"
    await agent.close()


@pytest.mark.asyncio
async def test_profile_scope_disposes_after_last_background_task_finishes() -> None:
    from linch import empty_tools
    from linch.kernel import Disposable
    from linch.profiles import _profile_scope

    cleaned, release = asyncio.Event(), asyncio.Event()

    class CleanupPlugin(_NamedPlugin):
        def setup(self, ctx: Any, config: Any = None) -> Any:
            return Disposable(cleaned.set)

    agent = _agent(_CaptureProvider([]), empty_tools())
    session = await agent.session()
    profile = Profile(id="support", version="1", plugins={"cleanup": (CleanupPlugin("c", "1"), {})})
    async with _profile_scope(session, profile, "profile-run"):
        from linch.profiles import _schedule_with_binding

        tasks = [
            _schedule_with_binding(session._run_bindings["profile-run"], release.wait())
            for _ in range(2)
        ]
        session.background_tasks.extend(tasks)
    assert not cleaned.is_set()
    release.set()
    await asyncio.gather(*tasks)
    await asyncio.wait_for(cleaned.wait(), 1)
    assert session.agent is agent
    await agent.close()


@pytest.mark.asyncio
async def test_profile_plugin_listener_does_not_affect_concurrent_plain_run() -> None:
    from linch import ToolCallEndEvent, empty_tools

    provider = _ScopedProfileProvider()
    agent = _agent(provider, empty_tools(_ConfiguredTool("plain")))
    scoped, plain = await agent.session(), await agent.session()
    profile = Profile(
        id="support",
        version="1",
        system_prompt="support role",
        plugins={"domain": (_ScopedProfilePlugin("support", []), {"label": "support"})},
    )
    running = asyncio.gather(
        _collect(scoped, RunOptions(profile=profile)), _collect(plain, RunOptions())
    )
    await asyncio.wait_for(provider.both_started.wait(), 1)
    provider.release.set()
    profiled_events, plain_events = await running
    profiled_end = next(e for e in profiled_events if isinstance(e, ToolCallEndEvent))
    plain_end = next(e for e in plain_events if isinstance(e, ToolCallEndEvent))
    assert profiled_end.result == "wrapped:support:tool:support"
    assert plain_end.result == "tool:plain"
    assert not agent.tool_pipeline.has_listeners()
    await agent.close()


async def test_profile_tool_observes_real_agent_identity():
    owner = None
    seen = []

    def inspect_owner(raw, ctx):
        seen.append(owner._sessions[ctx.session_id].agent)

    tool = StubTool("inspect_owner", on_execute=inspect_owner)
    owner = _agent(
        ScriptedProvider([ToolUseTurn("inspect_owner", {}), TextTurn("done")]), tool_registry(tool)
    )
    session = await owner.session()
    await _collect(session, RunOptions(profile=Profile("empty", "1")))
    assert seen == [owner]
    await owner.close()


def _child_definition():
    from linch.subagents.types import AgentDefinition, AgentFrontmatter

    return AgentDefinition(
        "child", "<test>", "built-in", AgentFrontmatter("child", "child", []), "child instructions"
    )


@pytest.mark.parametrize("bound", [None, 1])
async def test_profile_retained_child_is_owned_and_can_be_forked(bound):
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    children = []
    owner = None
    parent = None

    async def spawn(raw, ctx):
        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=parent.agent,
                definition=_child_definition(),
                prompt="child",
                display_name="child",
                subagent_run_id="child",
                retain=True,
            )
        )
        children.append(owner._sessions[result.child_session_id])

    owner = _agent(
        ScriptedProvider([ToolUseTurn("spawn", {}), TextTurn("child"), TextTurn("done")]),
        tool_registry(StubTool("spawn", on_execute=spawn)),
        max_idle_sessions=bound,
    )
    parent = await owner.session()
    events = await _collect(parent, RunOptions(profile=Profile("empty", "1")))
    assert not any(e.type == "tool_call_end" and e.is_error for e in events)
    assert children
    if bound is None:
        fork = await owner.fork_session(children[0])
        assert fork.agent is owner
        assert fork._config_seed is None
        assert children[0].agent is owner
        await owner.release_session(children[0])
    else:
        assert all(child._lifecycle_state != "quiescing" for child in children)
    await owner.close()


async def test_profile_child_abort_after_record_creation_releases_owned_child():
    from linch.abort import AbortContext
    from linch.kernel import Disposable
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    cleaned = asyncio.Event()

    class Cleanup(_NamedPlugin):
        def setup(self, ctx, config=None):
            return Disposable(cleaned.set)

    owner = None
    parent = None

    async def spawn(raw, ctx):
        signal = AbortContext()
        store = owner._get_store()
        create = store.create

        async def aborted_create(*args, **kwargs):
            record = await create(*args, **kwargs)
            signal.abort()
            return record

        store.create = aborted_create
        try:
            await run_subagent(
                RunSubagentArgs(
                    parent_session=parent,
                    parent_agent=parent.agent,
                    definition=_child_definition(),
                    prompt="child",
                    display_name="child",
                    subagent_run_id="child",
                    retain=True,
                    signal=signal,
                )
            )
        finally:
            store.create = create

    owner = _agent(
        ScriptedProvider([ToolUseTurn("spawn", {}), TextTurn("done")]),
        tool_registry(StubTool("spawn", on_execute=spawn)),
    )
    parent = await owner.session()
    await _collect(
        parent,
        RunOptions(
            profile=Profile("empty", "1", plugins={"cleanup": (Cleanup("cleanup", "1"), {})})
        ),
    )
    assert list(owner._sessions) == [parent.id]
    assert cleaned.is_set()
    await owner.close()


async def test_cancelled_retained_foreground_profile_child_does_not_orphan():
    from linch.kernel import Disposable
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    started, cleaned = asyncio.Event(), asyncio.Event()

    class Cleanup(_NamedPlugin):
        def setup(self, ctx, config=None):
            return Disposable(cleaned.set)

    owner = None
    parent = None

    class HeldChild(ScriptedProvider):
        async def stream(self, req):
            if self.requests:
                started.set()
                await asyncio.Future()
            async for event in super().stream(req):
                yield event

    async def spawn(raw, ctx):
        await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=parent.agent,
                definition=_child_definition(),
                prompt="child",
                display_name="child",
                subagent_run_id="child",
                retain=True,
            )
        )

    owner = _agent(
        HeldChild([ToolUseTurn("spawn", {})]), tool_registry(StubTool("spawn", on_execute=spawn))
    )
    parent = await owner.session()
    profile = Profile("empty", "1", plugins={"cleanup": (Cleanup("cleanup", "1"), {})})
    task = asyncio.create_task(_collect(parent, RunOptions(profile=profile)))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(owner._sessions) == [parent.id]
    assert cleaned.is_set()
    assert parent._run_bindings == {}
    await owner.close()


async def test_run_binding_rejects_missing_and_foreign_ids():
    from linch.errors import ConfigError
    from linch.profiles import _effective_run_config, _profile_scope

    owner = _agent(ScriptedProvider([]), tool_registry())
    first, second = await owner.session(), await owner.session()
    async with _profile_scope(first, Profile("first", "1"), "first") as config:
        assert _effective_run_config(first, "first") is config
        with pytest.raises(ConfigError, match="missing or foreign"):
            _effective_run_config(first, "other")
        with pytest.raises(ConfigError, match="missing or foreign"):
            _effective_run_config(second, "first")
    assert first._run_bindings == {}
    await owner.close()


async def test_profile_setup_sees_reserved_binding_and_real_owner_before_publication():
    from linch.kernel import Disposable
    from linch.profiles import _effective_run_config

    observations = []
    owner = _agent(ScriptedProvider([TextTurn("done")]), tool_registry())
    session = await owner.session()

    class Inspect(_NamedPlugin):
        def setup(self, ctx, config=None):
            observations.append(
                (
                    ctx.agent,
                    session.active_run_id,
                    _effective_run_config(session, "reserved") is ctx.run_config,
                )
            )
            return Disposable()

    await _collect(
        session,
        RunOptions(
            run_id="reserved",
            profile=Profile("inspect", "1", plugins={"inspect": (Inspect("inspect", "1"), {})}),
        ),
    )
    assert observations == [(owner, None, True)]
    assert session._run_bindings == {}
    await owner.close()


async def test_base_configuration_tracks_replaced_agent_fields():
    owner = _agent(ScriptedProvider([]), tool_registry())
    base = owner.context.run_config
    owner.tools = tool_registry(StubTool("late"))
    owner.hooks = [object()]
    owner.system_prompt = "changed"
    assert base.tools is owner.tools
    assert base.hooks is owner.hooks
    assert base.system_prompt == "changed"
    await owner.close()


async def test_background_run_a_keeps_its_config_while_same_session_runs_b():
    from linch.kernel import Disposable

    reading, read_release, b_started, b_release = (asyncio.Event() for _ in range(4))
    cleaned = []
    seen = []

    class Service(_NamedPlugin):
        provides = ("profile.service",)

        def setup(self, ctx, config=None):
            ctx.register("profile.service", config["label"])
            return Disposable(lambda: cleaned.append(config["label"]))

    class HeldB(ScriptedProvider):
        async def stream(self, req):
            if len(self.requests) == 2:
                b_started.set()
                await b_release.wait()
            async for event in super().stream(req):
                yield event

    async def read(raw, ctx):
        reading.set()
        await read_release.wait()
        seen.append((ctx.run_id, ctx._run_config.context.get("profile.service")))

    provider = HeldB(
        [ToolUseTurn("read", {"run_in_background": True}), TextTurn("A"), TextTurn("B")]
    )
    owner = _agent(
        provider, tool_registry(StubTool("read", on_execute=read)), enable_background_tools=True
    )
    session = await owner.session()

    def options(label):
        return RunOptions(
            run_id=label,
            profile=Profile(
                label, "1", plugins={"service": (Service("service", "1"), {"label": label})}
            ),
        )

    await _collect(session, options("A"))
    await asyncio.wait_for(reading.wait(), 1)
    assert cleaned == []
    running_b = asyncio.create_task(_collect(session, options("B")))
    await asyncio.wait_for(b_started.wait(), 1)
    read_release.set()
    await asyncio.gather(*session.background_tasks)
    assert seen == [("A", "A")]
    assert cleaned == ["A"]
    assert "A" not in session._run_bindings
    assert "B" in session._run_bindings
    b_release.set()
    await running_b
    assert cleaned == ["A", "B"]
    assert session._run_bindings == {}
    await owner.close()


async def test_profile_cleanup_failure_keeps_retry_ownership_and_releases_admission():
    from linch.kernel import Disposable

    attempts = []

    class RetryCleanup(_NamedPlugin):
        def setup(self, ctx, config=None):
            def cleanup():
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("retry me")

            return Disposable(cleanup)

    owner = _agent(ScriptedProvider([TextTurn("first"), TextTurn("second")]), tool_registry())
    session = await owner.session()
    from linch._telemetry import telemetry_for

    telemetry_for(owner)
    baseline = len(owner.context._scope._disposers)
    profile = Profile("retry", "1", plugins={"retry": (RetryCleanup("retry", "1"), {})})
    with pytest.raises(RuntimeError, match="retry me"):
        await _collect(session, RunOptions(run_id="retry", profile=profile))
    assert not session._active
    assert session._idle.is_set()
    assert "retry" in session._run_bindings
    assert len(owner.context._scope._disposers) > baseline
    await _collect(session, RunOptions())
    await session.aclose()
    assert attempts == [1, 1]
    assert session._run_bindings == {}
    assert len(owner.context._scope._disposers) == baseline
    await owner.close()


async def test_background_binding_is_released_when_task_cancelled_before_start():
    from linch.kernel import Disposable
    from linch.profiles import _profile_scope, _schedule_with_binding

    cleaned = asyncio.Event()
    entered = []

    class Cleanup(_NamedPlugin):
        def setup(self, ctx, config=None):
            return Disposable(cleaned.set)

    owner = _agent(ScriptedProvider([]), tool_registry())
    session = await owner.session()
    profile = Profile("cancel", "1", plugins={"cleanup": (Cleanup("cleanup", "1"), {})})
    async with _profile_scope(session, profile, "cancel"):
        binding = session._run_bindings["cancel"]

        async def work():
            entered.append(True)

        inner = work()
        task = _schedule_with_binding(binding, inner)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert entered == []
        assert inner.cr_frame is None
    assert cleaned.is_set()
    assert session._run_bindings == {}
    await owner.close()


async def test_profile_event_emit_hook_uses_effective_hooks_and_real_owner():
    from linch.kernel import Disposable

    emitted = []

    class Hook:
        def on_event_emit(self, ctx):
            emitted.append((ctx.event.type, ctx.session.agent))

    class Hooks(_NamedPlugin):
        def setup(self, ctx, config=None):
            hook = Hook()
            hooks = ctx.run_config.hooks
            hooks.append(hook)
            return Disposable(lambda: hooks.remove(hook))

    owner = _agent(ScriptedProvider([TextTurn("profile"), TextTurn("plain")]), tool_registry())
    profile = Profile("hooks", "1", plugins={"hooks": (Hooks("hooks", "1"), {})})
    await _collect(await owner.session(), RunOptions(profile=profile))
    assert emitted and emitted[-1] == ("result", owner)
    count = len(emitted)
    await _collect(await owner.session(), RunOptions())
    assert len(emitted) == count
    await owner.close()


async def test_profile_setup_failure_rolls_back_effects_and_binding():
    from linch._telemetry import telemetry_for
    from linch.kernel import Disposable

    cleaned = []

    class Failing(_NamedPlugin):
        provides = ("partial",)

        def setup(self, ctx, config=None):
            ctx.register("partial", object())
            ctx.effect(lambda: Disposable(lambda: cleaned.append(True)))
            raise RuntimeError("setup failed")

    owner = _agent(ScriptedProvider([]), tool_registry())
    telemetry_for(owner)
    session = await owner.session()
    baseline = len(owner.context._scope._disposers)
    with pytest.raises(RuntimeError, match="setup failed"):
        await _collect(
            session,
            RunOptions(profile=Profile("fail", "1", plugins={"fail": (Failing("fail", "1"), {})})),
        )
    assert cleaned == [True]
    assert session.agent is owner and not session._active
    assert session._run_bindings == {}
    assert len(owner.context._scope._disposers) == baseline
    assert owner.context.get("partial") is None
    await owner.close()


async def test_profile_preserves_custom_context_adapter_override_signature():
    from linch.context import ContextBuildResult
    from linch.hooks.adapters import ContextInjectionHook
    from linch.types import SystemBlock

    class CustomContext(ContextInjectionHook):
        async def build_context(self, session, turn_index):
            return ContextBuildResult(system_blocks=[SystemBlock(text="custom context")])

    provider = ScriptedProvider([TextTurn("done")])
    owner = _agent(provider, tool_registry(), hooks=[CustomContext(None)])
    events = await _collect(await owner.session(), RunOptions(profile=Profile("empty", "1")))
    assert events[-1].subtype == "success"
    assert any(block.text == "custom context" for block in provider.requests[0].system)
    await owner.close()


@pytest.mark.parametrize("nested", [False, True])
async def test_retained_child_continuation_keeps_seed_services_and_restrictions(nested):
    from linch.context import ContextBuildResult
    from linch.hooks.adapters import ContextInjectionHook
    from linch.kernel import Disposable
    from linch.subagents.runner import (
        ContinueSubagentArgs,
        RunSubagentArgs,
        continue_subagent,
        run_subagent,
    )
    from linch.subagents.workers import WorkerHandle
    from linch.types import SystemBlock

    cleaned = []
    children = []
    parent = None

    class Builder:
        async def build(self, turn):
            value = turn._run_config.context.get("profile.service")
            return ContextBuildResult(system_blocks=[SystemBlock(text=f"service:{value}")])

    class Service(_NamedPlugin):
        provides = ("profile.service",)

        def setup(self, ctx, config=None):
            ctx.register("profile.service", "parent")
            hooks = ctx.run_config.hooks
            hook = ContextInjectionHook(Builder())
            hooks.append(hook)
            return Disposable(lambda: (hooks.remove(hook), cleaned.append(True)))

    class Extra(_NamedPlugin):
        def setup(self, ctx, config=None):
            return ctx.run_config.tools.register(StubTool("extra"))

    async def spawn(raw, ctx):
        result = await run_subagent(
            RunSubagentArgs(
                parent_session=parent,
                parent_agent=parent.agent,
                config_binding=parent._run_bindings[ctx.run_id],
                definition=_child_definition(),
                prompt="child",
                display_name="child",
                subagent_run_id="child",
                retain=True,
            )
        )
        children.append(owner._sessions[result.child_session_id])

    provider = ScriptedProvider(
        [ToolUseTurn("spawn", {}), TextTurn("child"), TextTurn("parent"), TextTurn("continued")]
    )
    owner = _agent(provider, tool_registry(StubTool("spawn", on_execute=spawn)))
    parent = await owner.session()
    profile = Profile("parent", "1", plugins={"service": (Service("service", "1"), {})})
    await _collect(parent, RunOptions(profile=profile))
    child = children[0]
    original = child._config_seed
    assert original.profile is profile
    assert cleaned == []
    handle = WorkerHandle("worker", child.id, "child", _child_definition())
    options = (
        RunOptions(profile=Profile("nested", "1", plugins={"extra": (Extra("extra", "1"), {})}))
        if nested
        else RunOptions()
    )
    result = await continue_subagent(
        ContinueSubagentArgs(parent, owner, handle, "continue", "continue", run_options=options)
    )
    assert not result.errored
    assert child._config_seed is original
    assert any(block.text == "service:parent" for block in provider.requests[-1].system)
    assert provider.requests[-1].tools == []
    assert cleaned == []
    await owner.release_session(child)
    assert cleaned == [True]
    assert parent._run_bindings == {}
    await owner.close()


async def test_failed_profile_teardown_is_retried_before_same_id_readmission():
    from linch.kernel import Disposable

    attempts = []

    class Retry(_NamedPlugin):
        def setup(self, ctx, config=None):
            def cleanup():
                attempts.append(True)
                if len(attempts) == 1:
                    raise RuntimeError("cleanup retry")

            return Disposable(cleanup)

    owner = _agent(ScriptedProvider([TextTurn("first"), TextTurn("second")]), tool_registry())
    session = await owner.session()
    options = RunOptions(
        run_id="same", profile=Profile("same", "1", plugins={"retry": (Retry("retry", "1"), {})})
    )
    with pytest.raises(RuntimeError, match="cleanup retry"):
        await _collect(session, options)
    events = await _collect(session, options)
    assert events[-1].subtype == "success"
    assert attempts == [True, True, True]
    assert session._run_bindings == {}
    await owner.close()


async def test_nested_profile_cleanup_retry_does_not_await_its_own_parent_release():
    from linch.kernel import Disposable
    from linch.profiles import _profile_scope

    attempts = []

    class Retry(_NamedPlugin):
        def setup(self, ctx, config=None):
            def cleanup():
                attempts.append(True)
                if len(attempts) == 1:
                    raise RuntimeError("nested cleanup")

            return Disposable(cleanup)

    owner = _agent(ScriptedProvider([]), tool_registry())
    parent = await owner.session()
    child = await owner.session()
    outer = Profile("outer", "1")
    inner = Profile("inner", "1", plugins={"retry": (Retry("retry", "1"), {})})
    async with _profile_scope(parent, outer, "outer"):
        child._config_seed = parent._run_bindings["outer"].retain()
        with pytest.raises(RuntimeError, match="nested cleanup"):
            async with _profile_scope(child, inner, "inner"):
                pass
    await asyncio.wait_for(child.aclose(), 1)
    assert attempts == [True, True]
    assert parent._run_bindings == child._run_bindings == {}
    await owner.close()


async def test_profile_reliability_validation_uses_restricted_session_catalog():
    from linch import ReliabilityPolicy, ToolPrerequisite

    owner = _agent(
        ScriptedProvider([TextTurn("done")]),
        tool_registry(StubTool("deploy"), StubTool("run_tests")),
        reliability=ReliabilityPolicy(prerequisites=(ToolPrerequisite("deploy", ("run_tests",)),)),
    )
    session = await owner.session()
    session.tools_override = owner.tools.copy()
    with pytest.raises(ValueError, match="unknown reliability tool name"):
        await _collect(
            session, RunOptions(profile=Profile("restricted", "1", tool_deny=("run_tests",)))
        )
    assert owner.provider.requests == []
    await owner.close()


async def test_ordinary_context_builder_sees_session_tool_override():
    from linch.context import ContextBuildResult
    from linch.hooks.adapters import ContextInjectionHook

    seen = []

    class Builder:
        async def build(self, turn):
            seen.append([tool.name for tool in turn.tools.list()])
            return ContextBuildResult()

    owner = _agent(
        ScriptedProvider([TextTurn("done")]),
        tool_registry(StubTool("host")),
        hooks=[ContextInjectionHook(Builder())],
    )
    session = await owner.session()
    session.tools_override = tool_registry(StubTool("limited"))
    await _collect(session, RunOptions())
    assert seen == [["limited"]]
    await owner.close()


async def test_live_run_config_builds_worker_catalog_prompt_contributions():
    from linch.config import SystemPromptSection
    from linch.profiles import _profile_scope

    tool = StubTool("worker_only")
    tool.system_prompt_sections = [SystemPromptSection(name="worker", text="worker instructions")]
    owner = _agent(ScriptedProvider([]), tool_registry())
    owner._subagent_tool_registry = tool_registry(tool)
    session = await owner.session()
    async with _profile_scope(session, None, "ordinary") as config:
        blocks = config.build_system_blocks_for_tool_names(["worker_only"])
        assert any(block.text == "worker instructions" for block in blocks)
    await owner.close()


async def test_inactive_request_and_contract_helpers_use_session_tool_override():
    from linch.loop.contracts import _build_run_contract
    from linch.loop.request import _build_turn_request

    owner = _agent(ScriptedProvider([]), tool_registry(StubTool("host")))
    session = await owner.session()
    session.tools_override = tool_registry(StubTool("limited"))
    request = _build_turn_request(session, RunOptions())
    contract = _build_run_contract(session, "go", RunOptions())
    assert [tool["name"] for tool in request.tools] == ["limited"]
    assert [tool["name"] for tool in contract.payload["tools"]] == ["limited"]
    assert session._run_bindings == {}
    await owner.close()


@pytest.mark.parametrize(
    "base,role", [(["SESSION BASE"], "PROFILE ROLE"), ([], "PROFILE ROLE"), ([], None)]
)
async def test_explicit_subagent_fork_preserves_effective_profile_system_override(base, role):
    from linch.profiles import _profile_scope
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.types import SystemBlock

    provider = ScriptedProvider([TextTurn("child")])
    owner = _agent(provider, tool_registry())
    parent = await owner.session()
    parent.system_blocks_override = [SystemBlock(text=text) for text in base]
    profile = Profile("fork", "1", system_prompt=role)
    async with _profile_scope(parent, profile, "parent") as config:
        expected = list(config.system_blocks_override)
        await run_subagent(
            RunSubagentArgs(
                parent,
                owner,
                _child_definition(),
                "child",
                "child",
                "child",
                config_binding=parent._run_bindings["parent"],
                fork=True,
            )
        )
    assert provider.requests[0].system == expected
    assert [block.text for block in provider.requests[0].system] == [
        *base,
        *([role] if role is not None else []),
    ]
    await owner.close()


@pytest.mark.parametrize("active_b", [False, True])
@pytest.mark.parametrize("fails", [False, True])
async def test_background_tool_hooks_keep_originating_profile_identity_and_deps(active_b, fails):
    from linch.kernel import Disposable
    from linch.profiles import _effective_run_config

    entered, tool_release, b_started, b_release = (asyncio.Event() for _ in range(4))
    seen_ids, seen_services, post_deps, failure_deps = [], [], [], []

    class Hook:
        def on_post_tool_use(self, ctx):
            post_deps.append(ctx.deps)

        def on_post_tool_use_failure(self, ctx):
            failure_deps.append(ctx.deps)
            seen_ids.append(ctx.run_id)
            seen_services.append(
                _effective_run_config(ctx.session, ctx.run_id).context.get("label")
            )

    class Service(_NamedPlugin):
        provides = ("label",)

        def setup(self, ctx, config=None):
            ctx.register("label", config["label"])
            hook, hooks = Hook(), ctx.run_config.hooks
            hooks.append(hook)
            return Disposable(lambda: hooks.remove(hook))

    class HeldB(ScriptedProvider):
        async def stream(self, req):
            if len(self.requests) == 2:
                b_started.set()
                await b_release.wait()
            async for event in super().stream(req):
                yield event

    async def fail(raw, ctx):
        entered.set()
        await tool_release.wait()
        if fails:
            raise ValueError("failure from A")

    provider = HeldB(
        [ToolUseTurn("fail", {"run_in_background": True}), TextTurn("A"), TextTurn("B")]
    )
    owner = _agent(
        provider, tool_registry(StubTool("fail", on_execute=fail)), enable_background_tools=True
    )
    session = await owner.session()

    def options(label):
        return RunOptions(
            run_id=label,
            deps={"label": label},
            profile=Profile(
                label, "1", plugins={"service": (Service("service", "1"), {"label": label})}
            ),
        )

    await _collect(session, options("A"))
    await entered.wait()
    running_b = None
    if active_b:
        running_b = asyncio.create_task(_collect(session, options("B")))
        await b_started.wait()
    tool_release.set()
    await asyncio.gather(*session.background_tasks)
    if running_b is not None:
        b_release.set()
        await running_b
    assert seen_ids == (["A"] if fails else [])
    assert seen_services == (["A"] if fails else [])
    assert post_deps == [{"label": "A"}]
    assert failure_deps == ([{"label": "A"}] if fails else [])
    assert session._run_bindings == {}
    await owner.close()
