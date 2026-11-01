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


async def _no_compaction(*args: Any, **kwargs: Any) -> bool:
    return False


async def _no_context(*args: Any, **kwargs: Any) -> None:
    return None
