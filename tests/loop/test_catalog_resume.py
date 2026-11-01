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
async def test_contract_previews_catalog_changes_for_a_new_run() -> None:
    """A new run contract matches the snapshot after its first catalog sync."""
    from linch.catalog import sync_catalog
    from linch.loop.contracts import _build_run_contract
    from linch.profiles import _base_run_config
    from linch.session import RunOptions
    from tests.support.tools import StubTool, tool_registry

    agent = make_agent(
        ScriptedProvider([]),
        tools=tool_registry(StubTool("Read")),
    )
    session = await agent.session(id="contract")
    await sync_catalog(session, _base_run_config(session), None)
    agent.tools.register(StubTool("Append"))
    before = _build_run_contract(session, "go", RunOptions())
    assert [tool["name"] for tool in before.payload["tools"]] == ["Read", "Append"]
    await sync_catalog(session, _base_run_config(session), None)
    after = _build_run_contract(session, "go", RunOptions())

    assert after.payload["tools"] == before.payload["tools"]
    assert after.fingerprint == before.fingerprint


@pytest.mark.asyncio
async def test_contract_previews_schema_edits_for_a_new_run() -> None:
    from linch.catalog import sync_catalog
    from linch.loop.contracts import _build_run_contract
    from linch.profiles import _base_run_config
    from linch.session import RunOptions
    from tests.support.tools import StubTool, tool_registry

    agent = make_agent(
        ScriptedProvider([]),
        tools=tool_registry(StubTool("Read")),
    )
    session = await agent.session(id="contract")
    config = _base_run_config(session)
    await sync_catalog(session, config, None)
    replacement = StubTool("Read")
    replacement.input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    agent.tools.replace(replacement)

    before = _build_run_contract(session, "go", RunOptions())
    assert before.payload["tools"][0]["input_schema"] == replacement.input_schema
    await sync_catalog(session, config, None)
    after = _build_run_contract(session, "go", RunOptions())

    assert after.payload["tools"] == before.payload["tools"]
    assert after.fingerprint == before.fingerprint


@pytest.mark.asyncio
async def test_bypassed_contract_does_not_fingerprint_a_stale_snapshot_listing() -> None:
    from linch.catalog import sync_catalog
    from linch.loop.contracts import _build_run_contract
    from linch.profiles import _base_run_config, _RunBinding, _RunConfig
    from linch.session import RunOptions
    from tests.support.tools import StubTool, tool_registry

    agent = make_agent(
        ScriptedProvider([]),
        tools=tool_registry(StubTool("Read")),
    )
    session = await agent.session(id="contract")
    base = _base_run_config(session)
    await sync_catalog(session, base, None)
    agent.skill_listing_text = "- live: live listing"
    overlay = _RunConfig(agent, _values=base._snapshot())

    session._run_bindings["overlay"] = _RunBinding(session, "overlay", overlay)
    options = RunOptions(run_id="overlay")
    with_snapshot = _build_run_contract(session, "go", options)
    await session.update_meta({"linch.prefix": None})
    without_snapshot = _build_run_contract(session, "go", options)

    assert with_snapshot.fingerprint == without_snapshot.fingerprint


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


@pytest.mark.asyncio
async def test_resume_after_skill_reload_compaction_and_crash_keeps_prefix_contract(
    tmp_path: Path,
) -> None:
    """A compacted, crashed run resumes against the original system/tools prefix."""
    from linch.abort import AbortContext
    from linch.catalog import reset_catalog_notifications, sync_catalog
    from linch.compaction import _run_compaction_impl
    from linch.config import FeatureFlags
    from linch.loop.request import _re_inject_skill_context
    from linch.loop.runtime import maybe_save_provider_snapshot
    from linch.profiles import _base_run_config
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.types import CatalogChangeBlock
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

    class DropCatalogChange:
        id = "drop-catalog-change"

        async def compact(self, ctx: Any, _provider: Any) -> list[Any]:
            return [
                message
                for message in ctx.messages
                if not any(isinstance(block, CatalogChangeBlock) for block in message.content)
            ]

    _skill_file(tmp_path, "alpha", "Alpha workflow skill.")
    sessions = InMemorySessionStore()
    runs = InMemoryRunStore()
    first_provider = ScriptedProvider([TextTurn("first")])
    first = make_agent(
        first_provider,
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        session_store=sessions,
        run_store=runs,
    )
    session = await first.session(id="contract")
    [event async for event in session.run("first")]
    prefix_before = repr(
        (
            session.meta["linch.prefix"]["system"],
            session.meta["linch.prefix"]["tools"],
        )
    )

    alpha = tmp_path / ".linch" / "skills" / "alpha"
    (alpha / "SKILL.md").unlink()
    alpha.rmdir()
    _skill_file(tmp_path, "beta", "Beta workflow skill.")
    await first.reload_skills()
    config = _base_run_config(session)
    await sync_catalog(session, config, None)
    compacted = await _run_compaction_impl(
        session,
        first,
        AbortContext(),
        DropCatalogChange(),
        _run_config=config,
    )
    assert compacted
    _re_inject_skill_context(session, config=config)
    await maybe_save_provider_snapshot(session)
    await reset_catalog_notifications(session, config)
    assert (
        repr(
            (
                session.meta["linch.prefix"]["system"],
                session.meta["linch.prefix"]["tools"],
            )
        )
        == prefix_before
    )
    assert await sessions.load_provider_snapshot("contract") is not None
    assert not any(
        isinstance(block, CatalogChangeBlock)
        for message in session.provider_view
        for block in message.content
    )

    fault = FaultRunStore(runs)
    crashing = make_agent(
        ScriptedProvider([TextTurn("crashed")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        session_store=sessions,
        run_store=fault,
    )
    crashed_session = await crashing.session(id="contract")
    run_id: str | None = None
    with pytest.raises(Crash):
        async for event in crashed_session.run("second"):
            run_id = getattr(event, "run_id", run_id)
    assert run_id is not None

    resumed_agent = make_agent(
        ScriptedProvider([TextTurn("resumed")]),
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        session_store=sessions,
        run_store=runs,
    )
    resumed = await resumed_agent.session(id="contract")
    events = [event async for event in resumed.resume(run_id)]

    assert events[-1].type == "result"
    assert (
        repr(
            (
                resumed.meta["linch.prefix"]["system"],
                resumed.meta["linch.prefix"]["tools"],
            )
        )
        == prefix_before
    )
    assert (
        sum(
            isinstance(block, CatalogChangeBlock)
            for message in resumed.provider_view
            for block in message.content
        )
        == 1
    )


@pytest.mark.asyncio
async def test_resume_contract_survives_in_turn_reload_compaction_and_crash(tmp_path: Path) -> None:
    """A reload during a failed request cannot rewrite the run's frozen prefix."""
    from linch.catalog import sync_catalog
    from linch.config import FeatureFlags
    from linch.errors import ContextLengthError
    from linch.profiles import _base_run_config
    from linch.run_store import InMemoryRunStore
    from linch.sessions import InMemorySessionStore
    from linch.types import CatalogChangeBlock
    from tests.support.providers import TextTurn

    class Crash(BaseException):
        pass

    class DropCatalogChange:
        id = "drop-catalog-change"

        async def compact(self, ctx: Any, _provider: Any) -> list[Any]:
            return [
                message
                for message in ctx.messages
                if not any(isinstance(block, CatalogChangeBlock) for block in message.content)
            ]

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

    class ReloadingProvider(ScriptedProvider):
        def __init__(self) -> None:
            super().__init__([TextTurn("answer")])
            self.agent: Any = None
            self.session: Any = None
            self.changed = False

        async def stream(self, req: Any):
            if not self.changed:
                self.requests.append(req)
                alpha = tmp_path / ".linch" / "skills" / "alpha"
                (alpha / "SKILL.md").unlink()
                alpha.rmdir()
                _skill_file(tmp_path, "beta", "Beta workflow skill.")
                await self.agent.reload_skills()
                await sync_catalog(self.session, _base_run_config(self.session), None)
                self.changed = True
                raise ContextLengthError("force catalog compaction")
            async for item in super().stream(req):
                yield item

    _skill_file(tmp_path, "alpha", "Alpha workflow skill.")
    sessions = InMemorySessionStore()
    runs = InMemoryRunStore()
    provider = ReloadingProvider()
    fault = FaultRunStore(runs)
    agent = make_agent(
        provider,
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        session_store=sessions,
        run_store=fault,
        compaction=DropCatalogChange(),
    )
    session = await agent.session(id="contract")
    provider.agent = agent
    provider.session = session
    run_id: str | None = None
    with pytest.raises(Crash):
        async for event in session.run("go"):
            run_id = getattr(event, "run_id", run_id)
    assert run_id is not None
    assert len(provider.requests) == 2
    assert (
        sum(
            isinstance(block, CatalogChangeBlock)
            for message in session.provider_view
            for block in message.content
        )
        == 1
    )

    resumed_provider = ScriptedProvider([TextTurn("resumed")])
    resumed_agent = make_agent(
        resumed_provider,
        cwd=str(tmp_path),
        config_dir=".linch",
        features=FeatureFlags(skills=True, subagents=False, mcp=False),
        session_store=sessions,
        run_store=runs,
        compaction=DropCatalogChange(),
    )
    resumed = await resumed_agent.session(id="contract")
    events = [event async for event in resumed.resume(run_id)]

    assert events[-1].type == "result"
    assert not resumed_provider.requests
    from linch.catalog import snapshot_system, snapshot_tools

    snapshot = resumed.meta["linch.prefix"]
    assert snapshot_system(snapshot) == provider.requests[0].system
    assert snapshot_tools(snapshot) == provider.requests[0].tools
