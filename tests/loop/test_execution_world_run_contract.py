"""Durable contracts cover the complete unified execution world."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider, TextTurn


def _world_agent(
    provider: Any,
    *,
    run_store: Any,
    session_store: Any,
    execution_backend: Any,
    filesystem: Any = None,
) -> Any:
    return make_agent(
        provider,
        session_store=session_store,
        run_store=run_store,
        execution_backend=execution_backend,
        filesystem=filesystem,
        cwd=getattr(execution_backend, "host_workspace_root", None),
    )


async def _interrupt_after_first_event(session: Any) -> str:
    iterator = session.run("go").__aiter__()
    event = await iterator.__anext__()
    assert event.type == "system"
    await iterator.aclose()
    return event.run_id


async def test_local_world_filesystem_root_change_denies_resume(tmp_path: Any) -> None:
    from linch.errors import ConfigError
    from linch.execution import LocalExecutionBackend
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()
    first = _world_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=session_store,
        run_store=run_store,
        execution_backend=LocalExecutionBackend(cwd=tmp_path / "world-a"),
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))

    changed = _world_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=session_store,
        run_store=run_store,
        execution_backend=LocalExecutionBackend(cwd=tmp_path / "world-b"),
    )
    with pytest.raises(ConfigError, match=r"execution_backend.*root"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


async def test_remote_world_confinement_change_denies_resume(tmp_path: Any) -> None:
    from linch.errors import ConfigError
    from linch.execution import RemoteExecutionBackend
    from linch.filesystem.disk import DiskFileBackend
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools.execution import LocalBackend

    run_store = InMemoryRunStore()
    session_store = InMemorySessionStore()

    def world(network: str) -> RemoteExecutionBackend:
        return RemoteExecutionBackend(
            shell=LocalBackend(),
            fs=DiskFileBackend(tmp_path / "remote"),
            confinement={"network": network},
            resume_policy_id="test.remote-world",
            resume_policy_version="1",
            resume_policy_config={"endpoint": "sandbox.example"},
        )

    first = _world_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=session_store,
        run_store=run_store,
        execution_backend=world("none"),
    )
    run_id = await _interrupt_after_first_event(await first.session(id="s1"))
    changed = _world_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=session_store,
        run_store=run_store,
        execution_backend=world("bridge"),
    )

    with pytest.raises(ConfigError, match=r"execution_backend.*confinement"):
        _ = [event async for event in (await changed.session(id="s1")).resume(run_id)]


async def test_opaque_filesystem_override_is_rejected_for_durable_world(tmp_path: Any) -> None:
    from linch.errors import ConfigError
    from linch.execution import LocalExecutionBackend
    from linch.filesystem.backend import StateFileBackend
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore

    with pytest.raises(ConfigError, match=r"filesystem=.*conflicts.*indivisible"):
        _world_agent(
            ScriptedProvider([TextTurn("done")]),
            session_store=InMemorySessionStore(),
            run_store=InMemoryRunStore(),
            execution_backend=LocalExecutionBackend(cwd=tmp_path / "world"),
            filesystem=StateFileBackend(),
        )


async def test_replaced_bash_backend_cannot_escape_configured_world(tmp_path: Any) -> None:
    from linch.errors import ConfigError
    from linch.execution import LocalExecutionBackend, RemoteExecutionBackend
    from linch.filesystem.disk import DiskFileBackend
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.tools.builtin import BashTool
    from linch.tools.execution import DockerBackend

    agent = _world_agent(
        ScriptedProvider([TextTurn("done")]),
        session_store=InMemorySessionStore(),
        run_store=InMemoryRunStore(),
        execution_backend=LocalExecutionBackend(cwd=tmp_path / "world"),
    )
    replacement_world = RemoteExecutionBackend(
        shell=DockerBackend(network="none"),
        fs=DiskFileBackend(tmp_path / "replacement"),
    )
    agent.tools.register(BashTool(execution=replacement_world))

    with pytest.raises(ConfigError, match=r"effective BashTool backend.*coherent"):
        _ = [event async for event in (await agent.session()).run("go")]
