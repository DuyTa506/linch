from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class LoopGuard:
    """Configuration for the loop guard subsystem.

    The loop guard detects obvious agentic loops — repeated identical tool
    calls and consecutive tool failures — and terminates the run cleanly
    rather than allowing it to spin until ``max_turns`` is exhausted.

    This guard is **on by default** at :class:`~linch.Agent` construction
    with conservative thresholds.  Disable entirely with
    ``Agent(loop_guard=None)``.

    Attributes:
        max_identical_tool_calls: Maximum number of times the same tool may
            be called with identical inputs before the guard trips.  Set to
            ``0`` to disable this check.  Defaults to ``3``.
        max_consecutive_failures: Maximum number of back-to-back tool-call
            batches where *every* call fails before the guard trips.  Set to
            ``0`` to disable.  Defaults to ``3``.
        force_final_answer: When ``True``, the guard injects a system
            reminder and removes all tools for one extra turn, prompting the
            model to produce a text summary instead of terminating with an
            error.  Defaults to ``False`` (clean stop).
    """

    max_identical_tool_calls: int = 3
    max_consecutive_failures: int = 3
    force_final_answer: bool = False


@dataclass(slots=True)
class LoopGuardState:
    """Mutable per-run state tracked by the loop guard.

    One instance is created at the start of each ``run_loop`` invocation.
    """

    call_counts: dict[str, int] = field(default_factory=dict)
    consecutive_failures: int = 0


@dataclass(slots=True)
class LoopGuardDecision:
    """Result returned by :func:`evaluate_loop_guard`.

    Attributes:
        action: ``"continue"`` — no guard threshold was crossed; ``"stop"``
            — terminate with an error result; ``"force_final"`` — inject a
            reminder and run one final tools-disabled turn.
        reason: Machine-readable tag for the trip condition (e.g.
            ``"repeated_tool_call"``, ``"repeated_failures"``).  Empty when
            action is ``"continue"``.
        detail: Human-readable description of why the guard tripped.  Empty
            when action is ``"continue"``.
    """

    action: Literal["continue", "stop", "force_final"]
    reason: str
    detail: str


def _canonical_sig(name: str, input: dict[str, Any]) -> str:
    """Return a stable string signature for a tool name + input pair."""
    return f"{name}:{json.dumps(input, sort_keys=True, separators=(',', ':'))}"


def evaluate_loop_guard(
    guard: LoopGuard,
    state: LoopGuardState,
    tool_blocks: list[Any],
    result_blocks: list[Any],
) -> LoopGuardDecision:
    """Evaluate loop-guard checks after a batch of tool calls completes.

    Modifies *state* in place to track cumulative call counts and the
    consecutive-failure streak.

    Parameters
    ----------
    guard:
        The guard configuration.
    state:
        Per-run mutable state updated by this call.
    tool_blocks:
        The :class:`~linch.types.ToolUseBlock` instances that were
        requested this turn.
    result_blocks:
        The :class:`~linch.types.ToolResultBlock` instances produced
        this turn (same length/order as *tool_blocks*).

    Returns
    -------
    LoopGuardDecision
        ``action="continue"`` if no threshold was crossed, otherwise
        ``"stop"`` or ``"force_final"`` depending on
        :attr:`LoopGuard.force_final_answer`.
    """
    # ── Repeated identical tool-call check ────────────────────────────────
    if guard.max_identical_tool_calls > 0:
        for block in tool_blocks:
            sig = _canonical_sig(block.name, block.input)
            state.call_counts[sig] = state.call_counts.get(sig, 0) + 1
            if state.call_counts[sig] >= guard.max_identical_tool_calls:
                action: Literal["stop", "force_final"] = (
                    "force_final" if guard.force_final_answer else "stop"
                )
                return LoopGuardDecision(
                    action=action,
                    reason="repeated_tool_call",
                    detail=(
                        f"Tool '{block.name}' called with identical inputs "
                        f"{state.call_counts[sig]} time(s) "
                        f"(limit: {guard.max_identical_tool_calls})."
                    ),
                )

    # ── Consecutive failure streak check ──────────────────────────────────
    if guard.max_consecutive_failures > 0 and result_blocks:
        all_failed = all(getattr(r, "is_error", False) for r in result_blocks)
        if all_failed:
            state.consecutive_failures += 1
        else:
            state.consecutive_failures = 0

        if state.consecutive_failures >= guard.max_consecutive_failures:
            action2: Literal["stop", "force_final"] = (
                "force_final" if guard.force_final_answer else "stop"
            )
            return LoopGuardDecision(
                action=action2,
                reason="repeated_failures",
                detail=(
                    f"All tool calls failed for {state.consecutive_failures} "
                    f"consecutive batch(es) "
                    f"(limit: {guard.max_consecutive_failures})."
                ),
            )

    return LoopGuardDecision(action="continue", reason="", detail="")


def normalize_loop_guard(value: Any) -> LoopGuard | None:
    """Normalize a loop-guard config value into a :class:`LoopGuard` or ``None``.

    * ``None`` / ``False`` → disabled (returns ``None``).
    * :class:`LoopGuard` instance → returned as-is.
    * ``dict`` → ``LoopGuard(**value)``.

    Raises :class:`TypeError` for any other type.
    """
    if value is None or value is False:
        return None
    if isinstance(value, LoopGuard):
        return value
    if isinstance(value, dict):
        return LoopGuard(**value)
    raise TypeError(f"loop_guard must be a LoopGuard, dict, None, or False; got {type(value)!r}")
