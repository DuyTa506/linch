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
- The loop guard ends a stuck run before its next provider call
  ([below](#the-loop-guard)): Python's repeated-call and repeated-failure
  checks, on by default, and a bound on turns without a provider call that
  is always on (`loop_guard.rs`, `runner/end.rs:156-170`).
- Hook points are the twenty-two in the [kernel](./kernel.md#hook-chains),
  thirteen of which only observe. `before_provider_call` can rewrite or
  block the request about to be sent, and `after_provider_call` can rewrite
  or block the response a call just assembled, before it is staged, or
  accept it and take another turn instead of acting on it (Python's `retry`
  and `force_continue` there). Python's `before_final_answer` is not a
  separate point: `stop` can rewrite the run's final-answer text (not the
  assistant message, already appended by the time `stop` runs), block the
  stop outright, or retry the answer once per run call (Python's
  `before_final_answer` `retry`); its `continue` is Python's stop
  `force_continue`. Python's `force_continue` at `before_provider_call`
  skips the call: the feedback is logged as a stop feedback is and the
  run takes another turn, bounded by `max_turns` and by the loop guard's
  `max_idle_turns`. Python also fires
  that point on a resumed response whose tool calls have no results yet,
  answering them with the feedback (`src/linch/loop/runner.py:767-783`); a
  Rust resume of that response goes on to its tool calls and never comes
  back to the point, so that case does not arise. `event_emit`, one of
  the observe-only points, fires once per event about to be delivered to the
  caller, but only for an event that is both streamed and durably logged —
  never for a hook's own record or a stream-only delta.
- A suspended run waits in the store, so it can be cancelled there
  (`Agent::cancel`, [below](#cancel-a-suspended-run)). Python's permission
  and `AskUser` waits run inside the live run (status `waiting_permission`):
  `session.abort()` ends them with `AbortError` (`src/linch/scheduler.py:1335`,
  `src/linch/tools/ask_user.py:323-326`) and the abort terminal
  (`src/linch/loop/runtime.py:968-1006`); another process can only mark such
  a run failed through the store (`mark_failed`, `src/linch/run_store.py:709`).
- A subagent's run cannot suspend: a call that needs approval is denied
  (`runner/batch.rs:73-75`).

## One turn

`runner.rs:1-39` lists the turn. In order:

1. **Compact** when `spec.compaction` is set and the provider view is due
   ([compaction](./compaction.md)): clear old tool results when `micro` is
   set and that is enough, else summarize. With it off, a turn starts at
   dispatch (`turn.rs:313`).
2. **Check limits.** A cancelled token aborts the run. `max_turns` and the
   budget end it as failed before the call is made (`turn.rs:271-284`).
3. **Build the request** from the session's provider view. The
   `context_build` hooks add blocks to the tail of the request only
   (`turn.rs:285-295`, `runner/hooks.rs:117-129`). They are never logged.
   `before_provider_call` then runs on the built request: a rewrite replaces
   it before anything downstream sees it; a block ends the run as failed and
   the provider is never called (`hooks/call.rs`, `runner/hooks.rs`). A
   `force_continue` skips steps 4-8: nothing is staged for the call, the
   feedback is staged as a `notification` and held as `ToolResults`, and
   step 9 appends it, as after a `stop` feedback.
4. **Stage and flush the delivery.** The `context_build` event and the
   model-call delivery are staged. The `ProviderPending` checkpoint is held,
   then `flush` writes the events and the checkpoint in one commit
   (`turn.rs:299-305`). This is the barrier before the provider is called.
5. **Drive the call.** Stream deltas go to the caller at once as stream-only
   events (`turn.rs:306-317`), unless the spec's redaction covers responses:
   then none are sent, since a match can span two deltas. The call settles with one receipt, whether it
   was accepted, failed or cancelled (`provider/call.rs:1-9`, `43-55`).
   When an `after_provider_call` hook is attached, an accepted receipt and
   its `Usage` are written at once, with `ProviderPending` re-saved at the
   totals that count them, before the hooks run, `provider_call_stop`
   included (`turn.rs:369-380`, `runner/hooks.rs`, `checks_responses`).
   A hook may be a client callback with no timeout; a crash while it runs
   keeps the paid usage ([below](#a-crash-between-a-receipt-and-its-response)).
   The response itself is not written yet: an `after_provider_call` rewrite
   (a redaction, say) must be the only version that reaches the log.
6. **Write the response through.** The spec's redaction, when it covers
   responses, scrubs the assembled response's text and unsigned reasoning
   first (`redaction.rs`, `Runner::after_provider_call` in
   `runner/hooks.rs`), so the hooks, the log and the next request see only
   the scrubbed text; it runs even with no hook attached, and again on a
   hook's rewrite. `after_provider_call` runs on the
   assembled response next: a rewrite replaces it before it is staged; a
   block ends the run as failed and nothing is staged (`hooks/assembly.rs`,
   `runner/hooks.rs`). On success the run then stages the `Assistant` event,
   and the `Usage` event unless step 5 already wrote it, and saves
   `AssistantReceived`, which writes through with the receipt when step 5
   did not (`turn.rs:422-466`, `journal.rs:192-193`). A hook's retry
   rides that same write as the checkpoint's `retry_feedback`, so the paid
   response and what to do with it are durable together. The caller sees
   those events only after this commit (`journal.rs:25-28`).
7. **Append the response to the session** (`turn.rs:395-397`).
8. **Branch on the tool calls** of the response (below).
9. **Append the tool results**, run `post_turn`, and start the next turn
   (`turn.rs:217-226`).

The observe-only points thread through these same steps: `agent_start` fires
once, right after the prompt is accepted and before the first turn
(`runner/start.rs`); `turn_start` opens step 1 of every turn
(`runner/turn.rs`); `provider_call_start`/`provider_call_stop` bracket step 5
(`runner/turn.rs`); `pre_compact`/`post_compact` bracket a compaction that
actually runs, before step 1 (`runner/compact.rs`); `tool_use_start`/
`tool_use_stop` bracket a tool call that actually runs, and
`post_tool_use_failure` fires beside `post_tool_use` when its result is an
error (`runner/sink.rs`, `scheduler/execute.rs`); `agent_stop` fires once, at
the very end of every run, success or failure (`runner/end.rs`).
`subagent_start`/`subagent_stop` fire the same way on the parent's hooks,
around a subagent child run (`subagents/run.rs`, `subagents/background.rs`).
None of them can change what they observe.

A call that fails ends the run as failed with one terminal write that carries
the receipt, the usage, the `Error`, the status and the `Result`
(`turn.rs:337-347`), unless the spec's `provider_retry` or `fallback_models`
take a retryable failure: then the run goes back to step 4 with the next
delivery `attempt`, after a `provider_retry` write and a wait, or at once to
a fallback model with a `model_fallback` event
([providers](./providers.md#retries-and-fallback-models), `runner/retry.rs`).
A `context_length` failure, the provider refusing the prompt as longer than
the model's context window, is never retried; with `spec.compaction` set,
`overflow` shortens the view (micro first, then a forced summary, bounded by
`max_forced`) and the run goes back to step 4 with the turn's next delivery
`attempt` and no `provider_retry` event; when nothing can shorten it, the run
ends failed ([context-length recovery](./compaction.md#context-length-recovery),
`turn.rs:464`, `runner/compact.rs:268-295`). A response the output limit cut off before it held any
text or a whole tool call is not an answer: the session gets nothing, and the
run ends the same way (`turn.rs:322-333`, `runner/end.rs:76-90, 130-139`).

With `spec.truncation_recovery`, a text answer the output limit cut off
(stop reason `max_tokens`, no tool call) is appended and answered with a
feedback message instead of ending the run, while `max_attempts` lasts; the
run's final text joins the parts
([cut-off answers](./compaction.md#cut-off-answers), `turn.rs:532-608`).

### The loop continues while a response has tool calls

`received` collects the `tool_use` blocks of the response (`turn.rs:388-392`).

- **Retried by `after_provider_call`**, the response is appended but neither
  of the branches below runs: none of its tool calls is planned, and `stop`
  is not asked. The next user message is the hook's feedback, built as
  Python's `_primary_control_message`: without tool calls, the feedback in a
  `<system-reminder>`; with them, an error result per call, the first one
  carrying the feedback and the others "Tool not executed; revise and
  resubmit your final answer.", so the provider history stays paired
  (`runner/hooks.rs`, `retry_message`). It is staged as a `notification`
  and held as `ToolResults`, then the next turn starts. Only `max_turns`
  bounds these retries, as in Python.
- **With tool calls**, the run goes to `Step::Batch` (`turn.rs:416-421`).
  Then `Step::Results` appends the results and starts the next turn. The
  provider's `stop_reason` does not decide this; the blocks do.
- **Without**, the run first waits for any background subagent it has, which
  can add a notification and another turn (`turn.rs:433-435`,
  `runner/inbox.rs:137-168`). With an output schema (`spec.output_schema`,
  or the run's own from its first `init` event) the answer is checked next
  (`runner/structured.rs`, `structured.rs`). A failing one, while
  `structured_output_retries` lasts, stages a `verification` event
  (`verifier: "output_schema"`, `action: "retry"`) and the feedback as a
  `notification`, held as `ToolResults`, so both are in the same commit
  before the session append; the next turn starts. The count of retries is
  read back from those events on resume, so a crash never resets it. Past
  the bound the run ends failed in one terminal write: `verification` with
  `action: "stop"`, an `Error` named `StructuredOutputError`, and a `Result`
  with `structured_error`. A resume from `AssistantReceived` checks the same
  stored answer again and stages the same retry; no paid response is bought
  twice. An answer that satisfies the schema (or any answer, without one)
  then meets the spec's verifiers (`verification.rs`, `Runner::verify` in
  `runner/structured.rs`) through the same steps, on their own budget,
  `verification.max_retries`, counted from every durable `verification`
  retry but the schema's. A `retry` verifier past it stages `exhausted` and
  the answer goes on; a `stop` verifier ends the run failed with a
  `VerificationError`. Then the `stop` hook chain runs, on a valid answer only and with the
  parsed `structured_output` in its request: a hook can keep
  the run going with feedback (`continue`, bounded only by `max_turns`), ask
  for another answer (`retry`, honoured once per run call, or with a
  `verification` spec up to its `max_retries`, each staged as a
  `verification` event under the hook's policy id; past the bound it
  counts as an allow and the later hooks decide), rewrite the final-answer
  text (the value `post_turn` and the terminal `Result` see next), or block
  the stop outright, ending the run failed (`turn.rs:436-440`,
  `runner/hooks.rs`). Feedback from either reaches the model as Python's
  does, in a `<system-reminder>` user message staged as a `notification`,
  and another turn starts. With an output schema a rewrite is checked again,
  and one that fails it ends the run failed without asking the model again;
  the answer after a `retry` or `continue` is checked from the start, on its
  own budget. When every hook lets it stop, `post_turn` runs and the run
  ends `completed` with that text, and the parsed value as the result's
  `structured_output` (`turn.rs:441-448`).

`max_turns` is the stop condition for the loop: the call that would exceed it
is not made and the run fails with `TurnLimitError` (`runner/end.rs:138-154`).

### The loop guard

`spec.loop_guard` (`loop_guard.rs:66`) ends a run that is stuck, with
`max_turns` unset as well. It is on by default; the default is left off the
wire, so it does not change the contract of a spec that does not set it.

| Check | Default | Trips | Checked |
|---|---|---|---|
| `max_identical_tool_calls` | 3 | The same call with the same result this many times in the run: one tool, the same input and the same result content (both canonical JSON), not an error, and not a tool that polls | At `Step::Results`, after the batch's results are appended (`turn.rs:303`) |
| `max_consecutive_failures` | 3 | This many tool batches in a row whose every call failed; a batch with one success starts again | Same |
| `max_idle_turns` | 25 | One turn more than this, in a row, without a provider call: a `before_provider_call` `force_continue` | In the `force_continue` branch of dispatch, before its feedback is staged (`turn.rs:415`) |

0 turns one of the first two off; `max_idle_turns` is at least 1, so the
idle bound is always on. Such a turn never reaches the budget and appends
to the session every time, so without the bound a hook that always skips
the call loops until the run is cancelled.

A trip is one terminal write: the `loop_guard` event (`reason`
`repeated_tool_call`, `repeated_failures` or `idle_turns`, `action` `stop`),
an `Error` named `LoopGuardError` with the event's `detail`, and a failed
`Result` with `stop_reason` `error` (`end.rs:156-170`). A tool trip comes
after the results are in the session, so it stays paired; no `post_turn`
runs for that turn.

A repeat is the same call with the same result. A call whose result differs
from every earlier identical call's made progress (a file read again after
an edit, a status that moved on) and starts a count of its own; going back
to an earlier result is a repeat of that result. An error result is never a
repeat, because Linch itself asks the model to repeat some refused calls (a
deferred tool: `call tool_search with select:X first`); calls that keep
failing are the failure streak's. A tool whose `polls()` is true
(`tool.rs:279`; `BashOutput`, which reads a background command until it
ends) is never a repeat either; its failures still count toward the streak.
Python counts identical inputs whatever the result, failed and refused
calls included. Python's `force_final_answer` (one more turn with no tools)
is not ported: a trip stops.

Nothing is checkpointed for the guard. `Watch::of` (`loop_guard.rs:145`)
folds the run's durable events: an `assistant` event names the calls of its
response (less those of tools that poll, which the runner reads from the
registry), the next `tool_result` user event counts those that did not fail
by their result content and moves the failure streak, and a primary
`model_call_delivery` records its turn. The live run folds the same events as it stages them
(`Runner::stage_seen`, `turn.rs:350`), and a resume folds the stored ones
(`start.rs:473`). Each is durable before the step that checks it (the
results with the `ToolResults` checkpoint, a delivery before its call), and
the turn comes from the checkpoint, so a crash at any write resumes with
the counts the run had and trips at the same point
(`tests/e2e_loop_guard.rs`).

## Barriers and group commit

A `Journal` holds one run's events and checkpoint in memory and writes them
with `RunStore::commit` at barriers (`journal.rs:1-33`). The count per turn and
the Postgres write paths are in
[stores](./stores.md#group-commit-write-counts). This section is about when a
write happens.

`Journal::save` applies the phase rule (`journal.rs:190-208`):

| Phase | Write | Status |
|---|---|---|
| `Started`, `AssistantReceived`, `Compacted`, `Aborting` | Through, at once | unchanged |
| `AwaitingApproval` | Through, at once | `awaiting_approval` |
| `AwaitingInput` | Through, at once | `awaiting_input` |
| `ProviderPending`, `CompactionPending`, `Cleared`, `ToolBatch`, `ToolResults`, `Finishing` | Held | unchanged |

A held checkpoint is a snapshot: it covers only the events pending when it
was saved (`journal.rs:76-80`, `201-202`). Every committed state is one that
writing each step through would also have produced (`journal.rs:25-28`).

The caller places the barriers, and the run does it in these places:

| Barrier | Call | Where |
|---|---|---|
| Before a provider dispatch | `Journal::flush` | `turn.rs:303-305` |
| After an accepted call, before the hooks that follow it (only with an `after_provider_call` hook) | `flush`, with `ProviderPending` re-saved | `turn.rs:373-380` |
| Before a retry's wait | `flush`, with `ProviderPending` re-saved | `runner/retry.rs:69-72` |
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
  (`turn.rs:460`), or, when an `after_provider_call` hook is attached, on
  its own with `ProviderPending` re-saved at the totals that count it,
  before the hooks (`turn.rs:373-380`). A compaction summary writes through with `Compacted`
  (`compact.rs:134-147`). A summary call that gave no text saves
  `CompactionPending` and flushes at once, so a resume does not buy it again
  (`compact.rs:20-24, 114-120`). A failed call the run retries saves
  `ProviderPending` again, at the totals that count its receipt, and flushes
  with its `provider_retry` event before the wait (`runner/retry.rs:69-72`).
- A tool end. It is flushed right after the tool returns, from
  `tool_finished`, so a resume finds the result and does not run the tool
  again (`sink.rs:179-181`, `journal/reconcile.rs:316-320`).
- A suspend. `AwaitingApproval` and `AwaitingInput` write through with
  their status (`journal.rs:196-197`).

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

The `event_emit` hook point fires right before each of these `deliver` calls,
once per envelope in that batch, in order (`runner/hooks.rs`). Since the
caller never sees a delta, tool progress, or a hook's own record this way
either, `event_emit` never sees them: it fires only for an event that was
both staged for the caller and just made durable.

## Tool batch

`Step::Batch` plans, then suspends or runs (`runner/batch.rs:29-118`).

1. **Plan.** `scheduler::plan` decides every call without running anything,
   under the run's cancel token (`batch.rs:58-72`). A cancel there ends the
   run as a store cancel does ([below](#cancel-a-suspended-run)): an aborted
   result for each call without one, `Aborting`, the session append, then
   the terminal, so the session stays paired. The hook records of each
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
and 0 counts as 1 (`agent/spec.rs:55-57`, `execute.rs:137`).

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
(`batch.rs:155-183`). That write-through sets the status
`awaiting_approval` (`journal.rs:195`). The stream ends with `Suspended`
instead of `Result`. The run then waits in the store with status
`awaiting_approval`; only a claim takes it back to `running`
([stores](./stores.md#run-store)). A failed write takes the park back
(`batch.rs:217-229`).

An `AskUser` call ([tools](./tools.md#askuser)) suspends the same way once
no call of the batch still needs approval: the plan marks it
`PlannedAction::AwaitingInput`, and `suspend_for_input` stages one
`input_request` per call (its questions) and a `suspended` event, then saves
`AwaitingInput` with the pending ids, which sets `awaiting_input`
(`batch.rs:188-217`). The other calls of that batch wait with it. A child
run cannot wait for the user: its `AskUser` calls are refused with an error
result before the batch runs (`batch.rs:76`).

### Resume and approve

`Agent::resume` continues a run after a crash, a lost lease or a dropped
stream. `Agent::approve` answers the pending calls of a suspended run and
continues it, and `Agent::answer` answers its pending `AskUser` calls
(`agent.rs:272-332`). All three enter `continue_run`
(`runner/start.rs:175-333`):

1. A token that is already cancelled does nothing: no claim, no write
   (`start.rs:185-187`).
2. `check` refuses a terminal run, a run of another contract, an approve
   on a run that is not awaiting approval, and an answer on one that is not
   awaiting input (`start.rs:534-563`). `check_answers` refuses an answer
   that does not fit the latest `input_request` questions with
   `InvalidAnswer` (`start.rs:565`). It runs before the claim, so a
   mistaken call does not fence a live owner, and again after it
   (`start.rs:189-207`, `414-439`).
3. `claim` takes the lease and adds 1 to the epoch, which fences out the
   previous owner ([stores](./stores.md#fencing)); a child run is seized
   instead (`start.rs:233-247`). On a store whose claims expire, a run
   another holder keeps alive is refused with `RunLeased` before anything
   is written, and from here on the run future renews the lease and
   releases it if the run stops without settling
   ([leases](./stores.md#leases), `runner/lease.rs`).
4. `reconcile` reads the checkpoint, the events and the session log, and
   returns what to do next. It is pure (`journal/reconcile.rs:149-169`).
5. A new `init` event with `resumed: true` is staged. It rides the first
   barrier (`start.rs:284-296`).
6. For an approve, the host's answers are recorded as decisions and committed
   with a `ToolBatch` checkpoint before the run goes on, so a crash after
   that keeps them (`start.rs:259-261, 329-331`, `batch.rs:199-214`).
   For an answer, each answered call's `tool_call_start` and
   `tool_call_end` (the rendered answer) are staged after the `init` and
   committed with a `ToolBatch` checkpoint before the run goes on
   (`start.rs:220`, `306-330`). The model then reads a durable result, as
   for any tool that ran. No `pre_tool_use`, `post_tool_use` or
   `tool_use_start`/`tool_use_stop` hook sees an answer.

What `reconcile` makes of each phase (`journal/reconcile.rs:170-231`):

| Checkpoint phase | Recovery | The run continues with |
|---|---|---|
| `Started` | `Started` | Append the prompt unless it is in the session, then the turn |
| `ProviderPending` | `Redispatch` | A cancelled receipt for a delivery with no receipt, then attempt + 1; attempt + 1 at once after a failed receipt (a retry's wait cut short, or a failover), or after an accepted receipt whose response was never written (an orphan, below), on the model and with the retries its `provider_retry` and `model_fallback` events say |
| `AssistantReceived` | `Received` | Append the response unless it is in the session, then branch on its tool calls; with `retry_feedback`, send that feedback instead, without asking the hook again |
| `ToolBatch` | `ToolBatch` | Each call: its durable result, a placeholder, or a re-plan |
| `AwaitingApproval` | `AwaitingApproval` | Re-plan with the recorded decisions; suspend again if one still needs an answer |
| `AwaitingInput` | `AwaitingInput` | With an answer, its results, then the rest of the batch; without one (a resume), suspend again with the same pending ids |
| `ToolResults` | `ToolResults` | Append the message unless it is in the session, then the next turn |
| `CompactionPending` | `Recompact` or `Redispatch` | Another summary attempt, or the primary call when the summary call already settled without a summary |
| `Compacted` | `Compacted` | Append the projection unless it is in the session, then dispatch |
| `Aborting` | `Aborting` | Append the cancel's results unless they are in the session, then the `aborted` terminal ([below](#cancel-a-suspended-run)) |
| `Finishing` | `Finishing` | Append the results that pair a final tool call unless they are in the session, then the terminal its `ending` holds ([below](#the-final-tool)) |

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

A retried approve or answer gets `NotSuspended`: once it has committed the
run is in `ToolBatch`, and the host continues it with `resume`
(`agent.rs:262-273`, `start.rs:432-437`).

### Cancel a suspended run

A suspended run has no stream, so no cancel token reaches it.
`Agent::cancel` (`agent.rs`) ends it through the store: it enters
`continue_run` with `Reply::Cancel` (`runner/start.rs`), and the steps
differ from an approve's:

1. `check` takes a run whose phase is `AwaitingApproval`, `AwaitingInput`
   or `Aborting` (a cancel cut short), under any contract: the cancel runs
   nothing of the agent's. Any other run is `NotSuspended` and a terminal
   one `Terminal`; `Agent::cancel` turns both into `false`.
2. `claim`, as for an approve. After the claim the cancel does not check
   the phase again: the claim fenced out whoever drove the run since the
   check, so the cancel ends the run from wherever it stands. Only a run
   that ended since is `Terminal`.
3. `reconcile`, then `cancel::step` (`runner/cancel.rs`) picks what the
   session still owes. The runner gets a token already cancelled, so a
   `Dispatch` or `Compact` step stages its abandoned receipt and aborts
   before any call.
4. `Step::Abort` pairs the session, then ends the run. For the calls of the
   last response without a durable end (every call of a suspended batch,
   read from the events after the response) it stages a `tool_call_start`
   and a `tool_call_end` with `ABORTED` ("Tool call aborted",
   `scheduler/execute.rs`), or the end of the interrupted placeholder for a
   call that started, then the `user` event of the results, and saves
   `Aborting` with that message: one write-through. It then appends the
   message to the session (the barrier finds nothing held) and calls
   `abort`, whose `end_with` stops the run's background children and
   commits the `Result` (subtype `aborted`) with status `aborted`.

A cancel of an `AwaitingApproval` run so costs four writes: the claim,
the `Aborting` commit, the session append, the terminal commit. A crash
before the second leaves the run suspended (a `running` status with the
suspended phase, which a cancel or an approve still takes); after it, the
cancel is durable: `reconcile` turns `Aborting` into `Recovery::Aborting`,
and a resume or another cancel appends the message unless it is there and
ends the run, while an approve or an answer gets `NotSuspended`. A results
message held in `ToolResults` is saved as `Aborting` first too; a session
already paired (the claim found the run past its batch) is ended at once.

Two continuations racing on one suspended run both pass `check`; the later
claim moves the epoch and fences out the earlier one. A cancel that claims
last wins: the approve's next write fails with `LeaseLost`, or its check
after the claim finds the run `aborted`. An approve that claims last wins:
the cancel's first write fails with `LeaseLost`, or, once the approve ended
the run, the cancel's check after the claim returns `Terminal`. On
Postgres a claim is refused while another holder's lease lives
(`RunLeased`), so a cancel never takes a run an approve elsewhere drives;
once that approve committed its decisions, the run is no longer suspended
and the cancel returns `false` without claiming. The tests are
`crates/linch-core/tests/e2e_cancel_suspended.rs` (both interleavings, and
a crash before and after each of the four writes) and
`crates/linch-postgres/tests/pg/cancel.rs`.

### A crash between a receipt and its response

With an `after_provider_call` hook attached, an accepted receipt is
durable before the hooks run (`provider_call_stop` included), and the
response only after them. A crash in between leaves `ProviderPending` (at the totals that
count the receipt), an `accepted` receipt and its `usage` event, and no
`assistant` event. That receipt is an orphan:

- It keeps the `accepted` label it was written with. The log is
  append-only, so nothing relabels it; an orphan is an accepted receipt with
  no `assistant` event before the next delivery of its logical call. A turn
  therefore has one answered accepted receipt, plus at most one orphan per
  crash in that window.
- It stays in the totals. The checkpoint already counts its usage and cost,
  the budget is rebuilt from every durable receipt (`Spent::of`), and its
  `usage` event is in the log, so the result's `total_usage` and
  `total_cost_usd` are the sum of every receipt, the orphan's included.
- The call goes again. `reconcile` returns `Redispatch` with the next
  attempt and no abandoned receipt (`journal/reconcile.rs:251-268`), so the
  call gets a new delivery attempt id under the same logical call id. No
  `provider_retry` event is written, so it spends none of the turn's
  retries (`Standing::of` counts only those). Under a capped budget the
  orphan's cost may leave no room for the call: the resumed run then ends on
  the budget, which is correct, since the money was spent.

The response is not kept, so `after_provider_call` runs again on the new
response. Python does the same: it settles a usage-only receipt as soon as
the stream ends (`src/linch/loop/streaming.py:498`), before its
`after_provider_call` hooks run (`src/linch/loop/provider_turn.py:370`,
`453`) and before the `primary_append` projection that records the
response (`provider_turn.py:466`). On resume a receipt with no such
projection recovers no prefix and the call is dispatched again
(`src/linch/loop/resume.py:243-300`), with the receipt restored into the
ledger and its totals (`src/linch/model_calls.py:870`).
Without an `after_provider_call` hook nothing changes, a
`provider_call_stop` observer or not: the receipt rides `AssistantReceived`
as before, byte for byte (pinned by
`without_hooks_the_receipt_rides_the_response_write`,
`crates/linch-core/tests/e2e_durable_receipt.rs`).

## Terminals

Every terminal goes through `Runner::end`: it cancels and awaits the run's
child runs, stages their final status, builds the `Result` and calls
`Journal::finish` (`runner/end.rs:24-58`).

| Terminal | Status | When |
|---|---|---|
| `Result` success | `completed` | A text-only response, after `stop` and `post_turn` (`turn.rs:407-414`) |
| Abort | `aborted` | The cancel token fired (`end.rs:61-64`), or `Agent::cancel` ended a suspended run (`runner/cancel.rs`) |
| Hook failed closed | `failed` | A `stop` hook failed closed (`end.rs:66-74`) |
| Output limit | `failed` | The response held no text or whole tool call (`end.rs:76-90`) |
| Turn limit | `failed` | `max_turns` reached, with a `loop_guard` event (`end.rs:138-154`) |
| Loop guard | `failed` | The loop guard tripped, with a `loop_guard` event and a `LoopGuardError` (`end.rs:156-170`, [above](#the-loop-guard)) |
| Budget | `failed` | The budget admits no more calls (`end.rs:110-122`) |
| Provider error | `failed` | The call failed, and no retry or fallback model took it (`turn.rs:343-346`, `runner/retry.rs`) |

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
(`journal/tests/turn.rs:116`). With an `after_provider_call` hook
attached, the response step is two writes: the
receipt and `Usage` with `ProviderPending` before the hooks, then
`Assistant` with `AssistantReceived` after them, so the turn costs `4 + 2k`.

## The final tool

A run may have a final tool (`structured.rs`, `FinalTool`): the agent's
`spec.final_tool_name`, or one made from the output schema when the
provider reports no native structured output
(`Provider::native_output_schema`). `Runner::request` (`runner/catalog.rs`)
then leaves `output_schema` off, appends the made tool after the others, and
forces it when it is the only tool and the provider takes a forced choice
with the request's thinking (`Provider::tool_choice_refusal`).

`received` hands a logged response that calls it to `final_call`
(`runner/final_tool.rs`) instead of planning a batch, so no tool event is
written. While the run decides, `Runner::unpaired` holds the response:

- Feedback (a schema failure, a verifier's or a `stop` hook's retry) goes
  through `send_back`, which turns it into the response's results (the
  final call's carries the feedback, any other `Tool not executed`) held as
  `ToolResults`, so the next turn starts paired.
- Every terminal goes through `end_with`, which, with a response still
  unpaired, builds its results (the acknowledgement of an answer taken, the
  schema error, or `Final answer not taken`), stages their `user` event,
  holds them with the `Ending` (status, stop reason and answer) as
  `Finishing`, appends them (the `flush_checkpoint` barrier commits the
  checkpoint and the staged `verification` and `error` events first), then
  writes the terminal.

A crash before that barrier resumes from `AssistantReceived` and decides
again: the schema check is deterministic and the retries spent are counted
from durable events. A crash after it resumes from `Finishing`, which only
appends and ends. `crates/linch-core/tests/e2e_tool_choice.rs` crashes
every write of a good answer, a retried one and one past its retries.

