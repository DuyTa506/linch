"""Byte-identical + linear-scaling guard for context-budget trimming (Phase 2.2).

The trim was rewritten from repeated ``pop(0)`` + per-drop system re-joins to a
moving cutoff, one suffix slice, and an incremental system-length total. This
pins the new implementation to the exact output of a naive per-pop oracle across
randomized inputs, and checks that trimming is linear (not quadratic) in message
count.
"""

from __future__ import annotations

import random
import time

from linch import ContextBudget, ContextBuildResult
from linch.context.builder import _estimate_message, _estimate_text, apply_context_budget
from linch.types import Message, SystemBlock, TextBlock


def _msg(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _naive_trim(system_blocks, messages, message_costs, system_used, max_tokens, trimmed0):
    """Reference: the original per-pop algorithm (the oracle we must match)."""
    system_blocks = list(system_blocks)
    messages = list(messages)
    message_costs = list(message_costs)
    used = system_used + sum(message_costs)
    trimmed = trimmed0
    while messages and used > max_tokens:
        messages.pop(0)
        used -= message_costs.pop(0)
        trimmed = True
    while system_blocks and used > max_tokens:
        system_blocks.pop(0)
        system_used = _estimate_text("\n".join(b.text for b in system_blocks))
        used = system_used + sum(message_costs)
        trimmed = True
    return system_blocks, messages, used, max(0, max_tokens - used), trimmed


def test_trim_matches_naive_oracle_over_random_inputs() -> None:
    rng = random.Random(20260711)
    for _ in range(400):
        n_sys = rng.randint(0, 5)
        n_msg = rng.randint(0, 40)
        system_blocks = [SystemBlock(text="x" * rng.randint(0, 30)) for _ in range(n_sys)]
        messages = [_msg("y" * rng.randint(0, 40)) for _ in range(n_msg)]
        max_tokens = rng.randint(0, 60)

        # Estimator path (callable) and fallback path (None) must both match.
        for estimator in (None, lambda ms, model: sum(_estimate_message(m) for m in ms)):
            result = ContextBuildResult(
                system_blocks=list(system_blocks),
                messages=list(messages),
                budget=ContextBudget(max_tokens=max_tokens),
            )
            system_used = _estimate_text("\n".join(b.text for b in system_blocks))
            costs = [_estimate_message(m) for m in messages]
            exp_sys, exp_msg, exp_used, exp_rem, exp_trim = _naive_trim(
                system_blocks, messages, costs, system_used, max_tokens, False
            )

            out = apply_context_budget(result, estimator=estimator, model="gpt-5")

            assert [b.text for b in out.system_blocks] == [b.text for b in exp_sys]
            assert [m.content[0].text for m in out.messages] == [m.content[0].text for m in exp_msg]
            assert out.budget.used_tokens == exp_used
            assert out.budget.remaining_tokens == exp_rem
            assert out.budget.trimmed is exp_trim


def test_trim_scales_roughly_linearly_in_message_count() -> None:
    def trim_n(n: int) -> float:
        messages = [_msg("word " * 20) for _ in range(n)]
        result = ContextBuildResult(messages=messages, budget=ContextBudget(max_tokens=5))
        start = time.perf_counter()
        apply_context_budget(result, estimator=None, model="gpt-5")
        return time.perf_counter() - start

    trim_n(2_000)  # warm
    t_small = min(trim_n(2_000) for _ in range(3))
    t_large = min(trim_n(20_000) for _ in range(3))

    # 10x the messages must not be ~100x the time (the quadratic signature).
    # Linear would be ~10x; allow generous slack for noise/constant factors.
    assert t_large < t_small * 30, f"trim looks superlinear: {t_small=} {t_large=}"
