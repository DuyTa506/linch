"""Loop wiring for the mailbox: the send_message tool + drain-into-next-turn.

A session with a ``mailbox_address`` drains its inbox at the top of each turn
(exactly like ``pending_notifications``); the ``send_message`` tool lets the
agent address a peer. Opt-in via ``Agent(mailbox=...)`` — with no mailbox the
tool is absent and no drain runs (byte-identical).

linch imports happen inside the test bodies/helpers because sibling tests pop
``linch*`` modules from ``sys.modules``.
"""

from __future__ import annotations

from typing import Any


def _agent(provider: Any, *, mailbox: Any = None, **kwargs: Any) -> Any:
    from linch import Agent
    from linch.sessions import InMemorySessionStore

    return Agent(
        model="m",
        provider=provider,
        session_store=InMemorySessionStore(),
        permissions={"mode": "skip-dangerous"},
        cwd=".",
        mailbox=mailbox,
        **kwargs,
    )


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


async def test_mailbox_registers_send_tool() -> None:
    from linch.coordination.mailbox import InMemoryMailbox
    from linch.evals import ScriptedProvider, TextTurn

    agent = _agent(ScriptedProvider([TextTurn(text="ok")]), mailbox=InMemoryMailbox())
    assert agent.tools.get("send_message") is not None


async def test_no_mailbox_means_no_send_tool() -> None:
    from linch.evals import ScriptedProvider, TextTurn

    agent = _agent(ScriptedProvider([TextTurn(text="ok")]))
    assert agent.tools.get("send_message") is None


# --- drain into next turn ----------------------------------------------------


async def test_pending_mailbox_message_drains_into_next_turn() -> None:
    from linch.coordination.mailbox import InMemoryMailbox, MailboxMessage
    from linch.evals import ScriptedProvider, TextTurn

    box = InMemoryMailbox()
    agent = _agent(ScriptedProvider([TextTurn(text="done")]), mailbox=box)
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


async def test_no_drain_without_address() -> None:
    from linch.coordination.mailbox import InMemoryMailbox, MailboxMessage
    from linch.evals import ScriptedProvider, TextTurn

    box = InMemoryMailbox()
    agent = _agent(ScriptedProvider([TextTurn(text="done")]), mailbox=box)
    session = await agent.session()
    # No mailbox_address set → nothing is drained even though a message exists
    # for some other address.
    await box.send(MailboxMessage(sender="bob", recipient="alice", content="hello"))

    events = [event async for event in session.run("go")]

    assert not any(e.type == "user" and "hello" in _texts(e.message) for e in events)
    # Message remains undelivered in the box.
    assert len(await box.drain("alice")) == 1


async def test_spawned_worker_gets_its_display_name_as_address() -> None:
    from linch.coordination.mailbox import InMemoryMailbox
    from linch.evals import ScriptedProvider, TextTurn
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent = _agent(ScriptedProvider([TextTurn(text="done")]), mailbox=InMemoryMailbox())
    parent = await agent.session()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="child task",
            display_name="worker-b",
            subagent_run_id="sa_mail",
            retain=True,
        )
    )

    child = agent._sessions[result.child_session_id]
    # The worker is now addressable by peers under its display_name.
    assert child.mailbox_address == "worker-b"


async def test_worker_address_unset_without_mailbox() -> None:
    from linch.evals import ScriptedProvider, TextTurn
    from linch.subagents.default_agent import DEFAULT_AGENT
    from linch.subagents.runner import RunSubagentArgs, run_subagent

    agent = _agent(ScriptedProvider([TextTurn(text="done")]))  # no mailbox
    parent = await agent.session()

    result = await run_subagent(
        RunSubagentArgs(
            parent_session=parent,
            parent_agent=agent,
            definition=DEFAULT_AGENT,
            prompt="child task",
            display_name="worker-b",
            subagent_run_id="sa_nomail",
            retain=True,
        )
    )

    child = agent._sessions[result.child_session_id]
    assert child.mailbox_address is None


async def test_agent_sends_to_peer_via_tool_during_turn() -> None:
    from linch.coordination.mailbox import InMemoryMailbox
    from linch.evals import ScriptedProvider, TextTurn, ToolUseTurn

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
    agent = _agent(provider, mailbox=box)
    session = await agent.session()
    session.mailbox_address = "alice"

    [event async for event in session.run("go")]

    drained = await box.drain("bob")
    assert len(drained) == 1
    assert drained[0].sender == "alice"
    assert drained[0].content == "do the thing"
