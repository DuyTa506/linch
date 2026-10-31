from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest


class _EchoTool:
    scope = "read"
    parallel = False
    input_schema = {"type": "object", "properties": {"value": {"type": "string"}}}

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"Echoes values for {name}."
        self.seen: list[dict[str, Any]] = []

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def summarize(self, raw: dict[str, Any]) -> str:
        return f"{self.name}({raw.get('value', '')})"

    async def execute(self, raw: dict[str, Any], ctx: Any) -> Any:
        from linch import ToolResult

        self.seen.append(dict(raw))
        return ToolResult(content=f"{self.name}:{raw.get('value', '')}")


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


class _ConfiguredTool(_EchoTool):
    def __init__(self, label: str) -> None:
        super().__init__("ProfileTool")
        self.label = label

    async def execute(self, raw: dict[str, Any], ctx: Any) -> Any:
        from linch import ToolResult

        self.seen.append(dict(raw))
        return ToolResult(content=f"tool:{self.label}")


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
        tool_disposer = ctx.agent.tools.replace(tool)
        wrapper_disposer = ctx.agent.tool_pipeline.on_execute(_ConfiguredWrapper(label))

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


class _CaptureProvider:
    """Small provider that records requests while emitting scripted turns."""

    id = "profile-capture"

    def __init__(self, turns: list[tuple[str, str]], *, barrier: bool = False) -> None:
        self._turns = list(turns)
        self._index = 0
        self.requests: list[Any] = []
        self.barrier = barrier
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()
        self._active = 0
        self.max_active = 0

    def context_window(self, model: str) -> int:
        return 128_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, Any]]:
        from linch.types import Usage

        self.requests.append(req)
        if self._index >= len(self._turns):
            raise AssertionError("profile provider ran out of turns")
        kind, value = self._turns[self._index]
        self._index += 1
        if self.barrier:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active >= 2:
                self.both_started.set()
            await self.both_started.wait()
            await self.release.wait()
        try:
            yield {"type": "message_start", "model": req.model}
            if kind == "text":
                yield {"type": "text_delta", "text": value}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
                return
            yield {
                "type": "tool_use_start",
                "id": f"profile-call-{self._index}",
                "name": value,
            }
            yield {
                "type": "tool_use_input_delta",
                "id": f"profile-call-{self._index}",
                "json_delta": '{"value":"profile"}',
            }
            yield {"type": "tool_use_end", "id": f"profile-call-{self._index}"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
        finally:
            if self.barrier:
                self._active -= 1


def _agent(provider: Any, tools: Any, *, run_store: Any = None, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore

    permissions = kwargs.pop("permissions", {"mode": "skip-dangerous"})
    return Agent(
        model="profile-model",
        provider=provider,
        tools=tools,
        permissions=permissions,
        session_store=InMemorySessionStore(),
        run_store=run_store,
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        **kwargs,
    )


def test_profile_layers_replace_rows_and_resolve_without_live_objects() -> None:
    from linch import Profile

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
    from linch import Profile, RunOptions
    from linch.tools import ToolRegistry

    support = _EchoTool("SupportSearch")
    billing = _EchoTool("BillingLookup")
    tools = ToolRegistry()
    tools.register(support)
    tools.register(billing)
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
    from linch import Profile, RunOptions
    from linch.tools import ToolRegistry

    support = _EchoTool("SupportSearch")
    tools = ToolRegistry()
    tools.register(support)
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
    from linch import Profile, RunOptions, ToolCallEndEvent
    from linch.permissions import ToolRule
    from linch.tools import ToolRegistry

    tool = _EchoTool("SupportSearch")
    tools = ToolRegistry()
    tools.register(tool)
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
    from linch import LoopGuardEvent, Profile, ResultEvent, RunOptions
    from linch.tools import ToolRegistry

    tool = _EchoTool("SupportSearch")
    tools = ToolRegistry()
    tools.register(tool)
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
    from linch import Profile, RunOptions
    from linch.errors import ConfigError
    from linch.run_store import InMemoryRunStore
    from linch.tools import ToolRegistry

    store = InMemoryRunStore()
    provider = _CaptureProvider([("text", "done")])
    agent = _agent(provider, ToolRegistry(), run_store=store)
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

    provider._turns = [("text", "must not dispatch")]
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
    from linch import Profile, RunBudget, RunOptions
    from linch.tools import ToolRegistry

    provider = _CaptureProvider([("text", "one"), ("text", "two")])
    agent = _agent(provider, ToolRegistry())
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
    from linch import Profile, RunOptions
    from linch.run_store import InMemoryRunStore
    from linch.tools import ToolRegistry

    store = InMemoryRunStore()
    profile = Profile(id="support", version="1", system_prompt="support")
    first_provider = _CaptureProvider([("text", "interrupted")])
    first = _agent(first_provider, ToolRegistry(), run_store=store)
    session = await first.session(id="s")
    iterator = session.run("go", RunOptions(profile=profile)).__aiter__()
    first_event = await iterator.__anext__()
    run_id = first_event.run_id
    await iterator.aclose()

    second_store = copy.copy(store)
    for name in ("_held_leases", "_lease_ttls", "_lease_owners"):
        if hasattr(store, name):
            setattr(second_store, name, dict(getattr(store, name)))
    await store.claim(run_id, "worker-a", 0.01)
    await asyncio.sleep(0.02)
    await second_store.claim(run_id, "worker-b", 60.0)

    second_provider = _CaptureProvider([("text", "resumed")])
    second = _agent(second_provider, ToolRegistry(), run_store=second_store)
    resumed = await second.session(id="s")
    events = [event async for event in resumed.resume(run_id, RunOptions(profile=profile))]
    assert events[-1].final_text == "resumed"
    assert second_provider._index == 1


@pytest.mark.asyncio
async def test_profile_plugins_receive_config_and_are_cleaned_up_per_run() -> None:
    from linch import Profile, RunOptions
    from linch.kernel import Disposable
    from linch.tools import ToolRegistry

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
    agent = _agent(provider, ToolRegistry())
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
    from linch import Profile, RunOptions
    from linch.kernel import Disposable
    from linch.tools import ToolRegistry

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
    agent = _agent(provider, ToolRegistry())
    session = await agent.session()
    iterator = session.run("go", RunOptions(profile=profile)).__aiter__()
    await iterator.__anext__()
    await iterator.aclose()

    assert cleaned == ["cleanup"]
    assert agent.context.get("profile_marker") is None


@pytest.mark.asyncio
async def test_profile_plugin_scope_adds_tool_pipeline_wrapper_and_context_listener() -> None:
    from linch import Profile, RunOptions, ToolCallEndEvent
    from linch.tools import ToolRegistry

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
    agent = _agent(provider, ToolRegistry())
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
    from linch import RunOptions
    from linch.loop.contracts import _build_run_contract
    from linch.run_store import run_contract_to_dict
    from linch.tools import ToolRegistry

    agent = _agent(_CaptureProvider([]), ToolRegistry())
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
    from linch import Profile

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
    from linch import Profile
    from linch.errors import ConfigError

    with pytest.raises(ConfigError, match=message):
        Profile(**kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_retained_child_keeps_profile_service_after_parent_scope_ends(
    background: bool,
) -> None:
    from linch import Profile, RunOptions
    from linch.profiles import _profile_scope
    from linch.run_store import InMemoryRunStore
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from linch.subagents.types import AgentDefinition, AgentFrontmatter
    from linch.subagents.workers import WorkerHandle
    from linch.tools import ToolRegistry

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
        ToolRegistry(),
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
    async with _profile_scope(session, profile):
        args = RunSubagentArgs(
            parent_session=session,
            parent_agent=session.agent,
            definition=definition,
            prompt="child prompt",
            display_name="retained",
            subagent_run_id="retained-run",
            run_options=RunOptions(),
            retain=True,
        )
        if background:
            task = asyncio.create_task(run_subagent(args))
            session.workers["worker"] = WorkerHandle(
                "worker", "pending", "retained", definition, task=task
            )
        else:
            result = await run_subagent(args)

    if background:
        result = await task
    child = agent._sessions[result.child_session_id]
    assert child.agent.context.get("profile.service") == {"tenant": "acme"}
    releasing = asyncio.create_task(agent.release_session(child.id))
    await asyncio.wait_for(cleanup_started.wait(), 1)
    waited_for_cleanup = not releasing.done()
    cleanup_release.set()
    await releasing
    await asyncio.wait_for(cleaned.wait(), 1)
    assert waited_for_cleanup
    assert child.agent.context.get("profile.service") is None
    await agent.close()


async def _collect(session: Any, opts: Any) -> list[Any]:
    return [event async for event in session.run("go", opts)]


@pytest.mark.asyncio
async def test_profile_plugin_tracks_removal_of_inherited_requirement() -> None:
    from linch import Profile
    from linch.profiles import _profile_scope
    from linch.tools import ToolRegistry

    class DependentPlugin:
        name = "dependent"
        version = "1"
        provides = ("dependent",)
        requires = ("dependency",)

        def setup(self, ctx: Any, config: Any = None) -> Any:
            return ctx.register("dependent", "active")

    agent = _agent(_CaptureProvider([]), ToolRegistry())
    dependency = agent.context.register("dependency", object())
    session = await agent.session()
    profile = Profile(id="support", version="1", plugins={"dep": (DependentPlugin(), {})})
    async with _profile_scope(session, profile):
        assert session.agent.context.get("dependent") == "active"
        await dependency.dispose()
        assert session.agent.context.get("dependent") is None
    await agent.close()


@pytest.mark.asyncio
async def test_profile_filter_preserves_host_ruleset_path_root() -> None:
    from linch import Profile, empty_tools
    from linch.permissions import PathRule, PermissionLayer, PermissionRuleSet
    from linch.permissions.engine import PendingToolCall
    from linch.profiles import _profile_scope

    write = _EchoTool("Write")
    agent = _agent(_CaptureProvider([]), empty_tools(write, _EchoTool("Other")))
    agent.permission_engine.rule_set = PermissionRuleSet(
        [PermissionLayer("host", [PathRule(["secret/**"], "deny")])],
        project_root="/tenant-policy",
    )
    call = PendingToolCall("call", write, {"file_path": "/tenant-policy/secret/key"})
    assert agent.permission_engine.evaluate(call).decision == "deny"
    session = await agent.session()
    async with _profile_scope(session, Profile(id="support", version="1", tool_allow=("Write",))):
        assert session.agent.permission_engine.evaluate(call).decision == "deny"
    await agent.close()


@pytest.mark.asyncio
async def test_profile_scope_disposes_after_last_background_task_finishes() -> None:
    from linch import Profile, empty_tools
    from linch.kernel import Disposable
    from linch.profiles import _profile_scope

    cleaned, release = asyncio.Event(), asyncio.Event()

    class CleanupPlugin(_NamedPlugin):
        def setup(self, ctx: Any, config: Any = None) -> Any:
            return Disposable(cleaned.set)

    agent = _agent(_CaptureProvider([]), empty_tools())
    session = await agent.session()
    profile = Profile(id="support", version="1", plugins={"cleanup": (CleanupPlugin("c", "1"), {})})
    async with _profile_scope(session, profile):
        tasks = [asyncio.create_task(release.wait()) for _ in range(2)]
        session.background_tasks.extend(tasks)
    assert not cleaned.is_set()
    release.set()
    await asyncio.gather(*tasks)
    await asyncio.wait_for(cleaned.wait(), 1)
    assert session.agent is agent
    await agent.close()


@pytest.mark.asyncio
async def test_profile_plugin_listener_does_not_affect_concurrent_plain_run() -> None:
    from linch import Profile, RunOptions, ToolCallEndEvent, empty_tools

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
