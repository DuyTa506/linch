from __future__ import annotations

import pytest

from linch.filesystem import (
    CompositeFileBackend,
    DiskFileBackend,
    StateFileBackend,
)
from linch.filesystem.backend import normalize_path


def test_normalize_path() -> None:
    assert normalize_path("a//b/") == "/a/b"
    assert normalize_path("/x/y") == "/x/y"
    assert normalize_path("") == "/"
    assert normalize_path("/") == "/"


async def test_disk_backend_sandbox(tmp_path) -> None:  # type: ignore[no-untyped-def]
    backend = DiskFileBackend(root=tmp_path / "offload")
    with pytest.raises(ValueError):
        await backend.write("/../escape.txt", "nope")


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
