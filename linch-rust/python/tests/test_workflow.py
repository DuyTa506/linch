"""Directed workflows through the client: replay after a crash of the
client or the server, handoff between steps, interrupts, fan-out failure,
budgets, deadlines, one driver per workflow, and `run_or_resume`.

Skips without `LINCH_TEST_PG_DSN`, except the tests that need no database.
Each test gets its own schema through the DSN's `search_path`. A killed
server never releases the lease of the child run it drove, so the stores
use the shortest lease (1 s) and the resumed call waits it out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from scripted import config, text_turn, tool_turn

from linch_rust import (
    Budget,
    ConnectionClosed,
    NotAllowed,
    Principal,
    ResultEvent,
    RunLeased,
    Server,
    SuspendedEvent,
    WorkflowBudgetExceeded,
    WorkflowCallFailed,
    WorkflowContext,
    WorkflowEvent,
    WorkflowHeld,
    WorkflowNoJournal,
    WorkflowNotFound,
    WorkflowSuspended,
    WorkflowTimeoutError,
    run_or_resume,
    run_workflow,
    tool,
    workflow_status,
)


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    """A DSN whose `search_path` is a fresh schema."""
    dsn = os.environ.get("LINCH_TEST_PG_DSN")
    if not dsn:
        pytest.skip("LINCH_TEST_PG_DSN is not set")
    import psycopg
    from psycopg import sql

    schema = f"wf_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            separator = "&" if "?" in dsn else "?"
            yield f"{dsn}{separator}options=-c%20search_path%3D{schema}"
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def leased(dsn: str) -> dict[str, Any]:
    return {"type": "postgres", "dsn": dsn, "lease_ttl_ms": 1000}


def blocking_tool() -> tuple[Any, asyncio.Event]:
    started = asyncio.Event()

    @tool(scope="read")
    async def wait(q: str) -> str:
        started.set()
        await asyncio.Event().wait()
        return q

    return wait, started


async def when_free(drive: Callable[[], Awaitable[Any]]) -> Any:
    """`drive` again while the connection that held the workflow is ending."""
    for _ in range(100):
        try:
            return await drive()
        except WorkflowHeld:
            await asyncio.sleep(0.1)
    raise AssertionError("the workflow was never freed")


def kinds(events: list[WorkflowEvent]) -> list[str]:
    return [event.kind for event in events if event.kind != "phase"]


async def three_stages(wf: WorkflowContext) -> dict[str, Any]:
    await wf.phase("scan")
    notes = await wf.parallel(
        [lambda: wf.agent("scan host a"), lambda: wf.agent("scan host b")],
    )
    merged = await wf.step("merge", lambda: {"n": len(notes), "hosts": sorted(notes)})
    verdicts = await wf.pipeline(
        notes,
        lambda note: wf.agent(f"check: {note}"),
    )
    report = await wf.agent_result(
        f"report on {json.dumps(merged)}",
        output_schema={
            "name": "report",
            "schema": {
                "type": "object",
                "properties": {"total": {"type": "number"}},
                "required": ["total"],
            },
        },
    )
    return {"merged": merged, "verdicts": verdicts, "report": report.structured}


async def test_a_three_stage_workflow_replays_whole_after_a_restart(pg_dsn: str) -> None:
    stores = leased(pg_dsn)
    turns = [text_turn("open")] * 2 + [text_turn("fine")] * 2 + [text_turn('{"total": 2}')]
    events: list[WorkflowEvent] = []
    async with Server() as server:
        agent = await server.open_agent(config(turns, stores=stores))
        first = await run_workflow(agent, three_stages, run_id="pentest", on_event=events.append)

    assert first["merged"] == {"n": 2, "hosts": ["open", "open"]}
    assert first["verdicts"] == ["fine", "fine"]
    assert first["report"] == {"total": 2}
    assert kinds(events).count("agent_end") == 5
    assert [e.title for e in events if e.kind == "phase"] == ["scan"]

    # A new server whose provider has no turn at all: every call replays.
    replayed: list[WorkflowEvent] = []
    async with Server() as server:
        agent = await server.open_agent(config([], stores=stores))
        again = await when_free(
            lambda: run_workflow(agent, three_stages, run_id="pentest", on_event=replayed.append)
        )
        status = await workflow_status(agent, "pentest")

    assert again == first
    assert "agent_start" not in kinds(replayed)
    assert kinds(replayed).count("agent_replayed") == 5
    assert status.status == "completed" and status.records == 6


async def test_a_client_that_fails_mid_way_resumes_without_repeating_finished_calls(
    pg_dsn: str,
) -> None:
    stores = leased(pg_dsn)
    ran: list[str] = []

    async def flow(wf: WorkflowContext, crash: bool) -> str:
        first = await wf.agent("stage one")
        await wf.step("note", lambda: ran.append("note") or "noted")
        if crash:
            raise RuntimeError("the client died here")
        return first + await wf.agent("stage two")

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("one")], stores=stores))
        with pytest.raises(RuntimeError):
            await run_workflow(agent, lambda wf: flow(wf, True), run_id="w")
        assert (await workflow_status(agent, "w")).status == "failed"

    async with Server() as server:
        # One turn is left: only the call after the crash can use it.
        agent = await server.open_agent(config([text_turn("two")], stores=stores))
        result = await when_free(
            lambda: run_workflow(agent, lambda wf: flow(wf, False), run_id="w")
        )

    assert result == "onetwo"
    assert ran == ["note"], "the finished step never ran again"


async def test_a_killed_server_mid_call_resumes_and_replays_what_finished(pg_dsn: str) -> None:
    stores = leased(pg_dsn)
    wait, started = blocking_tool()

    async def flow(wf: WorkflowContext) -> tuple[str, str]:
        return await wf.agent("scan"), await wf.agent("probe")

    first = config([text_turn("scanned"), tool_turn("c1", "wait", {"q": "x"})], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(first, tools=[wait])
        driving = asyncio.create_task(run_workflow(agent, flow, run_id="w"))
        await asyncio.wait_for(started.wait(), 10)
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await driving

    wait, started = blocking_tool()
    events: list[WorkflowEvent] = []
    async with Server() as server:
        # The script holds only the turn the interrupted call still needs.
        second = config([text_turn("probed")], stores=stores)
        agent = await server.open_agent(second, tools=[wait])
        result = await when_free(
            lambda: run_workflow(agent, flow, run_id="w", on_event=events.append)
        )

    assert result == ("scanned", "probed")
    assert not started.is_set(), "the interrupted tool call never ran twice"
    assert kinds(events) == ["agent_replayed", "agent_start", "agent_end"]


async def test_an_edited_prompt_reruns_only_that_call(pg_dsn: str) -> None:
    stores = leased(pg_dsn)

    def flow(first: str) -> Callable[[WorkflowContext], Awaitable[list[str]]]:
        async def run(wf: WorkflowContext) -> list[str]:
            return [await wf.agent(first), await wf.agent("second prompt")]

        return run

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("a"), text_turn("b")], stores=stores))
        await run_workflow(agent, flow("first prompt"), run_id="w")
    events: list[WorkflowEvent] = []
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("c")], stores=stores))
        result = await when_free(
            lambda: run_workflow(agent, flow("first, edited"), run_id="w", on_event=events.append)
        )
    assert result == ["c", "b"]
    assert kinds(events) == ["agent_start", "agent_end", "agent_replayed"]


async def test_identical_parallel_calls_replay_in_any_order(pg_dsn: str) -> None:
    stores = leased(pg_dsn)

    async def flow(wf: WorkflowContext) -> list[str]:
        same = await wf.parallel([lambda: wf.agent("same")] * 3)
        return same + [await wf.agent("same")]

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("x")] * 4, stores=stores))
        await run_workflow(agent, flow, run_id="w")
    events: list[WorkflowEvent] = []
    async with Server() as server:
        agent = await server.open_agent(config([], stores=stores))
        result = await when_free(
            lambda: run_workflow(agent, flow, run_id="w", on_event=events.append)
        )
    assert result == ["x"] * 4
    assert kinds(events) == ["agent_replayed"] * 4


async def test_an_interrupt_parks_the_workflow_until_it_is_answered_after_a_restart(
    pg_dsn: str,
) -> None:
    stores = leased(pg_dsn)
    signed: list[str] = []

    async def flow(wf: WorkflowContext) -> str:
        draft = await wf.agent("draft the contract")
        approved = await wf.interrupt("approve", {"draft": draft})
        if not approved:
            return "withdrawn"
        await wf.step("sign", lambda: signed.append(draft) or "signed", key="contract-9")
        return "signed"

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("draft-a")], stores=stores))
        with pytest.raises(WorkflowSuspended) as parked:
            await run_workflow(agent, flow, run_id="w")
        parked_status = await workflow_status(agent, "w")
    assert (parked.value.key, parked.value.payload) == ("approve", {"draft": "draft-a"})
    assert parked_status.status == "awaiting_input"
    assert parked_status.interrupt_payload == {"draft": "draft-a"}
    assert signed == []

    async with Server() as server:
        agent = await server.open_agent(config([], stores=stores))
        result = await run_workflow(agent, flow, run_id="w", resume={"approve": True})
    assert (result, signed) == ("signed", ["draft-a"])

    async with Server() as server:
        agent = await server.open_agent(config([], stores=stores))
        again = await when_free(lambda: run_workflow(agent, flow, run_id="w"))
    assert (again, signed) == ("signed", ["draft-a"]), "the answer and the signature replay"


async def test_an_irreversible_step_is_rerun_with_the_same_idempotency_key(pg_dsn: str) -> None:
    stores = leased(pg_dsn)
    signatures: dict[str, str] = {}
    sent: list[str] = []

    def sign(key: str) -> str:
        # A signing service that dedupes on the key it is given.
        sent.append(key)
        return signatures.setdefault(key, hashlib.sha256(key.encode()).hexdigest()[:8])

    async def flow(wf: WorkflowContext, crash: bool) -> str:
        key = "contract-9"
        signature = await wf.step("sign", lambda: sign(key), key=key)
        if crash:
            raise RuntimeError("died after the call, before the journal write")
        return signature

    class Died(Exception):
        pass

    async with Server() as server:
        agent = await server.open_agent(config([], stores=stores))

        async def crash_after_the_function(wf: WorkflowContext) -> str:
            # The step's function ran; the client dies before the value is
            # journaled: begin without complete, as a kill would leave it.
            begun = await wf._call("workflow.step.begin", {"name": "sign", "key": "contract-9"})  # pyright: ignore[reportPrivateUsage]
            assert not begun["replayed"]
            sign("contract-9")
            raise Died

        with pytest.raises(Died):
            await run_workflow(agent, crash_after_the_function, run_id="w")

    async with Server() as server:
        agent = await server.open_agent(config([], stores=stores))
        result = await when_free(
            lambda: run_workflow(agent, lambda wf: flow(wf, False), run_id="w")
        )
    # The function ran again, with the same key, and the service answered
    # with the signature it already made.
    assert sent == ["contract-9", "contract-9"]
    assert len(signatures) == 1 and result == signatures["contract-9"]


async def test_a_failing_branch_of_parallel_aborts_its_siblings_but_settled_waits() -> None:
    wait, started = blocking_tool()

    async def boom() -> None:
        await asyncio.wait_for(started.wait(), 10)
        raise RuntimeError("boom")

    async def parallel(wf: WorkflowContext) -> None:
        await wf.parallel([lambda: wf.agent("block"), boom])

    stalled = config([tool_turn("c1", "wait", {"q": "x"})])
    async with Server() as server:
        agent = await server.open_agent(stalled, tools=[wait])
        with pytest.raises(RuntimeError, match="boom"):
            await run_workflow(agent, parallel, run_id="p")
        # The blocked sibling was aborted on the server, so the agent is idle.
        await agent.close()

    async def settled(wf: WorkflowContext) -> list[str | None]:
        async def bad() -> str:
            raise RuntimeError("bad")

        outcomes = await wf.settled([lambda: wf.agent("fine"), bad])
        assert [outcome.ok for outcome in outcomes] == [True, False]
        assert isinstance(outcomes[1].error, RuntimeError)
        return [outcome.value for outcome in outcomes]

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("done")]))
        assert await run_workflow(agent, settled, run_id="s") == ["done", None]


async def test_the_budget_stops_the_next_agent_call() -> None:
    async def flow(wf: WorkflowContext) -> None:
        await wf.agent("one")
        await wf.agent("two")

    # Each scripted turn reports 4 tokens.
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("a"), text_turn("b")]))
        with pytest.raises(WorkflowBudgetExceeded) as spent:
            await run_workflow(agent, flow, run_id="w", budget=Budget(max_tokens=4))
    assert spent.value.spent_tokens == 4


async def test_a_deadline_and_a_step_timeout_end_the_workflow() -> None:
    async def slow(wf: WorkflowContext) -> None:
        await wf.step("slow", lambda: asyncio.sleep(30))

    async with Server() as server:
        agent = await server.open_agent(config([]))
        with pytest.raises(WorkflowTimeoutError, match="deadline"):
            await run_workflow(agent, slow, run_id="d", deadline_ms=300)
        with pytest.raises(WorkflowTimeoutError, match="step slow"):
            await run_workflow(agent, slow, run_id="t", step_timeout_ms=300)

        # A step may opt out of the default.
        async def quick(wf: WorkflowContext) -> int:
            return await wf.step("q", lambda: 1, timeout_ms=0)

        assert await run_workflow(agent, quick, run_id="q", step_timeout_ms=300) == 1


async def test_a_retried_step_is_journaled_once() -> None:
    attempts: list[int] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("not yet")
        return "ok"

    from linch_rust import Retry

    async def flow(wf: WorkflowContext) -> Any:
        return await wf.step("flaky", flaky, retry=Retry(max_attempts=3, base_delay_ms=10))

    async with Server() as server:
        agent = await server.open_agent(config([]))
        assert await run_workflow(agent, flow, run_id="r") == "ok"
    assert len(attempts) == 3


async def test_a_second_client_is_refused_while_the_first_drives(pg_dsn: str) -> None:
    stores = leased(pg_dsn)
    release = asyncio.Event()

    async def slow(wf: WorkflowContext) -> str:
        await wf.step("hold", lambda: 1)
        await release.wait()
        return "first"

    async with Server() as one, Server() as two:
        first = await one.open_agent(config([], stores=stores))
        second = await two.open_agent(config([], stores=stores))
        driving = asyncio.create_task(run_workflow(first, slow, run_id="w"))
        await asyncio.sleep(0.5)
        with pytest.raises(WorkflowHeld):
            await run_workflow(second, slow, run_id="w")
        # The refusal changed nothing: the first client still writes and finishes.
        release.set()
        assert await driving == "first"
        status = await workflow_status(first, "w")
    assert (status.status, status.records) == ("completed", 1)


async def test_answers_for_a_workflow_with_no_journal_are_refused() -> None:
    """A memory-store agent's journal ends with its connection, so a resume
    with answers on a new connection has nothing to answer."""

    async def flow(wf: WorkflowContext) -> bool:
        return bool(await wf.interrupt("approve", {"plan": "p"}))

    async with Server() as server:
        agent = await server.open_agent(config([]))
        with pytest.raises(WorkflowNoJournal):
            await run_workflow(agent, flow, run_id="w", resume={"approve": True})
        # Nothing was claimed: the workflow starts and parks as usual.
        with pytest.raises(WorkflowSuspended):
            await run_workflow(agent, flow, run_id="w")
        parked = await workflow_status(agent, "w")
    assert (parked.status, parked.interrupt_key) == ("awaiting_input", "approve")


async def test_a_call_with_no_started_workflow_is_refused() -> None:
    async with Server() as server:
        agent = await server.open_agent(config([]))
        wf = WorkflowContext(
            agent._conn,  # pyright: ignore[reportPrivateUsage]
            "nope",
            max_concurrency=1,
            step_timeout_ms=None,
            budget=None,
            on_event=None,
        )
        with pytest.raises(WorkflowNotFound):
            await wf.agent("hi")
        with pytest.raises(WorkflowCallFailed) as unknown:

            async def flow(wf: WorkflowContext) -> None:
                await wf.agent("hi", subagent="missing")

            await run_workflow(agent, flow, run_id="w")
        assert unknown.value.kind == "invalid"


async def test_fork_continues_the_conversation_of_an_earlier_call() -> None:
    async def flow(wf: WorkflowContext) -> tuple[str, str]:
        recon = await wf.agent_result("remember host 10.0.0.7")
        again = await wf.agent_result("which host?", fork=recon)
        assert again.session_id != recon.session_id
        return recon.text, again.text

    async with Server() as server:
        agent = await server.open_agent(config([text_turn("noted"), text_turn("10.0.0.7")]))
        assert await run_workflow(agent, flow, run_id="w") == ("noted", "10.0.0.7")


# run_or_resume


async def test_run_or_resume_runs_a_new_run_and_returns_a_finished_one_without_a_model_call(
    pg_dsn: str,
) -> None:
    stores = leased(pg_dsn)
    async with Server() as server:
        agent = await server.open_agent(config([text_turn("Done.")], stores=stores))
        first = await run_or_resume(agent, "job-1", "Do the job")
    assert isinstance(first, ResultEvent) and first.final_text == "Done."

    async with Server() as server:
        # No turn at all: a model call would fail the run.
        agent = await server.open_agent(config([], stores=stores))
        again = await run_or_resume(agent, "job-1", "Do the job")
        events = await agent.events("job-1")
    assert isinstance(again, ResultEvent) and again.final_text == "Done."
    assert [e.event.type for e in events].count("result") == 1


async def test_run_or_resume_continues_a_run_whose_server_was_killed(pg_dsn: str) -> None:
    stores = leased(pg_dsn)
    wait, started = blocking_tool()
    first = config([tool_turn("c1", "wait", {"q": "x"})], stores=stores)
    async with Server() as server:
        agent = await server.open_agent(first, tools=[wait])
        driving = asyncio.create_task(run_or_resume(agent, "job-2", "Probe"))
        await asyncio.wait_for(started.wait(), 10)
        os.kill(server.pid, signal.SIGKILL)
        with pytest.raises(ConnectionClosed):
            await driving

    wait, started = blocking_tool()
    async with Server() as server:
        second = config([text_turn("Recovered.")], stores=stores)
        agent = await server.open_agent(second, tools=[wait])
        result = await run_or_resume(agent, "job-2", "Probe", wait=20)
    assert isinstance(result, ResultEvent) and result.final_text == "Recovered."
    assert not started.is_set()


async def test_run_or_resume_waits_for_a_run_another_server_drives(pg_dsn: str) -> None:
    stores = leased(pg_dsn)
    wait, started = blocking_tool()
    async with Server() as one, Server() as two:
        busy = await one.open_agent(
            config([tool_turn("c1", "wait", {"q": "x"})], stores=stores), tools=[wait]
        )
        run = await busy.run("job-3", "Probe", run_id="job-3")
        await asyncio.wait_for(started.wait(), 10)
        # The same tools, so the same contract.
        other_wait, _ = blocking_tool()
        other = await two.open_agent(config([], stores=stores), tools=[other_wait])
        with pytest.raises(RunLeased):
            await run_or_resume(other, "job-3", "Probe", wait=0.5)
        await run.cancel()
        async for _ in run:
            pass


async def test_run_or_resume_returns_a_run_that_waits_for_an_approval(pg_dsn: str) -> None:
    stores = leased(pg_dsn)

    @tool(scope="write")
    async def edit(q: str) -> str:
        return q

    turns = [tool_turn("c1", "edit", {"q": "x"}), text_turn("Edited.")]
    async with Server() as server:
        agent = await server.open_agent(config(turns, stores=stores), tools=[edit])
        first = await run_or_resume(agent, "job-4", "Edit it")
        again = await run_or_resume(agent, "job-4", "Edit it")
    assert isinstance(first, SuspendedEvent) and isinstance(again, SuspendedEvent)


async def test_a_workflow_is_resumed_only_by_the_principal_it_started_with() -> None:
    alice: Principal = {"tenant": "acme", "user": "alice", "roles": ["dev"]}
    bob: Principal = {"tenant": "acme", "user": "bob"}
    async with Server() as server:
        agent = await server.open_agent(config([]))

        async def flow(wf: WorkflowContext) -> str:
            return await wf.step("n", lambda: "ok")

        assert await run_workflow(agent, flow, run_id="w", principal=alice) == "ok"
        for other in (bob, None):
            with pytest.raises(NotAllowed) as refused:
                await run_workflow(agent, flow, run_id="w", principal=other)
            assert refused.value.kind == "workflow_principal"
        assert await run_workflow(agent, flow, run_id="w", principal=alice) == "ok"
