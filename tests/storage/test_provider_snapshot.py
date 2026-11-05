"""Durable compacted provider views.

Built-in session stores can persist a compacted ``provider_view`` plus the
message ``seq`` watermark it covers; on reload the view is restored and messages
appended after that watermark are re-appended. Custom stores lacking the optional
methods keep rebuilding the view from full history. Compaction never mutates or
removes the append-only message log.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from linch.sessions import InMemorySessionStore
from linch.sessions.store import (
    ProviderViewSnapshot,
    SessionRecord,
    StoredMessage,
    snapshot_from_dict,
)
from linch.types import Message, TextBlock
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


def _msg(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _texts(messages: list[Message]) -> list[str]:
    return [b.text for m in messages for b in m.content if isinstance(b, TextBlock)]


def _snapshot_agent(store: Any) -> Any:
    """Build the inert agent used by store-backed snapshot tests."""
    return make_agent(
        ScriptedProvider([TextTurn("ok")], context_window=100_000),
        session_store=store,
        run_store=None,
        model="gpt-5",
        result_offload=None,
    )


# ── store-level snapshot round-trip ──────────────────────────────────────────


async def test_inmemory_snapshot_roundtrip_and_delete() -> None:
    store = InMemorySessionStore()
    await store.create(id="s1")
    assert await store.load_provider_snapshot("s1") is None

    snap = ProviderViewSnapshot(provider_view=[_msg("summary"), _msg("recent")], covers_seq=7)
    await store.save_provider_snapshot("s1", snap)

    got = await store.load_provider_snapshot("s1")
    assert got is not None
    assert got.covers_seq == 7
    assert _texts(got.provider_view) == ["summary", "recent"]

    await store.delete("s1")
    assert await store.load_provider_snapshot("s1") is None


# ── wire-format contract ─────────────────────────────────────────────────────


def test_snapshot_from_dict_defaults_missing_keys() -> None:
    from linch._wire import UnsupportedWireVersionError

    with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
        snapshot_from_dict({})


# ── load-path reconstruction (the acceptance criterion) ──────────────────────


class _MinimalStore:
    """A SessionStore with no snapshot methods (custom-store fallback path)."""

    def __init__(self) -> None:
        self._msgs: dict[str, list[StoredMessage]] = {}
        self._recs: dict[str, SessionRecord] = {}

    async def create(self, *, id: str | None = None, meta: Any = None) -> SessionRecord:
        sid = id or "auto"
        rec = self._recs.get(sid) or SessionRecord(id=sid, created_at="t", updated_at="t")
        self._recs[sid] = rec
        self._msgs.setdefault(sid, [])
        return rec

    async def load_messages(self, id: str) -> list[StoredMessage]:
        return list(self._msgs.get(id, []))

    def _seed(self, id: str, messages: list[Message]) -> None:
        bucket = self._msgs.setdefault(id, [])
        for m in messages:
            bucket.append(StoredMessage(seq=len(bucket) + 1, appended_at="t", message=m))


# ── save seam: the runner persists the just-compacted view ───────────────────


# ── snapshot robustness: the cache never corrupts the reloaded view (WS2.4) ──


async def test_reload_falls_back_when_snapshot_loader_raises() -> None:
    class _RaisingStore(InMemorySessionStore):
        async def load_provider_snapshot(self, id: str) -> Any:
            raise RuntimeError("corrupt snapshot blob")

    store = _RaisingStore()
    agent = _snapshot_agent(store)
    session = await agent.session(id="s1")
    await session.append([_msg("m1"), _msg("m2")])

    reloaded = await _snapshot_agent(store).session(id="s1")
    # A raising loader must not abort the reload — rebuild from full history.
    assert _texts(reloaded.provider_view) == ["m1", "m2"]


async def test_reload_propagates_incompatible_snapshot_version() -> None:
    from linch._wire import UnsupportedWireVersionError

    class _IncompatibleStore(InMemorySessionStore):
        raise_incompatible = False

        async def load_provider_snapshot(self, id: str) -> Any:
            if not self.raise_incompatible:
                return await super().load_provider_snapshot(id)
            raise UnsupportedWireVersionError("state was written by Linch < 3.0; start a new run")

    store = _IncompatibleStore()
    agent = _snapshot_agent(store)
    session = await agent.session(id="s1")
    await session.append([_msg("m1")])
    store.raise_incompatible = True

    with pytest.raises(UnsupportedWireVersionError, match="start a new run"):
        await _snapshot_agent(store).session(id="s1")


@pytest.mark.parametrize("watermark", [-5], ids=["negative"])
async def test_reload_falls_back_on_invalid_watermark(watermark: int) -> None:
    store = InMemorySessionStore()
    agent = _snapshot_agent(store)
    session = await agent.session(id="s1")
    await session.append([_msg("m1"), _msg("m2"), _msg("m3")])  # seq 1..3
    await store.save_provider_snapshot(
        "s1", ProviderViewSnapshot(provider_view=[_msg("SUMMARY")], covers_seq=watermark)
    )

    reloaded = await _snapshot_agent(store).session(id="s1")
    assert _texts(reloaded.provider_view) == ["m1", "m2", "m3"]


async def test_snapshot_caching_disabled_when_store_seqs_non_monotonic() -> None:
    from linch.loop.runner import _maybe_save_provider_snapshot

    class _NonMonotonicStore(InMemorySessionStore):
        async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
            # Return a fixed, non-increasing seq to simulate a misbehaving store.
            return [StoredMessage(seq=1, appended_at="t", message=m) for m in messages]

    store = _NonMonotonicStore()
    session = await _snapshot_agent(store).session(id="s1")
    await session.append([_msg("a")])
    await session.append([_msg("b")])  # seq 1 again → non-increasing
    assert session._seq_cacheable is False

    session.session_log.record_projection([_msg("SUM")], reason="test-compaction")
    await _maybe_save_provider_snapshot(session)
    # Caching disabled: no snapshot persisted (watermark can't be trusted).
    assert await store.load_provider_snapshot("s1") is None


# ── end-to-end: a real compacting run reloads the same compacted view ────────


class _BigTool:
    name = "BigTool"
    description = "Returns a large string to grow the context."
    input_schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    parallel = False
    scope: Any = "read"

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def summarize(self, input: dict[str, Any]) -> str:
        return "BigTool"

    async def execute(self, input: dict[str, Any], ctx: Any) -> Any:
        from linch.tools import ToolResult

        return ToolResult(content="x" * int(input.get("n", 0)))


class _LadderProvider:
    id = "fake"

    def __init__(self, behaviors: list[tuple[str, int]], window: int) -> None:
        self.behaviors = behaviors
        self.window = window
        self.calls = 0
        self.summarize_calls = 0

    def context_window(self, model: str) -> int:
        return self.window

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        if not req.tools:  # DetailedCompaction's summary call has no tools
            self.summarize_calls += 1
            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "summary of earlier work"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
            return
        behavior, n = self.behaviors[self.calls]
        self.calls += 1
        yield {"type": "message_start", "model": req.model}
        if behavior == "tool":
            yield {"type": "tool_use_start", "id": f"call_{self.calls}", "name": "BigTool"}
            yield {
                "type": "tool_use_input_delta",
                "id": f"call_{self.calls}",
                "json_delta": f'{{"n":{n}}}',
            }
            yield {"type": "tool_use_end", "id": f"call_{self.calls}"}
            stop = "tool_use"
        else:
            yield {"type": "text_delta", "text": "done"}
            stop = "end_turn"
        yield {"type": "message_end", "stop_reason": stop, "usage": Usage(input_tokens=10)}
