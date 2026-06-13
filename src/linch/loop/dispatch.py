"""Hook-dispatch wrappers for the run loop.

Each function wraps a single ``HookDispatcher.dispatch(...)`` call for one
lifecycle point and normalises the dispatcher result into the small
``(value, events, action, feedback)`` tuples the loop consumes. They were
extracted from ``_run_loop_impl`` to keep that generator focused on control
flow; the loop binds ``hook_dispatcher``/``session``/``run_id`` via
``functools.partial`` so its call sites stay unchanged."""

from __future__ import annotations

from typing import Any, cast

from ..events import Event, ResultEvent
from ..hooks import (
    AfterProviderCallContext,
    BeforeFinalAnswerContext,
    BeforeProviderCallContext,
    HookDispatcher,
    HookEvent,
    StopContext,
    UserPromptSubmitContext,
)
from ..session import Session
from ..types import AssistantAssembly, StopReason, ToolUseBlock


async def dispatch_user_prompt(
    hook_dispatcher: HookDispatcher,
    session: Session,
    run_id: str,
    prompt_value: str,
    images_value: list[dict[str, str]] | None,
) -> tuple[str, list[dict[str, str]] | None, list[Event], str | None]:
    if not hook_dispatcher.active:
        return prompt_value, images_value, [], None
    dispatched = await hook_dispatcher.dispatch(
        HookEvent.USER_PROMPT_SUBMIT,
        UserPromptSubmitContext(
            session=session,
            run_id=run_id,
            turn_index=None,
            deps=getattr(session, "run_deps", None),
            prompt=prompt_value,
            images=images_value,
        ),
    )
    result = dispatched.result
    if result.action == "mutate":
        return (
            result.prompt if result.prompt is not None else prompt_value,
            result.images if result.images is not None else images_value,
            dispatched.events,
            None,
        )
    if result.action in {"block", "stop"}:
        return prompt_value, images_value, dispatched.events, result.reason or result.feedback
    return prompt_value, images_value, dispatched.events, None


async def dispatch_lifecycle(hook_dispatcher: HookDispatcher, event: HookEvent, ctx: Any) -> None:
    if hook_dispatcher.active:
        await hook_dispatcher.dispatch(event, ctx)


async def dispatch_before_provider_call(
    hook_dispatcher: HookDispatcher,
    session: Session,
    run_id: str,
    request: Any,
    turn_index: int,
    context_result: Any,
) -> tuple[Any, list[Event], str | None, str | None]:
    if not hook_dispatcher.active:
        return request, [], None, None
    dispatched = await hook_dispatcher.dispatch(
        HookEvent.BEFORE_PROVIDER_CALL,
        BeforeProviderCallContext(
            session=session,
            run_id=run_id,
            turn_index=turn_index,
            deps=getattr(session, "run_deps", None),
            request=request,
            context_result=context_result,
        ),
    )
    result = dispatched.result
    if result.action == "mutate" and result.request is not None:
        return result.request, dispatched.events, None, None
    if result.action in {"stop", "block"}:
        if result.metadata.get("subtype") == "success":
            return request, dispatched.events, "stop_success", result.reason or result.feedback
        return request, dispatched.events, "stop", result.reason or result.feedback
    if result.action == "force_continue":
        return request, dispatched.events, "continue", result.feedback
    return request, dispatched.events, None, None


async def dispatch_after_provider_call(
    hook_dispatcher: HookDispatcher,
    session: Session,
    run_id: str,
    assembly: AssistantAssembly,
    turn_index: int,
) -> tuple[AssistantAssembly, list[Event], str | None, str | None]:
    if not hook_dispatcher.active:
        return assembly, [], None, None
    dispatched = await hook_dispatcher.dispatch(
        HookEvent.AFTER_PROVIDER_CALL,
        AfterProviderCallContext(
            session=session,
            run_id=run_id,
            turn_index=turn_index,
            deps=getattr(session, "run_deps", None),
            assembly=assembly,
        ),
    )
    result = dispatched.result
    if result.action == "mutate" and result.assembly is not None:
        return result.assembly, dispatched.events, None, None
    if result.action in {"stop", "block"}:
        return assembly, dispatched.events, "stop", result.reason or result.feedback
    if result.action in {"retry", "force_continue"}:
        return assembly, dispatched.events, "continue", result.feedback
    return assembly, dispatched.events, None, None


async def dispatch_before_final_answer(
    hook_dispatcher: HookDispatcher,
    session: Session,
    run_id: str,
    *,
    turn_index: int,
    final_text_value: str | None,
    structured_output: dict[str, object] | None,
    structured_error: str | None,
    stop_reason: StopReason,
    final_tool_name: str | None = None,
    tool_use: ToolUseBlock | None = None,
    skip: bool = False,
) -> tuple[
    str | None,
    dict[str, object] | None,
    str | None,
    list[Event],
    str | None,
    str | None,
]:
    if skip or not hook_dispatcher.active:
        return final_text_value, structured_output, structured_error, [], None, None
    dispatched = await hook_dispatcher.dispatch(
        HookEvent.BEFORE_FINAL_ANSWER,
        BeforeFinalAnswerContext(
            session=session,
            run_id=run_id,
            turn_index=turn_index,
            deps=getattr(session, "run_deps", None),
            final_text=final_text_value,
            structured_output=structured_output,
            structured_error=structured_error,
            stop_reason=stop_reason,
            final_tool_name=final_tool_name,
            tool_use=tool_use,
        ),
    )
    result = dispatched.result
    if result.action == "mutate":
        return (
            result.final_text if result.final_text is not None else final_text_value,
            result.structured_output if result.structured_output is not None else structured_output,
            result.structured_error if result.structured_error is not None else structured_error,
            dispatched.events,
            None,
            None,
        )
    if result.action in {"retry", "force_continue"}:
        return (
            final_text_value,
            structured_output,
            structured_error,
            dispatched.events,
            "retry",
            result.feedback,
        )
    if result.action in {"stop", "block"}:
        return (
            final_text_value,
            structured_output,
            structured_error,
            dispatched.events,
            "stop",
            result.reason or result.feedback,
        )
    return final_text_value, structured_output, structured_error, dispatched.events, None, None


async def dispatch_stop(
    hook_dispatcher: HookDispatcher,
    session: Session,
    run_id: str,
    result_event: ResultEvent,
    turn_index: int | None,
) -> tuple[ResultEvent, list[Event], str | None, str | None]:
    if not hook_dispatcher.active:
        return result_event, [], None, None
    dispatched = await hook_dispatcher.dispatch(
        HookEvent.STOP,
        StopContext(
            session=session,
            run_id=run_id,
            turn_index=turn_index,
            deps=getattr(session, "run_deps", None),
            result_event=result_event,
        ),
    )
    result = dispatched.result
    if result.action == "mutate" and result.result_event is not None:
        return cast(ResultEvent, result.result_event), dispatched.events, None, None
    if result.action == "force_continue":
        return result_event, dispatched.events, "continue", result.feedback
    if result.action in {"stop", "block"}:
        return result_event, dispatched.events, "stop", result.reason or result.feedback
    return result_event, dispatched.events, None, None
