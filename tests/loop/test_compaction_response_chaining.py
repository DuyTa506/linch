"""Compaction must strip the OpenAI Responses ``response_id`` from its output.

If a compacted ``provider_view`` keeps a message carrying
``provider_metadata.openai_responses.response_id``, the Responses provider chains
the next request onto the server's stored, *uncompacted* history (see
``openai_responses.previous_response``) — the inserted summary at the head is
never sent and compaction is silently defeated.  The strip happens at the single
``_run_compaction_impl`` chokepoint so every strategy (built-in or custom) is
covered.  The helper is pure metadata surgery — no ``openai_responses`` import —
so ``test_compaction_module_has_no_openai_responses_import`` still holds.
"""

from __future__ import annotations

import pytest

from linch.abort import AbortContext
from linch.compaction import _run_compaction_impl, strip_response_chaining
from linch.types import Message, TextBlock


def _msg_with_response_id(rid: str, *, extra: dict | None = None) -> Message:
    responses = {"response_id": rid}
    meta: dict = {"openai_responses": responses}
    if extra:
        meta.update(extra)
    return Message(
        role="assistant",
        content=[TextBlock(text="hi")],
        provider_metadata=meta,
    )


def test_strip_removes_response_id_without_mutating_input():
    msgs = [_msg_with_response_id("resp_1"), _msg_with_response_id("resp_2")]
    out = strip_response_chaining(msgs)

    for m in out:
        meta = m.provider_metadata or {}
        assert "response_id" not in meta.get("openai_responses", {})

    # Originals are shared with full_history and the session store — never mutated.
    assert msgs[0].provider_metadata["openai_responses"]["response_id"] == "resp_1"
    assert msgs[1].provider_metadata["openai_responses"]["response_id"] == "resp_2"


def test_strip_preserves_sibling_metadata_and_drops_emptied_containers():
    # openai_responses keeps a sibling key; provider_metadata keeps a sibling key.
    m = _msg_with_response_id("r", extra={"other": 2})
    m.provider_metadata["openai_responses"]["foo"] = 1
    out = strip_response_chaining([m])[0]
    assert out.provider_metadata == {"openai_responses": {"foo": 1}, "other": 2}

    # response_id was the only key everywhere -> metadata collapses to None.
    m2 = _msg_with_response_id("r")
    out2 = strip_response_chaining([m2])[0]
    assert out2.provider_metadata is None


def test_message_without_response_id_returned_by_identity():
    m = Message(role="user", content=[TextBlock(text="x")])
    out = strip_response_chaining([m])
    assert out[0] is m


@pytest.mark.asyncio
async def test_chokepoint_strips_even_for_a_passthrough_strategy():
    """A custom strategy that returns recent messages verbatim still gets stripped."""

    class _PassthroughStrategy:
        id = "passthrough"

        async def compact(self, ctx, provider):
            return list(ctx.messages)  # response_id metadata intact

    class _FakeAgent:
        model = "model-x"
        provider = None
        token_estimator = None
        hooks = None
        compaction = None

    class _FakeSession:
        def __init__(self) -> None:
            self.provider_view = [_msg_with_response_id("resp_final")]
            self.active_run_id = "run-1"
            self.run_deps = None
            self.last_compaction_info = None

    session = _FakeSession()
    await _run_compaction_impl(session, _FakeAgent(), AbortContext(), _PassthroughStrategy())

    assert session.provider_view  # still has the message
    for m in session.provider_view:
        meta = m.provider_metadata or {}
        assert "response_id" not in meta.get("openai_responses", {})
