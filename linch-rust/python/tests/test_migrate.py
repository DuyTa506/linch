"""`linch_rust.migrate` and `schema_status`: the schema as a deploy step.

The database tests skip without `LINCH_TEST_PG_DSN`.
"""

from __future__ import annotations

import os
import re

import pytest
from test_resume import pg_dsn

from linch_rust import (
    OpenError,
    SchemaNewer,
    SchemaStatus,
    migrate,
    schema_status,
)
from linch_rust.errors import error_from

__all__ = ["pg_dsn"]


def test_a_newer_schema_is_an_open_error_that_names_the_versions() -> None:
    error = error_from(
        {
            "code": -32016,
            "message": "the database schema is newer",
            "data": {"table": "linch_migrations", "applied": 14, "latest": 10},
        }
    )

    assert isinstance(error, SchemaNewer) and isinstance(error, OpenError)
    assert (error.table, error.applied, error.latest) == ("linch_migrations", 14, 10)


async def test_schema_status_reads_and_migrate_applies(pg_dsn: str) -> None:
    before = await schema_status(pg_dsn)
    assert isinstance(before, SchemaStatus)
    assert before.store.applied is None and not before.is_current

    after = await migrate(pg_dsn)
    assert after.is_current
    assert after.store.applied == after.store.latest
    assert after.worker.applied == after.worker.latest
    assert await schema_status(pg_dsn) == after
    assert await migrate(pg_dsn) == after


async def test_a_database_a_newer_release_migrated_raises_schema_newer(pg_dsn: str) -> None:
    import psycopg
    from psycopg import sql

    status = await migrate(pg_dsn)
    schema = re.search(r"search_path%3D(\w+)", pg_dsn)
    assert schema is not None
    insert = sql.SQL(
        "INSERT INTO {}.linch_migrations "
        "(version, description, success, checksum, execution_time) "
        "VALUES (%s, 'a later release', true, '\\x00', 0)"
    ).format(sql.Identifier(schema.group(1)))
    with psycopg.connect(os.environ["LINCH_TEST_PG_DSN"], autocommit=True) as admin:
        admin.execute(insert, (status.store.latest + 3,))

    for call in (migrate, schema_status):
        with pytest.raises(SchemaNewer) as raised:
            await call(pg_dsn)
        assert raised.value.applied == status.store.latest + 3
        assert raised.value.latest == status.store.latest
        assert pg_dsn not in str(raised.value)


async def test_an_unreachable_database_is_an_open_error_without_the_dsn() -> None:
    dsn = "postgres://linch:hunter2-secret@127.0.0.1:1/linch?connect_timeout=1"

    with pytest.raises(OpenError) as raised:
        await schema_status(dsn)

    assert "hunter2-secret" not in str(raised.value)
