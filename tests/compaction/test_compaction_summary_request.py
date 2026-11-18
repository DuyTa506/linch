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
    DefaultCompaction,
    DetailedCompaction,
    _estimate_history_tokens,
    _summary_text_limit,
    summarize_with_provider,
    summary_instruction,
)
from linch.errors import ProviderError
from linch.types import (
    ImageBlock,
    Message,
    SystemBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

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


# linch-rust measures the checkpoint message (the header and the summary) against the folded
# content, four characters per token, so the same summary is rejected by both. The folded
# part below is 400 characters of text (100 tokens): 400 - 35 (the header) = 365 is the
# first summary length that is rejected.
_FOLDED_TOKENS_100 = [_user("f" * 400)]


@pytest.mark.parametrize(("length", "rejected"), [(364, False), (365, True)])
async def test_the_rejection_counts_the_header_and_the_folded_content_like_linch_rust(
    length: int, rejected: bool
) -> None:
    provider = _Provider(_events("s" * length))
    call = summarize_with_provider(provider, "m", _FOLDED_TOKENS_100, AbortContext())
    if rejected:
        with pytest.raises(ProviderError, match="not smaller"):
            await call
    else:
        assert await call == "s" * length


# The shared rule (linch-rust `compaction::rejection`): a summary is rejected unless the view
# after it is estimated strictly smaller than the view before it, four bytes per token over
# the whole view (the folded part and the turns kept after it), the header counted. Each row
# is (folded characters, kept characters, summary bytes, rejected), worked out by hand from
# the Rust arithmetic: floor((kept + 35 + summary) / 4) >= floor((kept + folded) / 4).
_SHARED_RULE = [
    # a folded part of 5 characters (1 token) cannot fit the 35-byte header
    (5, 0, 1, True),
    (5, 3, 1, True),
    # 100 characters: 65 bytes is the first rejected summary when nothing is kept
    (100, 0, 64, False),
    (100, 0, 65, True),
    (100, 1, 64, True),  # one kept character rounds the saving away
    (100, 1, 63, False),
    (100, 3, 61, False),
    (100, 3, 62, True),
    (100, 4, 64, False),  # four kept characters are one whole token: as with none
    # 400 characters: 364 accepted and 365 rejected with nothing kept
    (400, 0, 364, False),
    (400, 0, 365, True),
    (400, 1, 364, True),
    (400, 2, 362, False),
    (400, 2, 363, True),
    (400, 3, 364, True),
    (400, 4, 364, False),
    # 4000 characters
    (4000, 0, 3964, False),
    (4000, 0, 3965, True),
    (4000, 2, 3964, True),
    (4000, 2, 3962, False),
]


@pytest.mark.parametrize(("folded", "kept", "summary", "rejected"), _SHARED_RULE)
async def test_the_rejection_is_decided_on_the_whole_view_like_linch_rust(
    folded: int, kept: int, summary: int, rejected: bool
) -> None:
    older = [_user("f" * folded)]
    recent = [_user("k" * kept)] if kept else []
    provider = _Provider(_events("s" * summary))
    call = summarize_with_provider(provider, "m", older, AbortContext(), kept=recent)
    if rejected:
        with pytest.raises(ProviderError, match="not smaller"):
            await call
    else:
        assert await call == "s" * summary
    assert (summary >= _summary_text_limit(older, recent)) is rejected


async def test_the_accounted_rejection_counts_the_header_too() -> None:
    for length, reason in [(364, None), (365, "not_smaller")]:
        provider = _Provider(_events("s" * length, usage=Usage(1, 1)))
        _, _, _, caller = _setup(provider=provider)
        result = await caller.text(
            logical_call_id="op",
            model="m",
            system=[SystemBlock("recap")],
            messages=_FOLDED_TOKENS_100,
            max_output_tokens=300,
            signal=AbortContext(),
            reject_text_bytes_at=_summary_text_limit(_FOLDED_TOKENS_100),
        )
        assert result.receipt.rejection_reason == reason


def test_the_folded_estimate_counts_content_not_the_serialization() -> None:
    folded = [
        Message("assistant", [ToolUseBlock("c1", "Read", {"path": "a.py"})]),
        Message("user", [ToolResultBlock("c1", "x" * 40)]),
        Message("user", [ToolResultBlock("c2", [TextBlock("y" * 8), ImageBlock({"url": "u"})])]),
    ]
    # Read (4) + {"path":"a.py"} (15) + 40 + 8 = 67 characters, one image.
    assert _estimate_history_tokens(folded) == 67 // 4 + 1000


async def test_the_accounted_summary_is_rejected_when_not_smaller() -> None:
    _, ledger, _, caller = _setup(provider=_Provider(_events("x" * 400, usage=Usage(11, 2))))
    result = await caller.text(
        logical_call_id="op",
        model="m",
        system=[SystemBlock("recap")],
        messages=[_user("short")],
        max_output_tokens=300,
        signal=AbortContext(),
        reject_text_bytes_at=40,
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


@pytest.mark.parametrize(
    ("strategy", "kept", "limit"),
    [
        (DefaultCompaction(), 10, 363),
        (DetailedCompaction(keep_recent_turns=2), 2, 363),
        (DetailedCompaction(keep_recent_turns=1), 1, 364),
    ],
    ids=["default-10", "detailed-2", "detailed-1"],
)
@pytest.mark.parametrize("below", [True, False])
async def test_a_strategy_counts_the_turns_it_keeps_in_the_rejection(
    strategy: Any, kept: int, limit: int, below: bool
) -> None:
    # 400 folded characters, one kept character a turn: the limit is 400 - kept - 35 rounded
    # as linch-rust rounds the whole view, not the 365 of an empty tail.
    messages = [_user("f" * 400), *[Message("assistant", [TextBlock("g")])] * kept]
    summary = limit - 1 if below else limit
    provider = _Provider(_events("s" * summary))
    ctx = CompactionContext(messages=messages, model="m", signal=AbortContext())
    if below:
        await strategy.compact(ctx, provider)
    else:
        with pytest.raises(ProviderError, match="not smaller"):
            await strategy.compact(ctx, provider)
