"""In-memory and isolated Postgres stores for tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from linch import InMemoryRunStore
    from linch.sessions import InMemorySessionStore


def memory_session_store() -> InMemorySessionStore:
    """Create an in-memory session store."""

    from linch.sessions import InMemorySessionStore

    return InMemorySessionStore()


def memory_run_store() -> InMemoryRunStore:
    """Create an in-memory durable run store."""

    from linch import InMemoryRunStore

    return InMemoryRunStore()


@pytest.fixture
async def pg_dsn(request: Any) -> AsyncIterator[str]:
    """Give durable cases a fresh schema; memory cases never require Postgres."""

    params = getattr(getattr(request.node, "callspec", None), "params", {})
    if any(
        params.get(name) == "memory"
        for name in ("kind", "inbox_store", "store_case", "lease_stores", "delivery_store")
    ):
        yield ""
        return
    raw_dsn = os.environ.get("AGENT_KIT_TEST_PG_DSN")
    if not raw_dsn:
        pytest.skip("AGENT_KIT_TEST_PG_DSN not set")
    dsn = str(raw_dsn)
    if find_spec("asyncpg") is None:
        pytest.skip("asyncpg not installed")
    import asyncpg

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
