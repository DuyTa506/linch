"""Keep live Postgres tests inside one disposable schema per pytest session."""

from __future__ import annotations

import asyncio
import os
from importlib.util import find_spec
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

_PG_SCHEMA = pytest.StashKey[tuple[str, str]]()


def pytest_sessionstart(session: pytest.Session) -> None:
    dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN")
    if not dsn or find_spec("asyncpg") is None:
        return
    import asyncpg

    schema = f"linch_test_{uuid4().hex}"

    async def create() -> None:
        conn = await asyncpg.connect(dsn, timeout=10)
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
        finally:
            await conn.close()

    asyncio.run(create())
    session.config.stash[_PG_SCHEMA] = (dsn, schema)
    parts = urlsplit(dsn)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "search_path"
    ]
    query.append(("search_path", schema))
    os.environ["AGENT_KIT_TEST_PG_DSN"] = urlunsplit(parts._replace(query=urlencode(query)))


def pytest_sessionfinish(session: pytest.Session) -> None:
    state = session.config.stash.get(_PG_SCHEMA, None)
    if state is None:
        return
    import asyncpg

    dsn, schema = state

    async def drop() -> None:
        conn = await asyncpg.connect(dsn, timeout=10)
        try:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await conn.close()

    try:
        asyncio.run(drop())
    finally:
        os.environ["AGENT_KIT_TEST_PG_DSN"] = dsn


@pytest.fixture
async def pg_dsn(request):
    """Give durable cases a fresh schema; memory cases never require Postgres."""
    params = getattr(getattr(request.node, "callspec", None), "params", {})
    if any(
        params.get(name) == "memory"
        for name in ("kind", "inbox_store", "store_case", "lease_stores")
    ):
        yield ""
        return
    dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN")
    if not dsn:
        pytest.skip("AGENT_KIT_TEST_PG_DSN not set")
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(dsn)
    try:
        schema = f"linch_case_{uuid4().hex}"
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            parts = urlsplit(dsn)
            query = [
                (key, value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
                if key != "search_path"
            ]
            query.append(("search_path", schema))
            yield urlunsplit(parts._replace(query=urlencode(query)))
        finally:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        await conn.close()
