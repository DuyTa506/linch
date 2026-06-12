"""Terminal event tails and closed-loop gates.

Every way a run can end (success, error, budget, max-turns, stop_when) has a
tail helper here that emits + persists the closing event sequence, plus the
verification gates evaluated on a would-be-final answer."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from ..events import (
    BudgetEvent,
    ErrorEvent,
    Event,
    LoopGuardEvent,
    ResultEvent,
    UserEvent,
    VerificationEvent,
)
from ..session import Session
from ..types import (
    ContentBlock,
    Message,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .checkpoint import _persist_event
from .request import final_text


def _parse_structured_output(text: str, schema: object) -> tuple[dict | None, str | None]:
    """Try to parse *text* as JSON and optionally validate against *schema*.

    Returns ``(parsed_dict, None)`` on success or ``(None, error_message)``
    on failure.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"JSON parse error: {exc}"

    if not isinstance(parsed, dict):
        return None, f"Expected a JSON object, got {type(parsed).__name__}"

    validation_error = _validate_structured_output(parsed, schema)
    if validation_error is not None:
        return None, validation_error

    return parsed, None


def _validate_structured_output(value: dict[str, Any], schema: object) -> str | None:
    """Validate a parsed structured-output object against *schema*."""
    try:
        import jsonschema  # type: ignore[import]

        schema_dict = getattr(schema, "schema", None)
        if schema_dict:
            jsonschema.validate(value, schema_dict)
    except ImportError:
        return None  # jsonschema not installed — skip validation
    except Exception as exc:
        return f"Schema validation error: {exc}"
    return None


def _last_assistant_text(session: Session) -> str | None:
    """Last non-empty assistant text in the provider view, if any."""
    for message in reversed(session.provider_view):
        if message.role == "assistant":
            text = final_text(message)
            if text:
                return text
    return None


async def _budget_exhausted_tail(
    session: Session,
    agent: Any,
    *,
    run_id: str,
    run_record: Any,
    checkpoint: Any,
    budget: Any,
    total: Usage,
    duration_ms: int,
    running_cost: float | None,
) -> AsyncIterator[Event]:
    """Emit the graceful-stop event sequence for an exhausted RunBudget."""
    event: Event = BudgetEvent(
        kind="exceeded",
        spent_tokens=budget.spent_tokens,
        spent_usd=budget.spent_usd,
        max_tokens=budget.max_tokens,
        max_cost_usd=budget.max_cost_usd,
    )
    await _persist_event(session, run_id, event)
    yield event
    budget_error = {
        "name": "BudgetExceededError",
        "message": (
            f"run budget exhausted ({budget.spent_tokens} tokens, ${budget.spent_usd:.4f} spent)"
        ),
        "retryable": False,
    }
    event = ErrorEvent(error=budget_error)
    await _persist_event(session, run_id, event)
    yield event
    event = ResultEvent(
        subtype="error",
        stop_reason="error",
        total_usage=total,
        duration_ms=duration_ms,
        total_cost_usd=running_cost,
    )
    await _persist_event(session, run_id, event)
    store = agent.run_store
    if run_record is not None and store is not None:
        checkpoint.total_usage = total
        await store.mark_failed(run_id, checkpoint, error=budget_error)
    yield event


async def _stop_when_tail(
    session: Session,
    agent: Any,
    *,
    run_id: str,
    run_record: Any,
    checkpoint: Any,
    total: Usage,
    duration_ms: int,
    running_cost: float | None,
) -> AsyncIterator[Event]:
    """Emit the graceful-stop event sequence for a met stop_when predicate."""
    event: Event = ResultEvent(
        subtype="success",
        stop_reason="end_turn",
        total_usage=total,
        duration_ms=duration_ms,
        final_text=_last_assistant_text(session),
        total_cost_usd=running_cost,
    )
    await _persist_event(session, run_id, event)
    store = agent.run_store
    if run_record is not None and store is not None:
        checkpoint.total_usage = total
        await store.mark_completed(run_id, checkpoint)
    yield event


async def _error_result_tail(
    session: Session,
    agent: Any,
    *,
    run_id: str,
    run_record: Any,
    checkpoint: Any,
    total: Usage,
    duration_ms: int,
    running_cost: float | None,
    final_text_value: str | None = None,
) -> AsyncIterator[Event]:
    """Emit the error ResultEvent and mark the run failed.

    Shared by the loop-guard hard stop and the verifier ``stop`` verdict."""
    event: Event = ResultEvent(
        subtype="error",
        stop_reason="error",
        total_usage=total,
        duration_ms=duration_ms,
        final_text=final_text_value,
        total_cost_usd=running_cost,
    )
    await _persist_event(session, run_id, event)
    store = agent.run_store
    if run_record is not None and store is not None:
        checkpoint.total_usage = total
        await store.mark_failed(run_id, checkpoint)
    yield event


async def _success_result_tail(
    session: Session,
    agent: Any,
    *,
    run_id: str,
    run_record: Any,
    checkpoint: Any,
    total: Usage,
    duration_ms: int,
    running_cost: float | None,
    stop_reason: StopReason,
    final_text_value: str | None = None,
    structured_output: dict[str, Any] | None = None,
    structured_error: str | None = None,
) -> AsyncIterator[Event]:
    """Emit the success ResultEvent and mark the run completed.

    Shared by the final-text and final-tool terminal paths."""
    event: Event = ResultEvent(
        subtype="success",
        stop_reason=stop_reason,
        total_usage=total,
        duration_ms=duration_ms,
        final_text=final_text_value,
        structured_output=structured_output,
        structured_error=structured_error,
        total_cost_usd=running_cost,
    )
    await _persist_event(session, run_id, event)
    store = agent.run_store
    if run_record is not None and store is not None:
        checkpoint.total_usage = total
        await store.mark_completed(run_id, checkpoint)
    yield event


async def _max_turns_tail(
    session: Session,
    agent: Any,
    *,
    run_id: str,
    run_record: Any,
    checkpoint: Any,
    max_turns: int,
    total: Usage,
    duration_ms: int,
    running_cost: float | None,
) -> AsyncIterator[Event]:
    """Emit the guard/error/result sequence for an exhausted max_turns."""
    turn_error = {
        "name": "TurnLimitError",
        "message": "max turns exceeded",
        "retryable": False,
    }
    event: Event = LoopGuardEvent(
        reason="max_turns",
        detail=f"Maximum turns ({max_turns}) reached.",
        action="stop",
    )
    await _persist_event(session, run_id, event)
    yield event
    event = ErrorEvent(error=turn_error)
    await _persist_event(session, run_id, event)
    yield event
    event = ResultEvent(
        subtype="error",
        stop_reason="error",
        total_usage=total,
        duration_ms=duration_ms,
        total_cost_usd=running_cost,
    )
    await _persist_event(session, run_id, event)
    store = agent.run_store
    if run_record is not None and store is not None:
        checkpoint.total_usage = total
        await store.mark_failed(run_id, checkpoint, error=turn_error)
    yield event


async def _gate_retry_tail(
    session: Session,
    *,
    run_id: str,
    feedback: str,
) -> AsyncIterator[Event]:
    """Inject gate *feedback* as a system-reminder user message."""
    from ..skills.system_reminder import wrap_in_system_reminder

    message = Message(role="user", content=[TextBlock(text=wrap_in_system_reminder(feedback))])
    await session.append([message])
    event: Event = UserEvent(message=message)
    await _persist_event(session, run_id, event)
    yield event


async def _final_tool_retry_tail(
    session: Session,
    *,
    run_id: str,
    tool_blocks: list[ToolUseBlock],
    final_id: str,
    feedback: str,
) -> AsyncIterator[Event]:
    """Bounce a final-tool answer back into the loop with *feedback*.

    Unlike :func:`_gate_retry_tail`, the assistant message that triggered this
    already contains an (unanswered) terminal ``tool_use`` block.  Injecting a
    plain user message would leave that ``tool_use`` unmatched and make the next
    provider request invalid (providers reject ``tool_use`` without a paired
    ``tool_result``).  So we answer every ``tool_use`` in the assistant turn —
    the terminal one carries the repair *feedback*, any others are marked not
    executed — which both satisfies pairing and delivers the instruction."""
    content: list[ContentBlock] = []
    for block in tool_blocks:
        if block.id == final_id:
            content.append(ToolResultBlock(tool_use_id=block.id, content=feedback, is_error=True))
        else:
            content.append(
                ToolResultBlock(
                    tool_use_id=block.id,
                    content="Tool not executed; revise and resubmit your final answer.",
                    is_error=True,
                )
            )
    message = Message(role="user", content=content)
    await session.append([message])
    event: Event = UserEvent(message=message)
    await _persist_event(session, run_id, event)
    yield event


@dataclass(slots=True)
class _GateOutcome:
    """Result of the closed-loop terminal gates (schema repair + verifiers)."""

    decision: Literal["pass", "retry", "stop"]
    events: list[Event]
    feedback: str | None = None


async def _evaluate_terminal_gates(
    session: Session,
    *,
    run_id: str,
    max_schema_retries: int,
    attempts: list[int],
    structured_output: dict[str, Any] | None,
    structured_error: str | None,
) -> _GateOutcome:
    """Run the schema-repair gate on a would-be-final answer.

    *attempts* is a mutable ``[schema_attempts]`` cell owned by the run loop.
    Emitted :class:`VerificationEvent`s are persisted here and returned for the
    caller to yield. Final-answer verifier/eval hooks run through
    ``BeforeFinalAnswer``.
    """
    if structured_error is not None and attempts[0] < max_schema_retries:
        attempts[0] += 1
        event: Event = VerificationEvent(
            verifier="output_schema",
            action="retry",
            feedback=structured_error,
            attempt=attempts[0],
        )
        await _persist_event(session, run_id, event)
        feedback = (
            "Your previous response failed structured-output validation: "
            f"{structured_error}\n"
            "Respond again with ONLY a JSON object matching the required schema."
        )
        return _GateOutcome(decision="retry", events=[event], feedback=feedback)
    if structured_error is not None and max_schema_retries > 0:
        event = VerificationEvent(
            verifier="output_schema",
            action="exhausted",
            feedback=structured_error,
            attempt=attempts[0],
        )
        await _persist_event(session, run_id, event)
        return _GateOutcome(decision="pass", events=[event])
    return _GateOutcome(decision="pass", events=[])
