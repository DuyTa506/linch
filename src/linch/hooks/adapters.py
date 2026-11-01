from __future__ import annotations

import inspect
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
    AgentStopContext,
    BeforeFinalAnswerContext,
    BeforeProviderCallContext,
    PostToolUseContext,
    PreToolUseContext,
)
from .types import HookResult


class ContextInjectionHook:
    """Adapt a context builder to the hook lifecycle.

    Args:
        builder: Context builder or builder sequence used for each turn.
    """

    name = "context_builder"

    def __init__(self, builder: Any) -> None:
        """Create an adapter around a context builder.

        Args:
            builder: Context builder configuration.
        """

        self.builder = normalize_context_builder(builder)

    async def build_context(self, session: Any, turn_index: int) -> ContextBuildResult | None:
        """Build bounded context for a session turn.

        Args:
            session: Session whose provider context is being assembled.
            turn_index: Turn number being assembled.

        Returns:
            Context build result, or ``None`` when no builder is configured.
        """

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

    async def on_agent_stop(self, ctx: AgentStopContext) -> None:
        """Run the builder's optional terminal cleanup hook.

        Args:
            ctx: Agent-stop lifecycle context.
        """

        cleanup = getattr(self.builder, "on_agent_stop", None)
        if cleanup is not None:
            result = cleanup(ctx)
            if inspect.isawaitable(result):
                await result


class ToolMiddlewareHook:
    """Adapt tool middleware objects to pre/post tool hooks.

    Args:
        middleware: Middleware object or sequence to invoke in order.
    """

    name = "tool_middleware"

    def __init__(self, middleware: Any) -> None:
        """Create an adapter around tool middleware.

        Args:
            middleware: Middleware configuration.
        """

        self.middleware = normalize_middleware(middleware)

    async def on_pre_tool_use(self, ctx: PreToolUseContext) -> HookResult | None:
        """Validate or rewrite tool input before execution.

        Args:
            ctx: Pre-tool lifecycle context.

        Returns:
            A hook result when input changes or execution is blocked; otherwise
            ``None``.
        """

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
                # call (matches the former middleware dispatch contract).
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
        """Validate or rewrite the legacy result after execution.

        Canonical-only ``ctx.tool_output`` values are not processed by this
        legacy middleware adapter.

        Args:
            ctx: Post-tool lifecycle context.

        Returns:
            A hook result when the result changes or middleware fails; otherwise
            ``None``.
        """

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
    """Adapt final-answer verifiers to the pre-final-answer hook.

    Args:
        verifiers: Verifier object or sequence evaluated in order.
        max_retries: Maximum verification retries allowed per run.
    """

    name = "verifiers"

    def __init__(self, verifiers: Any, *, max_retries: int = 2) -> None:
        """Create a bounded final-answer verifier adapter.

        Args:
            verifiers: Verifier configuration.
            max_retries: Maximum retries allowed for each run.
        """

        from ..verification import normalize_verifiers

        self.verifiers = normalize_verifiers(verifiers)
        self.max_retries = max(0, int(max_retries))
        self.attempts_by_run: dict[str, int] = {}

    async def on_before_final_answer(self, ctx: BeforeFinalAnswerContext) -> HookResult | None:
        """Evaluate verifiers before returning a candidate answer.

        Args:
            ctx: Candidate final-answer context.

        Returns:
            A retry, stop, or exhausted-result hook decision; ``None`` when all
            verifiers pass.
        """

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
        """Drop retry counters when a run reaches any terminal path.

        Args:
            ctx: Agent-stop context containing the run identifier.
        """

        # Drop per-run retry state on every terminal path (incl. max_turns,
        # budget, abort, error) so this per-agent hook can't leak run ids.
        self.attempts_by_run.pop(ctx.run_id, None)


class StopPredicateHook:
    """Stop a run when a host predicate returns true.

    Args:
        predicate: Callable receiving the current session.
    """

    name = "stop_when"

    def __init__(self, predicate: Any) -> None:
        """Create a predicate-backed stop hook.

        Args:
            predicate: Callable evaluated before each provider call.
        """

        self.predicate = predicate

    def on_before_provider_call(self, ctx: BeforeProviderCallContext) -> HookResult | None:
        """Evaluate the stop predicate before a provider request.

        Args:
            ctx: Before-provider-call lifecycle context.

        Returns:
            A successful stop result when the predicate is true; otherwise
            ``None``.
        """

        try:
            if self.predicate(ctx.session):
                return HookResult.stop("stop_when", metadata={"subtype": "success"})
        except Exception:
            return None
        return None
