"""Admission SQL boundaries without a database; real races use conformance tests."""

from contextlib import asynccontextmanager

import pytest

from linch.run_stores.postgres import PostgresRunStore

asyncpg = pytest.importorskip("asyncpg")


def violation(name="runs_one_active_per_session_idx"):
    error = asyncpg.UniqueViolationError("conflicting session run")
    error.constraint_name = name
    return error


class AdmissionConnection:
    def __init__(self, conflict=None, winner="active", disappear=False, transactional=True):
        self.conflict = conflict or violation()
        self.winner = winner
        self.disappear = disappear
        self.depth = 0
        self.attempts = 0
        self.transactional = transactional

    def is_in_transaction(self):
        return self.transactional

    @asynccontextmanager
    async def transaction(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    async def fetchrow(self, sql, *args):
        if sql.startswith("SELECT id, session_id"):
            assert self.depth == 0  # failed savepoint was rolled back
            assert "session_id" in sql and args in (("s", None), (None, "new"))
            return None if self.disappear else {"id": self.winner, "session_id": "s"}
        self.attempts += 1
        if self.attempts == 1:
            raise self.conflict
        return {
            "id": "new",
            "session_id": "s",
            "status": "pending",
            "checkpoint": None,
            "meta": "{}",
            "created_at": "now",
            "updated_at": "now",
        }


async def test_pending_conflict_maps_active_run_after_savepoint_rollback():
    from linch import SessionBusyError

    conn = AdmissionConnection()
    with pytest.raises(SessionBusyError) as busy:
        await PostgresRunStore("").create_pending_run(conn, "s", id="new")
    assert busy.value.run_id == "active"
    assert busy.value.session_id == "s"


async def test_unrelated_unique_violation_preserves_original_error():
    error = violation("unrelated_unique_index")
    conn = AdmissionConnection(conflict=error)
    with pytest.raises(asyncpg.UniqueViolationError) as caught:
        await PostgresRunStore("").create_pending_run(conn, "s", id="new")
    assert caught.value is error


async def test_admission_retries_when_conflicting_run_has_already_finished():
    conn = AdmissionConnection(disappear=True, transactional=False)
    row = await PostgresRunStore("").create_pending_run(conn, "s", id="new")
    assert row is not None
    assert row.id == "new" and row.status == "pending"
    assert conn.attempts == 2


@pytest.mark.parametrize("transactional", [False, True])
async def test_invisible_conflict_is_bounded_and_chains_violation(transactional):
    conn = AdmissionConnection(disappear=True, transactional=transactional)
    original_fetchrow = conn.fetchrow

    async def always_conflict(sql, *args):
        if sql.startswith("SELECT id, session_id"):
            return await original_fetchrow(sql, *args)
        conn.attempts += 1
        assert conn.attempts <= 3, "admission retried indefinitely"
        raise conn.conflict

    conn.fetchrow = always_conflict
    with pytest.raises(RuntimeError, match="admission.*conflict") as caught:
        await PostgresRunStore("").create_pending_run(conn, "s", id="new")
    assert caught.value.__cause__ is conn.conflict
    assert conn.attempts == (1 if transactional else 3)


async def test_duplicate_active_rows_fail_schema_install_with_clear_error(monkeypatch):
    import linch.run_stores.postgres as postgres
    from linch import ConfigError

    async def duplicate_rows(*args, **kwargs):
        raise violation()

    monkeypatch.setattr(postgres, "ensure_pool", duplicate_rows)
    with pytest.raises(ConfigError, match="duplicate.*nonterminal|nonterminal.*duplicate"):
        await PostgresRunStore("")._ensure()


@pytest.mark.parametrize("operation", ["create", "checkpoint", "checkpoint_events"])
async def test_direct_run_and_checkpoint_conflicts_map_to_recoverable_busy_error(
    monkeypatch, operation
):
    from linch import RunCheckpoint, SessionBusyError, Usage

    conn = AdmissionConnection()
    pool = type("Pool", (), {"acquire": lambda self: acquire()})()

    @asynccontextmanager
    async def acquire():
        yield conn

    async def initialized():
        return pool

    store = PostgresRunStore("")
    monkeypatch.setattr(store, "_ensure", initialized)
    # The update mapper resolves the target session from the existing run id.
    with pytest.raises(SessionBusyError) as busy:
        if operation == "create":
            await store.create_run("s", id="new")
        elif operation == "checkpoint":
            await store.save_checkpoint("new", RunCheckpoint("started", "p", 0, Usage()))
        else:
            await store.save_checkpoint_with_events(
                "new", RunCheckpoint("started", "p", 0, Usage()), []
            )
    assert busy.value.run_id == "active"


async def test_existing_duplicate_rows_reject_index_installation(pg_dsn):
    from linch import ConfigError

    store = PostgresRunStore(pg_dsn)
    try:
        await store.create_run("s", id="first")
        pool = await store._ensure()
        await pool.execute("DROP INDEX runs_one_active_per_session_idx")
        await pool.execute(
            "INSERT INTO runs (id, session_id, status, created_at, updated_at) "
            "VALUES ('second', 's', 'suspended', 'now', 'now')"
        )
        fresh = PostgresRunStore("", pool=pool)
        with pytest.raises(ConfigError, match="duplicate nonterminal"):
            await fresh._ensure()
    finally:
        await store.close()


async def test_existing_schema_installs_admission_index_under_transaction():
    from linch import ConfigError

    class ExistingSchemaConnection:
        depth = 0

        @asynccontextmanager
        async def transaction(self):
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

        async def fetchval(self, *args):
            return True  # version metadata and required columns already exist

        async def fetchrow(self, *args):
            return {"version": 3}

        async def execute(self, sql, *args):
            if "CREATE UNIQUE INDEX" in sql:
                assert self.depth == 1
                assert "ON runs (session_id)" in sql
                for status in ("pending", "running", "waiting_permission", "suspended"):
                    assert f"'{status}'" in sql
                raise violation()

    conn = ExistingSchemaConnection()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    store = PostgresRunStore("", pool=Pool())
    with pytest.raises(ConfigError, match="duplicate nonterminal"):
        await store._ensure()
    assert conn.depth == 0
    assert not store._initialized
