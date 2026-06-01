from __future__ import annotations

import pytest

from agent_kit.filesystem import (
    CompositeFileBackend,
    DiskFileBackend,
    SqliteFileBackend,
    StateFileBackend,
)
from agent_kit.filesystem.backend import normalize_path


def test_normalize_path() -> None:
    assert normalize_path("a//b/") == "/a/b"
    assert normalize_path("/x/y") == "/x/y"
    assert normalize_path("") == "/"
    assert normalize_path("/") == "/"


async def _exercise(backend) -> None:  # type: ignore[no-untyped-def]
    await backend.write("/notes/a.txt", "line1\nline2\nline3")
    assert await backend.exists("/notes/a.txt")
    assert await backend.read("/notes/a.txt") == "line1\nline2\nline3"
    # offset/limit windowing (1-indexed)
    assert await backend.read("/notes/a.txt", offset=2, limit=1) == "line2"
    # ls + prefix
    await backend.write("/other.txt", "x")
    assert "/notes/a.txt" in await backend.ls()
    assert await backend.ls("/notes") == ["/notes/a.txt"]
    # edit
    count = await backend.edit("/notes/a.txt", "line2", "LINE2")
    assert count == 1
    assert "LINE2" in await backend.read("/notes/a.txt")
    # missing file
    with pytest.raises(FileNotFoundError):
        await backend.read("/nope.txt")
    # delete
    await backend.delete("/notes/a.txt")
    assert not await backend.exists("/notes/a.txt")


async def test_state_backend() -> None:
    await _exercise(StateFileBackend())


async def test_sqlite_backend() -> None:
    backend = SqliteFileBackend(":memory:")
    await _exercise(backend)
    backend.close()


async def test_disk_backend(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = DiskFileBackend(root=tmp_path / "offload")
    await _exercise(backend)


async def test_disk_backend_sandbox(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = DiskFileBackend(root=tmp_path / "offload")
    with pytest.raises(ValueError):
        await backend.write("/../escape.txt", "nope")


async def test_edit_not_unique_requires_replace_all() -> None:
    backend = StateFileBackend()
    await backend.write("/f.txt", "x x x")
    with pytest.raises(ValueError):
        await backend.edit("/f.txt", "x", "y")
    count = await backend.edit("/f.txt", "x", "y", replace_all=True)
    assert count == 3
    assert await backend.read("/f.txt") == "y y y"


async def test_composite_routing() -> None:
    default = StateFileBackend()
    persistent = StateFileBackend()
    fs = CompositeFileBackend(default=default, routes={"/memories/": persistent})

    await fs.write("/memories/fact.txt", "remembered")
    await fs.write("/scratch/tmp.txt", "ephemeral")

    # Routed write lands in the persistent backend, not the default.
    assert await persistent.exists("/memories/fact.txt")
    assert not await default.exists("/memories/fact.txt")
    assert await default.exists("/scratch/tmp.txt")

    # Reads route correctly and ls unions across backends.
    assert await fs.read("/memories/fact.txt") == "remembered"
    listing = await fs.ls()
    assert "/memories/fact.txt" in listing
    assert "/scratch/tmp.txt" in listing
