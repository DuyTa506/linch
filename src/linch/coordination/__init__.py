"""Coordination primitives — driving the loop from a clock or a peer.

The whole SDK is the *harness* (``Agent = Model + Harness``): the loop, tools,
memory, context, verification, and persistence around the model. This package is
not "the harness" — it is one optional capability *within* it: ways to advance
the agent from a source other than a direct user turn.

* **Scheduling** (``scheduling/``) — a dependency-free cron/interval primitive
  plus the ``schedule_*`` tools that let an agent enqueue its own future work.
  The embedder runs a :class:`SchedulerLoop` over a :class:`ScheduleStore`; a
  fired schedule advances the loop from a *clock*.
* **Mailbox** (``mailbox/`` + ``send_message``) — a peer-addressable message
  substrate for multi-agent *teams* (peers message each other, not just
  parent↔child), with a :class:`Correlator` for request/response handshakes; a
  delivered message advances the loop from a *peer*.

Everything here is opt-in: with ``Agent(schedule_store=None, mailbox=None)``
(the defaults) nothing is registered and the loop is byte-identical. All public
names are also re-exported from the top-level ``linch`` package, so importing
``from linch import Schedule`` is unchanged — this package is internal
organization, not a new public path.
"""

from __future__ import annotations

from .mailbox import Correlator, InMemoryMailbox, Mailbox, MailboxMessage
from .scheduling import (
    InMemoryScheduleStore,
    Schedule,
    SchedulerLoop,
    ScheduleStore,
    SqliteScheduleStore,
    cron_matches,
    next_cron_time,
    render_schedule_message,
    schedule_tools,
    validate_cron,
)
from .send_message import SendMessageTool

__all__ = [
    "Correlator",
    "InMemoryMailbox",
    "Mailbox",
    "MailboxMessage",
    "InMemoryScheduleStore",
    "Schedule",
    "SchedulerLoop",
    "ScheduleStore",
    "SqliteScheduleStore",
    "cron_matches",
    "next_cron_time",
    "render_schedule_message",
    "schedule_tools",
    "validate_cron",
    "SendMessageTool",
]
