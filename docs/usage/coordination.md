# Coordination

> **A note on "harness."** In the agent literature, *harness* means everything
> around the model — `Agent = Model + Harness` — i.e. the tools, memory, context
> engineering, verification, persistence, and the loop itself. By that
> definition the **whole linch SDK is the harness**. This page is *not* "the
> harness"; it is one optional capability *within* it.

`linch.coordination` retains the in-process mailbox capability for peer
messages. Core stays single-conversation and user-driven.

The mailbox is an opt-in primitive; the host supplies the policy for interpreting
peer messages. Durable recurring work uses `linch.worker` and PgQueuer; see
[Scale out](./production.md#scale-out).

> Mailbox names are re-exported from the top-level package. The
> `linch.coordination.*` path is internal organization, not a separate import
> surface.

For the *implement-your-own* side of these seams (durable stores, a git-worktree
isolation backend, a Redis mailbox), see [Extending](./extending.md).

---

## Mailbox — multi-agent teams

The core subagent model is parent→child: a worker runs once and returns a
conclusion. `linch.coordination.mailbox` adds the other half — **peers that address
each other** and exchange messages across turns. Opt in with the mailbox plugin;
the `send_message` tool registers:

```python
from linch import Agent, InMemoryMailbox, MailboxPlugin

box = InMemoryMailbox()
agent = Agent(model="gpt-5")
await agent.use(MailboxPlugin(), box)   # registers send_message and inbox
```

Two mechanisms:

**Message bus.** A session with a `mailbox_address` receives its inbox at the top of
each turn (through the same next-turn notification seam). One agent addresses a peer with
`send_message(to=..., content=..., type=...)`; the peer picks it up on its next
run as a `<peer-message>` `UserEvent`. Spawned workers use a unique mailbox address
when the agent has a mailbox: the worker ID exposed by the Subagent tool, or the
child session ID for direct runner calls. Display names are labels only.

```python
lead = await agent.session();   lead.mailbox_address = "lead"
worker = await agent.session(); worker.mailbox_address = "alice"
# lead's turn calls send_message(to="alice", ...) → alice drains it next run
```

**Request/response protocol.** A `Correlator` is a pending→resolved state machine
keyed by `request_id`. It is **non-blocking by design** — a turn-based agent can't
block awaiting a peer, so it opens a request, continues, and checks later:

```python
from linch import Correlator, MailboxMessage

c = Correlator()
c.open("req-001")                                    # requester registers
await box.send(MailboxMessage(sender="alice", recipient="lead",
                              content="Approve my plan?", request_id="req-001"))
# ... responder replies, echoing the id in in_reply_to ...
c.resolve(MailboxMessage(sender="lead", recipient="alice",
                         content="Approved", in_reply_to="req-001"))
assert c.is_resolved("req-001")                      # requester checks on a later turn
```

`InMemoryMailbox` is the only built-in mailbox. Messages live for the current
process (one agent, many workers), without restart survival or cross-process
delivery. Mailbox functionality moves to `linch.ext` in WP10. Hosts needing
external infrastructure can implement the `Mailbox` protocol — see
[Extending](./extending.md).

Host adapters supporting durable inbox mode use the optional `ClaimableMailbox` lease protocol instead
of destructive drain: claim → enqueue by `mailbox:<message-id>` → acknowledge.
A crash before enqueue permits reclaim; a crash after enqueue is deduplicated by
the session inbox receipt. Legacy `send()`/`drain()` behavior remains unchanged.

The generic Context inbox seam distinguishes uncommitted `Message` values from
already history-committed `Event` values. Register producers on the agent's
context through a plugin-owned `ctx.inbox(...)`; the loop consumes them in
registration order at the same next-turn boundary as notifications.

**When to reach for it:** long-lived teammates that negotiate (plan approval,
graceful shutdown) or self-organize. For one-shot delegation, plain
parent→child subagents are simpler — use those.

Runnable: [`examples/coordination/team_mailbox.py`](../../examples/coordination/team_mailbox.py).

---

Back to the [Usage guide index](./README.md).
