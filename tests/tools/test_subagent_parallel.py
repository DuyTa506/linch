from __future__ import annotations

import asyncio
import json
from typing import Any, cast


class _ReadTool:
    name = "Read"
    scope = "read"


class _WriteTool:
    name = "Write"
    scope = "write"


class _WildcardReadTool:
    name = "*"
    scope = "read"


class _Catalog:
    def __init__(self, tools: list[Any]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> Any | None:
        return self._tools.get(name)


class _SubagentParallelProvider:
    """Parent emits two subagent calls; children finish out of order."""

    id = "subagent-parallel-test"

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0
        self.active_children = 0
        self.max_active_children = 0
        self.child_finished: list[int] = []
        self.parent_result_ids: list[str] = []

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any):
        from linch.types import Usage

        self.calls += 1
        call_number = self.calls
        yield {"type": "message_start", "model": req.model}
        if call_number == 1:
            first_input = {
                "description": "first",
                "prompt": "first child",
            }
            second_input = {
                "description": "second",
                "prompt": "second child",
            }
            if self.mode == "reader":
                first_input["subagent_type"] = "reader"
                second_input["subagent_type"] = "reader"
            elif self.mode == "writer":
                first_input["subagent_type"] = "writer"
                second_input["subagent_type"] = "writer"
            for tool_id, tool_input in (("child-1", first_input), ("child-2", second_input)):
                yield {"type": "tool_use_start", "id": tool_id, "name": "Subagent"}
                yield {
                    "type": "tool_use_input_delta",
                    "id": tool_id,
                    "json_delta": json.dumps(tool_input),
                }
                yield {"type": "tool_use_end", "id": tool_id}
            stop_reason = "tool_use"
        elif call_number in (2, 3):
            self.active_children += 1
            self.max_active_children = max(self.max_active_children, self.active_children)
            # The second provider call finishes first, so the scheduler must
            # still emit child-1's result before child-2's result.
            await asyncio.sleep(0.05 if call_number == 2 else 0.01)
            self.child_finished.append(call_number)
            self.active_children -= 1
            yield {"type": "text_delta", "text": f"child-{call_number}"}
            stop_reason = "end_turn"
        else:
            from linch.types import ToolResultBlock

            self.parent_result_ids = [
                block.tool_use_id
                for message in req.messages
                for block in message.content
                if isinstance(block, ToolResultBlock)
            ]
            yield {"type": "text_delta", "text": "parent-done"}
            stop_reason = "end_turn"
        yield {
            "type": "message_end",
            "stop_reason": stop_reason,
            "usage": Usage(),
        }


async def _run_real_subagent_pair(
    tmp_path: Any, mode: str, *, session_write_override: bool = False
) -> tuple[Any, _SubagentParallelProvider]:
    from linch import Agent, workspace_tools
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore

    definition_tools = ["Read"] if mode == "reader" else ["Read", "Write"]
    definition = _definition(mode, definition_tools)
    provider = _SubagentParallelProvider(mode)
    agent = Agent(
        model="test-model",
        provider=cast(Any, provider),
        tools=workspace_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        features=FeatureFlags(subagents=True),
        extra_subagents=[definition],
        max_tool_concurrency=2,
    )
    try:
        session = await agent.session(id="parent")
        if session_write_override:
            from linch.tools.builtin import WriteTool

            class SameNameWriteTool(WriteTool):
                name = "Read"

            session.tools_override = agent.tools.copy()
            session.tools_override.replace(SameNameWriteTool())
        events = [event async for event in session.run("delegate two tasks")]
        return events, provider
    finally:
        await agent.close()


def _subagent_end_ids(events: list[Any]) -> list[str]:
    return [
        event.tool_use_id
        for event in events
        if getattr(event, "type", None) == "tool_call_end"
        and getattr(event, "tool_name", None) == "Subagent"
    ]


async def test_real_read_only_subagents_overlap_and_reassemble_provider_order(
    tmp_path: Any,
) -> None:
    events, provider = await _run_real_subagent_pair(tmp_path, "reader")

    assert provider.max_active_children == 2
    assert provider.child_finished == [3, 2]
    assert _subagent_end_ids(events) == ["child-1", "child-2"]
    assert provider.parent_result_ids == ["child-1", "child-2"]


async def test_real_general_purpose_subagents_serialize(tmp_path: Any) -> None:
    events, provider = await _run_real_subagent_pair(tmp_path, "general")

    assert provider.max_active_children == 1
    assert provider.child_finished == [2, 3]
    assert _subagent_end_ids(events) == ["child-1", "child-2"]


async def test_real_write_capable_typed_subagents_serialize(tmp_path: Any) -> None:
    events, provider = await _run_real_subagent_pair(tmp_path, "writer")

    assert provider.max_active_children == 1
    assert provider.child_finished == [2, 3]
    assert _subagent_end_ids(events) == ["child-1", "child-2"]


async def test_session_write_override_prevents_read_typed_subagent_parallelism(
    tmp_path: Any,
) -> None:
    events, provider = await _run_real_subagent_pair(
        tmp_path, "reader", session_write_override=True
    )

    assert provider.max_active_children == 1
    assert provider.child_finished == [2, 3]
    assert _subagent_end_ids(events) == ["child-1", "child-2"]


async def test_parallel_resolves_catalog_installed_after_subagent_connect(tmp_path: Any) -> None:
    from linch import Agent, empty_tools, workspace_tools
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore

    definition = _definition("reader", ["Read"])
    agent = Agent(
        model="test-model",
        provider=cast(Any, _SubagentParallelProvider("reader")),
        tools=empty_tools(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        features=FeatureFlags(subagents=True),
        extra_subagents=[definition],
    )
    try:
        await agent.connect_subagents()
        subagent = cast(Any, agent.tools.get("Subagent"))
        assert subagent.parallel({"subagent_type": "reader"}) is False

        # Coordinator mode installs the worker catalog after connect_subagents.
        # The callable must resolve this late catalog instead of freezing the
        # parent catalog captured during construction.
        agent._set_subagent_tool_registry(workspace_tools())
        assert subagent.parallel({"subagent_type": "reader"}) is True
    finally:
        await agent.close()


def test_parallel_fails_closed_when_registry_or_scope_raises() -> None:
    from linch.tools.subagent import SubagentTool

    class _RaisingRegistry:
        def get(self, _name: str) -> Any:
            raise RuntimeError("registry unavailable")

    class _RaisingScope:
        name = "Exploding"

        @property
        def scope(self) -> str:
            raise RuntimeError("scope unavailable")

    assert (
        SubagentTool(
            registry=_RaisingRegistry(),
            tool_registry=_Catalog([]),
            get_session=lambda _sid: None,
            next_default_display_name=lambda _sid: "Agent #1",
        ).parallel({"subagent_type": "reader"})
        is False
    )
    tool = _subagent_tool(_definition("reader", ["Exploding"]), tools=[_RaisingScope()])
    assert tool.parallel({"subagent_type": "reader"}) is False


def _definition(name: str, tools: list[str] | None) -> Any:
    from linch.subagents.types import AgentDefinition, AgentFrontmatter

    return AgentDefinition(
        name=name,
        file_path=f"<{name}>",
        source="built-in",
        frontmatter=AgentFrontmatter(name=name, description=name, tools=tools),
        body="",
    )


def _subagent_tool(*definitions: Any, tools: list[Any]) -> Any:
    from linch.subagents.registry import AgentRegistry
    from linch.tools.subagent import SubagentTool

    return SubagentTool(
        registry=AgentRegistry(list(definitions)),
        tool_registry=_Catalog(tools),
        get_session=lambda _sid: None,
        next_default_display_name=lambda _sid: "Agent #1",
    )


def _call(tool: Any, input: dict[str, object], idx: int) -> Any:
    from linch.scheduler import ResolvedCall
    from linch.types import ToolUseBlock

    return ResolvedCall(
        id=f"t{idx}",
        block=ToolUseBlock(id=f"t{idx}", name=tool.name, input=input),
        tool=tool,
        input=input,
        summary="",
        is_immediate_error=False,
    )


def test_typed_read_only_subagents_are_parallel_safe() -> None:
    from linch.permissions.engine import PermissionDecision
    from linch.scheduler import _partition_batches

    tool = _subagent_tool(_definition("researcher", ["Read"]), tools=[_ReadTool()])

    assert tool.parallel({"subagent_type": "researcher"}) is True
    resolved = [
        _call(tool, {"subagent_type": "researcher"}, 0),
        _call(tool, {"subagent_type": "researcher"}, 1),
    ]
    decisions = [PermissionDecision(decision="allow") for _ in resolved]
    batches = _partition_batches(resolved, decisions, max_concurrency=2)

    assert batches[0]["parallel"] is True
    assert [idx for _, idx, _ in batches[0]["calls"]] == [0, 1]


def test_general_purpose_subagents_serialize() -> None:
    from linch.permissions.engine import PermissionDecision
    from linch.scheduler import _partition_batches

    tool = _subagent_tool(_definition("researcher", ["Read"]), tools=[_ReadTool()])

    assert tool.parallel({}) is False
    assert tool.parallel({"subagent_type": "general-purpose"}) is False
    assert tool.parallel({"subagent_type": "_default"}) is False

    resolved = [_call(tool, {}, 0), _call(tool, {}, 1)]
    decisions = [PermissionDecision(decision="allow") for _ in resolved]
    batches = _partition_batches(resolved, decisions, max_concurrency=2)
    assert [(batch["parallel"], [idx for _, idx, _ in batch["calls"]]) for batch in batches] == [
        (False, [0]),
        (False, [1]),
    ]


def test_write_capable_typed_subagents_serialize() -> None:
    from linch.permissions.engine import PermissionDecision
    from linch.scheduler import _partition_batches

    tool = _subagent_tool(
        _definition("implementer", ["Read", "Write"]),
        tools=[_ReadTool(), _WriteTool()],
    )

    assert tool.parallel({"subagent_type": "implementer"}) is False
    resolved = [
        _call(tool, {"subagent_type": "implementer"}, 0),
        _call(tool, {"subagent_type": "implementer"}, 1),
    ]
    decisions = [PermissionDecision(decision="allow") for _ in resolved]
    batches = _partition_batches(resolved, decisions, max_concurrency=2)
    assert [(batch["parallel"], [idx for _, idx, _ in batch["calls"]]) for batch in batches] == [
        (False, [0]),
        (False, [1]),
    ]


def test_parallel_fails_closed_for_unknown_inherited_or_unresolvable_tools() -> None:
    from linch.subagents.registry import AgentRegistry
    from linch.tools.subagent import SubagentTool

    inherited = _subagent_tool(_definition("inherited", None), tools=[_ReadTool()])
    unknown = _subagent_tool(_definition("known", ["Missing"]), tools=[_ReadTool()])
    missing_registry = SubagentTool(
        registry=AgentRegistry([_definition("known", ["Read"])]),
        get_session=lambda _sid: None,
        next_default_display_name=lambda _sid: "Agent #1",
    )

    assert inherited.parallel({"subagent_type": "inherited"}) is False
    assert unknown.parallel({"subagent_type": "known"}) is False
    assert missing_registry.parallel({"subagent_type": "known"}) is False
    assert missing_registry.parallel({"subagent_type": "not-known"}) is False
    assert missing_registry.parallel({"subagent_type": "known", "run_in_background": True}) is False


def test_parallel_fails_closed_for_wildcard_empty_and_malformed_lists() -> None:
    wildcard = _subagent_tool(_definition("wildcard", ["*"]), tools=[_WildcardReadTool()])
    empty = _subagent_tool(_definition("empty", []), tools=[_ReadTool()])
    malformed_tools = cast(Any, ["Read", None])
    malformed = _subagent_tool(_definition("malformed", malformed_tools), tools=[_ReadTool()])
    reader = _subagent_tool(_definition("reader", ["Read"]), tools=[_ReadTool()])

    assert wildcard.parallel({"subagent_type": "wildcard"}) is False
    assert empty.parallel({"subagent_type": "empty"}) is True
    assert malformed.parallel({"subagent_type": "malformed"}) is False
    assert reader.parallel({"subagent_type": "reader", "run_in_background": True}) is False
