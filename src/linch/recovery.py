"""Explicit, opt-in output-truncation recovery.

When a model's *text* response is cut off because it hit the output-token limit
(normalized to ``stop_reason == "max_tokens"`` across providers), the default
loop returns that truncated text as the final answer. Linch never silently
escalates the output cap — raising caps changes cost and latency, which is the
embedder's policy decision, not the runtime's.

``TruncationRecovery`` makes recovery an explicit knob: when configured on an
``Agent`` (``truncation_recovery=...``), a truncated text turn is not finalized
immediately. Instead the loop appends ``feedback`` as a user turn and runs
again so the model can continue from where it stopped, up to ``max_attempts``
times. With no ``truncation_recovery`` set, behavior is byte-identical to before.
"""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_TRUNCATION_FEEDBACK = (
    "Your previous response was cut off because it reached the output token "
    "limit. Continue exactly where you left off — do not repeat earlier text."
)


@dataclass(frozen=True, slots=True)
class TruncationRecovery:
    """Opt-in recovery for responses truncated by the output-token limit.

    Attributes:
        max_attempts: Maximum number of continuation turns to spend recovering a
            truncated answer within a single run. Once exhausted, the truncated
            response is returned as the final answer.
        feedback: The user-turn text injected to ask the model to continue.
    """

    max_attempts: int = 1
    feedback: str = _DEFAULT_TRUNCATION_FEEDBACK

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("truncation_recovery.max_attempts must be >= 1")
        if not self.feedback.strip():
            raise ValueError("truncation_recovery.feedback must be a non-empty string")
