from __future__ import annotations

import pytest

from linch.filesystem import StateFileBackend
from linch.filesystem.tools import EditFileTool, LsTool, ReadFileTool, WriteFileTool
from linch.tools.base import ToolContext


def _ctx(backend: StateFileBackend) -> ToolContext:
    return ToolContext(
        cwd=".",
        session_id="s1",
        run_id="r1",
        session_store=None,  # type: ignore[arg-type]
        filesystem=backend,
    )


@pytest.fixture
def fs_ctx() -> tuple[StateFileBackend, ToolContext]:
    backend = StateFileBackend()
    return backend, _ctx(backend)


@pytest.mark.parametrize("offset", [None, 2])
async def test_write_and_read_variants(offset, fs_ctx) -> None:
    backend, ctx = fs_ctx
    if offset is None:
        result = await WriteFileTool().execute(
            {"path": "/note.txt", "content": "hello\nworld"}, ctx
        )
        assert not result.is_error
        assert "note.txt" in result.content
        result = await ReadFileTool().execute({"path": "/note.txt"}, ctx)
        assert result.content == "hello\nworld"
    else:
        await WriteFileTool().execute({"path": "/f.txt", "content": "a\nb\nc"}, ctx)
        result = await ReadFileTool().execute({"path": "/f.txt", "offset": offset, "limit": 1}, ctx)
        assert result.content == "b"


async def test_ls_empty_then_populated(fs_ctx) -> None:
    _, ctx = fs_ctx

    result = await LsTool().execute({"prefix": ""}, ctx)
    assert "No files" in result.content

    await WriteFileTool().execute({"path": "/a.txt", "content": "x"}, ctx)
    result = await LsTool().execute({"prefix": ""}, ctx)
    assert "/a.txt" in result.content


async def test_edit_tool(fs_ctx) -> None:
    backend, ctx = fs_ctx
    await WriteFileTool().execute({"path": "/p.txt", "content": "foo bar foo"}, ctx)

    result = await EditFileTool().execute(
        {"path": "/p.txt", "old_string": "foo", "new_string": "baz", "replace_all": True},
        ctx,
    )
    assert not result.is_error
    text = await backend.read("/p.txt")
    assert text == "baz bar baz"


@pytest.mark.parametrize(
    ("tool", "input"),
    [
        (ReadFileTool, {"path": "/missing.txt"}),
        (EditFileTool, {"path": "/nope.txt", "old_string": "x", "new_string": "y"}),
    ],
)
async def test_missing_file_returns_error(tool, input, fs_ctx) -> None:
    _, ctx = fs_ctx
    result = await tool().execute(input, ctx)
    assert result.is_error


async def test_no_backend_raises() -> None:
    ctx = ToolContext(cwd=".", session_id="s", run_id="r", session_store=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="No filesystem backend"):
        await ReadFileTool().execute({"path": "/x"}, ctx)
