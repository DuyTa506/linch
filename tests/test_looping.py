from __future__ import annotations

import asyncio
import json
import os

import pytest

from linch import (
    Agent,
    ConfigError,
    FileLoopArtifactStore,
    FileLoopLeaseStore,
    InMemoryLoopLeaseStore,
    LoopRunner,
    LoopSpec,
    LoopTickResult,
    LoopTrigger,
)
from linch.evals import ScriptedProvider, TextTurn
from linch.sessions import InMemorySessionStore


def _agent(provider: ScriptedProvider, tmp_path) -> Agent:
    return Agent(
        model="gpt-5",
        provider=provider,
        session_store=InMemorySessionStore(),
        cwd=str(tmp_path),
    )


def _spec(tmp_path, loop_id: str = "docs-loop") -> LoopSpec:
    return LoopSpec(
        id=loop_id,
        charter="Maintain the docs domain.",
        prompt="Inspect the domain and report progress.",
        root=tmp_path / "domains",
        session_meta={"owner": "tests"},
    )


async def test_run_once_creates_fresh_session_report_and_artifacts(tmp_path) -> None:
    provider = ScriptedProvider([TextTurn(text="done")])
    runner = LoopRunner(_agent(provider, tmp_path))
    spec = _spec(tmp_path)

    result = await runner.run_once(spec, LoopTrigger(source="manual", payload="tick"))

    domain = tmp_path / "domains" / spec.id
    assert result.status == "completed"
    assert result.final_text == "done"
    assert result.report.session_id
    assert (domain / "README.md").read_text(encoding="utf-8").startswith("# Loop Domain")
    assert "Tick 1" in (domain / "LOG.md").read_text(encoding="utf-8")
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


async def test_predicates_can_mark_done_from_report_summary(tmp_path) -> None:
    provider = ScriptedProvider([TextTurn(text="finished")])

    def done(result: LoopTickResult, _artifacts) -> bool:
        return result.report.summary["event_counts"]["result"] == 1

    runner = LoopRunner(_agent(provider, tmp_path), done_predicate=done)
    result = await runner.run_once(_spec(tmp_path))

    assert result.done is True


async def test_failed_verifier_marks_not_done_and_logs_failure(tmp_path) -> None:
    provider = ScriptedProvider([TextTurn(text="draft")])

    def verify(_result: LoopTickResult) -> bool:
        return False

    runner = LoopRunner(_agent(provider, tmp_path), verify=verify)
    spec = _spec(tmp_path)

    result = await runner.run_once(spec)

    assert result.done is False
    assert result.status == "verification_failed"
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
    lease = await store.try_acquire("docs-loop", owner="first", ttl_s=0.01)
    assert lease is not None
    assert await store.try_acquire("docs-loop", owner="second", ttl_s=60) is None
    await asyncio.sleep(0.02)

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


async def test_run_failure_releases_durable_lease(tmp_path) -> None:
    lease_store = InMemoryLoopLeaseStore()
    provider = ScriptedProvider([])
    runner = LoopRunner(_agent(provider, tmp_path), leases=lease_store, lease_owner="runner")
    spec = _spec(tmp_path)

    result = await runner.run_once(spec)
    reacquired = await lease_store.try_acquire(spec.id, owner="other", ttl_s=60)

    assert result.status == "error"
    assert reacquired is not None


async def test_agent_run_errors_still_write_log_and_report(tmp_path) -> None:
    provider = ScriptedProvider([])
    runner = LoopRunner(_agent(provider, tmp_path))
    spec = _spec(tmp_path)

    result = await runner.run_once(spec)

    assert result.status == "error"
    domain = tmp_path / "domains" / spec.id
    log = (domain / "LOG.md").read_text(encoding="utf-8")
    assert f"Tick {result.iteration}" in log
    report_json = json.loads(
        (domain / "artifacts" / "runs" / f"{result.run_id}.json").read_text(encoding="utf-8")
    )
    assert report_json["status"] == "error"


def test_loop_types_are_exported() -> None:
    import linch

    for name in [
        "LoopSpec",
        "LoopTrigger",
        "LoopTickResult",
        "LoopArtifactStore",
        "LoopLease",
        "LoopLeaseStore",
        "FileLoopArtifactStore",
        "FileLoopLeaseStore",
        "InMemoryLoopLeaseStore",
        "LoopRunner",
    ]:
        assert hasattr(linch, name)
        assert name in linch.__all__

    assert FileLoopArtifactStore(root="domains") is not None
