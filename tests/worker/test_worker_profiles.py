"""Queued profile and principal references are resolved by fleet workers."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from test_dispatch import running, wait_for

from tests.support.agents import make_worker_agent as make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.tools import StubTool, tool_registry

_PROFILE_CRASH_WORKER = r"""
import asyncio, sys
import asyncpg
from linch import Agent, Profile, empty_tools
from linch.ext.evals import ScriptedProvider, TextTurn
from linch.worker import Worker

async def main():
    pool = await asyncpg.create_pool(sys.argv[1], min_size=1, max_size=5,
                                     server_settings={'search_path': sys.argv[2]})
    profile = Profile(id='support', version='1', system_prompt='Support role')
    class Blocked(ScriptedProvider):
        async def stream(self, request):
            await asyncio.Event().wait()
            async for event in super().stream(request):
                yield event
    async def factory(job):
        return Agent(model='m', provider=Blocked([TextTurn('never')]),
                     tools=empty_tools(), permissions={'mode':'skip-dangerous'},
                     session_store=job.session_store, run_store=job.run_store)
    await Worker(pool, factory, worker_id='profile-worker-a', profiles=(profile,),
                 lease_ttl_s=.6, stall_timeout_s=1.2, poll_interval_s=.05).run()
asyncio.run(main())
"""


async def test_enqueue_persists_profile_and_principal_refs_and_worker_applies_them(pool):
    from linch import Agent, Principal, Profile
    from linch.worker import Worker, enqueue_run

    profile = Profile(
        id="support",
        version="2026-10-31",
        system_prompt="Support role",
        tool_allow=("whoami",),
    )
    principal = Principal("tenant-a", "alice", frozenset({"support"}))
    seen = []
    provider = ScriptedProvider([ToolUseTurn("whoami", {}), TextTurn("done")])
    tool = StubTool(
        "whoami", output="captured", on_execute=lambda _input, ctx: seen.append(ctx.principal)
    )

    async def factory(job):
        return Agent(
            model="m",
            provider=provider,
            tools=tool_registry(tool),
            permissions={"mode": "skip-dangerous"},
            session_store=job.session_store,
            run_store=job.run_store,
        )

    rid = await enqueue_run(pool, "support-session", "help", profile=profile, principal=principal)
    payload = json.loads(await pool.fetchval("SELECT payload FROM pgqueuer"))
    row = await pool.fetchrow("SELECT meta FROM runs WHERE id=$1", rid)
    metadata = json.loads(row["meta"])

    assert payload["profile"] == metadata["profile"]
    assert payload["principal"] == metadata["principal"]
    assert payload["profile"]["id"] == "support"
    assert payload["profile"]["version"] == "2026-10-31"
    assert payload["profile"]["content_hash"].startswith("sha256:")
    assert payload["principal"] == {
        "tenant": "tenant-a",
        "user": "alice",
        "roles": ["support"],
    }

    worker = Worker(pool, factory, profiles=(profile,), poll_interval_s=0.02)
    async with running(worker):
        await wait_for(
            lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
        )

    assert len(provider.requests) == 2
    assert any("Support role" in block.text for block in provider.requests[0].system)
    assert seen == [principal]


async def test_missing_or_changed_queued_profile_fails_before_provider_dispatch(pool):
    from linch import Profile
    from linch.worker import Worker, enqueue_run

    source = Profile(id="billing", version="1", system_prompt="Billing role")
    changed = Profile(id="billing", version="1", system_prompt="Different role")
    provider = ScriptedProvider([])

    async def factory(job):
        return make_agent(job, provider=provider)

    missing_id = await enqueue_run(pool, "missing", "hello", profile=source)
    worker = Worker(pool, factory, poll_interval_s=0.02)
    async with running(worker):
        await wait_for(
            lambda: pool.fetchval("SELECT status='failed' FROM runs WHERE id=$1", missing_id)
        )
    missing_meta = await pool.fetchval("SELECT meta FROM runs WHERE id=$1", missing_id)
    assert "billing" in missing_meta
    assert not provider.requests

    changed_id = await enqueue_run(pool, "changed", "hello", profile=source)
    worker = Worker(pool, factory, profiles=(changed,), poll_interval_s=0.02)
    async with running(worker):
        await wait_for(
            lambda: pool.fetchval("SELECT status='failed' FROM runs WHERE id=$1", changed_id)
        )
    changed_meta = await pool.fetchval("SELECT meta FROM runs WHERE id=$1", changed_id)
    assert "content_hash" in changed_meta
    assert not provider.requests


async def test_profile_and_principal_refs_are_optional_for_legacy_queue_payloads(pool):
    from linch.worker import Worker, enqueue_run

    rid = await enqueue_run(pool, "legacy", "hello")
    payload = json.loads(await pool.fetchval("SELECT payload FROM pgqueuer"))
    metadata = json.loads(await pool.fetchval("SELECT meta FROM runs WHERE id=$1", rid))
    assert "profile" not in payload
    assert "principal" not in payload
    assert metadata == {}

    async def factory(job):
        return make_agent(job)

    async with running(Worker(pool, factory, poll_interval_s=0.02)):
        await wait_for(
            lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
        )


async def test_expired_lease_takeover_uses_the_pinned_profile_contract(pool):
    from linch import PostgresRunStore, Profile
    from linch.worker import Worker, enqueue_run

    profile = Profile(id="support", version="1", system_prompt="Support role")
    rid = await enqueue_run(pool, "takeover", "hello", profile=profile)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _PROFILE_CRASH_WORKER,
        os.environ["AGENT_KIT_TEST_PG_DSN"],
        await pool.fetchval("SELECT current_schema()"),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stale = PostgresRunStore("", pool=pool)
    try:
        await wait_for(
            lambda: pool.fetchval(
                "SELECT checkpoint::jsonb->>'phase'='provider_pending' FROM runs WHERE id=$1", rid
            )
        )
        original = await stale.claim(rid, "profile-worker-a", 0.6)
        proc.kill()
        await proc.wait()

        async def factory(job):
            return make_agent(job)

        worker = Worker(
            pool,
            factory,
            worker_id="profile-worker-b",
            profiles=(profile,),
            lease_ttl_s=0.6,
            stall_timeout_s=1.2,
            poll_interval_s=0.05,
        )
        async with running(worker):
            await wait_for(
                lambda: pool.fetchval("SELECT status='completed' FROM runs WHERE id=$1", rid)
            )
            await wait_for(lambda: pool.fetchval("SELECT count(*)=0 FROM pgqueuer"))
        assert await pool.fetchval(
            "SELECT lease_epoch>$2 FROM runs WHERE id=$1", rid, original.epoch
        )
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        _, stderr = await proc.communicate()
        assert b"Traceback" not in stderr, stderr.decode()


async def test_pending_adoption_rejects_changed_profile_and_principal_refs(pool):
    from linch import Agent, ConfigError, Principal, Profile, RunOptions
    from linch.run_stores.postgres import PostgresRunStore
    from linch.sessions.postgres import PostgresSessionStore
    from linch.worker import enqueue_run
    from linch.worker.dispatch import _BorrowedPool

    stored_profile = Profile(id="support", version="1", system_prompt="Stored role")
    changed_profile = Profile(id="support", version="1", system_prompt="Changed role")
    stored_principal = Principal("tenant-a", "alice", frozenset({"support"}))
    changed_principal = Principal("tenant-a", "mallory", frozenset({"support"}))

    providers: list[ScriptedProvider] = []

    async def adopt(
        session_id: str, run_id: str, profile: Profile | None, principal: Principal
    ) -> None:
        provider = ScriptedProvider([])
        providers.append(provider)
        agent = Agent(
            model="m",
            provider=provider,
            tools=tool_registry(),
            permissions={"mode": "skip-dangerous"},
            session_store=PostgresSessionStore("", pool=_BorrowedPool(pool)),
            run_store=PostgresRunStore("", pool=_BorrowedPool(pool)),
        )
        try:
            session = await agent.session(id=session_id)
            [
                event
                async for event in session.run(
                    "hello", RunOptions(run_id=run_id, profile=profile, principal=principal)
                )
            ]
        finally:
            await agent.close()

    profile_run = await enqueue_run(
        pool, "adopt-profile", "hello", profile=stored_profile, principal=stored_principal
    )
    with pytest.raises(ConfigError, match="profile"):
        await adopt("adopt-profile", profile_run, changed_profile, stored_principal)

    missing_profile_run = await enqueue_run(
        pool, "adopt-profile-missing", "hello", profile=stored_profile, principal=stored_principal
    )
    with pytest.raises(ConfigError, match="profile"):
        await adopt("adopt-profile-missing", missing_profile_run, None, stored_principal)

    principal_run = await enqueue_run(
        pool, "adopt-principal", "hello", profile=stored_profile, principal=stored_principal
    )
    with pytest.raises(ConfigError, match="principal"):
        await adopt("adopt-principal", principal_run, stored_profile, changed_principal)
    assert [len(provider.requests) for provider in providers] == [0, 0, 0]
