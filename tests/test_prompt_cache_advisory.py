"""Prompt-cache prefix-stability hardening + advisories.

Providers cache the leading identical bytes of each request (tools → system →
messages). This slice keeps that prefix stable and surfaces a
``PromptCacheAdvisoryEvent`` when the embedder breaks it. These tests pin:

- ephemeral context system blocks never reach the wire as ``cacheable=True``,
- equivalent tool name-sets select in a stable, byte-identical order,
- the fingerprint/advisory helpers detect tool-set and model churn (and nothing
  else), and round-trip through the event serde,
- the loop actually emits the advisory when ``selected_tools`` rotates, and
  stays silent when tools are stable,
- the run report folds advisories into a ``prompt_cache`` diagnostics block.
"""

from __future__ import annotations

import json
from typing import Any

from linch import Agent, ContextBuildResult
from linch._prompt_cache import prompt_cache_advisories, tool_signature
from linch.events import PromptCacheAdvisoryEvent, event_from_dict, event_to_dict
from linch.hooks import ContextInjectionHook
from linch.loop.request import _build_turn_request
from linch.providers.base import BaseProvider, ProviderCapabilities
from linch.reports import build_run_report
from linch.session import RunOptions
from linch.sessions import InMemorySessionStore
from linch.tools import ToolContext, ToolRegistry, ToolResult
from linch.types import ProviderRequest, SystemBlock, Usage

# ── Tool signature + advisory helpers ────────────────────────────────────────


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"name": n, "description": n, "input_schema": {"type": "object"}} for n in names]


def test_tool_signature_is_order_and_schema_sensitive() -> None:
    a = tool_signature(_tools("Read", "Write"))
    assert a == tool_signature(_tools("Read", "Write"))  # stable
    assert a != tool_signature(_tools("Write", "Read"))  # order matters
    assert a != tool_signature(_tools("Read"))  # membership matters
    edited = _tools("Read", "Write")
    edited[0]["input_schema"] = {"type": "object", "properties": {"x": {}}}
    assert a != tool_signature(edited)  # schema edit matters


def test_no_advisory_without_a_baseline() -> None:
    sig = tool_signature(_tools("Read"))
    assert (
        prompt_cache_advisories(prev_model=None, prev_signature=None, model="m", signature=sig)
        == []
    )


def test_no_advisory_when_prefix_stable() -> None:
    sig = tool_signature(_tools("Read", "Write"))
    assert (
        prompt_cache_advisories(prev_model="m", prev_signature=sig, model="m", signature=sig) == []
    )


def test_tool_set_change_emits_tool_advisory() -> None:
    prev = tool_signature(_tools("Read", "Write"))
    cur = tool_signature(_tools("Read"))
    advisories = prompt_cache_advisories(
        prev_model="m", prev_signature=prev, model="m", signature=cur
    )
    assert [reason for reason, _ in advisories] == ["tool_set_changed"]
    assert "removed=['Write']" in advisories[0][1]


def test_model_change_emits_model_advisory() -> None:
    sig = tool_signature(_tools("Read"))
    advisories = prompt_cache_advisories(
        prev_model="old", prev_signature=sig, model="new", signature=sig
    )
    assert [reason for reason, _ in advisories] == ["model_changed"]
    assert "old" in advisories[0][1] and "new" in advisories[0][1]


def test_both_changes_emit_both_advisories() -> None:
    prev = tool_signature(_tools("Read"))
    cur = tool_signature(_tools("Read", "Write"))
    reasons = {
        reason
        for reason, _ in prompt_cache_advisories(
            prev_model="old", prev_signature=prev, model="new", signature=cur
        )
    }
    assert reasons == {"model_changed", "tool_set_changed"}


# ── Event serde ──────────────────────────────────────────────────────────────


def test_advisory_event_round_trips() -> None:
    event = PromptCacheAdvisoryEvent(reason="tool_set_changed", detail="tools changed")
    restored = event_from_dict(event_to_dict(event))
    assert isinstance(restored, PromptCacheAdvisoryEvent)
    assert restored.reason == "tool_set_changed"
    assert restored.detail == "tools changed"


def test_advisory_event_from_dict_defaults_unknown_reason() -> None:
    restored = event_from_dict({"type": "prompt_cache_advisory", "reason": "???", "detail": "d"})
    assert isinstance(restored, PromptCacheAdvisoryEvent)
    assert restored.reason == "tool_set_changed"


# ── Stable tool selection ────────────────────────────────────────────────────


def test_equivalent_name_sets_select_identical_schema_bytes() -> None:
    registry = ToolRegistry()
    for name in ("Read", "Write", "Edit", "Bash"):
        registry.register(_NamedTool(name))

    a = registry.select(names={"Read", "Bash"})
    b = registry.select(names={"Bash", "Read"})
    assert json.dumps(a.schemas()) == json.dumps(b.schemas())
    # Order follows the parent registry insertion order, not the set iteration.
    assert [s["name"] for s in a.schemas()] == ["Read", "Bash"]


# ── Ephemeral system blocks forced out of the cached prefix ──────────────────


class _DummyProvider(BaseProvider):
    id = "dummy"

    async def stream(self, req: ProviderRequest):  # pragma: no cover - not streamed here
        if False:
            yield {}

    def context_window(self, model: str) -> int:
        return 100_000

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(context_window=100_000, prompt_cache=True)


async def test_context_system_blocks_forced_non_cacheable() -> None:
    agent = Agent(
        model="dummy",
        provider=_DummyProvider(),
        session_store=InMemorySessionStore(),
        system_prompt="static system",
        result_offload=None,
    )
    session = await agent.session()
    context = ContextBuildResult(
        system_blocks=[
            SystemBlock(text="volatile RAG snippet", cacheable=True),
            SystemBlock(text="also volatile", cacheable=False),
        ]
    )
    req = _build_turn_request(session, RunOptions(), context=context)

    ephemeral = [b for b in req.system if "volatile" in b.text or "also volatile" in b.text]
    assert ephemeral, "context blocks should be present on the request"
    assert all(b.cacheable is False for b in ephemeral)
    await agent.close()


# ── End-to-end advisory emission through the loop ────────────────────────────


class _NamedTool:
    scope = "read"
    parallel = True
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "lookup"

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return self.name

    async def execute(self, input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(content=f"{self.name}:ok")


class _ToolThenTextProvider(BaseProvider):
    id = "advisory-probe"

    def __init__(self) -> None:
        self.calls = 0

    def context_window(self, model: str) -> int:
        return 128_000

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(context_window=128_000, prompt_cache=True)

    async def stream(self, req: ProviderRequest):
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if self.calls == 1:
            name = str((req.tools[0] if req.tools else {"name": "LookupA"})["name"])
            yield {"type": "tool_use_start", "id": "c1", "name": name}
            yield {"type": "tool_use_input_delta", "id": "c1", "json_delta": json.dumps({"q": "x"})}
            yield {"type": "tool_use_end", "id": "c1"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}
            return
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


class _RotatingTools:
    async def build(self, turn: Any) -> ContextBuildResult:
        name = "LookupA" if turn.turn_index % 2 == 0 else "LookupB"
        return ContextBuildResult(selected_tools={name})


def _lookup_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_NamedTool("LookupA"))
    registry.register(_NamedTool("LookupB"))
    return registry


async def _run_and_collect(context_builder: Any) -> list[Any]:
    hooks = [ContextInjectionHook(context_builder)] if context_builder is not None else []
    agent = Agent(
        model="probe",
        provider=_ToolThenTextProvider(),
        tools=_lookup_registry(),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        hooks=hooks,
        result_offload=None,
    )
    session = await agent.session()
    events = [event async for event in session.run("look things up then answer")]
    await agent.close()
    return events


async def test_rotating_selected_tools_emits_advisory() -> None:
    events = await _run_and_collect(_RotatingTools())
    advisories = [e for e in events if isinstance(e, PromptCacheAdvisoryEvent)]
    assert advisories, "rotating selected_tools should trip a prompt-cache advisory"
    assert all(a.reason == "tool_set_changed" for a in advisories)


async def test_stable_tools_emit_no_advisory() -> None:
    events = await _run_and_collect(None)
    assert [e for e in events if isinstance(e, PromptCacheAdvisoryEvent)] == []


# ── Report diagnostics ───────────────────────────────────────────────────────


def test_report_folds_advisories_into_prompt_cache_block() -> None:
    events = [
        PromptCacheAdvisoryEvent(reason="tool_set_changed", detail="a"),
        PromptCacheAdvisoryEvent(reason="tool_set_changed", detail="b"),
        PromptCacheAdvisoryEvent(reason="model_changed", detail="c"),
    ]
    report = build_run_report(events)
    block = report.summary["prompt_cache"]
    assert block["advisory_count"] == 3
    assert block["reasons"] == {"model_changed": 1, "tool_set_changed": 2}
    assert block["tool_selection_changes"] == 2
    assert "prompt cache advisories: 3 (tool changes=2)" in report.to_markdown()
