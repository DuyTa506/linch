# Coordination

> **A note on "harness."** In the agent literature, *harness* means everything
> around the model — `Agent = Model + Harness` — i.e. the tools, memory, context
> engineering, verification, persistence, and the loop itself. By that
> definition the **whole linch SDK is the harness**. This page is *not* "the
> harness"; it is one optional capability *within* it.

`linch.coordination` is about **advancing the loop from a source other than a
direct user turn**: from a *clock* (scheduling) or from a *peer* (mailbox /
multi-agent teams). Core stays single-conversation and user-driven; this is the
opt-in layer for scheduled and multi-agent work.

These are deliberately **thin primitives, not implementations**. The SDK ships
the mechanism; *you* supply the policy (what a fired schedule does, what a peer
message means). Everything here is **opt-in and zero-overhead when unused** —
with the defaults (`schedule_store=None`, `mailbox=None`) nothing is registered
and the loop is byte-identical.

> All public names are re-exported from the top-level package, so
> `from linch import Schedule, Mailbox` is unchanged. The `linch.coordination.*`
> path is internal organization, not a separate import surface.

For the *implement-your-own* side of these seams (durable stores, a git-worktree
isolation backend, a Redis mailbox), see [Extending](./extending.md).

---

## Scheduling — produce work on a schedule

`linch.coordination.scheduling` is a dependency-free cron/interval primitive plus the
tools that let an agent enqueue its *own* future work. Pass a store and three
tools register automatically:

```python
from linch import Agent, InMemoryScheduleStore, SchedulerLoop

agent = Agent(model="gpt-5", schedule_store=InMemoryScheduleStore())
# Auto-registers: CreateSchedule, ListSchedules, CancelSchedule
```

The round trip has two halves — the agent schedules, **the embedder fires**:

1. The agent calls `CreateSchedule(payload=..., cron="0 9 * * *")` (a 5-field UTC
   cron expression) or `CreateSchedule(payload=..., interval_s=3600)`.
2. You drive a `SchedulerLoop` over the same store. `loop.start()` ticks once a
   second; each due schedule fires its `payload` into the session's
   `pending_notifications` — the same channel background workers use.
3. The next `session.run()` drains the payload as a `<scheduled-task>`
   `UserEvent` at the top of the turn, so the agent acts on it as if a user asked.

```python
loop = SchedulerLoop(store, session)
loop.start()                 # background asyncio task for the process lifetime
# ... or tick by hand in tests / bounded runs:
fired = await loop.tick()    # returns the schedules that fired this tick
```

`Schedule` takes exactly one of `cron` / `interval_s`; the cron expression is
validated at creation, so an invalid expression is rejected before it can fire.
`SqliteScheduleStore` makes schedules durable across restarts and supports atomic
due claims: if two `SchedulerLoop`s share the same SQLite database, only one loop
claims and fires a due schedule for a given tick. A `ScheduleEvent` is emitted on
each fire for observers.

**When to reach for it:** the agent itself should set up recurring work
("check CI every 30 min", "summarize the inbox at 9am"). If your *application*
already owns a scheduler (cron, k8s `CronJob`, Celery beat), drive `session.run`
from that instead — you don't need this.

Runnable: [`examples/coordination/scheduling_agent.py`](../../examples/coordination/scheduling_agent.py).

---

## Mailbox — multi-agent teams

The core subagent model is parent→child: a worker runs once and returns a
conclusion. `linch.coordination.mailbox` adds the other half — **peers that address
each other** and exchange messages across turns. Opt in with `Agent(mailbox=...)`
and the `send_message` tool registers:

```python
from linch import Agent, InMemoryMailbox

box = InMemoryMailbox()
agent = Agent(model="gpt-5", mailbox=box)   # registers send_message
```

Two mechanisms:

**Message bus.** A session with a `mailbox_address` drains its inbox at the top of
each turn (exactly like `pending_notifications`). One agent addresses a peer with
`send_message(to=..., content=..., type=...)`; the peer picks it up on its next
run as a `<peer-message>` `UserEvent`. Spawned workers are auto-addressed by their
`display_name` when the agent has a mailbox.

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

`InMemoryMailbox` is in-process (one agent, many workers). For cross-process teams
or restart survival, use `SqliteMailbox`:

```python
from linch import Agent, SqliteMailbox

box = SqliteMailbox("coordination.db")
agent = Agent(model="gpt-5", mailbox=box)
```

`SqliteMailbox` preserves FIFO order per recipient and makes `drain()` destructive
inside one SQLite transaction, so two workers draining the same inbox do not both
receive the same message. For Redis/SQS or other infrastructure, implement the
same `Mailbox` protocol — see [Extending](./extending.md).

**When to reach for it:** long-lived teammates that negotiate (plan approval,
graceful shutdown) or self-organize. For one-shot delegation, plain
parent→child subagents are simpler — use those.

Runnable: [`examples/coordination/team_mailbox.py`](../../examples/coordination/team_mailbox.py).

---

Back to the [Usage guide index](./README.md).
