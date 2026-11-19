"""Two servers built from different commits on one Postgres: a rolling
upgrade and a rollback.

The later build is the one under test (`LINCH_SERVER_BIN`, or the usual
search); `LINCH_OTHER_SERVER_BIN` names a build of an earlier commit, one
before `server.info` and `usage_by_model`. Without it, without
`LINCH_TEST_PG_DSN`, or when both name the same file, every test skips, so CI
(one binary) is unaffected.

The later client refuses a server that has no `server.info`, so the tests
that drive the earlier build start it with the handshake stubbed out for the
start call only. Each test gets its own schema (see `test_resume.pg_dsn`).
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from scripted import config, text_turn, tool_turn
from test_resume import blocking_tool, leased_stores, once_free, pg_dsn
from test_scheduler import TOKEN, acme, serving

import linch_rust.server as server_module
from linch_rust import (
    ConnectionClosed,
    IncompatibleServer,
    ResultEvent,
    RunError,
    Server,
    find_server,
    tool,
)
from linch_rust.info import ServerInfo

__all__ = ["pg_dsn"]

OTHER = os.environ.get("LINCH_OTHER_SERVER_BIN")


def _second_binary_missing() -> bool:
    if not OTHER or not os.environ.get("LINCH_TEST_PG_DSN"):
        return True
    try:
        return Path(OTHER).resolve() == Path(find_server()).resolve()
    except FileNotFoundError:
        return True


pytestmark = pytest.mark.skipif(
    _second_binary_missing(),
    reason="needs LINCH_OTHER_SERVER_BIN (a second build), LINCH_TEST_PG_DSN and the later binary",
)


async def _no_handshake(conn: Any, **_: Any) -> ServerInfo:
    return ServerInfo("earlier", 1, 1, frozenset(), (), 0, 0)


@asynccontextmanager
async def earlier() -> AsyncIterator[Server]:
    """The other binary, started without the handshake the later client
    insists on (the handshake is stubbed for the start call only)."""
    assert OTHER is not None
    server = Server(OTHER)
    with mock.patch.object(server_module, "handshake", _no_handshake):
        await server.start()
    try:
        yield server
    finally:
        await server.close()


def rows(dsn: str, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    import psycopg

    with psycopg.connect(dsn.split("?")[0], autocommit=True) as conn:
        search = dsn.split("search_path%3D")[1]
        conn.execute(f'SET search_path TO "{search}"')  # type: ignore[call-overload]
        return conn.execute(sql, args).fetchall()  # type: ignore[arg-type]


def session_entries(dsn: str) -> list[str]:
    return [r[0] for r in rows(dsn, "SELECT entry FROM linch_session_entries ORDER BY position")]


def check_runs(dsn: str, expected: int) -> None:
    """Every run ended `completed`, its events are 1..n with no gap, and it
    holds exactly one `result` event."""
    runs = rows(dsn, "SELECT run_id, status, last_seq FROM linch_runs")
    assert len(runs) == expected
    for run_id, status, last_seq in runs:
        assert status == "completed", (run_id, status)
        events = "FROM linch_run_events WHERE run_id = %s"
        seqs = [r[0] for r in rows(dsn, f"SELECT seq {events} ORDER BY seq", run_id)]
        assert seqs == list(range(1, last_seq + 1)), run_id
        results = rows(dsn, f"SELECT 1 {events} AND envelope LIKE %s", run_id, '%"type":"result"%')
        assert len(results) == 1, run_id


async def test_the_later_build_resumes_a_run_the_earlier_one_left_unfinished(pg_dsn: str) -> None:
    stores = leased_stores(pg_dsn)
    wait, started = blocking_tool()
    async with earlier() as earlier_server:
        agent = await earlier_server.open_agent(
            config([tool_turn("call_1", "wait", {"q": "x"})], stores=stores), tools=[wait]
        )
        run = await agent.run("s1", "Wait for x")
        await asyncio.wait_for(started.wait(), 20)
        before = await agent.events(run.id)
        entries_before = session_entries(pg_dsn)
        os.kill(earlier_server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await run.result()

    wait, started = blocking_tool()
    async with Server() as later_server:
        agent = await later_server.open_agent(
            config([text_turn("Recovered.")], stores=stores), tools=[wait]
        )
        result = await once_free(lambda: agent.resume(run.id))
        replayed = await agent.events(run.id)

    assert not started.is_set(), "a started tool call is never run twice"
    assert isinstance(result, ResultEvent) and result.final_text == "Recovered."
    assert [(e.seq, e.event) for e in replayed[: len(before)]] == [(e.seq, e.event) for e in before]
    assert session_entries(pg_dsn)[: len(entries_before)] == entries_before
    check_runs(pg_dsn, 1)


async def test_the_earlier_build_cannot_resume_a_run_the_later_one_wrote(pg_dsn: str) -> None:
    stores = leased_stores(pg_dsn)
    wait, started = blocking_tool()
    async with Server() as later_server:
        agent = await later_server.open_agent(
            config([tool_turn("call_1", "wait", {"q": "x"})], stores=stores), tools=[wait]
        )
        run = await agent.run("s1", "Wait for x")
        await asyncio.wait_for(started.wait(), 20)
        os.kill(later_server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await run.result()

    wait, started = blocking_tool()
    async with earlier() as earlier_server:
        agent = await earlier_server.open_agent(
            config([text_turn("Never.")], stores=stores), tools=[wait]
        )
        failure: BaseException | None = None
        for _ in range(20):
            resumed = await agent.resume(run.id)
            try:
                async with asyncio.timeout(30):
                    await resumed.result()
            except RunError as error:
                failure = error
                break
        assert failure is not None, "the earlier build must refuse, not resume"
        # A named store error that quotes the unknown field; the same each time.
        assert failure.code == -32020 and "usage_by_model" in str(failure), failure
        with pytest.raises(RunError) as again:
            await (await agent.resume(run.id)).result()
        assert str(again.value) == str(failure)
        with pytest.raises(RunError) as unread:
            await agent.events(run.id)
        assert "cumulative_by_model" in str(unread.value)

    assert not started.is_set()
    # Not half resumed: still unfinished, nothing appended by the earlier build.
    [(status, last_seq)] = rows(pg_dsn, "SELECT status, last_seq FROM linch_runs")
    assert status == "running"
    assert rows(pg_dsn, "SELECT count(*) FROM linch_run_events")[0][0] == last_seq


async def test_a_rolling_upgrade_loses_no_run_and_repeats_no_effect(pg_dsn: str) -> None:
    stores = leased_stores(pg_dsn)
    runs = 4
    bumped: dict[str, int] = {}
    slow_started = {i: asyncio.Event() for i in range(runs)}

    def tools_for(i: int, hold: bool) -> list[Any]:
        @tool(scope="read")
        async def bump(key: str) -> str:
            bumped[key] = bumped.get(key, 0) + 1
            return key

        @tool(scope="read")
        async def slow(key: str) -> str:
            bumped[f"slow-{key}"] = bumped.get(f"slow-{key}", 0) + 1
            slow_started[i].set()
            if hold:
                await asyncio.Event().wait()
            return key

        return [bump, slow]

    def turns(i: int, first: bool) -> list[dict[str, Any]]:
        if not first:
            return [text_turn("Upgraded.")]
        return [
            tool_turn(f"b{i}", "bump", {"key": f"r{i}"}),
            tool_turn(f"s{i}", "slow", {"key": f"r{i}"}),
        ]

    handles = []
    async with earlier() as earlier_server:
        live = []
        for i in range(runs):
            agent = await earlier_server.open_agent(
                config(turns(i, True), stores=stores), tools=tools_for(i, True)
            )
            run = await agent.run(f"s{i}", "go")
            live.append(run)
            handles.append((i, run.id))
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in slow_started.values())), 30)
        earlier_server._process.send_signal(signal.SIGTERM)  # type: ignore[union-attr]
        for run in live:
            with pytest.raises(ConnectionClosed):
                await run.result()
        assert earlier_server._process is not None
        await asyncio.wait_for(earlier_server._process.wait(), 30)

    assert all(count == 1 for count in bumped.values())
    for e in slow_started.values():
        e.clear()
    async with Server() as later_server:
        results = []
        for i, run_id in handles:
            agent = await later_server.open_agent(
                config(turns(i, False), stores=stores), tools=tools_for(i, False)
            )
            results.append(await once_free(lambda a=agent, r=run_id: a.resume(r)))  # type: ignore[misc]
    assert all(isinstance(r, ResultEvent) and r.subtype == "success" for r in results)
    assert not any(e.is_set() for e in slow_started.values()), "no interrupted call ran again"
    assert all(count == 1 for count in bumped.values()), bumped
    check_runs(pg_dsn, runs)


async def test_a_job_queue_shared_by_both_builds_runs_each_job_once(pg_dsn: str) -> None:
    assert OTHER is not None
    jobs = 12
    allow = {"max_queued_jobs": 64}
    async with serving(pg_dsn, allow=allow) as at_new:
        async with serving_other(pg_dsn, OTHER, allow=allow):
            async with Server.connect(at_new, token=TOKEN) as server:
                agent = await server.open_agent(acme(pg_dsn))
                queued = [await agent.enqueue(f"s{i}", "Go.") for i in range(jobs)]
                done = [await agent.wait_job(j["job_id"], timeout=90) for j in queued]
    assert all(j["state"] == "done" for j in done)
    for job in done:
        if job.get("attempts") != 1:
            print("job attempts", job.get("job_id"), job.get("attempts"))
    assert len({j.get("run_id") for j in done}) == jobs
    check_runs(pg_dsn, jobs)


@asynccontextmanager
async def serving_other(dsn: str, binary: str, *, allow: dict[str, Any]) -> AsyncIterator[str]:
    """`test_scheduler.serving` for the other binary."""
    import test_scheduler

    with mock.patch.object(test_scheduler, "find_server", lambda: Path(binary)):
        async with test_scheduler.serving(dsn, allow=allow) as address:
            yield address


async def test_server_info_differs_and_the_later_client_refuses_the_earlier_build() -> None:
    assert OTHER is not None
    async with Server() as later_server:
        assert later_server.info.protocol >= 1 and later_server.info.contract_version >= 1
    with pytest.raises(IncompatibleServer) as refused:
        async with Server(OTHER):
            pass
    assert refused.value.protocol is None
