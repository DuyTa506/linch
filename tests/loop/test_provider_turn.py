"""Regression tests for provider-turn state boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _Ledger:
    def assert_may_start(self) -> None:
        return None


def _preparation_context(
    *,
    pending_overlay: Any = None,
    resume_checkpoint: Any = None,
    force_final_pending: bool = False,
) -> Any:
    from linch.loop.provider_turn import ProviderPreparationContext

    checkpoint = resume_checkpoint or SimpleNamespace(
        phase="started",
        turn_index=0,
        assistant_message=None,
        current_turn_allowed_tools=None,
    )
    session = SimpleNamespace(
        pending_skill_overlay=pending_overlay,
        current_turn_allowed_tools=None,
        current_turn_permission_decisions={},
        active_model=None,
    )
    async def empty_events() -> Any:
        if False:
            yield None

    return ProviderPreparationContext(
        session=session,
        agent=SimpleNamespace(model="base-model"),
        config=SimpleNamespace(),
        opts=SimpleNamespace(),
        turn_index=0,
        run_id="run",
        signal=SimpleNamespace(),
        checkpoint=checkpoint,
        resume_checkpoint=resume_checkpoint,
        start_turn=0,
        exact_model_input=False,
        force_final_pending=force_final_pending,
        forced_final_turn=force_final_pending,
        recovered_compaction=False,
        recovered_primary_projection=None,
        hook_dispatcher_active=False,
        ledger=_Ledger(),
        budget=None,
        auxiliary_caller=SimpleNamespace(),
        primary_logical_call_id="primary",
        proactive_compaction_logical_call_id="proactive",
        forced_compaction_logical_call_id="forced",
        load_pending_provider_request=None,
        dispatch_before_provider_call=None,
        persist_event=None,
        drain_auxiliary_usage=empty_events,
        save_checkpoint=None,
        emit_budget_exhausted=None,
        maybe_save_provider_snapshot=None,
    )


@pytest.mark.asyncio
async def test_resumed_tool_results_keep_pending_skill_overlay(monkeypatch: Any) -> None:
    """A checkpointed skill overlay applies to the following resumed turn."""
    from linch.loop import provider_turn
    from linch.types import SkillOverlay

    checkpoint = SimpleNamespace(
        phase="tool_results_appended",
        turn_index=0,
        assistant_message=object(),
        current_turn_allowed_tools=None,
    )
    ctx = _preparation_context(
        pending_overlay=SkillOverlay(allowed_tools=["OnlyThis"], model_override="skill-model"),
        resume_checkpoint=checkpoint,
    )
    monkeypatch.setattr(provider_turn, "maybe_compact_resilient", _no_compaction)
    monkeypatch.setattr(provider_turn, "_build_context_result", _no_context)
    monkeypatch.setattr(
        provider_turn,
        "_build_turn_request",
        lambda *args, **kwargs: SimpleNamespace(tools=[], tool_choice=None),
    )

    [event async for event in provider_turn.prepare_provider_turn(ctx)]

    assert ctx.session.pending_skill_overlay is not None

    ctx.turn_index = 1
    [event async for event in provider_turn.prepare_provider_turn(ctx)]

    assert ctx.session.pending_skill_overlay is None
    assert ctx.session.current_turn_allowed_tools == ["OnlyThis"]
    assert ctx.outcome_model_override == "skill-model"


@pytest.mark.asyncio
async def test_force_final_turn_closes_execution_boundary(monkeypatch: Any) -> None:
    """A forced-final request also denies provider-emitted tool execution."""
    from linch.loop import provider_turn

    ctx = _preparation_context(force_final_pending=True)
    monkeypatch.setattr(provider_turn, "maybe_compact_resilient", _no_compaction)
    monkeypatch.setattr(provider_turn, "_build_context_result", _no_context)

    def build_request(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(tools=[{"name": "Write"}], tool_choice="auto")

    monkeypatch.setattr(provider_turn, "_build_turn_request", build_request)

    [event async for event in provider_turn.prepare_provider_turn(ctx)]

    assert ctx.session.current_turn_allowed_tools == []
    assert ctx.outcome_req is not None
    assert ctx.outcome_req.tools == []
    assert ctx.outcome_req.tool_choice is None


@pytest.mark.asyncio
async def test_force_final_turn_rejects_provider_tool_calls() -> None:
    """A provider that ignores the empty offer cannot execute a forced tool."""
    import json

    from linch.loop_guard import LoopGuard
    from tests.support.agents import make_agent
    from tests.support.tools import StubTool, tool_registry

    class IgnoringToolsProvider:
        id = "ignoring-tools"

        def __init__(self) -> None:
            self.requests: list[Any] = []
            self.calls = 0

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> Any:
            self.requests.append(req)
            self.calls += 1
            yield {"type": "message_start", "model": req.model}
            if self.calls > 2:
                yield {"type": "text_delta", "text": "done"}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": _usage()}
                return
            yield {"type": "tool_use_start", "id": f"call-{self.calls}", "name": "Write"}
            yield {
                "type": "tool_use_input_delta",
                "id": f"call-{self.calls}",
                "json_delta": json.dumps({"value": "x"}),
            }
            yield {"type": "tool_use_end", "id": f"call-{self.calls}"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": _usage()}

    executed: list[dict[str, Any]] = []
    provider = IgnoringToolsProvider()

    def record(input: dict[str, Any], _ctx: Any) -> None:
        executed.append(input)

    agent = make_agent(
        provider,
        tools=tool_registry(StubTool("Write", scope="write", on_execute=record)),
        permissions={"mode": "skip-dangerous"},
        loop_guard=LoopGuard(max_identical_tool_calls=1, force_final_answer=True),
        max_turns=3,
        run_store=None,
    )
    session = await agent.session()
    events = [event async for event in session.run("go")]

    assert provider.requests[1].tools == []
    assert len(executed) == 1
    second_end = [event for event in events if event.type == "tool_call_end"][1]
    assert second_end.is_error is True
    assert "not offered for the current turn" in second_end.result


async def _no_compaction(*args: Any, **kwargs: Any) -> bool:
    return False


async def _no_context(*args: Any, **kwargs: Any) -> None:
    return None


def _usage() -> Any:
    from linch.types import Usage

    return Usage()
