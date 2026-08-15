"""Durable compacted provider views (ROADMAP Phase 3.3).

Built-in session stores can persist a compacted ``provider_view`` plus the
message ``seq`` watermark it covers; on reload the view is restored and messages
appended after that watermark are re-appended. Custom stores lacking the optional
methods keep rebuilding the view from full history. Compaction never mutates or
removes the append-only message log.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from linch.sessions import InMemorySessionStore, SqliteSessionStore
from linch.sessions.store import (
    SNAPSHOT_SCHEMA_VERSION,
    ProviderViewSnapshot,
    SessionRecord,
    StoredMessage,
    snapshot_from_dict,
    snapshot_to_dict,
)
from linch.types import Message, TextBlock, message_to_dict


def _msg(text: str) -> Message:
    return Message(role="user", content=[TextBlock(text=text)])


def _texts(messages: list[Message]) -> list[str]:
    return [b.text for m in messages for b in m.content if isinstance(b, TextBlock)]


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


async def test_sqlite_snapshot_roundtrip_persists_across_reopen(tmp_path: Any) -> None:
    path = tmp_path / "sessions.db"
    store = SqliteSessionStore(path)
    try:
        await store.create(id="s1")
        await store.save_provider_snapshot(
            "s1", ProviderViewSnapshot(provider_view=[_msg("sum")], covers_seq=3)
        )
        # Overwrite (upsert) with a newer snapshot.
        await store.save_provider_snapshot(
            "s1", ProviderViewSnapshot(provider_view=[_msg("sum2"), _msg("x")], covers_seq=5)
        )
    finally:
        await store.close()

    reopened = SqliteSessionStore(path)
    try:
        got = await reopened.load_provider_snapshot("s1")
        assert got is not None
        assert got.covers_seq == 5
        assert _texts(got.provider_view) == ["sum2", "x"]
        assert await reopened.load_provider_snapshot("missing") is None
    finally:
        await reopened.close()


# ── wire-format tolerance (forward-compatible) ───────────────────────────────


def test_snapshot_to_dict_stamps_schema_version() -> None:
    raw = snapshot_to_dict(ProviderViewSnapshot(provider_view=[_msg("a")], covers_seq=2))
    assert raw["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert raw["covers_seq"] == 2
    assert raw["provider_view"] == [message_to_dict(_msg("a"))]


def test_snapshot_from_dict_tolerates_future_version_and_unknown_keys() -> None:
    raw = snapshot_to_dict(ProviderViewSnapshot(provider_view=[_msg("a")], covers_seq=2))
    raw["schema_version"] = SNAPSHOT_SCHEMA_VERSION + 99
    raw["some_future_field"] = {"nested": True}
    restored = snapshot_from_dict(raw)
    assert restored.covers_seq == 2
    assert _texts(restored.provider_view) == ["a"]


def test_snapshot_from_dict_defaults_missing_keys() -> None:
    restored = snapshot_from_dict({})
    assert restored.covers_seq == 0
    assert restored.provider_view == []


# ── load-path reconstruction (the acceptance criterion) ──────────────────────


def _agent(store: Any, provider: Any):
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.tools.registry import empty_tools

    return Agent(
        model="gpt-5",
        provider=provider,
        session_store=store,
        permissions={"mode": "skip-dangerous"},
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        result_offload=None,
        cwd=".",
        tools=empty_tools(),
    )


class _Provider:
    id = "p"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "ok"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


async def test_reload_restores_compacted_view_plus_newer_messages() -> None:
    store = InMemorySessionStore()
    agent = _agent(store, _Provider())
    session = await agent.session(id="s1")
    await session.append([_msg("m1"), _msg("m2"), _msg("m3")])  # seq 1..3

    # A compaction that replaced the first messages with a summary + the last one.
    await store.save_provider_snapshot(
        "s1",
        ProviderViewSnapshot(provider_view=[_msg("SUMMARY"), _msg("m3")], covers_seq=3),
    )
    await session.append([_msg("m4")])  # seq 4, newer than the snapshot watermark

    reloaded = await _agent(store, _Provider()).session(id="s1")

    assert _texts(reloaded.full_history) == ["m1", "m2", "m3", "m4"]  # audit log intact
    assert _texts(reloaded.provider_view) == ["SUMMARY", "m3", "m4"]  # view + newer msgs
    assert len(reloaded.provider_view) < len(reloaded.full_history)


async def test_reload_without_snapshot_rebuilds_from_full_history() -> None:
    store = InMemorySessionStore()
    agent = _agent(store, _Provider())
    session = await agent.session(id="s1")
    await session.append([_msg("m1"), _msg("m2")])

    reloaded = await _agent(store, _Provider()).session(id="s1")
    assert _texts(reloaded.provider_view) == ["m1", "m2"]
    assert reloaded.provider_view is not reloaded.full_history
    assert _texts(reloaded.provider_view) == _texts(reloaded.full_history)


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


async def test_custom_store_without_snapshot_methods_falls_back() -> None:
    store = _MinimalStore()
    await store.create(id="s1")
    store._seed("s1", [_msg("m1"), _msg("m2")])
    assert not hasattr(store, "load_provider_snapshot")

    reloaded = await _agent(store, _Provider()).session(id="s1")
    assert _texts(reloaded.provider_view) == ["m1", "m2"]  # rebuilt from full history


# ── save seam: the runner persists the just-compacted view ───────────────────


async def test_maybe_save_provider_snapshot_persists_current_view() -> None:
    from linch.loop.runner import _maybe_save_provider_snapshot

    store = InMemorySessionStore()
    session = await _agent(store, _Provider()).session(id="s1")
    await session.append([_msg("a"), _msg("b")])  # full_history == 2
    session.session_log.record_projection([_msg("SUM")], reason="test-compaction")

    await _maybe_save_provider_snapshot(session)

    snap = await store.load_provider_snapshot("s1")
    assert snap is not None
    assert snap.covers_seq == 2  # == len(full_history) at snapshot time
    assert _texts(snap.provider_view) == ["SUM"]


# ── snapshot robustness: the cache never corrupts the reloaded view (WS2.4) ──


async def test_reload_falls_back_when_snapshot_loader_raises() -> None:
    class _RaisingStore(InMemorySessionStore):
        async def load_provider_snapshot(self, id: str) -> Any:
            raise RuntimeError("corrupt snapshot blob")

    store = _RaisingStore()
    agent = _agent(store, _Provider())
    session = await agent.session(id="s1")
    await session.append([_msg("m1"), _msg("m2")])

    reloaded = await _agent(store, _Provider()).session(id="s1")
    # A raising loader must not abort the reload — rebuild from full history.
    assert _texts(reloaded.provider_view) == ["m1", "m2"]


async def test_reload_falls_back_on_out_of_range_watermark() -> None:
    store = InMemorySessionStore()
    agent = _agent(store, _Provider())
    session = await agent.session(id="s1")
    await session.append([_msg("m1"), _msg("m2"), _msg("m3")])  # seq 1..3
    # A watermark past the last stored seq would drop real messages from the view.
    await store.save_provider_snapshot(
        "s1", ProviderViewSnapshot(provider_view=[_msg("SUMMARY")], covers_seq=999)
    )

    reloaded = await _agent(store, _Provider()).session(id="s1")
    assert _texts(reloaded.provider_view) == ["m1", "m2", "m3"]  # fell back to full history


async def test_reload_falls_back_on_negative_watermark() -> None:
    store = InMemorySessionStore()
    agent = _agent(store, _Provider())
    session = await agent.session(id="s1")
    await session.append([_msg("m1"), _msg("m2")])
    await store.save_provider_snapshot(
        "s1", ProviderViewSnapshot(provider_view=[_msg("SUMMARY")], covers_seq=-5)
    )

    reloaded = await _agent(store, _Provider()).session(id="s1")
    assert _texts(reloaded.provider_view) == ["m1", "m2"]


async def test_snapshot_caching_disabled_when_store_seqs_non_monotonic() -> None:
    from linch.loop.runner import _maybe_save_provider_snapshot

    class _NonMonotonicStore(InMemorySessionStore):
        async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
            # Return a fixed, non-increasing seq to simulate a misbehaving store.
            return [StoredMessage(seq=1, appended_at="t", message=m) for m in messages]

    store = _NonMonotonicStore()
    session = await _agent(store, _Provider()).session(id="s1")
    await session.append([_msg("a")])
    await session.append([_msg("b")])  # seq 1 again → non-increasing
    assert session._seq_cacheable is False

    session.session_log.record_projection([_msg("SUM")], reason="test-compaction")
    await _maybe_save_provider_snapshot(session)
    # Caching disabled: no snapshot persisted (watermark can't be trusted).
    assert await store.load_provider_snapshot("s1") is None


async def test_snapshot_watermark_tolerates_gapped_seqs() -> None:
    from linch.loop.runner import _maybe_save_provider_snapshot

    class _GappedStore(InMemorySessionStore):
        def __init__(self) -> None:
            super().__init__()
            self._next = 10

        async def append_messages(self, id: str, messages: list[Message]) -> list[StoredMessage]:
            out: list[StoredMessage] = []
            for m in messages:
                out.append(StoredMessage(seq=self._next, appended_at="t", message=m))
                self._next += 10  # increasing, but gapped
            return out

    store = _GappedStore()
    session = await _agent(store, _Provider()).session(id="s1")
    await session.append([_msg("a"), _msg("b")])  # seq 10, 20
    assert session._seq_cacheable is True
    assert session._last_seq == 20

    session.session_log.record_projection([_msg("SUM")], reason="test-compaction")
    await _maybe_save_provider_snapshot(session)
    snap = await store.load_provider_snapshot("s1")
    assert snap is not None
    assert snap.covers_seq == 20  # real watermark, not len(full_history)


async def test_maybe_save_provider_snapshot_is_noop_without_store_support() -> None:
    from types import SimpleNamespace

    from linch.loop.runner import _maybe_save_provider_snapshot

    # A store object with no save_provider_snapshot: helper returns without raising.
    stub = SimpleNamespace(
        store=object(), provider_view=[_msg("x")], full_history=[_msg("x")], id="s"
    )
    await _maybe_save_provider_snapshot(stub)  # no raise


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


def _char_estimator(messages: list[Any], model: str) -> int:
    from linch.types import TextBlock as _TB
    from linch.types import ToolResultBlock

    total = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, _TB):
                total += len(block.text)
            elif isinstance(block, ToolResultBlock) and isinstance(block.content, str):
                total += len(block.content)
    return total


async def test_compacted_run_reloads_identical_provider_view() -> None:
    from linch import Agent, DetailedCompaction
    from linch.compaction import CompactionLadder
    from linch.config import FeatureFlags
    from linch.tools import ToolRegistry

    def _tools() -> Any:
        registry = ToolRegistry()
        registry.register(_BigTool())
        return registry

    no_skills = FeatureFlags(skills=False, subagents=False, mcp=False)
    store = InMemorySessionStore()
    provider = _LadderProvider([("tool", 500), ("tool", 9_000), ("text", 0)], window=10_000)
    agent = Agent(
        model="gpt-5",
        provider=provider,
        session_store=store,
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=_tools(),
        features=no_skills,
        compaction_ladder=CompactionLadder(keep_recent_turns=1),
        compaction=DetailedCompaction(keep_recent_turns=1),
        token_estimator=_char_estimator,
        max_output_tokens=10,
    )
    session = await agent.session(id="s1")
    events = [event async for event in session.run("go")]

    assert [e.type for e in events if e.type == "compaction"], "compaction did not fire"
    assert provider.summarize_calls == 1  # a real (summary) compaction occurred
    assert await store.load_provider_snapshot("s1") is not None  # snapshot persisted

    # Reload with a fresh agent sharing the store: the compacted view is restored
    # exactly (summary + newer messages), and the audit history is complete.
    reloaded = await Agent(
        model="gpt-5",
        provider=provider,
        session_store=store,
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        tools=_tools(),
        features=no_skills,
    ).session(id="s1")

    assert [message_to_dict(m) for m in reloaded.provider_view] == [
        message_to_dict(m) for m in session.provider_view
    ]
    assert len(reloaded.provider_view) < len(reloaded.full_history)
    assert any("summary of earlier work" in t for t in _texts(reloaded.provider_view))
