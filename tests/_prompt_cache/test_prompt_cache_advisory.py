from __future__ import annotations

import json
from typing import Any

import pytest

from linch import ContextBuildResult, RunOptions
from linch._prompt_cache import prompt_cache_advisories, tool_signature
from linch.events import PromptCacheAdvisoryEvent, event_from_dict, event_to_dict
from linch.ext.reports import build_run_report
from linch.hooks import ContextInjectionHook
from linch.loop.request import _build_turn_request
from linch.providers.base import ProviderCapabilities
from linch.types import SystemBlock
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry


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


@pytest.mark.parametrize(
    "has_baseline",
    [False, True],
    ids=["no-baseline", "unchanged"],
)
def test_unchanged_prefix_emits_no_advisory(has_baseline: bool) -> None:
    names = ("Read", "Write") if has_baseline else ("Read",)
    signature = tool_signature(_tools(*names))
    assert (
        prompt_cache_advisories(
            prev_model="m" if has_baseline else None,
            prev_signature=signature if has_baseline else None,
            model="m",
            signature=signature,
        )
        == []
    )


@pytest.mark.parametrize(
    ("previous", "current", "prev_model", "model", "expected"),
    [
        (("Read", "Write"), ("Read",), "m", "m", ["tool_set_changed"]),
        (("Read",), ("Read",), "old", "new", ["model_changed"]),
        (("Read",), ("Read", "Write"), "old", "new", ["model_changed", "tool_set_changed"]),
    ],
    ids=["tool-set", "model", "both"],
)
def test_advisory_reason_changes(previous, current, prev_model, model, expected) -> None:
    prev = tool_signature(_tools(*previous))
    cur = tool_signature(_tools(*current))
    advisories = prompt_cache_advisories(
        prev_model=prev_model, prev_signature=prev, model=model, signature=cur
    )
    assert [reason for reason, _ in advisories] == expected
    if expected == ["tool_set_changed"]:
        assert "removed=['Write']" in advisories[0][1]
    elif expected == ["model_changed"]:
        assert "old" in advisories[0][1] and "new" in advisories[0][1]
    else:
        assert {reason for reason, _ in advisories} == {"model_changed", "tool_set_changed"}


def test_advisory_event_round_trips() -> None:
    event = PromptCacheAdvisoryEvent(reason="tool_set_changed", detail="tools changed")
    restored = event_from_dict(event_to_dict(event))
    assert isinstance(restored, PromptCacheAdvisoryEvent)
    assert restored.reason == "tool_set_changed"
    assert restored.detail == "tools changed"


def test_advisory_event_from_dict_rejects_unknown_reason() -> None:
    with pytest.raises(ValueError, match="unknown reason"):
        event_from_dict({"type": "prompt_cache_advisory", "reason": "???", "detail": "d"})


def test_equivalent_name_sets_select_identical_schema_bytes() -> None:
    registry = tool_registry(*(StubTool(name) for name in ("Read", "Write", "Edit", "Bash")))

    a = registry.select(names={"Read", "Bash"})
    b = registry.select(names={"Bash", "Read"})
    assert json.dumps(a.schemas()) == json.dumps(b.schemas())
    assert [s["name"] for s in a.schemas()] == ["Read", "Bash"]


class _PromptCacheProvider(ScriptedProvider):
    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(context_window=self.context_window(model), prompt_cache=True)


async def test_context_system_blocks_forced_non_cacheable() -> None:
    agent = make_agent(
        _PromptCacheProvider([]),
        model="dummy",
        run_store=None,
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


class _RotatingTools:
    async def build(self, turn: Any) -> ContextBuildResult:
        name = "LookupA" if turn.turn_index % 2 == 0 else "LookupB"
        return ContextBuildResult(selected_tools={name})


async def _run_and_collect(context_builder: Any) -> list[Any]:
    hooks = [ContextInjectionHook(context_builder)] if context_builder is not None else []
    agent = make_agent(
        _PromptCacheProvider([ToolUseTurn("LookupA", {"q": "x"}), TextTurn("done")]),
        model="probe",
        run_store=None,
        tools=tool_registry(
            StubTool("LookupA", output="LookupA:ok", parallel=True),
            StubTool("LookupB", output="LookupB:ok", parallel=True),
        ),
        hooks=hooks,
        result_offload=None,
    )
    session = await agent.session()
    events = [event async for event in session.run("look things up then answer")]
    await agent.close()
    return events


@pytest.mark.parametrize("rotating", [True, False], ids=["rotating", "stable"])
async def test_selected_tools_advisories(rotating: bool) -> None:
    events = await _run_and_collect(_RotatingTools() if rotating else None)
    advisories = [e for e in events if isinstance(e, PromptCacheAdvisoryEvent)]
    if rotating:
        assert advisories, "rotating selected_tools should trip a prompt-cache advisory"
        assert all(a.reason == "tool_set_changed" for a in advisories)
    else:
        assert advisories == []


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
