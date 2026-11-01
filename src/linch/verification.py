"""Closed-loop verification primitives.

A :class:`Verifier` is a duck-typed gate evaluated when the loop is about to
return a final answer (a text-only response).  Its :class:`Verdict` either
lets the run finish (``"pass"``), bounces the answer back into the loop with
feedback (``"retry"``), or fails the run (``"stop"``).  Verifiers are opt-in
via ``Agent(hooks=[FinalAnswerVerifierHook(...)])`` — with the default ``None`` the loop behavior
is unchanged.

Retries are bounded by ``Agent(max_verification_retries=...)`` and still
count toward ``max_turns`` and the run budget, so a strict verifier can never
loop unboundedly.  Verification retry counters are per-run and are not
checkpointed; a resumed run starts with fresh counters.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

VerdictAction = Literal["pass", "retry", "stop"]


@dataclass(slots=True)
class Verdict:
    """Outcome of one verifier evaluation.

    Attributes:
        action: ``"pass"`` (accept the answer), ``"retry"`` (inject
            *feedback* as a user message and run another turn), or
            ``"stop"`` (fail the run with an error result).
        feedback: Guidance injected into the conversation on retry.
        reason: Short machine-readable tag for observability.

    Args:
        action: Decision returned by the verifier.
        feedback: Guidance injected when the decision requests a retry.
        reason: Machine-readable explanation for the decision.
    """

    action: VerdictAction = "pass"
    feedback: str = ""
    reason: str = ""


@dataclass(slots=True)
class VerificationContext:
    """Snapshot of the would-be-final answer handed to each verifier.

    Attributes:
        attempt: Retry count shared by verifiers for the current run.
        session: Live session reference provided for read-only inspection by
            convention.

    Args:
        final_text: Text answer that would otherwise be returned.
        structured_output: Parsed structured answer, when available.
        structured_error: Structured-output parsing or validation error.
        turn_index: Turn being evaluated.
        attempt: Verification retries already used in this run.
        session: Live session associated with the run.
    """

    final_text: str | None
    structured_output: dict[str, Any] | None
    structured_error: str | None
    turn_index: int
    attempt: int
    """Verification retries already used in this run (0 on first evaluation)."""
    session: Any = None
    """The live :class:`~linch.session.Session` (read-only by convention)."""


@runtime_checkable
class Verifier(Protocol):
    """Define a duck-typed gate for validating a would-be-final answer.

    Implementations may return a :class:`Verdict` directly or awaitably. The
    loop evaluates verifiers in registration order and continues on verifier
    exceptions.

    Attributes:
        name: Stable name used in verification events and diagnostics.
    """

    name: str

    def verify(self, ctx: VerificationContext) -> Any:  # Verdict | Awaitable[Verdict]
        """Evaluate the candidate answer and return a verdict.

        Args:
            ctx: Snapshot of the candidate answer and run state.

        Returns:
            A :class:`Verdict` or an awaitable resolving to one.
        """

        ...


def normalize_verifiers(value: Any) -> list[Any]:
    """Normalize verifier input to a list, validating shape.

    Args:
        value: A verifier, list or tuple of verifiers, or ``None``.

    Returns:
        Verifier objects in registration order.

    Raises:
        ConfigError: If an item lacks a callable ``verify`` method.
    """
    if value is None:
        return []
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    for item in items:
        if not callable(getattr(item, "verify", None)):
            from .errors import ConfigError

            raise ConfigError(
                f"verifier {item!r} does not implement the Verifier protocol "
                "(missing a callable .verify(ctx) method)"
            )
    return items


async def evaluate_verifiers(verifiers: list[Any], ctx: VerificationContext) -> tuple[str, Verdict]:
    """Evaluate verifiers in order and return the first non-pass verdict.

    Returns ``(verifier_name, verdict)``; ``("", Verdict())`` when every
    verifier passes.  A verifier that raises is treated as passing — a faulty
    verifier never crashes a run (mirrors the observer contract).

    Args:
        verifiers: Verifiers to evaluate in registration order.
        ctx: Candidate answer and run state supplied to each verifier.

    Returns:
        The first non-pass verdict and its name, or an empty name with a pass.
    """
    for verifier in verifiers:
        name = str(getattr(verifier, "name", verifier.__class__.__name__))
        try:
            result = verifier.verify(ctx)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            continue
        if isinstance(result, Verdict) and result.action != "pass":
            return name, result
    return "", Verdict()


class ScorerVerifier:
    """Lift an evals scorer (``linch.ext.evals.scorers``) into a live verifier.

    The scorer is called with ``output=ctx.final_text``; ``False`` maps to
    *on_fail* (default ``"retry"`` with *feedback*), ``True``/``None`` map to
    pass.  Only output-based scorers (``text_contains``, ``schema_valid``)
    are meaningful here — event-based scorers (``tool_called``, the context
    scorers) receive no events in a live run; write a custom Verifier for
    those instead.

    Args:
        scorer: Eval scorer callable receiving output and event context.
        feedback: Feedback supplied when the scorer rejects the output.
        name: Optional name used in diagnostics and events.
        on_fail: Action to take when the scorer returns ``False``.
    """

    def __init__(
        self,
        scorer: Any,
        *,
        feedback: str,
        name: str | None = None,
        on_fail: Literal["retry", "stop"] = "retry",
    ) -> None:
        """Create a verifier backed by an evaluation scorer.

        Args:
            scorer: Eval scorer callable receiving output and event context.
            feedback: Feedback supplied when the scorer rejects the output.
            name: Optional name used in diagnostics and events.
            on_fail: Action to take when the scorer returns ``False``.
        """

        self.scorer = scorer
        self.feedback = feedback
        self.on_fail: Literal["retry", "stop"] = on_fail
        self.name = name or str(getattr(scorer, "__name__", "scorer"))

    def verify(self, ctx: VerificationContext) -> Verdict:
        """Evaluate the wrapped scorer against a candidate answer.

        Args:
            ctx: Candidate answer and run state.

        Returns:
            A retry or stop verdict when the scorer rejects the output;
            otherwise a passing verdict.
        """

        try:
            outcome = self.scorer(output=ctx.final_text or "", events=None, result_event=None)
        except Exception:
            return Verdict()
        if outcome is False:
            return Verdict(action=self.on_fail, feedback=self.feedback, reason=self.name)
        return Verdict()
