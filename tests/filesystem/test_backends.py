from __future__ import annotations

import asyncio
import os
import stat
from typing import Any
from uuid import uuid4

import pytest

from linch.filesystem import (
    CompositeFileBackend,
    DiskFileBackend,
    StateFileBackend,
)
from linch.filesystem.backend import normalize_path


def _filesystem(kind: str, tmp_path, pg_dsn: str | None = None) -> Any:
    if kind == "memory":
        return StateFileBackend()
    if kind == "disk":
        return DiskFileBackend(tmp_path)
    from linch.filesystem.postgres import PostgresFileBackend

    assert pg_dsn is not None
    return PostgresFileBackend(pg_dsn)


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


@pytest.mark.parametrize(
    ("kind", "store_case"),
    [("memory", "memory"), ("disk", "memory"), ("postgres", "postgres")],
    ids=["memory", "disk", "postgres"],
)
async def test_file_backend_preserves_crlf_and_lone_cr(
    kind: str, store_case: str, tmp_path, pg_dsn
) -> None:
    del store_case
    backend = _filesystem(kind, tmp_path, pg_dsn)
    try:
        content = "keep\r\nold\r\nprogress\rend"
        await backend.write("/line-endings.txt", content)
        assert await backend.read("/line-endings.txt") == content
        assert await backend.edit("/line-endings.txt", "old\r\n", "new\r\n") == 1
        assert await backend.read("/line-endings.txt") == "keep\r\nnew\r\nprogress\rend"
    finally:
        if kind == "postgres":
            await backend.close()


@pytest.mark.parametrize(
    ("kind", "store_case"),
    [("memory", "memory"), ("disk", "memory"), ("postgres", "postgres")],
    ids=["memory", "disk", "postgres"],
)
async def test_file_backend_ls_treats_prefix_metacharacters_literally(
    kind: str, store_case: str, tmp_path, pg_dsn
) -> None:
    del store_case
    backend = _filesystem(kind, tmp_path, pg_dsn)
    try:
        await backend.write("/user_1/secret.txt", "owned")
        await backend.write("/userX1/secret.txt", "foreign")
        assert await backend.ls("/user_1") == ["/user_1/secret.txt"]
        await backend.write("/100%/secret.txt", "owned")
        await backend.write("/100x/secret.txt", "foreign")
        assert await backend.ls("/100%") == ["/100%/secret.txt"]
    finally:
        if kind == "postgres":
            await backend.close()


@pytest.mark.parametrize(
    ("kind", "store_case"),
    [("memory", "memory"), ("disk", "memory"), ("postgres", "postgres")],
    ids=["memory", "disk", "postgres"],
)
async def test_file_backend_serializes_concurrent_distinct_edits(
    kind: str, store_case: str, tmp_path, pg_dsn
) -> None:
    del store_case
    backend = _filesystem(kind, tmp_path, pg_dsn)
    try:
        lines = [f"line-{index:02d}=old\n" for index in range(16)]
        await backend.write("/concurrent.txt", "".join(lines))
        results = await asyncio.gather(
            *(
                backend.edit(
                    "/concurrent.txt",
                    line.rstrip("\n"),
                    line.rstrip("\n").replace("old", "new"),
                )
                for line in lines
            )
        )
        assert results == [1] * len(lines)
        expected = "".join(line.replace("old", "new") for line in lines)
        assert await backend.read("/concurrent.txt") == expected
    finally:
        if kind == "postgres":
            await backend.close()


async def test_disk_write_failure_leaves_original_file_intact(tmp_path, monkeypatch) -> None:
    backend = DiskFileBackend(tmp_path)
    await backend.write("/file.txt", "original\r\n")
    original_path = tmp_path / "file.txt"

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr("linch.filesystem.disk.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        await backend.write("/file.txt", "replacement")
    assert original_path.read_bytes() == b"original\r\n"
    assert list(tmp_path.glob(".file.txt.*.tmp")) == []


async def test_disk_write_rejects_unencodable_content_without_touching_file(tmp_path) -> None:
    backend = DiskFileBackend(tmp_path)
    await backend.write("/file.txt", "original\r\n")
    original_path = tmp_path / "file.txt"
    with pytest.raises(UnicodeEncodeError):
        await backend.write("/file.txt", "broken: \ud800")
    assert original_path.read_bytes() == b"original\r\n"


async def test_disk_write_preserves_existing_mode(tmp_path) -> None:
    backend = DiskFileBackend(tmp_path)
    await backend.write("/file.txt", "one")
    path = tmp_path / "file.txt"
    os.chmod(path, 0o640)
    await backend.write("/file.txt", "two")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.asyncio
async def test_postgres_concurrent_edits_apply_both_changes(pg_dsn) -> None:
    import asyncpg

    from linch.filesystem.postgres import PostgresFileBackend

    application_name = f"linch_fs_test_{uuid4().hex}"
    pool = await asyncpg.create_pool(
        pg_dsn,
        min_size=2,
        max_size=2,
        server_settings={"application_name": application_name},
    )
    lock_connection = await asyncpg.connect(pg_dsn)
    lock_transaction = lock_connection.transaction()
    tasks: list[asyncio.Task[int]] = []
    lock_held = False
    backend = PostgresFileBackend(pg_dsn, pool=pool)
    try:
        await backend.write("/concurrent.txt", "alpha=0\nbeta=0\n")
        await lock_transaction.start()
        await lock_connection.execute(
            "SELECT path FROM agentkit_files WHERE path = $1 FOR UPDATE", "/concurrent.txt"
        )
        lock_held = True

        async def edit(old: str, new: str) -> int:
            return await backend.edit("/concurrent.txt", old, new)

        tasks = [
            asyncio.create_task(edit("alpha=0", "alpha=1")),
            asyncio.create_task(edit("beta=0", "beta=1")),
        ]
        deadline = asyncio.get_running_loop().time() + 5
        while (
            await lock_connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = $1 AND wait_event_type = 'Lock'",
                application_name,
            )
            < 2
        ):
            if any(task.done() for task in tasks):
                await asyncio.gather(*tasks)
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("both edits did not wait on the held row lock")
            await asyncio.sleep(0.01)
        await lock_transaction.rollback()
        lock_held = False
        results = await asyncio.gather(*tasks)
        assert results == [1, 1]
        assert await backend.read("/concurrent.txt") == "alpha=1\nbeta=1\n"
    finally:
        if lock_held:
            await lock_transaction.rollback()
        if tasks and not all(task.done() for task in tasks):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await lock_connection.close()
        await pool.close()
