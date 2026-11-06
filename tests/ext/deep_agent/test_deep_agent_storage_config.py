"""Deep-agent durability requires explicitly supplied host storage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from linch.providers.base import BaseProvider


class OfflineProvider(BaseProvider):
    id = "offline"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        raise AssertionError("storage configuration must not invoke the provider")
        yield {}


class HostAdapter:
    """A duck-typed host adapter, with no inheritance from built-in stores."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def __bool__(self) -> bool:
        # Explicit configuration is about presence, not truthiness.
        return False


@pytest.mark.parametrize("missing", ["run_store", "all"])
async def test_durable_deep_agent_rejects_missing_host_storage(
    tmp_path: Path, missing: str
) -> None:
    from linch import ConfigError, InMemoryRunStore, InMemorySessionStore
    from linch.ext.deep_agent import create_deep_agent
    from linch.filesystem import StateFileBackend

    kwargs: dict[str, Any] = {
        "session_store": InMemorySessionStore(),
        "run_store": InMemoryRunStore(),
        "filesystem": StateFileBackend(),
    }
    if missing == "all":
        kwargs.clear()
    else:
        kwargs[missing] = None
    agent = None
    try:
        with pytest.raises(ConfigError) as exc:
            agent = create_deep_agent(
                model="offline", provider=OfflineProvider(), cwd=str(tmp_path), **kwargs
            )
        assert "Postgres" in str(exc.value)
        assert "durable=False" in str(exc.value)
        expected_missing = (
            ("session_store", "run_store", "filesystem") if missing == "all" else (missing,)
        )
        for name in expected_missing:
            assert name in str(exc.value)
        assert not (tmp_path / ".linch").exists()
    finally:
        if agent is not None:
            await agent.close()


def test_durable_storage_validation_precedes_agent_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from linch import ConfigError
    from linch.ext.deep_agent import factory

    def reject_creation(**kwargs: Any) -> None:
        raise AssertionError("Agent construction reached before storage validation")

    monkeypatch.setattr(factory, "Agent", reject_creation)
    with pytest.raises(ConfigError, match="session_store"):
        factory.create_deep_agent(model="offline", cwd=str(tmp_path))


@pytest.mark.parametrize("durable", [True, False])
async def test_deep_agent_preserves_explicit_host_adapters(tmp_path: Path, durable: bool) -> None:
    from linch import InMemoryRunStore, InMemorySessionStore
    from linch.ext.deep_agent import create_deep_agent
    from linch.filesystem import StateFileBackend

    sessions: Any = HostAdapter(InMemorySessionStore())
    runs: Any = HostAdapter(InMemoryRunStore())
    filesystem: Any = HostAdapter(StateFileBackend())
    agent = create_deep_agent(
        model="offline",
        provider=OfflineProvider(),
        cwd=str(tmp_path),
        durable=durable,
        session_store=sessions,
        run_store=runs,
        filesystem=filesystem,
    )
    try:
        assert agent.session_store is sessions
        assert agent.run_store is runs
        session = await agent.session()
        assert session.filesystem is filesystem
        await session.filesystem.write("/memories/note.md", "host memory")
        assert await filesystem.read("/memories/note.md") == "host memory"
        assert await sessions.load(session.id) is not None
        assert not (tmp_path / ".linch").exists()
    finally:
        await agent.close()


async def test_durable_deep_agent_allows_disabled_filesystem(tmp_path: Path) -> None:
    from linch import (
        FeatureFlags,
        InMemoryRunStore,
        InMemorySessionStore,
    )
    from linch.ext.deep_agent import create_deep_agent

    agent = create_deep_agent(
        model="offline",
        provider=OfflineProvider(),
        cwd=str(tmp_path),
        session_store=InMemorySessionStore(),
        run_store=InMemoryRunStore(),
        features=FeatureFlags(filesystem=False),
    )
    try:
        session = await agent.session()
        assert session.filesystem is None
        assert agent.tools.get("read_file") is None
        assert not (tmp_path / ".linch").exists()
    finally:
        await agent.close()


async def test_ephemeral_deep_agent_needs_no_host_storage(tmp_path: Path) -> None:
    from linch.ext.deep_agent import create_deep_agent
    from linch.filesystem import StateFileBackend

    agent = create_deep_agent(
        model="offline", provider=OfflineProvider(), cwd=str(tmp_path), durable=False
    )
    try:
        session = await agent.session()
        assert isinstance(session.filesystem, StateFileBackend)
        assert agent.run_store is None
        assert not (tmp_path / ".linch").exists()
    finally:
        await agent.close()
