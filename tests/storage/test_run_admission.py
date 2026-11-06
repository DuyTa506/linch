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
            assert "session_id" in sql and args in (("s", None), (None, "new"), ("s", "new"))
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


async def test_unrelated_unique_violation_preserves_original_error():
    error = violation("unrelated_unique_index")
    conn = AdmissionConnection(conflict=error)
    with pytest.raises(asyncpg.UniqueViolationError) as caught:
        await PostgresRunStore("").create_pending_run(conn, "s", id="new")
    assert caught.value is error


@pytest.mark.parametrize("transactional", [False])
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


@pytest.mark.parametrize("operation", ["create"])
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


async def test_same_id_race_falls_through_to_existing_run_check():
    class SameId(AdmissionConnection):
        async def fetchrow(self, sql, *args):
            if sql.startswith("SELECT id, session_id"):
                assert args == ("s", "new")
                return {"id": "new", "session_id": "s"}
            raise self.conflict

    row = await PostgresRunStore("")._admit(SameId(), "INSERT", session_id="s", run_id="new")
    assert row is None
