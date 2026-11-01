from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.support.agents import make_agent


def _turn(backend, run_id="run-1"):
    from linch import ContextBuildTurn

    session = SimpleNamespace(active_run_id=run_id, filesystem=None, agent=SimpleNamespace())
    return ContextBuildTurn(
        session=session,
        messages=[],
        turn_index=0,
        deps={"filesystem": backend},
        model="fake",
        tools=None,
    )


async def test_file_memory_index_is_bounded_and_reports_truncation():
    from linch import FileMemoryContextBuilder, StateFileBackend

    backend = StateFileBackend(
        {"/memories/MEMORY.md": "\n".join(f"fact-{i}.md" for i in range(201))}
    )
    result = await FileMemoryContextBuilder().build(_turn(backend))
    text = "\n".join(block.text for block in result.system_blocks)
    assert "fact-199.md" in text
    assert "fact-200.md" not in text
    assert "truncated" in text.lower()
    assert "200" in text
    assert "/memories/" in text
    assert "one file per fact" in text
    assert "one-line-per-file index" in text
    assert "update or delete stale facts" in text


async def test_file_memory_missing_index_is_cached_only_for_current_run():
    from linch import FileMemoryContextBuilder, StateFileBackend

    backend = StateFileBackend()
    builder = FileMemoryContextBuilder()
    turn = _turn(backend)
    missing = await builder.build(turn)
    assert len(missing.system_blocks) == 1
    await backend.write("/memories/MEMORY.md", "new-fact.md: a new fact")
    same_run = await builder.build(turn)
    assert same_run.system_blocks == missing.system_blocks
    turn.session.active_run_id = "run-2"
    new_run = await builder.build(turn)
    assert any("new-fact.md" in block.text for block in new_run.system_blocks)


async def test_file_memory_read_errors_are_not_treated_as_missing_index():
    from linch import FileMemoryContextBuilder, StateFileBackend

    class BrokenBackend(StateFileBackend):
        async def read(self, path, *, offset=0, limit=None):
            raise OSError("store unavailable")

    with pytest.raises(OSError, match="store unavailable"):
        await FileMemoryContextBuilder().build(_turn(BrokenBackend()))


async def test_file_memory_releases_cache_when_another_builder_cleanup_fails():
    from linch import ContextInjectionHook, FileMemoryContextBuilder, StateFileBackend

    class BrokenCleanup:
        async def build(self, turn):
            from linch import ContextBuildResult

            return ContextBuildResult()

        def on_agent_stop(self, ctx):
            raise ValueError("cleanup failed")

    builder = FileMemoryContextBuilder()
    await builder.build(_turn(StateFileBackend()))
    hook = ContextInjectionHook([BrokenCleanup(), builder])
    with pytest.raises(ExceptionGroup, match="cleanup"):
        await hook.on_agent_stop(SimpleNamespace(run_id="run-1"))
    assert builder._by_run == {}


@pytest.mark.parametrize("fallback", ["session", "execution"])
async def test_file_memory_uses_same_backend_precedence_as_file_tools(fallback):
    from linch import FileMemoryContextBuilder, StateFileBackend

    default = StateFileBackend({"/memories/MEMORY.md": "default-fact"})
    scoped = StateFileBackend({"/memories/MEMORY.md": "run-fact"})
    turn = _turn(scoped)
    turn.session.filesystem = default if fallback == "session" else None
    turn.session.agent.execution_backend = SimpleNamespace(fs=default)
    result = await FileMemoryContextBuilder().build(turn)
    assert any("run-fact" in block.text for block in result.system_blocks)
    assert all("default-fact" not in block.text for block in result.system_blocks)
    turn.deps = None
    turn.session.active_run_id = "run-2"
    result = await FileMemoryContextBuilder().build(turn)
    assert any("default-fact" in block.text for block in result.system_blocks)


async def test_file_memory_concurrent_runs_keep_prefix_stable_and_release_cache():
    from linch import (
        ContextBuildResult,
        ContextInjectionHook,
        FeatureFlags,
        FileMemoryContextBuilder,
        RunOptions,
        ScopedFileBackend,
        StateFileBackend,
        TextBlock,
        Usage,
    )

    class CountingBackend(StateFileBackend):
        def __init__(self):
            super().__init__()
            self.reads = []

        async def read(self, path, *, offset=0, limit=None):
            self.reads.append((path, limit))
            return await super().read(path, offset=offset, limit=limit)

    class OtherBuilder:
        async def build(self, turn):
            return ContextBuildResult()

    class Provider:
        id = "file-memory-test"

        def __init__(self):
            self.prefixes = {"alice": [], "bob": []}

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            who = next(
                block.text
                for message in req.messages
                for block in message.content
                if isinstance(block, TextBlock) and block.text in self.prefixes
            )
            self.prefixes[who].append([block.text for block in req.system])
            if any(
                getattr(block, "type", "") == "tool_result"
                for message in req.messages
                for block in message.content
            ):
                yield {"type": "text_delta", "text": "done"}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
                return
            yield {"type": "tool_use_start", "id": "update", "name": "write_file"}
            yield {
                "type": "tool_use_input_delta",
                "id": "update",
                "json_delta": json.dumps(
                    {"path": "/memories/MEMORY.md", "content": f"{who}-updated.md"}
                ),
            }
            yield {"type": "tool_use_end", "id": "update"}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}

    backend = CountingBackend()
    for who in ("alice", "bob"):
        await backend.write(f"/tenants/{who}/memories/MEMORY.md", f"{who}-original.md")
    builder = FileMemoryContextBuilder()
    provider = Provider()
    agent = make_agent(
        provider,
        model="fake",
        features=FeatureFlags(filesystem=True),
        hooks=[ContextInjectionHook([OtherBuilder(), builder])],
        max_turns=3,
        run_store=None,
    )
    try:
        sessions = [await agent.session() for _ in range(2)]

        async def run(session, who):
            return [
                event
                async for event in session.run(
                    who,
                    RunOptions(deps={"filesystem": ScopedFileBackend(backend, f"/tenants/{who}")}),
                )
            ]

        results = await asyncio.gather(
            *(run(session, who) for session, who in zip(sessions, ["alice", "bob"], strict=True))
        )
        assert all(events[-1].subtype == "success" for events in results), [
            event.error for events in results for event in events if event.type == "error"
        ]
        assert len(backend.reads) == 2
        assert all(limit == 201 for _, limit in backend.reads)
        for who, prefixes in provider.prefixes.items():
            assert len(prefixes) == 2
            assert prefixes[0] == prefixes[1]
            text = "\n".join(prefixes[0])
            assert f"{who}-original.md" in text
            assert "updated.md" not in text
            other = "bob" if who == "alice" else "alice"
            assert f"{other}-original.md" not in text
        assert builder._by_run == {}
        await run(sessions[0], "alice")
        assert len(backend.reads) == 3
        assert any("alice-updated.md" in text for text in provider.prefixes["alice"][-1])
    finally:
        await agent.close()
