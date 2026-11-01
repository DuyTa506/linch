"""Durable run-contract identity for frozen catalog prefixes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.support.agents import make_agent
from tests.support.providers import ScriptedProvider


def _skill_file(root: Path, name: str, description: str) -> None:
    directory = root / ".linch" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\ndescription: {description}\n---\n# {name}\n", encoding="utf-8"
    )


def test_bypass_contract_keeps_generated_skill_permission_rules() -> None:
    from types import SimpleNamespace

    from linch.loop.contracts import _permission_policy_contract
    from linch.permissions import PermissionEngine, ToolRule

    generated = ToolRule(tool="Skill", decision="allow", arg="alpha")
    agent = SimpleNamespace(
        permission_engine=PermissionEngine(mode="default", rules=[generated]),
        _skill_permission_rules=[generated],
    )

    contract = _permission_policy_contract(agent)

    assert contract["rules"] == [
        {"kind": "tool", "decision": "allow", "tool": "Skill", "arg": "alpha"}
    ]


@pytest.mark.asyncio
async def test_contract_uses_the_same_frozen_prefix_before_and_after_snapshot() -> None:
    from linch.catalog import sync_catalog
    from linch.loop.contracts import _build_run_contract
    from linch.profiles import _base_run_config
    from linch.session import RunOptions
    from tests.support.tools import StubTool, tool_registry

    agent = make_agent(ScriptedProvider([]), tools=tool_registry(StubTool("Read")))
    session = await agent.session(id="contract")
    before = _build_run_contract(session, "go", RunOptions())
    await sync_catalog(session, _base_run_config(session), None)
    after = _build_run_contract(session, "go", RunOptions())

    assert before.payload["system"] == after.payload["system"]
    assert before.payload["tools"] == after.payload["tools"]
    assert before.fingerprint == after.fingerprint


@pytest.mark.asyncio
async def test_skill_reload_keeps_durable_contract_identity(tmp_path: Path) -> None:
    from linch.catalog import sync_catalog
    from linch.config import FeatureFlags
    from linch.loop.contracts import _build_run_contract
    from linch.profiles import _base_run_config
    from linch.session import RunOptions
    from tests.support.tools import StubTool, tool_registry

    _skill_file(tmp_path, "alpha", "Alpha workflow skill.")
    agent = make_agent(
        ScriptedProvider([]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        tools=tool_registry(StubTool("Read")),
    )
    session = await agent.session(id="contract")
    before = _build_run_contract(session, "go", RunOptions())
    await sync_catalog(session, _base_run_config(session), None)

    alpha = tmp_path / ".linch" / "skills" / "alpha"
    (alpha / "SKILL.md").unlink()
    alpha.rmdir()
    _skill_file(tmp_path, "beta", "Beta workflow skill.")
    await agent.reload_skills()
    after = _build_run_contract(session, "go", RunOptions())

    assert before.fingerprint == after.fingerprint


@pytest.mark.asyncio
async def test_restart_host_platform_does_not_change_frozen_contract(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import linch.agent as agent_module
    from linch.catalog import sync_catalog
    from linch.loop.contracts import _build_run_contract
    from linch.profiles import _base_run_config
    from linch.run_store import InMemoryRunStore
    from linch.session import RunOptions
    from linch.sessions import InMemorySessionStore
    from tests.support.tools import StubTool, tool_registry

    sessions = InMemorySessionStore()
    runs = InMemoryRunStore()
    first = make_agent(
        ScriptedProvider([]),
        session_store=sessions,
        run_store=runs,
        tools=tool_registry(StubTool("Read")),
    )
    first_session = await first.session(id="contract")
    await sync_catalog(first_session, _base_run_config(first_session), None)
    before = _build_run_contract(first_session, "go", RunOptions())

    monkeypatch.setattr(agent_module.platform, "python_version", lambda: "99.99.99")
    second = make_agent(
        ScriptedProvider([]),
        session_store=sessions,
        run_store=runs,
        tools=tool_registry(StubTool("Read")),
    )
    second_session = await second.session(id="contract")
    after = _build_run_contract(second_session, "go", RunOptions())

    assert before.fingerprint == after.fingerprint


@pytest.mark.asyncio
async def test_durable_resume_after_skill_reload_passes_contract_validation(tmp_path: Path) -> None:
    from linch.config import FeatureFlags
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from tests.support.providers import TextTurn

    class Crash(BaseException):
        pass

    class FaultRunStore:
        def __init__(self, store: Any) -> None:
            self.store = store
            self.crashed = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.store, name)

        async def save_checkpoint(self, run_id: str, checkpoint: Any, **kwargs: Any) -> Any:
            if checkpoint.phase == "assistant_appended" and not self.crashed:
                self.crashed = True
                raise Crash("checkpoint")
            return await self.store.save_checkpoint(run_id, checkpoint, **kwargs)

        async def save_checkpoint_with_events(
            self, run_id: str, checkpoint: Any, events: list[Any], **kwargs: Any
        ) -> Any:
            if checkpoint.phase == "assistant_appended" and not self.crashed:
                self.crashed = True
                raise Crash("checkpoint")
            return await self.store.save_checkpoint_with_events(
                run_id, checkpoint, events, **kwargs
            )

    _skill_file(tmp_path, "alpha", "Alpha workflow skill.")
    sessions = InMemorySessionStore()
    runs = InMemoryRunStore()
    fault = FaultRunStore(runs)
    first = make_agent(
        ScriptedProvider([TextTurn("done")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        session_store=sessions,
        run_store=fault,
    )
    first_session = await first.session(id="contract")
    run_id: str | None = None
    with pytest.raises(Crash):
        async for event in first_session.run("go"):
            run_id = getattr(event, "run_id", run_id)
    assert run_id is not None

    alpha = tmp_path / ".linch" / "skills" / "alpha"
    (alpha / "SKILL.md").unlink()
    alpha.rmdir()
    _skill_file(tmp_path, "beta", "Beta workflow skill.")
    resumed_agent = make_agent(
        ScriptedProvider([TextTurn("unused")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        session_store=sessions,
        run_store=runs,
    )
    resumed = await resumed_agent.session(id="contract")

    events = [event async for event in resumed.resume(run_id)]

    assert events[-1].type == "result"
