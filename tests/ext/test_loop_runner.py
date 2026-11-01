from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace

import pytest

from linch import ConfigError, OutputSchema, RunOptions
from linch.ext import loop_runner as loop_runner_module
from linch.ext.loop_runner import (
    FileLoopLeaseStore,
    InMemoryLoopLeaseStore,
    LoopRunner,
    LoopSpec,
    LoopTickResult,
    LoopTrigger,
)
from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.stores import memory_session_store


def _agent(provider: ScriptedProvider, tmp_path):
    return make_agent(provider, model="gpt-5", cwd=str(tmp_path), run_store=None)


def _spec(tmp_path, loop_id: str = "docs-loop") -> LoopSpec:
    return LoopSpec(
        id=loop_id,
        charter="Maintain the docs domain.",
        prompt="Inspect the domain and report progress.",
        root=tmp_path / "domains",
        session_meta={"owner": "tests"},
    )


async def test_run_once_creates_fresh_session_report_and_artifacts(tmp_path) -> None:
    provider = ScriptedProvider([TextTurn(text="done"), TextTurn(text="next")])
    runner = LoopRunner(_agent(provider, tmp_path))
    spec = _spec(tmp_path)

    result = await runner.run_once(spec, LoopTrigger(source="manual", payload="tick"))
    second = await runner.run_once(spec)

    domain = tmp_path / "domains" / spec.id
    assert result.status == "completed"
    assert result.final_text == "done"
    assert result.report.session_id
    assert second.iteration == 2
    assert second.report.session_id != result.report.session_id
    assert second.final_text == "next"
    assert (domain / "README.md").read_text(encoding="utf-8").startswith("# Loop Domain")
    log = (domain / "LOG.md").read_text(encoding="utf-8")
    assert "Tick 1" in log
    assert "Tick 2" in log
    assert len(result.artifact_paths) == 2
    assert (domain / "artifacts" / "runs" / f"{result.run_id}.md").exists()
    report_json = json.loads(
        (domain / "artifacts" / "runs" / f"{result.run_id}.json").read_text(encoding="utf-8")
    )
    assert report_json["run_id"] == result.run_id
    assert report_json["status"] == "completed"


async def test_run_once_uses_fresh_session_each_tick(tmp_path) -> None:
    provider = ScriptedProvider([TextTurn(text="one"), TextTurn(text="two")])
    runner = LoopRunner(_agent(provider, tmp_path))
    spec = _spec(tmp_path)

    first = await runner.run_once(spec)
    second = await runner.run_once(spec)

    assert first.iteration == 1
    assert second.iteration == 2
    assert first.report.session_id != second.report.session_id
    assert first.final_text == "one"
    assert second.final_text == "two"
    log = (tmp_path / "domains" / spec.id / "LOG.md").read_text(encoding="utf-8")
    assert "Tick 1" in log
    assert "Tick 2" in log


async def test_run_once_exposes_structured_output(tmp_path) -> None:
    schema = OutputSchema(
        name="tick",
        schema={
            "type": "object",
            "properties": {"done": {"type": "boolean"}},
            "required": ["done"],
        },
    )
    provider = ScriptedProvider([TextTurn(text='{"done":true}')])
    runner = LoopRunner(_agent(provider, tmp_path))
    spec = _spec(tmp_path)
    spec.run_options = RunOptions(output_schema=schema)

    result = await runner.run_once(spec)

    assert result.final_text == '{"done":true}'
    assert result.structured_output == {"done": True}
    assert result.structured_error is None


async def test_predicates_can_mark_done_from_report_summary(tmp_path) -> None:
    provider = ScriptedProvider([TextTurn(text="finished")])

    def done(result: LoopTickResult, _artifacts) -> bool:
        return result.report.summary["event_counts"]["result"] == 1

    runner = LoopRunner(_agent(provider, tmp_path), done_predicate=done)
    result = await runner.run_once(_spec(tmp_path))

    assert result.done is True


async def test_failed_verifier_marks_not_done_and_logs_failure(tmp_path) -> None:
    provider = ScriptedProvider([TextTurn(text="draft")])

    async def verify(_result: LoopTickResult) -> bool:
        return False

    runner = LoopRunner(_agent(provider, tmp_path), verify=verify)
    spec = _spec(tmp_path)

    result = await runner.run_once(spec)

    assert result.done is False
    assert result.status == "verification_failed"
    assert result.report.status == "verification_failed"
    log = (tmp_path / "domains" / spec.id / "LOG.md").read_text(encoding="utf-8")
    assert "verification_error: verify returned False" in log


async def test_concurrent_run_once_same_loop_id_raises(tmp_path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    provider = ScriptedProvider([TextTurn(text="held")])

    async def verify(_result: LoopTickResult) -> bool:
        entered.set()
        await release.wait()
        return True

    runner = LoopRunner(_agent(provider, tmp_path), verify=verify)
    spec = _spec(tmp_path)
    task = asyncio.create_task(runner.run_once(spec))
    await entered.wait()
    try:
        with pytest.raises(ConfigError, match="already has an active"):
            await runner.run_once(spec)
    finally:
        release.set()
    await task


async def test_independent_runners_same_loop_id_do_not_collide(tmp_path) -> None:
    # Regression: the active-run guard is per-instance, not process-global, so
    # two unrelated LoopRunners (e.g. two agents in one process) may run the
    # same spec.id concurrently without a spurious ConfigError.
    entered = asyncio.Event()
    release = asyncio.Event()

    async def verify(_result: LoopTickResult) -> bool:
        entered.set()
        await release.wait()
        return True

    runner_a = LoopRunner(_agent(ScriptedProvider([TextTurn(text="a")]), tmp_path), verify=verify)
    runner_b = LoopRunner(_agent(ScriptedProvider([TextTurn(text="b")]), tmp_path / "b"))
    spec = _spec(tmp_path)
    task = asyncio.create_task(runner_a.run_once(spec))
    await entered.wait()
    try:
        # Same spec.id, different runner — must not raise.
        result_b = await runner_b.run_once(_spec(tmp_path / "b"))
        assert result_b.status == "completed"
    finally:
        release.set()
    await task


async def test_durable_lease_blocks_second_runner(tmp_path) -> None:
    lease_store = InMemoryLoopLeaseStore()
    held = await lease_store.try_acquire("docs-loop", owner="other", ttl_s=60)
    assert held is not None
    provider = ScriptedProvider([TextTurn(text="blocked")])
    runner = LoopRunner(_agent(provider, tmp_path), leases=lease_store, lease_owner="runner")

    with pytest.raises(ConfigError, match="active lease"):
        await runner.run_once(_spec(tmp_path))


async def test_file_lease_store_reclaims_stale_lease(tmp_path) -> None:
    store = FileLoopLeaseStore(root=tmp_path / "domains")
    lease = await store.try_acquire("docs-loop", owner="first", ttl_s=60)
    assert lease is not None
    assert await store.try_acquire("docs-loop", owner="second", ttl_s=60) is None

    lock = tmp_path / "domains" / "docs-loop" / ".lock.json"
    lock.write_text(
        json.dumps(
            {
                "loop_id": lease.loop_id,
                "owner": lease.owner,
                "token": lease.token,
                "expires_at": 0,
            }
        ),
        encoding="utf-8",
    )

    reclaimed = await store.try_acquire("docs-loop", owner="second", ttl_s=60)

    assert reclaimed is not None
    assert reclaimed.owner == "second"
    assert reclaimed.token != lease.token


async def test_file_lease_store_refresh_and_release(tmp_path) -> None:
    store = FileLoopLeaseStore(root=tmp_path / "domains")
    lease = await store.try_acquire("docs-loop", owner="owner", ttl_s=60)
    assert lease is not None

    refreshed = await store.refresh(lease, ttl_s=120)
    await store.release(refreshed)
    reacquired = await store.try_acquire("docs-loop", owner="other", ttl_s=60)

    assert refreshed.expires_at > lease.expires_at
    assert reacquired is not None


async def test_file_lease_store_requires_owner_on_refresh_and_release(tmp_path) -> None:
    store = FileLoopLeaseStore(root=tmp_path / "domains")
    lease = await store.try_acquire("docs-loop", owner="owner", ttl_s=60)
    assert lease is not None
    forged = replace(lease, owner="other")

    with pytest.raises(ConfigError, match="not held by this owner"):
        await store.refresh(forged, ttl_s=60)
    await store.release(forged)
    assert await store.try_acquire("docs-loop", owner="other", ttl_s=60) is None

    await store.release(lease)


async def test_inmemory_lease_store_requires_owner_on_refresh_and_release() -> None:
    store = InMemoryLoopLeaseStore()
    lease = await store.try_acquire("docs-loop", owner="owner", ttl_s=60)
    assert lease is not None
    forged = replace(lease, owner="other")

    with pytest.raises(ConfigError, match="not held by this owner"):
        await store.refresh(forged, ttl_s=60)
    await store.release(forged)
    assert await store.try_acquire("docs-loop", owner="other", ttl_s=60) is None

    await store.release(lease)


async def test_inmemory_lease_store_rejects_expired_refresh(monkeypatch) -> None:
    now = 100.0
    monkeypatch.setattr(loop_runner_module.time, "time", lambda: now)
    store = InMemoryLoopLeaseStore()
    lease = await store.try_acquire("docs-loop", owner="owner", ttl_s=1)
    assert lease is not None

    now = 102.0
    with pytest.raises(ConfigError, match="expired"):
        await store.refresh(lease, ttl_s=60)


async def test_file_lease_refresh_refuses_symlinked_lock(tmp_path) -> None:
    # Hardening: a symlink planted at the lock path must not let a refresh write
    # the lease JSON through it to an arbitrary target (O_NOFOLLOW). The refresh
    # raises instead of following the link, and the target file is untouched.
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW not available on this platform")
    store = FileLoopLeaseStore(root=tmp_path / "domains")
    lease = await store.try_acquire("docs-loop", owner="owner", ttl_s=60)
    assert lease is not None

    lock = tmp_path / "domains" / "docs-loop" / ".lock.json"
    target = tmp_path / "victim.txt"
    target.write_text("untouched", encoding="utf-8")
    lock.unlink()
    lock.symlink_to(target)

    # The symlink is refused (either at the O_NOFOLLOW read, surfacing as
    # ConfigError, or at the write) — never followed, so the target is untouched.
    with pytest.raises((OSError, ConfigError)):
        await store.refresh(lease, ttl_s=120)
    assert target.read_text(encoding="utf-8") == "untouched"


async def test_file_lease_store_recovers_malformed_lock(tmp_path) -> None:
    lock = tmp_path / "domains" / "docs-loop" / ".lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_text("{not json", encoding="utf-8")
    store = FileLoopLeaseStore(root=tmp_path / "domains")

    lease = await store.try_acquire("docs-loop", owner="owner", ttl_s=60)

    assert lease is not None
    assert json.loads(lock.read_text(encoding="utf-8"))["owner"] == "owner"


async def test_file_lease_store_recovers_malformed_utf8_lock(tmp_path) -> None:
    lock = tmp_path / "domains" / "docs-loop" / ".lock.json"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"\xff")
    store = FileLoopLeaseStore(root=tmp_path / "domains")

    lease = await store.try_acquire("docs-loop", owner="owner", ttl_s=60)

    assert lease is not None
    assert json.loads(lock.read_text(encoding="utf-8"))["owner"] == "owner"


async def test_file_lease_store_does_not_steal_an_in_progress_acquisition(
    tmp_path, monkeypatch
) -> None:
    store = FileLoopLeaseStore(root=tmp_path / "domains")
    entered = threading.Event()
    proceed = threading.Event()
    original_fdopen = loop_runner_module.os.fdopen
    first = True

    def delayed_fdopen(*args, **kwargs):
        nonlocal first
        handle = original_fdopen(*args, **kwargs)
        if first:
            first = False
            entered.set()
            assert proceed.wait(timeout=2)
        return handle

    monkeypatch.setattr(loop_runner_module.os, "fdopen", delayed_fdopen)
    first_task = asyncio.create_task(store.try_acquire("docs-loop", owner="first", ttl_s=60))
    await asyncio.to_thread(entered.wait)
    second_task = asyncio.create_task(store.try_acquire("docs-loop", owner="second", ttl_s=60))
    try:
        second = await asyncio.wait_for(asyncio.shield(second_task), timeout=0.1)
    except TimeoutError:
        second = None
    proceed.set()
    first = await first_task
    if not second_task.done():
        second = await second_task

    assert first is not None
    assert second is None


async def test_file_lease_store_treats_empty_lock_as_held(tmp_path) -> None:
    lock = tmp_path / "domains" / "docs-loop" / ".lock.json"
    lock.parent.mkdir(parents=True)
    lock.touch()
    store = FileLoopLeaseStore(root=tmp_path / "domains")

    assert await store.try_acquire("docs-loop", owner="second", ttl_s=60) is None


async def test_file_lease_store_serializes_refresh_and_reclaim(tmp_path, monkeypatch) -> None:
    store = FileLoopLeaseStore(root=tmp_path / "domains")
    lease = await store.try_acquire("docs-loop", owner="first", ttl_s=60)
    assert lease is not None
    truncated = threading.Event()
    proceed = threading.Event()
    original_open = loop_runner_module.os.open

    def delayed_open(path, flags, mode=0o777):
        fd = original_open(path, flags, mode)
        if flags & os.O_TRUNC:
            truncated.set()
            assert proceed.wait(timeout=2)
        return fd

    monkeypatch.setattr(loop_runner_module.os, "open", delayed_open)
    refresh_task = asyncio.create_task(store.refresh(lease, ttl_s=60))
    try:
        await asyncio.wait_for(asyncio.to_thread(truncated.wait, 1), timeout=0.2)
    except TimeoutError:
        # The fixed implementation publishes via atomic replacement and never
        # opens the live path with O_TRUNC.
        refreshed = await refresh_task
        assert refreshed.token == lease.token
        assert await store.try_acquire("docs-loop", owner="second", ttl_s=60) is None
        return
    second_task = asyncio.create_task(store.try_acquire("docs-loop", owner="second", ttl_s=60))
    try:
        second = await asyncio.wait_for(asyncio.shield(second_task), timeout=0.1)
    except TimeoutError:
        second = None
    proceed.set()
    refreshed = await refresh_task
    if not second_task.done():
        second = await second_task

    assert refreshed.token == lease.token
    assert second is None


async def test_run_once_releases_retained_subagent_sessions(tmp_path) -> None:
    from linch.ext.deep_agent import create_deep_agent

    provider = ScriptedProvider(
        [
            turn
            for _ in range(3)
            for turn in (
                ToolUseTurn("Subagent", {"description": "helper", "prompt": "inspect"}),
                TextTurn("child done"),
                TextTurn("tick done"),
            )
        ]
    )
    agent = create_deep_agent(
        model="gpt-5",
        provider=provider,
        session_store=memory_session_store(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        durable=False,
    )
    runner = LoopRunner(agent)

    for _ in range(3):
        await runner.run_once(_spec(tmp_path))

    assert agent._sessions == {}


async def test_run_once_renews_long_tick_lease(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(ScriptedProvider):
        async def stream(self, request):
            started.set()
            await release.wait()
            async for event in super().stream(request):
                yield event

    class RecordingLeaseStore(InMemoryLoopLeaseStore):
        def __init__(self) -> None:
            super().__init__()
            self.refreshed = asyncio.Event()

        async def refresh(self, lease, *, ttl_s):
            self.refreshed.set()
            return await super().refresh(lease, ttl_s=ttl_s)

    lease_store = RecordingLeaseStore()
    agent = _agent(BlockingProvider([TextTurn("done")]), tmp_path)
    runner = LoopRunner(
        agent,
        leases=lease_store,
        lease_owner="runner",
        lease_ttl_s=0.03,
    )
    task = asyncio.create_task(runner.run_once(_spec(tmp_path)))
    await asyncio.wait_for(started.wait(), timeout=1)
    renewed = False
    try:
        await asyncio.wait_for(lease_store.refreshed.wait(), timeout=1)
        renewed = True
    except TimeoutError:
        pass
    finally:
        release.set()
    result = await task

    assert renewed
    assert result.status == "completed"


async def test_run_once_stops_when_lease_renewal_fails(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingProvider(ScriptedProvider):
        async def stream(self, request):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            async for event in super().stream(request):
                yield event

    class FailingLeaseStore(InMemoryLoopLeaseStore):
        def __init__(self) -> None:
            super().__init__()
            self.released = asyncio.Event()

        async def refresh(self, lease, *, ttl_s):
            raise ConfigError("lease lost")

        async def release(self, lease) -> None:
            self.released.set()
            await super().release(lease)

    lease_store = FailingLeaseStore()
    agent = _agent(BlockingProvider([TextTurn("done")]), tmp_path)
    runner = LoopRunner(
        agent,
        leases=lease_store,
        lease_owner="runner",
        lease_ttl_s=0.03,
    )
    task = asyncio.create_task(runner.run_once(_spec(tmp_path)))
    await asyncio.wait_for(started.wait(), timeout=1)
    try:
        with pytest.raises(ConfigError, match="lease lost"):
            await asyncio.wait_for(asyncio.shield(task), timeout=1)
    except TimeoutError:
        release.set()
        await task
        pytest.fail("run_once did not stop after lease renewal failed")

    assert cancelled.is_set()
    assert lease_store.released.is_set()


async def test_run_once_clears_active_guard_when_release_fails(tmp_path) -> None:
    class FailingReleaseStore(InMemoryLoopLeaseStore):
        async def release(self, lease) -> None:
            await super().release(lease)
            raise ConfigError("release failed")

    lease_store = FailingReleaseStore()
    agent = _agent(ScriptedProvider([TextTurn("done"), TextTurn("done")]), tmp_path)
    runner = LoopRunner(
        agent,
        leases=lease_store,
        lease_owner="runner",
        lease_ttl_s=60,
    )

    with pytest.raises(ConfigError, match="release failed"):
        await runner.run_once(_spec(tmp_path))
    with pytest.raises(ConfigError, match="release failed"):
        await runner.run_once(_spec(tmp_path))


async def test_run_once_drains_release_after_repeated_cancellation(tmp_path) -> None:
    started = asyncio.Event()
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    release_finished = asyncio.Event()

    class BlockingProvider(ScriptedProvider):
        async def stream(self, request):
            started.set()
            await allow_release.wait()
            async for event in super().stream(request):
                yield event

    class SlowReleaseStore(InMemoryLoopLeaseStore):
        async def release(self, lease) -> None:
            release_started.set()
            await allow_release.wait()
            await super().release(lease)
            release_finished.set()

    lease_store = SlowReleaseStore()
    agent = _agent(BlockingProvider([TextTurn("done")]), tmp_path)
    runner = LoopRunner(
        agent,
        leases=lease_store,
        lease_owner="runner",
        lease_ttl_s=60,
    )
    task = asyncio.create_task(runner.run_once(_spec(tmp_path)))
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    await asyncio.wait_for(release_started.wait(), timeout=1)
    task.cancel()
    allow_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert release_finished.is_set()

    result = await runner.run_once(_spec(tmp_path))
    assert result.status == "completed"


async def test_run_once_drains_session_release_after_repeated_cancellation(
    tmp_path, monkeypatch
) -> None:
    from linch.session import Session

    started = asyncio.Event()
    allow_provider = asyncio.Event()
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingProvider(ScriptedProvider):
        async def stream(self, request):
            started.set()
            await allow_provider.wait()
            async for event in super().stream(request):
                yield event

    agent = _agent(BlockingProvider([TextTurn("done")]), tmp_path)
    session = await agent.session(id="tick")

    async def return_session(*, meta=None, **kwargs):
        return session

    monkeypatch.setattr(agent, "session", return_session)
    original_aclose_impl = Session._aclose_impl

    async def delayed_aclose_impl(self, *, force):
        if self is session:
            close_started.set()
            await allow_close.wait()
        await original_aclose_impl(self, force=force)

    monkeypatch.setattr(Session, "_aclose_impl", delayed_aclose_impl)
    runner = LoopRunner(agent)
    task = asyncio.create_task(runner.run_once(_spec(tmp_path)))
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    await asyncio.wait_for(close_started.wait(), timeout=1)
    task.cancel()

    returned_early = False
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
    except TimeoutError:
        pass
    except asyncio.CancelledError:
        returned_early = True
    else:
        returned_early = True

    close_task = session._close_task
    assert close_task is not None
    allow_close.set()
    await asyncio.wait_for(asyncio.shield(close_task), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert close_task.done()
    assert session.id not in agent._sessions
    assert not returned_early, "run_once returned before session release drained"


async def test_run_failure_releases_durable_lease(tmp_path) -> None:
    lease_store = InMemoryLoopLeaseStore()
    provider = ScriptedProvider([])
    runner = LoopRunner(_agent(provider, tmp_path), leases=lease_store, lease_owner="runner")
    spec = _spec(tmp_path)

    result = await runner.run_once(spec)
    reacquired = await lease_store.try_acquire(spec.id, owner="other", ttl_s=60)

    assert result.status == "error"
    assert reacquired is not None
    domain = tmp_path / "domains" / spec.id
    log = (domain / "LOG.md").read_text(encoding="utf-8")
    assert f"Tick {result.iteration}" in log
    report_json = json.loads(
        (domain / "artifacts" / "runs" / f"{result.run_id}.json").read_text(encoding="utf-8")
    )
    assert report_json["status"] == "error"


def test_loop_types_are_explicit_extension_exports() -> None:
    import linch
    from linch.ext.loop_runner import (
        FileLoopArtifactStore,
        FileLoopLeaseStore,
        LoopArtifactStore,
        LoopLease,
        LoopLeaseStore,
        LoopRunner,
        LoopSpec,
        LoopTickResult,
        LoopTrigger,
    )

    assert all(
        value.__module__ == "linch.ext.loop_runner"
        for value in (
            LoopSpec,
            LoopTrigger,
            LoopTickResult,
            LoopArtifactStore,
            LoopLease,
            LoopLeaseStore,
            FileLoopArtifactStore,
            FileLoopLeaseStore,
            InMemoryLoopLeaseStore,
            LoopRunner,
        )
    )
    assert all(not hasattr(linch, name) for name in ("LoopSpec", "LoopRunner"))
    assert FileLoopArtifactStore(root="domains") is not None


def test_loop_runner_rejects_non_finite_ttl(tmp_path) -> None:
    provider = ScriptedProvider([TextTurn(text="x")])
    with pytest.raises(ConfigError):
        LoopRunner(_agent(provider, tmp_path), lease_ttl_s=float("inf"))
    with pytest.raises(ConfigError):
        LoopRunner(_agent(provider, tmp_path), lease_ttl_s=float("nan"))


async def test_refresh_rejects_expired_lease(tmp_path) -> None:
    store = FileLoopLeaseStore(root=tmp_path / "domains")
    lease = await store.try_acquire("docs-loop", owner="owner", ttl_s=0.01)
    assert lease is not None
    await asyncio.sleep(0.02)

    with pytest.raises(ConfigError, match="expired"):
        await store.refresh(lease, ttl_s=60)
