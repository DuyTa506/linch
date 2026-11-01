from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from linch import (
    FeatureFlags,
    RunOptions,
    ScopedFileBackend,
    StateFileBackend,
    TextBlock,
    ToolRegistry,
    Usage,
)
from linch.filesystem.tools import filesystem_tools
from tests.support.agents import make_agent
from tests.support.tools import StubTool


@pytest.mark.parametrize("storage", ["state", "disk"])
async def test_scoped_backend_maps_paths_and_lists_scope_relative(storage, tmp_path):
    from linch import DiskFileBackend, ScopedFileBackend, StateFileBackend

    inner = StateFileBackend() if storage == "state" else DiskFileBackend(tmp_path)
    scoped = ScopedFileBackend(inner, "/tenants/alice/")
    await inner.write("/tenants/alice-other/secret", "outside")
    await scoped.write("//memories/./fact.md", "first\nsecond\nsecond")

    assert await inner.read("/tenants/alice/memories/fact.md") == "first\nsecond\nsecond"
    assert await scoped.read("memories/fact.md", offset=2, limit=1) == "second"
    assert await scoped.exists("memories/fact.md")
    assert await scoped.ls() == ["/memories/fact.md"]
    assert await scoped.ls("/memories") == ["/memories/fact.md"]
    assert await scoped.edit("memories/fact.md", "second", "updated", replace_all=True) == 2
    await scoped.delete("/memories/fact.md")
    assert not await scoped.exists("/memories/fact.md")
    assert await inner.read("/tenants/alice-other/secret") == "outside"


@pytest.mark.parametrize(
    "path", ["../bob/secret", "/../../secret", r"..\bob\secret", "x/../secret", "a\x00b"]
)
async def test_scoped_backend_rejects_unsafe_paths_for_every_operation(path):
    from linch import ScopedFileBackend, StateFileBackend

    inner = StateFileBackend({"/tenants/bob/secret": "private"})
    scoped = ScopedFileBackend(inner, "/tenants/alice")
    operations = [
        scoped.read(path),
        scoped.write(path, "overwrite"),
        scoped.ls(path),
        scoped.edit(path, "private", "changed"),
        scoped.exists(path),
        scoped.delete(path),
    ]
    results = await asyncio.gather(*operations, return_exceptions=True)
    assert all(isinstance(result, ValueError) for result in results)
    assert await inner.read("/tenants/bob/secret") == "private"
    assert await inner.ls() == ["/tenants/bob/secret"]


async def test_scoped_backend_absolute_paths_are_inside_scope():
    from linch import ScopedFileBackend, StateFileBackend

    inner = StateFileBackend({"/tenants/bob/secret": "private"})
    scoped = ScopedFileBackend(inner, "/tenants/alice")
    await scoped.write("/tenants/bob/secret", "local")
    assert await scoped.read("/tenants/bob/secret") == "local"
    assert await inner.read("/tenants/alice/tenants/bob/secret") == "local"
    assert await inner.read("/tenants/bob/secret") == "private"


async def test_scoped_backend_filters_overbroad_inner_listings():
    from linch import ScopedFileBackend, StateFileBackend

    class OverbroadBackend(StateFileBackend):
        async def ls(self, prefix=""):
            return [
                "/tenants/alice/memories/fact.md",
                "/tenants/alice-other/secret",
                "/tenants/bob/secret",
                "/tenants/alice/../bob/secret",
            ]

    scoped = ScopedFileBackend(OverbroadBackend(), "/tenants/alice")
    assert await scoped.ls() == ["/memories/fact.md"]


async def test_concurrent_runs_one_agent_cannot_access_other_file_scopes():
    class ProbeProvider:
        id = "scope-probe"

        def context_window(self, model):
            return 128_000

        async def stream(self, req):
            yield {"type": "message_start", "model": req.model}
            who = next(
                block.text
                for message in req.messages
                for block in message.content
                if isinstance(block, TextBlock) and block.text in {"alice", "bob"}
            )
            other = "bob" if who == "alice" else "alice"
            if any(
                getattr(block, "type", "") == "tool_result"
                for message in req.messages
                for block in message.content
            ):
                yield {"type": "text_delta", "text": "done"}
                yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}
                return
            attempts = (
                f"/tenants/{other}/{other}.md",
                f"../{other}/{other}.md",
                f"/../../tenants/{other}/{other}.md",
                rf"..\{other}\{other}.md",
            )
            calls: list[tuple[str, dict[str, Any]]] = [
                ("read_file", {"path": f"/{who}.md"}),
                ("ls", {}),
                ("write_file", {"path": "/own.md", "content": who}),
            ] + [
                item
                for path in attempts
                for item in (
                    ("read_file", {"path": path}),
                    ("ls", {"prefix": path}),
                    ("edit_file", {"path": path, "old_string": "secret", "new_string": "changed"}),
                    ("DeleteForTest", {"path": path}),
                    ("write_file", {"path": path, "content": "overwrite"}),
                )
            ]
            await asyncio.sleep(0)
            for index, (name, raw) in enumerate(calls):
                call_id = f"{who}-{index}"
                yield {"type": "tool_use_start", "id": call_id, "name": name}
                yield {"type": "tool_use_input_delta", "id": call_id, "json_delta": json.dumps(raw)}
                yield {"type": "tool_use_end", "id": call_id}
            yield {"type": "message_end", "stop_reason": "tool_use", "usage": Usage()}

    inner = StateFileBackend(
        {
            "/tenants/alice/alice.md": "alice secret",
            "/tenants/bob/bob.md": "bob secret",
        }
    )

    async def delete(_input: dict[str, str], ctx: Any) -> None:
        await ctx.deps["filesystem"].delete(_input["path"])

    registry = ToolRegistry()
    for tool in (
        *filesystem_tools(),
        StubTool("DeleteForTest", scope="write", on_execute=delete, output="deleted"),
    ):
        registry.register(tool)  # type: ignore[arg-type]
    agent = make_agent(
        ProbeProvider(),
        model="fake",
        tools=registry,
        filesystem=inner,
        features=FeatureFlags(filesystem=True),
        max_turns=3,
    )
    try:
        sessions = [await agent.session() for _ in range(2)]

        async def run(session, who):
            return [
                event
                async for event in session.run(
                    who,
                    RunOptions(deps={"filesystem": ScopedFileBackend(inner, f"/tenants/{who}")}),
                )
            ]

        results = await asyncio.gather(
            *(run(session, who) for session, who in zip(sessions, ["alice", "bob"], strict=True))
        )
        for session, events, who in zip(sessions, results, ["alice", "bob"], strict=True):
            assert events[-1].type == "result" and events[-1].subtype == "success", [
                event.error for event in events if event.type == "error"
            ]
            contents = [
                block.content
                for message in session.provider_view
                for block in message.content
                if getattr(block, "type", "") == "tool_result"
            ]
            other = "bob" if who == "alice" else "alice"
            assert f"{who} secret" in contents
            assert all(f"{other} secret" not in content for content in contents)
            assert f"/{who}.md" in contents
            assert await inner.read(f"/tenants/{who}/{who}.md") == f"{who} secret"
            assert await inner.read(f"/tenants/{who}/own.md") == who
    finally:
        await agent.close()
