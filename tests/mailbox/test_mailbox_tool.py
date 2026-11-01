"""Loop wiring for the mailbox: the send_message tool + drain-into-next-turn.

A session with a ``mailbox_address`` drains its inbox at the top of each turn
(exactly like ``pending_notifications``); the ``send_message`` tool lets the
agent address a peer. Opt in via ``await agent.use(MailboxPlugin(), mailbox)`` — with no mailbox the
tool is absent and no drain runs (byte-identical).

linch imports happen inside the test bodies/helpers because sibling tests pop
``linch*`` modules from ``sys.modules``.
"""

from __future__ import annotations

from typing import Any

import pytest


async def _agent(provider: Any, *, mailbox: Any = None, **kwargs: Any) -> Any:
    from linch import MailboxPlugin
    from tests.support.agents import make_agent
    from tests.support.stores import memory_session_store

    agent = make_agent(
        provider,
        model="m",
        session_store=memory_session_store(),
        run_store=None,
        **kwargs,
    )
    if mailbox is not None:
        await agent.use(MailboxPlugin(), mailbox)
    return agent


def _texts(message: Any) -> str:
    return "".join(b.text for b in message.content if hasattr(b, "text"))


# --- send_message tool -------------------------------------------------------


async def test_send_message_tool_addresses_a_peer() -> None:
    from linch.coordination.mailbox import InMemoryMailbox
    from linch.coordination.send_message import SendMessageTool
    from linch.tools import ToolContext

    box = InMemoryMailbox()

    class _Sess:
        mailbox_address = "alice"

    tool = SendMessageTool(mailbox=box, get_session=lambda _sid: _Sess())
    ctx = ToolContext(cwd=".", session_id="s1", run_id="r1", session_store=None)

    result = await tool.execute({"to": "bob", "content": "task for you", "type": "assignment"}, ctx)

    assert not result.is_error
    drained = await box.drain("bob")
    assert len(drained) == 1
    assert drained[0].sender == "alice"
    assert drained[0].recipient == "bob"
    assert drained[0].content == "task for you"
    assert drained[0].type == "assignment"


async def test_send_message_tool_falls_back_to_session_id_address() -> None:
    from linch.coordination.mailbox import InMemoryMailbox
    from linch.coordination.send_message import SendMessageTool
    from linch.tools import ToolContext

    box = InMemoryMailbox()
    # Session has no mailbox_address → sender defaults to the session id.
    tool = SendMessageTool(mailbox=box, get_session=lambda _sid: object())
    ctx = ToolContext(cwd=".", session_id="sess-xyz", run_id="r1", session_store=None)

    await tool.execute({"to": "bob", "content": "hi"}, ctx)

    drained = await box.drain("bob")
    assert drained[0].sender == "sess-xyz"


# --- registration & default byte-identical ----------------------------------


@pytest.mark.parametrize("configured", [True, False])
async def test_mailbox_send_tool_registration(configured: bool) -> None:
    from linch.coordination.mailbox import InMemoryMailbox
    from tests.support.providers import ScriptedProvider, TextTurn

    mailbox = InMemoryMailbox() if configured else None
    agent = await _agent(ScriptedProvider([TextTurn(text="ok")]), mailbox=mailbox)
    assert (agent.tools.get("send_message") is not None) is configured


async def test_same_mailbox_plugin_object_keeps_agents_and_boxes_isolated() -> None:
    from linch import MailboxPlugin
    from linch.coordination.mailbox import InMemoryMailbox
    from tests.support.providers import ScriptedProvider

    first = await _agent(ScriptedProvider([]))
    second = await _agent(ScriptedProvider([]))
    plugin = MailboxPlugin()
    first_box = InMemoryMailbox()
    second_box = InMemoryMailbox()

    await first.use(plugin, first_box)
    await second.use(plugin, second_box)

    assert first.mailbox is first_box
    assert second.mailbox is second_box
    assert first.tools.get("send_message")._mailbox is first_box
    assert second.tools.get("send_message")._mailbox is second_box


async def test_mailbox_unmount_removes_tool_service_and_inbox() -> None:
    from linch import MailboxPlugin
    from linch.coordination.mailbox import InMemoryMailbox
    from tests.support.providers import ScriptedProvider, TextTurn

    agent = await _agent(ScriptedProvider([TextTurn(text="ok")]))
    mounted = await agent.use(MailboxPlugin(), InMemoryMailbox())
    assert agent.mailbox is not None
    assert agent.tools.get("send_message") is not None
    assert agent.context._inbox

    await mounted.dispose()
    assert agent.mailbox is None
    assert agent.tools.get("send_message") is None
    assert not agent.context._inbox


async def test_mailbox_agent_close_does_not_deadlock_during_withdrawal() -> None:
    import asyncio

    from linch import MailboxPlugin
    from linch.coordination.mailbox import InMemoryMailbox
    from tests.support.providers import ScriptedProvider

    agent = await _agent(ScriptedProvider([]))
    await agent.use(MailboxPlugin(), InMemoryMailbox())
    await asyncio.wait_for(agent.close(), timeout=2)


# --- drain into next turn ----------------------------------------------------


async def test_pending_mailbox_message_drains_into_next_turn() -> None:
    from linch.coordination.mailbox import InMemoryMailbox, MailboxMessage
    from tests.support.providers import ScriptedProvider, TextTurn

    box = InMemoryMailbox()
    agent = await _agent(ScriptedProvider([TextTurn(text="done")]), mailbox=box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(
        MailboxMessage(sender="bob", recipient="alice", content="hello-alice", type="greeting")
    )

    events = [event async for event in session.run("go")]

    user_texts = " ".join(_texts(e.message) for e in events if e.type == "user")
    assert "hello-alice" in user_texts
    assert "bob" in user_texts
    # The peer message also lands in provider_view so the model sees it.
    assert any(m.role == "user" and "hello-alice" in _texts(m) for m in session.provider_view)


async def test_durable_mailbox_delivery_is_committed_once() -> None:
    from linch import MailboxPlugin
    from linch.coordination.mailbox import InMemoryMailbox, MailboxMessage
    from linch.durability import DurabilityOptions
    from tests.support.providers import ScriptedProvider, TextTurn

    box = InMemoryMailbox()
    agent = await _agent(
        ScriptedProvider([TextTurn(text="one"), TextTurn(text="two")]),
        durability=DurabilityOptions(durable_inbox=True),
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="durable", id="stable"))

    first = [event async for event in session.run("go")]
    second = [event async for event in session.run("again")]

    assert sum(event.type == "user" and event.subtype == "notification" for event in first) == 1
    assert not any(event.type == "user" and event.subtype == "notification" for event in second)
    assert sum("durable" in _texts(message) for message in session.provider_view) == 1


async def test_no_drain_without_address() -> None:
    from linch.coordination.mailbox import InMemoryMailbox, MailboxMessage
    from tests.support.providers import ScriptedProvider, TextTurn

    box = InMemoryMailbox()
    agent = await _agent(ScriptedProvider([TextTurn(text="done")]), mailbox=box)
    session = await agent.session()
    # No mailbox_address set → nothing is drained even though a message exists
    # for some other address.
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="hello"))

    events = [event async for event in session.run("go")]

    assert not any(e.type == "user" and "hello" in _texts(e.message) for e in events)
    # Message remains undelivered in the box.
    assert len(await box.drain("alice")) == 1


async def test_durable_mailbox_releases_unacknowledged_claims_on_error() -> None:
    from linch import MailboxPlugin
    from linch.coordination.mailbox import InMemoryMailbox, MailboxMessage
    from linch.durability import DurabilityOptions
    from tests.support.providers import ScriptedProvider, TextTurn

    class FailingAckMailbox(InMemoryMailbox):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True
            self.released = 0

        async def ack(self, claims: Any) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("ack failed")
            await super().ack(claims)

        async def release(self, claims: Any) -> None:
            self.released += len(claims)
            await super().release(claims)

    box = FailingAckMailbox()
    agent = await _agent(
        ScriptedProvider([TextTurn(text="recovered")]),
        durability=DurabilityOptions(durable_inbox=True),
    )
    await agent.use(MailboxPlugin(), box)
    session = await agent.session()
    session.mailbox_address = "alice"
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="retry-me"))

    failed = [event async for event in session.run("first")]
    recovered = [event async for event in session.run("second")]

    assert any(event.type == "error" for event in failed)
    assert box.released == 1
    assert not any(
        event.type == "user" and event.subtype == "notification" for event in (*failed, *recovered)
    )
    assert sum("retry-me" in _texts(message) for message in session.full_history) == 1


@pytest.mark.parametrize("configured", [True, False])
async def test_spawned_worker_address(configured: bool) -> None:
    from linch.coordination.mailbox import InMemoryMailbox
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent
    from tests.support.providers import ScriptedProvider, TextTurn

    mailbox = InMemoryMailbox() if configured else None
    agent = await _agent(ScriptedProvider([TextTurn(text="done")]), mailbox=mailbox)
    parent = await agent.session()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="child task",
            display_name="worker-b",
            subagent_run_id="sa_mail" if configured else "sa_nomail",
            retain=True,
        )
    )

    child = agent._sessions[result.child_session_id]
    assert child.mailbox_address == ("worker-b" if configured else None)


async def test_agent_sends_to_peer_via_tool_during_turn() -> None:
    from linch.coordination.mailbox import InMemoryMailbox
    from tests.support.providers import ScriptedProvider, TextTurn, ToolUseTurn

    box = InMemoryMailbox()
    provider = ScriptedProvider(
        [
            ToolUseTurn(
                tool_name="send_message",
                tool_input={"to": "bob", "content": "do the thing"},
            ),
            TextTurn(text="sent"),
        ]
    )
    agent = await _agent(provider, mailbox=box)
    session = await agent.session()
    session.mailbox_address = "alice"

    [event async for event in session.run("go")]

    drained = await box.drain("bob")
    assert len(drained) == 1
    assert drained[0].sender == "alice"
    assert drained[0].content == "do the thing"
