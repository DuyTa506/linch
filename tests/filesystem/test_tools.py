from __future__ import annotations

import pytest

from linch.filesystem import StateFileBackend
from linch.filesystem.tools import EditFileTool, LsTool, ReadFileTool, WriteFileTool
from linch.tools.base import ToolContext


def _ctx(backend) -> ToolContext:  # type: ignore[no-untyped-def]
    ctx = ToolContext(
        cwd=".",
        session_id="s1",
        run_id="r1",
        session_store=None,  # type: ignore[arg-type]
        filesystem=backend,
    )
    return ctx


async def test_write_then_read() -> None:
    backend = StateFileBackend()
    ctx = _ctx(backend)

    result = await WriteFileTool().execute({"path": "/note.txt", "content": "hello\nworld"}, ctx)
    assert not result.is_error
    assert "note.txt" in result.content

    result = await ReadFileTool().execute({"path": "/note.txt"}, ctx)
    assert result.content == "hello\nworld"


async def test_read_with_offset_limit() -> None:
    backend = StateFileBackend()
    ctx = _ctx(backend)
    await WriteFileTool().execute({"path": "/f.txt", "content": "a\nb\nc"}, ctx)
    result = await ReadFileTool().execute({"path": "/f.txt", "offset": 2, "limit": 1}, ctx)
    assert result.content == "b"


async def test_ls_empty_then_populated() -> None:
    backend = StateFileBackend()
    ctx = _ctx(backend)

    result = await LsTool().execute({"prefix": ""}, ctx)
    assert "No files" in result.content

    await WriteFileTool().execute({"path": "/a.txt", "content": "x"}, ctx)
    result = await LsTool().execute({"prefix": ""}, ctx)
    assert "/a.txt" in result.content


async def test_edit_tool() -> None:
    backend = StateFileBackend()
    ctx = _ctx(backend)
    await WriteFileTool().execute({"path": "/p.txt", "content": "foo bar foo"}, ctx)

    result = await EditFileTool().execute(
        {"path": "/p.txt", "old_string": "foo", "new_string": "baz", "replace_all": True},
        ctx,
    )
    assert not result.is_error
    text = await backend.read("/p.txt")
    assert text == "baz bar baz"


async def test_read_missing_file_returns_error() -> None:
    ctx = _ctx(StateFileBackend())
    result = await ReadFileTool().execute({"path": "/missing.txt"}, ctx)
    assert result.is_error


async def test_edit_missing_file_returns_error() -> None:
    ctx = _ctx(StateFileBackend())
    result = await EditFileTool().execute(
        {"path": "/nope.txt", "old_string": "x", "new_string": "y"},
        ctx,
    )
    assert result.is_error


async def test_no_backend_raises() -> None:
    ctx = ToolContext(cwd=".", session_id="s", run_id="r", session_store=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="No filesystem backend"):
        await ReadFileTool().execute({"path": "/x"}, ctx)
