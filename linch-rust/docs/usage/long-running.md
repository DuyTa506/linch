# Long-running agents

An agent that works for hours does not fail the way a chat does. Its context
fills and is compacted, its server restarts, a new session starts with an
empty conversation, and a model that is told "continue" has to find out what
"continue" means. This page says what to keep **outside** the conversation so
none of that loses the work, how to steer a run while it works, and how to
test the whole thing.

Everything here is built from pieces the other pages describe; the only
additions are `task_ledger` ([Tasks](./tasks.md#a-ledger-that-outlives-sessions))
and `Agent.steer` ([Runs](./runs.md#steer)).

## What differs from a short run

| Short run | Long run |
|---|---|
| The prompt is the goal | The prompt scrolls out of the context, or is summarised |
| The conversation is the plan and the progress | Compaction rewrites the conversation; a new session starts empty |
| A crash means start over | A crash is routine; the run resumes and must not redo finished work |
| "Done" is what the model says | The model says "done" early, or never |
| You wait for the result | You may need to correct the run while it works |

## Four things kept outside the context

Put each of these somewhere compaction cannot reach. Each has a home in this
SDK.

### 1. The goal contract: a system section

Write what "done" means as a system section. A session sends its system
blocks first with every request, byte for byte as its first request did (they
are the session's frozen prefix, logged once with its first prompt), and
compaction rewrites only the messages after them, so it cannot summarise the
goal away. A resumed run sends the same blocks, and a fresh session sends the
sections of the config it is started under.

```python
config: AgentConfig = {
    "spec": {
        "model": "deepseek-flash",
        "system": {
            "sections": [{
                "name": "goal",
                "text": "The report is done when report.md has one section "
                        "per input file and every section cites its file.",
                "order": 0,
            }],
        },
    },
    ...
}
```

The sections are part of the run contract, so a run started under one goal is
not resumed under another ([Agent config](./agent.md#the-run-contract)).
A `context_budget` caps only what hooks add to a request, never a section.

### 2. The progress ledger: the task list

Tasks are rows in the session store, not messages, so they survive
compaction. By default they belong to one session; a new session starts with
none. Set `task_ledger` and every session of the agent shares one list, and
each request carries the open tasks as extra context:

```python
"tasks": True,
"task_ledger": {"list": "report-ledger"},
```

The model sees `#2 [in_progress] Summarise b.txt` lines and a count of the
finished ones, however empty its conversation is, and keeps them current with
`TaskUpdate`. The block is read again for every request, never logged, and
bounded by `max_chars`.

### 3. The artifacts: files, not messages

What the run produced belongs in files: the report so far, notes, a scratch
list. With `filesystem` on and Postgres stores, every session of the
namespace shares one virtual filesystem, so a later session reads what an
earlier one wrote. Tell the model, in the goal section, which file is the
deliverable and that it should write to it as it goes. Offloading
([Agent config](./agent.md)) already moves large tool results there.

### 4. The event journal: what happened

The journal is the record of what ran: every tool call, every result, every
steer. It is what you read to check the work, and what a resumed run reads to
know what it already did (a tool whose start was recorded is never run
again). Read it with `agent.events(run_id)`; do not ask the model what it did.

## An independent "done" check

A model that judges its own work says "done" too early. Check from outside the
loop, on durable facts: the ledger has no open task, the deliverable file
exists and has the shape the goal names, the journal shows the calls the goal
requires. Run the check in your own code after the run ends, and if it fails,
start the next session with the failure as the prompt. Where a host has
`spec.policy` and a verification step, use that as the check; the pattern is
the same: the verdict comes from something other than the model's own last
message.

## Steering

`agent.steer(run_id, "Use tabs, not spaces.")` sends text to a run that is
going on. The run reads it at the next turn boundary, as a user message of its
own, and a run that would have ended takes one more turn to act on it. The
steer is durable before the call returns, a resumed run reads it exactly once,
and a run waiting for an approval takes it too. Details, limits and errors:
[Runs](./runs.md#steer).

Use it for what a person in the loop would say: "skip file c", "the deadline
moved", "stop and summarise". Do not use it as a second prompt channel for
other agents: a model's own `send_message` goes to a mailbox and is
framed as a peer message, a steer is yours.

## The harness pattern

Test a long-run setup by breaking it, and assert on what is durable, not on
what the model said:

1. Start the run on Postgres stores, with a very low compaction threshold so
   compaction fires every turn, and a task that needs several steps (build a
   markdown report from three text files).
2. SIGKILL the server in the middle. A killed server never releases its lease,
   so use the shortest lease (`lease_ttl_ms: 1000`) and retry the resume until
   it is free.
3. Resume on a new server, then start a **fresh session** for the next piece
   of work.
4. When it ends, read the durable state: the ledger rows (every task
   `completed`), the files (the report has three sections), and the journal (no
   tool ran twice; each file read once; the compaction events are there).

The tests in `python/tests/test_long_haul.py` (scripted provider, Postgres)
and `python/tests/live/test_long_haul_vllm.py` (a real model, skipped without
`LINCH_LIVE_VLLM_URL`) are runnable examples.

## Honest limits

- A steer waits for the step in progress. The call answers once the run has
  written the steer, which happens at its next step boundary, so a long model
  call or tool batch delays the answer (and the delivery) by that long.
- Memory stores have no crash safety. The ledger, the files and a steer
  survive a restart only on Postgres.
- The ledger is a list, not a plan. The model must keep it current; nothing
  checks that its updates are true. That is what the independent check is for.
- The ledger block spends context on every request (bounded by `max_chars`).
  A very long list is shown as a count past the bound.
- Compaction summarises the conversation, so details the model learned only
  in conversation can be lost. Anything it must keep belongs in the ledger or
  a file.
- A steer goes to the server that drives the run. Another server refuses it
  with `RunLeased`; there is no routing.

## Related pages

- [Tasks](./tasks.md): the task tools and the ledger.
- [Runs](./runs.md): steer, resume, approve, what is durable.
- [Memory](./memory.md): context injected into requests.
- [Agent config](./agent.md): `filesystem`, `spec.system` and compaction.
