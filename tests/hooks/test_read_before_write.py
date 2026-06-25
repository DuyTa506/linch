from __future__ import annotations

from typing import Any


def _agent(provider: Any, tmp_path: Any, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.config import FeatureFlags
    from linch.sessions import InMemorySessionStore

    kwargs.setdefault("result_offload", None)
    return Agent(
        model="m",
        provider=provider,
        cwd=str(tmp_path),
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        features=FeatureFlags(skills=False, subagents=False, mcp=False),
        **kwargs,
    )


async def _collect(session: Any, prompt: str = "go") -> list[Any]:
    return [event async for event in session.run(prompt)]


def _tool_end(events: list[Any], name: str) -> Any:
    from linch import ToolCallEndEvent

    return next(
        event for event in events if isinstance(event, ToolCallEndEvent) and event.tool_name == name
    )


async def test_default_agent_installs_read_before_write_hook(tmp_path: Any) -> None:
    from linch import ReadBeforeWriteHook
    from linch.evals import ScriptedProvider, TextTurn

    agent = _agent(ScriptedProvider([TextTurn("done")]), tmp_path)

    assert any(isinstance(hook, ReadBeforeWriteHook) for hook in agent.hooks)


async def test_read_before_write_can_be_disabled(tmp_path: Any) -> None:
    from linch import ReadBeforeWriteHook
    from linch.evals import ScriptedProvider, TextTurn

    agent = _agent(
        ScriptedProvider([TextTurn("done")]),
        tmp_path,
        read_before_write=False,
    )

    assert not any(isinstance(hook, ReadBeforeWriteHook) for hook in agent.hooks)


async def test_workspace_edit_is_blocked_until_read(tmp_path: Any) -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="Edit",
                tool_input={"file_path": "a.txt", "old_string": "hello", "new_string": "bye"},
            ),
            TextTurn("done"),
        ]
    )

    events = await _collect(await _agent(provider, tmp_path).session())
    end = _tool_end(events, "Edit")

    assert end.is_error
    assert end.result == "Error: You must read this file before editing it."
    assert target.read_text(encoding="utf-8") == "hello"


async def test_workspace_read_allows_later_edit(tmp_path: Any) -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="Read", tool_input={"file_path": "a.txt"}, tool_id="r1"),
            ToolUseTurn(
                tool_name="Edit",
                tool_input={"file_path": "a.txt", "old_string": "hello", "new_string": "bye"},
                tool_id="e1",
            ),
            TextTurn("done"),
        ]
    )

    events = await _collect(await _agent(provider, tmp_path).session())
    end = _tool_end(events, "Edit")

    assert not end.is_error
    assert target.read_text(encoding="utf-8") == "bye"


async def test_virtual_edit_file_is_blocked_until_read_file(tmp_path: Any) -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.filesystem import StateFileBackend

    backend = StateFileBackend({"/note.txt": "alpha"})
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="edit_file",
                tool_input={"path": "/note.txt", "old_string": "alpha", "new_string": "beta"},
            ),
            TextTurn("done"),
        ]
    )

    events = await _collect(await _agent(provider, tmp_path, filesystem=backend).session())
    end = _tool_end(events, "edit_file")

    assert end.is_error
    assert end.result == "Error: You must read this file before editing it."
    assert await backend.read("/note.txt") == "alpha"


async def test_virtual_read_file_allows_later_edit_file(tmp_path: Any) -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.filesystem import StateFileBackend

    backend = StateFileBackend({"/note.txt": "alpha"})
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="read_file", tool_input={"path": "/note.txt"}, tool_id="r1"),
            ToolUseTurn(
                tool_name="edit_file",
                tool_input={"path": "/note.txt", "old_string": "alpha", "new_string": "beta"},
                tool_id="e1",
            ),
            TextTurn("done"),
        ]
    )

    events = await _collect(await _agent(provider, tmp_path, filesystem=backend).session())
    end = _tool_end(events, "edit_file")

    assert not end.is_error
    assert await backend.read("/note.txt") == "beta"


async def test_virtual_edit_file_opt_out_keeps_existing_behavior(tmp_path: Any) -> None:
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.filesystem import StateFileBackend

    backend = StateFileBackend({"/note.txt": "alpha"})
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="edit_file",
                tool_input={"path": "/note.txt", "old_string": "alpha", "new_string": "beta"},
            ),
            TextTurn("done"),
        ]
    )

    events = await _collect(
        await _agent(
            provider,
            tmp_path,
            filesystem=backend,
            read_before_write=False,
        ).session()
    )
    end = _tool_end(events, "edit_file")

    assert not end.is_error
    assert await backend.read("/note.txt") == "beta"


async def test_cached_read_still_marks_file_as_read(tmp_path: Any) -> None:
    from linch import ToolCacheConfig
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="Read", tool_input={"file_path": "a.txt"}, tool_id="r1"),
            ToolUseTurn(tool_name="Read", tool_input={"file_path": "a.txt"}, tool_id="r2"),
            ToolUseTurn(
                tool_name="Edit",
                tool_input={"file_path": "a.txt", "old_string": "hello", "new_string": "bye"},
                tool_id="e1",
            ),
            TextTurn("done"),
        ]
    )
    agent = _agent(provider, tmp_path, tool_cache=ToolCacheConfig())

    class ClearAfterFirstRead:
        def __init__(self) -> None:
            self.seen = 0

        def on_post_tool_use(self, ctx: Any) -> None:
            if ctx.tool_name != "Read":
                return None
            self.seen += 1
            if self.seen == 1:
                ctx.session.file_read_tracker.clear()
            return None

    agent.hooks = [agent.hooks[0], ClearAfterFirstRead(), *agent.hooks[1:]]

    events = await _collect(await agent.session())
    end = _tool_end(events, "Edit")

    assert not end.is_error
    assert target.read_text(encoding="utf-8") == "bye"
