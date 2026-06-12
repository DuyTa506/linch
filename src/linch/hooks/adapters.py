from __future__ import annotations

import inspect
import logging
from dataclasses import replace
from typing import Any

from ..context import (
    ContextBuildResult,
    ContextBuildTurn,
    apply_context_budget,
    normalize_context_builder,
)
from ..middleware import (
    MiddlewareContext,
    ToolCallMiddlewareInput,
    ToolCallMiddlewareResult,
    normalize_middleware,
)
from ..tools import ToolResult
from .contexts import (
    AgentStartContext,
    AgentStopContext,
    BeforeFinalAnswerContext,
    BeforeProviderCallContext,
    EventEmitContext,
    PostToolUseContext,
    PreToolUseContext,
    ProviderCallStartContext,
    ProviderCallStopContext,
    ToolUseStartContext,
    ToolUseStopContext,
    TurnStartContext,
    TurnStopContext,
)
from .types import HookResult

_log = logging.getLogger("linch.observability")


class ContextInjectionHook:
    name = "context_builder"

    def __init__(self, builder: Any) -> None:
        self.builder = normalize_context_builder(builder)

    async def build_context(self, session: Any, turn_index: int) -> ContextBuildResult | None:
        if self.builder is None:
            return None
        agent = session.agent
        turn = ContextBuildTurn(
            session=session,
            messages=list(session.provider_view),
            turn_index=turn_index,
            deps=getattr(session, "run_deps", None),
            model=agent.model,
            tools=getattr(session, "tools_override", None) or agent.tools,
            token_estimator=getattr(agent, "token_estimator", None),
        )
        result = await self.builder.build(turn)
        return apply_context_budget(
            result,
            estimator=getattr(agent, "token_estimator", None),
            model=agent.model,
        )


class ToolMiddlewareHook:
    name = "tool_middleware"

    def __init__(self, middleware: Any) -> None:
        self.middleware = normalize_middleware(middleware)

    async def on_pre_tool_use(self, ctx: PreToolUseContext) -> HookResult | None:
        current = ToolCallMiddlewareInput(
            tool_use_id=ctx.tool_use_id,
            tool_name=ctx.tool_name,
            input=dict(ctx.input),
            summary=ctx.summary,
        )
        mctx = MiddlewareContext(
            session_id=ctx.session.id,
            run_id=ctx.run_id,
            turn_index=ctx.turn_index,
            tool_use_id=ctx.tool_use_id,
            tool_name=ctx.tool_name,
            deps=ctx.deps,
        )
        for item in self.middleware:
            hook = getattr(item, "before_tool_call", None)
            if hook is None:
                continue
            try:
                raw = hook(current, mctx)
                if inspect.isawaitable(raw):
                    raw = await raw
            except Exception as exc:
                # Fail closed: a raising before_tool_call middleware blocks the
                # call (matches the legacy dispatch_before_tool_call contract).
                return HookResult.block(
                    f"Middleware before_tool_call failed: {exc}", input=current.input
                )
            if raw is None:
                continue
            if not isinstance(raw, ToolCallMiddlewareResult):
                return HookResult.block(
                    "Middleware before_tool_call failed: expected ToolCallMiddlewareResult or None",
                    input=current.input,
                )
            if raw.input is not None:
                current = replace(current, input=raw.input)
            if raw.error:
                return HookResult.block(raw.error, input=current.input)
        if current.input != ctx.input:
            return HookResult.mutate(input=current.input)
        return None

    async def on_post_tool_use(self, ctx: PostToolUseContext) -> HookResult | None:
        current = ctx.result
        if current is None:
            return None
        call = ToolCallMiddlewareInput(
            tool_use_id=ctx.tool_use_id,
            tool_name=ctx.tool_name,
            input=dict(ctx.input),
        )
        mctx = MiddlewareContext(
            session_id=ctx.session.id,
            run_id=ctx.run_id,
            turn_index=ctx.turn_index,
            tool_use_id=ctx.tool_use_id,
            tool_name=ctx.tool_name,
            deps=ctx.deps,
        )
        for item in self.middleware:
            hook = getattr(item, "after_tool_result", None)
            if hook is None:
                continue
            try:
                raw = hook(call, current, mctx)
                if inspect.isawaitable(raw):
                    raw = await raw
            except Exception as exc:
                # Fail closed: a raising after_tool_result middleware yields an
                # error result rather than letting the unredacted result pass.
                return HookResult.mutate(
                    tool_result=ToolResult(
                        content=f"Middleware after_tool_result failed: {exc}",
                        is_error=True,
                        duration_ms=current.duration_ms,
                    )
                )
            if not isinstance(raw, ToolResult):
                return HookResult.mutate(
                    tool_result=ToolResult(
                        content="Middleware after_tool_result failed: expected ToolResult",
                        is_error=True,
                        duration_ms=current.duration_ms,
                    )
                )
            current = raw
        if current != ctx.result:
            return HookResult.mutate(tool_result=current)
        return None


class FinalAnswerVerifierHook:
    name = "verifiers"

    def __init__(self, verifiers: Any, *, max_retries: int = 2) -> None:
        from ..verification import normalize_verifiers

        self.verifiers = normalize_verifiers(verifiers)
        self.max_retries = max(0, int(max_retries))
        self.attempts_by_run: dict[str, int] = {}

    async def on_before_final_answer(self, ctx: BeforeFinalAnswerContext) -> HookResult | None:
        if not self.verifiers:
            return None
        from ..events import VerificationEvent
        from ..verification import VerificationContext, evaluate_verifiers

        attempt = self.attempts_by_run.get(ctx.run_id, 0)
        name, verdict = await evaluate_verifiers(
            self.verifiers,
            VerificationContext(
                final_text=ctx.final_text,
                structured_output=ctx.structured_output,
                structured_error=ctx.structured_error,
                turn_index=int(ctx.turn_index or 0),
                attempt=attempt,
                session=ctx.session,
            ),
        )
        if verdict.action == "stop":
            event = VerificationEvent(
                verifier=name,
                action="stop",
                feedback=verdict.feedback or verdict.reason,
                attempt=attempt,
            )
            self.attempts_by_run.pop(ctx.run_id, None)
            return HookResult.stop(
                verdict.reason or name,
                feedback=verdict.feedback,
                metadata={"events": [event]},
            )
        if verdict.action == "retry":
            if attempt < self.max_retries:
                attempt += 1
                self.attempts_by_run[ctx.run_id] = attempt
                event = VerificationEvent(
                    verifier=name,
                    action="retry",
                    feedback=verdict.feedback,
                    attempt=attempt,
                )
                return HookResult.retry(
                    verdict.feedback
                    or "The previous answer failed verification. Improve it and answer again.",
                    reason=name,
                    metadata={"events": [event]},
                )
            event = VerificationEvent(
                verifier=name,
                action="exhausted",
                feedback=verdict.feedback,
                attempt=attempt,
            )
            self.attempts_by_run.pop(ctx.run_id, None)
            return HookResult.continue_().with_events([event])
        self.attempts_by_run.pop(ctx.run_id, None)
        return None

    def on_agent_stop(self, ctx: Any) -> None:
        # Drop per-run retry state on every terminal path (incl. max_turns,
        # budget, abort, error) so this per-agent hook can't leak run ids.
        self.attempts_by_run.pop(ctx.run_id, None)


class StopPredicateHook:
    name = "stop_when"

    def __init__(self, predicate: Any) -> None:
        self.predicate = predicate

    def on_before_provider_call(self, ctx: BeforeProviderCallContext) -> HookResult | None:
        try:
            if self.predicate(ctx.session):
                return HookResult.stop("stop_when", metadata={"subtype": "success"})
        except Exception:
            return None
        return None


class RunTelemetryHook:
    name = "observers"

    def __init__(self, observers: Any) -> None:
        from ..observability import ObserverDispatcher, normalize_observers

        self.observers = normalize_observers(observers)
        # Reuse the observability hub for fan-out (await async, isolate
        # exceptions) instead of re-implementing it here.
        self._hub = ObserverDispatcher(self.observers)

    async def on_agent_start(self, ctx: AgentStartContext) -> None:
        from ..observability import RunInfo

        await self._dispatch(
            "on_run_start",
            RunInfo(
                run_id=ctx.run_id,
                session_id=ctx.session.id,
                model=ctx.model,
                prompt=ctx.prompt,
                tools=ctx.tools,
            ),
        )

    async def on_agent_stop(self, ctx: AgentStopContext) -> None:
        await self._dispatch("on_run_end", ctx.result)

    async def on_turn_start(self, ctx: TurnStartContext) -> None:
        from ..observability import TurnInfo

        await self._dispatch(
            "on_turn_start",
            TurnInfo(run_id=ctx.run_id, turn_index=int(ctx.turn_index or 0)),
        )

    async def on_turn_stop(self, ctx: TurnStopContext) -> None:
        from ..observability import TurnInfo

        await self._dispatch(
            "on_turn_end",
            TurnInfo(run_id=ctx.run_id, turn_index=int(ctx.turn_index or 0)),
        )

    async def on_provider_call_start(self, ctx: ProviderCallStartContext) -> None:
        from ..observability import ProviderCallInfo

        await self._dispatch(
            "on_provider_call_start",
            ProviderCallInfo(
                run_id=ctx.run_id,
                turn_index=int(ctx.turn_index or 0),
                model=ctx.model,
            ),
        )

    async def on_provider_call_stop(self, ctx: ProviderCallStopContext) -> None:
        from ..observability import ProviderCallResult
        from ..types import Usage

        await self._dispatch(
            "on_provider_call_end",
            ProviderCallResult(
                run_id=ctx.run_id,
                turn_index=int(ctx.turn_index or 0),
                model=ctx.model,
                stop_reason=ctx.stop_reason,
                usage=ctx.usage or Usage(),
                duration_ms=ctx.duration_ms,
            ),
        )

    async def on_tool_use_start(self, ctx: ToolUseStartContext) -> None:
        from ..observability import ToolInfo

        await self._dispatch(
            "on_tool_start",
            ToolInfo(
                run_id=ctx.run_id,
                turn_index=int(ctx.turn_index or 0),
                tool_use_id=ctx.tool_use_id,
                tool_name=ctx.tool_name,
                input=ctx.input,
                summary=ctx.summary,
            ),
        )

    async def on_tool_use_stop(self, ctx: ToolUseStopContext) -> None:
        from ..observability import ToolResultInfo

        await self._dispatch(
            "on_tool_end",
            ToolResultInfo(
                run_id=ctx.run_id,
                turn_index=int(ctx.turn_index or 0),
                tool_use_id=ctx.tool_use_id,
                tool_name=ctx.tool_name,
                is_error=ctx.is_error,
                duration_ms=ctx.duration_ms,
                result=ctx.result,
                tool_result=ctx.tool_result,
            ),
        )

    async def on_event_emit(self, ctx: EventEmitContext) -> None:
        await self._dispatch("on_event", ctx.event)

    async def aclose(self) -> None:
        # Forward close/flush to wrapped observers (e.g. OTel exporters) so
        # Agent.close() still flushes them after the observers→hooks migration.
        for observer in self.observers:
            closer = getattr(observer, "aclose", None) or getattr(observer, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                _log.exception("observer %r raised on close; continuing", type(observer).__name__)

    async def _dispatch(self, method: str, *args: Any) -> None:
        await self._hub.dispatch(method, *args)
