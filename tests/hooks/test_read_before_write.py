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
    # Workspace Edit is gated by the builtin Edit tool (single source of truth),
    # which keeps its original capital-R message.
    assert end.result == "Error: You must Read this file before editing it."
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


# ── Fixed-finding regression tests ──────────────────────────────────────────


async def test_write_file_then_edit_file_allowed(tmp_path: Any) -> None:
    # Finding #2: writing a virtual file marks it read, so write_file -> edit_file
    # (a previously-broken flow under the default-on hook) now succeeds.
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.filesystem import StateFileBackend

    backend = StateFileBackend()
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="write_file",
                tool_input={"path": "/n.txt", "content": "alpha"},
                tool_id="w1",
            ),
            ToolUseTurn(
                tool_name="edit_file",
                tool_input={"path": "/n.txt", "old_string": "alpha", "new_string": "beta"},
                tool_id="e1",
            ),
            TextTurn("done"),
        ]
    )
    events = await _collect(await _agent(provider, tmp_path, filesystem=backend).session())
    end = _tool_end(events, "edit_file")
    assert not end.is_error
    assert await backend.read("/n.txt") == "beta"


async def test_workspace_write_new_file_is_allowed(tmp_path: Any) -> None:
    # Finding #1: creating a new file is never blocked (only overwrite is gated).
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    provider = ScriptedProvider(
        [
            ToolUseTurn(tool_name="Write", tool_input={"file_path": "new.txt", "content": "fresh"}),
            TextTurn("done"),
        ]
    )
    events = await _collect(await _agent(provider, tmp_path).session())
    end = _tool_end(events, "Write")
    assert not end.is_error
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "fresh"


def _overwrite_guarded_hook() -> Any:
    # Overwrite-gating is opt-in (default off so regen/scratchpad flows work).
    from linch import ReadBeforeWriteHook
    from linch.hooks.read_before_write import ReadBeforeWriteConfig

    return ReadBeforeWriteHook(
        ReadBeforeWriteConfig(overwrite_tools={"Write": "workspace", "write_file": "virtual"})
    )


async def test_workspace_write_overwrite_blocked_when_opted_in(tmp_path: Any) -> None:
    # Finding #1 (data loss), opt-in: overwriting an existing, unread file is
    # blocked and the prior contents survive.
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="Write",
                tool_input={"file_path": "a.txt", "content": "OVERWRITTEN"},
            ),
            TextTurn("done"),
        ]
    )
    events = await _collect(
        await _agent(provider, tmp_path, hooks=[_overwrite_guarded_hook()]).session()
    )
    end = _tool_end(events, "Write")
    assert end.is_error
    assert "overwriting" in end.result
    assert target.read_text(encoding="utf-8") == "hello"


async def test_workspace_write_overwrite_allowed_by_default(tmp_path: Any) -> None:
    # Default: whole-file overwrite is Write's purpose and is NOT gated (regen).
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="Write",
                tool_input={"file_path": "a.txt", "content": "regenerated"},
            ),
            TextTurn("done"),
        ]
    )
    events = await _collect(await _agent(provider, tmp_path).session())
    end = _tool_end(events, "Write")
    assert not end.is_error
    assert target.read_text(encoding="utf-8") == "regenerated"


async def test_workspace_write_then_edit_allowed(tmp_path: Any) -> None:
    # Writing a file marks it read, so a later Edit on it is allowed.
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="Write",
                tool_input={"file_path": "n.txt", "content": "abc"},
                tool_id="w1",
            ),
            ToolUseTurn(
                tool_name="Edit",
                tool_input={"file_path": "n.txt", "old_string": "abc", "new_string": "xyz"},
                tool_id="e1",
            ),
            TextTurn("done"),
        ]
    )
    events = await _collect(await _agent(provider, tmp_path).session())
    end = _tool_end(events, "Edit")
    assert not end.is_error
    assert (tmp_path / "n.txt").read_text(encoding="utf-8") == "xyz"


async def test_virtual_write_file_overwrite_blocked_when_opted_in(tmp_path: Any) -> None:
    # Finding #1 for the virtual filesystem, opt-in.
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.filesystem import StateFileBackend

    backend = StateFileBackend({"/n.txt": "alpha"})
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="write_file",
                tool_input={"path": "/n.txt", "content": "OVERWRITE"},
            ),
            TextTurn("done"),
        ]
    )
    events = await _collect(
        await _agent(
            provider, tmp_path, filesystem=backend, hooks=[_overwrite_guarded_hook()]
        ).session()
    )
    end = _tool_end(events, "write_file")
    assert end.is_error
    assert await backend.read("/n.txt") == "alpha"


async def test_windowed_read_file_does_not_grant_edit(tmp_path: Any) -> None:
    # Finding #6: a partial (windowed) read must not unlock edits to unseen regions.
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn
    from linch.filesystem import StateFileBackend

    backend = StateFileBackend({"/n.txt": "l1\nl2\nl3\n"})
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="read_file",
                tool_input={"path": "/n.txt", "offset": 1, "limit": 1},
                tool_id="r1",
            ),
            ToolUseTurn(
                tool_name="edit_file",
                tool_input={"path": "/n.txt", "old_string": "l2", "new_string": "X"},
                tool_id="e1",
            ),
            TextTurn("done"),
        ]
    )
    events = await _collect(await _agent(provider, tmp_path, filesystem=backend).session())
    end = _tool_end(events, "edit_file")
    assert end.is_error


async def test_overwrite_gate_unresolvable_path_fails_closed(tmp_path: Any) -> None:
    # Finding #5: an unresolvable (cwd-escaping) path on the opt-in overwrite gate
    # blocks (fail-closed) rather than silently allowing it.
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="Write",
                tool_input={"file_path": "../../escape.txt", "content": "x"},
            ),
            TextTurn("done"),
        ]
    )
    events = await _collect(
        await _agent(provider, tmp_path, hooks=[_overwrite_guarded_hook()]).session()
    )
    end = _tool_end(events, "Write")
    assert end.is_error
    assert "overwriting" in end.result


async def test_unrelated_hook_named_rbw_does_not_suppress_default(tmp_path: Any) -> None:
    # Finding #7: a foreign hook whose name collides must not suppress the guard.
    from linch import ReadBeforeWriteHook
    from linch.evals import ScriptedProvider, TextTurn

    class Impostor:
        name = "read_before_write"

        async def on_pre_tool_use(self, ctx: Any) -> Any:
            return None

        async def on_post_tool_use(self, ctx: Any) -> Any:
            return None

    agent = _agent(ScriptedProvider([TextTurn("done")]), tmp_path, hooks=[Impostor()])
    assert any(isinstance(hook, ReadBeforeWriteHook) for hook in agent.hooks)
