"""The summary request ends with an instruction, and an unusable summary is not applied.

The folded history ends with a user message that a model answers instead of summarizing,
so the request carries one more user message with the summary prompt. The instruction
cases are shared with linch-rust (``fixtures/compaction_request.json``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_compaction_accounting import _events, _Provider, _setup

from linch.abort import AbortContext
from linch.compaction import (
    CompactionContext,
    DetailedCompaction,
    summarize_with_provider,
    summary_instruction,
)
from linch.errors import ProviderError
from linch.types import Message, SystemBlock, TextBlock, Usage

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "compaction_request.json").read_text())


def _user(text: str) -> Message:
    return Message("user", [TextBlock(text)])


@pytest.mark.parametrize("case", FIXTURE["instruction"], ids=lambda case: case["name"])
def test_the_instruction_matches_the_shared_cases(case: dict[str, Any]) -> None:
    older = [_user("go")]
    if case["folded_has_checkpoint"]:
        older.append(_user("<summary of earlier conversation>\n\nearlier"))
    assert summary_instruction(case["prompt"], older) == case["expected"]


async def test_the_summary_request_ends_with_the_instruction() -> None:
    provider = _Provider(_events("Recap: the code word is PELICAN-7421."))
    older = [_user("the code word is PELICAN-7421"), _user("log excerpt 2, no action needed " * 20)]
    text = await summarize_with_provider(
        provider, "m", older, AbortContext(), prompt="recap", max_output_tokens=300
    )
    assert "PELICAN-7421" in text
    request = provider.requests[0]
    assert request.messages[:2] == older
    last = request.messages[-1]
    assert last.role == "user" and len(request.messages) == 3
    assert last.content[0].text == summary_instruction("recap", older)
    assert request.system[0].text == "recap"


async def test_a_summary_no_smaller_than_the_folded_history_is_rejected() -> None:
    older = [_user("short")]
    provider = _Provider(_events("x" * 400))
    with pytest.raises(ProviderError, match="not smaller"):
        await summarize_with_provider(provider, "m", older, AbortContext())


async def test_the_accounted_summary_is_rejected_when_not_smaller() -> None:
    _, ledger, _, caller = _setup(provider=_Provider(_events("x" * 400, usage=Usage(11, 2))))
    result = await caller.text(
        logical_call_id="op",
        model="m",
        system=[SystemBlock("recap")],
        messages=[_user("short")],
        max_output_tokens=300,
        signal=AbortContext(),
        reject_text_tokens_at=10,
    )
    assert result.text is None
    assert result.receipt.disposition == "rejected"
    assert result.receipt.rejection_reason == "not_smaller"
    assert result.receipt.usage == Usage(11, 2)
    assert ledger.total_usage == Usage(11, 2), "a rejected summary is still paid for"


async def test_a_strategy_asks_for_a_summary_that_updates_the_checkpoint() -> None:
    provider = _Provider(_events("S2"))
    messages = [
        _user("go"),
        _user("<summary of earlier conversation>\n\n" + "S1 " * 100),
        Message("assistant", [TextBlock("ok " * 100)]),
        _user("then " * 100),
        Message("assistant", [TextBlock("done " * 100)]),
    ]
    ctx = CompactionContext(messages=messages, model="m", signal=AbortContext())
    strategy = DetailedCompaction(keep_recent_turns=1, prompt="recap")
    await strategy.compact(ctx, provider)
    instruction = provider.requests[0].messages[-1].content[0].text
    assert instruction == summary_instruction("recap", messages[:4])
    assert "Update that summary" in instruction
