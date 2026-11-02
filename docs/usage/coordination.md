# Coordination

Use mailbox coordination when agents need to exchange peer messages across turns.

`linch.coordination` provides an opt-in in-process mailbox for peer messages.
The host decides what peer messages mean.

Durable recurring work uses `linch.worker` and PgQueuer; see
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
`send_message(to=..., content=..., type=...)`; the peer receives it at the next
eligible turn boundary, during the current `session.run()` if another turn remains
or during a later run, as a `<peer-message>` `UserEvent`. Spawned workers use a
unique mailbox address when the agent has a mailbox: the worker ID exposed by the
Subagent tool, or the child session ID for direct runner calls. Display names are
labels only.

```python
lead = await agent.session();   lead.mailbox_address = "lead"
worker = await agent.session(); worker.mailbox_address = "alice"
# lead's turn calls send_message(to="alice", ...) → alice receives it at the next
# eligible turn boundary, in this run or a later one
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
Python process and agents, sessions, and workers can share the mailbox object;
messages have no restart survival or cross-process delivery. Hosts needing
external infrastructure can implement the `Mailbox` protocol — see
[Extending](./extending.md).

When `DurabilityOptions(durable_inbox=True)` is enabled, a mailbox implementing
`ClaimableMailbox` uses leases instead of destructive drain: claim → enqueue by
`mailbox:<message-id>` → commit the inbox/live log → acknowledge. A crash before
acknowledgement permits reclaim; a crash after enqueue is deduplicated by the
session inbox receipt. The session store must provide the durable inbox methods
as well. Without durable inbox mode, delivery uses `send()` and destructive
`drain()`.

The generic Context inbox seam distinguishes uncommitted `Message` values from
already history-committed `Event` values. Register producers on the agent's
context through a plugin-owned `ctx.inbox(...)`; the loop consumes them at the
same next-turn boundary as notifications, preserving registration order within
each context.

**When to reach for it:** long-lived teammates that negotiate (plan approval,
graceful shutdown) or self-organize. For one-shot delegation, plain
parent→child subagents are simpler — use those.

Runnable: [`examples/coordination/team_mailbox.py`](../../examples/coordination/team_mailbox.py).

---

Back to the [Usage guide index](./README.md).
