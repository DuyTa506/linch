# Directed workflows

A directed workflow is a Python function that runs your fixed plan: fan out
to agents, check what comes back, hand one step's typed result to the next.
The server journals every finished call in Postgres, so a crash of the
client or of the server resumes the same `run_id` and no finished call is
paid for twice.

[← Usage guide](./README.md)

## When to use it

Use a workflow when you know the **shape** of the job and want a program to
own it: a signing or approval pipeline, a fixed review fan-out, recon, then
one check per service, then a report. The control flow is your code; the
model does the work inside each call.

Use an open-ended agent when the model should decide the shape. Those
agents are served by what is already here, so a workflow does not repeat it:
[steering](./long-running.md) a live run, the [task ledger](./tasks.md),
[compaction](./memory.md) for context that outgrows the window, and
[schedules](./scheduler.md) for heartbeats and background runs.

| Job | Reach for |
|---|---|
| The steps are known, some are irreversible, a crash must not repeat paid work | a workflow |
| One agent works for hours on a goal it plans itself | a long-running agent ([Long-running agents](./long-running.md)) |
| A run that must be started once under a chosen id, from a job queue or an orchestrator | [`run_or_resume`](#run_or_resume) |
| A prompt on a timer | a [schedule](./scheduler.md) |

## An example

```python
from linch_rust import Server, run_workflow, WorkflowContext, Budget

REPORT = {"name": "report", "schema": {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    "required": ["findings"]}}

async def pentest(wf: WorkflowContext) -> dict:
    await wf.phase("recon")
    services = await wf.step("recon", lambda: scan_network())        # JSON, journaled
    await wf.phase("checks")
    notes = await wf.parallel([
        (lambda s=s: wf.agent(f"check {s['name']} on port {s['port']}", label=s["name"]))
        for s in services])
    report = await wf.agent_result(
        "write the report from these notes:\n" + "\n".join(notes), output_schema=REPORT)
    return report.structured

async with Server() as server:
    agent = await server.open_agent(config)           # stores: Postgres
    result = await run_workflow(agent, pentest, run_id="pentest-2026-11",
                                budget=Budget(max_tokens=500_000), max_concurrency=4)
```

Run it again with the same `run_id` after a crash: the recon step and every
finished check replay from the journal, and only the calls that had not
finished run.

## What a workflow function can call

`run_workflow(agent, fn, run_id=...)` runs `fn(wf)` in your process and
returns what it returns. `wf` is a `WorkflowContext`:

| Call | What it does |
|---|---|
| `await wf.agent(prompt, ...)` | One journaled agent call; returns the final text (compact JSON with `output_schema`). `agent_result` returns an `AgentResult` with `text`, `structured`, `session_id`, `tokens`, `cost_usd`, `replayed` |
| `await wf.step(name, fn, key=...)` | Runs `fn` (sync or async, in your process) and journals its JSON value |
| `await wf.interrupt(key, payload)` | Parks the workflow until someone answers; raises `WorkflowSuspended` |
| `await wf.parallel([thunks])` | Concurrent branches, at most `max_concurrency`; input order; the first failure cancels the others |
| `await wf.settled([thunks])` | Like `parallel`, but every branch finishes and each outcome is a `StepOutcome` |
| `await wf.pipeline(items, *stages)` | Each item runs through every stage on its own, with no barrier between stages |
| `await wf.phase(title)` | A progress event for `on_event`; nothing is journaled |
| `wf.budget`, `wf.spent_tokens`, `wf.spent_usd` | The cap and what the drive has spent (replays counted) |

`wf.agent` takes `name`, `subagent`, `label`, `key`, `output_schema`,
`fork`, `timeout_ms` and `retry`. `run_workflow` takes `budget`,
`max_concurrency` (4 when unset; a nested fan-out has its own limit),
`step_timeout_ms`, `deadline_ms`, `resume` (answers to interrupts),
`principal` (below) and `on_event`, which receives `WorkflowEvent`s: `phase`, `agent_start`,
`agent_end`, `agent_replayed`, `step_start`, `step_end`, `step_replayed`,
`interrupt_requested`, `interrupt_resolved`, `interrupt_replayed` and
`interrupt_stale`.

A `budget` with `max_cost_usd` needs a price for every model the agent can
use (its own and each of `fallback_models`, from the built-in table or the
agent's `pricing`), because an unpriced call would count as free and the cap
could never trip. `workflow.start` refuses it (`-32011`, naming the model), and
a call whose run had usage and no cost (a provider that served a model the
pricing lacks) ends the workflow's next agent call with `-32046`, `data.kind:
unpriced` (`WorkflowBudgetExceeded`, `WorkflowError::BudgetUnpriced`); the call
stays journaled, and a resume refuses again. Set `pricing` or cap `max_tokens`
instead. A tenant's `max_cost_usd` ceiling (below) is a `max_cost_usd` too.

`parallel`, `settled` and `pipeline` run in the client. Each branch counts
its own calls, under its branch path, so branches that reach their calls in
another order on a resume still find their own records. A call's `name`,
`key` (a step's `key` is hashed, so it is not bounded) and `branch` are at
most 256 bytes, like the workflow id (`-32602` past it).

## How replay works

A call is identified by a **content key** plus its **occurrence**: how many
calls with that key came before it, counted apart inside each fan-out
branch. A resume replays every call whose key and occurrence the journal
holds. The key is a digest of:

| Call | Key |
|---|---|
| `wf.agent` | `name` (or the subagent's name), the prompt, the `subagent`, the `output_schema`, the `fork` session; with `key=` only the name and the key |
| `wf.step` | the step name and its `key` |
| `wf.interrupt` | the interrupt key only: an answer already journaled replays whatever the payload is, but an answer not journaled yet also needs the payload to match ([below](#an-answer-is-for-the-question-that-was-asked)) |

Editing one prompt makes only that call miss its record; the calls before
and after it, whose keys still match, replay. Renaming a step does the same
for that step.

**Honest limits.** The workflow function runs again from the top on every
resume; only journaled calls replay. So code outside `wf.agent`, `wf.step`
and `wf.interrupt` must be deterministic or idempotent. Branching on a
journaled value is fine (the value replays); branching on the clock,
randomness, the environment or an HTTP answer fetched by bare code is not:
put that inside a `wf.step`. A step's value is JSON; a value that does not
encode fails before it is journaled. A call that was cancelled or that
failed journals nothing and runs again on the next drive. Journaled
agent results are objects (`text`, `structured`, `session_id`, `tokens`,
`cost_usd`): a journal written by an earlier build of the engine is not
read; start a fresh `run_id`.

## Handing one step's result to the next

The next step's input is built by your code from the previous result, so
nothing hidden crosses between calls. There are four ways to carry it.

**Typed handoff.** Give the call an `output_schema`. The answer is checked
against it, journaled, and returned as compact JSON (`text`) and as an
object (`structured`). Build the next prompt from it:

```python
recon = await wf.agent_result("list the exposed services", output_schema=SERVICES)
for service in recon.structured["services"]:
    ...
```

**Large artifacts by path.** Do not put a 2 MB scan in a prompt or in the
journal (a tenant's journal record is capped; see [Admission](#admission)).
Write it to the workspace or the [filesystem backend](./tools.md) and pass
the path and a hash:

```python
path, digest = await wf.step("save-scan", lambda: save_scan(raw))   # (path, sha256)
verdict = await wf.agent(f"read {path} (sha256 {digest}) and list the open ports")
```

The hash lets a later step notice that the file changed under it.

**`fork`.** Pass the `AgentResult` (or its `session_id`) of an earlier call
as `fork=`: the call's run starts on a copy of that session's durable log, so
it sees the same conversation (`Agent::fork_session`). Use it to ask
several follow-up questions about one long context without sending it
again. The fork is part of the call's key. Costs: the copied prefix is
billed as input again, and is cheap only where the provider caches the
prefix; the copy is taken when the call starts, so fork a session that is
finished, not one a live run is still writing.

**Shared files.** Agents in one workspace read each other's files. This
carries anything, but it is the least visible handoff and the least
replayable: a replayed call does not rewrite its files. Prefer a step that
returns the path and hash.

| Handoff | Tokens | Cache reuse | Determinism |
|---|---|---|---|
| `output_schema` result in the next prompt | only the result | none | the journaled value replays exactly |
| path + hash | a few tokens | none | the artifact is outside the journal; the hash detects change |
| `fork` | the whole prefix again, as input | yes, where the provider caches | the source session must not change |
| shared files | whatever the agent reads | none | a replay does not recreate them |

### An irreversible step

Sign, send and pay go through `wf.step`, **after** a durable approval, and
carry an idempotency key:

```python
async def sign_flow(wf: WorkflowContext) -> str:
    draft = await wf.agent("draft the contract", output_schema=CONTRACT)
    if not await wf.interrupt("approve", {"draft": draft.structured}):
        return "withdrawn"
    key = f"contract-{draft.structured['id']}"
    return await wf.step("sign", lambda: signer.sign(draft.structured, idempotency_key=key),
                         key=key)
```

`wf.interrupt` stores the workflow as `awaiting_input` and raises
`WorkflowSuspended`; nothing runs while it waits. Answer by starting the
same id with `resume={"approve": True}` (from any process). The answer is
journaled, so it is never asked again.

### An answer is for the question that was asked

An answer applies only to the question the workflow was parked on: the same
key and an equal payload (compared by canonical digest). If the draft was
edited between the question and the resume, the call asks again with the
payload it has now: the stale answer is dropped, an `interrupt_stale` event reports it,
nothing is journaled, and the workflow is stored as `awaiting_input` with
that question. Answer that one. So answer the interrupt `workflow_status`
names, with the payload the function will rebuild; a resume that carries an
answer for a workflow that was never parked, or for another interrupt than
the stored one (a second interrupt in the same resume), asks again too. If
the server crashed after it took the workflow and before the answer was
journaled, the stored question is gone (the status is `running`): ask and
answer again.

A crash after the signer ran but before the step's record landed makes the
resume run the function again. The journal cannot tell "never ran" from
"ran, then crashed", so the guard is the key: the function is called again
with the **same** `key`, and the signing service must treat a repeated key
as the same request and return the first result. Pass the key to the
service; do not make a fresh one inside the function.

## Admission

A tenant cannot run workflows unless the operator allows them: `allow.workflow` is
absent by default and `workflow.start` is refused (`-32004`, `kind:
workflow`). The server config's [`workflow` section](./server.md#config)
turns them on and sets the caps:

| Cap | Default | What it bounds |
|---|---|---|
| `max_workflows` | 4 | Workflows the tenant drives at once, across its connections (`-32005`, `kind: workflows`) |
| `max_records` | 10000 | Journal records of one workflow, replays included, counting the steps begun and not completed and the distinct calls the connection has keyed (`-32005`, `kind: journal_records`); checked before a call spends, so what the server keeps for a workflow is bounded by it |
| `max_fan_out` | 8 | Agent calls of one workflow in flight at once (`-32005`, `kind: agent_calls_in_flight`); the client lowers its `max_concurrency` to it |
| `max_record_bytes` | 1 MiB | One record's JSON value |
| `max_tokens`, `max_cost_usd` | none | A ceiling on a workflow's `budget`; a workflow with none gets it, and one that asks for more is refused (`-32004`, `kind: workflow_budget`) |

Each live agent call also takes one of the tenant's `max_runs` places.
Each workflow holds one database connection for its hold, so
`max_workflows` bounds those. A workflow's journal key is the agent's
namespace (which must be the tenant's) and the `run_id`, so one tenant
cannot reach another's workflow. A tenant never reads a store's or a run's
own error text: a journal failure answers `the workflow journal failed`.

## Acting for a principal

`run_workflow(..., principal={"tenant": "acme", "user": "ada", "roles":
["support"]})` (`principal` in `workflow.start`) makes every agent call's
run act for that principal, so a permission rule keyed on a role applies to
the workflow's calls as it does to a run started for the same principal. A
tenant's principal must name the tenant, as for `run.start`. Without one the
calls act for nobody, and a principal-scoped feature (the structured
memory) has nothing to reach.

The principal is stored with the workflow when it is first started. Starting
the same id again must name the same principal, whole (tenant, user and
roles; the order of roles does not matter), or the start is refused with
`NotAllowed` (`-32004`, `kind: workflow_principal`) and nothing changes: the
journaled calls ran under that principal's rules, and a replay under others
would hand their results to a user they were not made for. That covers a
principal where there was none, and none where there was one. Start a new
`run_id` to act for someone else.

## One driver per workflow

`workflow.start` takes an exclusive **hold** on `(namespace, run_id)`
(a Postgres session advisory lock on a connection of its own, so a server
that is killed frees it at once; memory stores hold it in the connection)
before it claims anything. A second client gets `WorkflowHeld` (`-32045`)
and the first is untouched. The journal is also fenced by a lease epoch,
which refuses a stale writer's records. `workflow.finish` (sent when
`run_workflow` ends, even if cancelled) frees the workflow; a client that
dies leaves it to its connection ending, so retry a start that gets
`WorkflowHeld` after a crash.

A client that vanishes without closing its connection (a power cut, a
dropped route) ends the same way once the server notices: it sets TCP
keepalive on every accepted socket (30 s idle, 3 probes 10 s apart, so about a
minute) and a TCP user timeout of the same length, pings each WebSocket every
30 s, and ends one that sent nothing for 60 s. Until then the connection keeps
its place among the tenant's `max_connections` and the workflow's hold, so a
replacement start gets `WorkflowHeld` for about a minute; the times are the
[`liveness`](./server.md#liveness) section of the server config. The hold's
own database connection sets the Postgres keepalives (`tcp_keepalives_idle`
30 s, `tcp_keepalives_interval` 10 s, `tcp_keepalives_count` 3), so a server
host that vanishes frees the lock in about a minute and not after the
operating system's default (two hours). A Unix socket needs neither: the
kernel closes it when the peer's process ends.

A killed server leaves the child run of the call it was driving at its last
durable state, with its lease (1 s to minutes, `stores.lease_ttl_ms`). The
resumed call waits the lease out and **continues that run**, so its model
calls are not paid twice.

## `run_or_resume`

`run.start` with a `run_id` that is already stored ends with `RunExists`
(`-32023`, "run_id is already stored"). `run_or_resume` makes the call
idempotent:

```python
result = await run_or_resume(agent, "job-7", "Reconcile the ledger")
```

| State of `job-7` | What happens |
|---|---|
| not stored | the run starts |
| finished | its `ResultEvent` is returned, with no model call |
| waiting for an approval or an answer | its `SuspendedEvent` is returned; approve or answer, then call again |
| unfinished (its process died) | the run is resumed from its journal |
| driven by another process | waits up to `wait` seconds for its lease, then resumes |

Use it from a job queue's task, or from a LangGraph or Prefect node: the
node can be retried any number of times and the run happens once.

## Errors

| Raised | When |
|---|---|
| `WorkflowHeld` (-32045) | Another client drives the workflow ([above](#one-driver-per-workflow)); nothing was written |
| `WorkflowBudgetExceeded` (-32046) | The `budget` is spent (`.spent_tokens`, `.spent_usd`), or `data["kind"] == "unpriced"` (`data["model"]`): a `max_cost_usd` budget on a model with no price. The finished calls stay journaled |
| `WorkflowCallFailed` (-32047) | A `wf.agent` call produced no result; `.kind` is `failed`, `aborted`, `timeout`, `approval` (the child run stopped for an approval) or `invalid` (an unknown subagent) |
| `WorkflowNotFound` (-32048) | A call for a workflow id this connection does not drive: `workflow.start` was never sent for it, or it finished |
| `WorkflowNoJournal` (-32049) | `resume` answers for a workflow whose journal holds nothing |
| `NotAllowed` (-32004) | `kind` `workflow` (no `allow.workflow`), `workflow_budget` (a budget above the ceiling) or `workflow_principal` (another principal than the workflow was started for); a `principal` of another tenant |
| `LimitReached` (-32005) | `kind` `workflows`, `agent_calls_in_flight` or `journal_records` ([Admission](#admission)) |
| `OpenError` (-32011) | `workflow.start` with a `max_cost_usd` budget for an agent with an unpriced model |
| `RunNotFound` (-32021) | `workflow_status` of an id the agent's namespace does not hold |
| `WorkflowSuspended` | Not an error of the server: an interrupt has no answer, and `run_workflow` raises it (a `BaseException`) after the workflow is stored as `awaiting_input` |
| `WorkflowTimeoutError` | A step attempt ran past its limit, or `deadline_ms` passed (client side) |

All the server's are `WorkflowError`s except `NotAllowed`, `LimitReached`,
`OpenError` and `RunNotFound`. `workflow_status(agent, run_id)` returns a
`WorkflowStatus` (`status`, `records`, `interrupt_key`, `interrupt_payload`)
without driving the workflow. `Retry(max_attempts, base_delay_ms,
max_delay_ms)` passed as `retry=` repeats a failed or timed out `wf.agent` or
`wf.step`; only the winning attempt is journaled.

## Differences from Python Linch

| | Python | Here |
|---|---|---|
| Where the function runs | in the process that owns the agent | in the client; the server keeps the journal |
| Call key | sha256 of the subagent type, the prompt, tool filter and run options (`journal.py:40`) | digest of name, prompt, subagent, `output_schema`, `fork`; `key=` replaces them |
| Occurrence and branch | counter per key | the same, counted apart per fan-out branch path |
| `tools=` | a tool filter per call | not accepted: a subagent definition names its tools, and `subagent=` selects it |
| `fork=True` | continue the host's conversation | `fork=<session id or AgentResult>`: continue a named session |
| `isolation=` | a separate working directory per branch | not available |
| `journal_snapshot_every` | snapshots the event-log fold | not needed: the journal is a record table loaded in one query |
| Lease | `RunLease`, five minutes, renewed | a hold plus an epoch; no expiry to wait out for the workflow itself |
| Interrupt answer | `resume=` on the next `run_workflow` | the same; `workflow.status` reads the pending question |
| Budget | one `RunBudget` shared by the tree | `Budget(max_tokens, max_cost_usd)`; checked before each live call, so one running call can overshoot |
| `max_agent_concurrency` | caps live provider calls | `allow.workflow.max_fan_out` for a tenant |
| Cancel | cancel the task | cancel the task: the server aborts that call's run |

## Known gaps

- There is no standalone `workflow.answer`: an answer goes through the next
  `workflow.start`'s `resume`.
- The deadline and step timeouts are enforced in the client; a client that
  dies keeps none of them. An agent call's `timeout_ms` is enforced by the
  server.
- A memory-store agent journals only for the life of its connection: after a
  new connection (or a server restart) the workflow runs again from its first
  step, **steps included**, because the journal is empty. A `workflow.start`
  that carries `resume` answers for a workflow whose journal holds nothing is
  refused (`-32049`, `WorkflowNoJournal`) instead of running everything again
  with answers that have nothing to answer. A start without answers is not
  refused: it is how a workflow starts. Use Postgres stores for a workflow
  that must survive a connection.

The wire methods are in [RPC](../architecture/rpc.md#workflow-methods) and
the engine in [Extensions](../architecture/ext.md#workflows-over-rpc).
