# Turn lifecycle

> Part of the [linch-rust architecture guide](./README.md).

One run is a state machine in `crates/linch-core/src/runner/`. A run is a
future that the caller drives by polling a `RunStream`. Each turn dispatches
one provider call, appends the response to the session, and, when the
response has tool calls, plans and runs them. Run state is written to the run
store in groups, at barriers. Use this page to trace one run from `Agent::run`
to its terminal, to place a new write relative to a barrier, or to see what a
resume does from each checkpoint.

Python Linch has the same loop ([turn lifecycle](../../../docs/architecture/turn-lifecycle.md)).
What differs in Rust:

- The loop is explicit: a `Step` enum, one variant per place a run can resume
  from (`crates/linch-core/src/runner/turn.rs:33-73`). A resume reads the
  durable checkpoint and picks the step; it does not replay the loop.
- The run advances only while its stream is polled. Dropping the stream drops
  the run where it stands, like a crash (`runner.rs:83-87`).
- Python's loop guard (repeated calls, repeated failures) is not in the
  runner. It emits `loop_guard` only for `max_turns` (`runner/end.rs:93-108`).
- Hook points are the seven in the [kernel](./kernel.md#hook-chains). Python's
  `AfterProviderCall` is gone (`hooks.rs:19-21`).
- A subagent's run cannot suspend: a call that needs approval is denied
  (`runner/batch.rs:73-75`).

## One turn

`runner.rs:1-39` lists the turn. In order:

1. **Compact** when `spec.compaction` is set and the provider view is due
   ([compaction](./compaction.md)). With it off, a turn starts at dispatch
   (`turn.rs:237-248`).
2. **Check limits.** A cancelled token aborts the run. `max_turns` and the
   budget end it as failed before the call is made (`turn.rs:271-284`).
3. **Build the request** from the session's provider view. The
   `context_build` hooks add blocks to the tail of the request only
   (`turn.rs:285-295`, `runner/hooks.rs:117-129`). They are never logged.
4. **Stage and flush the delivery.** The `context_build` event and the
   model-call delivery are staged. The `ProviderPending` checkpoint is held,
   then `flush` writes the events and the checkpoint in one commit
   (`turn.rs:299-305`). This is the barrier before the provider is called.
5. **Drive the call.** Stream deltas go to the caller at once as stream-only
   events (`turn.rs:306-317`). The call settles with one receipt, whether it
   was accepted, failed or cancelled (`provider/call.rs:1-9`, `43-55`).
6. **Write the response through.** On success the run stages the `Assistant`
   and `Usage` events and saves `AssistantReceived`, which writes through
   with the receipt (`turn.rs:353-380`, `journal.rs:192-193`). The caller sees
   those events only after this commit (`journal.rs:25-28`).
7. **Append the response to the session** (`turn.rs:395-397`).
8. **Branch on the tool calls** of the response (below).
9. **Append the tool results**, run `post_turn`, and start the next turn
   (`turn.rs:217-226`).

A call that fails ends the run as failed with one terminal write that carries
the receipt, the usage, the `Error`, the status and the `Result`
(`turn.rs:337-347`). A response the output limit cut off before it held any
text or a whole tool call is not an answer: the session gets nothing, and the
run ends the same way (`turn.rs:322-333`, `runner/end.rs:76-90, 130-139`).

### The loop continues while a response has tool calls

`received` collects the `tool_use` blocks of the response (`turn.rs:388-392`).

- **With tool calls**, the run goes to `Step::Batch` (`turn.rs:416-421`).
  Then `Step::Results` appends the results and starts the next turn. The
  provider's `stop_reason` does not decide this; the blocks do.
- **Without**, the run first waits for any background subagent it has, which
  can add a notification and another turn (`turn.rs:398-401`,
  `runner/inbox.rs:137-168`). Then the `stop` hook chain runs: a hook can keep
  the run going with feedback, which is appended as a user message and starts
  another turn (`turn.rs:402-406`, `runner/hooks.rs:176-219`). When every hook
  lets it stop, `post_turn` runs and the run ends `completed`
  (`turn.rs:407-414`).

`max_turns` is the stop condition for the loop: the call that would exceed it
is not made and the run fails with `TurnLimitError` (`runner/end.rs:93-108`).

## Barriers and group commit

A `Journal` holds one run's events and checkpoint in memory and writes them
with `RunStore::commit` at barriers (`journal.rs:1-33`). The count per turn and
the Postgres write paths are in
[stores](./stores.md#group-commit-write-counts). This section is about when a
write happens.

`Journal::save` applies the phase rule (`journal.rs:190-208`):

| Phase | Write | Status |
|---|---|---|
| `Started`, `AssistantReceived`, `Compacted` | Through, at once | unchanged |
| `AwaitingApproval` | Through, at once | `awaiting_approval` |
| `ProviderPending`, `CompactionPending`, `ToolBatch`, `ToolResults` | Held | unchanged |

A held checkpoint is a snapshot: it covers only the events pending when it
was saved (`journal.rs:76-80`, `201-202`). Every committed state is one that
writing each step through would also have produced (`journal.rs:25-28`).

The caller places the barriers, and the run does it in these places:

| Barrier | Call | Where |
|---|---|---|
| Before a provider dispatch | `Journal::flush` | `turn.rs:303-305` |
| Around a compaction summary call | `flush` before the call; `flush` or the `Compacted` write-through after it | `runner/compact.rs:94-96, 116-118, 143-147` |
| Before each tool that really runs | `ToolSink::tool_starting` flushes | `scheduler/execute.rs:39-45`, `runner/sink.rs:137-140` |
| After each tool that really ran | `tool_finished` flushes | `sink.rs:142-182` |
| Before every session-store write | `Journal::flush_checkpoint` | `runner/write.rs:96-103`, `journal.rs:220-231` |
| The end of the run | `Journal::finish` | `journal.rs:233-257`, `runner/end.rs:55` |

This is the rule that run state is durable before the next side effect: the
provider is called, a tool starts, the session store is written, or the run
ends only after every pending event and checkpoint is durable. Never defer a
write past a barrier. Three kinds of write do not wait for the next barrier:

- A paid receipt. An accepted call writes through with `AssistantReceived`
  (`turn.rs:370-374`). A compaction summary writes through with `Compacted`
  (`compact.rs:134-147`). A summary call that gave no text saves
  `CompactionPending` and flushes at once, so a resume does not buy it again
  (`compact.rs:20-24, 114-120`).
- A tool end. It is flushed right after the tool returns, from
  `tool_finished`, so a resume finds the result and does not run the tool
  again (`sink.rs:179-181`, `journal/reconcile.rs:316-320`).
- A suspend. `AwaitingApproval` writes through with its status
  (`journal.rs:195`).

A call that runs no tool (served by a hook, refused, denied or aborted) has
its start and end staged without a flush. They reach the store with the next
barrier (`sink.rs:3-7`, `179-181`). Hooks are not barriers
(`runner/hooks.rs:1-9`).

`Journal::finish` commits the held state first, then the terminal status with
the `Result` event. With nothing held, the pending events, the `Result` and the
status go in one write, so a crash never leaves a failed receipt with its
`Error` but a `running` status (`journal.rs:233-257`).

A failed write poisons the journal: every later write fails with
`JournalError::Poisoned`, and nothing more reaches the store. The run keeps
its last durable state and a resume recovers it (`journal.rs:30-32`,
`285-295`).

The caller sees an event only after its commit. `Outbox::deliver` queues the
envelopes a commit made durable and yields once, so the stream hands them over
before the run goes on (`runner.rs:278-298`). Deltas and tool progress skip
this: they go out at once and carry the last durable seq (`runner.rs:74-80`,
`runner/sink.rs:184-199`).

## Tool batch

`Step::Batch` plans, then suspends or runs (`runner/batch.rs:29-118`).

1. **Plan.** `scheduler::plan` decides every call without running anything,
   under the run's cancel token (`batch.rs:58-72`). The hook records of each
   call go to the caller (`batch.rs:77-80`). The decisions are recorded for
   the checkpoint (`batch.rs:81-83`). The steps of `plan` are in the
   [kernel](./kernel.md#tool-call-seams).
2. **Suspend or run.** If any call awaits approval, the run suspends and the
   batch is not executed (`batch.rs:84-87`). Otherwise it saves `ToolBatch`
   (held, with `after_seq` = the last staged seq) and executes (`batch.rs:88-94`).
3. **Collect.** The results, any peer notes and catalog notices become one
   user message, staged as a `user` event with subtype `tool_result`. The run
   saves `ToolResults` (held). `Step::Results` then appends the message,
   which first flushes that checkpoint (`batch.rs:99-118`, `turn.rs:217-226`).

### Only read work runs in parallel

`execute` groups calls (`scheduler/execute.rs:137-151`). A call joins a group
only if the first call of the group is a read call, the group is under
`max_tool_concurrency`, and the call is a read call too, or runs no tool
(`execute.rs:155-163`). Every other call runs alone. `try_join_all` runs a
group's calls together, and results come back in call order.

A read call is one whose `Tool::parallel(input)` is true. The default is true
only for `Read` scope (`crates/linch-core/src/tool.rs:34-36, 250-257`). The
`Subagent` tool is `Exec` scope and says true only for a foreground call of a
named type whose definition lists its tools and every one of them is a
registered `Read` tool. Anything unknown fails closed
(`crates/linch-core/src/subagents/tool.rs:124-146`). The default limit is 4,
and 0 counts as 1 (`agent/spec.rs:54-56`, `execute.rs:137`).

Calls in one group overlap, so the `ToolSink` must accept concurrent calls
(`execute.rs:33`). The journal sink takes turns on a mutex held across each
flush (`sink.rs:38-40`). A sink error stops the batch: this tool and every
later one do not run, and running tools in the group are cancelled
(`execute.rs:39-45`).

## Suspend and resume

### Suspend

A call that still needs approval after the `permission_request` chain
suspends the run. `suspend` stages a `permission_request` event and a
`suspended` event, parks the subagent family (if the agent has subagents) so
its children outlive the stream, and saves `AwaitingApproval` with the pending ids
(`batch.rs:147-183`). That write-through sets the status
`awaiting_approval` (`journal.rs:195`). The stream ends with `Suspended`
instead of `Result`. The run then waits in the store with status
`awaiting_approval`; only a claim takes it back to `running`
([stores](./stores.md#run-store)). A failed write takes the park back
(`batch.rs:177-182`).

### Resume and approve

`Agent::resume` continues a run after a crash, a lost lease or a dropped
stream. `Agent::approve` answers the pending calls of a suspended run and
continues it (`agent.rs:253-288`). Both enter `continue_run`
(`runner/start.rs:175-333`):

1. A token that is already cancelled does nothing: no claim, no write
   (`start.rs:185-187`).
2. `check` refuses a terminal run, a run of another contract, and an approve
   on a run that is not awaiting approval. It runs before the claim, so a
   mistaken call does not fence a live owner, and again after it
   (`start.rs:189-207`, `414-439`).
3. `claim` takes the lease and adds 1 to the epoch, which fences out the
   previous owner ([stores](./stores.md#fencing)) (`start.rs:196-205`).
4. `reconcile` reads the checkpoint, the events and the session log, and
   returns what to do next. It is pure (`journal/reconcile.rs:149-169`).
5. A new `init` event with `resumed: true` is staged. It rides the first
   barrier (`start.rs:284-296`).
6. For an approve, the host's answers are recorded as decisions and committed
   with a `ToolBatch` checkpoint before the run goes on, so a crash after
   that keeps them (`start.rs:259-261, 329-331`, `batch.rs:199-214`).

What `reconcile` makes of each phase (`journal/reconcile.rs:170-231`):

| Checkpoint phase | Recovery | The run continues with |
|---|---|---|
| `Started` | `Started` | Append the prompt unless it is in the session, then the turn |
| `ProviderPending` | `Redispatch`, or `Received` with an accepted receipt | A cancelled receipt for a delivery with no receipt, then attempt + 1; or the durable response |
| `AssistantReceived` | `Received` | Append the response unless it is in the session, then branch on its tool calls |
| `ToolBatch` | `ToolBatch` | Each call: its durable result, a placeholder, or a re-plan |
| `AwaitingApproval` | `AwaitingApproval` | Re-plan with the recorded decisions; suspend again if one still needs an answer |
| `ToolResults` | `ToolResults` | Append the message unless it is in the session, then the next turn |
| `CompactionPending` | `Recompact` or `Redispatch` | Another summary attempt, or the primary call when the summary call already settled without a summary |
| `Compacted` | `Compacted` | Append the projection unless it is in the session, then dispatch |

Each tool call of a recovered batch recovers on its own
(`reconcile.rs:309-332`):

- A call with a durable end reuses that result (`Done`).
- A call that started but has no end gets an error result that says it was
  interrupted, and the tool is not run again (`Interrupted`,
  `reconcile.rs:20-22`). The run stages the placeholder as that call's end
  (`batch.rs:44-57`). A `Subagent` or `SubagentContinue` call is the
  exception: it is planned again and resumes its durable child run
  (`batch.rs:120-141`).
- A call that never started is planned again, and asks for permission again
  unless a recorded decision covers it (`Replan`).

A recorded allow covers only the input the host saw, and a recorded deny
always denies (`scheduler.rs:59-66`). Reconcile fails when the session log
does not fit the checkpoint (`journal/reconcile/session.rs:9-22`).

A retried approve gets `NotSuspended`: once an approve has committed its
answers the run is in `ToolBatch`, and the host continues it with `resume`
(`agent.rs:262-273`, `start.rs:432-437`).

## Terminals

Every terminal goes through `Runner::end`: it cancels and awaits the run's
child runs, stages their final status, builds the `Result` and calls
`Journal::finish` (`runner/end.rs:24-58`).

| Terminal | Status | When |
|---|---|---|
| `Result` success | `completed` | A text-only response, after `stop` and `post_turn` (`turn.rs:407-414`) |
| Abort | `aborted` | The cancel token fired (`end.rs:61-64`) |
| Hook failed closed | `failed` | A `stop` hook failed closed (`end.rs:66-74`) |
| Output limit | `failed` | The response held no text or whole tool call (`end.rs:76-90`) |
| Turn limit | `failed` | `max_turns` reached, with a `loop_guard` event (`end.rs:92-108`) |
| Budget | `failed` | The budget admits no more calls (`end.rs:110-122`) |
| Provider error | `failed` | The call failed (`turn.rs:343-346`) |

A terminal status is final in the store ([stores](./stores.md#run-store)).
`Agent::fail` marks a run that can never continue (a contract mismatch, a
state that does not reconcile) as `failed` (`agent.rs:290-327`).

## Example trace

A turn with one write tool, `k` = 1, costs `3 + 2k` = 5 run writes and 2
session writes (`runner.rs:36-37`):

| Step | Write |
|---|---|
| Dispatch | Run: delivery, `context_build` and `ProviderPending` flushed (1) |
| Response | Run: receipt, `Assistant`, `Usage` and `AssistantReceived` through (2) |
| Append the response | Session (1) |
| Tool start | Run: `ToolBatch` and `tool_call_start` flushed (3) |
| Tool end | Run: `tool_call_end` flushed (4) |
| Results | Run: `ToolResults` flushed before the session write (5); session (2) |

The table is the shape pinned by
`a_tool_turn_costs_three_plus_two_k_run_writes_and_two_session_writes`
(`journal/tests/turn.rs:116`).
