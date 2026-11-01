from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn
from tests.support.stores import memory_session_store


def test_subagent_tool_description_lists_verification_and_delegation_rules() -> None:
    from linch.subagents.registry import AgentRegistry
    from linch.tools.subagent import SubagentTool

    tool = SubagentTool(
        registry=AgentRegistry([]),
        get_session=lambda _sid: None,
        next_default_display_name=lambda _sid: "Agent #1",
    )

    description = tool.description

    assert "- verification:" in description
    assert "complete context" in description
    assert "meaningful research, implementation, or verification" in description
    assert "based on your findings" in description
    assert "parallel" in description
    assert "_default" not in description


# ── Phase 1: fork/continue ────────────────────────────────────────────────────


def _make_ctx(session: Any) -> Any:
    from linch.tools.base import ToolContext

    return ToolContext(
        cwd=session.agent.cwd,
        session_id=session.id,
        run_id="test-run",
        session_store=session.store,
        signal=None,
        file_read_tracker=getattr(session, "file_read_tracker", None),
        deps=None,
        filesystem=getattr(session, "filesystem", None),
    )


async def _make_agent_and_session(tmp_path: Any, provider: Any = None) -> tuple[Any, Any]:
    from linch.ext.deep_agent import create_deep_agent

    agent = create_deep_agent(
        model="gpt-5",
        provider=provider or ScriptedProvider([TextTurn("done") for _ in range(4)]),
        session_store=memory_session_store(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        durable=False,
    )
    # agent.session() calls connect_subagents() internally
    session = await agent.session(id="s1")
    return agent, session


async def _make_plain_agent_and_session(tmp_path: Any) -> tuple[Any, Any]:
    from linch import empty_tools
    from linch.config import FeatureFlags
    from tests.support.agents import make_agent

    agent = make_agent(
        ScriptedProvider([TextTurn("done")]),
        tools=empty_tools(),
        model="gpt-5",
        cwd=str(tmp_path),
        features=FeatureFlags(subagents=True),
        run_store=None,
    )
    session = await agent.session(id="s1")
    return agent, session


async def _spawn(agent: Any, session: Any, **input: Any) -> Any:
    """Execute the parent subagent tool with the standard test context."""

    return await agent.tools.get("Subagent").execute(input, _make_ctx(session))


async def test_plain_agent_registers_only_subagent_tool(tmp_path: Any) -> None:
    agent, _session = await _make_plain_agent_and_session(tmp_path)

    assert agent.tools.get("Subagent") is not None
    assert agent.tools.get("SubagentContinue") is None
    assert agent.tools.get("TaskStop") is None


async def test_plain_subagent_does_not_retain_worker_handle(tmp_path: Any) -> None:
    agent, session = await _make_plain_agent_and_session(tmp_path)
    result = await _spawn(agent, session, description="test", prompt="hello")

    assert not result.is_error
    assert session.workers == {}
    assert "[Worker ID:" not in result.content
    child_sessions = [
        sess
        for sid, sess in agent._sessions.items()
        if sid != session.id and sess.meta.get("parentSessionId") == session.id
    ]
    assert child_sessions == []


async def test_plain_background_subagent_returns_clear_error(tmp_path: Any) -> None:
    agent, session = await _make_plain_agent_and_session(tmp_path)
    result = await _spawn(
        agent, session, description="test", prompt="hello", run_in_background=True
    )

    assert result.is_error
    assert "not enabled" in result.content
    assert session.workers == {}


@pytest.mark.parametrize("background", [False, True])
async def test_subagent_inherits_invoking_tenant_filesystem(
    tmp_path: Any, background: bool
) -> None:
    from linch.config import FeatureFlags
    from linch.filesystem import StateFileBackend, filesystem_tools
    from tests.support.agents import make_agent
    from tests.support.tools import tool_registry

    tenant, workspace = StateFileBackend(), StateFileBackend()
    agent = make_agent(
        ScriptedProvider(
            [ToolUseTurn("write_file", {"path": "/note", "content": "tenant"}), TextTurn("done")]
        ),
        tools=tool_registry(*filesystem_tools()),
        filesystem=workspace,
        cwd=str(tmp_path),
        features=FeatureFlags(subagents=True),
        retain_subagents=True,
        enable_background_subagents=True,
        run_store=None,
    )
    session = await agent.session()
    ctx = _make_ctx(session)
    ctx.deps = {"filesystem": tenant}
    try:
        result = await agent.tools.get("Subagent").execute(
            {"description": "tenant writer", "prompt": "write", "run_in_background": background},
            ctx,
        )
        assert not result.is_error
        if background:
            await next(iter(session.workers.values())).task
        assert await tenant.read("/note") == "tenant"
        assert await workspace.ls() == []
    finally:
        await agent.close()


async def test_subagent_continue_inherits_current_run_deps(tmp_path: Any) -> None:
    from linch import RunOptions
    from linch.config import FeatureFlags
    from linch.filesystem import StateFileBackend, filesystem_tools
    from tests.support.agents import make_agent
    from tests.support.tools import tool_registry

    tenant, workspace = StateFileBackend(), StateFileBackend()
    provider = ScriptedProvider(
        [
            ToolUseTurn("Subagent", {"description": "writer", "prompt": "wait"}),
            TextTurn("ready"),
            TextTurn("spawned"),
            ToolUseTurn("SubagentContinue", {"to": "Agent #1", "message": "write"}),
            ToolUseTurn("write_file", {"path": "/note", "content": "current tenant"}),
            TextTurn("written"),
            TextTurn("done"),
        ]
    )
    agent = make_agent(
        provider,
        tools=tool_registry(*filesystem_tools()),
        filesystem=workspace,
        cwd=str(tmp_path),
        features=FeatureFlags(subagents=True),
        retain_subagents=True,
        enable_worker_tools=True,
        run_store=None,
    )
    session = await agent.session()
    try:
        [_ async for _ in session.run("spawn")]
        [_ async for _ in session.run("continue", RunOptions(deps={"filesystem": tenant}))]
        assert await tenant.read("/note") == "current tenant"
        assert await workspace.ls() == []
    finally:
        await agent.close()


async def test_subagent_preserves_captured_none_deps_after_parent_changes(tmp_path: Any) -> None:
    from linch.config import FeatureFlags
    from tests.support.agents import make_agent
    from tests.support.tools import StubTool, tool_registry

    seen = []
    agent = make_agent(
        ScriptedProvider([ToolUseTurn("inspect", {}), TextTurn("done")]),
        tools=tool_registry(StubTool("inspect", on_execute=lambda raw, ctx: seen.append(ctx.deps))),
        features=FeatureFlags(subagents=True),
        run_store=None,
    )
    session = await agent.session()
    session.run_deps = {"tenant": "later run"}
    try:
        await _spawn(agent, session, description="worker", prompt="go")
        assert seen == [None]
    finally:
        await agent.close()


async def test_subagent_tool_stores_worker_handle(tmp_path: Any) -> None:
    """Subagent execution stores a WorkerHandle in session.workers."""
    agent, session = await _make_agent_and_session(tmp_path)
    result = await _spawn(agent, session, description="test", prompt="hello")

    assert not result.is_error
    # Worker handle registered
    assert hasattr(session, "workers")
    assert len(session.workers) == 1
    assert next(iter(session.workers)) in result.content


@pytest.mark.parametrize("background", [False, True])
async def test_subagent_result_exposes_unique_mailbox_address(
    tmp_path: Any, background: bool
) -> None:
    from linch import InMemoryMailbox, MailboxPlugin

    agent, session = await _make_agent_and_session(tmp_path)
    await agent.use(MailboxPlugin(), InMemoryMailbox())
    try:
        result = await _spawn(
            agent, session, description="worker", prompt="go", run_in_background=background
        )
        handle = next(iter(session.workers.values()))
        if background:
            await handle.task
        child = agent._sessions[handle.child_session_id]
        assert child.mailbox_address == handle.worker_id
        assert child.mailbox_address in result.content
        assert child.mailbox_address != handle.display_name
    finally:
        await agent.close()


async def test_subagent_tool_returns_structured_output_as_json_text(tmp_path: Any) -> None:
    from linch.ext.deep_agent import create_deep_agent

    agent = create_deep_agent(
        model="gpt-5",
        provider=ScriptedProvider([ToolUseTurn("emit_answer", {"answer": 42})]),
        final_tool_name="emit_answer",
        session_store=memory_session_store(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        durable=False,
    )
    session = await agent.session(id="s1")
    result = await _spawn(agent, session, description="test", prompt="hello")

    assert not result.is_error
    assert result.content.startswith('{"answer":42}')
    worker_id = next(iter(session.workers))
    assert session.workers[worker_id].last_result_text == '{"answer":42}'


async def test_subagent_continue_tool_exists_and_is_registered(tmp_path: Any) -> None:
    """SubagentContinue is registered for deep agents after connect_subagents."""
    from linch.tools.subagent_continue import SUBAGENT_CONTINUE_TOOL_NAME

    agent, session = await _make_agent_and_session(tmp_path)

    assert agent.tools.get(SUBAGENT_CONTINUE_TOOL_NAME) is not None


@pytest.mark.parametrize("target_kind", ["id", "display-name"])
async def test_subagent_continue_by_target(tmp_path: Any, target_kind: str) -> None:
    """SubagentContinue accepts both worker IDs and display names."""
    agent, session = await _make_agent_and_session(tmp_path)
    continue_tool = agent.tools.get("SubagentContinue")
    ctx = _make_ctx(session)

    r1 = await _spawn(agent, session, description="test", prompt="hello")
    assert not r1.is_error
    worker_id = next(iter(session.workers))

    handle = session.workers[worker_id]
    child_session = agent._sessions.get(handle.child_session_id)
    assert child_session is not None, "Child session should be retained"
    msgs_after_first = len(child_session.provider_view)
    assert msgs_after_first > 0

    target = worker_id if target_kind == "id" else session.workers[worker_id].display_name
    r2 = await continue_tool.execute({"to": target, "message": "follow up"}, ctx)
    assert not r2.is_error
    if target_kind == "id":
        assert len(child_session.provider_view) > msgs_after_first


async def test_subagent_continue_unknown_worker_returns_error(tmp_path: Any) -> None:
    """Continue with an unknown id returns is_error with the list of known workers."""
    agent, session = await _make_agent_and_session(tmp_path)
    continue_tool = agent.tools.get("SubagentContinue")
    ctx = _make_ctx(session)

    result = await continue_tool.execute({"to": "nonexistent-id", "message": "test"}, ctx)

    assert result.is_error
    # Should mention no live workers or list known ones
    assert "no live" in result.content.lower() or "nonexistent" in result.content.lower()


async def test_pending_child_events_populated_by_subagent_run(tmp_path: Any) -> None:
    """ctx.emit is wired: child SubagentEvents land in session.pending_child_events."""
    from linch.events import SubagentEvent

    agent, session = await _make_agent_and_session(tmp_path)
    await _spawn(agent, session, description="test", prompt="hello")

    assert hasattr(session, "pending_child_events")
    assert any(isinstance(e, SubagentEvent) for e in session.pending_child_events)


# ── Phase 2: background workers + <task-notification> ────────────────────────


async def test_background_subagent_returns_ack_immediately(tmp_path: Any) -> None:
    """run_in_background=True returns an ack string (not the worker's final text)."""
    import asyncio

    agent, session = await _make_agent_and_session(tmp_path)
    result = await _spawn(
        agent, session, description="test", prompt="hello", run_in_background=True
    )

    assert not result.is_error
    # Ack, not the worker's text output
    assert "background" in result.content.lower() or "started" in result.content.lower()
    worker_id = next(iter(session.workers))
    handle = session.workers[worker_id]
    assert handle.task is not None
    await asyncio.wait_for(handle.task, timeout=5.0)

    assert len(session.pending_notifications) == 1
    notification = session.pending_notifications[0]
    assert "<task-notification>" in notification.content[0].text
    assert worker_id in notification.content[0].text
    text = notification.content[0].text
    assert "<task-id>" in text
    assert "<status>" in text
    assert "<result>" in text
    assert "</task-notification>" in text


async def test_background_subagent_pre_drive_failure_notifies(tmp_path: Any, monkeypatch) -> None:
    import contextlib

    agent, session = await _make_agent_and_session(tmp_path)

    async def fail_create(**kwargs):
        raise ConnectionError("child store unavailable")

    monkeypatch.setattr(session.store, "create", fail_create)
    try:
        result = await _spawn(
            agent, session, description="failing child", prompt="go", run_in_background=True
        )
        assert not result.is_error
        handle = next(iter(session.workers.values()))
        with contextlib.suppress(ConnectionError):
            await handle.task
        assert handle.status == "failed"
        assert len(session.pending_notifications) == 1
        text = session.pending_notifications[0].content[0].text
        assert "<status>failed</status>" in text
        assert "ConnectionError: child store unavailable" in text
    finally:
        await agent.close()


async def test_background_subagent_cancelled_before_start_marks_killed(tmp_path: Any) -> None:
    import asyncio

    agent, session = await _make_agent_and_session(tmp_path)
    try:
        await _spawn(agent, session, description="worker", prompt="go", run_in_background=True)
        handle = next(iter(session.workers.values()))
        handle.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handle.task
        assert handle.status == "killed"
        assert set(agent._sessions) == {session.id}
    finally:
        await agent.close()


async def test_background_subagent_notification_uses_post_tool_redaction(tmp_path: Any) -> None:
    from linch import RedactionConfig, RedactionHook, RedactionRule

    agent, session = await _make_agent_and_session(
        tmp_path, ScriptedProvider([TextTurn("SECRET_VALUE")])
    )
    agent.hooks.append(
        RedactionHook(
            RedactionConfig(
                rules=(RedactionRule("SECRET_VALUE", "[REDACTED]"),),
                redact_final_answer=False,
            )
        )
    )
    try:
        await _spawn(agent, session, description="worker", prompt="go", run_in_background=True)
        await next(iter(session.workers.values())).task
        text = session.pending_notifications[0].content[0].text
        assert "SECRET_VALUE" not in text
        assert "[REDACTED]" in text
    finally:
        await agent.close()


async def test_background_subagent_completion_keeps_origin_profile_and_deps(tmp_path: Any) -> None:
    import asyncio
    from dataclasses import replace

    from linch import Disposable, HookResult, Profile, RunOptions
    from linch.config import FeatureFlags
    from tests.support.agents import make_agent

    started, release = asyncio.Event(), asyncio.Event()
    completion_calls = []

    class RedactOrigin:
        def on_post_tool_use(self, ctx):
            if "SECRET_VALUE" in ctx.result.content:
                completion_calls.append((ctx.run_id, ctx.deps))
                return HookResult.mutate(tool_result=replace(ctx.result, content="origin-redacted"))

    class OriginPolicy:
        name, version = "origin-policy", "1"
        requires = provides = ()

        def setup(self, ctx, config=None):
            hooks, hook = ctx.run_config.hooks, RedactOrigin()
            hooks.append(hook)
            return Disposable(lambda: hooks.remove(hook))

    class PausedChild(ScriptedProvider):
        async def stream(self, req):
            if any(getattr(block, "text", "") == "child work" for block in req.messages[0].content):
                started.set()
                await release.wait()
                async for event in child_provider.stream(req):
                    yield event
            else:
                async for event in super().stream(req):
                    yield event

    child_provider = ScriptedProvider([TextTurn("SECRET_VALUE")])
    provider = PausedChild(
        [
            ToolUseTurn(
                "Subagent",
                {"description": "worker", "prompt": "child work", "run_in_background": True},
            ),
            TextTurn("parent A"),
            TextTurn("parent B"),
        ]
    )
    agent = make_agent(
        provider,
        cwd=str(tmp_path),
        run_store=None,
        features=FeatureFlags(subagents=True),
        enable_background_subagents=True,
    )
    session = await agent.session()
    deps = {"tenant": "A"}
    profile = Profile("origin", "1", plugins={"policy": (OriginPolicy(), {})})
    try:
        events = [_ async for _ in session.run("A", RunOptions(profile=profile, deps=deps))]
        origin_run_id = events[0].run_id
        await asyncio.wait_for(started.wait(), 5)
        [_ async for _ in session.run("B", RunOptions(deps={"tenant": "B"}))]
        release.set()
        await next(iter(session.workers.values())).task
        assert session.pending_notifications[0].content[0].text == "origin-redacted"
        assert completion_calls == [(origin_run_id, deps)]
    finally:
        release.set()
        await agent.close()


@pytest.mark.parametrize("action", ["block", "stop"])
async def test_background_subagent_notification_honors_post_tool_veto(
    tmp_path: Any, action
) -> None:
    from linch import HookResult

    class VetoCompletion:
        def on_post_tool_use(self, ctx):
            return HookResult(action=action, reason="completion withheld")

    agent, session = await _make_agent_and_session(
        tmp_path, ScriptedProvider([TextTurn("PRIVATE_RESULT")])
    )
    agent.hooks.append(VetoCompletion())
    try:
        await _spawn(agent, session, description="worker", prompt="go", run_in_background=True)
        await next(iter(session.workers.values())).task
        text = session.pending_notifications[0].content[0].text
        assert "PRIVATE_RESULT" not in text
        assert "completion withheld" in text
    finally:
        await agent.close()


async def test_background_subagent_completion_is_audited_on_origin_run(tmp_path: Any) -> None:
    """Completion telemetry is stored on the launcher run, not a later turn."""
    import asyncio

    from linch.events import BackgroundWorkerEvent
    from linch.run_store import InMemoryRunStore

    agent, session = await _make_agent_and_session(tmp_path)
    run_store = InMemoryRunStore()
    agent.run_store = run_store
    await run_store.create_run(session.id, id="test-run")
    await _spawn(agent, session, description="test", prompt="hello", run_in_background=True)
    worker_id = next(iter(session.workers))
    await asyncio.wait_for(session.workers[worker_id].task, timeout=5.0)

    stored = await run_store.load_events("test-run")
    audit = [item.event for item in stored if isinstance(item.event, BackgroundWorkerEvent)]
    assert len(audit) == 1
    assert audit[0].worker_id == worker_id
    assert audit[0].status == "completed"

    # Draining the live event projection on a later parent turn must not
    # duplicate or misattribute the origin audit record.
    later_events = [event async for event in session.run("next")]
    later_run_id = next(event.run_id for event in later_events if event.type == "system")
    later_stored = await run_store.load_events(later_run_id)
    assert not any(isinstance(item.event, BackgroundWorkerEvent) for item in later_stored)


async def test_background_worker_notification_escapes_worker_output(tmp_path: Any) -> None:
    import asyncio
    import xml.etree.ElementTree as ET

    from linch.ext.deep_agent import create_deep_agent

    agent = create_deep_agent(
        model="gpt-5",
        provider=ScriptedProvider([TextTurn("<bad>&result</bad>")]),
        session_store=memory_session_store(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        durable=False,
    )
    session = await agent.session(id="s1")
    await _spawn(
        agent,
        session,
        description="test <worker>",
        prompt="hello",
        run_in_background=True,
    )
    worker_id = next(iter(session.workers))
    await asyncio.wait_for(session.workers[worker_id].task, timeout=5.0)

    text = session.pending_notifications[0].content[0].text
    root = ET.fromstring(text)

    assert root.findtext("result") == "<bad>&result</bad>"


# ── Regression: stopping a mid-run background worker must not leak/orphan it ──


async def test_stopped_background_worker_remains_addressable(tmp_path: Any) -> None:
    """A background worker stopped mid-run keeps its real child_session_id.

    Reproduces the resource-leak bug: child_session_id was only set on the
    post-run return path, so cancelling the task mid-run left the handle with
    an empty id — TaskStop could not abort the child, SubagentContinue returned
    'WorkerNotLive', and the child session leaked in agent._sessions.
    """
    import asyncio

    from linch.ext.deep_agent import create_deep_agent

    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingProvider:
        id = "blocking"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            from linch.types import Usage

            yield {"type": "message_start", "model": req.model}
            # Signal that the child run is in progress, then block until released.
            started.set()
            await release.wait()
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    agent = create_deep_agent(
        model="gpt-5",
        provider=_BlockingProvider(),
        session_store=memory_session_store(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        durable=False,
    )
    session = await agent.session(id="s1")
    continue_tool = agent.tools.get("SubagentContinue")
    ctx = _make_ctx(session)

    try:
        await _spawn(
            agent,
            session,
            description="blocker",
            prompt="hello",
            run_in_background=True,
        )
        worker_id = next(iter(session.workers))
        handle = session.workers[worker_id]

        # Wait until the child run is actually in progress (mid-run).
        await asyncio.wait_for(started.wait(), timeout=5.0)

        # The child session id must be known as soon as the child is registered,
        # before the background run returns.
        assert handle.child_session_id != "", "child_session_id not recorded mid-run"
        child_id = handle.child_session_id
        assert agent._sessions.get(child_id) is not None, (
            "child session must be reachable for abort/continue"
        )

        # Stop the worker mid-run (cancels the task + aborts the child session).
        handle.task.cancel()
        try:
            await handle.task
        except asyncio.CancelledError:
            pass

        # The handle still carries the real child id and is addressable.
        assert handle.child_session_id == child_id

        # SubagentContinue must not report the worker as not-live.
        release.set()  # let any resumed child run complete cleanly
        r = await continue_tool.execute({"to": worker_id, "message": "follow up"}, ctx)
        assert "WorkerNotLive" not in (r.content or "")
    finally:
        release.set()


async def test_running_coordinator_preserves_full_worker_catalog(tmp_path):
    from linch.ext.deep_agent import create_deep_agent

    provider = ScriptedProvider(
        [
            ToolUseTurn(
                "Subagent",
                {"description": "implement", "prompt": "work", "subagent_type": "implementer"},
            ),
            TextTurn("child"),
            TextTurn("parent"),
        ]
    )
    agent = create_deep_agent(
        model="test",
        provider=provider,
        coordinator=True,
        cwd=str(tmp_path),
        session_store=memory_session_store(),
        permissions={"mode": "skip-dangerous"},
        durable=False,
    )
    session = await agent.session()
    events = [event async for event in session.run("delegate")]
    assert not any(event.type == "tool_call_end" and event.is_error for event in events)
    assert {"Read", "Write", "Edit", "Bash", "Glob", "Grep"} <= {
        tool["name"] for tool in provider.requests[1].tools
    }
    await agent.close()
