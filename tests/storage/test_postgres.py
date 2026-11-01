"""Postgres filesystem backend integration checks."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("asyncpg", reason="asyncpg not installed")

DSN = os.environ.get("AGENT_KIT_TEST_PG_DSN", "")
needs_pg = pytest.mark.skipif(not DSN, reason="AGENT_KIT_TEST_PG_DSN not set")


@needs_pg
async def test_pg_file_backend_round_trip() -> None:
    from linch.filesystem.postgres import PostgresFileBackend

    backend = PostgresFileBackend(DSN)
    try:
        await backend.write("/pg/test.txt", "hello postgres fs")
        assert await backend.read("/pg/test.txt") == "hello postgres fs"
        assert await backend.exists("/pg/test.txt")
        assert not await backend.exists("/pg/missing.txt")
        assert "/pg/test.txt" in await backend.ls("/pg")
        assert await backend.edit("/pg/test.txt", "hello", "goodbye") == 1
        assert await backend.read("/pg/test.txt") == "goodbye postgres fs"
        await backend.delete("/pg/test.txt")
        assert not await backend.exists("/pg/test.txt")
    finally:
        await backend.close()


@needs_pg
async def test_pg_file_backend_concurrent_writes() -> None:
    import asyncio

    from linch.filesystem.postgres import PostgresFileBackend

    backend = PostgresFileBackend(DSN, min_size=5, max_size=20)
    paths = [f"/pg-concurrent/{index}.txt" for index in range(50)]
    try:
        await asyncio.gather(
            *(backend.write(path, f"content {index}") for index, path in enumerate(paths))
        )
        assert len(await backend.ls("/pg-concurrent")) == 50
    finally:
        for path in paths:
            await backend.delete(path)
        await backend.close()
