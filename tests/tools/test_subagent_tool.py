from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from linch.subagents.registry import AgentRegistry
from linch.tools.subagent import SubagentTool


def test_subagent_tool_description_lists_verification_and_delegation_rules() -> None:
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


class _TextProvider:
    """Always returns a text 'done' response."""

    id = "text"

    def context_window(self, model: str) -> int:
        return 100_000

    async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
        from linch.types import Usage

        yield {"type": "message_start", "model": req.model}
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}


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


async def _make_agent_and_session(tmp_path: Any) -> tuple[Any, Any]:
    from linch import create_deep_agent
    from linch.sessions import InMemorySessionStore

    agent = create_deep_agent(
        model="gpt-5",
        provider=_TextProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        durable=False,
    )
    # agent.session() calls connect_subagents() internally
    session = await agent.session(id="s1")
    return agent, session


async def _make_plain_agent_and_session(tmp_path: Any) -> tuple[Any, Any]:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    agent = Agent(
        model="gpt-5",
        provider=_TextProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
    )
    session = await agent.session(id="s1")
    return agent, session


async def test_plain_agent_registers_only_subagent_tool(tmp_path: Any) -> None:
    agent, _session = await _make_plain_agent_and_session(tmp_path)

    assert agent.tools.get("Subagent") is not None
    assert agent.tools.get("SubagentContinue") is None
    assert agent.tools.get("TaskStop") is None


async def test_plain_subagent_does_not_retain_worker_handle(tmp_path: Any) -> None:
    agent, session = await _make_plain_agent_and_session(tmp_path)
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    result = await subagent_tool.execute({"description": "test", "prompt": "hello"}, ctx)

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
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    result = await subagent_tool.execute(
        {"description": "test", "prompt": "hello", "run_in_background": True}, ctx
    )

    assert result.is_error
    assert "not enabled" in result.content
    assert session.workers == {}


async def test_subagent_tool_stores_worker_handle(tmp_path: Any) -> None:
    """Subagent execution stores a WorkerHandle in session.workers."""
    agent, session = await _make_agent_and_session(tmp_path)
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    result = await subagent_tool.execute({"description": "test", "prompt": "hello"}, ctx)

    assert not result.is_error
    # Worker handle registered
    assert hasattr(session, "workers")
    assert len(session.workers) == 1


async def test_subagent_tool_result_includes_worker_id(tmp_path: Any) -> None:
    """Tool result contains the worker_id so the model can address it later."""
    agent, session = await _make_agent_and_session(tmp_path)
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    result = await subagent_tool.execute({"description": "test", "prompt": "hello"}, ctx)

    worker_id = next(iter(session.workers))
    assert worker_id in result.content


async def test_subagent_continue_tool_exists_and_is_registered(tmp_path: Any) -> None:
    """SubagentContinue is registered for deep agents after connect_subagents."""
    from linch.tools.subagent_continue import SUBAGENT_CONTINUE_TOOL_NAME

    agent, session = await _make_agent_and_session(tmp_path)

    assert agent.tools.get(SUBAGENT_CONTINUE_TOOL_NAME) is not None


async def test_subagent_continue_resumes_worker_by_id(tmp_path: Any) -> None:
    """SubagentContinue re-runs the retained child session and grows its context."""
    agent, session = await _make_agent_and_session(tmp_path)
    subagent_tool = agent.tools.get("Subagent")
    continue_tool = agent.tools.get("SubagentContinue")
    ctx = _make_ctx(session)

    # First spawn
    r1 = await subagent_tool.execute({"description": "test", "prompt": "hello"}, ctx)
    assert not r1.is_error
    worker_id = next(iter(session.workers))

    # Get child's initial message count
    handle = session.workers[worker_id]
    child_session = agent._sessions.get(handle.child_session_id)
    assert child_session is not None, "Child session should be retained"
    msgs_after_first = len(child_session.provider_view)
    assert msgs_after_first > 0

    # Continue the worker
    r2 = await continue_tool.execute({"to": worker_id, "message": "follow up"}, ctx)
    assert not r2.is_error

    # Child context grew (more messages after continue)
    assert len(child_session.provider_view) > msgs_after_first


async def test_subagent_continue_by_display_name(tmp_path: Any) -> None:
    """SubagentContinue can also address a worker by its display name."""
    agent, session = await _make_agent_and_session(tmp_path)
    subagent_tool = agent.tools.get("Subagent")
    continue_tool = agent.tools.get("SubagentContinue")
    ctx = _make_ctx(session)

    r1 = await subagent_tool.execute({"description": "my task", "prompt": "hello"}, ctx)
    assert not r1.is_error
    worker_id = next(iter(session.workers))
    display_name = session.workers[worker_id].display_name

    r2 = await continue_tool.execute({"to": display_name, "message": "follow up"}, ctx)
    assert not r2.is_error


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
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    await subagent_tool.execute({"description": "test", "prompt": "hello"}, ctx)

    assert hasattr(session, "pending_child_events")
    assert any(isinstance(e, SubagentEvent) for e in session.pending_child_events)


# ── Phase 2: background workers + <task-notification> ────────────────────────


async def test_background_subagent_returns_ack_immediately(tmp_path: Any) -> None:
    """run_in_background=True returns an ack string (not the worker's final text)."""
    agent, session = await _make_agent_and_session(tmp_path)
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    result = await subagent_tool.execute(
        {"description": "test", "prompt": "hello", "run_in_background": True}, ctx
    )

    assert not result.is_error
    # Ack, not the worker's text output
    assert "background" in result.content.lower() or "started" in result.content.lower()
    worker_id = next(iter(session.workers))
    handle = session.workers[worker_id]
    assert handle.task is not None


async def test_background_worker_notification_added_to_pending(tmp_path: Any) -> None:
    """After background task completes, pending_notifications has a <task-notification>."""
    import asyncio

    agent, session = await _make_agent_and_session(tmp_path)
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    await subagent_tool.execute(
        {"description": "test", "prompt": "hello", "run_in_background": True}, ctx
    )
    worker_id = next(iter(session.workers))
    handle = session.workers[worker_id]

    # Wait for the background task
    await asyncio.wait_for(handle.task, timeout=5.0)

    assert len(session.pending_notifications) == 1
    notification = session.pending_notifications[0]
    assert "<task-notification>" in notification.content[0].text
    assert worker_id in notification.content[0].text


async def test_background_worker_notification_xml_shape(tmp_path: Any) -> None:
    """The <task-notification> has the expected XML structure."""
    import asyncio

    agent, session = await _make_agent_and_session(tmp_path)
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    await subagent_tool.execute(
        {"description": "test", "prompt": "hello", "run_in_background": True}, ctx
    )
    worker_id = next(iter(session.workers))
    await asyncio.wait_for(session.workers[worker_id].task, timeout=5.0)

    text = session.pending_notifications[0].content[0].text
    assert "<task-id>" in text
    assert "<status>" in text
    assert "<result>" in text
    assert "</task-notification>" in text


async def test_background_worker_notification_escapes_worker_output(tmp_path: Any) -> None:
    import asyncio
    import xml.etree.ElementTree as ET

    from linch import create_deep_agent
    from linch.sessions import InMemorySessionStore

    class _XmlTextProvider:
        id = "xml-text"

        def context_window(self, model: str) -> int:
            return 100_000

        async def stream(self, req: Any) -> AsyncIterator[dict[str, object]]:
            from linch.types import Usage

            yield {"type": "message_start", "model": req.model}
            yield {"type": "text_delta", "text": "<bad>&result</bad>"}
            yield {"type": "message_end", "stop_reason": "end_turn", "usage": Usage()}

    agent = create_deep_agent(
        model="gpt-5",
        provider=_XmlTextProvider(),
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        durable=False,
    )
    session = await agent.session(id="s1")
    subagent_tool = agent.tools.get("Subagent")
    ctx = _make_ctx(session)

    await subagent_tool.execute(
        {"description": "test <worker>", "prompt": "hello", "run_in_background": True}, ctx
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

    from linch import create_deep_agent
    from linch.sessions import InMemorySessionStore

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
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=str(tmp_path),
        durable=False,
    )
    session = await agent.session(id="s1")
    subagent_tool = agent.tools.get("Subagent")
    continue_tool = agent.tools.get("SubagentContinue")
    ctx = _make_ctx(session)

    try:
        await subagent_tool.execute(
            {"description": "blocker", "prompt": "hello", "run_in_background": True}, ctx
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
