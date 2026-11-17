"""Live: jobs of the worker with a real model: an erroring run, the queue
cap and a tightened allowlist. Skips without the live environment.
"""

from __future__ import annotations

import pytest
from live_sched_support import (
    TOKEN,
    kind,
    live_config,
    pg_dsn,
    serving,
    tenant_allow,
    until,
    vllm,
)

from linch_rust import ErrorEvent, Job, LimitReached, ResultEvent

__all__ = ["kind", "pg_dsn", "vllm"]

pytestmark = pytest.mark.live

PING = "Reply with the single word PONG and nothing else."


async def test_a_run_that_errors_is_a_done_job_with_an_error_result(vllm: str, pg_dsn: str) -> None:
    # A wrong model name is a 404 from vLLM: not retryable, so the run ends
    # with an error event; the job did its work, so it is `done`.
    config = live_config(vllm, "acme", pg_dsn, spec={"model": "no-such-model"})
    async with serving(pg_dsn) as live, live.connect() as server:
        agent = await server.open_agent(config)
        job = await agent.enqueue("bad-model", PING)
        done = await agent.wait_job(job["job_id"], timeout=60)
        assert done["state"] == "done"
        run_id = done.get("run_id")
        assert run_id is not None
        events = await agent.events(run_id)
        last = events[-1].event
        assert isinstance(last, ResultEvent) and last.subtype == "error"
        assert any(isinstance(e.event, ErrorEvent) for e in events)


async def test_the_queue_cap_holds_and_a_tightened_allowlist_fails_the_job(
    vllm: str, pg_dsn: str
) -> None:
    allow = tenant_allow(pg_dsn, max_queued_jobs=2)
    tenants = {"acme": {"tokens": [{"value": TOKEN}], "allow": allow}}
    # No worker yet: the jobs stay queued, so the cap counts them.
    async with serving(pg_dsn, tenants=tenants, worker=False) as live:
        async with live.connect() as server:
            agent = await server.open_agent(live_config(vllm, "acme", pg_dsn))
            first = await agent.enqueue("cap-1", PING)
            second = await agent.enqueue("cap-2", PING)
            with pytest.raises(LimitReached) as refused:
                await agent.enqueue("cap-3", PING)
            assert refused.value.kind == "jobs"
            assert {first["state"], second["state"]} == {"queued"}

    # The operator removes the vLLM endpoint from the allowlist; the worker
    # checks it again at every attempt, so the stored jobs fail with the reason.
    tight = tenant_allow(pg_dsn, max_queued_jobs=2)
    del tight["provider_base_urls"]
    tenants = {"acme": {"tokens": [{"value": TOKEN}], "allow": tight}}
    async with serving(pg_dsn, tenants=tenants) as live, live.connect() as server:
        # The tightened tenant refuses the vLLM config, so read the jobs
        # through an agent of the same namespace on the public DeepSeek endpoint.
        agent = await server.open_agent(live_config("deepseek", "acme", pg_dsn))
        failed: list[Job] = []

        async def check() -> bool:
            failed[:] = [job for job in await agent.jobs() if job["state"] == "failed"]
            return len(failed) == 2

        await until(check, timeout=40, what="both jobs to fail")
        for job in failed:
            reason = job.get("error", "")
            assert reason, "a failed job says why"
            assert "postgres://" not in reason, "no DSN in the reason"
