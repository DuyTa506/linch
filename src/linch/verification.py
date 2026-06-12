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
    """

    action: VerdictAction = "pass"
    feedback: str = ""
    reason: str = ""


@dataclass(slots=True)
class VerificationContext:
    """Snapshot of the would-be-final answer handed to each verifier."""

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
    """Duck-typed verification gate.  ``verify`` may be sync or async."""

    name: str

    def verify(self, ctx: VerificationContext) -> Any:  # Verdict | Awaitable[Verdict]
        ...


def normalize_verifiers(value: Any) -> list[Any]:
    """Normalize verifier input to a list, validating shape."""
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
    """Evaluate *verifiers* in order; return the first non-pass verdict.

    Returns ``(verifier_name, verdict)``; ``("", Verdict())`` when every
    verifier passes.  A verifier that raises is treated as passing — a faulty
    verifier never crashes a run (mirrors the observer contract).
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
    """Lift an evals scorer (``linch.evals.scorers``) into a live verifier.

    The scorer is called with ``output=ctx.final_text``; ``False`` maps to
    *on_fail* (default ``"retry"`` with *feedback*), ``True``/``None`` map to
    pass.  Only output-based scorers (``text_contains``, ``schema_valid``)
    are meaningful here — event-based scorers (``tool_called``, the context
    scorers) receive no events in a live run; write a custom Verifier for
    those instead.
    """

    def __init__(
        self,
        scorer: Any,
        *,
        feedback: str,
        name: str | None = None,
        on_fail: Literal["retry", "stop"] = "retry",
    ) -> None:
        self.scorer = scorer
        self.feedback = feedback
        self.on_fail: Literal["retry", "stop"] = on_fail
        self.name = name or str(getattr(scorer, "__name__", "scorer"))

    def verify(self, ctx: VerificationContext) -> Verdict:
        try:
            outcome = self.scorer(output=ctx.final_text or "", events=None, result_event=None)
        except Exception:
            return Verdict()
        if outcome is False:
            return Verdict(action=self.on_fail, feedback=self.feedback, reason=self.name)
        return Verdict()
